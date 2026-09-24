"""Skill-implemented Monarch writes (monarch_writes.py) and their gating.
No network: a fake client records gql_call and returns canned payloads."""
import asyncio

import pytest
from gql import gql

import monarch_writes as W
from monarch_client import (
    CONFIRM_METHODS, ConfirmationRequiredError, GuardedMonarch,
)


class FakeMM:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    async def gql_call(self, operation, graphql_query, variables=None):
        self.calls.append((operation, variables))
        resp = self.responses.get(operation, {})
        return resp(variables) if callable(resp) else resp


def run(coro):
    return asyncio.run(coro)


EXISTING_RULE = {
    "id": "r1", "merchantCriteriaUseOriginalStatement": False,
    "merchantCriteria": [{"operator": "contains", "value": "third federal"}],
    "amountCriteria": {"operator": "gt", "isExpense": False, "value": 100.0,
                       "valueRange": None},
    "categoryIds": None, "accountIds": ["a1"],
    "setCategoryAction": {"id": "c9", "name": "Mortgage"},
    "setMerchantAction": None, "addTagsAction": [],
    "setHideFromReportsAction": None, "reviewStatusAction": None,
    "sendNotificationAction": False, "splitTransactionsAction": None,
}


# ---- error contract ------------------------------------------------------ #
@pytest.mark.parametrize("errors", [
    None, {}, {"message": None, "fieldErrors": []},
    {"message": "", "fieldErrors": None, "code": None}, [],
])
def test_empty_errors_are_success(errors):
    W.check_errors(errors, "x")


@pytest.mark.parametrize("errors", [
    {"message": "bad"},
    {"message": None, "fieldErrors": [{"field": "input", "messages": ["no"]}]},
    [{"message": "bad", "code": "X"}],
])
def test_nonempty_errors_fail(errors):
    with pytest.raises(W.MonarchWriteError):
        W.check_errors(errors, "x")


# ---- 1. transaction rules ------------------------------------------------ #
def test_create_rule_input_matches_spec_and_defaults_not_retroactive():
    req = W.build_create_transaction_rule(
        merchant_criteria=[{"operator": "contains", "value": "third federal"}],
        amount_criteria={"operator": "gt", "isExpense": False, "value": 100},
        set_category_action="c9")
    assert req.operation == "Common_CreateTransactionRuleMutationV2"
    assert req.variables == {"input": {
        "merchantCriteria": [{"operator": "contains", "value": "third federal"}],
        "amountCriteria": {"operator": "gt", "isExpense": False, "value": 100.0},
        "merchantCriteriaUseOriginalStatement": False,
        "setCategoryAction": "c9",
        "applyToExistingTransactions": False,
    }}
    gql(req.document)  # document parses


def test_create_rule_retroactive_only_on_literal_true():
    kw = dict(merchant_criteria=[{"operator": "eq", "value": "x"}],
              set_category_action="c1")
    assert W.build_create_transaction_rule("yes", **kw).variables["input"][
        "applyToExistingTransactions"] is False
    assert W.build_create_transaction_rule(True, **kw).variables["input"][
        "applyToExistingTransactions"] is True


def test_create_rule_between_uses_value_range():
    req = W.build_create_transaction_rule(
        amount_criteria={"operator": "between", "isExpense": True,
                         "valueRange": {"lower": 10, "upper": 20}},
        set_category_action="c1")
    assert req.variables["input"]["amountCriteria"] == {
        "operator": "between", "isExpense": True,
        "valueRange": {"lower": 10.0, "upper": 20.0}}


@pytest.mark.parametrize("kw", [
    {"set_category_action": "c1"},                                  # no criteria
    {"merchant_criteria": [{"operator": "eq", "value": "x"}]},      # no action
    {"merchant_criteria": [{"operator": "regex", "value": "x"}],
     "set_category_action": "c1"},                                  # bad op
    {"merchant_criteria": [{"operator": "eq", "value": " "}],
     "set_category_action": "c1"},                                  # empty value
    {"amount_criteria": {"operator": "gt", "isExpense": False},
     "set_category_action": "c1"},                                  # no value
])
def test_create_rule_rejects_bad_input(kw):
    with pytest.raises(ValueError):
        W.build_create_transaction_rule(**kw)


