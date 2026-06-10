"""
Live integration tests. SKIPPED unless a real Monarch session pickle exists at
monarch-money/data/session.pickle (created by scripts/setup_auth.py). These hit
the real Monarch API and are never run in CI.
"""
import os
import pickle

import pytest

import fetch
from monarch_client import MonarchCache, MonarchClient, AuthExpiredError

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SESSION = os.path.join(_REPO, "monarch-money", "data", "session.pickle")

pytestmark = pytest.mark.skipif(
    not os.path.exists(_SESSION),
    reason="No live session — run setup_auth.py first",
)


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "live_cache.db")


def test_live_accounts_sync(capsys, db):
    code = fetch.main(["accounts", "--db", db])
    out = capsys.readouterr().out
    assert code == 0
    assert "Net Worth" in out
    c = MonarchCache(db)
    try:
        assert c.status()["accounts_rows"] > 0
    finally:
        c.close()


def test_live_transactions_incremental(capsys, db):
    # First sync establishes last_tx_sync; second must advance it and add no dups.
    fetch.main(["transactions", "--days", "30", "--db", db])
    capsys.readouterr()
    c = MonarchCache(db)
    first_marker = c.get_last_tx_sync()
    first_count = c.status()["transactions_rows"]
    c.close()

    fetch.main(["transactions", "--days", "30", "--db", db, "--refresh"])
    capsys.readouterr()
    c = MonarchCache(db)
    try:
        assert c.get_last_tx_sync() >= first_marker
        # No primary-key duplicates after a second incremental pull.
        dups = c._conn.execute(
            "SELECT id, COUNT(*) c FROM transactions GROUP BY id HAVING c > 1"
        ).fetchall()
        assert dups == []
        assert c.status()["transactions_rows"] >= first_count
    finally:
        c.close()


def test_live_holdings_null_basis_handled(capsys, db):
    code = fetch.main(["holdings", "--db", db])
    out = capsys.readouterr().out
    assert code == 0
    # Some custodians return null cost basis; the renderer must not crash and
    # should show an em-dash for those rows.
    assert "Total Portfolio" in out


def test_live_auth_expiry_simulation(db, tmp_path):
    # A corrupt session pickle must classify as AuthExpiredError (never a crash).
    bad = tmp_path / "bad_session.pickle"
    bad.write_bytes(b"not a valid pickle at all")
    with pytest.raises(AuthExpiredError):
        MonarchClient.from_session(str(bad), db)


def test_live_auth_expiry_exit_code(capsys, db, tmp_path):
    bad = tmp_path / "bad_session.pickle"
    bad.write_bytes(b"not a valid pickle at all")
    code = fetch.main(["accounts", "--db", db, "--session-path", str(bad)])
    out = capsys.readouterr().out
    assert code == 1
    assert "AuthExpiredError" in out or "error" in out.lower()


def test_live_sync_all(capsys, db):
    code = fetch.main(["sync-all", "--db", db])
    out = capsys.readouterr().out
    assert code == 0
    assert "Sync Complete" in out
    c = MonarchCache(db)
    try:
        st = c.status()
        assert st["accounts_rows"] > 0
    finally:
        c.close()
