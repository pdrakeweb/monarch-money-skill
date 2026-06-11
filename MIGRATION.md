# Migration: monolithic monarch-money → three skills

Splitting `monarch-money` (v0.9.0) into three independently-installable skills,
each in its own repo, each buildable to an uploadable `.zip` via the skeleton's
`build.ps1`/`build.sh`. They compose through two stable contracts (cache schema,
namespaced `profile.json`) defined in `contracts/CONTRACTS.md`.

**For users upgrading from monolithic v0.8/0.9:** upload all three zips via
Settings → Capabilities → Skills. The first run of any skill migrates your
`profile.json` to schema 2 in place (every value preserved). The cache is
unchanged (schema_version 2) — no re-sync required.

## What moves where (route map)

| From v0.9.0 | → Repo | Notes |
|-------------|--------|-------|
| `setup_auth.py`, auth options A–D, `persistence.md` | **monarch-money** | unchanged |
| `monarch_client.py` (classifier incl. §A2 display-type map, §A3 charitable; GuardedMonarch write policy; error classes; MonarchCache + schema migration) | **monarch-money** | unchanged |
| `cache_writer.py` (all §B: B1 `--from-investments`, B2 combined cashflow, B3 four-bucket recurring, B4 unwrap/pagination, B5 hidden-account doc) | **monarch-money** | unchanged |
| `fetch.py` minus reports | **monarch-money** | keeps auth-status/accounts/holdings/transactions/budgets/cashflow/recurring/sync-all/status/clear-cache/version; `dashboard`/`report`/`reports`/`profile` become stubs pointing to the consumer skills |
| `skill_cache.py`, `skill_common.py`, `skill_schema.py`, build/deploy/validate (skeleton) | **all three** | shared skeleton; each repo carries its own copy |
| `contracts/` (`profile.py`, `cache_contract.py`, `CONTRACTS.md`) | **all three** | vendored verbatim |
| `reports.py`: net-worth, allocation, asset-location, concentration, savings-rate, expenses, emergency-fund, tlh + the expense-classification engine (transfer/one_time/lumpy/recurring) + Net Worth/Net Means framing + §A1 excluded-authoritative + §A4 income-churn | **personal-finance** | becomes `pf.py`; owns the `dashboard` composer |
| `reports.py`: fire-progress, withdrawal (static/guardrails/**tpaw** §C2–C3), life-value + the TPAW/ABW engine + Monte Carlo + §C4 strategy-aware findings | **retirement** | becomes `retire.py` |
| §2b interview (finance.* + identity) | **personal-finance** | |
| §2b interview (retirement.*) | **retirement** | |
| **NEW** Social Security estimator (3b) | **retirement** | `social_security.py` — 3-tier (paste / computed top-35 AIME + bend points / flagged default), trust-fund haircut toggle, web-search for current params |
| **NEW** retire-age comparison (3c) | **retirement** | `retire.py compare --ages 46,50` → self-contained HTML; per-age SS recomputed by stop-work age |

## Spec §A–D routing (already implemented in v0.9.0; relocated by the split)

- **A1** (excluded bucket authoritative), **A4** (income-churn exclusions) → **personal-finance**
- **A2** (MCP display-type map), **A3** (charitable keywords / type Other), **all of §B** → **monarch-money**
- **§C** (TPAW/ABW + MC + strategy-aware findings) → **retirement**
- **§D** (docs) → split across the three SKILL.md / references files accordingly

## Status

- [x] Shared contracts (`contracts/profile.py`, `cache_contract.py`, `CONTRACTS.md`) — built + tested (`tests/test_contracts.py`).
- [ ] **monarch-money-skill** repo (data layer, v1.0.0): slim fetch.py + stubs, vendor contracts, SKILL.md routing, build/tests/push.
- [ ] **personal-finance-skill** repo (v1.0.0): port 8 reports + expense engine + dashboard composer + finance/identity interview.
- [ ] **retirement-skill** repo (v1.0.0): port fire/withdrawal/life-value + TPAW; NEW `social_security.py`; NEW `compare` HTML report; retirement interview.

Acceptance numbers to reproduce (from the 2026-06-11 live session, to be
captured as fixtures under each repo's `tests/fixtures/`): taxable
$1,318,595 / trad $920,748 / roth $579,076 / hsa $115,123; Net Means
$3,583,710 incl. $205,616 DAF; investable $2,954,661 (stable when the DAF
balance changes).
