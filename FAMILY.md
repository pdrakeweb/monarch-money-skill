# The monarch-money skill family

Three composable Claude skills that split personal-finance work into clean
layers. Each installs and runs independently; together they cover data →
consumption → wealth. All three are built from
[claude-skill-skeleton](https://github.com/pdrakeweb/claude-skill-skeleton) and
share two contracts (see `references/CONTRACTS.md` in any of them).

| Skill | Repo | Layer | Owns |
|-------|------|-------|------|
| **monarch-money** | [monarch-money-skill](https://github.com/pdrakeweb/monarch-money-skill) | Data | Monarch auth + sync, the SQLite cache (`monarch_cache.db`), tax-bucket classification, exclusions |
| **personal-finance** | [personal-finance-skill](https://github.com/pdrakeweb/personal-finance-skill) | Consumption | Spending, budget, cashflow, savings-rate, recurring, emergency-fund over a source-agnostic **Canonical Financial Context** (`fh_cache.db`) |
| **investing** | [investing-skill](https://github.com/pdrakeweb/investing-skill) | Wealth | Net worth, allocation, asset location, FIRE, withdrawal (TPAW), TLH, concentration, Social Security, goals, retire-age comparison, die-with-zero |

## How they compose

```
                ┌─────────────────────────────────────────────┐
                │  monarch-money  (the ONLY thing that syncs)  │
                │  writes monarch_cache.db + profile.json      │
                └───────────────┬─────────────────────────────┘
                  read-only      │  (immutable URI + schema_version guard)
        ┌──────────────────────┴───────────────────────┐
        ▼                                               ▼
┌───────────────────────────┐               ┌───────────────────────────┐
│ personal-finance          │               │ investing                 │
│ ingests Monarch → CFC,    │               │ reads holdings/accounts/  │
│ OR documents Claude reads │               │ tax-buckets/transactions  │
│ natively, OR manual Q&A   │               │ directly (sanctioned       │
│ → fh_cache.db (canonical) │               │ consumer exception)        │
└───────────────────────────┘               └───────────────────────────┘
        │                                               │
        └──────────────► shared profile.json ◄──────────┘
            (namespaced: identity / finance / retirement; each skill
             writes only its own section, last-write-wins on identity)
```

### Two shared contracts (`CONTRACTS.md`, vendored verbatim into each skill)

1. **`profile.py`** — one namespaced `profile.json` (schema 2) with
   `identity` / `finance` / `retirement` sections. A flat v0.8/0.9 profile is
   migrated in place. Consumers read the whole file (or a `flat_view`) and write
   only their own section with `save_section(...)`, so siblings never clobber
   each other.
2. **`cache_contract.py`** — how consumers open `monarch_cache.db`:
   **read-only** (immutable `file:…?mode=ro` URI) with a `schema_version` guard
   that fails with a user-facing message rather than cryptically. The one
   sanctioned write — a user correcting a mis-classified account
   (`account_tax_buckets.auto_classified=0`) — stays with monarch-money.

## Which skill answers which question

- "How much did I spend on dining?" / "What's my savings rate?" / "Am I
  overspending?" → **personal-finance**
- "Is my portfolio allocated right?" / "When can I retire?" / "What's my safe
  withdrawal rate?" / "Should I do a Roth conversion?" / "How big will Social
  Security be?" → **investing**
- "Sync my accounts" / "Re-pull transactions" / "Fix this account's tax
  treatment" → **monarch-money**

personal-finance works **without** Monarch too (Claude reads uploaded
statements natively, or manual entry), so the consumption layer isn't gated on
the data layer. investing needs the Monarch cache for holdings.

## Install order

1. **monarch-money** first if you use Monarch (it produces the cache + profile).
2. **personal-finance** and **investing** in any order; each detects whether the
   Monarch cache exists and falls back / instructs accordingly.

## Conventions shared across the family

- Compact Markdown to stdout; analysis by querying the SQLite cache directly.
- Fatal errors → `{"error","message"}` JSON, exit 1.
- **User config survives `clear-cache`**: monarch-money/personal-finance
  exclusions and investing goals live outside the cleared table set.
- Dual version stamps (`skill_schema.SKILL_VERSION` == SKILL.md `version:`);
  builds refuse to package on mismatch.
- Server-synced skills (uploaded via claude.ai Settings) update on all devices
  only by re-uploading the built zip — see each repo's `CLAUDE.md`
  (three-location model).
