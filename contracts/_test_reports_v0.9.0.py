"""Tests for the Boglehead/FIRE reports + dashboard. Run against the mock
fixtures synced into a temp cache."""
import json
import os

import pytest

import fetch
import reports
from monarch_client import MonarchCache
from conftest import FIXTURES_DIR


@pytest.fixture
def db(tmp_path, capsys):
    d = str(tmp_path / "cache.db")
    fetch.main(["sync-all", "--mock", "--db", d, "--fixtures-dir", FIXTURES_DIR])
    capsys.readouterr()
    return d


def _run(capsys, args):
    code = fetch.main(args)
    return code, capsys.readouterr().out


def test_asset_class_classifier():
    assert reports.classify_asset_class("VTI", "Vanguard Total Stock") == "us_stock"
    assert reports.classify_asset_class("VXUS", "Total Intl") == "intl_stock"
    assert reports.classify_asset_class("BND", "Total Bond") == "bond"
    assert reports.classify_asset_class("VNQ", "Real Estate") == "reit"
    assert reports.classify_asset_class(None, "US Treasury Bond Fund") == "bond"
    assert reports.classify_asset_class("AAPL", "Apple Inc") == "us_stock"


def test_all_reports_render(db):
    c = MonarchCache(db)
    prof = reports.DEFAULT_PROFILE
    try:
        assert len(reports.REPORT_KEYS) == 11
        for key in reports.REPORT_KEYS:
            r = reports.run_report(c, prof, key)
            assert r["title"]
            assert r["full"].startswith("## ")
            assert isinstance(r["summary"], list) and r["summary"]
    finally:
        c.close()


def test_classify_outflow_buckets():
    p = reports.DEFAULT_PROFILE
    # transfers: investment buys & contributions
    assert reports.classify_outflow("Buy", "Fidelity", -200000, p) == "transfer"
    assert reports.classify_outflow("401k Contribution", "x", -2000, p) == "transfer"
    assert reports.classify_outflow("HSA Contributions", "x", -3000, p) == "transfer"
    assert reports.classify_outflow("Transfer", "x", -500, p) == "transfer"
    # one-time capital outlay
    assert reports.classify_outflow("Sailboat Acquisition", "Cape Yachts",
                                    -177000, p) == "one_time"
    # recurring
    assert reports.classify_outflow("Groceries", "Store", -87, p) == "recurring"
    assert reports.classify_outflow("Mortgage", "Bank", -2500, p) == "recurring"


def test_short_keyword_does_not_false_match_merchant():
    # 'buy' must NOT classify a Best Buy purchase as a transfer.
    p = reports.DEFAULT_PROFILE
    assert reports.classify_outflow("Shopping", "Best Buy", -300, p) == "recurring"


def test_lumpy_amortization():
    p = dict(reports.DEFAULT_PROFILE, lumpy_expenses=[
        {"name": "Car", "amount": 60000, "frequency_years": 8},
        {"name": "Roof", "amount": 25000, "frequency_years": 25}])
    # 60000/8 + 25000/25 = 7500 + 1000 = 8500
    assert round(reports.lumpy_annual(p)) == 8500


def test_horizon_swr_derivation():
    # 95 - 65 = 30y horizon → 4.0%
    p = dict(reports.DEFAULT_PROFILE, swr_pct=None, retirement_age=65,
             life_expectancy_age=95)
    assert reports.derive_swr(p)[0] == 4.0
    # 95 - 50 = 45y → 3.25%
    p2 = dict(p, retirement_age=50)
    assert reports.derive_swr(p2)[0] == 3.25
    # 95 - 55 = 40y → 3.5%
    p3 = dict(p, retirement_age=55)
    assert reports.derive_swr(p3)[0] == 3.5
    # explicit override honored
    p4 = dict(p, swr_pct=3.0)
    assert reports.derive_swr(p4) == (3.0, "profile-set")


def test_recurring_excludes_transfers_and_one_time(db):
    c = MonarchCache(db)
    p = reports.DEFAULT_PROFILE
    try:
        bd = reports.expense_breakdown(c, p)
        # the $200k buy and $177k boat must NOT be in recurring
        assert bd["transfer_total"] >= 200000
        assert bd["one_time_total"] >= 177000
        assert bd["recurring_monthly"] < 20000  # not blown up by the buy/boat
    finally:
        c.close()


