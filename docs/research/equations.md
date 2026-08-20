# Every equation from the six posts, and the part that implements it

The six Instagram posts read 2026-08-20 (assessment: `instagram-2026-08-20.md`)
carried real formulas. That file judged them; **this file is the specification** —
each equation written out exactly, with the part that owns it named beside it, and
the ones nothing owns yet marked as unclaimed.

Written because the assessment recorded most of these in shorthand and the parts
pointed at nothing. A part built from shorthand is a part that was guessed at —
the same failure already corrected once for order-flow entropy.

**Provenance is marked on every line.** `[post]` came from the slides or frames.
`[standard]` is textbook maths the post named without writing out, included so the
part is implementable, and labelled so it is never mistaken for something the post
taught.

---

## 1 · Volatility forecast — post `reel/Dby33G4thmA`

**The target and the signal** `[post]`

    target:  E[RV₁₀]                     10-day forward realised volatility
    signal:  E[RV₁₀] > IV₃₀   →  options are pricing too little movement
             E[RV₁₀] < IV₃₀   →  options are pricing too much

**The regression** `[post]` — the transcript only ever said "combine them in a
linear regression"; the final frames spell it out:

    RV̂₁₀,ₜ = β₀
           + β₁  RV₁d,t              1-day realised vol
           + β₂  RV₅d,t              5-day
           + β₃  RV₂₂d,t             22-day, what is typical recently
           + β₄  DownsideRV₅d,t      vol from sell-offs
           + β₅  JumpVar₅d,t         vol from jumps
           + β₆  VV₂₀d,t             vol of vol
           + β₇  ATMIV₃₀d,t          what the option market expects
           + β₈  TermSlope₃₀₋₆₀,t    near-term event pricing
           + β₉  PutSkew₂₅Δ,t        what fear costs
           + β₁₀ SpyRV₅d,t           the market's own vol

**The ten features** `[post]`

    RV₁     = √(252 · Σᵢ rᵢ²)
    RV₅d    = √(252 · (1/5)  · Σⱼ₌₀⁴  variance_{t−j})
    RV₂₂d   = √(252 · (1/22) · Σⱼ₌₀²¹ variance_{t−j})

    DSvar   = Σ_{rᵢ < 0} rᵢ²                    squared negative returns only
    BV      = (π/2) · Σ |rᵢ| · |rᵢ₋₁|           bipower variation
    JumpVar = max(RV − BV, 0)                   RV = Σ rᵢ²

    VV₂₀    = σ( RV₁ over t−19 … t )            stdev of a rolling realised vol
    ATMIV₃₀ : solve  C_market = BS(S, K, T, r, σ)  for σ
    TermSlope = IV₃₀ − IV₆₀
    PutSkew   = IV₂₅Δput − IV_ATM
    RV_spy,5d = √(252 · Var_spy,5d)

**Why each one is there, in the post's own words** `[post]`

| # | feature | what it is asking |
|---|---|---|
| 1 | RV₁ | how much did it move today |
| 2 | RV₅d | does today's vol survive |
| 3 | RV₂₂d | what is typical recently |
| 4 | DownsideRV₅d | is the vol coming from sell-offs |
| 5 | JumpVar₅d | is it jumps rather than diffusion |
| 6 | VV₂₀ | 40% steady and 20%→60% are not the same 40% |
| 7 | ATMIV₃₀ | what the option market expects |
| 8 | TermSlope | near-term event pricing |
| 9 | PutSkew | what fear costs |
| 10 | SpyRV₅d | asset vol, or the whole market's |

**Owned by:** `volatility-feature-builder` → `vol-feature-set`;
`realised-vol-regressor` → `volatility-forecast`; the signal comparison by
`volatility-gap-detector` → `entry-candidate`.

**The constraint that must not be papered over.** Features **7, 8 and 9 need an
options surface.** Spot and futures can compute 1–6 and 10 from candles alone;
they cannot compute implied vol, term structure or skew without an options venue.
With spot and futures the focus (RL-039), **three of the ten features are
unavailable to those two bots**, and the regression they run is a different
regression from the options bot's. That is a real split, and the part must declare
which features it actually had rather than silently zero the missing ones.

*Note on annualisation:* `252` is the equity trading-year constant. Crypto trades
365 days a year, so the constant is wrong here and has to be restated before any
of these numbers mean anything (RL-018, RL-020).

---

