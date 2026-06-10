"""Unit tests for MonarchCache (SQLite offline cache). Written before implementation."""
import os
import tempfile
import time

import pytest

import monarch_client
from monarch_client import MonarchCache


EXPECTED_TABLES = {
    "accounts", "account_tax_buckets", "holdings", "transactions",
    "budgets", "cashflow", "recurring", "meta",
}


def _acct(id, type="investment", subtype="brokerage", **kw):
    base = dict(
        id=id, name=f"Acct {id}", type=type, subtype=subtype,
        institution="Test Bank", balance=1000.0, display_balance=1000.0,
        currency="USD", is_manual=0, is_hidden=0, is_closed=0,
        updated_at=int(time.time()),
    )
    base.update(kw)
    return base


@pytest.fixture
def cache():
    c = MonarchCache(":memory:")
    yield c
    c.close()


def _table_names(c):
    rows = c._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def test_schema_creation(cache):
    assert EXPECTED_TABLES.issubset(_table_names(cache))


def test_is_fresh_hit(cache):
    cache._conn.execute(
        "INSERT INTO accounts (id, fetched_at) VALUES (?, ?)",
        ("a1", int(time.time()) - 60),
    )
    cache._conn.commit()
    assert cache.is_fresh("accounts", "id", "a1", 3600) is True


def test_is_fresh_miss(cache):
    cache._conn.execute(
        "INSERT INTO accounts (id, fetched_at) VALUES (?, ?)",
        ("a1", int(time.time()) - 7200),
    )
    cache._conn.commit()
    assert cache.is_fresh("accounts", "id", "a1", 3600) is False


def test_write_accounts_upsert(cache):
    cache.write_accounts([_acct("a1", balance=10.0)])
    cache.write_accounts([_acct("a1", balance=99.0)])
    n = cache._conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    assert n == 1
    bal = cache._conn.execute(
        "SELECT balance FROM accounts WHERE id='a1'"
    ).fetchone()[0]
    assert bal == 99.0


def _tx(id, **kw):
    base = dict(
        id=id, account_id="a1", date="2025-09-15", amount=-10.0,
        merchant="M", category="Groceries", category_group="Food",
        tags=[], notes="", is_pending=0,
    )
    base.update(kw)
    return base


def test_write_transactions_no_delete(cache):
    cache.write_transactions([_tx(f"t{i}") for i in range(10)])
    # 5 more, 3 overlapping ids (t7, t8, t9)
    cache.write_transactions([_tx(f"t{i}") for i in (7, 8, 9, 10, 11)])
    n = cache._conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert n == 12


def _hold(ticker, **kw):
    base = dict(ticker=ticker, name=ticker, quantity=1.0, value=100.0,
                price=100.0, cost_basis=80.0)
    base.update(kw)
    return base


def test_write_holdings_replaces_per_account(cache):
    cache.write_holdings("A", [_hold("X"), _hold("Y"), _hold("Z")])
    cache.write_holdings("A", [_hold("X"), _hold("Y")])
    n = cache._conn.execute(
        "SELECT COUNT(*) FROM holdings WHERE account_id='A'"
    ).fetchone()[0]
    assert n == 2


def test_api_call_counter(cache):
    cache.increment_api_calls()
    cache.increment_api_calls()
    cache.increment_api_calls()
    assert int(cache.status()["api_call_count"]) == 3


