"""
reports.py — Boglehead / FIRE financial reports computed from the cached
Monarch data. Pure SQL/Python over data/monarch_cache.db; no API calls.

Ten reports plus a comprehensive dashboard that summarises each and links to
the full version. Vocabulary and methodology are documented in
references/reports.md. Defaults follow Boglehead/FIRE convention (4% SWR /
25x, three-fund lens, tax-efficient placement, essential-vs-discretionary);
the user can override via a profile.json in the persistent data dir.

Each report function returns a dict:
    {"title", "kpi": (label, value)|None, "summary": [lines], "full": md}
"""
import json
import os
import datetime as _dt

# --------------------------------------------------------------------------- #
# Profile (FIRE/Boglehead assumptions; override via <data_dir>/profile.json)
# --------------------------------------------------------------------------- #
DEFAULT_PROFILE = {
    "swr_pct": 4.0,                 # safe withdrawal rate (4% rule → 25x)
    "birth_year": None,             # for Coast FIRE; None → prompt for it
    "retirement_age": 65,           # Coast FIRE target age
    "expected_real_return_pct": 5.0,  # real growth assumption for Coast FIRE
    # Three-fund target (% of total portfolio). Default 80/20 with intl = 40%
    # of equities — a common Boglehead split; override per your IPS.
    "target_us_stock_pct": 48.0,
    "target_intl_stock_pct": 32.0,
    "target_bond_pct": 20.0,
    "lean_annual_expenses": 40000,  # Lean FIRE ceiling (per Trinity-era norms)
    "fat_annual_expenses": 100000,  # Fat FIRE floor
    "essential_categories": [
        "groceries", "utilities", "transportation", "housing", "home", "rent",
        "mortgage", "health", "insurance", "childcare", "debt", "loan",
        "bills", "medical", "auto",
    ],
}


