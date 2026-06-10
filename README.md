# monarch-money

A claude.ai user skill that surfaces your **Monarch Money** financial data —
accounts, net worth, holdings, transactions, budgets, and cashflow — for
conversational analysis.

It wraps the community [`monarchmoneycommunity`](https://pypi.org/project/monarchmoneycommunity/)
library (MIT), adds a SQLite offline cache, and exposes a small CLI
(`scripts/fetch.py`) that Claude calls. Scripts print **compact Markdown
summaries**; Claude queries the SQLite cache directly for real analysis.

## What it does

- **Accounts & net worth** — balances by institution, assets vs liabilities.
- **Holdings** — positions, unrealized P/L, concentration, top-5 by value.
- **Transactions** — windowed summaries, top merchants, category filters.
- **Budgets** — budget vs actual with overage flags.
- **Cashflow** — income / expense / savings rate, single or multi-month.
- **Recurring** — subscriptions and monthly run-rate.
- **Tax buckets** — auto-classifies investment accounts (roth / traditional /
  hsa / taxable / other) for asset-location analysis; user-correctable.

## Requirements

- Python 3.10+
- `pip install -r requirements.txt` (pins `monarchmoneycommunity==1.4.0`, which
  in turn pins `gql==4.0` — **do not override**).

## First-time auth

**You don't need Python installed locally.** The skill runs in Anthropic's
container, which has Python — just ask Claude to set up Monarch auth in a
conversation where the skill is enabled, and it runs `setup_auth.py` for you.
Four options — **all are supported; pick whichever you're comfortable with**
(Claude will state the trade-off and proceed with your choice):

### Option D — browser cookies (easiest no-password route)

Copy two cookie values from a logged-in Monarch browser session:

1. Log in at app.monarch.com in a desktop browser.
2. DevTools (F12) → **Application** tab (Firefox: **Storage**) → **Cookies** →
   the `app.monarch.com` entry.
3. Copy the **Value** of the `csrftoken` and `session_id` rows (note the
   underscore) and give both to Claude — or run locally:
   `python monarch-money/scripts/setup_auth.py --csrf-token X --session-id Y`
   (or `--cookie "session_id=...; csrftoken=..."`).

The values are validated with a real API call (`login_with_cookies`) and saved
in the standard session pickle — cookie sessions reload like token sessions.
QA-verified: every read and CSRF-protected write works identically under
cookie auth. Cookies expire with the browser session (typically weeks) — just
re-copy when that happens.

### Option C — bearer token

`python monarch-money/scripts/setup_auth.py --token YOUR_TOKEN` adopts an
API bearer token.

**Note (mid-2026):** the Monarch web app no longer uses bearer tokens — it
authenticates with session cookies only, so you can't extract a token from the
browser anymore (no `Authorization: Token …` header, nothing in localStorage).
Tokens are still mintable via the login API, though: a short Python script
calling the library's `login()` and reading `mm.token` produces one (Claude
can run it in-session — it needs your credentials, so the Option A caveats
apply). Or use a token you saved from a previous integration or another tool.
For browser-based no-password setup, use Option D.

### Option A — email + password + TOTP code

```
python monarch-money/scripts/setup_auth.py \
    --email you@example.com --password 'YOUR_PASSWORD' --mfa-code 123456
```

Saves `monarch-money/data/session.pickle`; your password is **never** written to
disk. If you do this through chat instead of locally, your password will appear
in the conversation history — the clean fix is to **rotate your password right
after setup: the saved session keeps working after a password change**, so
nothing left in the history remains usable. TOTP codes expire in 30 seconds —
send a freshly generated code.

### Option B — persisted credentials (zero-touch reauth, opt-in)

Stores credentials so the skill can re-authenticate unattended:

```
python monarch-money/scripts/setup_auth.py \
    --email you@example.com --password 'YOUR_PASSWORD' \
    --mfa-secret-key BASE32SECRET --persist-credentials
```

Writes `data/credentials.json` (git-ignored). Revoke by deleting that file and
rotating your Monarch password / MFA secret.

## Command reference

Run from the `monarch-money/` directory: `python scripts/fetch.py <command>`.

