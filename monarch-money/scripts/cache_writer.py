"""
cache_writer.py — the MCP → SQLite bridge for the monarch-money skill.

In **MCP mode** the skill does NOT call Monarch directly. Instead Claude calls
a connected Monarch Money MCP server's read tools (robcerda/monarch-mcp-server,
the official monarchmoney/monarch-mcp, …) for live data, then pipes that JSON
through this script to populate the same SQLite cache fetch.py uses. Analysis
then proceeds identically — Claude queries the cache with SQL.

    <mcp_json>  | python scripts/cache_writer.py --table TABLE [options]
    python scripts/cache_writer.py --table TABLE --input data.json [options]

This is the only part of the MCP path that touches the cache; everything below
(tax-bucket classification, idempotent transaction inserts, snapshot history)
is shared verbatim with the Direct-mode fetch.py code via MonarchCache and the
fetch.py normalisers, so the two modes can never drift.

Design notes:
  * MCP tool output is **camelCase** (merchantName, accountId, isManual). We
    reuse fetch.py's normalisers, which already collapse camelCase *and* the
    live GraphQL nested shapes to the cache's snake_case columns.
  * **Missing fields degrade gracefully** — normalisers use .get(); absent
    columns land as NULL. **Unknown fields are silently dropped**, so a newer
    MCP tool version with extra keys still writes cleanly.
  * On any failure: print {"error", "message"} JSON to stdout, exit 1 — same
    machine-parseable contract as fetch.py.
"""
import argparse
import datetime as _dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from monarch_client import (  # noqa: E402
    MonarchCache, MonarchSkillError, classify_error, DEFAULT_DB_PATH,
)
# Reuse the Direct-mode normalisers so MCP-mode and fetch.py write identical
# rows from the same upstream shapes (camelCase fixtures / nested GraphQL).
from fetch import (  # noqa: E402
    normalize_account, normalize_holding, normalize_txn, normalize_budget,
    normalize_recurring, extract_holding_nodes,
)

# Same fixtures fetch.py uses, for --mock (development without credentials).
DEFAULT_FIXTURES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tests", "fixtures",
)


def money(x):
    if x is None:
        return "$0.00"
    if x < 0:
        return f"-${abs(x):,.2f}"
    return f"${x:,.2f}"


