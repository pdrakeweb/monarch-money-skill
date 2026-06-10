# CLAUDE.md — monarch-money

## ⛔ STOP — DEPLOYMENT REQUIRED AFTER ANY EDIT

After editing ANY file in `monarch-money\`, run:

```powershell
.\deploy.ps1                 # build + mirror into Claude Desktop's skills dir
.\deploy.ps1 -RestartClaude  # ...and bounce Claude Desktop for you
.\deploy.ps1 -DryRun         # preview target + file changes, write nothing
```

Then **restart Claude Desktop** (unless you passed `-RestartClaude`).

`deploy.ps1` does NOT refresh the running skill automatically — the deployed copy
is a separate artifact. Edit source → `deploy.ps1` → restart Claude Desktop.
Skip this and you are testing stale code.

Deploy details: it auto-detects the nested `<guid>\<guid>\skills\monarch-money\`
layout under `%APPDATA%\Claude\local-agent-mode-sessions\skills-plugin`, backs up
the previous deployment (last 5 kept in `.backups\`; skip with `-SkipBackup`),
and mirrors with robocopy /MIR — **excluding `data\`**, so the deployed session
pickle / cache / credentials survive every redeploy.

## Repo layout

```
monarch-money/          Canonical skill source. EDIT HERE.
  SKILL.md              Frontmatter `name:` MUST equal the folder name.
  scripts/              monarch_client.py, setup_auth.py, fetch.py
  references/           api_surface.md (methods + SQL patterns)
  data/                 runtime cache/session — git-ignored, ships EMPTY
tests/                  pytest unit + mock + (skipped) integration tests
docs/ documents/ planning/   design + architecture + execution plan
build.ps1 / deploy.ps1  package to monarch-money.zip / push to Claude Desktop
```

## Versioning — bump on EVERY commit that touches monarch-money\

The skill carries a semver version in TWO places that must always match
(build.ps1/build.sh refuse to package on mismatch; a unit test checks too):

1. `monarch-money\scripts\skill_schema.py` → `SKILL_VERSION = "X.Y.Z"`
2. `monarch-money\SKILL.md` frontmatter → `version: "X.Y.Z"`

Before committing any change under `monarch-money\`:
- **patch** (X.Y.Z+1): bug fixes, doc/instruction tweaks, classifier rules
- **minor** (X.Y+1.0): new commands, new auth options, new capabilities
- **major** (X+1.0.0): breaking changes — CLI flags removed/renamed, SQLite
  schema changes, auth-flow changes that require user re-setup

Update both files, commit, then tag: `git tag vX.Y.Z`. The user verifies the
installed version by asking the skill's Claude, which runs
`python scripts/fetch.py version` — if that lags the repo, the deploy is stale.

## Skeleton usage — what's upstream vs what's monarch's

This repo adopted **claude-skill-skeleton** (remote `skeleton`,
github.com/pdrakeweb/claude-skill-skeleton). Pull improvements with:

```powershell
git fetch skeleton
git merge skeleton/main
```

**Skeleton-owned files — fix in the skeleton repo first, push, then merge
down here** (editing them only in this repo forks them silently):
- `build.ps1`, `build.sh`, `deploy.ps1`, `validate_setup.py` (repo root)
- `monarch-money\scripts\skill_cache.py` — SkillCache (schema-driven SQLite
  cache, TTL freshness, counters, status, clear/prune)
- `monarch-money\scripts\skill_common.py` — SkillError base, UTF-8 fix,
  resolve_data_dir/bootstrap_data (cross-session persistence), load_dotenv

**Monarch-owned files — fix here:**
- `monarch-money\scripts\skill_schema.py` — SKILL_VERSION + SCHEMA (DDL,
  TTLs, protected tax-bucket rows)
- `monarch-money\scripts\monarch_client.py` — MonarchCache write methods,
  tax-bucket inference, auth flows (token/cookie/credentials/.env),
  GuardedMonarch write policy, error classification
- `monarch-money\scripts\fetch.py`, `setup_auth.py`, `SKILL.md`, `references\`

Note: the skeleton sample dir (`my-skill/`), its `cli.py` scaffold, and
`tests/test_skeleton.py` were intentionally dropped in the adoption merge —
monarch's `fetch.py` and test suite predate them and cover the same ground.
Expect modify/delete conflicts on those paths in future skeleton merges;
resolve by deleting (`git rm`).

## Persistence model (why data\ is NOT the data dir)

Skill-relative `data\` dies with each chat session (sessions get a fresh copy
of the skill). Runtime data lives in the persistent dir resolved by
`skill_common.resolve_data_dir`: `$MONARCH_MONEY_DATA_DIR` override >
`/mnt/user-data/.claude-skills/monarch-money/` (cloud) >
`~/.claude-skills/monarch-money/` (desktop). Zero-touch reauth: a `.env`
there with `MONARCH_COOKIES` (or email/password/MFA secret) is the final
ensure_authenticated fallback. Cross-device auth: `.\build.ps1 -IncludeAuth`
bundles auth files into the package's `data\bootstrap\` (opt-in; secrets ship
in the uploaded zip), which new sessions copy into their persistent dir on
first run.

## Invariants (don't break these)

- **`gql==4.0`** is pinned transitively by `monarchmoneycommunity==1.4.0`. Never
  override it.
- Scripts print **compact Markdown only** — never raw JSON/row dumps. Analysis
  happens by querying the SQLite cache in the persistent data dir directly.
- **No secrets in stdout** — never print password, session token, or MFA secret.
  Option B credentials live only in the persistent data dir (git-ignored).
- Auth failures are **fatal** — classify and exit 1; never retry a dead session.
- The skill runs in Anthropic's Linux/UTF-8 container; `fetch.py` reconfigures
  stdout to UTF-8 so the ✓/⚠/↑ glyphs don't crash a cp1252 Windows console.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest tests\test_cache.py tests\test_auth.py tests\test_fetch.py -v
```

Quick offline check (no credentials needed):

```powershell
.venv\Scripts\python.exe monarch-money\scripts\fetch.py accounts --mock --db $env:TEMP\demo.db
```