def test_create_rule_returns_id():
    mm = FakeMM({"Common_CreateTransactionRuleMutationV2": {
        "createTransactionRuleV2": {"errors": None,
                                    "transactionRule": {"id": "r42"}}}})
    rid = run(W.create_transaction_rule(
        mm, merchant_criteria=[{"operator": "eq", "value": "x"}],
        set_category_action="c1"))
    assert rid == "r42"


def test_create_rule_surfaces_field_errors():
    mm = FakeMM({"Common_CreateTransactionRuleMutationV2": {
        "createTransactionRuleV2": {"errors": {
            "message": None,
            "fieldErrors": [{"field": "setCategoryAction",
                             "messages": ["not found"]}]}}}})
    with pytest.raises(W.MonarchWriteError, match="setCategoryAction"):
        run(W.create_transaction_rule(
            mm, merchant_criteria=[{"operator": "eq", "value": "x"}],
            set_category_action="nope"))


def test_update_rule_resends_full_current_state_plus_change():
    mm = FakeMM({
        "GetTransactionRules": {"transactionRules": [EXISTING_RULE]},
        "Common_UpdateTransactionRuleMutationV2": {
            "updateTransactionRuleV2": {"errors": {"message": None,
                                                   "fieldErrors": []}}},
    })
    run(W.update_transaction_rule(mm, "r1", set_category_action="c10"))
    op, variables = mm.calls[-1]
    assert op == "Common_UpdateTransactionRuleMutationV2"
    assert variables["input"] == {
        "id": "r1",
        "merchantCriteriaUseOriginalStatement": False,
        "merchantCriteria": [{"operator": "contains", "value": "third federal"}],
        "amountCriteria": {"operator": "gt", "isExpense": False, "value": 100.0},
        "accountIds": ["a1"],
        "setCategoryAction": "c10",
        "sendNotificationAction": False,
        "applyToExistingTransactions": False,
    }


def test_update_refuses_rule_with_fields_it_cannot_round_trip():
    rule = dict(EXISTING_RULE, splitTransactionsAction={"amountType": "pct"})
    mm = FakeMM({"GetTransactionRules": {"transactionRules": [rule]}})
    with pytest.raises(W.MonarchWriteError, match="splitTransactionsAction"):
        run(W.update_transaction_rule(mm, "r1", set_category_action="c10"))
    assert [c[0] for c in mm.calls] == ["GetTransactionRules"]  # nothing sent


def test_update_unknown_rule_sends_nothing():
    mm = FakeMM({"GetTransactionRules": {"transactionRules": [EXISTING_RULE]}})
    with pytest.raises(W.MonarchWriteError, match="No transaction rule"):
        run(W.update_transaction_rule(mm, "missing", set_category_action="c"))
    assert len(mm.calls) == 1


def test_delete_rule_by_id():
    mm = FakeMM({"Common_DeleteTransactionRule": {
        "deleteTransactionRule": {"deleted": True, "errors": None}}})
    assert run(W.delete_transaction_rule(mm, "r1"))
    assert mm.calls == [("Common_DeleteTransactionRule", {"id": "r1"})]


def test_delete_rule_not_deleted_fails():
    mm = FakeMM({"Common_DeleteTransactionRule": {
        "deleteTransactionRule": {"deleted": False, "errors": None}}})
    with pytest.raises(W.MonarchWriteError):
        run(W.delete_transaction_rule(mm, "r1"))


def test_no_delete_all_or_reorder():
    names = set(W.WRITE_METHODS) | {n for n in dir(W) if not n.startswith("_")}
    assert not any("reorder" in n or "delete_all" in n for n in names)


# ---- 2. merchant rename --------------------------------------------------- #
def _merchants(variables):
    names = {"m1": "Third Federal Savings & Loan Association",
             "m2": "Third Federal Savings & Loan"}
    mid = variables["merchantId"]
    return {"merchant": {"id": mid, "name": names[mid]} if mid in names else None}


def test_rename_merchant_to_target_uses_exact_target_name():
    mm = FakeMM({"Common_GetEditMerchant": _merchants,
                 "Common_UpdateMerchant": lambda v: {"updateMerchant": {
                     "errors": None,
                     "merchant": {"id": v["input"]["merchantId"],
                                  "name": v["input"]["name"]}}}})
    m = run(W.rename_merchant(mm, "m1", target_merchant_id="m2"))
    assert m == {"id": "m1", "name": "Third Federal Savings & Loan"}
    assert mm.calls[-1] == ("Common_UpdateMerchant", {"input": {
        "merchantId": "m1", "name": "Third Federal Savings & Loan"}})