def test_savings_rate_positive_after_reclassification(capsys, db):
    code, out = _run(capsys, ["report", "savings-rate", "--db", db])
    assert code == 0
    # no deeply-negative headline; capital deployment surfaced
    assert "savings rate" in out.lower()
    assert "Capital deployed" in out


def test_expenses_three_sections(capsys, db):
    code, out = _run(capsys, ["report", "expenses", "--db", db])
    assert code == 0
    assert "Recurring budget" in out
    assert "Lumpy allowances" in out
    assert "EXCLUDED from run-rate" in out
    assert "Sailboat Acquisition" in out


def test_fire_uses_retirement_budget_and_tax_gross_up(capsys, db):
    code, out = _run(capsys, ["report", "fire-progress", "--db", db])
    assert code == 0
    assert "Retirement budget" in out
    assert "gross" in out.lower()


def test_dashboard_profile_incomplete_banner(capsys, db):
    code, out = _run(capsys, ["dashboard", "--db", db])
    assert "Profile incomplete" in out  # DEFAULT_PROFILE has profile_complete False


def test_dashboard_lists_every_report_with_link(capsys, db):
    code, out = _run(capsys, ["dashboard", "--db", db])
    assert code == 0
    assert "Financial Dashboard" in out
    assert "Key metrics" in out
    for key in reports.REPORT_KEYS:
        # each report has a full-report command link
        assert f"report {key}" in out


def test_report_all_emits_all_sections(capsys, db):
    code, out = _run(capsys, ["report", "all", "--db", db])
    assert code == 0
    assert out.count("<!-- report:") == 11


def test_fire_progress_math_isolated(db):
    c = MonarchCache(db)
    try:
        # Isolate: override budget, no lumpy/deltas/tax → 40000 / 4% = 1,000,000
        prof = dict(reports.DEFAULT_PROFILE, annual_expenses_override=40000,
                    swr_pct=4.0, lumpy_expenses=[],
                    retirement_expense_reductions=[],
                    retirement_expense_deltas={},
                    retirement_trad_tax_rate=0.0, retirement_ltcg_rate=0.0)
        num, d = reports.fire_number(c, prof)
        assert round(num) == 1000000
        assert round(d["budget"]) == 40000
    finally:
        c.close()


def test_fire_tax_gross_up_raises_number(db):
    c = MonarchCache(db)
    try:
        notax = dict(reports.DEFAULT_PROFILE, annual_expenses_override=40000,
                     swr_pct=4.0, lumpy_expenses=[],
                     retirement_expense_reductions=[], retirement_expense_deltas={},
                     retirement_trad_tax_rate=0.0, retirement_ltcg_rate=0.0)
        taxed = dict(notax, retirement_trad_tax_rate=0.25, retirement_ltcg_rate=0.15)
        n0, _ = reports.fire_number(c, notax)
        n1, _ = reports.fire_number(c, taxed)
        assert n1 > n0  # grossing up for taxes raises the required number
    finally:
        c.close()


def test_unknown_report_exits_1(capsys, db):
    code, out = _run(capsys, ["report", "nope", "--db", db])
    assert code == 1
    assert "UnknownReport" in out


def test_profile_override_changes_swr(db, tmp_path):
    # profile.json sits next to the db (the persistent data dir).
    prof_path = os.path.join(os.path.dirname(db), "profile.json")
    with open(prof_path, "w") as fh:
        json.dump({"swr_pct": 3.5, "annual_expenses_override": 40000}, fh)
    loaded = reports.load_profile(os.path.dirname(db))
    assert loaded["swr_pct"] == 3.5
    c = MonarchCache(db)
    try:
        r = reports.run_report(c, loaded, "fire-progress")
        # swr 3.5% honored → multiplier 100/3.5 = 28.6×
        assert "28.6×" in r["full"]
    finally:
        c.close()


def test_reports_command_no_auth(capsys, db):
    # dashboard/report/reports must work without credentials (cache-only).
    code, out = _run(capsys, ["reports", "--db", db])
    assert code == 0
    assert "net-worth" in out


