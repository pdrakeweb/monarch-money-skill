"""Tests for the shared skeleton machinery. Keep these green in every skill —
they guard the contracts that skeleton updates rely on."""
import os
import re
import time

import pytest

import cli
import skill_schema
from skill_cache import SkillCache
from conftest import SKILL_DIR


@pytest.fixture
def cache():
    c = SkillCache(":memory:", skill_schema.SCHEMA)
    yield c
    c.close()


def _now_row(c, table="example_items", id="x1", age=0):
    c._conn.execute(
        f"INSERT OR REPLACE INTO {table} (id, name, value, fetched_at) "
        "VALUES (?, 'n', 1.0, ?)",
        (id, int(time.time()) - age),
    )
    c._conn.commit()


def test_schema_creation(cache):
    tables = {r[0] for r in cache._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "meta" in tables
    for table in skill_schema.SCHEMA["tables"]:
        assert table in tables, table


def test_ttl_fresh_and_stale(cache):
    _now_row(cache, age=60)
    assert cache.any_fresh("example_items") is True
    cache._conn.execute("UPDATE example_items SET fetched_at = ?",
                        (int(time.time()) - 10 * 3600,))
    cache._conn.commit()
    assert cache.any_fresh("example_items") is False


def test_counter(cache):
    cache.increment_counter()
    cache.increment_counter(n=2)
    assert int(cache.get_meta("api_call_count")) == 3


def test_clear_preserves_protected_rows(cache):
    _now_row(cache)
    cache._conn.execute(
        "INSERT INTO example_annotations (item_id, label, user_set, fetched_at) "
        "VALUES ('a1', 'auto', 0, 1), ('a2', 'manual', 1, 1)")
    cache._conn.commit()
    counts = cache.clear()
    assert cache._conn.execute(
        "SELECT COUNT(*) FROM example_items").fetchone()[0] == 0
    rows = cache._conn.execute(
        "SELECT item_id FROM example_annotations").fetchall()
    assert rows == [("a2",)]            # user_set=1 survived
    assert counts["protected_rows_kept"] == 1


def test_clear_force_removes_protected_rows(cache):
    cache._conn.execute(
        "INSERT INTO example_annotations (item_id, label, user_set, fetched_at) "
        "VALUES ('a2', 'manual', 1, 1)")
    cache._conn.commit()
    cache.clear(force=True)
    assert cache._conn.execute(
        "SELECT COUNT(*) FROM example_annotations").fetchone()[0] == 0


def test_clear_resets_markers_keeps_counters(cache):
    cache.set_meta("last_full_sync", "2026-01-01")
    cache.increment_counter(n=5)
    cache.clear()
    assert cache.get_meta("last_full_sync") is None
    assert int(cache.get_meta("api_call_count")) == 5


def test_writability_fallback(monkeypatch, tmp_path):
    import skill_cache as sc
    def boom(*a, **k):
        raise PermissionError("read-only fs")
    monkeypatch.setattr(sc.os, "makedirs", boom)
    c = SkillCache(str(tmp_path / "ro" / "cache.db"), skill_schema.SCHEMA)
    try:
        import tempfile
        assert c.db_path.startswith(tempfile.gettempdir())
    finally:
        c.close()


def test_resolve_data_dir_env_override(monkeypatch, tmp_path):
    from skill_common import resolve_data_dir
    override = str(tmp_path / "override")
    monkeypatch.setenv("TESTSKILL_DATA_DIR", override)
    got = resolve_data_dir("test-skill", str(tmp_path / "skill"),
                           env_var="TESTSKILL_DATA_DIR")
    assert got == override
    assert os.path.isdir(override)


def test_resolve_data_dir_prefers_persistent_home(monkeypatch, tmp_path):
    from skill_common import resolve_data_dir
    fake_home = str(tmp_path / "home")
    os.makedirs(fake_home)
    monkeypatch.setenv("HOME", fake_home)
    monkeypatch.setenv("USERPROFILE", fake_home)
    got = resolve_data_dir("test-skill", str(tmp_path / "skill"))
    assert got == os.path.join(fake_home, ".claude-skills", "test-skill")


def test_bootstrap_data_copies_once_never_overwrites(tmp_path):
    from skill_common import bootstrap_data
    skill_dir = tmp_path / "skill"
    boot = skill_dir / "data" / "bootstrap"
    boot.mkdir(parents=True)
    (boot / "session.pickle").write_bytes(b"BUNDLED")
    data_dir = tmp_path / "persistent"
    data_dir.mkdir()
    copied = bootstrap_data(str(data_dir), str(skill_dir))
    assert copied == ["session.pickle"]
    assert (data_dir / "session.pickle").read_bytes() == b"BUNDLED"
    # Existing (e.g. newer, locally refreshed) files are never clobbered.
    (data_dir / "session.pickle").write_bytes(b"LOCAL_NEWER")
    copied2 = bootstrap_data(str(data_dir), str(skill_dir))
    assert copied2 == []
    assert (data_dir / "session.pickle").read_bytes() == b"LOCAL_NEWER"


def test_version_stamps_match():
    skill_md = os.path.join(SKILL_DIR, "SKILL.md")
    with open(skill_md, encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r'^version:\s*"?([0-9]+\.[0-9]+\.[0-9]+)"?', text, re.M)
    assert m, "SKILL.md frontmatter is missing version:"
    assert m.group(1) == skill_schema.SKILL_VERSION


def _run(capsys, args):
    code = cli.main(args)
    return code, capsys.readouterr().out


def test_cli_version(capsys, tmp_path):
    code, out = _run(capsys, ["version", "--db", str(tmp_path / "c.db")])
    assert code == 0
    assert skill_schema.SKILL_VERSION in out


def test_cli_status(capsys, tmp_path):
    code, out = _run(capsys, ["status", "--db", str(tmp_path / "c.db")])
    assert code == 0
    assert "Cache Status" in out
    assert skill_schema.SKILL_VERSION in out


def test_cli_clear_cache(capsys, tmp_path):
    db = str(tmp_path / "c.db")
    _run(capsys, ["status", "--db", db])  # creates schema
    code, out = _run(capsys, ["clear-cache", "--db", db])
    assert code == 0
    assert "Cache Cleared" in out
