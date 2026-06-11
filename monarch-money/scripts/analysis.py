"""
analysis.py — pure analysis helpers for the monarch-money skill.

No DB access, no I/O, no side effects: functions take plain Python data and
return plain Python data, so they are trivially unit-testable and reusable by
the downstream personal-finance / retirement skills.

`suggest_exclusions()` scans a list of transactions and proposes candidate
one-time / non-recurring expenses (orthodontia, surgery, an annual insurance
lump, a big home repair) that skew monthly cashflow, savings rate, and category
trends. It only PROPOSES — the skill never auto-adds exclusions; the user
reviews each `suggested_command` and runs it (or not).
"""
import statistics

# Case-insensitive substrings that strongly imply a one-off or annual-lump
# expense (heuristic 3). Order doesn't matter — first match wins per tx.
_KEYWORDS = (
    "orthodont", "braces", "surgery", "implant", "procedure", "funeral",
    "wedding", "adoption", "annual", "yearly", "renewal", "insurance lump",
    "premium", "deductible",
)

# Tunables — kept conservative so regular spending never trips a suggestion.
_OUTLIER_MULTIPLE = 3.0     # tx > 3× the category's median monthly total
_OUTLIER_FLOOR = 200.0      # ...and at least this many dollars (noise guard)
_SPARSE_MAX_MONTHS = 2      # category present in ≤2 distinct months
_SINGLE_LARGE_MIN = 500.0   # one tx over this...
_SMALL_CATEGORY_AVG = 100.0  # ...in a category whose other tx average under this

_CONFIDENCE_RANK = {"High": 3, "Medium": 2, "Low": 1}


def _month(date):
    return (date or "")[:7]


def _amt(tx):
    """Absolute outflow magnitude for an expense transaction."""
    return abs(tx.get("amount") or 0.0)


def _command(type, value, start=None, end=None, reason=None):
    parts = [f"python scripts/manage_exclusions.py add --type {type}",
             f'--value "{value}"']
    if start:
        parts.append(f"--start {start}")
    if end:
        parts.append(f"--end {end}")
    if reason:
        parts.append(f'--reason "{reason}"')
    return " ".join(parts)


def _suggestion(confidence, type, value, reason, start=None, end=None):
    return {
        "confidence": confidence,
        "type": type,
        "value": value,
        "start_date": start,
        "end_date": end,
        "reason": reason,
        "suggested_command": _command(type, value, start, end, reason),
    }


def suggest_exclusions(transactions):
    """Return a list of candidate-exclusion dicts, highest confidence first.

    Each dict: {confidence, type, value, start_date, end_date, reason,
    suggested_command}. Pure function — `transactions` is a list of dicts with
    at least: id, date, amount, merchant, category, notes (amount<0 = expense).
    """
    expenses = [t for t in transactions if (t.get("amount") or 0.0) < 0]

    by_cat = {}                 # category -> [tx, ...]
    cat_month_total = {}        # category -> {month: total_abs}
    for t in expenses:
        cat = t.get("category") or "(uncategorized)"
        by_cat.setdefault(cat, []).append(t)
        m = _month(t.get("date"))
        cat_month_total.setdefault(cat, {})
        cat_month_total[cat][m] = cat_month_total[cat].get(m, 0.0) + _amt(t)

    suggestions = []

    # Heuristic 1 — large outliers (High): a single tx exceeding 3× the
    # category's median monthly total. Routes to a category_period exclusion
    # scoped to the offending month.
    for cat, txns in by_cat.items():
        monthly = list(cat_month_total[cat].values())
        if not monthly:
            continue
        median_monthly = statistics.median(monthly)
        if median_monthly <= 0:
            continue
        for t in txns:
            a = _amt(t)
            if a > _OUTLIER_MULTIPLE * median_monthly and a >= _OUTLIER_FLOOR:
                m = _month(t.get("date"))
                ratio = a / median_monthly
                merch = t.get("merchant") or cat
                suggestions.append(_suggestion(
                    "High", "category_period", cat,
                    reason=(f'"{merch}" (${a:,.0f}) — {ratio:.0f}× the '
                            f"category's typical month"),
                    start=m, end=m))

    # Heuristic 2 — sparse categories (Medium): a category appearing in ≤2 of
    # the (up to) last 12 distinct months is unlikely to be a recurring budget
    # line. Routes to an all-time category exclusion.
    all_months = sorted({_month(t.get("date")) for t in expenses})
    window = set(all_months[-12:])
    for cat, months in cat_month_total.items():
        active = [m for m in months if m in window]
        if 0 < len(active) <= _SPARSE_MAX_MONTHS:
            suggestions.append(_suggestion(
                "Medium", "category", cat,
                reason=(f"appears in only {len(active)} of "
                        f"{len(window)} recent months")))

    # Heuristic 3 — keyword patterns (High): merchant or notes matches a
    # one-off / annual-lump keyword. Routes to a single-transaction exclusion.
    for t in expenses:
        hay = f"{t.get('merchant') or ''} {t.get('notes') or ''}".lower()
        kw = next((k for k in _KEYWORDS if k in hay), None)
        if kw and t.get("id"):
            suggestions.append(_suggestion(
                "High", "transaction", t["id"],
                reason=(f'{t.get("merchant") or "transaction"} '
                        f'(${_amt(t):,.0f}) matches "{kw}"')))

    # Heuristic 4 — single large tx in an otherwise small category (Medium):
    # one tx > $500 where the category's other tx average < $100.
    for cat, txns in by_cat.items():
        for t in txns:
            a = _amt(t)
            if a <= _SINGLE_LARGE_MIN or not t.get("id"):
                continue
            others = [_amt(o) for o in txns if o is not t]
            others_avg = statistics.mean(others) if others else 0.0
            if others and others_avg < _SMALL_CATEGORY_AVG:
                suggestions.append(_suggestion(
                    "Medium", "transaction", t["id"],
                    reason=(f'{t.get("merchant") or "transaction"} '
                            f"(${a:,.0f}) dwarfs {cat} average "
                            f"(${others_avg:,.0f})")))

    # De-duplicate by (type, value, start, end), keeping the highest
    # confidence; then sort highest confidence first (stable within a tier).
    best = {}
    for s in suggestions:
        key = (s["type"], s["value"], s["start_date"], s["end_date"])
        cur = best.get(key)
        if cur is None or _CONFIDENCE_RANK[s["confidence"]] > _CONFIDENCE_RANK[cur["confidence"]]:
            best[key] = s
    return sorted(best.values(),
                  key=lambda s: -_CONFIDENCE_RANK[s["confidence"]])
