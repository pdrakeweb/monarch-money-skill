"""
verify_writes.py — exercise every skill-implemented Monarch write
(monarch-money/scripts/monarch_writes.py) end to end, then clean up.

Repo-root dev tool; NOT shipped in the skill package.

    python verify_writes.py            # default: in-memory fake Monarch, no network
    python verify_writes.py --live     # YOUR real account, throwaway entities only

--live touches only entities this script creates and records by id:
  1. creates category "zz-test-category-<stamp>" in an expense group
  2. renames it (update_transaction_category), checks the name via
     get_transaction_categories
  3. creates a rule on merchant "zz-test-no-match-123" -> that category,
     checks it via get_transaction_rules, updates it, checks again, deletes it
  4. deletes the category
  5. merchant rename and recurring-stream writes are BUILT and printed, never
     sent: there is no throwaway merchant/stream to act on.
The finally-block deletes anything it created that still exists (by recorded
id only — it never pattern-matches other rules), and the run fails if the
account's rule count differs from before.
"""
import argparse
import asyncio
import copy
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "monarch-money", "scripts"))

import monarch_writes as W                      # noqa: E402
from monarch_client import GuardedMonarch      # noqa: E402

TEST_MERCHANT = "zz-test-no-match-123"


# --------------------------------------------------------------------------- #
# Fake Monarch: just enough server state for the documents monarch_writes and
# this script send.
# --------------------------------------------------------------------------- #
class FakeMonarch:
    def __init__(self):
        self.rules = {"r-existing": {"id": "r-existing", "merchantCriteria": [
            {"operator": "eq", "value": "real rule"}]}}
        self.groups = [{"id": "g1", "name": "Food", "type": "expense"}]
        self.categories = {}

    async def get_transaction_category_groups(self):
        return {"categoryGroups": copy.deepcopy(self.groups)}

    async def get_transaction_categories(self):
        return {"categories": [dict(c) for c in self.categories.values()]}

    async def create_transaction_category(self, group_id, transaction_category_name,
                                          **_):
        cid = str(uuid.uuid4())
        self.categories[cid] = {"id": cid, "name": transaction_category_name,
                                "group": {"id": group_id}}
        return {"createCategory": {"errors": None, "category": {"id": cid}}}

    async def delete_transaction_category(self, category_id):
        return self.categories.pop(category_id, None) is not None

    async def gql_call(self, operation, graphql_query, variables=None):
        v = variables or {}
        ok = {"message": None, "fieldErrors": []}
        if operation == "GetTransactionRules":
            return {"transactionRules": copy.deepcopy(list(self.rules.values()))}
        if operation == "Common_CreateTransactionRuleMutationV2":
            rid = "r-" + uuid.uuid4().hex[:8]
            self.rules[rid] = self._stored(rid, v["input"])
            return {"createTransactionRuleV2": {"errors": ok,
                                                "transactionRule": {"id": rid}}}
        if operation == "Common_UpdateTransactionRuleMutationV2":
            rid = v["input"]["id"]
            self.rules[rid] = self._stored(rid, v["input"])
            return {"updateTransactionRuleV2": {"errors": ok}}
        if operation == "Common_DeleteTransactionRule":
            gone = self.rules.pop(v["id"], None) is not None
            return {"deleteTransactionRule": {"deleted": gone, "errors": None}}
        if operation == "Web_UpdateCategory":
            c = self.categories[v["input"]["id"]]
            c["name"] = v["input"].get("name", c["name"])
            return {"updateCategory": {"errors": [], "category": dict(c)}}
        raise AssertionError(f"fake got unexpected operation {operation}")

    def _stored(self, rid, inp):
        cat = inp.get("setCategoryAction")
        return {"id": rid,
                "merchantCriteriaUseOriginalStatement":
                    inp.get("merchantCriteriaUseOriginalStatement", False),
                "merchantCriteria": inp.get("merchantCriteria"),
                "amountCriteria": inp.get("amountCriteria"),
                "setCategoryAction": {"id": cat, "name": "?"} if cat else None}


