# Shared contracts — monarch-money skill family

Three skills compose through **two stable contracts**. Any subset may be
installed; each skill degrades gracefully and names what's missing.

- **monarch-money** — data layer. The ONLY writer of Monarch data into the cache.
- **personal-finance** — analysis & reporting. Reads cache + profile.
- **retirement** — planning engine (TPAW, Social Security, retire-age compare).

Copy this file and the two modules (`profile.py`, `cache_contract.py`) verbatim
into each repo's `scripts/`. Improve them HERE, then re-copy — do not fork.

## 0. Where the shared data lives

`monarch_cache.db` and the namespaced `profile.json` live in ONE **family
shared dir** that every member resolves identically — **monarch-money's own
persistent data dir**. A consumer must NOT use its own
`resolve_data_dir("<own-skill>")`: that dir is empty because monarch-money
never writes there. Resolve the shared dir through the cache contract:

```python
from cache_contract import shared_data_dir
data_dir = shared_data_dir(SKILL_DIR)   # identical path for producer + consumers
```

Order: `$MONARCH_MONEY_FAMILY_DIR` (relocate the whole family) >
`$MONARCH_MONEY_DATA_DIR` (monarch-money's own override) >
`~/.claude-skills/monarch-money/` (desktop) or
`/mnt/user-data/.claude-skills/monarch-money/` (cloud). `config-export` reads
`profile.json` from this dir, so the family shares ONE exported profile — not
one per skill. Each skill still keeps its OWN private store (the CFC, the goals
ledger, the skill's SkillCache) under its own `resolve_data_dir`; only the
cache and the profile are shared.

## 1. Cache contract (owner: monarch-money)

- SQLite `monarch_cache.db` in the family shared dir (§0). Tables:
  `accounts`, `account_tax_buckets`, `holdings`, `transactions`, `budgets`,
  `cashflow`, `recurring`, `net_worth_history`, `account_history`, plus `meta`.
- `meta.schema_version` records the cache version. **Consumers open read-only
  and check it on open** (`cache_contract.open_cache`), refusing with a
  user-facing message ("cache schema vX, this skill needs >= vY — update the
  monarch-money skill") instead of failing cryptically. Current: **v2**.
- **Only monarch-money writes Monarch data.** The single exception is the
  `account_tax_buckets.auto_classified = 0` user-correction path — analysis
  metadata, writable by any skill via `open_cache(writable_tax_buckets=True)`.
  Even then, callers touch no other table.
- monarch-money bumps the cache schema **only with a migration**
  (`_migrate_schema` in its MonarchCache); consumers raise their
  `MIN_SCHEMA_VERSION` only when they start needing a newer table/column.

```python
from cache_contract import open_cache, CacheUnavailable
try:
    conn = open_cache(data_dir, min_schema=2)   # read-only
except CacheUnavailable as e:
    print(e)        # name what's missing; degrade gracefully
```

## 2. Profile contract (owner: personal-finance; extended by retirement)

Single namespaced `profile.json` (schema 2) in the shared data dir:

```jsonc
{
  "schema": 2,
  "profile_complete": false,
  "identity":   { "birth_year": null, "state": null, "filing_status": null },
  "finance":    { /* expense classification, lumpy_expenses, essential_categories,
                     allocation targets, emergency fund, net_means_mapping,
                     life_value_categories, life_value_discount_rate_pct,
                     anticipated_changes, legacy_intent,
                     charitable: { annual_giving, daf_balance_source: "cache|manual",
                                   daf_growth_real_pct } */ },
  "retirement": { /* retirement_age, life_expectancy_age, withdrawal_strategy
                     (static|guardrails|tpaw), swr_pct, spending_tilt_pct,
                     expected_real_return_pct, equity_vol_pct, annual_new_savings,
                     income_streams, social_security, retirement_expense_deltas,
                     retirement_trad_tax_rate, retirement_ltcg_rate,
                     relocate_no_income_tax_state,
                     confidence_band: { floor_annual, max_p_below } */ }
}
```

- **Migration:** `profile.load_profile(data_dir)` migrates a flat v0.8/0.9
  profile to schema 2 on first load, preserving every value, and persists the
  upgrade in place. Routing: `birth_year`/`state`/`filing_status` → identity;
  the retirement-planning keys → retirement; everything else → finance.
- **Read whole, write own.** Each skill reads the full profile (use
  `flat_view()` for backward-compatible flat reads), and writes ONLY its
  section(s) via `save_section(data_dir, section, {...})`, which preserves
  siblings. `identity` is writable by all (last-write-wins).

```python
from profile import load_profile, flat_view, save_section
prof = load_profile(data_dir)             # namespaced, migrated
flat = flat_view(prof)                     # flat dict for report reads
save_section(data_dir, "finance", {"net_means_mapping": "A"})
```

## 3. Sibling detection

A consumer detects an optional sibling by its marker module in the data dir or
its scripts (e.g. personal-finance checks for the retirement skill before
rendering fire-progress/withdrawal/life-value sections; if absent it prints one
line: "Retirement projections require the retirement skill.").

## 4. Versioning across the family

Each `SKILL.md` states the cache `schema_version` and profile `schema` it
requires. Upgrading from monolithic monarch-money v0.8.x: upload all three zips
via Settings → Capabilities → Skills; the first run of any skill migrates
`profile.json` to schema 2 in place.
