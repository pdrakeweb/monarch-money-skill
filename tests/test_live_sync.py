"""Live-path (non-mock) sync behaviour, against a fake MonarchMoney client."""
import datetime as dt
import os
import time
from types import SimpleNamespace

import pytest

import fetch
import monarch_client
from monarch_client import MonarchCache, MonarchClient, AuthExpiredError


class FakeMM:
    """Async stand-ins for the library calls fetch.py makes."""

    def __init__(self):
        self.budget_calls = []
        self.cashflow_calls = []
        self.transactions = []

    async def get_budgets(self, start_date=None, end_date=None):
        self.budget_calls.append((start_date, end_date))
        month = start_date[:7]
        return {
            "categoryGroups": [{"name": "Food", "categories": [
                {"id": "c1", "name": "Groceries"}]}],
            "budgetData": {"monthlyAmountsByCategory": [{
                "category": {"id": "c1"},
                "monthlyAmounts": [
                    {"month": "1999-01-01", "plannedCashFlowAmount": 1,
                     "actualAmount": 1},
                    {"month": f"{month}-01", "plannedCashFlowAmount": 500,
                     "actualAmount": 420},
                ]}]},
        }

    async def get_cashflow_summary(self, start_date=None, end_date=None):
        self.cashflow_calls.append(start_date[:7])
        return {"summary": [{"summary": {
            "sumIncome": 1000.0, "sumExpense": -400.0, "savings": 600.0}}]}

    async def get_transactions(self, limit=100, offset=0, start_date=None,
                               end_date=None):
        return {"allTransactions": {"results": self.transactions[offset:offset + limit]}}


@pytest.fixture
def ctx(tmp_path):
    cache = MonarchCache(str(tmp_path / "c.db"))
    mm = FakeMM()
    c = SimpleNamespace(cache=cache, client=SimpleNamespace(mm=mm), mock=False,
                        refresh=False, fixtures_dir=None, days=30)
    yield c
    cache.close()


def test_budgets_requests_and_stores_the_requested_month(ctx):
    period, wrote = fetch.sync_budgets(ctx, "2026-08")
    assert wrote and period == "2026-08"
    assert ctx.client.mm.budget_calls == [("2026-08-01", "2026-08-31")]
    row = ctx.cache._conn.execute(
        "SELECT period, category, budgeted, actual FROM budgets").fetchone()
    assert row == ("2026-08", "Groceries", 500, 420)


def test_budgets_freshness_is_per_period(ctx):
    fetch.sync_budgets(ctx, "2026-09")
    _, wrote_again = fetch.sync_budgets(ctx, "2026-09")
    _, wrote_other = fetch.sync_budgets(ctx, "2026-08")
    assert not wrote_again and wrote_other
    assert len(ctx.client.mm.budget_calls) == 2


def test_cashflow_month_flag_syncs_that_month(ctx):
    fetch.sync_cashflow(ctx, 1, month="2025-03")
    assert ctx.client.mm.cashflow_calls == ["2025-03"]


def test_cashflow_partial_month_is_refetched_after_rollover(ctx, monkeypatch):
    # Synced mid-August (partial), then viewed in October: must re-fetch.
    ctx.cache.write_cashflow({"income": 1, "expense": 1, "savings": 0,
                              "savings_rate": 0}, "2026-08")
    mid_aug = time.mktime(dt.date(2026, 8, 15).timetuple())
    ctx.cache._conn.execute("UPDATE cashflow SET fetched_at=?", (mid_aug,))
    monkeypatch.setattr(fetch, "_today", lambda: dt.date(2026, 10, 2))
    fetch.sync_cashflow(ctx, 3)
    assert "2026-08" in ctx.client.mm.cashflow_calls


def test_cashflow_settled_month_is_final(ctx, monkeypatch):
    ctx.cache.write_cashflow({"income": 1, "expense": 1, "savings": 0,
                              "savings_rate": 0}, "2026-08")
    settled = time.mktime(dt.date(2026, 9, 20).timetuple())
    ctx.cache._conn.execute("UPDATE cashflow SET fetched_at=?", (settled,))
    monkeypatch.setattr(fetch, "_today", lambda: dt.date(2026, 10, 2))
    fetch.sync_cashflow(ctx, 3)
    assert "2026-08" not in ctx.client.mm.cashflow_calls


def _tx(id_, pending, category="Food", date=None):
    return {"id": id_, "date": date or dt.date.today().isoformat(),
            "amount": -10.0, "merchant": {"name": "M"},
            "category": {"name": category}, "pending": pending}


def test_transaction_resync_updates_row_and_prunes_stale_pending(ctx):
    mm = ctx.client.mm
    mm.transactions = [_tx("p1", True), _tx("t2", False, "Food")]
    fetch.sync_transactions(ctx)
    # p1 settles under a new id; t2 is recategorised upstream.
    mm.transactions = [_tx("t1", False), _tx("t2", False, "Dining")]
    ctx.refresh = True
    fetch.sync_transactions(ctx)
    rows = dict(ctx.cache._conn.execute(
        "SELECT id, category FROM transactions").fetchall())
    assert rows == {"t1": "Food", "t2": "Dining"}


def test_expired_session_fails_over_to_env(monkeypatch, tmp_path, capsys):
    """A dead session pickle must not shadow working .env credentials."""
    db = str(tmp_path / "c.db")
    calls = []

    def fake_ensure(*a, skip_session=False, **kw):
        c = MonarchClient(object(), MonarchCache(db))
        c.auth_source = "env" if skip_session else "session"
        calls.append(c.auth_source)
        return c

    def fake_dispatch(args, cache, client):
        if client.auth_source == "session":
            raise Exception("401 Unauthorized")
        print("ok via", client.auth_source)
        return 0

    monkeypatch.setattr(MonarchClient, "ensure_authenticated", fake_ensure)
    monkeypatch.setattr(fetch, "dispatch", fake_dispatch)
    code = fetch.main(["accounts", "--db", db])
    assert code == 0 and calls == ["session", "env"]
    assert "ok via env" in capsys.readouterr().out


def test_non_session_auth_failure_is_not_retried(monkeypatch, tmp_path, capsys):
    db = str(tmp_path / "c.db")
    calls = []

    def fake_ensure(*a, skip_session=False, **kw):
        c = MonarchClient(object(), MonarchCache(db))
        c.auth_source = "env"
        calls.append(skip_session)
        return c

    def fake_dispatch(args, cache, client):
        raise Exception("401 Unauthorized")

    monkeypatch.setattr(MonarchClient, "ensure_authenticated", fake_ensure)
    monkeypatch.setattr(fetch, "dispatch", fake_dispatch)
    code = fetch.main(["accounts", "--db", db])
    assert code == 1 and calls == [False]
    assert "AuthExpiredError" in capsys.readouterr().out


def test_powershell_scripts_are_ascii_only():
    # Windows PowerShell 5.1 reads BOM-less .ps1 as ANSI; one em dash breaks
    # parsing of the whole file (ported from claude-skill-skeleton).
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("build.ps1", "deploy.ps1"):
        with open(os.path.join(root, name), "rb") as fh:
            data = fh.read()
        bad = [i for i, b in enumerate(data) if b > 127]
        assert not bad, f"{name} has non-ASCII bytes at offsets {bad[:5]}"
