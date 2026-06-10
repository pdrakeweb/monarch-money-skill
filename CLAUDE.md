# CLAUDE.md — monarch-money

## ⛔ STOP — DEPLOYMENT REQUIRED AFTER ANY EDIT

After editing ANY file in `monarch-money\`, run:

```powershell
.\deploy.ps1
```

Then **restart Claude Desktop**.

`deploy.ps1` does NOT refresh the running skill automatically — the deployed copy
is a separate artifact. Edit source → `deploy.ps1` → restart Claude Desktop.
Skip this and you are testing stale code.

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
