# Monarch API surface (`monarchmoneycommunity` 1.4.0)

## MCP Tool Reference (robcerda/monarch-mcp-server)

When the MCP is connected, use these tool names instead of fetch.py subcommands.
Output from each tool feeds into cache_writer.py (see SKILL.md Dual-Mode section).

| fetch.py subcommand | MCP tool | Key parameters |
|---------------------|----------|----------------|
| accounts | `get_accounts` | none |
| holdings | `get_account_holdings` | `account_id` |
| transactions | `get_transactions` | `start_date`, `end_date`, `limit` (use 500+) |
| budgets | `get_budgets` | `start_date`, `end_date` |
| cashflow | `get_cashflow` | `start_date`, `end_date` |
| recurring | `get_recurring_transactions` | none |
| net-worth | `get_net_worth` | `start_date`, `end_date` |
| (new) account-balance-history | `get_account_balance_history` | `account_id` |
| (new) spending-summary | `get_spending_summary` | `start_date`, `end_date`, `limit` |
| (new) review-queue | `get_transactions_needing_review` | `days`, `uncategorized`, `no_notes` |

## Auth tools
| Tool | Purpose |
|------|---------|
| `check_auth_status` | Verify MCP connection + session validity |
| `setup_authentication` | Get setup instructions if session missing |

## Official Monarch MCP note
The first-party Monarch MCP (monarchmoney.com) may use different tool names.
To probe: list available MCP tools and look for tools containing "accounts",
"transactions", or "monarch" in their names. Adapt the mapping accordingly.

---

The skill wraps the **`monarchmoney`** package (community fork, MIT). Every
method is **async** — call it with `asyncio.run(mm.method(...))`. The library
pins `gql==4.0`; do not override.

Authentication is a saved-session pickle (`mm.load_session(path)`); see
`setup_auth.py`. `fetch.py` only ever calls the **read** methods below.

**Write endpoints are gated in code.** `MonarchClient` wraps the library in a
`GuardedMonarch` proxy with two tiers:
- `create_manual_account` / `delete_account` → `BlockedEndpointError`, always.
  Cannot be armed; account lifecycle happens in the Monarch app only.
- Every other write (transaction mutations, budget writes, tags, splits,
  uploads, `update_account`, refresh requests) → `ConfirmationRequiredError`
  unless wrapped in `with client.mm.allow('<method>'):`. Open that block only
  after the user explicitly confirmed the specific action in chat.

---

## Read methods used by the skill

| Method | Key return fields | Used for |
|--------|-------------------|----------|
| `get_accounts()` | `accounts[]`: `id`, `displayName`, `type{name}`, `subtype{name}`, `currentBalance`, `displayBalance`, `institution{name}`, `isManual`, `isHidden`, `isAsset`, `updatedAt` | accounts, net-worth |
| `get_account_holdings(account_id)` | `holdings[]`: `ticker`, `name`, `quantity`, `totalValue`, `securityPriceChangeDollars`, `costBasis*` | holdings (per investment account) |
| `get_transactions(start_date, end_date, limit, offset, ...)` | `allTransactions.results[]`: `id`, `date`, `amount`, `merchant{name}`, `category{name, group{name}}`, `tags[]`, `notes`, `pending` | transactions (incremental) |
| `get_transactions_summary()` | aggregate income/expense totals | sanity totals |
| `get_budgets(start_date, end_date)` | `budgetData.monthlyAmountsByCategory[]`: `category{id,name,group}`, `monthlyAmounts[]{plannedCashFlowAmount, actualAmount}` | budgets |
| `get_cashflow(start_date, end_date)` | `byCategory[]`, `byCategoryGroup[]`, `byMonth[]` | cashflow detail |
| `get_cashflow_summary(start_date, end_date)` | `summary[].summary`: `sumIncome`, `sumExpense`, `savings`, `savingsRate` | cashflow summary |
| `get_recurring_transactions(start_date, end_date)` | `recurringTransactionItems[]`: `stream{name, frequency}`, `amount`, `date`, `account{id}` | recurring |
| `get_subscription_details()` | household / plan info | diagnostics |
| `get_account_history(account_id)` | daily balance series | balance trends |
| `get_recent_account_balances(start_date)` | recent per-account balances | quick refresh |
| `get_account_snapshots_by_type(timeframe)` | balances grouped by account type over time | asset-mix trend |
| `get_aggregate_snapshots(start_date, end_date, account_type)` | net-worth time series | net-worth history |
| `get_account_type_options()` | valid type/subtype enum | classification reference |
| `get_credit_history()` | credit-score history | credit tracking |
| `get_institutions()` | linked institutions + link status | connection health |
| `get_transaction_categories()` | category list w/ ids | category mapping |
| `get_transaction_category_groups()` | group list | grouping |
| `get_transaction_tags()` | tag list | tag filtering |
| `get_transaction_details(transaction_id)` | full single-tx detail | drill-down |
| `get_transaction_splits(transaction_id)` | split allocations | split-aware spend |
| `find_duplicate_transactions()` | candidate dup pairs | data hygiene |
| `is_accounts_refresh_complete()` | bool refresh status | post-refresh poll |
| `request_accounts_refresh_and_wait()` | triggers + waits for a provider refresh | force fresh balances |