# --------------------------------------------------------------------------- #
# Shape extractors — pull the row list/dict out of whatever wrapper an MCP
# tool (or fixture) returns. MCP implementations vary; be liberal.
# --------------------------------------------------------------------------- #
def _as_list(data, *keys):
    """Return a list of records from `data`: it's already a list, or it's a
    dict wrapping the list under one of `keys` (first match wins)."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def _num(*vals):
    """First non-None numeric value among vals, else None."""
    for v in vals:
        if v is not None:
            return v
    return None


def normalize_nw_snapshot(s):
    """A single net-worth snapshot → {date, total, assets, liabilities}.

    MCP / library shapes vary: flat {date, total/netWorth/balance, assets,
    liabilities}, or nested under balance objects. Take whatever's present;
    derive total from assets-liabilities when only the components are given,
    and vice versa."""
    date = s.get("date") or s.get("day") or s.get("snapshotDate")
    assets = _num(s.get("assets"), s.get("assetsBalance"),
                  s.get("totalAssets"), s.get("assetBalance"))
    liabilities = _num(s.get("liabilities"), s.get("liabilitiesBalance"),
                       s.get("totalLiabilities"), s.get("liabilityBalance"))
    total = _num(s.get("total"), s.get("netWorth"), s.get("net_worth"),
                 s.get("balance"), s.get("value"))
    if total is None and (assets is not None or liabilities is not None):
        total = (assets or 0.0) - (liabilities or 0.0)
    if liabilities is not None and liabilities < 0:
        # Some sources report liabilities as a negative number; store positive.
        liabilities = abs(liabilities)
    return {"date": date, "total": total, "assets": assets,
            "liabilities": liabilities}


def normalize_balance_snapshot(s):
    """A single account-balance snapshot → {date, balance}."""
    date = s.get("date") or s.get("day") or s.get("snapshotDate")
    balance = _num(s.get("balance"), s.get("value"), s.get("amount"),
                   s.get("signedBalance"))
    return {"date": date, "balance": balance}


# --------------------------------------------------------------------------- #
# Per-table writers. Each takes (cache, data, args) and returns a summary line.
# --------------------------------------------------------------------------- #
def _write_accounts(cache, data, args):
    raw = _as_list(data, "accounts")
    rows = [normalize_account(a) for a in raw]
    cache.write_accounts(rows)
    net = sum((r.get("balance") or 0.0) for r in rows)
    warnings = len(cache.unclassified_investment_accounts())
    return (f"Cached {len(rows)} accounts. Net worth: {money(net)}. "
            f"Tax bucket warnings: {warnings}.")


def _write_holdings(cache, data, args):
    if not args.account_id:
        raise MonarchSkillError("--account-id is required for --table holdings.")
    nodes = extract_holding_nodes(data)
    rows = [normalize_holding(h) for h in nodes]
    cache.write_holdings(args.account_id, rows)
    return f"Cached {len(rows)} holdings for account {args.account_id}."


def _write_transactions(cache, data, args):
    raw = _as_list(data, "results", "transactions", "allTransactions")
    # Nested live shape: {"allTransactions": {"results": [...]}}
    if not raw and isinstance(data, dict):
        at = data.get("allTransactions")
        if isinstance(at, dict):
            raw = at.get("results", [])
    rows = [normalize_txn(t) for t in raw]
    cache.write_transactions(rows)
    dates = [r["date"] for r in rows if r.get("date")]
    if dates:
        cache.set_last_tx_sync(max(dates))
        return (f"Cached {len(rows)} transactions. "
                f"Date range: {min(dates)} to {max(dates)}.")
    return "Cached 0 transactions. Date range: n/a."


def _write_budgets(cache, data, args):
    if not args.period:
        raise MonarchSkillError("--period YYYY-MM is required for --table budgets.")
    raw = _as_list(data, "categories", "budgets")
    rows = [normalize_budget(b) for b in raw]
    cache.write_budgets(rows, args.period)
    return f"Cached {len(rows)} budget categories for {args.period}."


def _write_cashflow(cache, data, args):
    if not args.period:
        raise MonarchSkillError("--period YYYY-MM is required for --table cashflow.")
    # Accept a flat summary dict, the nested get_cashflow_summary shape, or
    # (e.g. from the fixture / a multi-period MCP response) a list of period
    # dicts — pick the one matching --period, else the first.
    if isinstance(data, list):
        summary = next((c for c in data if c.get("period") == args.period),
                       data[0] if data else {})
    elif isinstance(data, dict) and isinstance(data.get("summary"), list):
        s = (data["summary"][0] or {}).get("summary", {})
        summary = {"sumIncome": s.get("sumIncome"),
                   "sumExpense": s.get("sumExpense"),
                   "savings": s.get("savings"),
                   "savingsRate": s.get("savingsRate")}
    else:
        summary = data or {}
    income = _num(summary.get("income"), summary.get("sumIncome")) or 0.0
    expense = summary.get("expense")
    if expense is None:
        expense = summary.get("sumExpense")
    expense = abs(expense) if expense is not None else 0.0
    savings = _num(summary.get("savings"), income - expense)
    rate = _num(summary.get("savings_rate"), summary.get("savingsRate"))
    if rate is None:
        rate = round(savings / income * 100, 1) if income else 0.0
    cache.write_cashflow({
        "income": income, "expense": expense, "savings": savings,
        "savings_rate": rate,
        "by_category": summary.get("by_category", {}),
        "by_group": summary.get("by_group", {}),
    }, args.period)
    return (f"Cached cashflow for {args.period}. "
            f"Income: {money(income)}, Expenses: {money(expense)}.")


def _write_recurring(cache, data, args):
    raw = _as_list(data, "recurringTransactionItems", "recurring", "items")
    rows = [normalize_recurring(r) for r in raw]
    cache.write_recurring(rows)
    return f"Cached {len(rows)} recurring transactions."


def _write_net_worth_history(cache, data, args):
    raw = _as_list(data, "history", "snapshots", "netWorthHistory",
                   "aggregateSnapshots")
    rows = [normalize_nw_snapshot(s) for s in raw]
    rows = [r for r in rows if r.get("date")]
    cache.write_net_worth_history(rows)
    return f"Cached {len(rows)} net worth snapshots."


def _write_account_balance_history(cache, data, args):
    if not args.account_id:
        raise MonarchSkillError(
            "--account-id is required for --table account-balance-history.")
    raw = _as_list(data, "history", "snapshots", "balances",
                   "balanceHistory")
    rows = [normalize_balance_snapshot(s) for s in raw]
    rows = [r for r in rows if r.get("date")]
    cache.write_account_history(args.account_id, rows)
    return f"Cached {len(rows)} balance snapshots for account {args.account_id}."


# Table name (CLI) → (writer, fixture-name-or-None). Fixture name drives --mock.
WRITERS = {
    "accounts": (_write_accounts, "accounts"),
    "holdings": (_write_holdings, "holdings"),
    "transactions": (_write_transactions, "transactions"),
    "budgets": (_write_budgets, "budgets"),
    "cashflow": (_write_cashflow, "cashflow"),
    "recurring": (_write_recurring, "recurring"),
    "net-worth-history": (_write_net_worth_history, None),
    "account-balance-history": (_write_account_balance_history, None),
}


def write_table(cache, table, data, account_id=None, period=None):
    """Programmatic entry point (used by tests): write `data` for `table` into
    `cache` and return the summary string. Raises on unknown table / bad shape;
    main() turns those into the error-JSON contract."""
    writer = WRITERS.get(table)
    if writer is None:
        raise MonarchSkillError(
            f"Unknown --table '{table}'. Options: {', '.join(WRITERS)}.")
    args = argparse.Namespace(account_id=account_id, period=period)
    return writer[0](cache, data, args)


# --------------------------------------------------------------------------- #
# Input loading
# --------------------------------------------------------------------------- #
def _load_input(args):
    """Load the JSON payload: from a fixture (--mock), an --input file, or
    stdin. Returns the parsed object."""
    if args.mock:
        _, fixture = WRITERS[args.table]
        if fixture is None:
            raise MonarchSkillError(
                f"--mock has no fixture for --table {args.table}.")
        path = os.path.join(args.fixtures_dir, f"{fixture}.json")
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    if args.input:
        with open(args.input, "r", encoding="utf-8") as fh:
            return json.load(fh)
    text = sys.stdin.read()
    if not text.strip():
        raise MonarchSkillError(
            "No JSON on stdin and no --input/--mock given. Pipe the MCP tool "
            "output in, or pass --input FILE.")
    return json.loads(text)


def build_parser():
    p = argparse.ArgumentParser(
        description="Bridge Monarch MCP tool output into the skill's SQLite "
                    "cache (MCP mode). See SKILL.md 'Dual-Mode Operation'.")
    p.add_argument("--table", required=True, choices=list(WRITERS),
                   help="Which cache table to populate.")
    p.add_argument("--input", help="Path to a JSON file (default: read stdin).")
    p.add_argument("--account-id", dest="account_id",
                   help="Account id (required for holdings / "
                        "account-balance-history).")
    p.add_argument("--period", help="YYYY-MM (required for budgets / cashflow).")
    p.add_argument("--db", default=DEFAULT_DB_PATH,
                   help="SQLite cache path (default: the persistent data dir, "
                        "same as fetch.py).")
    p.add_argument("--mock", action="store_true",
                   help="Load tests/fixtures instead of stdin/--input.")
    p.add_argument("--fixtures-dir", default=DEFAULT_FIXTURES_DIR)
    return p


def main(argv=None):
    # Force UTF-8 stdout so the cache summary glyphs never crash a cp1252
    # Windows console (the skill container is Linux/UTF-8 already).
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv)
    cache = None
    try:
        data = _load_input(args)
        cache = MonarchCache(args.db)
        summary = write_table(cache, args.table, data,
                              account_id=args.account_id, period=args.period)
        print(summary)
        return 0
    except MonarchSkillError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}))
        return 1
    except Exception as exc:  # noqa: BLE001 — classify everything else
        classified = classify_error(exc)
        print(json.dumps({"error": type(classified).__name__,
                          "message": str(classified)}))
        return 1
    finally:
        if cache is not None:
            cache.close()


if __name__ == "__main__":
    sys.exit(main())
