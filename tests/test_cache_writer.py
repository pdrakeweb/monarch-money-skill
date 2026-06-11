"""Unit tests for cache_writer.py — the MCP → SQLite bridge.

These feed camelCase JSON (the shape MCP tools emit) through cache_writer and
assert the snake_case cache rows, idempotency, graceful field handling, and the
error-JSON-on-stdout / exit-1 contract. All use an in-memory MonarchCache; the
two main()-level tests use a tmp db file.
"""
import json
import sqlite3

import pytest

import cache_writer
from monarch_client import MonarchCache


@pytest.fixture
def cache():
    c = MonarchCache(":memory:")
    yield c
    c.close()


def _count(cache, table, where="", params=()):
    return cache._conn.execute(
        f"SELECT COUNT(*) FROM {table} {where}", params
    ).fetchone()[0]


# --------------------------------------------------------------------------- #
# accounts
# --------------------------------------------------------------------------- #
def test_cache_writer_accounts(cache):
    data = [
        {"id": "a1", "name": "Fidelity Roth IRA", "type": "investment",
         "subtype": "roth ira", "institution": "Fidelity",
         "currentBalance": 145000.0, "isManual": True, "isHidden": False,
         "isClosed": False, "updatedAt": 1745000000},
        {"id": "a2", "name": "Chase Checking", "type": "checking",
         "subtype": "checking", "balance": 8200.0, "isManual": False},
    ]
    summary = cache_writer.write_table(cache, "accounts", data)
    assert "Cached 2 accounts" in summary

    row = cache._conn.execute(
        "SELECT name, type, subtype, balance, is_manual FROM accounts "
        "WHERE id='a1'").fetchone()
    assert row == ("Fidelity Roth IRA", "investment", "roth ira", 145000.0, 1)
    # camelCase currentBalance was normalised into the snake_case balance col.
    assert cache._conn.execute(
        "SELECT balance FROM accounts WHERE id='a2'").fetchone()[0] == 8200.0
    # Tax bucket auto-classification ran on write.
    bucket = cache._conn.execute(
        "SELECT tax_bucket FROM account_tax_buckets WHERE account_id='a1'"
    ).fetchone()[0]
    assert bucket == "roth"


# --------------------------------------------------------------------------- #
# holdings
# --------------------------------------------------------------------------- #
def test_cache_writer_holdings(cache):
    first = [
        {"ticker": "VTI", "name": "Vanguard Total Stock", "quantity": 10,
         "value": 2400, "price": 240, "costBasis": 2000},
        {"ticker": "BND", "name": "Vanguard Bond", "quantity": 5,
         "value": 360, "price": 72, "costBasis": 400},
        {"ticker": "VXUS", "name": "Intl", "quantity": 8, "value": 500,
         "price": 62, "costBasis": 480},
    ]
    cache_writer.write_table(cache, "holdings", first, account_id="X")
    assert _count(cache, "holdings", "WHERE account_id='X'") == 3

    # Re-sync with a shorter list — old tickers for X must be deleted first.
    second = [
        {"ticker": "VTI", "name": "Vanguard Total Stock", "quantity": 10,
         "value": 2500, "price": 250, "costBasis": 2000},
    ]
    summary = cache_writer.write_table(cache, "holdings", second, account_id="X")
    assert _count(cache, "holdings", "WHERE account_id='X'") == 1
    assert "Cached 1 holdings for account X" in summary
    # costBasis camelCase landed in cost_basis.
    assert cache._conn.execute(
        "SELECT cost_basis FROM holdings WHERE account_id='X' AND ticker='VTI'"
    ).fetchone()[0] == 2000


def test_cache_writer_holdings_requires_account_id(cache):
    from monarch_client import MonarchSkillError
    with pytest.raises(MonarchSkillError):
        cache_writer.write_table(cache, "holdings", [])


# --------------------------------------------------------------------------- #
# transactions
# --------------------------------------------------------------------------- #
def _txns(n, start_day=1):
    return [
        {"id": f"t{i}", "accountId": "a1", "date": f"2026-03-{start_day+i:02d}",
         "amount": -10.0 * (i + 1), "merchantName": f"Store {i}",
         "category": "Groceries", "categoryGroup": "Food", "isPending": False}
        for i in range(n)
    ]


