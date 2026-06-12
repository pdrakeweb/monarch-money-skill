"""
fetch.py — data retrieval + caching CLI for the monarch-money skill.

Every subcommand:
  1. (Unless --mock or a no-API command) authenticates a saved session.
  2. Honours the cache TTL — skips the network when data is fresh, unless
     --refresh is passed.
  3. With --mock, loads anonymised fixture JSON instead of hitting Monarch.
  4. Writes normalised rows to the SQLite cache and bumps the call counter.
  5. Prints a COMPACT Markdown summary to stdout — never raw dumps.

Claude then queries data/monarch_cache.db directly (pandas/SQL) for analysis.

Subcommands:
  accounts | holdings | transactions | budgets | cashflow | recurring
  net-worth | sync-all | status | auth-status

Global flags: --refresh (bypass TTL), --mock (use fixtures),
              --db PATH, --fixtures-dir DIR, --session-path, --cred-path
"""
import argparse
import asyncio
import datetime as _dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from monarch_client import (  # noqa: E402
    MonarchCache, MonarchClient, MonarchSkillError, classify_error,
    SKILL_VERSION,
    DEFAULT_DB_PATH, DEFAULT_SESSION_PATH, DEFAULT_CRED_PATH,
    DEFAULT_COOKIE_PATH, DEFAULT_DATA_DIR,
)

TTL_4H = 4 * 3600
TTL_24H = 24 * 3600
DEFAULT_FIXTURES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tests", "fixtures",
)


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def money(x):
    if x is None:
        return "—"
    if x < 0:
        return f"-${abs(x):,.2f}"
    return f"${x:,.2f}"


def age_phrase(ts):
    if not ts:
        return "never"
    sec = int(time.time() - ts)
    if sec < 90:
        return "just now"
    mins = sec // 60
    if mins < 60:
        return f"{mins} min ago"
    hrs = mins // 60
    if hrs < 48:
        return f"{hrs} hr ago"
    return f"{hrs // 24} days ago"


def _today():
    return _dt.date.today()


# --------------------------------------------------------------------------- #
# Normalisers: source (fixture camelCase / live GraphQL) -> internal snake_case
#
# The live API nests most labels as objects ({"name": ...}); the fixtures use
# flat strings. _name_of() collapses both to a plain string so the SQLite cache
# never stores a dict repr.
# --------------------------------------------------------------------------- #
def _name_of(v):
    """Flatten a GraphQL label that may be a string or a {name: ...} object."""
    if isinstance(v, dict):
        return v.get("name")
    return v


# Official Monarch MCP GetAccounts returns human DISPLAY types in `type` with
# NO subtype (e.g. "Roth IRA", "401k", "Brokerage (Taxable)"). The tax-bucket
# classifier keys off subtype and the reports filter type IN
# ('investment','brokerage') — so without this map every MCP investment account
# landed in bucket 'other' and fell out of every bucket-dependent report (A2).
# Applied only when subtype is absent (Direct-mode GraphQL already supplies it).
_DISPLAY_TYPE_MAP = {
    "roth ira": ("investment", "roth_ira"),
    "roth 401k": ("investment", "roth_401k"),
    "401k": ("investment", "st_401k"),
    "403b": ("investment", "st_403b"),
    "individual retirement account (ira)": ("investment", "ira"),
    "ira": ("investment", "ira"),
    "health savings account (hsa)": ("investment", "hsa"),
    "hsa": ("investment", "hsa"),
    "trust": ("brokerage", "trust"),
    "brokerage (taxable)": ("brokerage", "brokerage"),
    "brokerage": ("brokerage", "brokerage"),
    "taxable": ("brokerage", "brokerage"),
    "stock plan": ("brokerage", "brokerage"),
}


def canonical_account_type(display_type, subtype):
    """(type, subtype) for the cache. When subtype is present (Direct mode),
    pass through. When absent (MCP display type), map known display types;
    'Other' stays 'Other' so the name-based charitable check runs; unknown
    types pass through unchanged (they'll surface via the unclassified warning
    if they're investment-ish)."""
    if subtype:
        return display_type, subtype
    key = (display_type or "").lower().strip()
    if key in _DISPLAY_TYPE_MAP:
        return _DISPLAY_TYPE_MAP[key]
    return display_type, subtype


def normalize_account(a):
    bal = a.get("balance", a.get("currentBalance"))
    typ, sub = canonical_account_type(_name_of(a.get("type")),
                                      _name_of(a.get("subtype")))
    return {
        "id": a.get("id"),
        "name": a.get("name") or a.get("displayName"),
        "type": typ,
        "subtype": sub,
        "institution": _name_of(a.get("institution")),
        "balance": bal,
        "display_balance": a.get("displayBalance", bal),
        "currency": a.get("currency", "USD"),
        "is_manual": a.get("isManual", a.get("is_manual", False)),
        "is_hidden": a.get("isHidden", a.get("is_hidden", False)),
        "is_closed": a.get("isClosed", a.get("is_closed", False)),
        "updated_at": a.get("updatedAt", a.get("updated_at")),
    }


def normalize_holding(h):
    # Live shape: portfolio.aggregateHoldings.edges[].node =
    #   {quantity, basis, totalValue, security{ticker, name, currentPrice}}
    sec = h.get("security") or {}
    # Monarch reports basis=0 for positions where the custodian provides no
    # basis (e.g. employer-plan funds). 0 means UNKNOWN, not free shares —
    # store NULL so P/L renders as em-dash and TLH screens exclude the row
    # instead of computing a 100% phantom gain (QA BUG-5).
    basis = h.get("costBasis", h.get("cost_basis", h.get("basis")))
    return {
        "ticker": h.get("ticker") or sec.get("ticker"),
        "name": h.get("name") or sec.get("name"),
        "quantity": h.get("quantity"),
        "value": h.get("value", h.get("totalValue")),
        "price": h.get("price", sec.get("currentPrice")),
        "cost_basis": basis if basis else None,
    }


