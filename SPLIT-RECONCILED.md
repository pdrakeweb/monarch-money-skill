# Skill split — reconciled plan (supersedes MIGRATION.md boundaries)

Reconciles two designs: this session's `personal-finance`/`retirement` plan and
the desktop dispatch brief's `financial-health`/`investing` + CFC plan.

## Locked decisions (2026-06)

- **Three skills:** `monarch-money` (data) · `personal-finance` (consumption +
  CFC) · `investing` (wealth/portfolio/retirement).  ← user chose the names
  personal-finance + investing.
- **Canonical Financial Context (CFC):** YES. `personal-finance` owns a
  source-agnostic `fh_cache.db` so it works WITH or WITHOUT Monarch.
- **Extraction = native Claude, not a parser script.** Claude reads
  PDFs/CSVs/screenshots itself → JSON → a thin persist endpoint writes to the
  CFC. No parsing library.
- **Keep the two new retirement features** (dispatch dropped them) in
  `investing`: the 3-tier **Social Security estimator** (bend points +
  web-searched current-year params + trust-fund-haircut) and the **retire-age
  `compare` HTML report**.
- **Keep the shared contracts** (namespaced `profile.json` + read-only cache
  opener with schema guard) — they complement the CFC: `finance` section →
  personal-finance, `retirement` section → investing; the cache guard
  formalizes investing's sanctioned read of monarch_cache.db (holdings).
- **TPAW bands p5/p25/p50/p75/p95** (already implemented in v0.9.0).
- Dispatch brief's "source repo is empty / implement from scratch" is **stale**
  — the source is the full v1.1.0 implementation; exclusions/analysis exist and
  reports.py is staged in `contracts/`. PORT, don't reimplement.

## Report ownership (clean cut)

- **personal-finance (consumption):** expenses, savings-rate, emergency-fund,
  cashflow, budget, recurring + exclusions + native extraction + finance/
  identity interview.
- **investing (wealth):** net-worth (Net Means), allocation, asset-location,
  concentration, tlh, fire-progress, withdrawal (TPAW), life-value + goals
  framework + SS estimator + retire-age compare + profile interview +
  methodology docs (TPAW human-capital, Roth-vs-trad, asset location, glide).
- **Holdings stay in monarch_cache.db** (investing reads them directly — the
  one sanctioned cross-skill direct read).

## Status

- [x] Shared contracts (profile namespacing + read-only cache) — built, tested.
- [x] monarch-money v1.0.0/v1.1.0 — data layer (slim + exclusions). Tagged.
- [x] **personal-finance CFC foundation** — `fh_cache.py` (CanonicalCache),
      `ingest_monarch.py`, exclusions, contracts, SKILL.md. 18 tests, builds.
- [ ] personal-finance: `ingest.py` (native-extraction persist), `report.py`
      (consumption reports, source→CFC), exclusions CLI (port), interview.
- [ ] investing: scaffold; port wealth reports (source→CFC + monarch holdings);
      goals framework; SS estimator; retire-age compare HTML; methodology docs.
- [ ] Build/test/push all three under pdrakeweb; family README.

Acceptance figures (from the 2026-06-11 session; capture as fixtures): taxable
$1,318,595 / trad $920,748 / roth $579,076 / hsa $115,123; Net Means
$3,583,710 incl. $205,616 DAF; investable $2,954,661 (stable when DAF changes).
