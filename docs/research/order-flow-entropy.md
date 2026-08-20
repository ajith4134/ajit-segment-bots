# Order-flow entropy — the complete specification

The maths behind the three C-08 parts: **order flow state encoder**, **flow entropy
meter**, **entropy magnitude forecaster**.

**Source:** `arXiv:2512.15720` — *"Hidden Order in Trades Predicts the Size of Price
Moves"*, Mainak Singha, Astrophysics Science Division, NASA Goddard Space Flight
Center / Department of Physics, The Catholic University of America. Preprint,
2025-12-02, `q-fin.TR`.

**Read two ways and cross-checked:** the paper itself, and the frames of the
`vince.quant` reel that carried it. The reel's on-screen boards are recorded here
separately from the paper's text, because they are not identical — the reel shows a
simplified 4-state matrix for teaching and a worked symmetry example the paper only
proves in the abstract.

Written 2026-08-20 because the design first recorded this mechanism in prose, and a
part built from prose would be guessed at rather than implemented.

---

## 1. State space — the encoder's job

Each **second** is labelled by one of fifteen states, the cross product of price-change
sign and volume quintile:

    q_t = sgn(P_t − P_{t−1})              ∈ {−1, 0, +1}
    v_t = ⌈5 · F_{V,t}(V_t)⌉              ∈ {1, 2, 3, 4, 5}
    S_t = (q_t, v_t)                      |S| = 3 × 5 = 15

`F_{V,t}` is the **empirical CDF of volume over the trailing 120 seconds** — so the
quintile is relative to recent activity, not to a fixed constant. That detail matters:
it is what makes the measure self-normalising across regimes and across symbols.

Data is tick-level trades aggregated to second-resolution bars. Each observation holds
the closing price and total volume for that second.

## 2. Transition matrix and entropy — the meter's job

    P̂_t   estimated from state transitions in the trailing 120-second window
    p̂_ij = 1/15   for all j, when row i has no observed transitions   ← uniform fallback
    π(P̂_t)  stationary distribution, by eigendecomposition

Normalised entropy:

    H_t = −(log 15)^(−1) · Σ_i π_i Σ_j p̂_ij log p̂_ij

The reel's board writes the same thing without the normaliser:

    H_t = −Σ_ij π_i P_ij log P_ij

`H_t ∈ [0, 1]`. **High entropy = unpredictable transitions. Low entropy = structure**,
which the paper reads as informed traders leaving a footprint.

## 3. The symmetry — why direction is impossible, not merely hard

The two theorems:

> **Theorem 1 (Magnitude Predictability).** Under standard microstructure assumptions,
> `E[|r_{t,t+Δ}| | H_t < H̲] > E[|r_{t,t+Δ}|]` for sufficiently low `H̲`.

> **Theorem 2 (Directional Unpredictability).** Under the same assumptions,
> `E[sgn(r_{t,t+Δ}) | H_t] = 0` when informed traders are equally likely to be buying
> or selling.

The proof rests on **permutation invariance**: swapping the "buy" and "sell" labels
transforms one transition matrix into another with identical entropy. An informed buyer
and an informed seller leave the same signature.

The reel demonstrates it numerically with a 2-state matrix:

              B     S                          B     S
        B   0.70  0.30      swap B↔S     B   0.65  0.35
        S   0.35  0.65        ───►       S   0.30  0.70

        H(P) = 0.847                     H(P′) = 0.847

        H(P) = H(P′)

**This is the constraint the part carries.** Detects structure in order flow; cannot
recover direction; mathematically guaranteed.

## 4. Measured results, on SPY

Data: 38,509,593 SPY trades, 1 Oct – 19 Nov 2025, 36 trading days. Regular-hours
trades 34,083,179, aggregated to 828,907 second-resolution bars.

Mean absolute 5-minute return by entropy quintile:

| quintile | mean abs 5-min return |
|---|---|
| Q1 (lowest entropy) | 8.14 bps |
| Q2 | 6.72 bps |
| Q3 | 5.51 bps |
| Q5 (highest entropy) | 3.75 bps |
| unconditional mean | 5.29 bps |

- **Q1 / Q5 ratio: 2.17**
- **Below the 5th percentile of `H`: 15.3 bps — ×2.89** the unconditional mean
  (`t = 12.41`, `p < 10⁻⁴`)
- **Directional accuracy: 45.0%** (108 of 240 out-of-sample trades), `z = −1.55`,
  `p = 0.12` — indistinguishable from chance, exactly as Theorem 2 predicts
- Label-permutation placebo: `z = 14.4` against the null