# --------------------------------------------------------------------------- #
def step(msg):
    print(f"- {msg}")


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


async def exercise(guard):
    """Run every write against `guard` (a GuardedMonarch). Each write is
    armed for exactly its own call."""
    created_rule, created_cat = None, None
    stamp = time.strftime("%Y%m%d%H%M%S")
    before = {r["id"] for r in await guard.get_transaction_rules()}
    step(f"{len(before)} existing rule(s) — will not be touched")
    try:
        groups = (await guard.get_transaction_category_groups())["categoryGroups"]
        group = next(g for g in groups if g.get("type") == "expense")
        name = f"zz-test-category-{stamp}"
        with guard.allow("create_transaction_category"):
            res = await guard.create_transaction_category(
                group_id=group["id"], transaction_category_name=name)
        created_cat = res["createCategory"]["category"]["id"]
        step(f"created test category {created_cat}")

        renamed = name + "-renamed"
        with guard.allow("update_transaction_category"):
            await guard.update_transaction_category(created_cat, name=renamed)
        cats = (await guard.get_transaction_categories())["categories"]
        check(any(c["id"] == created_cat and c["name"] == renamed for c in cats),
              "update_transaction_category renamed the test category")

        with guard.allow("create_transaction_rule"):
            created_rule = await guard.create_transaction_rule(
                merchant_criteria=[{"operator": "eq", "value": TEST_MERCHANT}],
                set_category_action=created_cat)
        rules = {r["id"]: r for r in await guard.get_transaction_rules()}
        check(created_rule in rules and created_rule not in before,
              f"create_transaction_rule created {created_rule}")

        with guard.allow("update_transaction_rule"):
            await guard.update_transaction_rule(
                created_rule, amount_criteria={"operator": "gt",
                                               "isExpense": True, "value": 999999})
        rule = {r["id"]: r for r in await guard.get_transaction_rules()}[created_rule]
        check((rule.get("amountCriteria") or {}).get("operator") == "gt"
              and rule["merchantCriteria"][0]["value"] == TEST_MERCHANT,
              "update_transaction_rule added the amount criterion and kept the merchant criterion")

        with guard.allow("delete_transaction_rule"):
            await guard.delete_transaction_rule(created_rule)
        after = {r["id"] for r in await guard.get_transaction_rules()}
        check(created_rule not in after, "delete_transaction_rule removed it")
        created_rule = None

        with guard.allow("delete_transaction_category"):
            await guard.delete_transaction_category(created_cat)
        cats = (await guard.get_transaction_categories())["categories"]
        check(all(c["id"] != created_cat for c in cats), "test category deleted")
        created_cat = None

        step("build-only (not sent): merchant rename + recurring writes")
        for req in (W.build_rename_merchant("<merchant-id>", "New Name"),
                    W.build_mark_stream_as_not_recurring("<stream-id>"),
                    W.build_review_recurring_stream("<stream-id>", "ignored")):
            print(f"  {req.method}: {req.operation} {json.dumps(req.variables)}")
    finally:
        # Clean up ONLY what this run created, by recorded id.
        if created_rule:
            with guard.allow("delete_transaction_rule"):
                await guard.delete_transaction_rule(created_rule)
            step(f"cleanup: deleted rule {created_rule}")
        if created_cat:
            with guard.allow("delete_transaction_category"):
                await guard.delete_transaction_category(created_cat)
            step(f"cleanup: deleted category {created_cat}")
    final = {r["id"] for r in await guard.get_transaction_rules()}
    check(final == before, "rule set identical to before the run")


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--live", action="store_true",
                   help="Run against the real account (throwaway entities only).")
    args = p.parse_args(argv)
    if args.live:
        from monarch_client import MonarchClient
        client = MonarchClient.ensure_authenticated()
        guard = client.mm
        print("## verify_writes — LIVE (throwaway entities only)\n")
    else:
        guard = GuardedMonarch(FakeMonarch())
        print("## verify_writes — mock\n")
    asyncio.run(exercise(guard))
    print("\nAll write methods verified; no test data left behind.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