| Command | Description | Flags |
|---------|-------------|-------|
| `accounts` | Accounts table, net worth, tax-bucket warnings | `--refresh` |
| `holdings` | Positions, unrealized P/L, top-5 | `--refresh` |
| `transactions` | Window summary + top merchants | `--days N`, `--account ID`, `--category C` |
| `budgets` | Budget vs actual, flags overages | `--month YYYY-MM` |
| `cashflow` | Income / expense / savings rate | `--month`, `--months N` |
| `recurring` | Subscriptions + monthly run-rate | `--refresh` |
| `net-worth` | Net worth from cache (no API call) | — |
| `sync-all` | Run every sync in order | `--refresh` |
| `status` | Cache row counts, ages, API-call total | — |
| `auth-status` | Session age + expiry estimate | — |

Global flags: `--refresh` (bypass cache TTL), `--mock` (use bundled fixtures,
no network — great for trying it offline), `--db PATH`, `--fixtures-dir DIR`.

Try it with no account:

```
python monarch-money/scripts/fetch.py sync-all --mock --db /tmp/demo.db
```

## MFA / TOTP secret

For `--mfa-code` you supply a current 6-digit TOTP code. For Option B's
`--mfa-secret-key` you need the underlying Base32 secret:

> Monarch → **Settings → Security → MFA / Two-factor authentication**. When
> enabling an authenticator app, choose "enter the code manually" / "can't scan"
> — Monarch then shows the Base32 secret string. That string is your
> `--mfa-secret-key`.

## Write policy: two tiers, enforced in code

The client layer (`GuardedMonarch` proxy) enforces a write policy that the
skill's instructions mirror — so both the model's instructions *and* the code
have to agree before anything changes in your Monarch account:

- **Account lifecycle is permanently blocked.** `create_manual_account` and
  `delete_account` raise `BlockedEndpointError` at attribute access. No
  override exists; those actions can only be done in the Monarch app.
- **All other write endpoints require per-action confirmation.** Transaction
  edits, budget writes (`set_budget_amount`, `reset_budget`), tags, splits,
  uploads, etc. raise `ConfirmationRequiredError` unless the call is wrapped
  in `with client.mm.allow('<method>'):` — and the skill instructs Claude to
  open that block only after you explicitly confirm the specific action in
  chat. Arming is one method at a time, scoped to the with-block.

Reads are unrestricted. The only thing the skill modifies without asking is
its own local SQLite cache.

## Known limitations

- **Lot-level data is unavailable.** Monarch exposes position-level holdings
  only — no per-lot cost basis, acquisition dates, or short/long-term split. The
  TLH screen flags *which tickers* are underwater, but exact loss amounts are
  approximate (blended basis) and wash-sale safety cannot be judged from Monarch
  data alone. Precise harvesting needs your brokerage's lot-level CSV.
- **Unofficial API.** `monarchmoneycommunity` reverse-engineers Monarch's
  GraphQL API; a schema change can break a sync (surfaced as `SchemaChangedError`
  → `pip install -U monarchmoneycommunity`).
- **Session lifetime.** Sessions expire eventually; re-run `setup_auth.py`.

## Build & deploy

```
.\build.ps1                  # validates folder==SKILL.md name; emits monarch-money.zip (empty data/)
./build.sh                   # same, for macOS/Linux (uses zip or falls back to Python)
.\deploy.ps1                 # build + extract + mirror into Claude Desktop's skills dir
.\deploy.ps1 -DryRun         # preview everything, write nothing
.\deploy.ps1 -RestartClaude  # deploy and restart Claude Desktop
.\deploy.ps1 -SkipBackup     # skip the pre-deploy backup (last 5 are kept)
```

The zip is also what you upload directly at claude.ai → Settings → Capabilities
→ Skills. `deploy.ps1` auto-detects Claude Desktop's nested skills-plugin
layout, backs up the previous deployment, and excludes `data\` from the mirror
so your session and cache survive redeploys. After deploying, **restart Claude
Desktop** to pick up changes.

## Development

```
python -m pytest tests/test_cache.py tests/test_auth.py tests/test_fetch.py -v
```

`tests/test_integration.py` runs against a live Monarch session and is skipped
unless `monarch-money/data/session.pickle` exists.

## License

MIT. Wraps `monarchmoneycommunity` (MIT).
