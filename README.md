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
- `pip install -r requirements.txt` (pins `monarchmoneycommunity==1.3.2`, which
  in turn pins `gql==4.0` — **do not override**).

## First-time auth (Option A — recommended)

Session-only login. Your password is **never** written to disk.

```
python monarch-money/scripts/setup_auth.py \
    --email you@example.com --password 'YOUR_PASSWORD' --mfa-code 123456
```

Saves `monarch-money/data/session.pickle`. Re-run with a fresh TOTP code when the
session expires (typically weeks to months).

### Option B (zero-touch reauth, opt-in)

Stores credentials so the skill can re-authenticate unattended. Only use this if
you want unattended operation:

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

## Build & deploy (Windows / Claude Desktop)

```
.\build.ps1     # validates folder==SKILL.md name; emits monarch-money.zip (empty data/)
.\deploy.ps1    # builds, then copies the zip into Claude Desktop's skills-plugin dir
```

After deploy, **restart Claude Desktop** to pick up changes.

## Development

```
python -m pytest tests/test_cache.py tests/test_auth.py tests/test_fetch.py -v
```

`tests/test_integration.py` runs against a live Monarch session and is skipped
unless `monarch-money/data/session.pickle` exists.

## License

MIT. Wraps `monarchmoneycommunity` (MIT).
