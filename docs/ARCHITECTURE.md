# monarch-money skill — Architecture

## Overview

`monarch-money` is a claude.ai user skill that surfaces Monarch Money financial
data for conversational analysis. It wraps `monarchmoneycommunity` (MIT), adds a
SQLite offline cache, and exposes a CLI that Claude invokes via `scripts/fetch.py`.

Claude does NOT get raw data dumps. Scripts print compact Markdown summaries to
stdout; Claude queries the SQLite cache directly with pandas/SQL for analysis.

## Repository Layout

```
monarch-money/          ← folder name MUST match SKILL.md frontmatter `name`
├── SKILL.md
├── scripts/
│   ├── monarch_client.py    ← session load, client init, error classification
│   ├── setup_auth.py        ← one-time login, writes session pickle
│   └── fetch.py             ← data retrieval + cache; all subcommands
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
are NEVER overwritten.

Inference map (case-insensitive subtype match):
- roth, roth ira, roth 401k, roth 403b → roth
- traditional, traditional ira, 401k, 403b, 457b, sep ira, simple ira, pension → traditional
- hsa, health savings → hsa
- 529, education → other
- brokerage, individual, joint, taxable, investment (without IRA/401k context) → taxable
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

### meta
key TEXT PK, value TEXT
Keys: last_full_sync, last_tx_sync, schema_version, api_call_count

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

Global flags: --refresh (bypass TTL), --mock (load fixture JSON)
