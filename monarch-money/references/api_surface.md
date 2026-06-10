# Monarch API surface (`monarchmoneycommunity` 1.3.2)

The skill wraps the **`monarchmoney`** package (community fork, MIT). Every
method is **async** — call it with `asyncio.run(mm.method(...))`. The library
pins `gql==4.0`; do not override.

Authentication is a saved-session pickle (`mm.load_session(path)`); see
`setup_auth.py`. `fetch.py` only ever calls the **read** methods below.

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
WHERE a.type = 'investment' AND a.is_hidden = 0
GROUP BY b.tax_bucket
ORDER BY total DESC;
```

### 3. Tax-loss-harvesting screen (positions currently at a loss)
```sql
SELECT h.account_id, h.ticker, h.name,
       h.value, h.cost_basis,
       ROUND(h.value - h.cost_basis, 2) AS unrealized_pl
FROM holdings h
WHERE h.cost_basis IS NOT NULL
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
