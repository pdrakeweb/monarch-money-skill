"""Functional tests for fetch.py in --mock mode. Written before implementation."""
import json
import os
import shutil

import pytest

import fetch
import monarch_client
from monarch_client import MonarchCache, AuthExpiredError
from conftest import FIXTURES_DIR


def _run(capsys, args):
    code = fetch.main(args)
    out = capsys.readouterr().out
    return code, out


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "monarch_cache.db")


def _base(db, *extra, fixtures=FIXTURES_DIR):
    return ["--mock", "--db", db, "--fixtures-dir", fixtures, *extra]


def test_accounts_output_contains_table(capsys, db):
    code, out = _run(capsys, ["accounts", *_base(db)])
    assert code == 0
    assert "| Checking |" in out


def test_accounts_net_worth_excludes_hidden(capsys, db, tmp_path):
    # Build a modified fixtures dir with a large hidden account added.
    fx = tmp_path / "fx"
    shutil.copytree(FIXTURES_DIR, fx)
    accts = json.loads((fx / "accounts.json").read_text())
    accts.append({
        "id": "acc_hidden", "name": "Hidden Stash", "type": "checking",
        "subtype": "checking", "institution": "Secret", "balance": 999999.0,
        "displayBalance": 999999.0, "currency": "USD", "isManual": False,
        "isHidden": True, "isClosed": False, "updatedAt": 1745000000,
    })
    (fx / "accounts.json").write_text(json.dumps(accts))
    code, out = _run(capsys, ["accounts", *_base(db, fixtures=str(fx))])
    assert code == 0
    assert "999,999" not in out
    assert "572,300" in out  # net worth excludes the hidden account


def test_accounts_ambiguous_flag(capsys, db, tmp_path):
    fx = tmp_path / "fx"
    shutil.copytree(FIXTURES_DIR, fx)
    accts = json.loads((fx / "accounts.json").read_text())
    accts.append({
        "id": "acc_amb", "name": "Mystery Brokerage", "type": "investment",
        "subtype": None, "institution": "Unknown", "balance": 5000.0,
        "displayBalance": 5000.0, "currency": "USD", "isManual": False,
        "isHidden": False, "isClosed": False, "updatedAt": 1745000000,
    })
    (fx / "accounts.json").write_text(json.dumps(accts))
    code, out = _run(capsys, ["accounts", *_base(db, fixtures=str(fx))])
    assert "⚠" in out
    assert "unclassified" in out.lower()


def test_tax_bucket_warning_in_accounts(capsys, db, tmp_path):
    fx = tmp_path / "fx"
    shutil.copytree(FIXTURES_DIR, fx)
    accts = json.loads((fx / "accounts.json").read_text())
    accts.append({
        "id": "acc_amb2", "name": "Weird Account", "type": "investment",
        "subtype": "", "institution": "X", "balance": 1000.0,
        "displayBalance": 1000.0, "currency": "USD", "isManual": False,
        "isHidden": False, "isClosed": False, "updatedAt": 1745000000,
    })
    (fx / "accounts.json").write_text(json.dumps(accts))
    _, out = _run(capsys, ["accounts", *_base(db, fixtures=str(fx))])
    assert "⚠" in out


def test_holdings_output_structure(capsys, db):
    code, out = _run(capsys, ["holdings", *_base(db)])
    assert code == 0
    assert "Ticker" in out
    assert "VTI" in out


def test_transactions_output_summary(capsys, db):
    code, out = _run(capsys, ["transactions", *_base(db)])
    assert code == 0
    assert "99 transactions" in out
    assert "Net:" in out


def test_budgets_over_budget_flagged(capsys, db):
    code, out = _run(capsys, ["budgets", *_base(db)])
    assert code == 0
    assert "Over budget" in out
    assert "Dining" in out


def test_cashflow_savings_rate(capsys, db):
    code, out = _run(capsys, ["cashflow", *_base(db)])
    assert code == 0
    assert "savings rate" in out.lower()


def test_recurring_output(capsys, db):
    code, out = _run(capsys, ["recurring", *_base(db)])
    assert code == 0
    assert "active" in out.lower()


def test_net_worth_from_cache(capsys, db):
    # Populate the cache via a mock accounts sync.
    _run(capsys, ["accounts", *_base(db)])
    # net-worth makes NO API/mock call — reads straight from the cache.
    code, out = _run(capsys, ["net-worth", "--db", db])
    assert code == 0
    assert "572,300" in out


