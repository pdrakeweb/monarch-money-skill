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


def test_all_ten_reports_render(db):
    c = MonarchCache(db)
    prof = reports.DEFAULT_PROFILE
    try:
        assert len(reports.REPORT_KEYS) == 10
        for key in reports.REPORT_KEYS:
            r = reports.run_report(c, prof, key)
            assert r["title"]
            assert r["full"].startswith("## ")
            assert isinstance(r["summary"], list) and r["summary"]
    finally:
        c.close()


def test_dashboard_lists_every_report_with_link(capsys, db):
    code, out = _run(capsys, ["dashboard", "--db", db])
    assert code == 0
    assert "Financial Dashboard" in out
    assert "Key metrics" in out
    for key in reports.REPORT_KEYS:
        # each report has a full-report command link
        assert f"report {key}" in out


def test_report_all_emits_ten_sections(capsys, db):
    code, out = _run(capsys, ["report", "all", "--db", db])
    assert code == 0
    assert out.count("<!-- report:") == 10


def test_fire_progress_math(db):
    c = MonarchCache(db)
    try:
        prof = dict(reports.DEFAULT_PROFILE, annual_expenses_override=40000,
                    swr_pct=4.0)
        r = reports.run_report(c, prof, "fire-progress")
        # FIRE number = 25 x 40000 = 1,000,000
        assert "$1,000,000" in r["full"]
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
        # 40000 / 3.5% = ~1,142,857
        assert "28.6×" in r["full"]
    finally:
        c.close()


def test_reports_command_no_auth(capsys, db):
    # dashboard/report/reports must work without credentials (cache-only).
    code, out = _run(capsys, ["reports", "--db", db])
    assert code == 0
    assert "net-worth" in out