def extract_holding_nodes(data):
    """Pull holding nodes out of the live get_account_holdings() response."""
    try:
        edges = data["portfolio"]["aggregateHoldings"]["edges"]
        return [e["node"] for e in edges if e.get("node")]
    except (KeyError, TypeError):
        # Fall back to any flat list the response (or a fixture) may carry.
        if isinstance(data, list):
            return data
        return data.get("holdings", []) if isinstance(data, dict) else []


def normalize_txn(t):
    # Live shape: merchant{name}, category{name, group{...}}, pending,
    # account{id}. Fixtures: flat merchantName/category/categoryGroup/isPending.
    cat = t.get("category")
    group = t.get("categoryGroup", t.get("category_group"))
    if isinstance(cat, dict):
        group = group or _name_of(cat.get("group"))
        cat = cat.get("name")
    acct = t.get("accountId", t.get("account_id"))
    if acct is None and isinstance(t.get("account"), dict):
        acct = t["account"].get("id")
    return {
        "id": t.get("id"),
        "account_id": acct,
        "date": t.get("date"),
        "amount": t.get("amount"),
        "merchant": t.get("merchantName") or _name_of(t.get("merchant")),
        "category": cat,
        "category_group": group,
        "tags": [_name_of(x) for x in (t.get("tags") or [])],
        "notes": t.get("notes", ""),
        "is_pending": t.get("isPending", t.get("is_pending", t.get("pending", False))),
    }


def normalize_budget(b):
    return {
        "category_id": b.get("categoryId", b.get("category_id")),
        "category": b.get("category"),
        "category_group": b.get("categoryGroup", b.get("category_group")),
        "budgeted": b.get("budgeted"),
        "actual": b.get("actual"),
    }


def normalize_recurring(r):
    # Live shape: {stream{id, frequency, merchant{name}}, date, amount,
    # account{id}}. Fixtures: flat id/merchant/frequency/next_date.
    stream = r.get("stream") or {}
    acct = r.get("accountId", r.get("account_id"))
    if acct is None and isinstance(r.get("account"), dict):
        acct = r["account"].get("id")
    return {
        "id": r.get("id") or stream.get("id") or r.get("transactionId"),
        "merchant": r.get("merchant") if isinstance(r.get("merchant"), str)
                    else _name_of(stream.get("merchant")) or _name_of(r.get("merchant")),
        "amount": r.get("amount", stream.get("amount")),
        "frequency": r.get("frequency", stream.get("frequency")),
        "next_date": r.get("next_date", r.get("nextDate", r.get("date"))),
        "account_id": acct,
    }


# --------------------------------------------------------------------------- #
# Fixture loading
# --------------------------------------------------------------------------- #
def load_fixture(fixtures_dir, name):
    path = os.path.join(fixtures_dir, f"{name}.json")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# Sync helpers — write fresh data into the cache (or skip if TTL-fresh)
# --------------------------------------------------------------------------- #
def _need_sync(cache, table, ttl, refresh):
    return refresh or not cache.any_fresh(table, ttl)


def sync_accounts(ctx):
    cache = ctx.cache
    if not _need_sync(cache, "accounts", TTL_4H, ctx.refresh):
        return False
    if ctx.mock:
        raw = load_fixture(ctx.fixtures_dir, "accounts")
    else:
        raw = asyncio.run(ctx.client.mm.get_accounts())
        raw = raw.get("accounts", raw) if isinstance(raw, dict) else raw
    cache.write_accounts([normalize_account(a) for a in raw])
    cache.increment_api_calls()
    return True


def sync_holdings(ctx):
    cache = ctx.cache
    if not _need_sync(cache, "holdings", TTL_4H, ctx.refresh):
        return False
    if ctx.mock:
        raw = load_fixture(ctx.fixtures_dir, "holdings")
        by_acct = {}
        for h in raw:
            by_acct.setdefault(h.get("accountId"), []).append(normalize_holding(h))
        for acct_id, rows in by_acct.items():
            cache.write_holdings(acct_id, rows)
        cache.increment_api_calls()
    else:
        # Monarch's live type vocabulary is 'brokerage'; 'investment' kept
        # for fixtures/older data (QA field finding).
        inv = cache._conn.execute(
            "SELECT id FROM accounts WHERE type IN ('investment','brokerage')"
        ).fetchall()
        for (acct_id,) in inv:
            data = asyncio.run(ctx.client.mm.get_account_holdings(acct_id))
            nodes = extract_holding_nodes(data)
            cache.write_holdings(acct_id, [normalize_holding(h) for h in nodes])
            cache.increment_api_calls()  # one GraphQL call per account
    return True


def _tx_sync_start(last, earliest, days, refresh, today):
    """Pure: pick the transaction-sync start date. Incremental resume from the
    last sync (−3d overlap), BUT widen to the requested `days` window when it
    reaches back further than what's been cached (tracked by `earliest`), or
    when `refresh` forces the full window. Fixes `--days 365` being a no-op
    after a prior sync."""
    desired_start = today - _dt.timedelta(days=days)
    if last:
        start = _dt.date.fromisoformat(last) - _dt.timedelta(days=3)
    else:
        start = desired_start
    if refresh or earliest is None or desired_start < _dt.date.fromisoformat(earliest):
        start = min(start, desired_start)
    return start


def sync_transactions(ctx):
    cache = ctx.cache
    if not _need_sync(cache, "transactions", TTL_24H, ctx.refresh):
        return False
    if ctx.mock:
        raw = load_fixture(ctx.fixtures_dir, "transactions")
    else:
        # Incremental resume from the last sync (−3d overlap; idempotent inserts
        # make the overlap harmless), BUT widen to the requested --days window
        # when it asks for history not yet cached — otherwise `--days 365` is a
        # no-op after any prior sync (it always resumed from last−3d). We track
        # the earliest date ever synced so a deeper request pulls the gap; an
        # explicit --refresh also forces the full requested window.
        last = cache.get_last_tx_sync()
        earliest = cache.get_meta("tx_earliest_synced")
        start = _tx_sync_start(last, earliest, ctx.days, ctx.refresh, _today())
        # get_transactions caps at `limit` per call — page until exhausted.
        raw = []
        offset = 0
        page = 100
        while True:
            data = asyncio.run(ctx.client.mm.get_transactions(
                limit=page, offset=offset,
                start_date=start.isoformat(), end_date=_today().isoformat()))
            batch = (data.get("allTransactions", {}).get("results", [])
                     if isinstance(data, dict) else data) or []
            raw.extend(batch)
            cache.increment_api_calls()
            if len(batch) < page:
                break
            offset += page
        # Remember the earliest window boundary we've fetched.
        new_earliest = start.isoformat()
        if earliest:
            new_earliest = min(earliest, new_earliest)
        cache.set_meta("tx_earliest_synced", new_earliest)
    rows = [normalize_txn(t) for t in raw]
    cache.write_transactions(rows)
    cache.set_last_tx_sync(_today().isoformat())
    if ctx.mock:
        cache.increment_api_calls()  # live path counts per page above
    return True


