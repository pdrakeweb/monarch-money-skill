---
name: "monarch-money"
description: >
  Access and analyze the user's Monarch Money financial data: accounts, balances,
  net worth, investment holdings, transactions, budgets, and cashflow. Use this
  skill whenever the user asks about their finances, spending, portfolio, net worth,
  account balances, transactions, budget, savings rate, investment allocation,
  tax-loss harvesting opportunities, subscription costs, or anything requiring
  their actual financial data — even if they don't mention Monarch by name.
---

# Monarch Money

Surfaces the user's real Monarch Money data for conversational analysis. Scripts
sync data into a local SQLite cache and print compact Markdown summaries. **For
any real analysis, query the SQLite cache directly** (`data/monarch_cache.db`)
with SQL/pandas — don't ask the scripts to dump raw rows.

All commands run from the skill directory:
`python scripts/fetch.py <command> [flags]`

## 1. Decision tree

```
User asks a finance question
        │
        ▼
  Is there a valid session?  ──no──▶  python scripts/fetch.py auth-status
        │ yes                         (missing/old → run §2 first-run setup)
        ▼
  Need fresh data?  ──▶  python scripts/fetch.py sync-all      (respects TTL)
        │                python scripts/fetch.py sync-all --refresh  (force)
        ▼
  Analysis beyond the summary?  ──▶  query data/monarch_cache.db directly
                                     (see references/api_surface.md for SQL)
```

A single-domain question (just balances, just this month's budget) can use the
matching subcommand instead of `sync-all`. Caches are TTL-guarded, so re-running
is cheap and won't re-hit the API unless stale or `--refresh` is passed.

## 2. First-run setup (Option A — recommended)

Session-only auth. The password is **never** written to disk; re-run with a
fresh TOTP code when the session expires (typically weeks–months).

```
python scripts/setup_auth.py --email you@example.com \
    --password 'YOUR_PASSWORD' --mfa-code 123456
```

This logs in and saves `data/session.pickle` only.

**Option B (zero-touch, opt-in — confirm with the user first):** stores email +
password + TOTP secret in `data/credentials.json` so the skill can silently
re-authenticate. Only use this if the user explicitly asks for unattended reauth:

```
python scripts/setup_auth.py --email you@example.com \
    --password 'YOUR_PASSWORD' --mfa-secret-key BASE32SECRET --persist-credentials
```

## 3. Command reference

| Command | What it does | Notable flags |
|---------|--------------|---------------|
| `accounts` | Account table + net worth + tax-bucket warnings | `--refresh` |
| `holdings` | Positions, values, unrealized P/L, top-5 | `--refresh` |
| `transactions` | Window summary + top merchants | `--days N`, `--account ID`, `--category C` |
| `budgets` | Budget vs actual; flags overages | `--month YYYY-MM` |
| `cashflow` | Income / expense / savings rate | `--month`, `--months N` |
| `recurring` | Subscriptions + monthly run-rate | `--refresh` |
| `net-worth` | Net worth from cache (no API call) | — |
| `sync-all` | Run every sync in order | `--refresh` |
| `status` | Cache row counts, ages, lifetime API calls | — |
| `auth-status` | Session age + expiry estimate | — |

Global: `--refresh` bypasses TTL; `--mock` loads bundled fixtures (offline demo).

## 4. Worked examples

### Net worth snapshot
```
python scripts/fetch.py accounts          # syncs + prints table & net worth
```
For a breakdown by type without an API call: `python scripts/fetch.py net-worth`.

### Asset location by tax bucket
```
python scripts/fetch.py accounts          # ensures tax buckets are populated
```
Then query the cache:
```sql
SELECT b.tax_bucket, ROUND(SUM(a.balance),2) total
FROM accounts a JOIN account_tax_buckets b ON a.id=b.account_id
WHERE a.type='investment' AND a.is_hidden=0
GROUP BY b.tax_bucket ORDER BY total DESC;
```

### Tax-loss-harvesting screen
```
python scripts/fetch.py holdings
```
```sql
SELECT account_id, ticker, value, cost_basis,
       ROUND(value-cost_basis,2) AS unrealized_pl
FROM holdings
WHERE cost_basis IS NOT NULL AND value < cost_basis
ORDER BY unrealized_pl ASC;
```
⚠ **Always state the lot-level caveat** (§6) when surfacing TLH candidates.

### Monthly cashflow, 3-month rolling
```
python scripts/fetch.py cashflow --months 3
```

## 5. Correcting tax buckets

Buckets are auto-classified from each account's Monarch subtype on every sync
(`roth`, `traditional`, `hsa`, `taxable`, `other`). Auto rows never overwrite a
user correction. To correct one, set `auto_classified = 0`:

```sql
UPDATE account_tax_buckets
SET tax_bucket = 'roth', auto_classified = 0
WHERE account_id = 'acc_123';
```

List anything still ambiguous:
```sql
SELECT a.id, a.name, a.subtype, b.tax_bucket
FROM accounts a JOIN account_tax_buckets b ON a.id=b.account_id
WHERE a.type='investment' AND b.tax_bucket='other';
```
The `accounts` command prints a ⚠ warning when any investment account is still
`other`.

## 6. Lot-level limitation (be honest about this)

Monarch returns **position-level holdings only** — one blended row per
`(account, ticker)`. There is **no per-lot cost basis, no acquisition dates, and
no short/long-term split**. So:

- The TLH screen shows *which tickers* are underwater — reliable.
- The loss *amount* is approximate (blended basis), and **wash-sale safety
  cannot be judged from Monarch data alone**.
- Precise harvest sizing and wash-sale checks need the brokerage's lot-level CSV.

Say this plainly; don't imply lot-level precision the data can't support.

## 7. Error handling

| Symptom (error JSON on stdout / stderr) | Meaning | Fix |
|------|---------|-----|
| `AuthExpiredError` | Session dead / 401 | Re-run `setup_auth.py` with a fresh `--mfa-code` |
| `SchemaChangedError` | Monarch GraphQL changed | `pip install -U monarchmoneycommunity` |
| `NetworkError` | Can't reach api.monarch.com | Check container network egress |

All three exit `1` and are **fatal** — the skill never retries a dead session.
If the data directory is read-only, the cache transparently falls back to a
temp-dir copy (analysis still works; it just won't persist).

## 8. Notes for analysis

- The user is finance-sophisticated — skip 101-level explanations; lead with the
  number and the non-obvious caveat.
- Amounts: positive = inflow/income, negative = outflow/expense.
- Hidden and closed accounts are excluded from net worth automatically.
- Detailed method signatures, return fields, and ready-made SQL live in
  `references/api_surface.md`.