def test_cache_writer_transactions_idempotent(cache):
    data = _txns(10)
    s1 = cache_writer.write_table(cache, "transactions", data)
    s2 = cache_writer.write_table(cache, "transactions", data)
    # INSERT OR IGNORE: feeding the same 10 twice yields 10 rows, not 20.
    assert _count(cache, "transactions") == 10
    assert "Cached 10 transactions" in s1 and "Cached 10 transactions" in s2
    # merchantName / accountId camelCase normalised.
    row = cache._conn.execute(
        "SELECT account_id, merchant, category FROM transactions WHERE id='t0'"
    ).fetchone()
    assert row == ("a1", "Store 0", "Groceries")
    # last_tx_sync updated to the max date in the batch.
    assert cache.get_last_tx_sync() == "2026-03-10"


# --------------------------------------------------------------------------- #
# budgets
# --------------------------------------------------------------------------- #
def test_cache_writer_budgets_with_period(cache):
    data = [
        {"categoryId": "c1", "category": "Groceries",
         "categoryGroup": "Food", "budgeted": 600, "actual": 540},
        {"categoryId": "c2", "category": "Dining",
         "categoryGroup": "Food", "budgeted": 200, "actual": 287},
    ]
    summary = cache_writer.write_table(cache, "budgets", data, period="2024-11")
    assert "Cached 2 budget categories for 2024-11" in summary
    rows = cache._conn.execute(
        "SELECT period, category, budgeted FROM budgets ORDER BY category_id"
    ).fetchall()
    assert rows[0] == ("2024-11", "Groceries", 600)
    assert all(r[0] == "2024-11" for r in rows)


def test_cache_writer_budgets_requires_period(cache):
    from monarch_client import MonarchSkillError
    with pytest.raises(MonarchSkillError):
        cache_writer.write_table(cache, "budgets", [])


# --------------------------------------------------------------------------- #
# cashflow
# --------------------------------------------------------------------------- #
def test_cache_writer_cashflow(cache):
    data = {"income": 6400.0, "expense": 4400.0, "savings": 2000.0,
            "savings_rate": 31.2}
    summary = cache_writer.write_table(cache, "cashflow", data, period="2026-06")
    assert "Income: $6,400.00" in summary and "Expenses: $4,400.00" in summary
    row = cache._conn.execute(
        "SELECT income, expense, savings, savings_rate FROM cashflow "
        "WHERE period='2026-06'").fetchone()
    assert row == (6400.0, 4400.0, 2000.0, 31.2)


def test_cache_writer_cashflow_camelcase_summary(cache):
    # The nested get_cashflow_summary shape (sumIncome/sumExpense/savingsRate).
    data = {"summary": [{"summary": {"sumIncome": 5000, "sumExpense": -3000,
                                     "savings": 2000, "savingsRate": 40.0}}]}
    cache_writer.write_table(cache, "cashflow", data, period="2026-05")
    row = cache._conn.execute(
        "SELECT income, expense, savings_rate FROM cashflow WHERE period='2026-05'"
    ).fetchone()
    assert row == (5000, 3000, 40.0)  # expense stored as positive magnitude


# --------------------------------------------------------------------------- #
# recurring
# --------------------------------------------------------------------------- #
def test_cache_writer_recurring(cache):
    data = [
        {"id": "r1", "merchant": "Netflix", "amount": -16.0,
         "frequency": "monthly", "next_date": "2026-06-12", "accountId": "a6"},
        {"id": "r2", "merchant": "Gym", "amount": -45.0,
         "frequency": "monthly", "next_date": "2026-06-20", "accountId": "a7"},
    ]
    summary = cache_writer.write_table(cache, "recurring", data)
    assert "Cached 2 recurring transactions" in summary
    assert _count(cache, "recurring") == 2


# --------------------------------------------------------------------------- #
# net worth history (v2)
# --------------------------------------------------------------------------- #
def test_cache_writer_net_worth_history(cache):
    data = [
        {"date": "2026-06-01", "total": 500000, "assets": 520000,
         "liabilities": 20000},
        # camelCase netWorth + derive nothing extra
        {"date": "2026-06-02", "netWorth": 501000, "assets": 521000,
         "liabilities": 20000},
    ]
    summary = cache_writer.write_table(cache, "net-worth-history", data)
    assert "Cached 2 net worth snapshots" in summary
    rows = cache._conn.execute(
        "SELECT date, total, assets, liabilities FROM net_worth_history "
        "ORDER BY date").fetchall()
    assert rows[0] == ("2026-06-01", 500000, 520000, 20000)
    assert rows[1] == ("2026-06-02", 501000, 521000, 20000)