## Skill-implemented endpoints (`scripts/monarch_writes.py`)

Not wrapped by the library; sent via `gql_call`. Documents match the Monarch
web app (as captured in github.com/thedavidweng/monarchmoney-cli
`queries/`). All writes are in `CONFIRM_METHODS`: call as `client.mm.<name>`
inside `with client.mm.allow("<name>"):`. Each write has a
`build_<name>(...)` twin returning `WriteRequest(operation, document,
variables)` without sending.

| Method | GraphQL operation | Variables | Returns |
|--------|-------------------|-----------|---------|
| `get_transaction_rules()` (read) | `GetTransactionRules` | — | `transactionRules[]` |
| `get_merchant(merchant_id)` (read) | `Common_GetEditMerchant` | `merchantId` | `{id, name, transactionCount, ruleCount, …}` |
| `create_transaction_rule(**rule, apply_to_existing_transactions=False)` | `Common_CreateTransactionRuleMutationV2` | `input: CreateTransactionRuleInput` | new rule id |
| `update_transaction_rule(rule_id, **changes, apply_to_existing_transactions=False)` | `Common_UpdateTransactionRuleMutationV2` | `input: UpdateTransactionRuleInput` (full current rule + changes + `id`) | rule id |
| `delete_transaction_rule(rule_id)` | `Common_DeleteTransactionRule` | `id` | `True` (success = empty `errors`; the `deleted` flag is unreliable and ignored) |
| `rename_merchant(merchant_id, new_name=None, target_merchant_id=None)` | `Common_UpdateMerchant` | `input: {merchantId, name}` | updated merchant |
| `mark_stream_as_not_recurring(stream_id)` | `Common_MarkAsNotRecurring` | `streamId` | `True` (checks `success`) |
| `review_recurring_stream(stream_id, review_status)` | `Web_ReviewStream` | `input: {streamId, reviewStatus}` | `{id, reviewStatus}` |
| `update_transaction_category(category_id, name, icon, budget_variability, exclude_from_budget)` | `Web_UpdateCategory` | `input: {id, …changed fields}` | updated category |

Rule input keys (keyword arg → input key): `merchant_criteria` →
`merchantCriteria` `[{operator: contains|eq, value}]`; `amount_criteria` →
`amountCriteria` `{operator: gt|eq|between, isExpense, value}` or
`{…, valueRange: {lower, upper}}` for `between`;
`merchant_criteria_use_original_statement`; `category_ids`; `account_ids`;
`set_category_action` (category id); `set_merchant_action` (merchant name);
`add_tags_action` (tag ids); `set_hide_from_reports_action`;
`review_status_action`; `send_notification_action`. A rule needs ≥1
criterion and ≥1 action.

**Error contract:** a mutation fails only if `errors.message` or
`errors.fieldErrors` is non-empty (`errors` may be null, an object, or a
list; an empty object is success) → `MonarchWriteError`.

**Not implemented on purpose:** rule delete-all, rule reorder.

`verify_writes.py` (repo root, not shipped) exercises every method against a
fake server by default; `--live` runs create→check→update→delete on a
throwaway rule (`zz-test-no-match-123`) and category, cleans up by recorded id,
and builds-but-never-sends the merchant/recurring writes.