def sync_budgets(ctx, period):
    cache = ctx.cache
    if not _need_sync(cache, "budgets", TTL_4H, ctx.refresh):
        return period, False
    if ctx.mock:
        data = load_fixture(ctx.fixtures_dir, "budgets")
        period = data.get("period", period)
        rows = [normalize_budget(b) for b in data.get("categories", [])]
    else:
        data = asyncio.run(ctx.client.mm.get_budgets())
        rows = [normalize_budget(b) for b in _extract_budget_rows(data)]
    cache.write_budgets(rows, period)
    cache.increment_api_calls()
    return period, True


def _extract_budget_rows(data):
    # Live shape (QA-verified): category nodes under monthlyAmountsByCategory
    # carry only {id, __typename}; the names live under
    # categoryGroups[].categories[]. Build id -> (name, group) first.
    name_by_id = {}
    for g in (data.get("categoryGroups") or []):
        gname = g.get("name")
        for ct in (g.get("categories") or []):
            name_by_id[ct.get("id")] = (ct.get("name"), gname)
    out = []
    try:
        for item in data.get("budgetData", {}).get("monthlyAmountsByCategory", []):
            cid = (item.get("category") or {}).get("id")
            nm, grp = name_by_id.get(cid, (None, None))
            amts = (item.get("monthlyAmounts") or [{}])[0]
            out.append({
                "categoryId": cid, "category": nm, "categoryGroup": grp,
                "budgeted": amts.get("plannedCashFlowAmount"),
                "actual": amts.get("actualAmount"),
            })
    except (AttributeError, TypeError, KeyError):
        pass
    return out


def _month_bounds(period):
    """(first-day, last-day) ISO dates for a YYYY-MM period string."""
    year, month = int(period[:4]), int(period[5:7])
    first = _dt.date(year, month, 1)
    if month == 12:
        nxt = _dt.date(year + 1, 1, 1)
    else:
        nxt = _dt.date(year, month + 1, 1)
    return first.isoformat(), (nxt - _dt.timedelta(days=1)).isoformat()


def _shift_month(period, delta):
    year, month = int(period[:4]), int(period[5:7])
    total = year * 12 + (month - 1) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def sync_cashflow(ctx, months):
    cache = ctx.cache
    if not _need_sync(cache, "cashflow", TTL_4H, ctx.refresh):
        return False
    if ctx.mock:
        data = load_fixture(ctx.fixtures_dir, "cashflow")
        for cf in data:
            cache.write_cashflow(cf, cf["period"])
        cache.increment_api_calls()
        return True
    # Live: one summary call per requested month (closed months only refresh
    # when missing — they never change).
    current = _today().strftime("%Y-%m")
    for delta in range(-(months - 1), 1):
        period = _shift_month(current, delta)
        if period != current and cache.is_fresh("cashflow", "period", period, 10**12):
            continue  # closed month already cached — permanent
        start, end = _month_bounds(period)
        summary = asyncio.run(ctx.client.mm.get_cashflow_summary(
            start_date=start, end_date=end))
        s = (summary.get("summary", [{}])[0].get("summary", {})
             if isinstance(summary, dict) else {})
        income = s.get("sumIncome", 0.0) or 0.0
        expense = abs(s.get("sumExpense", 0.0) or 0.0)
        savings = s.get("savings", income - expense)
        rate = round(savings / income * 100, 1) if income else 0.0
        cache.write_cashflow(
            {"income": income, "expense": expense, "savings": savings,
             "savings_rate": rate, "by_category": {}, "by_group": {}}, period)
        cache.increment_api_calls()
    return True


def sync_recurring(ctx):
    cache = ctx.cache
    if not _need_sync(cache, "recurring", TTL_4H, ctx.refresh):
        return False
    if ctx.mock:
        raw = load_fixture(ctx.fixtures_dir, "recurring")
    else:
        data = asyncio.run(ctx.client.mm.get_recurring_transactions())
        raw = data.get("recurringTransactionItems", data) if isinstance(data, dict) else data
    cache.write_recurring([normalize_recurring(r) for r in (raw or [])])
    cache.increment_api_calls()
    return True


# --------------------------------------------------------------------------- #
# Display renderers (read from cache, print Markdown)
# --------------------------------------------------------------------------- #
def render_accounts(cache):
    rows = cache._conn.execute(
        "SELECT type, institution, name, balance, fetched_at FROM accounts "
        "WHERE is_hidden=0 AND is_closed=0 ORDER BY type, balance DESC"
    ).fetchall()
    age = cache._conn.execute("SELECT MAX(fetched_at) FROM accounts").fetchone()[0]
    print(f"## Accounts (synced {age_phrase(age)}, {len(rows)} accounts)\n")
    print("| Type | Institution | Account | Balance |")
    print("|------|-------------|---------|---------|")
    assets = liabilities = 0.0
    for typ, inst, name, bal, _ in rows:
        print(f"| {(typ or '').title()} | {inst or ''} | {name or ''} | {money(bal)} |")
        if bal and bal >= 0:
            assets += bal
        elif bal:
            liabilities += abs(bal)
    net = assets - liabilities
    print(f"\n**Net Worth: {money(net)}**")
    print(f"Assets: {money(assets)} | Liabilities: {money(liabilities)}")
    unclassified = cache.unclassified_investment_accounts()
    if unclassified:
        print(f"\n⚠ {len(unclassified)} investment account(s) have unclassified "
              f"tax buckets — ask Claude to list and correct them.")


