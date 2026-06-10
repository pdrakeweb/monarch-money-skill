"""Tests that the normalisers handle LIVE GraphQL shapes (nested objects),
not just the flat camelCase fixtures. These shapes are taken verbatim from the
monarchmoneycommunity 1.3.2 query fragments."""
from fetch import (
    normalize_account, normalize_txn, normalize_holding, normalize_recurring,
    extract_holding_nodes, _extract_budget_rows, _month_bounds, _shift_month,
)


def test_account_live_nested_type():
    a = {
        "id": "a1", "displayName": "My 401k",
        "type": {"name": "investment", "display": "Investment"},
        "subtype": {"name": "401k", "display": "401(k)"},
        "institution": {"name": "Fidelity"},
        "currentBalance": 100.0, "displayBalance": 100.0,
        "isManual": False, "isHidden": False,
    }
    n = normalize_account(a)
    assert n["type"] == "investment"          # NOT the dict repr
    assert n["subtype"] == "401k"
    assert n["institution"] == "Fidelity"
    assert n["name"] == "My 401k"
    assert n["balance"] == 100.0


def test_txn_live_nested_merchant_category():
    t = {
        "id": "t1", "amount": -42.0, "pending": True, "date": "2026-06-01",
        "notes": None,
        "category": {"id": "c9", "name": "Groceries",
                     "group": {"id": "g1", "type": "expense"}},
        "merchant": {"name": "Grocery Store A", "id": "m1"},
        "account": {"id": "acc_001"},
        "tags": [{"name": "vacation"}],
    }
    n = normalize_txn(t)
    assert n["merchant"] == "Grocery Store A"
    assert n["category"] == "Groceries"
    assert n["account_id"] == "acc_001"
    assert n["is_pending"] is True
    assert n["tags"] == ["vacation"]


def test_txn_fixture_flat_shape_still_works():
    t = {"id": "t1", "accountId": "acc_001", "date": "2025-09-01",
         "amount": -10.0, "merchantName": "M", "category": "Dining",
         "categoryGroup": "Food & Drink", "isPending": False}
    n = normalize_txn(t)
    assert n["merchant"] == "M"
    assert n["category"] == "Dining"
    assert n["category_group"] == "Food & Drink"


def test_holding_live_node_shape():
    node = {
        "id": "h1", "quantity": 10.0, "basis": 900.0, "totalValue": 1100.0,
        "security": {"ticker": "VTI", "name": "Vanguard Total Stock",
                     "currentPrice": 110.0},
    }
    n = normalize_holding(node)
    assert n["ticker"] == "VTI"
    assert n["value"] == 1100.0
    assert n["cost_basis"] == 900.0
    assert n["price"] == 110.0


def test_extract_holding_nodes_from_live_response():
    data = {"portfolio": {"aggregateHoldings": {"edges": [
        {"node": {"id": "h1", "quantity": 1.0}},
        {"node": None},
        {"node": {"id": "h2", "quantity": 2.0}},
    ]}}}
    nodes = extract_holding_nodes(data)
    assert [x["id"] for x in nodes] == ["h1", "h2"]


def test_extract_holding_nodes_fallback_list():
    assert extract_holding_nodes([{"ticker": "X"}]) == [{"ticker": "X"}]


def test_recurring_live_stream_shape():
    r = {
        "stream": {"id": "s1", "frequency": "monthly", "amount": -15.0,
                   "merchant": {"name": "Streaming Svc 1"}},
        "date": "2026-06-12", "amount": -15.0,
        "account": {"id": "acc_006"},
    }
    n = normalize_recurring(r)
    assert n["merchant"] == "Streaming Svc 1"
    assert n["frequency"] == "monthly"
    assert n["next_date"] == "2026-06-12"
    assert n["account_id"] == "acc_006"
    assert n["id"] == "s1"


def test_holding_zero_basis_is_unknown_not_zero():
    # QA BUG-5: custodians that report no basis send 0 — treating it as a
    # real $0 basis fabricates a 100% gain and pollutes TLH screens.
    node = {"quantity": 100.0, "basis": 0, "totalValue": 5000.0,
            "security": {"ticker": None, "name": "SS TOT STK MKT IDX I",
                         "currentPrice": 50.0}}
    n = normalize_holding(node)
    assert n["cost_basis"] is None
    assert n["ticker"] is None


def test_budget_rows_names_from_category_groups():
    # QA BUG-2: monthlyAmountsByCategory[].category carries only {id};
    # the names live under categoryGroups[].categories[].
    data = {
        "categoryGroups": [
            {"name": "Food & Drink", "categories": [
                {"id": "c_groc", "name": "Groceries"},
                {"id": "c_dine", "name": "Dining"},
            ]},
            {"name": "Bills", "categories": [
                {"id": "c_util", "name": "Utilities"},
            ]},
        ],
        "budgetData": {"monthlyAmountsByCategory": [
            {"category": {"id": "c_groc", "__typename": "Category"},
             "monthlyAmounts": [{"plannedCashFlowAmount": 600.0,
                                 "actualAmount": 540.0}]},
            {"category": {"id": "c_util", "__typename": "Category"},
             "monthlyAmounts": [{"plannedCashFlowAmount": 300.0,
                                 "actualAmount": 345.0}]},
        ]},
    }
    rows = _extract_budget_rows(data)
    assert rows[0]["category"] == "Groceries"
    assert rows[0]["categoryGroup"] == "Food & Drink"
    assert rows[0]["budgeted"] == 600.0
    assert rows[1]["category"] == "Utilities"
    assert rows[1]["categoryGroup"] == "Bills"


def test_month_helpers():
    assert _month_bounds("2026-06") == ("2026-06-01", "2026-06-30")
    assert _month_bounds("2026-12") == ("2026-12-01", "2026-12-31")
    assert _shift_month("2026-01", -1) == "2025-12"
    assert _shift_month("2026-06", -5) == "2026-01"
    assert _shift_month("2026-06", 0) == "2026-06"
