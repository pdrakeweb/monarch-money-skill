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
    if getattr(args, "from_investments", False):
        return _write_holdings_from_investments(cache, data, args)
    if not args.account_id:
        raise MonarchSkillError(
            "--account-id is required for --table holdings (or pass "
            "--from-investments for the official MCP GetInvestments shape).")
    nodes = extract_holding_nodes(data)
    rows = [normalize_holding(h) for h in nodes]
    cache.write_holdings(args.account_id, rows)
    return f"Cached {len(rows)} holdings for account {args.account_id}."


def _account_name_to_id(cache):
    rows = cache._conn.execute("SELECT id, name FROM accounts").fetchall()
    return {(n or "").strip().lower(): i for i, n in rows}


def _write_holdings_from_investments(cache, data, args):
    """Official MCP GetInvestments: one row per security with
    account_holdings:[{account_name, quantity}], a combined value,
    variation_dollars (unrealized P/L), security_type, and NO cost basis (B1).

    Per security: price = value/total_qty; per-account value = price×qty;
    blended cost_basis = (value − variation_dollars) prorated by qty, NULL when
    ticker is null or security_type == 'other'. Rows grouped per account →
    write_holdings per account. Accounts must be synced first."""
    securities = _as_list(data, "holdings", "investments", "securities",
                          "results")
    name_to_id = _account_name_to_id(cache)
    if not name_to_id:
        raise MonarchSkillError(
            "No accounts in the cache — sync accounts before --from-investments.")
    per_account = {}
    unmatched = set()
    for sec in securities:
        s = sec.get("security") or {}
        ticker = sec.get("ticker") or s.get("ticker")
        name = (sec.get("name") or s.get("name") or sec.get("security_name")
                or ticker)
        sec_type = (sec.get("security_type") or s.get("type") or "").lower()
        value = _num(sec.get("value"), sec.get("total_value"),
                     sec.get("totalValue")) or 0.0
        variation = _num(sec.get("variation_dollars"),
                         sec.get("variationDollars"))
        ah = (sec.get("account_holdings") or sec.get("accountHoldings") or [])
        total_qty = sum((h.get("quantity") or 0) for h in ah) \
            or (sec.get("quantity") or 0)
        if total_qty <= 0:
            continue
        price = value / total_qty
        # Blended total basis; NULL when unknowable (no ticker or 'other').
        basis_known = ticker is not None and sec_type != "other" \
            and variation is not None
        total_basis = (value - variation) if basis_known else None
        for h in ah:
            acct_name = (h.get("account_name") or h.get("accountName") or "")
            key = acct_name.strip().lower()
            acct_id = name_to_id.get(key)
            if acct_id is None:
                unmatched.add(acct_name)
                continue
            qty = h.get("quantity") or 0
            row = {
                "ticker": ticker, "name": name, "quantity": qty,
                "value": price * qty, "price": price,
                "cost_basis": (total_basis * qty / total_qty)
                if total_basis is not None else None,
            }
            per_account.setdefault(acct_id, []).append(normalize_holding(row))
    if unmatched:
        raise MonarchSkillError(
            "GetInvestments references account name(s) not in the cache: "
            + ", ".join(sorted(unmatched))
            + ". Sync accounts first (names must match).")
    total = 0
    for acct_id, rows in per_account.items():
        cache.write_holdings(acct_id, rows)
        total += len(rows)
    return (f"Cached {total} holdings across {len(per_account)} accounts "
            "(from GetInvestments).")


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
    # B2: official MCP GetCashFlow returns net only; income & expense need two
    # calls (filters category_type income|expense). Accept a combined payload
    # {"months":[{"period","income","expense"}]} (or a bare list of such dicts)
    # and write every period in one call.
    months = None
    if isinstance(data, dict) and isinstance(data.get("months"), list):
        months = data["months"]
    elif isinstance(data, list) and data and all(
            isinstance(x, dict) and "period" in x for x in data):
        months = data
    if months is not None:
        n = 0
        for mo in months:
            period = mo.get("period")
            if not period:
                continue
            _write_one_cashflow(cache, mo, period)
            n += 1
        return f"Cached cashflow for {n} month(s)."
    if not args.period:
        raise MonarchSkillError("--period YYYY-MM is required for --table cashflow.")
    _write_one_cashflow(cache, data, args.period)
    return _cashflow_summary_line(cache, data, args.period)