## 2 · Mean reversion — post `p/DbPaMI1ndH2`, sheet 1

    Z = (Price − Mean) / StdDev            [post]

    entry:  |Z| > 2                        [post]
    exit:   |Z| < 0.5                      [post]

    Bollinger bands, RSI 70/30             [post] — named, not written out

**When it fails** `[post]` — more useful than the entry rule, and the reason the
regime classifier exists:

- strong trends
- news spikes
- low liquidity
- structural breaks

**Owned by:** `mean-reversion-detector` → `entry-candidate`, gated by
`regime-classifier` → `market-regime`.

---

## 3 · Statistical arbitrage — post `p/DbPaMI1ndH2`, sheet 2

    Spread = A − βB                        [post]

    Ornstein-Uhlenbeck:
      dXₜ = κ(μ − Xₜ) dt + σ dWₜ           [post]

    half-life = ln(2) / κ                  [standard] — the post named half-life
                                            without writing it; this is the OU result

    stationarity tests: ADF, KPSS          [post]
    cointegration:      Engle-Granger, Johansen   [post]

**Reading the parameters:** `κ` is the speed of reversion — how hard the spread is
pulled back to `μ`. The half-life turns that into a holding period, which is what
decides whether a pair is tradeable intraday at all (RL-043: 1m/5m/15m/30m). A pair
with a three-day half-life is real and useless here.

**Was unclaimed until now.** Two parts added 2026-08-20:
`cointegration-pair-finder` → `cointegrated-pair`, and
`spread-reversion-detector` → `entry-candidate`.

---

## 4 · Neural networks in trading — post `p/DbPaMI1ndH2`, sheet 3

No equations. A best-practice list, and it is the method `forecast-scorer`
currently lacks `[post]`:

- walk-forward validation
- time-based splits, never random
- avoid look-ahead bias
- always evaluate out-of-sample **with transaction costs**

**Owned by:** `forecast-scorer`. Recorded as its stated method rather than as a new
part — a scorer with no protocol is the same defect as a part with no equation.

---

## 5 · Regime detection — post `p/DbYieIWmUxW` (OmniPhi frames)

    Hurst exponent H                       [post]

      H < 0.5   mean-reverting
      H = 0.5   random walk                [standard] — the boundary the post implies
      H > 0.5   trending

    observed: H = 0.462 → "mild mean-reversion tendency"     [post]

Reported alongside `RSI(14)`, current drawdown, and hourly volatility annualised.

**The third answer matters as much as the other two** `[post]`: the label
*"Mixed — no clear trending/ranging label"* rather than forcing a call. This is
where `market-regime`'s **mixed = stand down** comes from.

**Owned by:** `regime-classifier` → `market-regime`.

---

## 6 · The Drake decomposition — post `p/DblhZgvkjbZ`

    N = R* × f_p × n_e × f_l × f_i × f_c × L          [post]

The subject is irrelevant; the **pattern** is the find. Take a quantity nobody can
measure directly, break it into a chain of factors that each *can* be estimated,
multiply through — and the uncertainty concentrates visibly in whichever factor is
worst known.

Applied to a trade:

    E[trade] = P(signal is real)
             × P(entry fills)
             × P(target before stop)
             × avg win
             − (1 − P(target before stop)) × avg loss
             − costs

Every factor is separately measurable from the ledger, so the product says **which
one** is sinking the expectancy rather than only that it is poor.

**Owned by:** `expectancy-decomposer` → `expectancy-breakdown`.

---

## What carries no equation, deliberately

- **Post 2, the memory tiers** (`p/Db9xNeMmJ5g`) is structural, not mathematical.
  Its rules — episodic append-only and written on resolution, semantic upserted
  under a closed-set key, procedural always injected and never searched and capped
  — are already the three `knowledge` parts. Nothing to add here.
- **Post 4, the ten repositories** carries no maths. It lives in
  `features.json → upstream_dependencies`.
- **Five of the six equations in post 6** — Schrödinger, Riemann, Euler,
  Einstein, Navier-Stokes — carry nothing usable for this project, and the two
  indirect links that exist (Euler → Fourier on price series; zeta-zero statistics
  → random matrix theory for cleaning correlation matrices) are **not made by that
  post.** Claiming them from it would be dressing up a guess.

## Every performance number in those posts is still decoration

`+24.37%`, `+128.47%`, `63.42%` win rate, Sharpe `1.89`, `+67.89%`, `+78.41%`,
`532` trades. Rendered mockups on marketing slides. None is a benchmark, none
reaches the design, and none appears anywhere in this file. The post that taught
the most — the volatility reel — made no profit claim at all.
