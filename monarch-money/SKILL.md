---
name: "monarch-money"
version: "1.0.0"
description: >
  Connect to and sync the user's Monarch Money account into a local cache:
  authentication/login, accounts and balances, investment holdings,
  transactions, budgets, cashflow, recurring, net-worth history. Use this skill
  to set up Monarch access, refresh data, or answer raw balance/transaction
  questions — even if they don't say Monarch by name. This is the DATA LAYER:
  for reports & dashboards (net worth, spending, allocation, tax-loss
  harvesting) the **personal-finance** skill consumes this cache; for
  retirement planning (FIRE, drawdown, TPAW, Social Security, retire-age
  what-ifs) the **retirement** skill does.
---

# Monarch Money (data layer)

The **data layer** of a three-skill family. It syncs the user's Monarch Money
account into a local SQLite cache (`monarch_cache.db`); the **personal-finance**
and **retirement** skills read that cache for analysis and planning. This skill
holds NO reports or financial opinions — it gets the data in, correctly and
securely. Cache & profile contracts: `references/CONTRACTS.md`.

All commands run from the skill directory:
`python scripts/fetch.py <command> [flags]`

## Dual-Mode Operation

This skill works in two modes. Claude detects which to use automatically.

### MCP Mode (preferred when available)
If a Monarch Money MCP server is connected, Claude:
1. Calls MCP tools directly for live data
2. Pipes JSON output through `scripts/cache_writer.py` to populate SQLite
3. Queries SQLite for analysis

Detection: attempt `check_auth_status` (robcerda MCP) or `get_accounts`. If
either succeeds, MCP mode is active. If both fail with "tool not found", fall
back to Direct Mode.

MCP tool → cache_writer.py mapping. Two server families ship different tool
names — **official monarchmoney/monarch-mcp** (PascalCase) and **robcerda**
(snake_case). Both are supported.

| Data | Official MCP tool | robcerda tool | cache_writer table |
|------|-------------------|---------------|--------------------|
| Accounts | `GetAccounts` | `get_accounts` | `--table accounts` |
| Holdings | `GetInvestments` (one row/security) | `get_account_holdings --account_id ID` | `--table holdings --from-investments` (official) / `--table holdings --account-id ID` (robcerda) |
| Transactions | `GetTransactions` (paginated, see below) | `get_transactions` | `--table transactions` |
| Budgets | — | `get_budgets` | `--table budgets --period YYYY-MM` |
| Cashflow | `GetCashFlow` ×2 (income+expense, see below) | `get_cashflow` | `--table cashflow` (combined payload) |
| Recurring | `GetRecurring` (4-bucket shape) | `get_recurring_transactions` | `--table recurring` |
| Net worth history | `GetNetWorthHistory` | `get_net_worth` | `--table net-worth-history` |
| Account balance history | — | `get_account_balance_history --account_id ID` | `--table account-balance-history --account-id ID` |

**Write to cache:** `<mcp_json_output> | python scripts/cache_writer.py --table TABLE [options]`
On Windows PowerShell: write MCP output to a temp file, then
`python scripts/cache_writer.py --table accounts --input $env:TEMP\mm_accounts.json`.

**Wrapped tool results:** MCP results may arrive as
`[{"type":"text","text":"<json-string>"}]`. cache_writer auto-unwraps that
envelope (parses the inner `text`); pipe it in as-is.

**Official MCP recipes (v0.9.0):**
- **Accounts** — `GetAccounts` returns human display `type`s (`Roth IRA`,
  `401k`, `Brokerage (Taxable)`, `Trust`, `Health Savings Account (HSA)`,
  `Other`, …) with NO `subtype`. cache_writer maps these to canonical
  type+subtype so the tax-bucket classifier and the reports' investment
  filters work with **zero manual SQL**. Unknown display types pass through
  and surface in the unclassified warning.
- **Holdings** — official MCP has no per-account holdings call. `GetInvestments`
  returns one row per security with `account_holdings:[{account_name,
  quantity}]`, combined `value`, `variation_dollars` (unrealized P/L),
  `security_type`, and **no cost basis**. Pipe it with
  `--table holdings --from-investments` (sync accounts first — it maps
  `account_name`→id; blended basis = value−variation prorated by quantity;
  NULL basis for null-ticker / `security_type=="other"` employer funds).
- **Cashflow** — `GetCashFlow` returns net only by default; income and expense
  need two calls with `filters={"category_type":"income"|"expense"}`. Combine
  them into one payload `{"months":[{"period":"YYYY-MM","income":X,"expense":Y}]}`
  and pipe once (`--table cashflow`, no `--period` needed — it loops).
- **Recurring** — `GetRecurring` returns four buckets
  (`recurring_income_streams` / `_expense_streams` / `_credit_card_streams` /
  `_transfer_streams`), each wrapping `recurring_transaction_streams:[...]`.
  cache_writer flattens income+expense (skips credit-card = duplicate spend,
  and transfers), mapping `next_forecasted_date`→next date.
