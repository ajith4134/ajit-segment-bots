# The vertical's numbers — measured on the tape, 2026-08-22

The first paper fill needs roughly seventy constructor arguments across 21 parts,
and RL-061 says each is either estimated from data or a named setting carrying its
provenance. This directory is the first half of that: the numbers the tape can
answer, answered by the tape rather than by whoever was typing.

    PYTHONPATH=. .venv/bin/python measurements/2026-08-22-vertical-numbers/<script>.py

---

## Regime thresholds — `measure_regime_thresholds.py`

`regime-classifier` estimates a Hurst exponent over a rolling window of trade
prices and calls the market trending above one threshold, reverting below another.
Four numbers, three of them measurable.

**What the tape says.** Four symbols, 60 000 real trades each, rolling windows
taken every 256 trades so they do not overlap into counting the same market twice.

| Symbol | Venue | Trades per second |
|---|---|---|
| ZECUSDT | binance-usdm | 202.4 |
| ETHUSDT | binance-usdm | 150.4 |
| XRPUSDT | bybit-linear | 883.0 |
| TRUMPUSDT | bybit-linear | 415.8 |

The Binance figures are 100 ms aggregates and the Bybit ones every print, which is
exactly the fidelity difference `NormalisedTrade` carries — XRPUSDT is not four
times busier than ZECUSDT, it is counted differently.

**How the estimate settles as the window grows** (spread across the four symbols):

| Window (trades) | Standard deviation | q05 | q95 | Market time per window |
|---|---|---|---|---|
| 64 | 0.177 – 0.262 | 0.19 – 0.28 | 0.82 – 1.07 | 0.1 – 0.4 s |
| 128 | 0.123 – 0.226 | 0.23 – 0.35 | 0.75 – 1.04 | 0.1 – 0.9 s |
| 256 | 0.091 – 0.202 | 0.24 – 0.38 | 0.65 – 0.93 | 0.3 – 1.7 s |
| 512 | 0.064 – 0.153 | 0.35 – 0.44 | 0.62 – 0.88 | 0.6 – 3.4 s |
| **1024** | **0.046 – 0.139** | **0.38 – 0.47** | **0.59 – 0.84** | **1.2 – 6.8 s** |
| 2048 | 0.034 – 0.133 | 0.40 – 0.49 | 0.57 – 0.84 | 2.3 – 13.6 s |

**Three findings, and each one sets a number.**

1. **The estimator is mostly noise below 512 trades.** At 64 its standard deviation
   is up to 0.26 — larger than the entire distance from a random walk to either
   regime. A classifier run there would flip between trending and reverting on the
   same market, which is worse than not classifying: it would look decisive.
2. **The spread halves from 512 to 1024 and barely moves again at 2048**, while
   market time per window doubles. So 1024 is where the estimate stops paying for
   itself in latency — and it is what `regime_window_length` and
   `regime_minimum_observations` are both set to. Requiring a full window means a
   symbol that has just started reads `unclassified`, which is a real state rather
   than a guess.
3. **The median is 0.48 – 0.52 on every symbol.** At these timescales this market
   *is* close to a random walk, which is the honest reading and the reason the
   thresholds sit where they do: 0.60 and 0.40 are roughly the 5th and 95th
   percentiles of the two least noisy symbols at 1024. About one window in ten
   classifies on a calm symbol, and fewer on a noisy one — a noisier estimate
   classifying *less* is the right direction, and the opposite of what a fixed
   distance from 0.5 chosen by eye would have done.

**What this does not settle.** These are the four busiest symbols on the tape. A
symbol trading once a minute takes hours to fill a 1024-trade window, and whether
a regime measured over that span still means anything is a question for a
measurement over quiet symbols, not for a threshold. Recorded, not answered.

---

## Cointegration thresholds — `measure_cointegration_thresholds.py`

This is the measurement the whole phase turns on. **Exactly one detector in the
blueprint can produce an `entry-candidate` from inputs the phase-3 spine has**:
`spread-reversion-detector`, fed by `cointegration-pair-finder`. Every other
detector needs a `playbook-rule`, and the playbook is built by the learning loop
out of instructions that do not exist until trades have happened. So if no real
pair passes the finder's two tests, the first paper fill has no way to begin.

Eight symbols per venue, ~320 000 real trades each, replayed in true time order,
run through the actual `CointegrationPairFinder` with its thresholds opened wide so
every pair is measured rather than filtered.

**Absolute correlation, and reversion pulled back per step:**

| Venue | Window | \|corr\| q25 / q50 / q75 | reversion q25 / q50 / q75 |
|---|---|---|---|
| binance-usdm | 256 | 0.18 / 0.32 / 0.50 | 0.035 / 0.066 / 0.077 |
| binance-usdm | 1024 | 0.15 / 0.29 / 0.41 | 0.010 / 0.017 / 0.037 |
| bybit-linear | 256 | 0.35 / 0.56 / 0.72 | 0.019 / 0.050 / 0.088 |
| bybit-linear | 1024 | 0.18 / 0.23 / 0.65 | 0.005 / 0.009 / 0.025 |

**What that sets.**

- **Window 256, not 1024.** Reversion is three to five times stronger at the
  shorter window on both venues, and a window that takes longer to fill delays the
  first candidate for no measured gain. Unlike the regime estimator — where the
  spread of the estimate was the thing being bought — here the quantity itself is
  larger at the shorter window.
- **`minimum_correlation` 0.5.** At window 256 that is roughly the median pair on
  Bybit and the top quartile on Binance. Selective without being empty: the two
  prices must share at least a quarter of their variance before the spread is
  looked at.
- **`minimum_reversion_strength` 0.05.** About the median at window 256 on both
  venues, so roughly half the pairs clear it and, combined with the correlation
  floor, something like a quarter of pairs pass both. That is a first run with
  candidates rather than a first run with none.

**A defect this exposed, recorded rather than fixed here.** The finder aligns two
symbols **by observation count, not by time**: it takes the last N prices of each
and zips them. On this tape XRPUSDT prints 883 trades a second and ZECUSDT 202, so
the last 256 prices of one span 0.3 seconds and of the other 1.3 — and the
"correlation" between them is computed across different stretches of market. The
measurement above replays trades in true time order, which is what the live bus
delivers, so these figures are what the running part would see; the misalignment is
in the part, not in the measurement.

It is not fixed in this phase because phase 3 is wiring, not redesign, and changing
what a part computes while wiring it would make a failure impossible to attribute.
What it means for the first fill is stated plainly: **a pair this finder calls
cointegrated may be an artefact of two symbols trading at different speeds**, and
the first fill proves the circuit conducts rather than that the trade was good.
The fix — resampling both symbols onto a common time grid before comparing — is a
change to `cointegration-pair-finder`'s own logic and belongs in its own piece of
work, with its own measurement.
