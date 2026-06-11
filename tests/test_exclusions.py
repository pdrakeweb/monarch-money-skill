"""Tests for the exclusions layer: MonarchCache methods, the
get_exclusion_filter_sql helper, and fetch.py integration (filtering + note +
--include-excluded bypass)."""
import pytest

import fetch
from monarch_client import MonarchCache
from conftest import FIXTURES_DIR


@pytest.fixture
def cache():
    c = MonarchCache(":memory:")
    yield c
    c.close()


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "monarch_cache.db")


def _run(capsys, args):
    code = fetch.main(args)
    return code, capsys.readouterr().out


def _base(db, *extra):
    return ["--mock", "--db", db, "--fixtures-dir", FIXTURES_DIR, *extra]


# --------------------------------------------------------------------------- #
# MonarchCache methods
# --------------------------------------------------------------------------- #
def test_add_exclusion_category_period(cache):
    ex_id = cache.add_exclusion(
        "category_period", "Dental & Medical", start_date="2024-03",
        end_date="2024-03", reason="orthodontia")
    assert isinstance(ex_id, int)
    rows = cache.list_exclusions()
    assert len(rows) == 1
    r = rows[0]
    assert r["type"] == "category_period"
    assert r["value"] == "Dental & Medical"
    # YYYY-MM expanded to full-month ISO bounds.
    assert r["start_date"] == "2024-03-01"
    assert r["end_date"] == "2024-03-31"
    assert r["reason"] == "orthodontia"
    assert r["exclude_from"] == "all"


def test_add_exclusion_duplicate_ignored(cache):
    # NULL-date all-time exclusion added twice → one row, same id (the SQLite
    # NULL-in-UNIQUE gotcha must not produce a second row).
    id1 = cache.add_exclusion("category", "Income")
    id2 = cache.add_exclusion("category", "Income")
    assert id1 == id2
    assert len(cache.list_exclusions()) == 1


def test_remove_exclusion(cache):
    ex_id = cache.add_exclusion("merchant", "Orthodontist")
    assert cache.remove_exclusion(ex_id) is True
    assert cache.list_exclusions() == []


def test_remove_nonexistent_returns_false(cache):
    assert cache.remove_exclusion(999) is False


def test_list_exclusions_type_filter(cache):
    cache.add_exclusion("category", "Income")
    cache.add_exclusion("merchant", "Orthodontist")
    assert len(cache.list_exclusions()) == 2
    only_merchant = cache.list_exclusions(type_filter="merchant")
    assert len(only_merchant) == 1
    assert only_merchant[0]["value"] == "Orthodontist"


def test_clear_auto_exclusions(cache):
    cache.add_exclusion("category", "Income", auto_suggested=False)
    cache.add_exclusion("merchant", "X", auto_suggested=True)
    cache.add_exclusion("merchant", "Y", auto_suggested=True)
    assert cache.clear_auto_exclusions() == 2
    rows = cache.list_exclusions()
    assert len(rows) == 1 and rows[0]["value"] == "Income"


# --------------------------------------------------------------------------- #
# get_exclusion_filter_sql shape
# --------------------------------------------------------------------------- #
def test_get_exclusion_filter_sql_shape(cache):
    frag, params = cache.get_exclusion_filter_sql("all")
    assert frag.strip()                      # non-empty SQL
    assert params == ["all", "all", "all", "all"]  # 4 params for scope='all'
    # references all four exclusion types
    for kind in ("transaction", "category", "merchant", "category_period"):
        assert kind in frag


def test_get_exclusion_filter_sql_custom_alias(cache):
    frag, _ = cache.get_exclusion_filter_sql("cashflow", tx_table_alias="x")
    assert "x.id" in frag and "x.category" in frag


def test_filter_sql_excludes_category_rows(cache):
    cache.write_transactions([
        {"id": "t1", "account_id": "a", "date": "2024-01-05", "amount": -10,
         "merchant": "M", "category": "Groceries", "category_group": "Food",
         "tags": [], "notes": "", "is_pending": 0},
        {"id": "t2", "account_id": "a", "date": "2024-01-06", "amount": -4800,
         "merchant": "Ortho", "category": "Dental", "category_group": "Health",
         "tags": [], "notes": "", "is_pending": 0},
    ])
    cache.add_exclusion("category", "Dental")
    frag, params = cache.get_exclusion_filter_sql("all")
    rows = cache._conn.execute(
        f"SELECT t.id FROM transactions t WHERE 1=1 {frag}", params).fetchall()
    ids = {r[0] for r in rows}
    assert ids == {"t1"}  # Dental row filtered out


def test_filter_sql_category_period_respects_dates(cache):
    cache.write_transactions([
        {"id": "in", "account_id": "a", "date": "2024-03-15", "amount": -4800,
         "merchant": "Ortho", "category": "Dental", "category_group": "H",
         "tags": [], "notes": "", "is_pending": 0},
        {"id": "out", "account_id": "a", "date": "2024-08-15", "amount": -120,
         "merchant": "Dentist", "category": "Dental", "category_group": "H",
         "tags": [], "notes": "", "is_pending": 0},
    ])
    cache.add_exclusion("category_period", "Dental", start_date="2024-03",
                        end_date="2024-03")
    frag, params = cache.get_exclusion_filter_sql("all")
    ids = {r[0] for r in cache._conn.execute(
        f"SELECT t.id FROM transactions t WHERE 1=1 {frag}", params).fetchall()}
    assert ids == {"out"}  # only the March tx is excluded


# --------------------------------------------------------------------------- #
# fetch.py integration
# --------------------------------------------------------------------------- #
def test_exclusion_applied_to_query(capsys, db):
    # Baseline: 99 mock transactions.
    code, out = _run(capsys, ["transactions", *_base(db)])
    assert code == 0 and "99 transactions" in out
    # Exclude the Income category, re-run → fewer transactions shown.
    c = MonarchCache(db)
    c.add_exclusion("category", "Income")
    c.close()
    code, out = _run(capsys, ["transactions", *_base(db)])
    assert code == 0
    import re
    n = int(re.search(r"(\d+) transactions\)", out).group(1))
    assert n < 99


def test_include_excluded_flag_bypasses(capsys, db):
    _run(capsys, ["transactions", *_base(db)])  # populate
    c = MonarchCache(db)
    c.add_exclusion("category", "Income")
    c.close()
    code, out = _run(capsys, ["transactions", *_base(db, "--include-excluded")])
    assert code == 0
    assert "99 transactions" in out          # full count restored
    assert "excluded as non-recurring" not in out  # no note in raw view


def test_note_appended_when_exclusions_active(capsys, db):
    _run(capsys, ["transactions", *_base(db)])  # populate
    c = MonarchCache(db)
    c.add_exclusion("category", "Income")
    c.close()
    code, out = _run(capsys, ["transactions", *_base(db)])
    assert "excluded as non-recurring" in out


def test_no_note_when_no_exclusions(capsys, db):
    code, out = _run(capsys, ["transactions", *_base(db)])
    assert "excluded as non-recurring" not in out
