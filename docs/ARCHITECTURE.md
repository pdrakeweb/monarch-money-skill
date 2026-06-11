# monarch-money skill — Architecture

## Overview

`monarch-money` is a claude.ai user skill that surfaces Monarch Money financial
data for conversational analysis. It wraps `monarchmoneycommunity` (MIT), adds a
SQLite offline cache, and exposes a CLI that Claude invokes via `scripts/fetch.py`.

Claude does NOT get raw data dumps. Scripts print compact Markdown summaries to
stdout; Claude queries the SQLite cache directly with pandas/SQL for analysis.

## Dual-Mode Operation

The skill retrieves live data one of two ways; the SQLite cache and all
analysis are identical either way.

**Detection.** Claude probes for a connected Monarch Money MCP server
(`check_auth_status` for robcerda/monarch-mcp-server, or `get_accounts`). If a
tool answers, **MCP mode** is active; if the tools are absent, the skill stays
in **Direct mode** and drives `fetch.py` as before. fetch.py is never removed —
it is the fallback and the only path when no MCP is connected.

**The bridge — `cache_writer.py`.** In MCP mode Claude calls the MCP read
tools for live data, then pipes their JSON through `scripts/cache_writer.py`,
which writes it into the same `monarch_cache.db` fetch.py uses. cache_writer
reuses fetch.py's normalisers (camelCase / nested-GraphQL → snake_case) and
MonarchCache's write methods, so the two modes can never produce divergent
rows. It normalises field-name variants, degrades missing fields to NULL,
silently drops unknown fields (forward-compatible with newer MCP tool
versions), runs tax-bucket classification on accounts, keeps transaction
inserts idempotent, and emits the same `{"error","message"}`/exit-1 contract
on failure. fetch.py still owns retrieval+caching in Direct mode; in MCP mode
the skill's scripts are used only for caching and analysis.

**Session sharing.** MCP servers save their own session pickle (robcerda:
`.mm/mm_session.pickle`, relative to where its login ran). `_find_session()`
checks, in priority order: the skill's own session → `<repo>/.mm/mm_session.pickle`
→ `~/.mm/mm_session.pickle` → a `session_path` from a `config.json` walking up
from the scripts dir. `MonarchClient.from_session` uses it, logging the chosen
path to stderr. So when an MCP is connected, the skill's own Direct-mode auth
usually needs no separate setup.

**New capability — net-worth history.** MCP exposes daily net-worth and
per-account balance snapshots (`get_net_worth`,
`get_account_balance_history`) that fetch.py never pulled. cache_writer writes
these to two v2 tables (`net_worth_history`, `account_history`), unlocking
trend analysis the Direct-mode path didn't have.

**Implementation agnosticism.** The design targets tool *shapes*, not a
specific server. The official first-party Monarch MCP may name tools
differently; the SKILL.md mapping table and the api_surface.md probe note tell
Claude to discover tool names and adapt, and cache_writer's liberal shape
extractors accept list/wrapped/nested payloads from any implementation.

## Repository Layout

```
monarch-money/          ← folder name MUST match SKILL.md frontmatter `name`
├── SKILL.md
├── scripts/
│   ├── monarch_client.py    ← session load, client init, error classification
│   ├── setup_auth.py        ← one-time login, writes session pickle
│   ├── fetch.py             ← Direct mode: data retrieval + cache; subcommands
│   └── cache_writer.py      ← MCP mode: pipe MCP tool JSON → SQLite cache
├── references/
│   └── api_surface.md
└── data/                    ← created at runtime (empty in ZIP package)
```

## Auth Design

**Option A (default):** `setup_auth.py --email E --password P --mfa-code 123456`
Logs in, saves session pickle only. Password NOT stored. Re-run on expiry with
fresh TOTP code.

**Option B (opt-in):** `--persist-credentials` flag. Stores email + password +
mfa_secret_key in data/credentials.json for zero-touch reauth. SKILL.md must
require user confirmation before this path is used.

**Option C (token):** `--token TOKEN`. Adopts an existing API bearer token.
Validated with one lightweight API call (get_subscription_details) before the
session pickle is saved. Cannot be combined with --persist-credentials (tokens
expire; they are not reusable credentials). NOTE (field finding, mid-2026): the
Monarch web app no longer uses bearer tokens — browser sessions are cookie-only,
so tokens cannot be extracted from DevTools anymore. Option C only applies to a
token obtained elsewhere; browser-based no-password setup is Option D.

**Session vs password lifecycle (field finding):** rotating the Monarch
password does NOT invalidate an existing saved session/token. Documented
pattern for chat-based Option A setup: authenticate once, then rotate the
password — the session keeps working and nothing left in chat history is
usable.

