"""
monarch_writes — Monarch write endpoints the installed `monarchmoneycommunity`
library does not wrap: transaction rules, merchant rename, recurring-stream
management, and category rename. Each is a direct GraphQL call through the
library's `gql_call`.

These are NOT called directly. GuardedMonarch (monarch_client.py) exposes each
write by name and gates it like any library write, so the only way to send one
is inside `with client.mm.allow("<method>"):` after the user confirmed that
specific action:

    with client.mm.allow("create_transaction_rule"):
        rule_id = asyncio.run(client.mm.create_transaction_rule(...))

Every write is split in two: `build_<method>(...)` returns a WriteRequest
(operation, document, variables) WITHOUT touching the network, and the async
`<method>(mm, ...)` sends it. The build step is what tests and
verify_writes.py inspect, and what Claude can show the user before asking.

Operation documents come from the Monarch web app, as captured by
github.com/thedavidweng/monarchmoney-cli (queries/*.graphql).

Safety conventions (keep them):
  * Retroactive flags (applyToExistingTransactions) default False.
  * A payload fails only when errors.message or errors.fieldErrors is
    non-empty; a present-but-empty errors object is success.
  * update_transaction_rule re-sends the rule's FULL current input with the
    changes applied, and refuses rules using fields it cannot round-trip, so
    an update can never silently clear criteria or actions.
  * Merchants are addressed by explicit id only; nothing here fuzzy-matches.
  * No delete-all and no reorder: those are deliberately not implemented.
"""
from dataclasses import dataclass, field

from skill_common import SkillError


class MonarchWriteError(SkillError):
    """Monarch rejected a write (non-empty errors in the mutation payload)."""


@dataclass
class WriteRequest:
    method: str
    operation: str
    document: str
    variables: dict = field(default_factory=dict)
    payload_key: str = ""


def _gql(document):
    from gql import gql  # lazy: keeps build_* importable without the library
    return gql(document)


def check_errors(errors, what):
    """Raise MonarchWriteError iff `errors` carries a message or fieldErrors.
    Accepts None, a PayloadError dict, or a list of them."""
    items = errors if isinstance(errors, list) else [errors]
    problems = []
    for e in items:
        if not isinstance(e, dict):
            continue
        if e.get("message"):
            problems.append(str(e["message"]))
        for fe in e.get("fieldErrors") or []:
            msgs = "; ".join(fe.get("messages") or []) or "invalid"
            problems.append(f"{fe.get('field')}: {msgs}")
    if problems:
        raise MonarchWriteError(f"Monarch rejected {what}: " + " | ".join(problems))


async def send(mm, req):
    """Send a built WriteRequest via the raw library client; return the
    mutation's payload object after the errors check."""
    result = await mm.gql_call(operation=req.operation,
                               graphql_query=_gql(req.document),
                               variables=req.variables)
    payload = (result or {}).get(req.payload_key) or {}
    check_errors(payload.get("errors"), req.method)
    return payload


# --------------------------------------------------------------------------- #
# GraphQL documents
# --------------------------------------------------------------------------- #
_PAYLOAD_ERRORS = "errors { fieldErrors { field messages } message code }"

CREATE_RULE = f"""
mutation Common_CreateTransactionRuleMutationV2($input: CreateTransactionRuleInput!) {{
  createTransactionRuleV2(input: $input) {{
    {_PAYLOAD_ERRORS}
    transactionRule {{ id }}
  }}
}}"""

UPDATE_RULE = f"""
mutation Common_UpdateTransactionRuleMutationV2($input: UpdateTransactionRuleInput!) {{
  updateTransactionRuleV2(input: $input) {{
    {_PAYLOAD_ERRORS}
  }}
}}"""

DELETE_RULE = """
mutation Common_DeleteTransactionRule($id: ID!) {
  deleteTransactionRule(id: $id) {
    deleted
    errors { message code }
  }
}"""

