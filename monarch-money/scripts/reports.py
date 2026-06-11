"""
reports.py — Boglehead / FIRE reports computed from cached Monarch data.
Pure SQL/Python over the cache; no API calls. Output is terse and expert-level.

Eleven reports + a dashboard. Outflows are classified (transfer / one_time /
recurring) so investment buys, contributions, and capital outlays do NOT count
as consumption. The FIRE number is built on a projected RETIREMENT budget,
grossed up for the taxable share of withdrawals, against a horizon-derived SWR.
A life-value layer reflects a die-with-zero objective. Methodology +
vocabulary: references/reports.md.

Each report function returns {"title", "kpi": (label,value)|None,
"summary": [lines], "full": md}.
"""
import json
import os
import datetime as _dt

# --------------------------------------------------------------------------- #
# Profile — all optional; defaults must produce CORRECT behavior unattended
# (transfers excluded, SWR horizon-derived). Interview fills the rest.
# --------------------------------------------------------------------------- #
DEFAULT_PROFILE = {
    "profile_complete": False,
    # --- horizon / SWR ---
    "swr_pct": None,                  # None → derive from horizon; set = honored
    "birth_year": None,
    "retirement_age": 65,
    "life_expectancy_age": 95,
    "expected_real_return_pct": 5.0,
    "withdrawal_strategy": "static",  # "static" | "guardrails"
    "guardrail_initial_swr_pct": 4.5,
    "guardrail_band_pct": 20,
    # --- allocation targets (three-fund) ---
    "target_us_stock_pct": 48.0,
    "target_intl_stock_pct": 32.0,
    "target_bond_pct": 20.0,
    # --- FIRE tiers / override ---
    "lean_annual_expenses": 40000,
    "fat_annual_expenses": 100000,
    "annual_expenses_override": None,
    # --- expense classification (Part 1) ---
    "transfer_categories": [
        "buy", "sell", "investment", "contribution", "401k", "403b", "ira",
        "roth", "hsa contributions", "brokerage", "transfer",
        "credit card payment", "principal", "deposit", "withdrawal",
    ],
    "one_time_categories": [
        "sailboat acquisition", "boat", "remodel", "renovation",
        "home improvement",
    ],
    "one_time_transaction_keywords": [],
    "lumpy_expenses": [
        {"name": "Vehicle replacement", "amount": 60000, "frequency_years": 8},
        {"name": "Roof", "amount": 25000, "frequency_years": 25},
        {"name": "HVAC", "amount": 12000, "frequency_years": 18},
        {"name": "Appliances", "amount": 8000, "frequency_years": 12},
    ],
    "essential_categories": [
        "groceries", "utilities", "transportation", "housing", "home", "rent",
        "mortgage", "health", "insurance", "childcare", "debt", "loan",
        "bills", "medical", "auto",
    ],
    # --- retirement budget (Part 2) ---
    "retirement_expense_reductions": [
        "business travel", "commute", "parking & tolls", "tuition", "childcare",
    ],
    "retirement_expense_deltas": {
        "healthcare_bridge_annual": 24000,   # ACA premiums+OOP, applies pre-65
        "extra_travel_annual": 15000,
        "mortgage_payoff_age": None,         # null=carry; set → P&I drops at age
    },
    "retirement_trad_tax_rate": 0.18,        # effective ordinary rate, retirement
    "retirement_ltcg_rate": 0.08,            # effective LTCG rate (0% harvesting)
    "relocate_no_income_tax_state": False,
    # --- emergency fund (Part 4) ---
    "income_stability": "stable",            # "stable" | "variable"
    "emergency_fund_months_target": 3,
    "count_taxable_as_liquid_for_ef": True,
    # --- interview capture (Part 5) ---
    "sector_views": {"1y": {}, "5y": {}, "10y": {}},
    "anticipated_changes": [],               # [{horizon_years,name,amount,kind}]
    # --- life-value (Part 6) ---
    "legacy_intent": "spend_down",           # "spend_down" | "leave_estate"
    "life_value_categories": [
        "travel & vacation", "travel", "vacation", "sailing", "charity",
        "charitable", "entertainment & recreation", "gifts", "donation",
    ],
    "life_value_discount_rate_pct": 5.0,
    # Display-label mapping (math is identical; only names change).
    # "A" (saved-memory): Net Means = life-value incl. directed DAF;
    #                     Net Worth = accounting (excludes DAF).
    # "B": swap the two labels.
    "net_means_mapping": "A",
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


def _norm(s):
    return (s or "").lower().strip()


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
    if t:
        return "us_stock"
    return "other"


# --------------------------------------------------------------------------- #
# Part 1 — outflow classification (pure, profile-driven, unit-testable)
# --------------------------------------------------------------------------- #
def _kw_match(kw, cat, mer):
    """Match keyword on category (substring), and on merchant only when the
    keyword is specific (>=6 chars) — avoids 'buy'→'Best Buy', 'ira'→'Tira's'."""
    k = kw.lower()
    if k in cat:
        return True
    if len(k) >= 6 and k in mer:
        return True
    return False


def classify_outflow(category, merchant, amount, profile):
    """Return 'transfer' | 'one_time' | 'recurring' for a single outflow.
    Order: transfer → one_time → recurring. 'lumpy' is NOT a per-transaction
    class (lumpy items are amortized sinking-fund allowances from the profile;
    a real lumpy spike in the ledger should be tagged one_time)."""
    cat = _norm(category)
    mer = _norm(merchant)
    for kw in profile.get("transfer_categories", []):
        if _kw_match(kw, cat, mer):
            return "transfer"
    for kw in profile.get("one_time_categories", []):
        if _kw_match(kw, cat, mer):
            return "one_time"
    for kw in profile.get("one_time_transaction_keywords", []):
        if _kw_match(kw, cat, mer):
            return "one_time"
    return "recurring"


def lumpy_allowances(profile):
    out = []
    for it in profile.get("lumpy_expenses", []):
        amt = it.get("amount", 0) or 0
        yrs = it.get("frequency_years", 1) or 1
        out.append((it.get("name", "lumpy"), amt / max(1, yrs)))
    return out


def lumpy_annual(profile):
    return sum(a for _, a in lumpy_allowances(profile))


# --------------------------------------------------------------------------- #
# Shared account / holdings helpers
# --------------------------------------------------------------------------- #
def _accounts(cache):
    return cache._conn.execute(
        "SELECT id, name, type, subtype, balance FROM accounts "
        "WHERE is_hidden=0 AND is_closed=0"
    ).fetchall()


_CASH_TYPES = ("checking", "savings", "depository", "cash")
_INVEST_TYPES = ("investment", "brokerage")


def cash_total(cache):
    return sum(b or 0 for _, _, t, _, b in _accounts(cache)
              if (t or "").lower() in _CASH_TYPES and (b or 0) > 0)


def investable_assets(cache):
    total = 0.0
    for _, _, t, _, b in _accounts(cache):
        tl = (t or "").lower()
        if (b or 0) > 0 and (tl in _CASH_TYPES or tl in _INVEST_TYPES):
            total += b
    return total


def net_worth(cache):
    return sum(b or 0 for _, _, _, _, b in _accounts(cache))


def _bucket_balances(cache):
    rows = cache._conn.execute(
        "SELECT b.tax_bucket, SUM(a.balance) FROM accounts a "
        "JOIN account_tax_buckets b ON a.id = b.account_id "
        "WHERE a.is_hidden=0 AND a.is_closed=0 AND a.balance > 0 "
        "GROUP BY b.tax_bucket"
    ).fetchall()
    return {(t or "other"): (v or 0) for t, v in rows}


def daf_total(cache):
    """Directed charitable / DAF assets — isolated by the 'excluded' bucket."""
    return _bucket_balances(cache).get("excluded", 0.0)


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
    return None if not by else _dt.date.today().year - int(by)


# --------------------------------------------------------------------------- #
# Expense engine (transactions-based; classified)
# --------------------------------------------------------------------------- #
def _outflows(cache):
    return cache._conn.execute(
        "SELECT date, category, merchant, amount FROM transactions "
        "WHERE amount < 0"
    ).fetchall()


def expense_breakdown(cache, profile, window=12):
    """Classify every outflow and aggregate by month. Returns a dict with
    recurring/one_time/transfer totals, monthly recurring average over the
    trailing `window` months that actually have data, and per-month detail."""
    by_month = {}
    one_time_items = []
    for date, cat, mer, amt in _outflows(cache):
        out = -(amt or 0)
        m = (date or "")[:7]
        if not m:
            continue
        bucket = classify_outflow(cat, mer, amt, profile)
        bm = by_month.setdefault(m, {"recurring": 0.0, "one_time": 0.0,
                                     "transfer": 0.0})
        bm[bucket] += out
        if bucket == "one_time":
            one_time_items.append((date, cat or mer, out))
    months = sorted(by_month.keys(), reverse=True)[:window]
    sel = {m: by_month[m] for m in months}
    n = len(sel) or 1
    rec_total = sum(v["recurring"] for v in sel.values())
    return {
        "by_month": sel,
        "months": months,
        "n_months": len(sel),
        "recurring_monthly": rec_total / n,
        "recurring_annual": rec_total / n * 12,
        "one_time_total": sum(v["one_time"] for v in sel.values()),
        "transfer_total": sum(v["transfer"] for v in sel.values()),
        "one_time_items": one_time_items,
        "income_monthly": _income_monthly(cache, profile, months),
    }


def _income_monthly(cache, profile, months):
    rows = cache._conn.execute(
        "SELECT strftime('%Y-%m', date) m, SUM(amount) FROM transactions "
        "WHERE amount > 0 GROUP BY m"
    ).fetchall()
    inc = {m: v for m, v in rows}
    if not months:
        return 0.0
    vals = [inc.get(m, 0) or 0 for m in months]
    return sum(vals) / (len(vals) or 1)


def recurring_monthly_expense(cache, profile):
    bd = expense_breakdown(cache, profile)
    if bd["n_months"]:
        return bd["recurring_monthly"], "transactions(recurring)", bd["n_months"]
    # Fallback: cashflow, minus a transfer estimate we cannot classify there.
    rows = cache._conn.execute(
        "SELECT expense FROM cashflow ORDER BY period DESC LIMIT 12"
    ).fetchall()
    vals = [r[0] for r in rows if r[0]]
    if vals:
        return sum(vals) / len(vals), "cashflow(unclassified)", len(vals)
    return 0.0, "none", 0


def annual_expense(cache, profile):
    """Recurring run-rate + amortized lumpy allowances. Override wins."""
    if profile.get("annual_expenses_override"):
        return float(profile["annual_expenses_override"]), "override"
    m, src, _ = recurring_monthly_expense(cache, profile)
    return m * 12 + lumpy_annual(profile), src + "+lumpy"


def _recurring_annual_for(cache, profile, keywords):
    """Annualized recurring spend whose category/merchant matches any keyword."""
    if not keywords:
        return 0.0
    bd = expense_breakdown(cache, profile)
    n = bd["n_months"] or 1
    total = 0.0
    for date, cat, mer, amt in _outflows(cache):
        m = (date or "")[:7]
        if m not in bd["by_month"]:
            continue
        if classify_outflow(cat, mer, amt, profile) != "recurring":
            continue
        c, me = _norm(cat), _norm(mer)
        if any(_kw_match(k, c, me) for k in keywords):
            total += -(amt or 0)
    return total / n * 12


# --------------------------------------------------------------------------- #
# Part 2/3 — retirement budget, tax-aware FIRE number, horizon SWR
# --------------------------------------------------------------------------- #
def derive_swr(profile):
    """(rate, source). Explicit swr_pct honored; else horizon-derived
    (Pfau/Kitces): ~4% @30y → 3.5% @40y → 3.25% @45y+ (3.5% floor past ~40y)."""
    if profile.get("swr_pct"):
        return float(profile["swr_pct"]), "profile-set"
    le = profile.get("life_expectancy_age", 95) or 95
    ra = profile.get("retirement_age", 65) or 65
    h = max(1, le - ra)
    if h <= 30:
        r = 4.0
    elif h <= 40:
        r = 4.0 - 0.5 * (h - 30) / 10
    elif h <= 45:
        r = 3.5 - 0.25 * (h - 40) / 5
    else:
        r = 3.25
    return round(r, 2), f"horizon-derived ({h}y)"


def retirement_annual_budget(cache, profile):
    """Projected retirement spend (today's $). Returns (budget, components)."""
    if profile.get("annual_expenses_override"):
        base, src = float(profile["annual_expenses_override"]), "override"
    else:
        m, src, _ = recurring_monthly_expense(cache, profile)
        base = m * 12
    lump = lumpy_annual(profile)
    reductions = _recurring_annual_for(
        cache, profile, profile.get("retirement_expense_reductions", []))
    deltas = profile.get("retirement_expense_deltas", {}) or {}
    add = (deltas.get("healthcare_bridge_annual", 0) or 0) + \
          (deltas.get("extra_travel_annual", 0) or 0)
    payoff_age = deltas.get("mortgage_payoff_age")
    ret_age = profile.get("retirement_age")
    mortgage = 0.0
    if payoff_age is not None and ret_age is not None and payoff_age <= ret_age:
        mortgage = _recurring_annual_for(cache, profile, ["mortgage"])
    budget = base + lump - reductions - mortgage + add
    return budget, {
        "recurring_annual": base, "lumpy": lump, "reductions": reductions,
        "mortgage_dropped": mortgage, "additions": add, "source": src,
    }


def withdrawal_tax_shares(cache):
    """Shares of investable assets by withdrawal tax treatment.
    tax-free = roth+hsa+cash; trad = traditional (ordinary); ltcg = taxable
    brokerage + investment 'other'."""
    bb = _bucket_balances(cache)
    cash = cash_total(cache)
    roth = bb.get("roth", 0) + bb.get("hsa", 0)
    trad = bb.get("traditional", 0)
    other_inv = max(0.0, bb.get("other", 0) - cash)  # 'other' bucket holds cash
    ltcg = bb.get("taxable", 0) + other_inv
    taxfree = roth + cash
    total = taxfree + trad + ltcg
    if total <= 0:
        return {"taxfree": 0, "trad": 0, "ltcg": 0, "total": 0}
    return {"taxfree": taxfree / total, "trad": trad / total,
            "ltcg": ltcg / total, "total": total}


def effective_withdrawal_tax(cache, profile):
    s = withdrawal_tax_shares(cache)
    return s["trad"] * profile.get("retirement_trad_tax_rate", 0.18) + \
        s["ltcg"] * profile.get("retirement_ltcg_rate", 0.08), s


def fire_number(cache, profile):
    """Tax-aware FIRE number off the retirement budget. Returns (num, detail)."""
    budget, comp = retirement_annual_budget(cache, profile)
    eff, shares = effective_withdrawal_tax(cache, profile)
    gross = budget / (1 - eff) if eff < 1 else budget
    swr, swr_src = derive_swr(profile)
    num = gross / (swr / 100) if swr else None
    return num, {"budget": budget, "comp": comp, "eff_tax": eff,
                 "shares": shares, "gross": gross, "swr": swr,
                 "swr_src": swr_src}


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #
def _means_labels(profile):
    """Return (accounting_label, lifevalue_label) per net_means_mapping."""
    if (profile.get("net_means_mapping") or "A").upper() == "B":
        return "Net Means", "Net Worth"
    return "Net Worth", "Net Means"


def report_net_worth(cache, profile):
    rows = _accounts(cache)
    by_type = {}
    for _, _, t, _, b in rows:
        by_type[t] = by_type.get(t, 0.0) + (b or 0.0)
    raw = sum(by_type.values())
    daf = daf_total(cache)
    acct = raw - daf                       # accounting figure excludes DAF
    means = raw                            # life-value figure includes DAF
    acct_label, life_label = _means_labels(profile)
    assets = sum(v for v in by_type.values() if v >= 0) - daf
    liab = sum(-v for v in by_type.values() if v < 0)
    lines = [f"**{acct_label} {_money(acct)}** · {life_label} {_money(means)} "
             f"(incl. {_money(daf)} directed charitable)."]
    body = ["## Net Worth Statement\n",
            f"**{acct_label}: {_money(acct)}** (assets owned, excl. DAF)",
            f"**{life_label}: {_money(means)}** (incl. {_money(daf)} directed "
            "DAF as an encumbered, charity-committed line; experiences & "
            "relationships are real life-value but not dollar-quantified)\n",
            "| Category | Type | Balance |", "|---|---|---|"]
    for t, v in sorted(by_type.items(), key=lambda kv: -kv[1]):
        side = "Asset" if v >= 0 else "Liability"
        body.append(f"| {side} | {(t or 'other').replace('_',' ').title()} "
                    f"| {_money(v)} |")
    if daf:
        body.append(f"| Charitable (DAF, directed) | Excluded bucket | "
                    f"{_money(daf)} |")
    body.append(f"\nAssets {_money(assets)} − Liabilities {_money(liab)} "
                f"= {acct_label} **{_money(acct)}**.")
    body.append(f"\n*Investable (FIRE) assets — cash + investments, excl. real "
                f"estate & DAF: {_money(investable_assets(cache))}.*")
    return {"title": "Net Worth Statement",
            "kpi": (acct_label, _money(acct)), "summary": lines,
            "full": "\n".join(body)}


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
    drift = []
    for cls in ("us_stock", "intl_stock", "bond", "reit", "other"):
        if cls not in bd:
            continue
        a = pct.get(cls, 0)
        t = tgt.get(cls)
        band = ""
        if t is not None:
            ad = abs(a - t)
            rd = ad / t * 100 if t else 0
            if ad >= 5 or rd >= 25:
                band = "⚠ rebalance"
                drift.append(f"{label[cls]} {a:.0f}% vs {t:.0f}%")
        body.append(f"| {label[cls]} | {_money(bd[cls])} | {a:.1f}% | "
                    f"{_pct(t) if t is not None else '—'} | {band} |")
    body.append(("\n**Rebalancing (5/25):** off-target — " + "; ".join(drift)
                 + ".") if drift else
                "\n**Rebalancing (5/25):** within bands — no action.")
    fee_w = known = 0.0
    for _, ticker, _, value, _ in _holdings(cache):
        er = _EXPENSE_RATIOS.get((ticker or "").upper())
        if er is not None and value:
            fee_w += er / 100 * value
            known += value
    if known:
        body.append(f"\n**Fee drag:** ~{_money(fee_w)}/yr on {_money(known)} "
                    f"mapped ({fee_w/known*100:.3f}% blended ER).")
    return {"title": "Asset Allocation",
            "kpi": ("Stock/Bond", f"{stock:.0f}/{pct.get('bond',0):.0f}"),
            "summary": lines, "full": "\n".join(body)}


def report_asset_location(cache, profile):
    rows = cache._conn.execute(
        "SELECT a.id, a.name, b.tax_bucket FROM accounts a "
        "JOIN account_tax_buckets b ON a.id=b.account_id "
        "WHERE a.type IN ('investment','brokerage') AND a.is_hidden=0"
    ).fetchall()
    bucket_of = {r[0]: (r[2] or "other") for r in rows}
    grid = {}
    bond_in_taxable = 0.0
    for acct_id, ticker, name, value, _ in _holdings(cache):
        bucket = bucket_of.get(acct_id, "other")
        cls = classify_asset_class(ticker, name)
        grid.setdefault(bucket, {}).setdefault(cls, 0.0)
        grid[bucket][cls] += value or 0.0
        if cls == "bond" and bucket in ("taxable", "other"):
            bond_in_taxable += value or 0.0
    lines = [("Bonds in taxable: " + _money(bond_in_taxable) + " — relocate to "
              "tax-deferred.") if bond_in_taxable > 0 else
             "Tax-efficient: no bonds in taxable."]
    body = ["## Asset Location (tax-efficient placement)\n",
            "Bonds & REITs → tax-deferred; total-market stock → taxable; "
            "international → tax-deferred/taxable for the foreign tax credit.\n",
            "| Tax bucket | Bonds | US stock | Intl | REIT | Other |",
            "|---|---|---|---|---|---|"]
    for bucket in ("taxable", "traditional", "roth", "hsa", "excluded", "other"):
        if bucket not in grid:
            continue
        g = grid[bucket]
        body.append(f"| {bucket.title()} | {_money(g.get('bond',0))} | "
                    f"{_money(g.get('us_stock',0))} | {_money(g.get('intl_stock',0))} "
                    f"| {_money(g.get('reit',0))} | {_money(g.get('other',0))} |")
    body.append((f"\n⚠ **{_money(bond_in_taxable)} of bonds in taxable** — bond "
                 "interest is taxed at your full marginal rate; relocating to "
                 "Traditional/401(k) can add ~0.75%/yr after-tax.")
                if bond_in_taxable > 0 else
                "\n✓ No bonds in taxable — placement looks tax-efficient.")
    return {"title": "Asset Location",
            "kpi": ("Bonds in taxable", _money(bond_in_taxable)),
            "summary": lines, "full": "\n".join(body)}


def report_fire_progress(cache, profile):
    num, d = fire_number(cache, profile)
    inv = investable_assets(cache)
    fi_pct = (100 * inv / num) if num else None
    lean = profile["lean_annual_expenses"] * 25
    fat = profile["fat_annual_expenses"] * 25
    lines = [f"**{_pct(fi_pct)} to FI** — {_money(inv)} of {_money(num)} "
             f"(retirement budget {_money(d['budget'])}, {d['swr']:.2f}% SWR)."]
    body = ["## FIRE Progress\n",
            f"- Retirement budget (today's $): **{_money(d['budget'])}** "
            f"(recurring {_money(d['comp']['recurring_annual'])} + lumpy "
            f"{_money(d['comp']['lumpy'])} − reductions "
            f"{_money(d['comp']['reductions'])} − mortgage-dropped "
            f"{_money(d['comp']['mortgage_dropped'])} + adds "
            f"{_money(d['comp']['additions'])})",
            f"- Tax gross-up: ÷(1−{d['eff_tax']*100:.1f}%) for the "
            f"trad/LTCG-funded share → **gross {_money(d['gross'])}**",
            f"- SWR: **{d['swr']:.2f}%** ({d['swr_src']}) → multiplier "
            f"**{100/d['swr']:.1f}×**" if d['swr'] else "- SWR: —",
            f"- **FIRE number: {_money(num)}**",
            f"- Investable assets: **{_money(inv)}**",
            f"- **Progress to FI: {_pct(fi_pct)}**\n",
            "| Tier | Annual spend | Target (25×) | Reached? |",
            "|---|---|---|---|",
            f"| Lean FIRE | {_money(profile['lean_annual_expenses'])} | "
            f"{_money(lean)} | {'✓' if inv>=lean else '—'} |",
            f"| Your FIRE | {_money(d['budget'])} (gross {_money(d['gross'])}) | "
            f"{_money(num)} | {'✓' if num and inv>=num else '—'} |",
            f"| Fat FIRE | {_money(profile['fat_annual_expenses'])} | "
            f"{_money(fat)} | {'✓' if inv>=fat else '—'} |"]
    if fi_pct is not None and fi_pct < 100:
        body.append(f"\nGap to FI: **{_money(num - inv)}**.")
    body.append("\n*Budget excludes investment contributions & one-time "
                "capital outlays. Tax rates are effective retirement "
                "assumptions (profile-set).*")
    return {"title": "FIRE Progress", "kpi": ("Progress to FI", _pct(fi_pct)),
            "summary": lines, "full": "\n".join(body)}


def report_withdrawal(cache, profile):
    inv = investable_assets(cache)
    num, d = fire_number(cache, profile)
    budget = d["budget"]
    swr, swr_src = d["swr"], d["swr_src"]
    lines = [f"{swr:.2f}% safe spend: **{_money(inv*swr/100)}/yr** "
             f"(net, after-tax ~{_money(inv*swr/100*(1-d['eff_tax']))}) on "
             f"{_money(inv)} — SWR {swr_src}."]
    body = ["## Safe Withdrawal & Dynamic Spending\n",
            f"Investable: **{_money(inv)}**. SWR **{swr:.2f}%** ({swr_src}); "
            f"4% is too aggressive for a long early-retirement horizon.\n",
            "| Withdrawal rate | Gross income | Net (after tax) | Covers budget? |",
            "|---|---|---|---|"]
    for rate in sorted({4.0, 3.5, 3.25, round(swr, 2)}, reverse=True):
        gross = inv * rate / 100
        net = gross * (1 - d["eff_tax"])
        body.append(f"| {rate:.2f}%{' ◀ default' if abs(rate-swr)<0.01 else ''} "
                    f"| {_money(gross)} | {_money(net)} | "
                    f"{'✓' if net >= budget else '—'} |")
    body.append(f"\nRetirement budget (after-tax): {_money(budget)}.")
    # Guyton-Klinger guardrails
    g0 = profile.get("guardrail_initial_swr_pct", 4.5)
    band = profile.get("guardrail_band_pct", 20)
    body += ["\n### Guyton-Klinger guardrails (dynamic spending)\n",
             f"Start higher at **{g0:.2f}%** = {_money(inv*g0/100)}/yr gross, "
             f"with ±{band}% guardrails: cut spending ~10% if the withdrawal "
             f"rate rises >{band}% above initial (portfolio fell), raise ~10% "
             f"if it falls >{band}% below. Front-loads income for the "
             "die-with-zero objective — **tradeoff:** real mid-retirement cuts "
             "are possible in bad sequences."]
    # Coast FIRE
    age = _current_age(profile)
    r = profile["expected_real_return_pct"] / 100
    ret_age = profile["retirement_age"]
    body.append("\n### Coast FIRE\n")
    if age and num and ret_age > age:
        yrs = ret_age - age
        coast = num / ((1 + r) ** yrs)
        reached = inv >= coast
        body.append(f"Age {age} → retire {ret_age} ({yrs}y @ {r*100:.0f}% real): "
                    f"**Coast number {_money(coast)}**. "
                    + ("✓ Reached — current portfolio coasts to FI with no new "
                       "contributions." if reached else
                       f"{_money(coast-inv)} to go."))
        lines.append("Coast FIRE " + ("reached." if reached
                     else f"{_money(coast-inv)} away."))
    else:
        body.append("Set `birth_year` to compute Coast FIRE.")
    return {"title": "Safe Withdrawal & Dynamic Spending",
            "kpi": (f"{swr:.2f}% income", _money(inv*swr/100)),
            "summary": lines, "full": "\n".join(body)}


def report_savings_rate(cache, profile):
    bd = expense_breakdown(cache, profile)
    if not bd["n_months"]:
        return _empty("Savings Rate", "No transactions cached — run `sync-all`.")
    inc_m = cache._conn.execute(
        "SELECT strftime('%Y-%m', date) m, SUM(amount) FROM transactions "
        "WHERE amount > 0 GROUP BY m"
    ).fetchall()
    inc = {m: (v or 0) for m, v in inc_m}
    months = bd["months"]
    tot_inc = sum(inc.get(m, 0) for m in months)
    tot_rec = sum(bd["by_month"][m]["recurring"] for m in months)
    tot_xfer = sum(bd["by_month"][m]["transfer"] for m in months)
    cons_rate = 100 * (tot_inc - tot_rec) / tot_inc if tot_inc else 0
    deploy_rate = 100 * tot_xfer / tot_inc if tot_inc else 0
    lines = [f"**{cons_rate:.0f}%** consumption-based savings rate; "
             f"**{deploy_rate:.0f}%** of income deployed to "
             f"investments/principal (trailing {len(months)}mo)."]
    body = ["## Savings Rate\n",
            f"Trailing {len(months)} months: income {_money(tot_inc)}, "
            f"recurring consumption {_money(tot_rec)} → **savings rate "
            f"{cons_rate:.0f}%** (income − recurring; excludes transfers & "
            f"one-time).",
            f"Capital deployed (investment buys, contributions, debt "
            f"principal): {_money(tot_xfer)} → **{deploy_rate:.0f}% of "
            f"income**.\n",
            "*Investment purchases and contributions are savings, NOT "
            "expenses — counting them as spend is what produced the bogus "
            "negative rate.*\n",
            "| Month | Income | Recurring | Capital deployed | One-time | Rate |",
            "|---|---|---|---|---|---|"]
    for m in reversed(months):
        b = bd["by_month"][m]
        i = inc.get(m, 0)
        rate = 100 * (i - b["recurring"]) / i if i else 0
        ot = f"{_money(b['one_time'])} ⚠" if b["one_time"] > 0 else "—"
        body.append(f"| {m} | {_money(i)} | {_money(b['recurring'])} | "
                    f"{_money(b['transfer'])} | {ot} | {rate:.0f}% |")
    if bd["one_time_total"] > 0:
        body.append(f"\n⚠ Months flagged contain one-time capital outlays "
                    f"({_money(bd['one_time_total'])} total) excluded from the "
                    "rate so a single capital month doesn't read as deeply "
                    "negative.")
    return {"title": "Savings Rate", "kpi": ("Savings rate", f"{cons_rate:.0f}%"),
            "summary": lines, "full": "\n".join(body)}


def report_expenses(cache, profile):
    bd = expense_breakdown(cache, profile)
    n = bd["n_months"] or 1
    ess_kw = [k.lower() for k in profile["essential_categories"]]
    rows = cache._conn.execute(
        "SELECT date, category, merchant, amount FROM transactions "
        "WHERE amount < 0"
    ).fetchall()
    rec_by_cat = {}
    ess = disc = 0.0
    for date, cat, mer, amt in rows:
        if (date or "")[:7] not in bd["by_month"]:
            continue
        if classify_outflow(cat, mer, amt, profile) != "recurring":
            continue
        out = -(amt or 0)
        rec_by_cat[cat] = rec_by_cat.get(cat, 0) + out
        if any(k in _norm(cat) for k in ess_kw):
            ess += out
        else:
            disc += out
    rec_total = ess + disc
    lump = lumpy_allowances(profile)
    lump_total = sum(a for _, a in lump)
    ess_share = 100 * ess / rec_total if rec_total else 0
    lines = [f"Recurring budget ~{_money(rec_total/n*12 + lump_total)}/yr — "
             f"**{ess_share:.0f}% essential / {100-ess_share:.0f}% "
             f"discretionary**; {_money(bd['one_time_total'])} one-time "
             f"excluded."]
    body = ["## Expense Analysis\n",
            "### 1. Recurring budget (the real run-rate)\n",
            f"Trailing-{n}mo recurring: **{_money(rec_total)}** → "
            f"{_money(rec_total/n*12)}/yr. Essential {_money(ess)} "
            f"({ess_share:.0f}%), discretionary {_money(disc)} "
            f"({100-ess_share:.0f}%).\n",
            "| Category | Recurring spend | Bucket |", "|---|---|---|"]
    for cat, v in sorted(rec_by_cat.items(), key=lambda kv: -kv[1]):
        bucket = "essential" if any(k in _norm(cat) for k in ess_kw) \
            else "discretionary"
        body.append(f"| {cat} | {_money(v)} | {bucket} |")
    body += ["\n### 2. Lumpy allowances (amortized sinking funds)\n",
             f"Added to the annual budget: **{_money(lump_total)}/yr**.\n",
             "| Item | Annual allowance |", "|---|---|"]
    for name, a in lump:
        body.append(f"| {name} | {_money(a)} |")
    body += ["\n### 3. Capital outlays & transfers (EXCLUDED from run-rate)\n",
             f"One-time outlays: **{_money(bd['one_time_total'])}** · "
             f"transfers/savings: **{_money(bd['transfer_total'])}** "
             "(investment buys, contributions, principal — not consumption)."]
    if bd["one_time_items"]:
        body.append("\nOne-time items:")
        for date, what, amt in sorted(bd["one_time_items"],
                                      key=lambda x: -x[2])[:8]:
            body.append(f"- {date}: {what} — {_money(amt)}")
    return {"title": "Expense Analysis",
            "kpi": ("Essential share", f"{ess_share:.0f}%" if rec_total else "—"),
            "summary": lines, "full": "\n".join(body)}


def report_emergency_fund(cache, profile):
    cash = cash_total(cache)
    m, src, _ = recurring_monthly_expense(cache, profile)
    bb = _bucket_balances(cache)
    taxable = bb.get("taxable", 0)
    accessible = cash + (taxable if profile.get("count_taxable_as_liquid_for_ef")
                         else 0)
    months_cash = cash / m if m else None
    months_acc = accessible / m if m else None
    target = profile.get("emergency_fund_months_target", 3)
    stable = profile.get("income_stability", "stable") == "stable"
    if profile.get("count_taxable_as_liquid_for_ef") and accessible > cash:
        verdict = (f"{months_acc:.0f}+ months accessible (cash + taxable "
                   "brokerage) — liquidity is a non-issue")
        flag = "cash-drag question, not coverage"
    elif months_cash and months_cash >= target:
        verdict = f"{months_cash:.1f} mo cash ≥ {target} target"
        flag = "✓ adequate"
    else:
        verdict = f"{months_cash:.1f} mo cash"
        flag = "thin if income were unstable" if not stable else "⚠ below target"
    lines = [f"{verdict} — {flag}."]
    body = ["## Emergency Fund Coverage\n",
            f"- Cash & equivalents: **{_money(cash)}** "
            f"({months_cash:.1f} mo of recurring expenses)" if months_cash
            else f"- Cash: {_money(cash)}",
            f"- Accessible liquid (cash + taxable brokerage): "
            f"**{_money(accessible)}** ({months_acc:.0f}+ mo)"
            if profile.get("count_taxable_as_liquid_for_ef") else "",
            f"- Income stability: **{profile.get('income_stability','stable')}**; "
            f"target **{target} mo**\n",
            f"For a {'stable' if stable else 'variable'}-income earner with a "
            "large liquid taxable account, the relevant question is **cash "
            "drag vs. liquidity**, not whether 6 months sit in savings. "
            f"Verdict: {flag}." if months_cash else ""]
    return {"title": "Emergency Fund",
            "kpi": ("EF (cash)", f"{months_cash:.1f} mo" if months_cash else "—"),
            "summary": lines, "full": "\n".join(body)}


def report_tlh(cache, profile):
    rows = cache._conn.execute(
        "SELECT account_id, ticker, name, value, cost_basis FROM holdings "
        "WHERE cost_basis IS NOT NULL AND ticker IS NOT NULL "
        "AND value < cost_basis ORDER BY (value - cost_basis) ASC"
    ).fetchall()
    loss = sum((v - cb) for _, _, _, v, cb in rows)
    lines = [f"**{_money(-loss)}** harvestable loss across {len(rows)} "
             f"position(s)." if rows else "No positions at a loss."]
    body = ["## Tax-Loss Harvesting\n"]
    if rows:
        body += [f"**{len(rows)} underwater — {_money(-loss)} harvestable.**\n",
                 "| Account | Ticker | Value | Cost basis | Unrealized |",
                 "|---|---|---|---|---|"]
        for acct, tk, nm, v, cb in rows:
            body.append(f"| {acct} | {tk} | {_money(v)} | {_money(cb)} | "
                        f"{_money(v-cb)} |")
        body.append("\n⚠ **Lot-level caveat:** Monarch reports blended "
                    "position basis, not tax lots. Confirm specific lots and "
                    "**wash-sale** windows (±30 days, all accounts incl. IRAs) "
                    "at the brokerage before harvesting.")
    else:
        body.append("No holdings below cost basis. (Unknown-basis / no-ticker "
                    "positions excluded.)")
    return {"title": "Tax-Loss Harvesting",
            "kpi": ("Harvestable loss", _money(-loss) if rows else "$0"),
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
    single = [(tk, nm, v) for tk, nm, v in rows
              if classify_asset_class(tk, nm) == "us_stock" and v
              and (tk or "").upper() not in _US_STOCK]
    single_pct = 100 * sum(v for _, _, v in single) / total
    views = profile.get("sector_views", {}) or {}
    tilt_note = " (intentional tilt per profile sector_views)" \
        if any(views.get(h) for h in ("1y", "5y", "10y")) else ""
    lines = [f"Top **{top[0]} {top_pct:.0f}%**; single-stock {single_pct:.0f}% "
             f"of portfolio{tilt_note}."]
    body = ["## Portfolio Concentration & Single-Stock Risk\n",
            f"Portfolio {_money(total)}. Top **{top[0]} ({top_pct:.1f}%)**. "
            f"Individual-stock exposure (excl. broad funds): "
            f"**{single_pct:.1f}%**.{tilt_note}\n",
            "| Ticker | Value | % portfolio | Flag |", "|---|---|---|---|"]
    for tk, nm, v in rows[:15]:
        p = 100 * (v or 0) / total
        flag = "⚠ >5%" if p > 5 and (tk or "").upper() not in _US_STOCK else ""
        body.append(f"| {tk} | {_money(v)} | {p:.1f}% | {flag} |")
    if tilt_note:
        body.append("\nFlagged single-name weight reflects an intended sector "
                    "view, not unmanaged drift — judge against that thesis.")
    return {"title": "Portfolio Concentration",
            "kpi": ("Top position", f"{top[0]} {top_pct:.0f}%"),
            "summary": lines, "full": "\n".join(body)}


def report_life_value(cache, profile):
    """Die-with-zero framing: time-discounted life spend + spend-down glidepath
    + Means/Worth pair."""
    bd = expense_breakdown(cache, profile)
    n = bd["n_months"] or 1
    lv_kw = [k.lower() for k in profile.get("life_value_categories", [])]
    lv_annual = 0.0
    rows = cache._conn.execute(
        "SELECT date, category, merchant, amount FROM transactions "
        "WHERE amount < 0"
    ).fetchall()
    for date, cat, mer, amt in rows:
        if (date or "")[:7] not in bd["by_month"]:
            continue
        if classify_outflow(cat, mer, amt, profile) != "recurring":
            continue
        if any(k in _norm(cat) or k in _norm(mer) for k in lv_kw):
            lv_annual += -(amt or 0)
    lv_annual = lv_annual / n * 12
    acct_label, life_label = _means_labels(profile)
    daf = daf_total(cache)
    # Spend-down glidepath projection.
    inv = investable_assets(cache)
    num, d = fire_number(cache, profile)
    r = profile["expected_real_return_pct"] / 100
    age = _current_age(profile)
    ret_age = profile.get("retirement_age", 65)
    le = profile.get("life_expectancy_age", 95)
    gross = d["gross"]
    bal = inv
    end_bal = None
    if age:
        for yr in range(age, le):
            grow = bal * (1 + r)
            draw = gross if yr >= ret_age else 0
            bal = grow - draw
        end_bal = bal
    disc = profile.get("life_value_discount_rate_pct", 5.0)
    lines = [f"Life-value spend ~{_money(lv_annual)}/yr (travel/charity/"
             f"experiences) — the point, not leakage."]
    body = ["## Life-Value / Die-With-Zero\n",
            f"Objective: maximize **time-discounted value of life lived** "
            f"(experiences, giving, relationships) under a never-outlive-means "
            f"constraint — not terminal estate.\n",
            f"- **{life_label}** (incl. {_money(daf)} directed DAF) vs "
            f"**{acct_label}** {_money(net_worth(cache)-daf)} — directed "
            "charitable assets are held life-value even though excluded from "
            "the accounting figure.",
            f"- Life-value recurring spend: **{_money(lv_annual)}/yr** "
            "(travel, sailing, charity, gifts) — presented positively; only "
            "convenience/consumption creep is worth scrutiny.",
            f"- Future life-value spend discounted at "
            f"**{disc:.1f}%/yr**: an experience or gift later is worth less "
            "than the same dollars now → **front-load** experiential & "
            "charitable spend; the static SWR is likely too conservative "
            "here."]
    if end_bal is not None:
        verdict = ("balloons — a die-with-zero signal to **spend/give more "
                   "now** (or earlier-retire)" if end_bal > inv * 0.5 else
                   "trends toward zero — consistent with the objective"
                   if end_bal > 0 else
                   "depletes before plan age — too aggressive")
        body.append(f"- **Spend-down glidepath:** at {d['swr']:.2f}% / "
                    f"{_money(gross)} gross draw from age {ret_age}, projected "
                    f"real balance at {le} ≈ **{_money(end_bal)}** — {verdict}.")
    else:
        body.append("- Spend-down glidepath: set `birth_year` to project the "
                    "terminal balance.")
    body.append("\n*Pair this with the guardrails view (Safe Withdrawal) — "
                "higher early income with downside guardrails best serves "
                "front-loaded life-value.*")
    return {"title": "Life-Value / Die-With-Zero",
            "kpi": ("Life-value spend", f"{_money(lv_annual)}/yr"),
            "summary": lines, "full": "\n".join(body)}


def _empty(title, msg):
    return {"title": title, "kpi": None, "summary": [msg],
            "full": f"## {title}\n\n{msg}"}


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
    ("life-value", report_life_value),
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


def _interview_banner(profile):
    if profile.get("profile_complete"):
        return None
    return ("> ⚠ **Profile incomplete.** Before computing, run the first-run "
            "interview (SKILL.md §2b): pre-fill every field from context/cache/"
            "memory, set research-grounded defaults, ask ONLY the genuine gaps "
            "with pick-lists, write `profile.json` with `profile_complete: "
            "true`, then re-render. Numbers below use defaults and may be off "
            "(SWR horizon-derived; age-dependent views skipped if birth_year "
            "is unset).")


def render_dashboard(cache, profile):
    out = ["# 💰 Financial Dashboard (Boglehead / FIRE / Die-With-Zero)\n"]
    banner = _interview_banner(profile)
    if banner:
        out.append(banner + "\n")
    kpis, results = [], {}
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
    out.append("*Render as one self-contained HTML artifact: KPI strip + a "
               "summary card per report, each linking (accordion/anchor) to "
               "its full body. Get all bodies in one call with "
               "`python scripts/fetch.py report all`. Assumptions live in "
               "profile.json — see references/reports.md.*")
    return "\n".join(out)
