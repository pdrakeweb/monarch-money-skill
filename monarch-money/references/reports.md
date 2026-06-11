# Boglehead / FIRE reports — methodology & vocabulary

The skill computes ten reports plus a dashboard from the cached data (no API
calls). `python scripts/fetch.py dashboard` prints all summaries with links;
`report <key>` prints one full report; `report all` prints every full report
in one call (each delimited by `<!-- report:<key> -->`). Assumptions come from
`profile.json` in the persistent data dir (see "Profile" below).

## Vocabulary (so the numbers mean what the user expects)

- **FI / FIRE** — Financial Independence / Retire Early. **FI number** = the
  portfolio that sustainably funds your spending.
- **4% rule / 25× / SWR** — the Trinity-study safe withdrawal rate. FIRE
  number = annual expenses ÷ SWR. At 4% that's 25×; many in 2026 use 3.5%
  (28.6×) or 3% (33×) for a longer horizon.
- **Lean / Fat / Coast / Barista FIRE** — Lean = low-spend (~$25–45k);
  Fat = high-spend (≥$100k); **Coast FIRE** = enough invested that, with *no
  new contributions*, compounding reaches your FIRE number by your target
  retirement age; Barista = part-time income bridges the gap.
- **Investable / liquid net worth** — cash + investment accounts that fund the
  withdrawal. Excludes primary-home equity (you can't spend the roof).
- **Savings rate** — (income − expense) ÷ income. The single biggest driver of
  time-to-FI — more than income or returns.
- **Asset allocation** — the stock/bond/intl/REIT split. **Three-fund
  portfolio** = total US + total international + total bond.
- **Asset location** — *which account type* holds each asset for tax
  efficiency (distinct from allocation).
- **Tax bucket** — taxable / traditional (tax-deferred) / roth (tax-free) /
  hsa. See the `account_tax_buckets` table.
- **Expense ratio (ER)** — annual fund fee. The Boglehead edge: minimize it.
- **5/25 rebalancing bands** (Larry Swedroe) — rebalance an asset class when
  it drifts an absolute 5 percentage points OR a relative 25% from target.
- **Wash sale** — buying a substantially identical security within ±30 days of
  realizing a loss disallows that loss (applies across ALL accounts, incl.
  IRAs). Critical for TLH.
- **Sequence-of-returns risk** — poor early-retirement returns can sink a
  portfolio even at a "safe" average; why SWR < expected return.

## The ten reports

| Key | Report | Computes |
|-----|--------|----------|
| `net-worth` | Net Worth Statement | Assets − liabilities by type; investable subtotal |
| `allocation` | Asset Allocation | Stock/bond/intl/REIT % vs three-fund target; 5/25 bands; blended ER fee drag |
| `asset-location` | Asset Location | Holdings × tax bucket grid; flags bonds/REITs in taxable |
| `fire-progress` | FIRE Progress | FIRE number (25×/28.6×), % to FI, Lean/Your/Fat tiers, gap |
| `withdrawal` | Safe Withdrawal & Coast FIRE | 4/3.5/3% annual income; Coast FIRE number given age |
| `savings-rate` | Savings Rate | Trailing-N-month gross savings rate, per-month table |
| `expenses` | Expense Analysis | Essential vs discretionary split, by-category spend |
| `emergency-fund` | Emergency Fund | Cash ÷ monthly expenses = months of coverage |
| `tlh` | Tax-Loss Harvesting | Positions below basis; harvestable loss; wash-sale caveat |
| `concentration` | Portfolio Concentration | Top position %, single-stock exposure, per-ticker weights |

### Methodology notes
- **Investable assets** = cash (checking/savings/depository) + investment/
  brokerage balances > 0. Excludes real estate, credit, loans.
- **Annual expenses** = avg monthly `cashflow.expense` × 12 (falls back to
  transactions; `annual_expenses_override` in profile wins). Transfers excluded.
- **Asset class** is inferred from ticker (a built-in Boglehead fund map) then
  name heuristics; unrecognised individual tickers count as US equity. Verify
  for exotic holdings.
- **Expense ratios** use a small built-in table of common funds; unmapped
  funds are simply excluded from the fee figure (stated as "mapped funds").
- **Tax buckets** come from the auto-classifier (correctable — see SKILL.md
  §5). Asset-location quality depends on them being right.

## Profile (`<data_dir>/profile.json`, all optional)

```json
{
  "swr_pct": 4.0,
  "birth_year": 1985,
  "retirement_age": 65,
  "expected_real_return_pct": 5.0,
  "target_us_stock_pct": 48.0,
  "target_intl_stock_pct": 32.0,
  "target_bond_pct": 20.0,
  "lean_annual_expenses": 40000,
  "fat_annual_expenses": 100000,
  "annual_expenses_override": null,
  "essential_categories": ["groceries", "utilities", "transportation", "..."]
}
```

- Without `birth_year`, Coast FIRE explains itself but can't compute (asks for
  age). Set it to unlock the Coast FIRE number.
- `target_*_pct` should sum to ~100 and encode the user's IPS allocation; the
  defaults are an 80/20 stock/bond with intl at 40% of equities.
- Get the data-dir path from `fetch.py status` (the `Database:` line); write
  `profile.json` beside the cache db.

## Rendering the dashboard (for Claude)

`fetch.py dashboard` returns a KPI header table + one summary block per report,
each with a `report <key>` link. To build the deliverable:
1. Run `fetch.py dashboard` (summaries + KPIs) and `fetch.py report all`
   (every full body, split on `<!-- report:<key> -->`).
2. Render ONE self-contained HTML artifact: a KPI strip at top, then a card per
   report showing its summary, each card linking (in-page accordion/anchor) to
   the embedded full report. Keep it self-contained — no external calls.
3. Use the theme-factory skill if the user wants it styled.
Numbers are already computed; don't recompute in the artifact — embed them.