def test_writability_fallback(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise PermissionError("read-only fs")
    monkeypatch.setattr(monarch_client.os, "makedirs", boom)
    target = str(tmp_path / "ro" / "monarch_cache.db")
    c = MonarchCache(target)
    try:
        assert c.db_path != target
        assert c.db_path.startswith(tempfile.gettempdir())
        assert "monarch_cache" in c.db_path
        # usable despite fallback
        assert EXPECTED_TABLES.issubset(_table_names(c))
    finally:
        c.close()


def _bucket(cache, account_id):
    return cache._conn.execute(
        "SELECT tax_bucket, auto_classified FROM account_tax_buckets WHERE account_id=?",
        (account_id,),
    ).fetchone()


def test_tax_bucket_auto_classify_roth(cache):
    cache.write_accounts([_acct("r1", type="investment", subtype="roth ira")])
    bucket, auto = _bucket(cache, "r1")
    assert bucket == "roth"
    assert auto == 1


def test_tax_bucket_auto_classify_401k(cache):
    cache.write_accounts([_acct("k1", type="investment", subtype="401k")])
    bucket, _ = _bucket(cache, "k1")
    assert bucket == "traditional"


def test_tax_bucket_auto_classify_brokerage(cache):
    cache.write_accounts([_acct("b1", type="investment", subtype="brokerage")])
    bucket, _ = _bucket(cache, "b1")
    assert bucket == "taxable"


def test_tax_bucket_user_override_preserved(cache):
    cache.write_accounts([_acct("u1", type="investment", subtype="brokerage")])
    cache._conn.execute(
        "UPDATE account_tax_buckets SET tax_bucket='roth', auto_classified=0 "
        "WHERE account_id='u1'"
    )
    cache._conn.commit()
    # Re-sync with original subtype; user override must survive.
    cache.write_accounts([_acct("u1", type="investment", subtype="brokerage")])
    bucket, auto = _bucket(cache, "u1")
    assert bucket == "roth"
    assert auto == 0


def test_tax_bucket_ambiguous_investment(cache):
    cache.write_accounts([_acct("x1", type="investment", subtype=None)])
    bucket, _ = _bucket(cache, "x1")
    assert bucket == "other"


def test_tax_bucket_trust_is_taxable(cache):
    cache.write_accounts([_acct("t1", subtype="trust")])
    bucket, _ = _bucket(cache, "t1")
    assert bucket == "taxable"


def test_tax_bucket_underscored_hsa(cache):
    cache.write_accounts([_acct("h1", subtype="health_savings_account")])
    bucket, _ = _bucket(cache, "h1")
    assert bucket == "hsa"


def test_tax_bucket_non_taxable_brokerage_by_name(cache):
    # Fidelity BrokerageLink windows: the subtype says non_taxable_brokerage,
    # the NAME disambiguates traditional vs roth (QA field finding).
    cache.write_accounts([
        _acct("bl1", subtype="non_taxable_brokerage_account",
              name="BrokerageLink"),
        _acct("bl2", subtype="non_taxable_brokerage_account",
              name="BrokerageLink Roth"),
    ])
    assert _bucket(cache, "bl1")[0] == "traditional"
    assert _bucket(cache, "bl2")[0] == "roth"


def test_tax_bucket_charitable_excluded(cache):
    # Donor-advised / charitable funds are not the user's assets-by-tax-bucket;
    # 'excluded' keeps them out of asset-location math AND out of the
    # unclassified-warning nag (which only counts 'other').
    cache.write_accounts([
        _acct("ch1", subtype="brokerage", name="Fidelity Charitable Giving"),
        _acct("ch2", subtype="brokerage", name="Daffy DAF"),
    ])
    assert _bucket(cache, "ch1")[0] == "excluded"
    assert _bucket(cache, "ch2")[0] == "excluded"


def test_tax_bucket_auto_rows_reclassified_on_logic_upgrade(cache):
    """Auto-classified rows update when the classifier improves; user rows
    (auto_classified=0) never do."""
    cache.write_accounts([_acct("rc1", subtype="trust")])
    # Simulate a stale auto classification from an older classifier.
    cache._conn.execute(
        "UPDATE account_tax_buckets SET tax_bucket='other' WHERE account_id='rc1'"
    )
    cache._conn.commit()
    cache.write_accounts([_acct("rc1", subtype="trust")])
    bucket, auto = _bucket(cache, "rc1")
    assert bucket == "taxable"  # re-sync healed the stale auto row
    assert auto == 1