**Option D (browser cookies):** `--csrf-token X --session-id Y` (or
`--cookie "session_id=...; csrftoken=..."`). Monarch's cookie names are
`session_id` and `csrftoken` (library REQUIRED_COOKIES), visible in DevTools →
Application → Cookies at app.monarch.com — including the HttpOnly session_id.
Library 1.4.0 supports cookies natively: login_with_cookies validates with a
real API call and the standard session pickle persists auth_mode='cookie' +
the cookie dict, so cookie sessions reload through from_session like any
other. QA-verified: all reads AND all CSRF-protected writes work under cookie
auth. apply_cookie_auth() prefers native set_cookies and falls back to manual
headers on pre-1.4 libraries; legacy data/cookie_session.json sidecars (from
pre-1.4 setups, possibly with the old `sessionid` key) still load via
from_cookie_session. ensure_authenticated() order: session pickle → legacy
cookie sidecar → persisted credentials → AuthExpiredError.

This matters because users may have no local Python: the skill executes in
Anthropic's container (which has Python), so all options can run
in-conversation. SKILL.md instructs Claude to state the trade-offs once and
proceed with the user's choice — never refuse an option the user picked
(password transits chat history for A/B; TOTP codes expire in 30s).

Session expiry: catch LoginFailedException → classified error → exit 1.
Never retry a dead session. Never print session token/password/secret to stdout.

## SQLite Schema

Database: `data/monarch_cache.db`

### accounts (TTL: 4h)
id TEXT PK, name, type, subtype, institution, balance REAL, display_balance REAL,
currency TEXT DEFAULT 'USD', is_manual INT, is_hidden INT, is_closed INT,
updated_at INT

### account_tax_buckets (auto-classified, user-correctable)
account_id TEXT PK, tax_bucket TEXT, auto_classified INT DEFAULT 1,
subtype_source TEXT

Auto-populated on every accounts sync. Rows with auto_classified=0 (user-set)
are NEVER overwritten; auto rows (auto_classified=1) are upserted each sync so
classifier improvements heal stale classifications.

Inference map (case-insensitive; underscores in live snake_case subtypes are
normalised to spaces; account NAME used where the subtype is ambiguous):
- name contains charitable/daffy/donor advised/giving account → excluded
  (out of asset-location math, no unclassified warning)
- roth, roth ira, roth 401k, roth 403b → roth
- non_taxable_brokerage_account (employer brokerage windows, e.g. Fidelity
  BrokerageLink) → roth if the NAME contains roth, else traditional
- traditional, traditional ira, 401k, 403b, 457b, sep ira, simple ira, pension → traditional
- hsa, health savings (incl. health_savings_account) → hsa
- 529, education → other
- brokerage, individual, joint, taxable, investment, trust → taxable
- anything else → other

After sync, if any investment account maps to 'other', append warning to stdout:
"⚠ N investment account(s) have unclassified tax buckets — ask Claude to list
and correct them."