def render_holdings(cache):
    rows = cache._conn.execute(
        "SELECT h.account_id, a.name, h.ticker, h.name, h.quantity, h.value, "
        "       h.cost_basis, h.fetched_at "
        "FROM holdings h LEFT JOIN accounts a ON h.account_id = a.id "
        "ORDER BY h.value DESC"
    ).fetchall()
    age = cache._conn.execute("SELECT MAX(fetched_at) FROM holdings").fetchone()[0]
    accts = {r[0] for r in rows}
    print(f"## Holdings (synced {age_phrase(age)}, {len(rows)} positions "
          f"across {len(accts)} accounts)\n")
    print("| Account | Ticker | Name | Qty | Value | Unrealized P/L |")
    print("|---------|--------|------|-----|-------|----------------|")
    total = 0.0
    no_basis = 0
    for acct_id, acct_name, ticker, hname, qty, value, basis, _ in rows:
        total += value or 0.0
        if basis is None:
            pl = "—"
            no_basis += 1
        else:
            pl = money((value or 0) - basis)
        print(f"| {acct_name or acct_id} | {ticker} | {hname or ''} | "
              f"{qty:g} | {money(value)} | {pl} |")
    print(f"\n**Total Portfolio: {money(total)}**")
    # Top 5 by value
    top = rows[:5]
    parts = []
    for r in top:
        ticker, value = r[2], r[5]
        pct = (value / total * 100) if total else 0
        parts.append(f"{ticker} {money(value)} ({pct:.1f}%)")
    if parts:
        print("Top 5 by value: " + ", ".join(parts))
    if no_basis:
        print(f"Note: {no_basis} position(s) have no cost basis data from Monarch.")


def render_transactions(cache, days, account, category, mock,
                        include_excluded=False):
    cutoff = "1900-01-01" if mock else (
        (_today() - _dt.timedelta(days=days)).isoformat())
    # Alias the table `t` so the exclusion filter fragment can reference its
    # columns (t.id / t.category / t.merchant / t.date).
    base = "FROM transactions t WHERE t.date >= ?"
    params = [cutoff]
    if account:
        base += " AND t.account_id = ?"
        params.append(account)
    if category:
        base += " AND t.category = ?"
        params.append(category)

    # Exclusions: scope 'all' for the raw transaction view. Count how many rows
    # the filter removes so the summary can disclose it. --include-excluded
    # bypasses filtering entirely (raw view).
    excl_frag, excl_params, n_excluded = "", [], 0
    if not include_excluded:
        frag, fparams = cache.get_exclusion_filter_sql("all", "t")
        total = cache._conn.execute(
            f"SELECT COUNT(*) {base}", params).fetchone()[0]
        kept = cache._conn.execute(
            f"SELECT COUNT(*) {base}{frag}", params + fparams).fetchone()[0]
        n_excluded = total - kept
        excl_frag, excl_params = frag, fparams

    q = (f"SELECT t.date, t.amount, t.merchant, t.category {base}{excl_frag} "
         f"ORDER BY t.date")
    rows = cache._conn.execute(q, params + excl_params).fetchall()
    age = cache._conn.execute("SELECT MAX(fetched_at) FROM transactions").fetchone()[0]
    print(f"## Transactions — last {days} days (synced {age_phrase(age)}, "
          f"{len(rows)} transactions)\n")
    if not rows:
        print("No transactions in window.")
    else:
        dates = [r[0] for r in rows]
        inflow = sum(r[1] for r in rows if r[1] and r[1] > 0)
        outflow = sum(r[1] for r in rows if r[1] and r[1] < 0)
        print(f"Date range: {min(dates)} to {max(dates)}")
        print(f"Total inflow: {money(inflow)} | Total outflow: {money(outflow)} "
              f"| Net: {money(inflow + outflow)}\n")
        # Top merchants by spend
        spend = {}
        counts = {}
        for _, amt, merch, _ in rows:
            if amt and amt < 0:
                spend[merch] = spend.get(merch, 0) + abs(amt)
                counts[merch] = counts.get(merch, 0) + 1
        top = sorted(spend.items(), key=lambda kv: kv[1], reverse=True)[:5]
        if top:
            print("Top merchants by spend:")
            for i, (merch, amt) in enumerate(top, 1):
                print(f"{i}. {merch} — {money(amt)} ({counts[merch]} transactions)")
    if n_excluded > 0:
        print(f"\n*Note: {n_excluded} transaction(s) excluded as non-recurring "
              f"(see `manage_exclusions.py list`).*")


def _scope_exclusion_note(cache, scope):
    """The cashflow/budgets cache tables hold Monarch's server-side aggregates,
    which can't be transaction-filtered locally. If exclusions are configured
    for `scope`, return an honest disclosure line (else '')."""
    try:
        n = cache._conn.execute(
            "SELECT COUNT(*) FROM exclusions "
            "WHERE exclude_from = 'all' OR exclude_from = ?", (scope,)
        ).fetchone()[0]
    except Exception:  # noqa: BLE001 — table absent on a pre-v3 db
        return ""
    if not n:
        return ""
    return (f"\n*Note: {n} exclusion(s) configured for {scope}. These are "
            f"Monarch's server-side totals and are NOT exclusion-adjusted — "
            f"query the transactions table (exclusion-aware) for adjusted "
            f"spend, or run `manage_exclusions.py list`.*")


def render_budgets(cache, period, include_excluded=False):
    rows = cache._conn.execute(
        "SELECT category, category_group, budgeted, actual, fetched_at "
        "FROM budgets WHERE period = ? ORDER BY category", (period,)
    ).fetchall()
    age = cache._conn.execute(
        "SELECT MAX(fetched_at) FROM budgets WHERE period=?", (period,)
    ).fetchone()[0]
    print(f"## Budget — {period} (synced {age_phrase(age)})\n")
    tot_b = sum(r[2] or 0 for r in rows)
    tot_a = sum(r[3] or 0 for r in rows)
    print(f"Total budgeted: {money(tot_b)} | Actual: {money(tot_a)} "
          f"| Remaining: {money(tot_b - tot_a)}\n")
    over = [r for r in rows if (r[3] or 0) > (r[2] or 0)]
    if over:
        print("Over budget:")
        for cat, _, b, a, _ in over:
            print(f"- {cat}: budgeted {money(b)}, actual {money(a)} "
                  f"(↑{money((a or 0) - (b or 0))})")
    else:
        print("All categories within budget.")
    if not include_excluded:
        note = _scope_exclusion_note(cache, "budget")
        if note:
            print(note)


