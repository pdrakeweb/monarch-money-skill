# Boglehead / FIRE reports — methodology, vocabulary, profile schema

Eleven reports + a dashboard, computed from the cache (no API calls).
`fetch.py dashboard` = summaries + links; `report <key>` = one full report;
`report all` = every full report (split on `<!-- report:<key> -->`).
Assumptions come from `profile.json` in the persistent data dir
(`fetch.py status` → `Database:` dir; `fetch.py profile` shows status).

## Core principle: outflows are classified, not summed

Every outflow is exactly one of four buckets. **Investment buys, contributions,
and capital outlays are NOT consumption** — counting them as expenses is what
produced the bogus −29% savings rate and $14.1M FIRE number.

| Bucket | What | Treatment |
|--------|------|-----------|
| `transfer` | Investment buys, 401k/IRA/HSA/brokerage contributions, debt principal, internal transfers, CC payments | Excluded from expenses/budget/FIRE; counted as **savings** |
| `one_time` | Non-recurring capital outlays (boat, remodel, big flagged purchases) | Excluded from run-rate & FIRE; shown separately as "capital outlays" |
| `lumpy` | Periodic infrequent (car ~8y, roof ~25y, HVAC, appliances) | **Amortized**: `amount / frequency_years` added as a sinking-fund allowance; the spike itself is `one_time` |
| `recurring` | Everything else — the real ongoing budget | Included at face value |

Classifier order: `transfer` → `one_time` → `recurring` (substring match on
category; on merchant only for keywords ≥6 chars, so "buy" doesn't catch
"Best Buy"). `lumpy` is profile-driven, not per-transaction.

## Vocabulary

- **FI / 25× / SWR** — FIRE number = (gross) retirement budget ÷ SWR. 4%→25×.
- **Horizon-derived SWR** (Pfau/Kitces) — 4% @30y, ~3.5% @40y, ~3.25% @45y+;
  4% is too aggressive for a long early-retirement horizon. Derived from
  `life_expectancy_age − retirement_age` unless `swr_pct` is set.
- **Retirement budget** — current recurring + lumpy allowances − expenses that
  end at retirement (work costs, tuition, mortgage P&I if paid off) +
  healthcare bridge (pre-65 ACA) + intended extra travel. Built on RETIREMENT
  spending, not working-life spending.
- **Tax-aware FIRE number** — spend budgets are after-tax; trad withdrawals are
  taxed. Gross up the trad/LTCG-funded share: `gross = budget / (1 − eff_tax)`,
  `eff_tax = trad_share·trad_rate + ltcg_share·ltcg_rate`. Roth/HSA/cash
  withdraw tax-free.