def test_rename_merchant_unknown_id_sends_no_write():
    mm = FakeMM({"Common_GetEditMerchant": _merchants})
    with pytest.raises(W.MonarchWriteError, match="No merchant"):
        run(W.rename_merchant(mm, "m1", target_merchant_id="zz"))
    assert all(op == "Common_GetEditMerchant" for op, _ in mm.calls)


@pytest.mark.parametrize("kw", [{}, {"new_name": "a", "target_merchant_id": "m2"},
                                {"target_merchant_id": "m1"}])
def test_rename_merchant_argument_rules(kw):
    with pytest.raises(ValueError):
        run(W.rename_merchant(FakeMM({"Common_GetEditMerchant": _merchants}),
                              "m1", **kw))


# ---- 3. recurring streams (build + mocked send only) ---------------------- #
def test_mark_not_recurring_request_shape():
    req = W.build_mark_stream_as_not_recurring("s1")
    assert (req.operation, req.variables) == ("Common_MarkAsNotRecurring",
                                              {"streamId": "s1"})
    gql(req.document)
    mm = FakeMM({"Common_MarkAsNotRecurring": {"markStreamAsNotRecurring": {
        "success": False, "errors": None}}})
    with pytest.raises(W.MonarchWriteError):
        run(W.mark_stream_as_not_recurring(mm, "s1"))


def test_review_stream_request_shape_and_status_validation():
    req = W.build_review_recurring_stream("s1", "ignored")
    assert req.variables == {"input": {"streamId": "s1",
                                       "reviewStatus": "ignored"}}
    gql(req.document)
    with pytest.raises(ValueError):
        W.build_review_recurring_stream("s1", "deleted")


# ---- 4. category rename ---------------------------------------------------- #
def test_update_category_rename():
    req = W.build_update_transaction_category("c1", name=" Mortgage ")
    assert (req.operation, req.variables) == (
        "Web_UpdateCategory", {"input": {"id": "c1", "name": "Mortgage"}})
    gql(req.document)
    with pytest.raises(ValueError):
        W.build_update_transaction_category("c1")
    mm = FakeMM({"Web_UpdateCategory": {"updateCategory": {
        "errors": [{"message": "Name taken", "code": "X"}]}}})
    with pytest.raises(W.MonarchWriteError, match="Name taken"):
        run(W.update_transaction_category(mm, "c1", name="Dup"))


# ---- gating --------------------------------------------------------------- #
def test_every_skill_write_and_receipt_upload_is_confirm_gated():
    assert set(W.WRITE_METHODS) <= CONFIRM_METHODS
    assert "upload_receipt_to_inbox" in CONFIRM_METHODS


@pytest.mark.parametrize("name", sorted(W.WRITE_METHODS))
def test_unarmed_skill_write_raises(name):
    with pytest.raises(ConfirmationRequiredError):
        getattr(GuardedMonarch(FakeMM()), name)


def test_armed_write_runs_against_raw_client():
    mm = FakeMM({"Common_DeleteTransactionRule": {
        "deleteTransactionRule": {"deleted": True, "errors": None}}})
    guard = GuardedMonarch(mm)
    with guard.allow("delete_transaction_rule"):
        assert run(guard.delete_transaction_rule("r1"))
    with pytest.raises(ConfirmationRequiredError):
        guard.delete_transaction_rule


def test_reads_are_ungated():
    mm = FakeMM({"GetTransactionRules": {"transactionRules": []}})
    assert run(GuardedMonarch(mm).get_transaction_rules()) == []


def test_raw_gql_mutation_is_gated_but_query_is_not():
    mm = FakeMM()
    guard = GuardedMonarch(mm)
    run(guard.gql_call("Q", gql("query Q { me { id } }")))
    with pytest.raises(ConfirmationRequiredError):
        guard.gql_call("M", gql("mutation M { x }"))
    with guard.allow("gql_call"):
        run(guard.gql_call("M", gql("mutation M { x }")))
    assert [c[0] for c in mm.calls] == ["Q", "M"]


def test_unarmed_receipt_upload_raises():
    with pytest.raises(ConfirmationRequiredError):
        GuardedMonarch(FakeMM()).upload_receipt_to_inbox


def test_verify_writes_mock_run(capsys):
    import importlib.util
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "verify_writes.py")
    spec = importlib.util.spec_from_file_location("verify_writes", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main([]) == 0
    assert "no test data left behind" in capsys.readouterr().out