- **Transactions pagination** — `GetTransactions` truncates at 1,000 rows and
  sets `"truncated": true` + `transaction_count`. Pull newest-first with
  `limit=1000`; while `truncated`, re-call with `end_date` = (earliest
  returned date − 1 day). Idempotent inserts make the boundary overlap
  harmless. A 12-month pull is typically 3–4 calls.

cache_writer.py normalizes camelCase MCP fields to the cache's snake_case
schema, runs the SAME tax-bucket classification and idempotent inserts as
Direct Mode, and prints a one-line summary (e.g. `Cached 8 accounts. Net
worth: $X. Tax bucket warnings: 0.`). Missing fields degrade to NULL and
unknown fields are dropped, so it tolerates MCP-implementation differences. On
failure it prints `{"error","message"}` JSON and exits 1, like fetch.py. After
writing, **query `monarch_cache.db` directly for analysis** — exactly as in
Direct Mode. MCP mode also shares the session pickle an MCP server saved
(`.mm/mm_session.pickle`), so the skill usually needs no separate auth when an
MCP is connected.

### Direct Mode (fallback)
When no MCP server is connected, use `scripts/fetch.py` as documented below.
fetch.py handles auth, API calls, caching, and summary output in one step.

## 0. Where data lives (persists across chat sessions)

The cache, session, and auth files live in a **persistent data directory**,
NOT the skill-relative `data/`. `python scripts/fetch.py auth-status` prints
the exact dir AND a persistence verdict. **Always check `auth-status` before
asking the user to log in again** — auth from a previous session is reused
automatically.

Persistence depends on where the skill runs (full detail in
`references/persistence.md`):
- **Desktop:** `~/.claude-skills/monarch-money/` — survives new chats AND skill
  updates. Set up auth once.
- **Cloud / mobile:** the container dir persists **within one conversation
  only**; a new chat starts fresh. So filesystem auth does NOT carry across
  chats there — that's a platform limit, not a bug.

If the user reports "lost credentials after updating the skill": versions
before v0.6.0 saved auth *inside* the skill package's `data/`, which a
re-upload wipes. v0.6.0+ fixed that (auth now lives in the persistent dir).
For cross-chat/cross-device zero-touch auth, a `.env` with `MONARCH_COOKIES`
(or `MONARCH_EMAIL`/`MONARCH_PASSWORD`/`MONARCH_MFA_SECRET`) bundled via
`build.ps1 -IncludeAuth` is the durable mechanism — see
`references/persistence.md`. Do NOT just ask them to re-auth without
explaining whether it will persist.

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

## 2. First-run setup

The user does NOT need Python locally — `setup_auth.py` runs right here in the
skill container. **Never instruct the user to run code or scripts on their own
machine**; everything below executes in this environment.

**All options are legitimate and supported.** Briefly state the trade-off of
the user's chosen option ONCE, then proceed with it. Do NOT refuse
email/password/MFA (Option A) or insist on a token/cookie option — the user
decides what they're comfortable sharing. If the user has already provided
credentials, use them; lecturing or stalling is worse than proceeding.

### Option A — email + password + TOTP code (simplest, start here)

The user pastes email, password, and a **current** 6-digit MFA code into chat.
One-sentence caveat to state, then move on: the password will sit in this
conversation's history — and the clean mitigation is to **rotate the password
right after setup: the saved session keeps working after a password change**
(verified in practice), so "authenticate once, then rotate" leaves nothing
sensitive usable in the history. TOTP codes expire every ~30 seconds — run the
command **immediately** after they send the code; if the login fails on
timing, just ask for a fresh code and re-run.

```
python scripts/setup_auth.py --email you@example.com \
    --password 'YOUR_PASSWORD' --mfa-code 123456
```

Saves `data/session.pickle` only — the password is **never** written to disk.
If the code expired mid-flight (login failure), just ask for a new code and
re-run; nothing is left in a bad state.

### Option D — browser cookies (easiest no-password route)

The user copies two cookie values from a logged-in Monarch browser session.
This is simpler than token hunting because DevTools lists cookies by name in a
table — including the HttpOnly `session_id` that JavaScript can't read:

1. Log in at **app.monarch.com** in a desktop browser.
2. DevTools (F12) → **Application** tab (Firefox: **Storage**) →
   **Cookies** → the `app.monarch.com` entry.
3. Copy the **Value** of the `csrftoken` row and the `session_id` row
   (Monarch's name — note the underscore; it is not Django's `sessionid`).
4. Paste both in the chat.

```
python scripts/setup_auth.py --csrf-token CSRFTOKEN --session-id SESSION_ID
```
(or the whole string at once:
`python scripts/setup_auth.py --cookie "session_id=...; csrftoken=..."`)