# --------------------------------------------------------------------------- #
# v0.9.0 — A1 (DAF leak) and A4 (income contamination)
# --------------------------------------------------------------------------- #
def test_daf_excluded_from_investable_a1(db):
    c = MonarchCache(db)
    try:
        base = reports.investable_assets(c)
        # Insert a DAF: brokerage type, bucket 'excluded'.
        c.write_accounts([{"id": "daf1", "name": "CHARITABLE FUND",
                           "type": "brokerage", "subtype": "brokerage",
                           "balance": 250000.0}])
        # name-based classifier should auto-set 'excluded'; assert it did
        bucket = c._conn.execute(
            "SELECT tax_bucket FROM account_tax_buckets WHERE account_id='daf1'"
        ).fetchone()[0]
        assert bucket == "excluded"
        after = reports.investable_assets(c)
        assert after == base                       # DAF did NOT inflate it (A1)
        assert reports.daf_total(c) >= 250000.0    # but shows in daf_total
    finally:
        c.close()


def test_savings_rate_excludes_brokerage_income_a4(db):
    c = MonarchCache(db)
    p = reports.DEFAULT_PROFILE
    try:
        base = reports.real_income_by_month(c, p)
        base_total = sum(base.values())
        # A big 'Sell' credit must NOT count as income.
        c.write_transactions([{"id": "sell1", "account_id": "acc_005",
                               "date": "2025-10-15", "amount": 500000.0,
                               "category": "Sell", "merchant": "Fidelity"}])
        after = reports.real_income_by_month(c, p)
        assert sum(after.values()) == base_total   # sale proceeds excluded
    finally:
        c.close()


# --------------------------------------------------------------------------- #
# v0.9.0 — Part C: TPAW / ABW
# --------------------------------------------------------------------------- #
def test_abw_rate_matches_worked_example():
    # r=4.4%, g=-1.5%, n=42 → ~6.6% (spec worked example)
    rate = reports.abw_rate(0.044, -0.015, 42)
    assert 0.064 <= rate <= 0.068
    # r_eff == 0 limit → 1/n
    assert abs(reports.abw_rate(0.0, 0.0, 40) - 1/40) < 1e-9


def test_abw_negative_tilt_front_loads():
    # more negative tilt → higher initial rate (front-load)
    base = reports.abw_rate(0.05, 0.0, 40)
    front = reports.abw_rate(0.05, -0.015, 40)
    assert front > base


def test_pv_income_streams():
    p = dict(reports.DEFAULT_PROFILE, life_expectancy_age=92,
             income_discount_rate_pct=2.0,
             income_streams=[{"name": "SS", "annual": 48000,
                              "start_age": 70, "end_age": None}])
    pv_at_70 = reports.pv_income_streams(p, 70, 0.02)
    pv_at_50 = reports.pv_income_streams(p, 50, 0.02)
    assert pv_at_70 > 0
    assert pv_at_50 < pv_at_70           # discounting 20 more years → smaller PV


def test_tpaw_monte_carlo_under_2s_and_shaped(db):
    import time
    c = MonarchCache(db)
    p = dict(reports.DEFAULT_PROFILE, withdrawal_strategy="tpaw",
             birth_year=1980, retirement_age=50, life_expectancy_age=92,
             spending_tilt_pct=-1.5, annual_new_savings=100000)
    try:
        t = time.time()
        mc = reports.tpaw_monte_carlo(c, p, paths=2000)
        assert time.time() - t < 2.0
        assert mc["paths"] == 2000
        assert mc["spending_by_age"]
        assert 0.0 <= mc["p_above_lean2x"] <= 1.0
        # reproducible (seeded)
        mc2 = reports.tpaw_monte_carlo(c, p, paths=2000)
        assert mc["median_terminal"] == mc2["median_terminal"]
    finally:
        c.close()


def test_tpaw_strategy_branches(db):
    c = MonarchCache(db)
    p = dict(reports.DEFAULT_PROFILE, withdrawal_strategy="tpaw",
             birth_year=1980, retirement_age=50, life_expectancy_age=92,
             spending_tilt_pct=-1.5,
             income_streams=[{"name": "SS", "annual": 48000, "start_age": 70}])
    try:
        w = reports.run_report(c, p, "withdrawal")
        assert "TPAW" in w["full"] and "Monte Carlo" in w["full"]
        assert w["kpi"][0] == "TPAW initial spend (med)"
        fp = reports.run_report(c, p, "fire-progress")
        assert "ABW rate" in fp["full"]
        lv = reports.run_report(c, p, "life-value")
        assert "never depletes" in lv["full"]
        # static keeps the fixed-draw depletion logic
        lv_static = reports.run_report(c, dict(p, withdrawal_strategy="static"),
                                       "life-value")
        assert "never depletes" not in lv_static["full"]
    finally:
        c.close()