def render_cashflow(cache, months, month=None, include_excluded=False):
    if month:
        rows = cache._conn.execute(
            "SELECT period, income, expense, savings, savings_rate FROM cashflow "
            "WHERE period = ?", (month,)
        ).fetchall()
        months = 1
    else:
        rows = cache._conn.execute(
            "SELECT period, income, expense, savings, savings_rate FROM cashflow "
            "ORDER BY period DESC LIMIT ?", (months,)
        ).fetchall()
    age = cache._conn.execute("SELECT MAX(fetched_at) FROM cashflow").fetchone()[0]
    if not rows:
        target = month or "requested window"
        print(f"## Cashflow\n\nNo cashflow data cached for {target}. "
              f"Run `fetch.py cashflow --months N --refresh` to sync.")
        return
    if months == 1:
        period, income, expense, savings, rate = rows[0]
        print(f"## Cashflow — {period} (synced {age_phrase(age)})\n")
        print(f"Income:   {money(income)}")
        print(f"Expenses: {money(expense)}")
        print(f"Savings:  {money(savings)} ({rate:.1f}% savings rate)")
    else:
        print(f"## Cashflow — last {len(rows)} months (synced {age_phrase(age)})\n")
        print("| Month | Income | Expenses | Savings | Savings Rate |")
        print("|-------|--------|----------|---------|--------------|")
        for period, income, expense, savings, rate in reversed(rows):
            print(f"| {period} | {money(income)} | {money(expense)} | "
                  f"{money(savings)} | {rate:.1f}% |")
        print("\n(Per-month savings rate shown; see SQLite cashflow table for detail.)")
    if not include_excluded:
        note = _scope_exclusion_note(cache, "cashflow")
        if note:
            print(note)


def render_recurring(cache):
    rows = cache._conn.execute(
        "SELECT merchant, amount, frequency, next_date, fetched_at "
        "FROM recurring ORDER BY next_date"
    ).fetchall()
    age = cache._conn.execute("SELECT MAX(fetched_at) FROM recurring").fetchone()[0]
    print(f"## Recurring (synced {age_phrase(age)}, {len(rows)} active)\n")
    monthly = 0.0
    for _, amt, freq, _, _ in rows:
        if amt and amt < 0:
            mult = {"monthly": 1, "biweekly": 2.17, "weekly": 4.33,
                    "yearly": 1 / 12, "quarterly": 1 / 3}.get((freq or "").lower(), 1)
            monthly += abs(amt) * mult
    print(f"Monthly total: ~{money(monthly)}\n")
    print("Next 7 upcoming:")
    for merch, amt, freq, nxt, _ in rows[:7]:
        print(f"- {nxt}: {merch} ({money(amt)}, {freq})")


def render_net_worth(cache):
    rows = cache._conn.execute(
        "SELECT type, balance FROM accounts WHERE is_hidden=0 AND is_closed=0"
    ).fetchall()
    age = cache._conn.execute("SELECT MAX(fetched_at) FROM accounts").fetchone()[0]
    if not rows:
        print("## Net Worth\n\nNo accounts cached yet — run "
              "`python scripts/fetch.py accounts` first.")
        return
    by_type = {}
    for typ, bal in rows:
        by_type[typ] = by_type.get(typ, 0) + (bal or 0)
    assets = sum(v for v in by_type.values() if v >= 0)
    liabilities = sum(-v for v in by_type.values() if v < 0)
    # Monarch's live vocabulary (depository/brokerage/real_estate/...) plus
    # the older flat names; anything unmapped falls back to a title-cased
    # type so no account category is silently dropped (QA BUG-3).
    labels = {"depository": "Cash", "brokerage": "Investments",
              "investment": "Investments", "real_estate": "Real Estate",
              "checking": "Checking", "savings": "Savings",
              "credit": "Credit", "loan": "Loans", "other": "Other"}

    def _label(typ):
        return labels.get(typ, (typ or "other").replace("_", " ").title())

    print(f"## Net Worth (from cached accounts, last synced {age_phrase(age)})\n")
    print(f"Assets:      {money(assets)}")
    for typ, v in sorted(by_type.items(), key=lambda kv: -kv[1]):
        if v >= 0:
            print(f"  {_label(typ)}: {money(v)}")
    print(f"Liabilities: {money(liabilities)}")
    for typ, v in sorted(by_type.items(), key=lambda kv: kv[1]):
        if v < 0:
            print(f"  {_label(typ)}: {money(abs(v))}")
    print(f"\nNet Worth: {money(assets - liabilities)}")


def render_status(cache):
    st = cache.status()
    size_mb = st["db_size_bytes"] / (1024 * 1024)
    print("## Cache Status\n")
    print(f"Skill version: {SKILL_VERSION}")
    print(f"Database: {st['db_path']} ({size_mb:.1f} MB)")
    for table in ("accounts", "holdings", "transactions", "budgets",
                  "cashflow", "recurring"):
        print(f"{table}: {st[f'{table}_rows']} rows, last synced "
              f"{age_phrase(st[f'{table}_last_sync'])}")
    print(f"Total API calls (lifetime): {st['api_call_count']}")


