"""Pure unit tests for analysis.suggest_exclusions() — no DB, plain dicts in,
suggestion dicts out. Each test isolates one heuristic."""
from analysis import suggest_exclusions


def _tx(id, date, amount, category, merchant="Store", notes=""):
    return {"id": id, "date": date, "amount": amount, "category": category,
            "merchant": merchant, "notes": notes}


def _months(n, start=1):
    """ISO first-of-month dates 2024-{start..}."""
    return [f"2024-{m:02d}-15" for m in range(start, start + n)]


def test_suggests_large_outlier():
    # Dental ~$200/month for 11 months, then one $4,800 spike.
    txns = [_tx(f"d{i}", d, -200, "Dental") for i, d in enumerate(_months(11))]
    txns.append(_tx("spike", "2024-12-10", -4800, "Dental", merchant="Endo"))
    sug = suggest_exclusions(txns)
    outliers = [s for s in sug
                if s["type"] == "category_period" and s["value"] == "Dental"]
    assert outliers, "expected a category_period outlier suggestion for Dental"
    assert outliers[0]["confidence"] == "High"
    assert outliers[0]["start_date"] == "2024-12"


def test_suggests_sparse_category():
    # 12 months of Groceries, but "Orthodontia" appears in exactly one.
    txns = [_tx(f"g{i}", d, -500, "Groceries") for i, d in enumerate(_months(12))]
    txns.append(_tx("o1", "2024-06-01", -300, "Orthodontia"))
    sug = suggest_exclusions(txns)
    sparse = [s for s in sug
              if s["type"] == "category" and s["value"] == "Orthodontia"]
    assert sparse, "expected a sparse-category suggestion for Orthodontia"
    assert sparse[0]["confidence"] == "Medium"


def test_suggests_keyword_orthodontia():
    txns = [_tx("k1", "2024-03-10", -1500, "Health",
                merchant="Orthodontist Associates")]
    sug = suggest_exclusions(txns)
    kw = [s for s in sug if s["type"] == "transaction" and s["value"] == "k1"]
    assert kw, "expected a keyword suggestion for the Orthodontist tx"
    assert kw[0]["confidence"] == "High"


def test_suggests_single_large_in_small_category():
    # "Shopping" averages ~$50, with one $600 outlier tx.
    txns = [_tx(f"s{i}", d, -50, "Shopping") for i, d in enumerate(_months(6))]
    txns.append(_tx("big", "2024-07-01", -600, "Shopping", merchant="Big Buy"))
    sug = suggest_exclusions(txns)
    hits = [s for s in sug
            if s["type"] == "transaction" and s["value"] == "big"]
    assert hits, "expected a single-large-in-small-category suggestion"


def test_no_suggestion_for_regular_expenses():
    # Groceries $400–$600 every month for 12 months, normal-sized line items.
    txns = []
    for i, d in enumerate(_months(12)):
        amt = -(400 + (i % 3) * 100)  # 400/500/600 cycling
        # split into small line items so no single tx is an outlier
        txns.append(_tx(f"a{i}", d, amt / 2, "Groceries", merchant="Market"))
        txns.append(_tx(f"b{i}", d, amt / 2, "Groceries", merchant="Market"))
    sug = suggest_exclusions(txns)
    assert sug == [], f"regular groceries should yield no suggestions, got {sug}"


def test_income_ignored():
    # Inflows (amount > 0) are never expense suggestions.
    txns = [_tx(f"p{i}", d, 3000, "Income", merchant="Payroll")
            for i, d in enumerate(_months(2))]
    assert suggest_exclusions(txns) == []