GET_RULES = """
query GetTransactionRules {
  transactionRules {
    id
    order
    merchantCriteriaUseOriginalStatement
    merchantCriteria { operator value }
    merchantNameCriteria { operator value }
    originalStatementCriteria { operator value }
    amountCriteria { operator isExpense value valueRange { lower upper } }
    categoryIds
    accountIds
    criteriaOwnerIsJoint
    criteriaOwnerUserIds
    criteriaBusinessEntityIds
    criteriaBusinessEntityIsUnassigned
    setCategoryAction { id name }
    setMerchantAction { id name }
    addTagsAction { id name }
    linkGoalAction { id }
    linkSavingsGoalAction { id }
    needsReviewByUserAction { id }
    unassignNeedsReviewByUserAction
    sendNotificationAction
    setHideFromReportsAction
    setLinkToPaydownBudgetAction
    reviewStatusAction
    actionSetOwnerIsJoint
    actionSetOwner { id }
    actionSetBusinessEntity { id }
    actionSetBusinessEntityIsUnassigned
    splitTransactionsAction { amountType }
    recentApplicationCount
    lastAppliedAt
  }
}"""

GET_MERCHANT = """
query Common_GetEditMerchant($merchantId: ID!) {
  merchant(id: $merchantId) {
    id
    name
    transactionCount
    ruleCount
    canBeDeleted
    recurringTransactionStream { id }
  }
}"""

UPDATE_MERCHANT = f"""
mutation Common_UpdateMerchant($input: UpdateMerchantInput!) {{
  updateMerchant(input: $input) {{
    merchant {{ id name transactionCount ruleCount }}
    {_PAYLOAD_ERRORS}
  }}
}}"""

MARK_NOT_RECURRING = f"""
mutation Common_MarkAsNotRecurring($streamId: ID!) {{
  markStreamAsNotRecurring(streamId: $streamId) {{
    success
    {_PAYLOAD_ERRORS}
  }}
}}"""

REVIEW_STREAM = """
mutation Web_ReviewStream($input: ReviewRecurringStreamInput!) {
  reviewRecurringStream(input: $input) {
    stream { id reviewStatus }
    errors { message code }
  }
}"""

UPDATE_CATEGORY = """
mutation Web_UpdateCategory($input: UpdateCategoryInput!) {
  updateCategory(input: $input) {
    errors { message code }
    category { id name icon budgetVariability excludeFromBudget group { id type } }
  }
}"""


# --------------------------------------------------------------------------- #
# Reads (ungated; used to validate writes)
# --------------------------------------------------------------------------- #
async def get_transaction_rules(mm):
    result = await mm.gql_call(operation="GetTransactionRules",
                               graphql_query=_gql(GET_RULES), variables={})
    return (result or {}).get("transactionRules") or []


async def get_merchant(mm, merchant_id):
    result = await mm.gql_call(operation="Common_GetEditMerchant",
                               graphql_query=_gql(GET_MERCHANT),
                               variables={"merchantId": merchant_id})
    merchant = (result or {}).get("merchant")
    if not merchant:
        raise MonarchWriteError(f"No merchant with id {merchant_id!r}.")
    return merchant


# --------------------------------------------------------------------------- #
# 1. Transaction rules
# --------------------------------------------------------------------------- #
MERCHANT_OPERATORS = ("contains", "eq")
AMOUNT_OPERATORS = ("gt", "eq", "between")


def _check_merchant_criteria(criteria):
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("merchant_criteria must be a non-empty list.")
    for c in criteria:
        if c.get("operator") not in MERCHANT_OPERATORS:
            raise ValueError(f"merchant operator must be one of {MERCHANT_OPERATORS}.")
        if not str(c.get("value") or "").strip():
            raise ValueError("merchant criterion needs a non-empty value.")
    return [{"operator": c["operator"], "value": c["value"]} for c in criteria]


def _check_amount_criteria(amount):
    op = amount.get("operator")
    if op not in AMOUNT_OPERATORS:
        raise ValueError(f"amount operator must be one of {AMOUNT_OPERATORS}.")
    if not isinstance(amount.get("isExpense"), bool):
        raise ValueError("amount_criteria.isExpense must be true or false.")
    out = {"operator": op, "isExpense": amount["isExpense"]}
    rng = amount.get("valueRange")
    if op == "between":
        if not rng or rng.get("lower") is None or rng.get("upper") is None:
            raise ValueError("'between' needs valueRange {lower, upper}.")
        out["valueRange"] = {"lower": float(rng["lower"]),
                             "upper": float(rng["upper"])}
    else:
        if amount.get("value") is None:
            raise ValueError(f"'{op}' needs a value.")
        out["value"] = float(amount["value"])
    return out