def render_auth_status(session_path, cred_path, cookie_path):
    from skill_common import data_dir_persistence
    data_dir = os.path.dirname(os.path.abspath(session_path))
    kind, why = data_dir_persistence(data_dir)
    print("## Auth Status\n")
    print(f"Data directory: {data_dir}")
    mark = "✓" if kind in ("home",) else ("⚠" if kind in ("skill", "temp") else "•")
    print(f"  {mark} Persistence: {kind} — {why}")
    env_path = os.path.join(data_dir, ".env")
    print(f"Session file: {session_path}")
    if os.path.exists(session_path):
        mtime = os.path.getmtime(session_path)
        dt = _dt.datetime.fromtimestamp(mtime)
        days = int((time.time() - mtime) / 86400)
        print(f"Last modified: {dt:%Y-%m-%d %H:%M} ({days} days ago)")
        try:
            import pickle
            with open(session_path, "rb") as fh:
                data = pickle.load(fh)
            mode = data.get("auth_mode", "token") if isinstance(data, dict) else "token"
            print(f"Auth mode: {mode}")
        except Exception:  # noqa: BLE001 - diagnostic only
            pass
        print("Estimated expiry: sessions typically last weeks to months "
              "(cookie sessions may rotate sooner)")
    else:
        print("Last modified: (no saved session)")
    if os.path.exists(cookie_path):
        mtime = os.path.getmtime(cookie_path)
        days = int((time.time() - mtime) / 86400)
        print(f"Cookie session: present, saved {days} days ago (Option D)")
    else:
        print("Cookie session: not present")
    if os.path.exists(cred_path):
        print("Credentials file: present (Option B — zero-touch reauth enabled)")
    else:
        print("Credentials file: not present")
    print(f"Env file: {'present' if os.path.exists(env_path) else 'not present'} "
          f"({env_path})")
    if not os.path.exists(session_path) and not os.path.exists(cookie_path) \
            and not os.path.exists(cred_path) and not os.path.exists(env_path):
        print("\nNo auth configured — run scripts/setup_auth.py first.")
        if kind == "cloud":
            print("Note: this is a per-conversation container. For auth that "
                  "survives new chats / other devices, bundle a .env via "
                  "`build.ps1 -IncludeAuth` (see references/persistence.md).")


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
class Ctx:
    def __init__(self, args, cache, client):
        self.args = args
        self.cache = cache
        self.client = client
        self.mock = args.mock
        self.refresh = args.refresh
        self.fixtures_dir = args.fixtures_dir
        self.days = getattr(args, "days", 30)


NO_AUTH = {"net-worth", "status", "auth-status", "clear-cache", "version",
           "report", "dashboard", "reports", "profile",
           "config-export", "config-import"}

# Durability backstop: the persistent data dir survives across sessions on
# desktop, but on cloud (claude.ai/mobile) every new conversation is a FRESH
# container — so the user's non-reproducible config (exclusions + corrected tax
# buckets + profile.json) is effectively per-chat unless exported. config-export
# writes ONE human-readable **Markdown** file: the profile + config as
# sections/tables with the machine-restorable bundle in a fenced ```json block
# at the end. Drop it into a Claude Desktop Project's Files (every chat can read
# it) — readable by a person AND re-importable by config-import. Synced Monarch
# data is excluded (sync-all regenerates it). Mirrors the skeleton.
CONFIG_BUNDLE_KIND = "config-export/v1"
CONFIG_FILES = ["profile.json"]


def _config_bundle(cache, data_dir):
    files = {}
    for fname in CONFIG_FILES:
        path = os.path.join(data_dir, fname)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    files[fname] = fh.read()
            except OSError:
                pass
    return {
        "kind": CONFIG_BUNDLE_KIND, "skill": "monarch-money",
        "skill_version": SKILL_VERSION,
        "schema_version": cache.get_meta("schema_version"),
        "exported_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "cache": cache.export_config(), "files": files,
    }


def _cfg_scalar(v):
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _cfg_md_lines(obj, depth=0):
    pad = "  " * depth
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                out.append(f"{pad}- **{k}:**")
                out.extend(_cfg_md_lines(v, depth + 1))
            else:
                out.append(f"{pad}- **{k}:** {_cfg_scalar(v)}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                out.append(f"{pad}- " + ", ".join(
                    f"{k}={_cfg_scalar(v)}" for k, v in item.items()))
            else:
                out.append(f"{pad}- {_cfg_scalar(item)}")
    else:
        out.append(f"{pad}- {_cfg_scalar(obj)}")
    return out


def _cfg_md_table(columns, rows):
    out = ["| " + " | ".join(columns) + " |",
           "|" + "|".join(["---"] * len(columns)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_cfg_scalar(c) for c in r) + " |")
    return out


def _render_config_markdown(bundle):
    lines = [
        "# monarch-money — financial config export",
        f"*Exported {bundle['exported_at']} by the **monarch-money** skill "
        f"(v{bundle['skill_version']}). Human-readable; the JSON block at the "
        "end is the machine-restorable copy. To restore: save this file and run "
        "`config-import --in <file>`, or attach it to the project's Files so a "
        "new chat can read it and re-import.*", ""]
    files = bundle.get("files") or {}
    prof_text = files.get("profile.json")
    if prof_text:
        try:
            prof = json.loads(prof_text)
        except ValueError:
            prof = None
        if isinstance(prof, dict):
            lines.append("## Profile")
            if any(k in prof for k in ("identity", "finance", "retirement")):
                for sec in ("identity", "finance", "retirement"):
                    body = prof.get(sec) or {}
                    if body:
                        lines.append(f"### {sec.title()}")
                        lines.extend(_cfg_md_lines(body))
                        lines.append("")
                if "profile_complete" in prof:
                    lines.append(
                        f"*profile_complete: {prof['profile_complete']}*\n")
            else:
                lines.extend(_cfg_md_lines(prof))
                lines.append("")
    cache = bundle.get("cache") or {}
    for table, blob in (cache.get("config_tables") or {}).items():
        lines.append(f"## {table}")
        if blob.get("rows"):
            lines.extend(_cfg_md_table(blob["columns"], blob["rows"]))
        else:
            lines.append("*(none)*")
        lines.append("")
    for table, blob in (cache.get("protected_rows") or {}).items():
        lines.append(f"## {table} (user corrections)")
        lines.extend(_cfg_md_table(blob["columns"], blob["rows"]))
        lines.append("")
    lines.append("## Machine-readable config")
    lines.append("*Do not edit by hand — `config-import` parses this block.*\n")
    lines.append("```json")
    lines.append(json.dumps(bundle, indent=2))
    lines.append("```")
    return "\n".join(lines) + "\n"