- **TPAW / ABW** (Ben Mathew's Total Portfolio Allocation and Withdrawal) —
  amortization-based variable withdrawal on the **total portfolio** (liquid
  assets + PV of future income streams). Refs: Bogleheads wiki "Total portfolio
  allocation and withdrawal" + "Amortization based withdrawal formulas",
  Mathew's thread t=331368.
  - Horizon `n = life_expectancy_age − retirement_age`.
  - ABW rate with spending tilt `g`: `r_eff = (1+r)/(1+g) − 1`;
    `rate = r_eff / (1 − (1+r_eff)^−n)` (limit `1/n` at `r_eff=0`), `r =
    expected_real_return_pct`. Negative `g` front-loads (declining real
    spending — die-with-zero); positive `g` back-loads.
  - Total portfolio = liquid balance + Σ PV(remaining income streams at
    `income_discount_rate_pct`). Year-1 spending = `rate × total portfolio`.
  - **Liquidity constraint:** portfolio draw = `min(spending − current-year
    stream income, balance)` — never borrow against future income (Mathew's
    flagged TPAW liquidity risk).
  - ABW **never depletes** (it re-amortizes the remaining balance each year);
    spending flexes with markets instead. The static-SWR "runs out of money"
    framing does not apply.
  - **Monte Carlo bands:** ≥2,000 lognormal-real-return paths (arithmetic mean
    `μ_a ≈ μ_g + σ²/2`, `σ = equity_vol_pct`), seeded. Accumulate
    `annual_new_savings` to retirement, then re-amortize each year. Reports
    spending percentiles (p5/p25/p50/p75/p95) at 5-year ages, portfolio
    percentiles at retirement, and `P(no year below 2×lean)` / `P(no year
    below fat)` — mapping a "must never cut below X" statement onto ABW's
    spending bands. stdlib only; runs in <2s.
- **Guyton-Klinger guardrails** — start ~0.5–1pp above static SWR with ±20%
  bands: cut ~10% if the withdrawal rate rises >band above initial, raise ~10%
  if it falls >band below. Higher early income, accepts real cuts in bad
  sequences. Serves front-loaded life-value.
- **Coast FIRE** — portfolio that compounds to FI with no new contributions by
  the target age.
- **Life-value / die-with-zero** (Perkins) — maximize time-discounted value of
  experiences, giving, relationships under a never-outlive-means constraint;
  earlier is worth more (positive discount), so front-load. Terminal estate is
  not the objective; a ballooning spend-down projection is a *finding*.
- **Net Worth vs Net Means** — accounting figure (assets owned, excludes DAF)
  vs life-value figure (includes directed-but-encumbered DAF as a
  charity-committed line). Label mapping via `net_means_mapping` ("A" default:
  Net Means = life-value; "B" swaps).
- **5/25 bands, wash sale, tax-efficient placement** — as before.

## The eleven reports

| Key | Report | Notes |
|-----|--------|-------|
| `net-worth` | Net Worth Statement | Net Worth / Net Means pair; DAF as encumbered line |
| `allocation` | Asset Allocation | three-fund vs target, 5/25 bands, ER fee drag |
| `asset-location` | Asset Location | tax-efficient placement; flags bonds in taxable |
| `fire-progress` | FIRE Progress | retirement budget × tax gross-up ÷ horizon SWR; tiers |
| `withdrawal` | Safe Withdrawal & Dynamic Spending | horizon SWR + Guyton-Klinger guardrails + **TPAW/ABW** (schedule + MC bands) + Coast FIRE |
| `savings-rate` | Savings Rate | consumption rate + capital-deployed rate; one-time months flagged |
| `expenses` | Expense Analysis | 3 sections: recurring / lumpy allowances / excluded capital+transfers |
| `emergency-fund` | Emergency Fund | contextual (cash vs accessible liquid; stability-aware target) |
| `tlh` | Tax-Loss Harvesting | underwater positions; blended-basis + wash-sale caveat |
| `concentration` | Portfolio Concentration | single-stock %, annotated with intended sector tilts |
| `life-value` | Life-Value / Die-With-Zero | time-discounted spend, spend-down glidepath, Means/Worth |

## profile.json — full schema (all optional; interview fills it)

```jsonc
{
  "profile_complete": false,           // false → dashboard runs the interview first
  // horizon / SWR
  "swr_pct": null,                      // null → horizon-derived; set = honored
  "birth_year": null,                   // for Coast FIRE + glidepath
  "retirement_age": 65,
  "life_expectancy_age": 95,            // drives horizon SWR
  "expected_real_return_pct": 5.0,
  "withdrawal_strategy": "static",      // "static" | "guardrails" | "tpaw"
  // TPAW / ABW (Part C):
  "spending_tilt_pct": 0.0,             // g, real %/yr; negative = front-load
  "income_streams": [                   // PV'd as bonds into the total portfolio
    {"name": "Social Security", "annual": 48000, "start_age": 70, "end_age": null}
  ],
  "annual_new_savings": null,           // real $/yr to retirement; null → ASK (A4)
  "equity_vol_pct": 20.0,               // MC return vol
  "income_discount_rate_pct": 2.0,      // real bond-rate PV for income streams
  "guardrail_initial_swr_pct": 4.5,
  "guardrail_band_pct": 20,
  // allocation targets (three-fund)
  "target_us_stock_pct": 48.0, "target_intl_stock_pct": 32.0, "target_bond_pct": 20.0,
  // FIRE tiers / override
  "lean_annual_expenses": 40000, "fat_annual_expenses": 100000,
  "annual_expenses_override": null,     // authoritative current/budget base if set
  // expense classification
  "transfer_categories": ["buy","sell","investment","contribution","401k","403b","ira","roth","hsa contributions","brokerage","transfer","credit card payment","principal","deposit","withdrawal"],
  "one_time_categories": ["sailboat acquisition","boat","remodel","renovation","home improvement"],
  "one_time_transaction_keywords": [],
  "lumpy_expenses": [
    {"name":"Vehicle replacement","amount":60000,"frequency_years":8},
    {"name":"Roof","amount":25000,"frequency_years":25},
    {"name":"HVAC","amount":12000,"frequency_years":18},
    {"name":"Appliances","amount":8000,"frequency_years":12}
  ],
  "essential_categories": ["groceries","utilities","transportation","housing","home","rent","mortgage","health","insurance","childcare","debt","loan","bills","medical","auto"],
  // retirement budget
  "retirement_expense_reductions": ["business travel","commute","parking & tolls","tuition","childcare"],
  "retirement_expense_deltas": {"healthcare_bridge_annual":24000,"extra_travel_annual":15000,"mortgage_payoff_age":null},
  "retirement_trad_tax_rate": 0.18,     // effective ordinary rate in retirement
  "retirement_ltcg_rate": 0.08,         // effective LTCG rate (0%-bracket harvesting)
  "relocate_no_income_tax_state": false,
  // emergency fund
  "income_stability": "stable",         // "stable" | "variable"
  "emergency_fund_months_target": 3,
  "count_taxable_as_liquid_for_ef": true,
  // interview capture
  "sector_views": {"1y":{},"5y":{},"10y":{}},
  "anticipated_changes": [],            // [{horizon_years,name,amount,kind:"one_time"|"lumpy"}]
  // life-value
  "legacy_intent": "spend_down",        // "spend_down" | "leave_estate"
  "life_value_categories": ["travel & vacation","sailing","charity","entertainment & recreation","gifts"],
  "life_value_discount_rate_pct": 5.0,
  "net_means_mapping": "A"              // "A": Net Means=life-value; "B": swap
}
```

Defaults produce correct behavior unattended (transfers excluded, SWR
horizon-derived). `annual_expenses_override` wins for the budget base.
`relocate_no_income_tax_state` → set the trad/LTCG rates to federal-only.

## Limits
- Monarch is **position-level only** — no lots/acquisition dates. The TLH
  report keeps the blended-basis + wash-sale caveat; don't fabricate lot
  precision.
- Asset class & fund ER come from built-in maps; unmapped funds are excluded
  from the ER figure (not guessed).

## TPAW worked example (the field case)

Investable $2.95M, currently age 50, retire at 50→92 (`n=42`), `r≈4.4%` real,
`g=−1.5%`, 5y × $250k new savings before retirement, Social Security $48k/yr at
age 70. Then:
- ABW rate = `r_eff/(1−(1+r_eff)^−42)` with `r_eff=(1.044/0.985)−1≈0.060` →
  **≈6.6%**.
- Projected investable at retirement ≈ $2.95M·(1.044)^5 + FV($250k×5y) ≈ **$5.0M**.
- Total portfolio = $5.0M + PV(SS at 50, 2% real) → year-1 spend ≈
  `6.6% × (≈$5.0M + PV(SS))` ≈ **$348k** gross.
A negative tilt schedules spending to **decline** in real terms over
retirement — front-loading life-value while the portfolio still self-corrects
each year via re-amortization.

## Rendering the dashboard (for Claude)
Run `fetch.py dashboard` (summaries + KPIs) and `fetch.py report all` (bodies),
then render ONE self-contained HTML artifact: KPI strip + a card per report,
each linking (accordion/anchor) to its embedded full body. Numbers are
precomputed — embed them. Use theme-factory for styling if asked.