def _rule_input(merchant_criteria=None, amount_criteria=None,
                merchant_criteria_use_original_statement=None,
                category_ids=None, account_ids=None,
                set_category_action=None, set_merchant_action=None,
                add_tags_action=None, set_hide_from_reports_action=None,
                review_status_action=None, send_notification_action=None):
    """Map keyword arguments to CreateTransactionRuleInput keys. Only
    arguments that were given are included."""
    inp = {}
    if merchant_criteria is not None:
        inp["merchantCriteria"] = _check_merchant_criteria(merchant_criteria)
    if amount_criteria is not None:
        inp["amountCriteria"] = _check_amount_criteria(amount_criteria)
    if merchant_criteria_use_original_statement is not None:
        inp["merchantCriteriaUseOriginalStatement"] = bool(
            merchant_criteria_use_original_statement)
    if category_ids is not None:
        inp["categoryIds"] = list(category_ids)
    if account_ids is not None:
        inp["accountIds"] = list(account_ids)
    if set_category_action is not None:
        inp["setCategoryAction"] = str(set_category_action)
    if set_merchant_action is not None:
        inp["setMerchantAction"] = str(set_merchant_action)
    if add_tags_action is not None:
        inp["addTagsAction"] = list(add_tags_action)
    if set_hide_from_reports_action is not None:
        inp["setHideFromReportsAction"] = bool(set_hide_from_reports_action)
    if review_status_action is not None:
        inp["reviewStatusAction"] = review_status_action
    if send_notification_action is not None:
        inp["sendNotificationAction"] = bool(send_notification_action)
    return inp


_CRITERIA_KEYS = ("merchantCriteria", "amountCriteria", "categoryIds",
                  "accountIds")
_ACTION_KEYS = ("setCategoryAction", "setMerchantAction", "addTagsAction",
                "setHideFromReportsAction", "reviewStatusAction",
                "sendNotificationAction")


def build_create_transaction_rule(apply_to_existing_transactions=False,
                                  **rule):
    """rule: keyword args of _rule_input (merchant_criteria=[{operator,value}],
    amount_criteria={operator,isExpense,value|valueRange},
    set_category_action="<category id>", ...). Needs at least one criterion
    and one action. apply_to_existing_transactions defaults False
    (retroactive — explicit opt-in only)."""
    inp = _rule_input(**rule)
    if not any(k in inp for k in _CRITERIA_KEYS):
        raise ValueError("A rule needs at least one criterion.")
    if not any(k in inp for k in _ACTION_KEYS):
        raise ValueError("A rule needs at least one action.")
    inp.setdefault("merchantCriteriaUseOriginalStatement", False)
    inp["applyToExistingTransactions"] = apply_to_existing_transactions is True
    return WriteRequest("create_transaction_rule",
                        "Common_CreateTransactionRuleMutationV2", CREATE_RULE,
                        {"input": inp}, "createTransactionRuleV2")


async def create_transaction_rule(mm, apply_to_existing_transactions=False,
                                  **rule):
    """Create a rule; returns the new rule id (None if Monarch omits it)."""
    payload = await send(mm, build_create_transaction_rule(
        apply_to_existing_transactions, **rule))
    return (payload.get("transactionRule") or {}).get("id")


# Rule fields update_transaction_rule cannot round-trip. A rule using any of
# them is refused (edit it in the Monarch app) rather than risking an update
# that silently drops the field.
_UNSUPPORTED_RULE_FIELDS = (
    "merchantNameCriteria", "originalStatementCriteria",
    "criteriaOwnerIsJoint", "criteriaOwnerUserIds",
    "criteriaBusinessEntityIds", "criteriaBusinessEntityIsUnassigned",
    "linkGoalAction", "linkSavingsGoalAction", "needsReviewByUserAction",
    "unassignNeedsReviewByUserAction", "setLinkToPaydownBudgetAction",
    "actionSetOwnerIsJoint", "actionSetOwner", "actionSetBusinessEntity",
    "actionSetBusinessEntityIsUnassigned", "splitTransactionsAction",
)