Validated via the library's native `login_with_cookies` (a real API call) and
saved in the standard `data/session.pickle` — cookie sessions reload exactly
like token sessions on every later fetch. QA-verified: all reads AND all
CSRF-protected writes work identically under cookie auth. The cookies last as
long as the browser session would (typically weeks; possibly shorter than a
bearer token); on expiry (`AuthExpiredError`) ask for fresh values.

### Option B — persisted credentials (zero-touch, opt-in)

Stores email + password + TOTP secret in `data/credentials.json` so the skill
silently re-authenticates forever. **Only use this if the user explicitly asks
for unattended reauth and confirms they understand the credentials are stored**:

```
python scripts/setup_auth.py --email you@example.com \
    --password 'YOUR_PASSWORD' --mfa-secret-key BASE32SECRET --persist-credentials
```

Revocation: delete `data/credentials.json` and rotate the Monarch password.

### Option C — bearer token

`setup_auth.py --token THEIR_TOKEN` adopts an API bearer token, validates it
with one lightweight call, and saves `data/session.pickle`.

⚠ **Do NOT send the user into the browser to find a token.** As of mid-2026
the Monarch web app no longer uses bearer tokens — it authenticates with
session cookies only, so there is no `Authorization: Token …` header in the
Network tab and no token in localStorage. Browser extraction is Option D
(cookies), not this.

Tokens are still mintable, though: the login API itself returns one. If the
user wants a bearer token (e.g. for portability to other tools), you can run a
short Python script HERE in the container that calls the library's
`login(email, password, mfa_secret_key=...)` and reads `mm.token` — that
requires their credentials, so it carries the same caveats as Option A
(confirm first, run immediately, suggest rotating after; note rotation does
not invalidate the session/token). Never print the token to stdout — write it
to a file under `data/` or pass it straight to `setup_auth.py --token`.
Otherwise, Option C is for a token the user already has from a previous
integration or another tool.

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
| `clear-cache` | Empty the local cache; next sync re-pulls and re-infers tax buckets | `--include-user-buckets` |
| `version` | Print the installed skill version | — |

`dashboard` / `report` / `reports` / `profile` were **removed in v1.0.0** (the
three-skill split) — they print a redirect to the personal-finance / retirement
skills. Reports, the dashboard, and the profile interview now live there.

If the user asks **which version of the skill is installed**, you MUST run
`python scripts/fetch.py version` and report THAT output — never answer from
this file's frontmatter or from memory (a stale session can hold an old
frontmatter in context). If the version looks older than expected after a
deploy: this skill is **account/server-synced** (it was uploaded via
Settings), so editing local files or running `deploy.ps1` does NOT change what
loads on web/mobile/desktop — the user must re-upload `monarch-money.zip` via
**Settings → Capabilities → Skills** (there is no API for it). Tell them that
plainly rather than re-deploying.

