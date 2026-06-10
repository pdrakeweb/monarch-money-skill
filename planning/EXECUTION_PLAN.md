# monarch-money skill — Execution Plan

## Phase 0 — Repo Init
- git init in monarch-skill\
- Verify .gitignore (data\, *.pickle, *.db, credentials.json, *.zip, __pycache__)
- Create venv, pip install monarchmoneycommunity==1.3.2 pyotp
- Confirm gql==4.0 installed
- Commit: "chore: repo scaffold"

## Phase 1 — Infrastructure (tests before implementation)
- Write tests\test_cache.py FIRST (all cases from spec)
- Implement scripts\monarch_client.py (MonarchCache + MonarchClient + error classes)
- Write tests\test_auth.py FIRST
- Implement scripts\setup_auth.py
- Run tests: pytest tests\test_cache.py tests\test_auth.py -v — all pass

## Phase 2 — Fetch layer (tests before implementation)
- Create tests\fixtures\ with anonymised JSON for all 6 subcommands
- Write tests\test_fetch.py FIRST (all --mock mode tests)
- Implement scripts\fetch.py with all subcommands
- Run tests: pytest tests\test_fetch.py -v — all pass

## Phase 3 — References
- Write references\api_surface.md (24 read methods, 5 SQL patterns, lot-level limit)

## Phase 4 — SKILL.md
- Write monarch-money\SKILL.md with max-trigger description and full docs

## Phase 5 — Integration tests
- Write tests\test_integration.py with CI-skip guard

## Phase 6 — Build + deploy scripts
- Write build.ps1 (validate folder name = SKILL.md name, package zip)
- Write deploy.ps1 (mirror ForecastSkill deploy.ps1 target path)

## Phase 7 — Docs polish
- Complete README.md
- Write CLAUDE.md (deploy reminder)
- Write .gitignore, requirements.txt
- Run full test suite + final checklist
- Tag v0.1.0

## Final checklist
- [ ] pytest tests\test_cache.py tests\test_auth.py tests\test_fetch.py -v — all pass
- [ ] python scripts\fetch.py accounts --mock — works without credentials
- [ ] git grep -i "password|secret|token" monarch-money\ — zero hits in tracked files
- [ ] SKILL.md frontmatter name = "monarch-money" (matches folder)
- [ ] gql==4.0 not overridden
- [ ] monarch-money.zip built and data\ inside is empty