def load_profile(data_dir):
    prof = dict(DEFAULT_PROFILE)
    path = os.path.join(data_dir, "profile.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                prof.update(json.load(fh))
        except (OSError, json.JSONDecodeError):
            pass
    return prof


def _money(x):
    if x is None:
        return "—"
    if x < 0:
        return f"-${abs(x):,.0f}"
    return f"${x:,.0f}"


def _pct(x):
    return "—" if x is None else f"{x:.1f}%"


# --------------------------------------------------------------------------- #
# Asset-class classification (three-fund lens)
# --------------------------------------------------------------------------- #
_US_STOCK = {"VTI", "VTSAX", "FZROX", "FSKAX", "FXAIX", "VOO", "SPY", "ITOT",
             "SWTSX", "SCHB", "SCHD", "VIG", "VUG", "VTV"}
_INTL_STOCK = {"VXUS", "VTIAX", "FSPSX", "FTIHX", "IXUS", "VEU", "VWO", "IEFA",
               "FZILX", "SCHF", "VEA"}
_BOND = {"BND", "BNDX", "FXNAX", "AGG", "VBTLX", "FBND", "VGIT", "VTEB",
         "SCHZ", "BIV", "VCIT"}
_REIT = {"VNQ", "VGSLX", "SCHH", "IYR", "VNQI", "FREL"}

# Approximate expense ratios for common Boglehead funds (for the fee note).
_EXPENSE_RATIOS = {
    "VTI": 0.03, "VTSAX": 0.04, "FZROX": 0.0, "FSKAX": 0.015, "FXAIX": 0.015,
    "VOO": 0.03, "VXUS": 0.08, "VTIAX": 0.11, "FSPSX": 0.035, "FZILX": 0.0,
    "BND": 0.03, "BNDX": 0.07, "FXNAX": 0.025, "VBTLX": 0.05, "VNQ": 0.13,
    "SCHD": 0.06, "VIG": 0.06,
}


def classify_asset_class(ticker, name):
    t = (ticker or "").upper().strip()
    n = (name or "").lower()
    if t in _US_STOCK:
        return "us_stock"
    if t in _INTL_STOCK:
        return "intl_stock"
    if t in _BOND:
        return "bond"
    if t in _REIT:
        return "reit"
    # Name heuristics for unmapped tickers.
    if any(k in n for k in ("bond", "treasury", "fixed income", "aggregate")):
        return "bond"
    if any(k in n for k in ("real estate", "reit")):
        return "reit"
    if any(k in n for k in ("international", "ex-us", "ex us", "emerging",
                            "developed market", "global ex")):
        return "intl_stock"
    if any(k in n for k in ("total stock", "s&p 500", "500 index", "us equity",
                            "large cap", "small cap", "growth", "value")):
        return "us_stock"
    if t and t not in ("",):
        return "us_stock"  # an unrecognised individual ticker is US equity-ish
    return "other"


# --------------------------------------------------------------------------- #
# Shared metric helpers
# --------------------------------------------------------------------------- #
def _accounts(cache):
    return cache._conn.execute(
        "SELECT id, name, type, subtype, balance FROM accounts "
        "WHERE is_hidden=0 AND is_closed=0"
    ).fetchall()


_CASH_TYPES = ("checking", "savings", "depository", "cash")
_INVEST_TYPES = ("investment", "brokerage")
_EXCLUDE_FROM_INVESTABLE = ("real_estate", "real estate", "loan", "credit",
                            "vehicle", "other_property")


def cash_total(cache):
    return sum(b or 0 for _, _, t, _, b in _accounts(cache)
              if (t or "").lower() in _CASH_TYPES and (b or 0) > 0)


def investable_assets(cache):
    """Liquid investable net worth for FIRE math: cash + investment accounts.
    Excludes real estate and liabilities (FIRE number compares to the
    portfolio that funds the 4% withdrawal, not home equity)."""
    total = 0.0
    for _, _, t, _, b in _accounts(cache):
        tl = (t or "").lower()
        if (b or 0) > 0 and (tl in _CASH_TYPES or tl in _INVEST_TYPES):
            total += b
    return total


def net_worth(cache):
    return sum(b or 0 for _, _, _, _, b in _accounts(cache))


def monthly_expense(cache):
    """Average monthly expense from cached cashflow (preferred) or the
    transactions table. Returns (monthly, source, n_months)."""
    rows = cache._conn.execute(
        "SELECT expense FROM cashflow ORDER BY period DESC LIMIT 12"
    ).fetchall()
    rows = [r[0] for r in rows if r[0]]
    if rows:
        return sum(rows) / len(rows), "cashflow", len(rows)
    # Fall back to transactions: outflow per distinct month.
    tx = cache._conn.execute(
        "SELECT strftime('%Y-%m', date) m, SUM(-amount) FROM transactions "
        "WHERE amount < 0 AND category NOT IN ('Transfer') GROUP BY m"
    ).fetchall()
    if tx:
        vals = [v for _, v in tx if v]
        return (sum(vals) / len(vals) if vals else 0.0), "transactions", len(vals)
    return 0.0, "none", 0


def annual_expense(cache, profile):
    if profile.get("annual_expenses_override"):
        return float(profile["annual_expenses_override"]), "profile override"
    m, src, _ = monthly_expense(cache)
    return m * 12, src


def _holdings(cache):
    return cache._conn.execute(
        "SELECT account_id, ticker, name, value, cost_basis FROM holdings"
    ).fetchall()


def asset_class_breakdown(cache):
    out = {}
    total = 0.0
    for _, ticker, name, value, _ in _holdings(cache):
        cls = classify_asset_class(ticker, name)
        out[cls] = out.get(cls, 0.0) + (value or 0.0)
        total += value or 0.0
    return out, total


def _current_age(profile):
    by = profile.get("birth_year")
    if not by:
        return None
    return _dt.date.today().year - int(by)


# --------------------------------------------------------------------------- #
# The ten reports
# --------------------------------------------------------------------------- #
def report_net_worth(cache, profile):
    rows = _accounts(cache)
    by_type = {}
    for _, _, t, _, b in rows:
        by_type[t] = by_type.get(t, 0.0) + (b or 0.0)
    assets = sum(v for v in by_type.values() if v >= 0)
    liab = sum(-v for v in by_type.values() if v < 0)
    nw = assets - liab
    lines = [f"**{_money(nw)}** net worth — {_money(assets)} assets, "
             f"{_money(liab)} liabilities."]
    body = [f"## Net Worth Statement\n", f"**Net Worth: {_money(nw)}**\n",
            "| Category | Type | Balance |", "|---|---|---|"]
    for t, v in sorted(by_type.items(), key=lambda kv: -kv[1]):
        side = "Asset" if v >= 0 else "Liability"
        body.append(f"| {side} | {(t or 'other').replace('_',' ').title()} "
                    f"| {_money(v)} |")
    body.append(f"\nAssets {_money(assets)} − Liabilities {_money(liab)} "
                f"= **{_money(nw)}**.")
    body.append("\n*Investable (FIRE) assets — cash + investments, excluding "
                f"real estate: {_money(investable_assets(cache))}.*")
    return {"title": "Net Worth Statement", "kpi": ("Net worth", _money(nw)),
            "summary": lines, "full": "\n".join(body)}


def report_allocation(cache, profile):
    bd, total = asset_class_breakdown(cache)
    if total <= 0:
        return _empty("Asset Allocation", "No holdings cached — run `holdings`.")
    pct = {k: 100 * v / total for k, v in bd.items()}
    tgt = {"us_stock": profile["target_us_stock_pct"],
           "intl_stock": profile["target_intl_stock_pct"],
           "bond": profile["target_bond_pct"]}
    label = {"us_stock": "US stock", "intl_stock": "Intl stock",
             "bond": "Bonds", "reit": "REIT", "other": "Other"}
    stock = pct.get("us_stock", 0) + pct.get("intl_stock", 0) + pct.get("reit", 0)
    lines = [f"{stock:.0f}/{pct.get('bond',0):.0f} stock/bond; "
             f"US {pct.get('us_stock',0):.0f}% · Intl {pct.get('intl_stock',0):.0f}% "
             f"· Bond {pct.get('bond',0):.0f}%."]
    body = ["## Asset Allocation (three-fund lens)\n",
            f"Portfolio: **{_money(total)}**\n",
            "| Asset class | Value | Actual | Target | 5/25 band? |",
            "|---|---|---|---|---|"]
    drift_flags = []
    for cls in ("us_stock", "intl_stock", "bond", "reit", "other"):
        if cls not in bd:
            continue
        a = pct.get(cls, 0)
        t = tgt.get(cls)
        band = ""
        if t is not None:
            absdrift = abs(a - t)
            reldrift = absdrift / t * 100 if t else 0
            if absdrift >= 5 or reldrift >= 25:
                band = "⚠ rebalance"
                drift_flags.append(f"{label[cls]} {a:.0f}% vs {t:.0f}% target")
        body.append(f"| {label[cls]} | {_money(bd[cls])} | {a:.1f}% | "
                    f"{_pct(t) if t is not None else '—'} | {band} |")
    if drift_flags:
        body.append(f"\n**Rebalancing (5/25 rule):** off-target — "
                    + "; ".join(drift_flags) + ".")
    else:
        body.append("\n**Rebalancing (5/25 rule):** within bands — no action.")
    # Fee note (Boglehead staple).
    fee_w = 0.0
    known = 0.0
    for _, ticker, _, value, _ in _holdings(cache):
        er = _EXPENSE_RATIOS.get((ticker or "").upper())
        if er is not None and value:
            fee_w += er / 100 * value
            known += value
        if er is None and value and total:
            pass
    if known:
        body.append(f"\n**Fee drag:** ~{_money(fee_w)}/yr on {_money(known)} of "
                    f"mapped funds ({fee_w/known*100:.3f}% blended ER). "
                    "Lower is better — the Boglehead edge.")
    return {"title": "Asset Allocation", "kpi": ("Stock/Bond",
            f"{stock:.0f}/{pct.get('bond',0):.0f}"), "summary": lines,
            "full": "\n".join(body)}


def report_asset_location(cache, profile):
    rows = cache._conn.execute(
        "SELECT a.id, a.name, b.tax_bucket FROM accounts a "
        "JOIN account_tax_buckets b ON a.id=b.account_id "
        "WHERE a.type IN ('investment','brokerage') AND a.is_hidden=0"
    ).fetchall()
    bucket_of = {r[0]: (r[2] or "other") for r in rows}
    name_of = {r[0]: r[1] for r in rows}
    # value by (bucket, asset_class)
    grid = {}
    bond_in_taxable = 0.0
    for acct_id, ticker, name, value, _ in _holdings(cache):
        bucket = bucket_of.get(acct_id, "other")
        cls = classify_asset_class(ticker, name)
        grid.setdefault(bucket, {}).setdefault(cls, 0.0)
        grid[bucket][cls] += value or 0.0
        if cls == "bond" and bucket in ("taxable", "other"):
            bond_in_taxable += value or 0.0
    lines = [("Bonds in taxable: " + _money(bond_in_taxable) +
              " — consider moving to tax-deferred.") if bond_in_taxable > 0
             else "Tax-efficient: no bonds sitting in taxable."]
    body = ["## Asset Location (tax-efficient placement)\n",
            "Bogleheads: **bonds & REITs → tax-deferred** (taxed at full "
            "rate), **total-market stock → taxable** (qualified dividends, "
            "step-up), **international → tax-deferred/taxable** for the foreign "
            "tax credit.\n",
            "| Tax bucket | Bonds | US stock | Intl | REIT | Other |",
            "|---|---|---|---|---|---|"]
    for bucket in ("taxable", "traditional", "roth", "hsa", "other"):
        if bucket not in grid:
            continue
        g = grid[bucket]
        body.append(f"| {bucket.title()} | {_money(g.get('bond',0))} | "
                    f"{_money(g.get('us_stock',0))} | {_money(g.get('intl_stock',0))} "
                    f"| {_money(g.get('reit',0))} | {_money(g.get('other',0))} |")
    if bond_in_taxable > 0:
        body.append(f"\n⚠ **{_money(bond_in_taxable)} of bonds in taxable.** "
                    "Bond interest is taxed at your full marginal rate; holding "
                    "them in a Traditional/401(k) can lift after-tax returns "
                    "~0.75%/yr. Hold tax-efficient total-market stock here "
                    "instead.")
    else:
        body.append("\n✓ No bonds in taxable — placement looks tax-efficient.")
    return {"title": "Asset Location", "kpi": ("Bonds in taxable",
            _money(bond_in_taxable)), "summary": lines, "full": "\n".join(body)}


def report_fire_progress(cache, profile):
    ann, src = annual_expense(cache, profile)
    swr = profile["swr_pct"]
    fire_num = ann * (100 / swr) if swr else None
    inv = investable_assets(cache)
    fi_pct = (100 * inv / fire_num) if fire_num else None
    lean = profile["lean_annual_expenses"] * 25
    fat = profile["fat_annual_expenses"] * 25
    lines = [f"**{_pct(fi_pct)} to FI** — {_money(inv)} of {_money(fire_num)} "
             f"(25× {_money(ann)} expenses at {swr:.1f}% SWR)."]
    body = ["## FIRE Progress\n",
            f"- Annual expenses (from {src}): **{_money(ann)}**",
            f"- Safe withdrawal rate: **{swr:.1f}%** → multiplier "
            f"**{100/swr:.1f}×**",
            f"- **FIRE number: {_money(fire_num)}**",
            f"- Investable assets: **{_money(inv)}**",
            f"- **Progress to FI: {_pct(fi_pct)}**\n",
            "| Tier | Annual spend | Target (25×) | Reached? |",
            "|---|---|---|---|",
            f"| Lean FIRE | {_money(profile['lean_annual_expenses'])} | "
            f"{_money(lean)} | {'✓' if inv>=lean else '—'} |",
            f"| Your FIRE | {_money(ann)} | {_money(fire_num)} | "
            f"{'✓' if fire_num and inv>=fire_num else '—'} |",
            f"| Fat FIRE | {_money(profile['fat_annual_expenses'])} | "
            f"{_money(fat)} | {'✓' if inv>=fat else '—'} |"]
    if fi_pct is not None and fi_pct < 100:
        gap = fire_num - inv
        body.append(f"\nGap to FI: **{_money(gap)}**.")
    return {"title": "FIRE Progress", "kpi": ("Progress to FI", _pct(fi_pct)),
            "summary": lines, "full": "\n".join(body)}


def report_withdrawal(cache, profile):
    inv = investable_assets(cache)
    ann, _ = annual_expense(cache, profile)
    rows = []
    for rate in (4.0, 3.5, 3.0):
        spend = inv * rate / 100
        rows.append((rate, spend, spend >= ann))
    lines = [f"4% safe spend: **{_money(inv*0.04)}/yr** "
             f"({_money(inv*0.04/12)}/mo) on {_money(inv)}."]
    body = ["## Safe Withdrawal & Coast FIRE\n",
            f"Investable assets: **{_money(inv)}**\n",
            "| Withdrawal rate | Annual income | Monthly | Covers expenses? |",
            "|---|---|---|---|"]
    for rate, spend, ok in rows:
        body.append(f"| {rate:.1f}% | {_money(spend)} | {_money(spend/12)} | "
                    f"{'✓' if ok else '—'} |")
    body.append(f"\nCurrent annual expenses: {_money(ann)}.")
    # Coast FIRE
    age = _current_age(profile)
    swr = profile["swr_pct"]
    fire_num = ann * (100 / swr) if swr else None
    r = profile["expected_real_return_pct"] / 100
    ret_age = profile["retirement_age"]
    body.append("\n### Coast FIRE\n")
    if age and fire_num and ret_age > age:
        years = ret_age - age
        coast_num = fire_num / ((1 + r) ** years)
        reached = inv >= coast_num
        body.append(
            f"At age {age}, targeting retirement at {ret_age} "
            f"({years} yrs of {r*100:.0f}% real growth), your **Coast FIRE "
            f"number is {_money(coast_num)}**. "
            + (f"✓ **Reached** — invested {_money(inv)} can coast to your FIRE "
               f"number with no further contributions."
               if reached else
               f"Not yet — {_money(coast_num - inv)} to go before you could "
               f"stop contributing and still hit FI by {ret_age}."))
        lines.append("Coast FIRE " + ("reached." if reached else
                     f"{_money(coast_num-inv)} away."))
    else:
        body.append("Set `birth_year` (and optionally `retirement_age`) in "
                    "your profile.json to compute Coast FIRE — the portfolio "
                    "that, with no new contributions, compounds to your FIRE "
                    "number by your target age.")
    return {"title": "Safe Withdrawal & Coast FIRE",
            "kpi": ("4% safe income", _money(inv*0.04)), "summary": lines,
            "full": "\n".join(body)}


def report_savings_rate(cache, profile):
    rows = cache._conn.execute(
        "SELECT period, income, expense, savings, savings_rate FROM cashflow "
        "ORDER BY period DESC LIMIT 12"
    ).fetchall()
    if not rows:
        return _empty("Savings Rate", "No cashflow cached — run `cashflow`.")
    inc = sum(r[1] or 0 for r in rows)
    exp = sum(r[2] or 0 for r in rows)
    rate = 100 * (inc - exp) / inc if inc else 0
    lines = [f"**{rate:.1f}%** trailing {len(rows)}-mo savings rate "
             f"({_money(inc-exp)} saved on {_money(inc)} income)."]
    body = ["## Savings Rate\n",
            f"Trailing {len(rows)} months: income {_money(inc)}, expenses "
            f"{_money(exp)}, **saved {_money(inc-exp)} → {rate:.1f}%**.\n",
            "*Savings rate is the single biggest lever on time-to-FI — more "
            "than income or returns.*\n",
            "| Month | Income | Expense | Saved | Rate |",
            "|---|---|---|---|---|"]
    for p, i, e, s, sr in reversed(rows):
        body.append(f"| {p} | {_money(i)} | {_money(e)} | {_money(s)} | "
                    f"{_pct(sr)} |")
    return {"title": "Savings Rate", "kpi": ("Savings rate", f"{rate:.0f}%"),
            "summary": lines, "full": "\n".join(body)}


def report_expenses(cache, profile):
    ess_kw = [k.lower() for k in profile["essential_categories"]]
    rows = cache._conn.execute(
        "SELECT category, category_group, SUM(-amount) spend, COUNT(*) n "
        "FROM transactions WHERE amount < 0 AND category NOT IN ('Transfer') "
        "GROUP BY category ORDER BY spend DESC"
    ).fetchall()
    if not rows:
        return _empty("Expense Analysis", "No transactions cached.")
    ess = disc = 0.0
    for cat, grp, spend, _ in rows:
        hay = f"{cat} {grp}".lower()
        if any(k in hay for k in ess_kw):
            ess += spend or 0
        else:
            disc += spend or 0
    total = ess + disc
    lines = [f"{_money(total)} spend — **{100*ess/total:.0f}% essential / "
             f"{100*disc/total:.0f}% discretionary**." if total else
             "No spend recorded."]
    body = ["## Expense Analysis (essential vs discretionary)\n",
            f"Total tracked spend: **{_money(total)}** — essential "
            f"{_money(ess)} ({100*ess/total:.0f}%), discretionary "
            f"{_money(disc)} ({100*disc/total:.0f}%).\n" if total else "",
            "Discretionary is your fastest lever on the FIRE number (every "
            "$1/yr cut lowers the target by ~$25).\n",
            "| Category | Spend | Txns | Bucket |", "|---|---|---|---|"]
    for cat, grp, spend, n in rows:
        hay = f"{cat} {grp}".lower()
        bucket = "essential" if any(k in hay for k in ess_kw) else "discretionary"
        body.append(f"| {cat} | {_money(spend)} | {n} | {bucket} |")
    return {"title": "Expense Analysis",
            "kpi": ("Essential share", f"{100*ess/total:.0f}%" if total else "—"),
            "summary": lines, "full": "\n".join(body)}


def report_emergency_fund(cache, profile):
    cash = cash_total(cache)
    m, src, _ = monthly_expense(cache)
    months = cash / m if m else None
    status = ("✓ solid" if months and months >= 6 else
              "⚠ thin (<3 mo)" if months and months < 3 else "building")
    lines = [f"**{months:.1f} months** of expenses in cash ({_money(cash)}) — "
             f"{status}." if months else f"{_money(cash)} cash."]
    body = ["## Emergency Fund Coverage\n",
            f"- Cash & equivalents: **{_money(cash)}**",
            f"- Avg monthly expenses (from {src}): **{_money(m)}**",
            f"- **Coverage: {months:.1f} months**" if months else "- Coverage: —",
            f"\nRule of thumb: 3–6 months (employed), 6–12 (variable income / "
            f"near-FIRE). You're at **{months:.1f}** — {status}." if months
            else ""]
    return {"title": "Emergency Fund",
            "kpi": ("EF coverage", f"{months:.1f} mo" if months else "—"),
            "summary": lines, "full": "\n".join(body)}


def report_tlh(cache, profile):
    rows = cache._conn.execute(
        "SELECT account_id, ticker, name, value, cost_basis FROM holdings "
        "WHERE cost_basis IS NOT NULL AND ticker IS NOT NULL "
        "AND value < cost_basis ORDER BY (value - cost_basis) ASC"
    ).fetchall()
    total_loss = sum((v - cb) for _, _, _, v, cb in rows)
    lines = [f"**{_money(-total_loss)}** harvestable loss across {len(rows)} "
             f"position(s)." if rows else "No positions currently at a loss."]
    body = ["## Tax-Loss Harvesting\n"]
    if rows:
        body += [f"**{len(rows)} position(s) underwater — "
                 f"{_money(-total_loss)} of harvestable losses.**\n",
                 "| Account | Ticker | Value | Cost basis | Unrealized |",
                 "|---|---|---|---|---|"]
        for acct, tk, nm, v, cb in rows:
            body.append(f"| {acct} | {tk} | {_money(v)} | {_money(cb)} | "
                        f"{_money(v-cb)} |")
        body.append("\n⚠ **Lot-level caveat:** Monarch reports a blended "
                    "position basis, not tax lots. Confirm specific lots and "
                    "**wash-sale** windows (±30 days, all accounts incl. IRAs) "
                    "from your brokerage before harvesting.")
    else:
        body.append("No holdings are below cost basis right now. (Positions "
                    "with unknown basis or no ticker are excluded.)")
    return {"title": "Tax-Loss Harvesting",
            "kpi": ("Harvestable loss", _money(-total_loss) if rows else "$0"),
            "summary": lines, "full": "\n".join(body)}


def report_concentration(cache, profile):
    rows = cache._conn.execute(
        "SELECT ticker, name, SUM(value) v FROM holdings "
        "WHERE ticker IS NOT NULL GROUP BY ticker ORDER BY v DESC"
    ).fetchall()
    total = sum(r[2] or 0 for r in rows)
    if total <= 0:
        return _empty("Portfolio Concentration", "No holdings cached.")
    top = rows[0]
    top_pct = 100 * (top[2] or 0) / total
    # Single-name (non-fund) concentration — individual stocks vs broad funds.
    single = [(tk, nm, v) for tk, nm, v in rows
              if classify_asset_class(tk, nm) == "us_stock" and v
              and (tk or "").upper() not in _US_STOCK]
    single_pct = 100 * sum(v for _, _, v in single) / total
    lines = [f"Top holding **{top[0]} {top_pct:.0f}%**; single-stock "
             f"{single_pct:.0f}% of portfolio."]
    body = ["## Portfolio Concentration & Single-Stock Risk\n",
            f"Portfolio {_money(total)}. Top position **{top[0]} "
            f"({top_pct:.1f}%)**. Individual-stock exposure (excl. broad "
            f"funds): **{single_pct:.1f}%**.\n",
            "Bogleheads keep single names small (often <5%) — broad index "
            "funds carry no idiosyncratic risk.\n",
            "| Ticker | Value | % portfolio | Flag |", "|---|---|---|---|"]
    for tk, nm, v in rows[:15]:
        p = 100 * (v or 0) / total
        flag = "⚠ >5%" if p > 5 and (tk or "").upper() not in _US_STOCK else ""
        body.append(f"| {tk} | {_money(v)} | {p:.1f}% | {flag} |")
    return {"title": "Portfolio Concentration",
            "kpi": ("Top position", f"{top[0]} {top_pct:.0f}%"),
            "summary": lines, "full": "\n".join(body)}


def _empty(title, msg):
    return {"title": title, "kpi": None, "summary": [msg],
            "full": f"## {title}\n\n{msg}"}


# Ordered registry — the dashboard walks this.
REPORTS = [
    ("net-worth", report_net_worth),
    ("allocation", report_allocation),
    ("asset-location", report_asset_location),
    ("fire-progress", report_fire_progress),
    ("withdrawal", report_withdrawal),
    ("savings-rate", report_savings_rate),
    ("expenses", report_expenses),
    ("emergency-fund", report_emergency_fund),
    ("tlh", report_tlh),
    ("concentration", report_concentration),
]
REPORT_KEYS = [k for k, _ in REPORTS]
_REPORT_FNS = dict(REPORTS)


def run_report(cache, profile, key):
    fn = _REPORT_FNS.get(key)
    if not fn:
        raise KeyError(key)
    return fn(cache, profile)


def render_full(cache, profile, key):
    return run_report(cache, profile, key)["full"]


def render_all(cache, profile):
    parts = []
    for key in REPORT_KEYS:
        parts.append(f"<!-- report:{key} -->")
        parts.append(run_report(cache, profile, key)["full"])
        parts.append("\n---\n")
    return "\n".join(parts)


def render_dashboard(cache, profile):
    """Comprehensive dashboard: KPI header + each report in summary form with
    a link (command) to its full report. Claude turns this into an HTML
    artifact whose summary cards link/accordion to the full bodies (fetched
    in one shot via `fetch.py report all`)."""
    out = ["# 💰 Financial Dashboard (Boglehead / FIRE)\n"]
    # KPI header row
    kpis = []
    results = {}
    for key in REPORT_KEYS:
        r = run_report(cache, profile, key)
        results[key] = r
        if r.get("kpi"):
            kpis.append(r["kpi"])
    if kpis:
        out.append("## Key metrics\n")
        out.append("| " + " | ".join(k for k, _ in kpis) + " |")
        out.append("|" + "|".join(["---"] * len(kpis)) + "|")
        out.append("| " + " | ".join(str(v) for _, v in kpis) + " |\n")
    out.append("## Reports\n")
    for i, key in enumerate(REPORT_KEYS, 1):
        r = results[key]
        out.append(f"### {i}. {r['title']}")
        for line in r["summary"]:
            out.append(f"- {line}")
        out.append(f"- 📄 *Full report:* `python scripts/fetch.py report {key}`\n")
    out.append("---")
    out.append("*Render this as a single HTML dashboard artifact: one summary "
               "card per report, each linking (accordion/anchor) to its full "
               "body. Get every full body in one call with "
               "`python scripts/fetch.py report all`. Assumptions (SWR, target "
               "allocation, age) come from profile.json in the data dir — see "
               "references/reports.md.*")
    return "\n".join(out)