def render_config_export(cache, data_dir, out_path):
    from skill_common import resolve_outputs_dir
    bundle = _config_bundle(cache, data_dir)
    if not out_path:
        stamp = _dt.date.today().isoformat()
        out = resolve_outputs_dir()
        out_path = os.path.join(out or data_dir,
                                f"monarch-money-config-{stamp}.md")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(_render_config_markdown(bundle))
    ct = bundle["cache"]["config_tables"]
    pr = bundle["cache"]["protected_rows"]
    has_profile = "profile.json" in (bundle.get("files") or {})
    print("## Config Export\n")
    print(f"Saved a readable config profile to **{out_path}**.\n")
    for table, blob in ct.items():
        print(f"- `{table}`: {len(blob['rows'])} row(s)")
    for table, blob in pr.items():
        print(f"- `{table}` (user corrections): {len(blob['rows'])} row(s)")
    if has_profile:
        print("- profile.json (rendered as sections + in the JSON block)")
    if not ct and not pr and not has_profile:
        print("> Nothing to export yet — no exclusions, tax-bucket "
              "corrections, or profile.")
    print("\n**Keep this file.** It's human-readable AND machine-restorable. On "
          "claude.ai/mobile a new chat starts with an empty container — attach "
          "it to the project's **Files** so every chat can read it, and run "
          "`config-import --in <file>` to restore. (Synced Monarch data is "
          "excluded; `sync-all` regenerates it.)")


def _restore_config_file(data_dir, fname, text):
    path = os.path.join(data_dir, fname)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"- file `{fname}`: restored")
    else:
        with open(path + ".imported", "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"- file `{fname}`: already present → wrote "
              f"`{fname}.imported` (reconcile manually)")


def _extract_config_bundle(text):
    """Pull the bundle from a config-export file: raw JSON, or the last fenced
    ```json block in the Markdown artifact."""
    if text.lstrip().startswith("{"):
        return json.loads(text)
    marker = "```json"
    i = text.rfind(marker)
    if i == -1:
        raise MonarchSkillError("no ```json config block found in the file.")
    start = i + len(marker)
    end = text.find("```", start)
    if end == -1:
        raise MonarchSkillError("unterminated ```json block in the file.")
    return json.loads(text[start:end])


def render_config_import(cache, data_dir, in_path):
    if not in_path or not os.path.exists(in_path):
        raise MonarchSkillError(f"--in file not found: {in_path}")
    try:
        with open(in_path, encoding="utf-8") as fh:
            bundle = _extract_config_bundle(fh.read())
    except (OSError, ValueError) as exc:
        raise MonarchSkillError(f"cannot read config export: {exc}")
    if bundle.get("kind") != CONFIG_BUNDLE_KIND:
        raise MonarchSkillError(f"not a {CONFIG_BUNDLE_KIND} export.")
    counts = cache.import_config(bundle.get("cache") or {})
    print("## Config Import\n")
    for table, n in (counts or {}).items():
        print(f"- `{table}`: {n} row(s) restored")
    if not counts:
        print("- (no config-table rows in export)")
    os.makedirs(data_dir, exist_ok=True)
    for fname, text in (bundle.get("files") or {}).items():
        _restore_config_file(data_dir, fname, text)
    print("\nConfig restored. Synced Monarch data is NOT in the bundle — run "
          "`sync-all` to repopulate it.")


def dispatch(args, cache, client):
    ctx = Ctx(args, cache, client)
    cmd = args.command

    if cmd == "accounts":
        sync_accounts(ctx)
        render_accounts(cache)
    elif cmd == "holdings":
        sync_accounts(ctx)  # holdings render needs account names
        sync_holdings(ctx)
        render_holdings(cache)
    elif cmd == "transactions":
        sync_transactions(ctx)
        render_transactions(cache, ctx.days, args.account, args.category,
                            ctx.mock, getattr(args, "include_excluded", False))
    elif cmd == "budgets":
        period = args.month or _today().strftime("%Y-%m")
        period, _ = sync_budgets(ctx, period)
        render_budgets(cache, period, getattr(args, "include_excluded", False))
    elif cmd == "cashflow":
        n_months = max(args.months, 1)
        sync_cashflow(ctx, n_months)
        render_cashflow(cache, n_months, month=args.month,
                        include_excluded=getattr(args, "include_excluded", False))
    elif cmd == "recurring":
        sync_recurring(ctx)
        render_recurring(cache)
    elif cmd == "net-worth":
        render_net_worth(cache)
    elif cmd == "sync-all":
        run_sync_all(ctx)
    elif cmd == "status":
        render_status(cache)
    elif cmd == "version":
        print(f"monarch-money skill version {SKILL_VERSION}")
    elif cmd in ("report", "dashboard", "reports", "profile"):
        # Moved out in v1.0.0 (the three-skill split). monarch-money is the
        # data layer only — it syncs Monarch into the cache.
        print(f"`{cmd}` moved out of monarch-money in v1.0.0.\n"
              "- Reports & dashboard (net worth, spending, allocation, TLH): "
              "the **personal-finance** skill.\n"
              "- Retirement projections (FIRE, drawdown, TPAW, Social "
              "Security, retire-age what-ifs): the **retirement** skill.\n"
              "Both read this skill's cache. monarch-money handles sync/auth "
              "only — see `python scripts/fetch.py --help`.")
    elif cmd == "auth-status":
        render_auth_status(args.session_path, args.cred_path, args.cookie_path)
    elif cmd == "clear-cache":
        counts = cache.clear(include_user_buckets=args.include_user_buckets)
        print("## Cache Cleared\n")
        for table in ("accounts", "holdings", "transactions", "budgets",
                      "cashflow", "recurring"):
            print(f"✓ {table}: {counts[table]} rows removed")
        print(f"✓ tax buckets: {counts['tax_buckets']} auto rows removed "
              f"({counts['user_buckets_kept']} user correction(s) kept)")
        if args.include_user_buckets:
            print("  (user corrections wiped — --include-user-buckets)")
        print("\nNext sync re-pulls everything and re-infers tax buckets "
              "with current classifier logic.")
    elif cmd == "config-export":
        render_config_export(
            cache, os.path.dirname(os.path.abspath(args.db)), args.out)
    elif cmd == "config-import":
        render_config_import(
            cache, os.path.dirname(os.path.abspath(args.db)), args.in_path)
    else:
        raise SystemExit(f"unknown command: {cmd}")
    return 0