Global: `--refresh` bypasses TTL. (`--mock` exists for development against the
repo's `tests/fixtures/` — those fixtures are not packaged in the deployed
skill, so don't offer it to the user.)

## 4. Worked examples (data layer)

```
python scripts/fetch.py sync-all          # full sync, all tables
python scripts/fetch.py status            # cache row counts, ages, schema
python scripts/fetch.py net-worth         # net worth from cache (no API call)
```

After syncing, **query `monarch_cache.db` directly** for anything beyond the
built-in summaries — e.g. balances by tax bucket:

```sql
SELECT b.tax_bucket, ROUND(SUM(a.balance),2) total
FROM accounts a JOIN account_tax_buckets b ON a.id=b.account_id
WHERE a.type IN ('investment','brokerage') AND a.is_hidden=0
GROUP BY b.tax_bucket ORDER BY total DESC;
```

For reports, the dashboard, allocation/TLH analysis, the profile interview, and
retirement planning, the **personal-finance** and **retirement** skills consume
this same cache (read-only) and own that logic. See `references/CONTRACTS.md`.


## 5. Correcting tax buckets

**Hidden accounts (esp. DAFs) don't flow through MCP.** Monarch's account
endpoints (MCP `GetAccounts` / robcerda `get_accounts`) omit accounts the user
hid in Monarch — and DAFs are commonly hidden. If the user reports a *missing*
account, **ask whether it's hidden in Monarch before debugging**. After they
unhide it, one re-pull + the name classifier (below) handles it — a DAF named
"CHARITABLE FUND" / containing "DAF" / "Daffy" auto-classifies to `excluded`
regardless of account type.

Buckets are auto-classified from each account's Monarch subtype — and, where
the subtype is ambiguous, its name — on every sync (`roth`, `traditional`,
`hsa`, `taxable`, `excluded`, `other`). The `excluded` bucket (DAF / charitable)
is **authoritative regardless of account type** — those accounts are kept out
of investable assets, FIRE, allocation, and TLH, and surface only in
`daf_total()` and the Net Means line. Notable rules: `trust` → taxable;
`non_taxable_brokerage_account` (employer windows like Fidelity BrokerageLink)
→ roth if the account name says Roth, else traditional; charitable /
donor-advised accounts (Charitable, Daffy, DAF) → `excluded`. MCP display types
(`Roth IRA`, `401k`, `Brokerage (Taxable)`, `Trust`, …) are mapped to canonical
type+subtype on write so classification works with zero manual SQL. Auto rows are refreshed each sync
but never overwrite a user correction. To force a full re-inference from
scratch (e.g. after a classifier change): `python scripts/fetch.py clear-cache`
then re-sync — user corrections survive the clear unless
`--include-user-buckets` is passed. To correct one bucket by hand, set
`auto_classified = 0`:

```sql
UPDATE account_tax_buckets
SET tax_bucket = 'roth', auto_classified = 0
WHERE account_id = 'acc_123';
```

List anything still ambiguous:
```sql
SELECT a.id, a.name, a.subtype, b.tax_bucket
FROM accounts a JOIN account_tax_buckets b ON a.id=b.account_id
WHERE a.type IN ('investment','brokerage') AND b.tax_bucket='other';
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
- Custodians that report no basis (common for employer-plan funds) send
  `basis = 0`; the sync stores those as NULL so they show `—` and stay out of
  TLH queries. Such positions usually also have no public ticker — never
  propose TLH swaps for null-ticker rows.

Say this plainly; don't imply lot-level precision the data can't support.

## 7. Error handling

| Symptom (error JSON on stdout / stderr) | Meaning | Fix |
|------|---------|-----|
| `AuthExpiredError` | Session dead / 401 | Re-run `setup_auth.py` — ask the user for a fresh `--mfa-code` (Option A), cookie values (Option D), or token (Option C) |
| `SchemaChangedError` | Monarch GraphQL changed | `pip install -U monarchmoneycommunity` |
| `NetworkError` | Can't reach api.monarch.com | Check container network egress |

All three exit `1` and are **fatal** — the skill never retries a dead session.
If the data directory is read-only, the cache transparently falls back to a
temp-dir copy (analysis still works; it just won't persist).

## 8. Notes for analysis

- **Write policy (enforced in code AND by these instructions):**
  - **Account create/delete is permanently blocked.** `create_manual_account`
    and `delete_account` raise `BlockedEndpointError`; there is no override.
    If the user wants this, direct them to the Monarch app.
  - **Every other write endpoint requires explicit user confirmation.** Before
    calling anything that modifies Monarch data (`update_transaction`,
    `set_budget_amount`, `reset_budget`, tags, splits, uploads, …): state
    exactly what will change, ask the user to confirm THAT specific action,
    and only after a clear yes wrap the call:
    `with client.mm.allow('set_budget_amount'): client.mm.set_budget_amount(...)`.
    Unarmed calls raise `ConfirmationRequiredError` — the gate exists in code,
    so skipping the question doesn't work. Never arm a method the user didn't
    just confirm; never arm more than the one method needed.
    Special warning for `create_transaction_tag`: the API has **no tag
    deletion** — a created tag is permanent until removed by hand in the
    Monarch UI. Say so in the confirmation question.
  - Correcting the local `account_tax_buckets` table (§5) needs no
    confirmation: that's the skill's own cache, not Monarch.
- The user is finance-sophisticated — skip 101-level explanations; lead with the
  number and the non-obvious caveat.
- Amounts: positive = inflow/income, negative = outflow/expense.
- Hidden and closed accounts are excluded from net worth automatically.
- Detailed method signatures, return fields, and ready-made SQL live in
  `references/api_surface.md`.

## 9. Skeleton vs skill (where fixes go)

This skill is built on **claude-skill-skeleton**
(github.com/pdrakeweb/claude-skill-skeleton). When fixing something, route
the change to the right layer:

- **Skeleton files** (fix UPSTREAM in the skeleton repo, then merge down via
  `git fetch skeleton && git merge skeleton/main`):
  `scripts/skill_cache.py` (SQLite cache, TTL, schema-driven clear-cache),
  `scripts/skill_common.py` (errors, UTF-8, persistent data-dir resolution,
  bootstrap, .env loader), and the repo-root `build.ps1` / `build.sh` /
  `deploy.ps1` / `validate_setup.py`.
- **Monarch-specific files** (fix HERE): `scripts/skill_schema.py` (version,
  DDL, TTLs, protected tax-bucket rows), `scripts/monarch_client.py`
  (MonarchCache write methods, tax-bucket inference, auth flows, GuardedMonarch
  write policy), `scripts/fetch.py`, `scripts/setup_auth.py`, this file, and
  `references/`.