\* `costBasis` is **not guaranteed** — Monarch omits it for many positions
(manual accounts, certain custodians). Treat as nullable. See "Lot-level
limitation".

### Quirks
- **Async everywhere** — forgetting `asyncio.run(...)` returns a coroutine.
- **Nested shapes** — `type`/`subtype`/`institution`/`category` arrive as
  objects (`{name: ...}`), not strings. `fetch.py`'s normalisers flatten them.
- **Pagination** — `get_transactions` caps at `limit` (default 100). For long
  windows, page with `offset`. The skill fetches incrementally
  (`last_tx_sync − 3 days → today`) so a single page usually suffices.
- **Schema drift** — a `KeyError`/GraphQL parse error means Monarch changed the
  schema; surface `SchemaChangedError` and `pip install -U monarchmoneycommunity`.
- **401 / LoginFailed** — the session is dead; never retry, re-run `setup_auth.py`.
- **Account types are live-vocabulary** — Monarch returns `brokerage`,
  `depository`, `real_estate`, `credit`, `loan`; there is no `investment`
  type in live data. Queries that filter investment accounts must use
  `type IN ('investment','brokerage')`.
- **`get_aggregate_snapshots` rejects `date` objects** (QA-verified): the
  library puts them in GraphQL variables unserialised →
  `Object of type date is not JSON serializable`. Pass ISO `"YYYY-MM-DD"`
  strings instead.
- **Budget category names are indirect** — `monthlyAmountsByCategory[].category`
  carries only `{id}`; names live under `categoryGroups[].categories[]`.
- **`basis = 0` means unknown, not zero** — custodians without basis data
  (employer-plan funds) report 0. The sync stores those as NULL.
- **No `delete_transaction_tag`** — `create_transaction_tag` exists but tags
  can only be deleted by hand in the Monarch UI. Don't create tags you can't
  clean up; warn the user before any tag creation.

---

## SQL query patterns (run against `data/monarch_cache.db`)

These are what Claude runs *after* a sync — the scripts only print summaries.

### 1. Net worth by account type
```sql
SELECT type,
       ROUND(SUM(balance), 2) AS total
FROM accounts
WHERE is_hidden = 0 AND is_closed = 0
GROUP BY type
ORDER BY total DESC;
```

### 2. Asset location by tax bucket
```sql
SELECT b.tax_bucket,
       ROUND(SUM(a.balance), 2) AS total,
       COUNT(*)                 AS n_accounts
FROM accounts a
JOIN account_tax_buckets b ON a.id = b.account_id
WHERE a.type IN ('investment','brokerage') AND a.is_hidden = 0
GROUP BY b.tax_bucket
ORDER BY total DESC;
```

### 3. Tax-loss-harvesting screen (positions currently at a loss)
```sql
SELECT h.account_id, h.ticker, h.name,
       h.value, h.cost_basis,
       ROUND(h.value - h.cost_basis, 2) AS unrealized_pl
FROM holdings h
WHERE h.cost_basis IS NOT NULL   -- unknown basis (incl. Monarch's 0) is NULL
  AND h.ticker IS NOT NULL       -- employer-plan funds have no public ticker
  AND h.value < h.cost_basis
ORDER BY unrealized_pl ASC;
-- NB: position-level only. See lot-level limitation before acting.
```

### 4. Concentration monitoring (single-ticker % of portfolio)
```sql
WITH port AS (SELECT SUM(value) AS tot FROM holdings)
SELECT ticker,
       ROUND(SUM(value), 2)                          AS value,
       ROUND(100.0 * SUM(value) / (SELECT tot FROM port), 1) AS pct_portfolio
FROM holdings
GROUP BY ticker
ORDER BY value DESC;
```

### 5. Spending by category, month over month
```sql
SELECT strftime('%Y-%m', date) AS month,
       category,
       ROUND(SUM(-amount), 2)   AS spend
FROM transactions
WHERE amount < 0
GROUP BY month, category
ORDER BY month DESC, spend DESC;
```

---

## Exclusion Management Queries