## 5. The trading rule, exactly as specified

This is the part the earlier write-up was missing entirely.

    Entry:      H_t < H_0.05          (5th percentile of H over the training window)
    Filter:     volume > 95th percentile
                5-min trailing return between 5 and 20 bps
                  — confirms unusual activity, excludes moves already underway
    Direction:  in the direction of the trailing 5-minute return (momentum heuristic)
    Exit:       5 bps stop-loss
                300-second timeout
                fixed take-profit chosen ex ante in the training window
    Costs:      1.57 bps round-trip
                  = half-spread entry + half-spread exit
                  + 0.30 bps slippage + 0.10 bps exchange fees
                  (calibrated to SPY's ~0.7 bps quoted spread)

**Validation protocol:** walk-forward. 10 trading days training, 5 days testing,
non-overlapping folds, five folds, thresholds estimated on training data only and
**frozen** for the test.

| Fold | Period | Trades | Win rate | Magnitude ratio | t | PnL (bps) |
|---|---|---|---|---|---|---|
| 1 | Oct 15–21 | 32 | 71.9% | 2.41 | 8.7 | 179.9 |
| 2 | Oct 22–28 | 27 | 33.3% | 2.18 | 7.2 | 212.9 |
| 3 | Oct 29–Nov 4 | 77 | 44.2% | 2.31 | 12.1 | 433.2 |
| 4 | Nov 5–11 | 12 | 41.7% | 1.89 | 4.9 | 66.5 |
| 5 | Nov 12–18 | 92 | 40.2% | 2.06 | 9.3 | 233.1 |
| **Pooled** | | **240** | **45.0%** | **2.17** | **12.41** | **1,125.6** |

**Profit attribution — the most important table here:**

| source | share |
|---|---|
| magnitude timing | **87.8%** |
| payoff structure | 12.2% |
| direction | **0.0%** |

The rule wins by knowing *when*, and by cutting losses at 5 bps. It wins nothing at all
from choosing a side — its own momentum heuristic contributed zero.

## 6. The author's stated limits — carried, not softened

1. **36 trading days, one instrument.** Far shorter than standard validation in
   quantitative finance.
2. **October 29 alone contributed 38.5% of profits.** Concentration, not a distribution.
3. **Execution assumptions** — immediate fills, fixed costs — may not hold at scale.
4. **VIX stayed in the 14–22 range throughout.** Behaviour in a high-volatility regime
   is unknown.

The author's own conclusion: *"Extended validation — longer time series, multiple
instruments, live execution — is required before practical application."*

## 7. What has to be decided before this is built here

None of these is answered by the paper, and none should be defaulted silently.

- **Crypto is not SPY.** 24/7, no opening auction, no closing bell, far wider spreads,
  and a venue-fragmented tape. The 1.57 bps cost model does not survive the move; the
  cost figure has to be rebuilt from the venue's real spread before any threshold means
  anything.
- **The second is the paper's unit.** This project's bots are intraday on 1m/5m/15m/30m
  (RL-043). Whether the entropy meter runs at 1-second resolution beneath those bars, or
  the state is redefined on a coarser clock, is an open design question.
- **Volume quintiles need a per-symbol trailing CDF**, and crypto volume distributions
  differ wildly between majors and small caps. One global quintile boundary would be
  wrong.
- **Thresholds are trained, not constants.** `H_0.05` is the 5th percentile *of the
  training window*. Any implementation that hard-codes an entropy number has already
  broken the method.
- **The 5–20 bps trailing-return filter** is calibrated to SPY's volatility. On crypto
  it is a different number, and it is the filter, not the entropy, that decides how
  often the part fires.

## 8. Where it lands in the blueprint

| Paper concept | Part | Produces |
|---|---|---|
| §1 state space | order flow state encoder | `order-flow-state` |
| §2 transition matrix, `H_t` | flow entropy meter | `flow-entropy` |
| §4 magnitude conditioning | entropy magnitude forecaster | `volatility-forecast` |
| §5 entry/filter/exit rule | **not a prediction part** — see below |

The **trading rule is deliberately not built into C-08.** Prediction says how far price
moves; when to enter is C-02's job, sizing and stops are C-03's, and direction belongs
to bull, bear and profit tailgating (RL-023). Importing the paper's momentum heuristic
into a prediction part would smuggle a direction call into a block that has no business
making one — and by the paper's own attribution, that heuristic earned exactly 0.0%.

**§3 is a rule, not a note:** the entropy chain produces `volatility-forecast` and never
`directional-opinion`.
