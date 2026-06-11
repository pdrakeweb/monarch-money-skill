---
name: "monarch-money"
version: "0.9.0"
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
any real analysis, query the SQLite cache directly** with SQL/pandas — don't
ask the scripts to dump raw rows.

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
| `dashboard` | Boglehead/FIRE dashboard: KPI header + all 11 reports in summary with links | — |
| `report <key>` | One full report (`report all` = every full report in one call) | — |
| `reports` | List the 11 report keys | — |
| `profile` | Profile path + completeness + active SWR | — |
| `version` | Print the installed skill version | — |

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
WHERE a.type IN ('investment','brokerage') AND a.is_hidden=0
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
WHERE cost_basis IS NOT NULL    -- unknown basis (incl. Monarch's 0) is NULL
  AND ticker IS NOT NULL        -- employer-plan funds have no public ticker
  AND value < cost_basis
ORDER BY unrealized_pl ASC;
```
⚠ **Always state the lot-level caveat** (§6) when surfacing TLH candidates.

### Monthly cashflow, 3-month rolling
```
python scripts/fetch.py cashflow --months 3
```

## 2b. First-run profile interview (do this before the first dashboard)

The reports depend on `profile.json` (in the data dir; `fetch.py profile` shows
status). When it's missing or has `"profile_complete": false`, **run an intake
interview before computing** — you conduct it, it is not a code loop. Order:

1. **Pre-fill from context FIRST — do NOT ask for what you already know.**
   Populate every field you can from this conversation, memory, and the cached
   data before asking anything. Examples that must be inferred, not asked:
   tax-bucket balances → Roth/trad/taxable shares; holdings → risk posture and
   sector tilts; mortgage transactions → payoff interaction; observed
   allocation → risk tolerance confirmation. Asking for a known fact is a
   failure.
2. **Research-fill defaults** for anything not in context (SWR from horizon,
   healthcare bridge from typical ACA costs, effective retirement tax rates),
   and mark them as assumptions.
3. **Ask ONLY the genuine gaps, batched, with pick-lists** (the surface has a
   tappable multi-select — use it; don't make the user free-type a choosable
   value). Question bank (ask only what context can't answer):
   - **birth year as a NUMBER** (e.g. "1976") — never an age range; the
     horizon math is sensitive and a range pick produced a 4-year error in the
     field. Then target retirement age; life-expectancy planning age
     (default 95).
   - **withdrawal philosophy** (pick-list): fixed SWR / Guyton-Klinger
     guardrails / **TPAW** (amortization-based variable withdrawal) →
     `withdrawal_strategy`.
   - **spending tilt** (pick-list): flat / **front-load** (earlier years worth
     more — die-with-zero) / back-load → `spending_tilt_pct` (front-load =
     negative, e.g. −1.5%).
   - **income streams**: Social Security estimate + claim age, pensions →
     `income_streams` (PV'd as bonds into the TPAW total portfolio).
   - **annual new savings** to retirement (`annual_new_savings`) — ASK
     explicitly and say WHY it can't be inferred: brokerage buys/sells churn
     through transactions so it's not observable from the cache (A4). Needed
     for the FIRE trajectory and the MC accumulation phase.
   - anticipated expense changes at 1y/5y/10y (pick-list: new vehicle,
     boat/upgrade, remodel, move/new mortgage, child costs start/end,
     healthcare change, eldercare, none) + amount + horizon → routes to
     `anticipated_changes` (`one_time` or `lumpy`).
   - **risk tolerance phrased as confidence** (pick-list): "must never cut" /
     "small cuts OK" / "wide bands OK — 50–60% confidence". Maps onto the MC
     confidence readouts and guardrail/TPAW band width. Confirm against
     observed allocation rather than asking blind.
   - risk capacity (years to retirement, income stability, % of NW
     discretionary).
   - sector/factor views 1y/5y/10y (pick-list: AI/semis, broad tech,
     healthcare/biotech, energy, financials, industrials, intl developed, EM,
     small-cap value, broad/no-tilt) + over/underweight → `sector_views`.
   - retirement relocation to a no-income-tax state? → tax model.
   - charitable intent + DAF in use/planned → Net Means treatment.
   - legacy intent: spend-down (die-with-zero) vs leave-estate.
   - mortgage payoff intent (early vs term) → `mortgage_payoff_age`.
4. **Write `profile.json`** (in the data dir) with the answers +
   `"profile_complete": true`, then compute and render.

The dashboard prints a "Profile incomplete" banner until this is done.

## 4b. Boglehead / FIRE reports & dashboard

The skill ships **11 reports** plus a **comprehensive dashboard**, all computed
from the cache (no API calls). Keys: `net-worth`, `allocation`,
`asset-location`, `fire-progress`, `withdrawal`, `savings-rate`, `expenses`,
`emergency-fund`, `tlh`, `concentration`, `life-value`.

- One report: `python scripts/fetch.py report fire-progress`
- Every full report at once: `python scripts/fetch.py report all`
- The dashboard: `python scripts/fetch.py dashboard`

**Sync first** (`sync-all`). **Run the §2b interview** if the profile is
incomplete. Then build the deliverable: run `fetch.py dashboard` AND
`fetch.py report all`, and render **one self-contained HTML artifact** — KPI
strip + a summary card per report, each linking (accordion/anchor) to the
embedded full body. Numbers are precomputed — embed them. Offer theme-factory
for styling.

**What the math now does correctly (don't undo this):**
- **Outflows are classified.** Investment buys, 401k/IRA/HSA contributions, and
  debt principal are `transfer` (savings, NOT expenses); a boat/remodel is
  `one_time` (excluded from the run-rate, shown separately); car/roof/HVAC are
  `lumpy` (amortized sinking-fund allowances). Only `recurring` is the budget.
- **FIRE number is built on the projected RETIREMENT budget**, grossed up for
  the trad/LTCG-taxed share of withdrawals, ÷ a **horizon-derived SWR** (not a
  flat 4%). State the SWR source and tax assumptions when presenting.
- **Savings rate is positive and sane** — consumption-based, with capital
  deployed shown separately; one-time-distorted months flagged.
- **Life-value framing:** experiential/charitable spend is the point, not
  leakage; a time-discounted view favors front-loading; the spend-down
  glidepath flags a ballooning terminal balance as a reason to spend/give more.
- **Net Worth vs Net Means:** accounting figure (excl. DAF) and life-value
  figure (incl. directed DAF). Label mapping is `net_means_mapping` in profile.

**Assumptions** all live in `profile.json`; the §2b interview fills it.
Defaults are correct unattended (transfers excluded, SWR horizon-derived). Full
methodology + the complete profile schema: `references/reports.md`. State the
key assumptions whenever you present numbers — this user is FIRE/Boglehead
literate; lead with the number and the non-obvious caveat.

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
