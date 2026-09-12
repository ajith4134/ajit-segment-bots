# A range from five prints is 60% of the real one — 2026-09-08

`bull-exit-plan-proposer` refused **84,838 of 86,943** plan requests on the live
spine today as `too-few-prints-in-the-window-to-measure-a-range`; the bear peer
refused 71,211 of 77,282. The whole trading half sizes against the stops those
plans carry, so a starved proposer is a bot that cannot open a position however
right it is.

The bar was `*_cold_start_minimum_prints = 20`, with this provenance:

> *"Measured on the tape 2026-08-22: BTCUSDT prints about 4 a second, so a 60s
> horizon carries roughly 240 — the bar binds on quiet symbols, which is exactly
> where it should."*

A crypto perpetual prints 240 times a minute. The median NSE option prints five.

## Why lowering the bar alone would have been the wrong fix

`measurements/2026-09-07-exit-plan-starvation/` refused to move it, and was
right: the range is what the **stop distance** is measured from, so an
under-measured range is a stop placed too tight — a machine for being stopped out
of trades that were going to work. It named the alternative it did not take:
correct the small-sample bias in the estimator instead.

That table's script was never saved, so this re-derives it independently.

## The measurement

Hold the window fixed at 300 s — which isolates sample size from span — take the
true range over every print inside it, then draw n of those prints at random and
record what share of the true range the sample spans. This project's own Upstox
tape for 2026-09-08: **300 symbols, 1,974 windows, 9,070 draws per sample size.**

| prints | p25 | **p50** | p75 |
|---|---|---|---|
| 3 | 20.2% | **41.4%** | 64.1% |
| 4 | 33.3% | **52.9%** | 75.0% |
| 5 | 40.3% | **60.2%** | 80.0% |
| 6 | 47.5% | **65.7%** | 83.3% |
| 8 | 55.3% | **73.0%** | 88.7% |
| 10 | 60.9% | **76.9%** | 92.3% |
| 15 | 70.0% | **85.1%** | 100.0% |
| 20 | 76.0% | **89.2%** | 100.0% |
| 30 | 83.3% | **94.2%** | 100.0% |

It reproduces the 2026-09-07 figures closely (3 → 42.9%, 5 → 60.5%, 8 → 72.9%,
20 → 92.9%), so the two are independent derivations of one curve.

## What changed

`runtime/range_from_a_small_sample.py` divides a measured range by the median
recovery at its own sample size, interpolating between the measured counts. The
bar drops from 20 to **5** — the smallest count where the correction is worth
making, since three prints would need a 2.4x correction.

**The median is used, not a lower quantile, and that is deliberate.**
Over-correcting is not the safe direction it looks like: a wider stop is a larger
risk fraction, which makes the round trip cheaper in units of that risk and so
*lowers* the conviction floor. Being generous with the correction would buy fewer
stop-outs by taking more trades — a trade-off this estimator is not entitled to
make on its own.

Below three prints nothing is corrected and the range is still refused: a
correction from two prints is guesswork wearing a measurement's clothes.

Both proposers now report `smallest_range_sample` and
`largest_small_sample_correction` on their standing, so a stop built from five
corrected prints and one built from fifty raw ones never read as the same claim
(Rule 8).

## What this does not settle

The **horizon** is still the crypto one for three of the four detectors
(`spread_reversion_horizon` 60 s, `mean_reversion_horizon` and
`momentum_burst_horizon` 300 s, both dated 2026-08-23 with no Indian
derivation). Correcting the estimator makes a short horizon survivable; it does
not make it right. `signal-horizon-profiler` measures the realised
time-to-resolution per detector and that is what would settle each one.
