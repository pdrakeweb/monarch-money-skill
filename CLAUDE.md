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

1. `monarch-money\scripts\monarch_client.py` → `SKILL_VERSION = "X.Y.Z"`
2. `monarch-money\SKILL.md` frontmatter → `version: "X.Y.Z"`

Before committing any change under `monarch-money\`:
- **patch** (X.Y.Z+1): bug fixes, doc/instruction tweaks, classifier rules
- **minor** (X.Y+1.0): new commands, new auth options, new capabilities
- **major** (X+1.0.0): breaking changes — CLI flags removed/renamed, SQLite
  schema changes, auth-flow changes that require user re-setup

Update both files, commit, then tag: `git tag vX.Y.Z`. The user verifies the
installed version by asking the skill's Claude, which runs
`python scripts/fetch.py version` — if that lags the repo, the deploy is stale.

## Invariants (don't break these)

- **`gql==4.0`** is pinned transitively by `monarchmoneycommunity==1.3.2`. Never
  override it.
- Scripts print **compact Markdown only** — never raw JSON/row dumps. Analysis
  happens by querying `data/monarch_cache.db` directly.
- **No secrets in stdout** — never print password, session token, or MFA secret.
  Option B credentials live only in `data/credentials.json` (git-ignored).
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