def test_refresh_bypasses_ttl(capsys, db):
    _run(capsys, ["accounts", *_base(db)])
    c1 = MonarchCache(db)
    calls_after_first = c1.status()["api_call_count"]
    c1.close()

    # Second run without --refresh: cache is fresh → no new sync.
    _run(capsys, ["accounts", *_base(db)])
    c2 = MonarchCache(db)
    calls_cached = c2.status()["api_call_count"]
    c2.close()
    assert calls_cached == calls_after_first

    # Third run with --refresh: bypasses TTL → another sync.
    _run(capsys, ["accounts", *_base(db, "--refresh")])
    c3 = MonarchCache(db)
    calls_refreshed = c3.status()["api_call_count"]
    c3.close()
    assert calls_refreshed > calls_cached


def test_auth_error_exit_code(capsys, db, monkeypatch):
    def boom(*a, **k):
        raise AuthExpiredError("session dead")
    monkeypatch.setattr(monarch_client.MonarchClient, "ensure_authenticated", boom)
    # No --mock → real auth path is exercised and fails.
    code = fetch.main(["accounts", "--db", db])
    out = capsys.readouterr().out
    assert code == 1
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["error"] == "AuthExpiredError"
    assert "message" in payload


def test_no_secret_in_stdout(capsys, db):
    for sub in ("accounts", "holdings", "transactions", "budgets",
                "cashflow", "recurring"):
        capsys.readouterr()  # clear
        _run(capsys, [sub, *_base(db)])
        out = capsys.readouterr().out  # already consumed by _run; re-capture safe
    # Run once more capturing everything for the assertion.
    code, out = _run(capsys, ["accounts", *_base(db)])
    low = out.lower()
    assert "password" not in low
    assert "pickle" not in low
    assert "session_token" not in low


def test_status_output(capsys, db):
    _run(capsys, ["sync-all", *_base(db)])
    code, out = _run(capsys, ["status", "--db", db])
    assert code == 0
    assert "Cache Status" in out
    assert "accounts" in out


def test_version_command(capsys, db):
    from monarch_client import SKILL_VERSION
    code, out = _run(capsys, ["version", "--db", db])
    assert code == 0
    assert SKILL_VERSION in out


def test_status_includes_version(capsys, db):
    from monarch_client import SKILL_VERSION
    _run(capsys, ["sync-all", *_base(db)])
    code, out = _run(capsys, ["status", "--db", db])
    assert SKILL_VERSION in out


def test_skill_md_frontmatter_version_matches_constant():
    """build.ps1 enforces this too — the frontmatter version and the
    SKILL_VERSION constant must always move together."""
    import re
    from monarch_client import SKILL_VERSION
    from conftest import _ROOT
    skill_md = os.path.join(_ROOT, "monarch-money", "SKILL.md")
    with open(skill_md, encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r'^version:\s*"?([0-9]+\.[0-9]+\.[0-9]+)"?', text, re.M)
    assert m, "SKILL.md frontmatter is missing a version: field"
    assert m.group(1) == SKILL_VERSION


def test_clear_cache_command(capsys, db):
    _run(capsys, ["sync-all", *_base(db)])
    code, out = _run(capsys, ["clear-cache", "--db", db])
    assert code == 0
    assert "cleared" in out.lower()
    c = MonarchCache(db)
    st = c.status()
    c.close()
    assert st["accounts_rows"] == 0
    assert st["transactions_rows"] == 0
    # Re-sync repopulates and re-infers tax buckets.
    _run(capsys, ["accounts", *_base(db)])
    c = MonarchCache(db)
    n_buckets = c._conn.execute(
        "SELECT COUNT(*) FROM account_tax_buckets WHERE auto_classified=1"
    ).fetchone()[0]
    c.close()
    assert n_buckets == 8


def test_sync_all(capsys, db):
    code, out = _run(capsys, ["sync-all", *_base(db)])
    assert code == 0
    assert "Sync Complete" in out
    c = MonarchCache(db)
    st = c.status()
    c.close()
    assert st["accounts_rows"] == 8
    assert st["holdings_rows"] == 12
    assert st["transactions_rows"] == 99