def run_sync_all(ctx):
    cache = ctx.cache
    sync_accounts(ctx)
    n_acc = cache._conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    sync_holdings(ctx)
    n_hold = cache._conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0]
    n_hacct = cache._conn.execute(
        "SELECT COUNT(DISTINCT account_id) FROM holdings").fetchone()[0]
    sync_transactions(ctx)
    n_tx = cache._conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    tx_range = cache._conn.execute(
        "SELECT MIN(date), MAX(date) FROM transactions").fetchone()
    period = _today().strftime("%Y-%m")
    period, _ = sync_budgets(ctx, period)
    n_bud = cache._conn.execute(
        "SELECT COUNT(*) FROM budgets WHERE period=?", (period,)).fetchone()[0]
    sync_cashflow(ctx, 6)
    n_cf = cache._conn.execute("SELECT COUNT(*) FROM cashflow").fetchone()[0]
    sync_recurring(ctx)
    n_rec = cache._conn.execute("SELECT COUNT(*) FROM recurring").fetchone()[0]
    cache.set_last_full_sync(_today().isoformat())

    print("## Sync Complete\n")
    print(f"✓ Accounts: {n_acc} accounts updated")
    print(f"✓ Holdings: {n_hold} positions across {n_hacct} accounts")
    print(f"✓ Transactions: {n_tx} cached ({tx_range[0]} to {tx_range[1]})")
    print(f"✓ Budgets: {n_bud} categories for {period}")
    print(f"✓ Cashflow: {n_cf} months updated")
    print(f"✓ Recurring: {n_rec} active transactions")
    print(f"\nTotal API calls: {cache.status()['api_call_count']}")


# --------------------------------------------------------------------------- #
# Argument parsing & main
# --------------------------------------------------------------------------- #
def build_parser():
    # Global flags live on a shared parent so they may follow the subcommand
    # (e.g. `fetch.py accounts --mock --refresh`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--refresh", action="store_true", help="Bypass cache TTL.")
    common.add_argument("--mock", action="store_true", help="Load fixtures, no network.")
    common.add_argument("--db", default=DEFAULT_DB_PATH)
    common.add_argument("--fixtures-dir", default=DEFAULT_FIXTURES_DIR)
    common.add_argument("--session-path", default=DEFAULT_SESSION_PATH)
    common.add_argument("--cred-path", default=DEFAULT_CRED_PATH)
    common.add_argument("--cookie-path", default=DEFAULT_COOKIE_PATH)

    p = argparse.ArgumentParser(
        description="Monarch Money data fetch + cache CLI.", parents=[common])
    sub = p.add_subparsers(dest="command", required=True)

    # --include-excluded bypasses the non-recurring exclusion filter (raw view)
    # on every transaction-derived subcommand.
    def _add_include_excluded(parser):
        parser.add_argument(
            "--include-excluded", action="store_true",
            help="Bypass non-recurring exclusion filtering (show raw data).")

    sub.add_parser("accounts", parents=[common])
    sub.add_parser("holdings", parents=[common])
    tx = sub.add_parser("transactions", parents=[common])
    tx.add_argument("--days", type=int, default=30)
    tx.add_argument("--account")
    tx.add_argument("--category")
    _add_include_excluded(tx)
    bg = sub.add_parser("budgets", parents=[common])
    bg.add_argument("--month")
    _add_include_excluded(bg)
    cf = sub.add_parser("cashflow", parents=[common])
    cf.add_argument("--month")
    cf.add_argument("--months", type=int, default=1)
    _add_include_excluded(cf)
    sub.add_parser("recurring", parents=[common])
    sub.add_parser("net-worth", parents=[common])
    sub.add_parser("sync-all", parents=[common])
    sub.add_parser("status", parents=[common])
    sub.add_parser("auth-status", parents=[common])
    cc = sub.add_parser("clear-cache", parents=[common])
    cc.add_argument("--include-user-buckets", action="store_true",
                    help="Also wipe user-corrected tax buckets "
                         "(auto_classified=0). Default keeps them.")
    ce = sub.add_parser("config-export", parents=[common],
                        help="Export non-reproducible config (exclusions + "
                             "tax-bucket corrections + profile) to a portable "
                             "JSON artifact (durability backstop).")
    ce.add_argument("--out", help="Output path (default: outputs dir).")
    ci = sub.add_parser("config-import", parents=[common],
                        help="Restore a config-export bundle into this session.")
    ci.add_argument("--in", dest="in_path", required=True,
                    help="Path to a config-export JSON file.")
    sub.add_parser("version", parents=[common])
    # Moved out in v1.0.0 — kept as stubs that redirect to the consumer skills.
    rp = sub.add_parser("report", parents=[common])
    rp.add_argument("key", nargs="?", help="(moved to personal-finance/retirement)")
    sub.add_parser("dashboard", parents=[common])
    sub.add_parser("reports", parents=[common])
    sub.add_parser("profile", parents=[common])
    return p


def main(argv=None):
    # Markdown output uses ✓ — ↑ ⚠; force UTF-8 so a cp1252 Windows console
    # (the container is Linux/UTF-8 already) doesn't raise UnicodeEncodeError.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv)
    cache = None
    client = None
    try:
        if args.command in NO_AUTH or args.mock:
            cache = MonarchCache(args.db)
        else:
            client = MonarchClient.ensure_authenticated(
                args.session_path, args.cred_path, args.db, args.cookie_path)
            cache = client.cache
        return dispatch(args, cache, client)
    except MonarchSkillError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}))
        return 1
    except Exception as exc:  # noqa: BLE001 - classify unknown failures
        classified = classify_error(exc)
        print(json.dumps({"error": type(classified).__name__,
                          "message": str(classified)}))
        return 1
    finally:
        if cache is not None:
            cache.close()


if __name__ == "__main__":
    sys.exit(main())