def test_cache_writer_net_worth_history_idempotent(cache):
    data = [{"date": "2026-06-01", "total": 500000}]
    cache_writer.write_table(cache, "net-worth-history", data)
    cache_writer.write_table(cache, "net-worth-history",
                             [{"date": "2026-06-01", "total": 510000}])
    # INSERT OR REPLACE keyed on date — one row, latest value.
    assert _count(cache, "net_worth_history") == 1
    assert cache._conn.execute(
        "SELECT total FROM net_worth_history WHERE date='2026-06-01'"
    ).fetchone()[0] == 510000


# --------------------------------------------------------------------------- #
# account balance history (v2)
# --------------------------------------------------------------------------- #
def test_cache_writer_account_balance_history(cache):
    data = [
        {"date": "2026-06-01", "balance": 8200},
        {"date": "2026-06-02", "balance": 8300},
    ]
    summary = cache_writer.write_table(cache, "account-balance-history", data,
                                       account_id="acc_001")
    assert "Cached 2 balance snapshots for account acc_001" in summary
    rows = cache._conn.execute(
        "SELECT account_id, date, balance FROM account_history ORDER BY date"
    ).fetchall()
    assert rows == [("acc_001", "2026-06-01", 8200),
                    ("acc_001", "2026-06-02", 8300)]


def test_cache_writer_account_balance_history_requires_account_id(cache):
    from monarch_client import MonarchSkillError
    with pytest.raises(MonarchSkillError):
        cache_writer.write_table(cache, "account-balance-history", [])


# --------------------------------------------------------------------------- #
# robustness: missing / unknown fields
# --------------------------------------------------------------------------- #
def test_cache_writer_missing_fields_graceful(cache):
    # Only id + name present — every other column should land as NULL, no crash.
    data = [{"id": "sparse1", "name": "Mystery Account"}]
    cache_writer.write_table(cache, "accounts", data)
    row = cache._conn.execute(
        "SELECT name, type, subtype, balance, institution FROM accounts "
        "WHERE id='sparse1'").fetchone()
    assert row[0] == "Mystery Account"
    assert row[1] is None and row[2] is None
    assert row[3] is None and row[4] is None


def test_cache_writer_unknown_fields_dropped(cache):
    # Extra unknown keys (a newer MCP tool version) must be ignored silently.
    data = [{"id": "u1", "name": "Acct", "type": "checking",
             "balance": 100.0, "someBrandNewField": "ignore me",
             "nested": {"a": 1}, "futureFlag": True}]
    summary = cache_writer.write_table(cache, "accounts", data)
    assert "Cached 1 accounts" in summary
    assert cache._conn.execute(
        "SELECT balance FROM accounts WHERE id='u1'").fetchone()[0] == 100.0


# --------------------------------------------------------------------------- #
# error contract (main-level)
# --------------------------------------------------------------------------- #
def test_cache_writer_bad_json_exit_1(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{ this is not valid json ]")
    code = cache_writer.main(
        ["--table", "accounts", "--input", str(bad),
         "--db", str(tmp_path / "c.db")])
    assert code == 1
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "error" in payload and "message" in payload


def test_cache_writer_unknown_table_exit_1(tmp_path, capsys):
    good = tmp_path / "g.json"
    good.write_text("[]")
    # argparse rejects an out-of-choices --table with SystemExit(2).
    with pytest.raises(SystemExit):
        cache_writer.main(["--table", "nonsense", "--input", str(good)])


def test_cache_writer_main_mock_accounts(tmp_path, capsys):
    code = cache_writer.main(
        ["--table", "accounts", "--mock", "--db", str(tmp_path / "m.db")])
    assert code == 0
    assert "Cached 8 accounts" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# schema migration v1 -> v2
# --------------------------------------------------------------------------- #
def test_schema_migration_v1_to_v2(tmp_path):
    from skill_schema import _V1_DDL
    db = str(tmp_path / "v1.db")

    # Build a v1 database: original tables only, schema_version = 1, and
    # crucially WITHOUT the v2 snapshot tables.
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);"
        + _V1_DDL)
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
    conn.commit()
    tables_before = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert "net_worth_history" not in tables_before
    assert "account_history" not in tables_before

    # Re-open through MonarchCache — migration must add the v2 tables and bump
    # the stored version.
    c = MonarchCache(db)
    try:
        tables_after = {r[0] for r in c._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "net_worth_history" in tables_after
        assert "account_history" in tables_after
        assert c.get_meta("schema_version") == "2"
        # And the upgraded db is usable.
        c.write_net_worth_history([{"date": "2026-06-01", "total": 1.0}])
        assert c._conn.execute(
            "SELECT COUNT(*) FROM net_worth_history").fetchone()[0] == 1
    finally:
        c.close()