### holdings (TTL: 4h)
account_id TEXT, ticker TEXT, name, quantity REAL, value REAL, price REAL,
cost_basis REAL (NULL if Monarch doesn't provide), fetched_at INT
PRIMARY KEY (account_id, ticker)
On re-sync: delete removed tickers per account first, then upsert.

### transactions (rolling window TTL: 24h; historical: permanent)
id TEXT PK, account_id, date TEXT, amount REAL (positive=income, negative=expense),
merchant, category, category_group, tags TEXT (JSON), notes, is_pending INT,
fetched_at INT
Indices on (date), (account_id), (category)
Incremental: fetch last_tx_sync - 3 days to today. Never delete rows.

### budgets (TTL: 4h open month, permanent closed)
period TEXT, category_id TEXT, category, category_group, budgeted REAL, actual REAL
PRIMARY KEY (period, category_id)

### cashflow (TTL: 4h open month, permanent closed)
period TEXT PK (YYYY-MM), income REAL, expense REAL, savings REAL,
savings_rate REAL, by_category_json TEXT, by_group_json TEXT, fetched_at INT

### recurring (TTL: 4h)
id TEXT PK, merchant, amount REAL, frequency, next_date TEXT, account_id, fetched_at INT

### net_worth_history (permanent; schema v2, MCP-mode only)
date TEXT PK (ISO-8601), total REAL, assets REAL, liabilities REAL, fetched_at INT
Daily net-worth snapshots from `get_net_worth`. INSERT OR REPLACE (idempotent
per date). Populated by `cache_writer.py --table net-worth-history`.

### account_history (permanent; schema v2, MCP-mode only)
account_id TEXT, date TEXT (ISO-8601), balance REAL, fetched_at INT
PRIMARY KEY (account_id, date)
Daily per-account balance snapshots from `get_account_balance_history`.
INSERT OR REPLACE (idempotent). Populated by
`cache_writer.py --table account-balance-history --account-id ID`.

### exclusions (user config; schema v3)
id INTEGER PK AUTOINCREMENT, type TEXT (transaction|category|merchant|tag|
category_period), value TEXT, start_date TEXT (NULL=all time), end_date TEXT,
reason TEXT, exclude_from TEXT DEFAULT 'all' (budget|cashflow|insights|all),
auto_suggested INT, created_at INT, UNIQUE(type, value, start_date, end_date)

The non-recurring-expense filter. Deliberately kept OUT of `SCHEMA["tables"]`
so the skeleton clear-cache / prune machinery never touches it — exclusions are
user config the API can't regenerate and must survive a cache clear. Managed
via `MonarchCache.add/remove/list_exclusions` + `manage_exclusions.py`.
`get_exclusion_filter_sql(scope)` is the single source of truth every
exclusion-aware caller embeds (NULL-safe dedup in `add_exclusion` because
SQLite treats NULLs as distinct in UNIQUE indexes). `analysis.suggest_exclusions`
(pure, no DB) proposes candidates via four heuristics — outlier / sparse /
keyword / single-large-in-small — which `manage_exclusions.py suggest` prints
as ready-to-run `add` commands (it never auto-adds).

### meta
key TEXT PK, value TEXT
Keys: last_full_sync, last_tx_sync, schema_version, api_call_count

**Schema migrations.** `schema_version` lives in `meta`. The base DDL is the
union of all versions (`CREATE TABLE IF NOT EXISTS`), so fresh installs get
every table at once. `MonarchCache._migrate_schema` captures the on-disk
version before init and, for a pre-v2 database, (re)creates the v2 snapshot
tables; for a pre-v3 database it creates the v3 `exclusions` table — making
each upgrade marker-driven and idempotent.

## Write Policy Enforcement

Two tiers, enforced in code (GuardedMonarch proxy) and mirrored verbatim in
SKILL.md so instructions and code agree:

1. FORBIDDEN_METHODS — account lifecycle (create_manual_account,
   delete_account). Raise BlockedEndpointError at attribute lookup, before any
   request object exists. allow() refuses to arm them; no override exists.
2. CONFIRM_METHODS — every other write endpoint (update_account, transaction
   mutations, budget writes incl. reset_budget/set_budget_amount, recurring
   updates, uploads, account refresh requests). Raise
   ConfirmationRequiredError unless called inside
   `with client.mm.allow("<method>"):` — a one-shot, per-method arming that
   SKILL.md permits ONLY after the user explicitly confirmed that specific
   action in the conversation. Arming is scoped to the with-block and does not
   extend to other methods.

Rationale for the double layer: instructions alone can be ignored by a
confused model; code alone gives no UX for legitimate writes. Together, the
instructions produce the confirmation conversation and the code makes skipping
it impossible. Reads and internals (_headers, token, save_session) pass
through. MonarchClient.__init__ always wraps, so every construction path
(token session, cookie session, credentials) is covered.

## Error Classes

AuthExpiredError — LoginFailedException / HTTP 401
  stderr: "Monarch session expired. Re-run: python scripts/setup_auth.py --email ... --mfa-code CODE"

SchemaChangedError — GraphQL parse error / unexpected field absence
  stderr: "Monarch API schema changed. Update: pip install -U monarchmoneycommunity"

NetworkError — ConnectionError / timeout to api.monarch.com
  stderr: "Cannot reach api.monarch.com. Check network egress from container."

Exit code 1 on all three. Never retry against a dead session.

Surfacing: fetch.py emits {"error": <class>, "message": <str>} as JSON on
stdout (machine-parseable by Claude); setup_auth.py writes its failure message
to stderr. The messages above are the canonical texts.

## Writability Fallback

On cache init: try makedirs(data/). On PermissionError: fall back to
/tmp/monarch_cache_{YYYYMMDD}.db. Session fallback is NOT possible — auth
failure is fatal.

## Output Format

- Scripts: compact Markdown summaries to stdout only. No JSON dumps, no raw
  transaction rows.
- Analysis: Claude queries SQLite directly (pandas/SQL) after syncing.
- Rich reports: Claude generates Markdown/HTML artifacts in-conversation from
  queried data. Scripts do NOT pre-generate HTML.

## CLI Subcommands (fetch.py)

accounts [--refresh]
holdings [--refresh]
transactions [--days N] [--account ID] [--category C] [--refresh]
budgets [--month YYYY-MM] [--refresh]
cashflow [--month YYYY-MM] [--months N] [--refresh]
recurring [--refresh]
net-worth                    (no API call, derives from cached accounts)
sync-all [--refresh]         (runs all syncs in order)
status                       (cache row counts, ages, API calls)
auth-status                  (session age, expiry estimate)
clear-cache [--include-user-buckets]
                             (no API call; empties synced data + auto tax
                              buckets so the next sync re-pulls and re-infers;
                              user-corrected buckets survive unless the flag
                              is passed; lifetime counters kept)

Global flags: --refresh (bypass TTL), --mock (load fixture JSON)