The `exclusions` table (schema v3) marks one-time / non-recurring expenses so
they drop out of ongoing analysis. It is USER CONFIG — it survives `clear-cache`
and is managed via `manage_exclusions.py` (CLI) or `MonarchCache`'s
add/remove/list methods. **Don't hand-filter transactions** — use
`cache.get_exclusion_filter_sql(scope)` (the single source of truth, used by
fetch.py and the downstream skills). The queries below are for inspection.

### E1. List all active exclusions
```sql
SELECT id, type, value, start_date, end_date, exclude_from,
       auto_suggested, reason
FROM exclusions
ORDER BY created_at DESC;
```

### E2. Count excluded transactions in a date range
```sql
SELECT COUNT(*) AS excluded_txns
FROM transactions t
WHERE t.date BETWEEN '2024-01-01' AND '2024-12-31'
  AND EXISTS (
    SELECT 1 FROM exclusions x
    WHERE (x.exclude_from = 'all' OR x.exclude_from = 'cashflow')
      AND (
        (x.type = 'transaction'     AND x.value = t.id) OR
        (x.type IN ('category','category_period') AND x.value = t.category
           AND t.date >= COALESCE(x.start_date, '0000-00-00')
           AND t.date <= COALESCE(x.end_date,   '9999-99-99')) OR
        (x.type = 'merchant'        AND x.value = t.merchant
           AND t.date >= COALESCE(x.start_date, '0000-00-00')
           AND t.date <= COALESCE(x.end_date,   '9999-99-99'))
      )
  );
```

### E3. Sum of excluded outflows for a month (how much was filtered)
```sql
SELECT ROUND(SUM(-t.amount), 2) AS excluded_spend
FROM transactions t
WHERE strftime('%Y-%m', t.date) = '2024-03'
  AND t.amount < 0
  AND EXISTS (
    SELECT 1 FROM exclusions x
    WHERE (x.exclude_from = 'all' OR x.exclude_from = 'cashflow')
      AND ((x.type = 'transaction' AND x.value = t.id)
        OR (x.type IN ('category','category_period') AND x.value = t.category
            AND t.date >= COALESCE(x.start_date, '0000-00-00')
            AND t.date <= COALESCE(x.end_date,   '9999-99-99'))
        OR (x.type = 'merchant' AND x.value = t.merchant
            AND t.date >= COALESCE(x.start_date, '0000-00-00')
            AND t.date <= COALESCE(x.end_date,   '9999-99-99')))
  );
```

### E4. Gross vs net expenses for a month (with / without exclusions)
```sql
SELECT
  ROUND(SUM(-t.amount), 2) AS gross_expense,
  ROUND(SUM(CASE WHEN NOT EXISTS (
      SELECT 1 FROM exclusions x
      WHERE (x.exclude_from = 'all' OR x.exclude_from = 'cashflow')
        AND ((x.type = 'transaction' AND x.value = t.id)
          OR (x.type IN ('category','category_period') AND x.value = t.category
              AND t.date >= COALESCE(x.start_date, '0000-00-00')
              AND t.date <= COALESCE(x.end_date,   '9999-99-99'))
          OR (x.type = 'merchant' AND x.value = t.merchant
              AND t.date >= COALESCE(x.start_date, '0000-00-00')
              AND t.date <= COALESCE(x.end_date,   '9999-99-99')))
    ) THEN -t.amount ELSE 0 END), 2) AS net_expense
FROM transactions t
WHERE strftime('%Y-%m', t.date) = '2024-03' AND t.amount < 0;
```

---

## Lot-level limitation (read before any TLH advice)

Monarch exposes **position-level holdings only**: one aggregated row per
`(account, ticker)` with a total `value`, `quantity`, and (sometimes) a single
blended `cost_basis`. It does **not** expose:

- per-**lot** cost basis or acquisition dates,
- short- vs long-term holding classification,
- wash-sale-relevant purchase history across accounts.

**Consequence:** the TLH screen (pattern 3) reliably identifies *which tickers*
are underwater, but the loss magnitude is approximate (blended basis) and
**wash-sale safety cannot be determined from Monarch data alone**. Precise
harvest amounts and wash-sale analysis require the brokerage's own lot-level
export (CSV). State this plainly whenever surfacing TLH candidates.