def rule_to_input(rule):
    """Convert a GetTransactionRules rule into UpdateTransactionRuleInput
    keys (the full current state). Raises MonarchWriteError if the rule uses
    a field this module cannot faithfully re-send."""
    used = [f for f in _UNSUPPORTED_RULE_FIELDS if rule.get(f)]
    if used:
        raise MonarchWriteError(
            f"Rule {rule.get('id')} uses {', '.join(used)}, which this skill "
            "cannot round-trip; edit it in the Monarch app instead.")
    inp = {"merchantCriteriaUseOriginalStatement":
           bool(rule.get("merchantCriteriaUseOriginalStatement"))}
    if rule.get("merchantCriteria"):
        inp["merchantCriteria"] = [
            {"operator": c["operator"], "value": c["value"]}
            for c in rule["merchantCriteria"]]
    amt = rule.get("amountCriteria")
    if amt:
        a = {"operator": amt.get("operator"), "isExpense": amt.get("isExpense")}
        if amt.get("valueRange"):
            a["valueRange"] = {"lower": amt["valueRange"].get("lower"),
                               "upper": amt["valueRange"].get("upper")}
        else:
            a["value"] = amt.get("value")
        inp["amountCriteria"] = a
    for key in ("categoryIds", "accountIds"):
        if rule.get(key):
            inp[key] = list(rule[key])
    if rule.get("setCategoryAction"):
        inp["setCategoryAction"] = rule["setCategoryAction"]["id"]
    if rule.get("setMerchantAction"):
        # The input takes the merchant NAME to set (read shape is {id, name}).
        inp["setMerchantAction"] = rule["setMerchantAction"]["name"]
    if rule.get("addTagsAction"):
        inp["addTagsAction"] = [t["id"] for t in rule["addTagsAction"]]
    for key in ("setHideFromReportsAction", "reviewStatusAction",
                "sendNotificationAction"):
        if rule.get(key) is not None:
            inp[key] = rule[key]
    return inp


def build_update_transaction_rule(current_rule, apply_to_existing_transactions=False,
                                  **changes):
    """`current_rule` is the rule as returned by get_transaction_rules();
    `changes` are _rule_input keyword args. The request carries the full
    current input with `changes` applied on top."""
    if not changes:
        raise ValueError("No changes given.")
    inp = rule_to_input(current_rule)
    inp.update(_rule_input(**changes))
    if not any(k in inp for k in _CRITERIA_KEYS):
        raise ValueError("The updated rule would have no criteria.")
    if not any(k in inp for k in _ACTION_KEYS):
        raise ValueError("The updated rule would have no actions.")
    inp["id"] = current_rule["id"]
    inp["applyToExistingTransactions"] = apply_to_existing_transactions is True
    return WriteRequest("update_transaction_rule",
                        "Common_UpdateTransactionRuleMutationV2", UPDATE_RULE,
                        {"input": inp}, "updateTransactionRuleV2")


async def update_transaction_rule(mm, rule_id,
                                  apply_to_existing_transactions=False,
                                  **changes):
    """Fetch rule `rule_id`, apply `changes`, send the full input."""
    rules = await get_transaction_rules(mm)
    current = next((r for r in rules if r.get("id") == rule_id), None)
    if current is None:
        raise MonarchWriteError(f"No transaction rule with id {rule_id!r}.")
    await send(mm, build_update_transaction_rule(
        current, apply_to_existing_transactions, **changes))
    return rule_id


def build_delete_transaction_rule(rule_id):
    if not rule_id:
        raise ValueError("rule_id is required.")
    return WriteRequest("delete_transaction_rule",
                        "Common_DeleteTransactionRule", DELETE_RULE,
                        {"id": rule_id}, "deleteTransactionRule")


async def delete_transaction_rule(mm, rule_id):
    """Delete ONE rule by id. There is deliberately no delete-all.

    Success is judged by the errors payload alone (send() raises on a
    non-empty one). The `deleted` flag is unreliable: a live delete returned
    deleted=False while the rule was in fact gone."""
    await send(mm, build_delete_transaction_rule(rule_id))
    return True


# --------------------------------------------------------------------------- #
# 2. Merchant rename
# --------------------------------------------------------------------------- #
def build_rename_merchant(merchant_id, new_name):
    if not merchant_id:
        raise ValueError("merchant_id is required.")
    if not str(new_name or "").strip():
        raise ValueError("new_name must be non-empty.")
    return WriteRequest("rename_merchant", "Common_UpdateMerchant",
                        UPDATE_MERCHANT,
                        {"input": {"merchantId": merchant_id,
                                   "name": new_name.strip()}},
                        "updateMerchant")


