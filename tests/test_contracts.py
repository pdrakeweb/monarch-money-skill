"""Tests for the shared cross-skill contracts (profile namespacing + migration,
read-only cache opener with schema-version guard)."""
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "contracts"))

import profile as P                      # noqa: E402
import cache_contract as C              # noqa: E402


# ---- profile migration ---------------------------------------------------- #
def test_migrate_flat_routes_sections():
    flat = {
        "profile_complete": True,
        "birth_year": 1976,                 # identity
        "swr_pct": 3.5,                     # retirement
        "withdrawal_strategy": "tpaw",      # retirement
        "net_means_mapping": "A",           # finance
        "lumpy_expenses": [{"name": "Car", "amount": 60000,
                            "frequency_years": 8}],  # finance
        "annual_new_savings": 250000,       # retirement
    }
    v2 = P.migrate_flat_to_v2(flat)
    assert v2["schema"] == 2
    assert v2["profile_complete"] is True
    assert v2["identity"]["birth_year"] == 1976
    assert v2["retirement"]["swr_pct"] == 3.5
    assert v2["retirement"]["withdrawal_strategy"] == "tpaw"
    assert v2["retirement"]["annual_new_savings"] == 250000
    assert v2["finance"]["net_means_mapping"] == "A"
    assert v2["finance"]["lumpy_expenses"][0]["name"] == "Car"


def test_migrate_is_idempotent():
    flat = {"birth_year": 1980, "swr_pct": 4.0}
    once = P.migrate_flat_to_v2(flat)
    twice = P.migrate_flat_to_v2(once)
    assert twice == once


def test_load_persists_migration_in_place(tmp_path):
    flat = {"birth_year": 1976, "swr_pct": 3.25, "net_means_mapping": "B"}
    (tmp_path / "profile.json").write_text(json.dumps(flat))
    prof = P.load_profile(str(tmp_path))
    assert prof["schema"] == 2
    # file on disk was upgraded
    on_disk = json.loads((tmp_path / "profile.json").read_text())
    assert on_disk["schema"] == 2
    assert on_disk["identity"]["birth_year"] == 1976
    assert on_disk["retirement"]["swr_pct"] == 3.25


def test_save_section_preserves_siblings(tmp_path):
    P.save_section(str(tmp_path), "finance", {"net_means_mapping": "A"})
    P.save_section(str(tmp_path), "retirement", {"swr_pct": 3.5})
    P.save_section(str(tmp_path), "identity", {"birth_year": 1976})
    prof = P.load_profile(str(tmp_path))
    assert prof["finance"]["net_means_mapping"] == "A"
    assert prof["retirement"]["swr_pct"] == 3.5
    assert prof["identity"]["birth_year"] == 1976


def test_flat_view_merges_sections():
    prof = {"schema": 2, "profile_complete": True,
            "identity": {"birth_year": 1976},
            "finance": {"net_means_mapping": "A"},
            "retirement": {"swr_pct": 3.5}}
    flat = P.flat_view(prof)
    assert flat["birth_year"] == 1976
    assert flat["net_means_mapping"] == "A"
    assert flat["swr_pct"] == 3.5
    assert flat["profile_complete"] is True


def test_missing_profile_returns_empty(tmp_path):
    prof = P.load_profile(str(tmp_path))
    assert prof == P.empty_profile()
    assert prof["profile_complete"] is False


# ---- cache contract ------------------------------------------------------- #
def _make_cache(path, schema_version):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE accounts (id TEXT)")
    conn.execute("CREATE TABLE account_tax_buckets (account_id TEXT, "
                 "tax_bucket TEXT, auto_classified INT)")
    if schema_version is not None:
        conn.execute("INSERT INTO meta VALUES ('schema_version', ?)",
                     (str(schema_version),))
    conn.commit()
    conn.close()


def test_open_cache_missing(tmp_path):
    with pytest.raises(C.CacheUnavailable) as e:
        C.open_cache(str(tmp_path))
    assert "monarch-money" in str(e.value)


def test_open_cache_too_old(tmp_path):
    _make_cache(C.cache_path(str(tmp_path)), schema_version=1)
    with pytest.raises(C.CacheUnavailable) as e:
        C.open_cache(str(tmp_path), min_schema=2)
    assert "schema v1" in str(e.value)


def test_open_cache_ok_readonly(tmp_path):
    _make_cache(C.cache_path(str(tmp_path)), schema_version=2)
    conn = C.open_cache(str(tmp_path), min_schema=2)
    try:
        # read works
        conn.execute("SELECT * FROM accounts").fetchall()
        # write is refused (read-only)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO accounts VALUES ('x')")
            conn.commit()
    finally:
        conn.close()


def test_open_cache_writable_tax_buckets(tmp_path):
    _make_cache(C.cache_path(str(tmp_path)), schema_version=2)
    conn = C.open_cache(str(tmp_path), writable_tax_buckets=True)
    try:
        conn.execute("INSERT INTO account_tax_buckets VALUES ('a','roth',0)")
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM account_tax_buckets").fetchone()[0] == 1
    finally:
        conn.close()


# ---- family shared dir ---------------------------------------------------- #
def test_shared_data_dir_honors_family_env(tmp_path, monkeypatch):
    # The family override resolves the SAME dir regardless of which skill asks.
    monkeypatch.setenv("MONARCH_MONEY_FAMILY_DIR", str(tmp_path))
    a = C.shared_data_dir("/skills/monarch-money")
    b = C.shared_data_dir("/skills/investing")
    assert os.path.abspath(a) == os.path.abspath(b) == os.path.abspath(str(tmp_path))


def test_consumer_opens_cache_producer_wrote(tmp_path, monkeypatch):
    # monarch-money (producer) writes its cache to the shared dir; a consumer
    # with a DIFFERENT skill dir resolves the SAME dir and opens it — the whole
    # point of the family contract (it would miss it if it used its own dir).
    monkeypatch.setenv("MONARCH_MONEY_FAMILY_DIR", str(tmp_path))
    producer_dir = C.shared_data_dir("/skills/monarch-money")
    _make_cache(C.cache_path(producer_dir), schema_version=2)
    consumer_dir = C.shared_data_dir("/skills/personal-finance")
    assert consumer_dir == producer_dir
    conn = C.open_cache(consumer_dir, min_schema=2)   # read-only; raises if missing
    try:
        conn.execute("SELECT * FROM accounts").fetchall()
    finally:
        conn.close()