def _cashflow_summary_line(cache, summary, period):
    row = cache._conn.execute(
        "SELECT income, expense FROM cashflow WHERE period=?", (period,)
    ).fetchone() or (0, 0)
    return (f"Cached cashflow for {period}. Income: {money(row[0])}, "
            f"Expenses: {money(row[1])}.")


def _write_one_cashflow(cache, data, period):
    # Accept a flat summary dict ({period,income,expense}) or the nested
    # get_cashflow_summary shape.
    if isinstance(data, dict) and isinstance(data.get("summary"), list):
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
    }, period)


# Official MCP recurring buckets — flatten income+expense, skip credit-card
# (duplicates of underlying spend) and transfer streams (B3).
_RECURRING_INCLUDE = ("recurring_income_streams", "recurring_expense_streams")
_RECURRING_SKIP = ("recurring_credit_card_streams",
                   "recurring_transfer_streams")


def _write_recurring(cache, data, args):
    raw = _as_list(data, "recurringTransactionItems", "recurring", "items")
    if not raw and isinstance(data, dict) and any(
            k in data for k in _RECURRING_INCLUDE + _RECURRING_SKIP):
        # Four-bucket official shape: each bucket wraps
        # recurring_transaction_streams:[...]. Keep income+expense only.
        raw = []
        for bucket in _RECURRING_INCLUDE:
            container = data.get(bucket) or {}
            streams = (container.get("recurring_transaction_streams")
                       if isinstance(container, dict) else container) or []
            for st in streams:
                # Map next_forecasted_date → next_date so the normaliser sees it.
                if "next_date" not in st and st.get("next_forecasted_date"):
                    st = {**st, "next_date": st["next_forecasted_date"]}
                raw.append(st)
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


def write_table(cache, table, data, account_id=None, period=None,
                from_investments=False):
    """Programmatic entry point (used by tests): write `data` for `table` into
    `cache` and return the summary string. Raises on unknown table / bad shape;
    main() turns those into the error-JSON contract."""
    writer = WRITERS.get(table)
    if writer is None:
        raise MonarchSkillError(
            f"Unknown --table '{table}'. Options: {', '.join(WRITERS)}.")
    args = argparse.Namespace(account_id=account_id, period=period,
                              from_investments=from_investments)
    return writer[0](cache, data, args)


# --------------------------------------------------------------------------- #
# Input loading
# --------------------------------------------------------------------------- #
def _unwrap_tool_result(data):
    """MCP tool results may arrive wrapped as
    [{"type":"text","text":"<json-string>"}] (the content-block envelope) —
    parse the inner JSON string (B4). Idempotent for already-unwrapped data."""
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) \
            and data[0].get("type") == "text" and "text" in data[0]:
        inner = data[0]["text"]
        if isinstance(inner, str):
            try:
                return json.loads(inner)
            except json.JSONDecodeError:
                return data
    return data


def _load_input(args):
    """Load the JSON payload: from a fixture (--mock), an --input file, or
    stdin. Returns the parsed object (tool-result envelope unwrapped)."""
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
            return _unwrap_tool_result(json.load(fh))
    text = sys.stdin.read()
    if not text.strip():
        raise MonarchSkillError(
            "No JSON on stdin and no --input/--mock given. Pipe the MCP tool "
            "output in, or pass --input FILE.")
    return _unwrap_tool_result(json.loads(text))


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
    p.add_argument("--from-investments", dest="from_investments",
                   action="store_true",
                   help="Holdings: parse the official MCP GetInvestments "
                        "aggregate shape (per-security rows with "
                        "account_holdings); no --account-id needed.")
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
                              account_id=args.account_id, period=args.period,
                              from_investments=args.from_investments)
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