async def rename_merchant(mm, merchant_id, new_name=None,
                          target_merchant_id=None):
    """Rename merchant `merchant_id` to `new_name`, OR to the exact current
    name of `target_merchant_id` (to fold a duplicate into it). Exactly one of
    new_name / target_merchant_id. Both merchants are looked up by id first;
    nothing is fuzzy-matched. Returns the updated merchant."""
    if (new_name is None) == (target_merchant_id is None):
        raise ValueError("Pass exactly one of new_name or target_merchant_id.")
    if target_merchant_id == merchant_id:
        raise ValueError("target_merchant_id is the merchant being renamed.")
    await get_merchant(mm, merchant_id)  # must exist
    if target_merchant_id is not None:
        new_name = (await get_merchant(mm, target_merchant_id))["name"]
    payload = await send(mm, build_rename_merchant(merchant_id, new_name))
    return payload.get("merchant")


# --------------------------------------------------------------------------- #
# 3. Recurring streams
# --------------------------------------------------------------------------- #
REVIEW_STATUSES = ("approved", "ignored", "pending")


def build_mark_stream_as_not_recurring(stream_id):
    if not stream_id:
        raise ValueError("stream_id is required.")
    return WriteRequest("mark_stream_as_not_recurring",
                        "Common_MarkAsNotRecurring", MARK_NOT_RECURRING,
                        {"streamId": stream_id}, "markStreamAsNotRecurring")


async def mark_stream_as_not_recurring(mm, stream_id):
    payload = await send(mm, build_mark_stream_as_not_recurring(stream_id))
    if not payload.get("success"):
        raise MonarchWriteError(
            f"Monarch did not mark stream {stream_id} as not recurring.")
    return True


def build_review_recurring_stream(stream_id, review_status):
    if not stream_id:
        raise ValueError("stream_id is required.")
    if review_status not in REVIEW_STATUSES:
        raise ValueError(f"review_status must be one of {REVIEW_STATUSES}.")
    return WriteRequest("review_recurring_stream", "Web_ReviewStream",
                        REVIEW_STREAM,
                        {"input": {"streamId": stream_id,
                                   "reviewStatus": review_status}},
                        "reviewRecurringStream")


async def review_recurring_stream(mm, stream_id, review_status):
    payload = await send(mm, build_review_recurring_stream(stream_id,
                                                           review_status))
    return payload.get("stream")


# --------------------------------------------------------------------------- #
# 4. Category rename / update
# --------------------------------------------------------------------------- #
def build_update_transaction_category(category_id, name=None, icon=None,
                                      budget_variability=None,
                                      exclude_from_budget=None):
    if not category_id:
        raise ValueError("category_id is required.")
    inp = {"id": category_id}
    if name is not None:
        if not str(name).strip():
            raise ValueError("name must be non-empty.")
        inp["name"] = name.strip()
    if icon is not None:
        inp["icon"] = icon
    if budget_variability is not None:
        inp["budgetVariability"] = budget_variability
    if exclude_from_budget is not None:
        inp["excludeFromBudget"] = bool(exclude_from_budget)
    if len(inp) == 1:
        raise ValueError("No changes given.")
    return WriteRequest("update_transaction_category", "Web_UpdateCategory",
                        UPDATE_CATEGORY, {"input": inp}, "updateCategory")


async def update_transaction_category(mm, category_id, **changes):
    payload = await send(mm, build_update_transaction_category(category_id,
                                                               **changes))
    return payload.get("category")


# Gated writes GuardedMonarch exposes by name (each is also in
# CONFIRM_METHODS). Reads are exposed ungated.
WRITE_METHODS = {
    "create_transaction_rule": create_transaction_rule,
    "update_transaction_rule": update_transaction_rule,
    "delete_transaction_rule": delete_transaction_rule,
    "rename_merchant": rename_merchant,
    "mark_stream_as_not_recurring": mark_stream_as_not_recurring,
    "review_recurring_stream": review_recurring_stream,
    "update_transaction_category": update_transaction_category,
}
READ_METHODS = {
    "get_transaction_rules": get_transaction_rules,
    "get_merchant": get_merchant,
}
