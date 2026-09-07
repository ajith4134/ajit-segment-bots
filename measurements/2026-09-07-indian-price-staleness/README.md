# Why no options order could fill — 2026-09-07

Three segment bots were running on live NSE data. Stock options had opened
eighteen positions all session, index options had opened none, and
cash-equity-intraday had never formed a single trade intent.

## What was measured, on the live spine and on this project's own tape

| | |
|---|---|
| `instrument-selector` `chosen` | 8,259 in ten minutes |
| of those, with no price for the contract | **6,981 — 84.5%** |
| `paper-fill-simulator` `refused_because_the_decision_was_stale` | 1,874 against 290 filled |
| median inter-print gap, 200 sampled live option contracts | **9.25 s** (p90 66.6 s) |
| share of gaps beyond the 1.5 s believable-age bound | **82.0%** |

## The chain

`stop-target-placer.priced_entry_for` took the chosen contract's own reference
price, **or the underlying's price when there was none**. `position-sizer` sizes
an open from exactly that number, so an order to buy `INFY 1200 CE 23 NOV 26`
went out priced at **1,088.40** — INFY's own spot — against a real premium of
**21.10**. `paper-fill-simulator` compares that against the contract's market
price and refuses past `maximum_decision_price_drift` (6%).

So the question was how often the contract had no price. The bound is
`(materiality / one-second move)²`, clamped to `[1s, 60s]`, and **both inputs
were the wrong market's**:

| | in use | measured here | out by |
|---|---|---|---|
| `reference_price_prior_one_second_move` | 0.000898 — median p95 one-second move across six Binance/Bybit perpetuals | **0.002324** — p50 across 327 fitted NSE option contracts (p90 0.9107%) | 2.6× |
| materiality (`2 × taker_fee_rate`) | 0.0011 — twice Bybit's published taker rate | **0.008532** — Upstox's real charge stack (0.2341% round trip at a ₹150,000 premium) plus two half spreads (0.3175% at p50 over 23,606 book snapshots) | 7.8× |

The two errors ran in opposite directions and cancelled into a believable age of
**1.50 s**, which looks like an ordinary number. Against a 9.25 s median print
gap it refused the newest price that existed for 82% of the session.

Corrected, the same formula gives **13.48 s** — longer than the median gap,
shorter than the 60 s ceiling that the fifty-six-minute ENAUSDT staleness of
2026-08-23 put there.

    crypto materiality, crypto prior          1.500s
    crypto materiality, measured move         0.224s
    measured materiality, measured move      13.484s

Crossing the spread twice is two thirds of the real round trip, which is why a
fee-only figure understated it by 7.8×.

## Re-running

    .venv/bin/python measurements/2026-09-07-indian-price-staleness/measure_indian_price_staleness.py [YYYY-MM-DD]

Samples 400 live option contracts from the tape for that day, replays the
estimator's own method (anchor ≥ 1 s, move scaled to one second by the square
root of the gap, 95th percentile), reads the touch spread from the book tape,
and prints the believable age each pair of numbers implies. Re-run when the
traded universe changes.

## What changed because of it

- `reference_price_prior_one_second_move` 0.000898 → 0.002324
- new `reference_price_materiality_fraction` 0.008532, replacing
  `2 × taker_fee_rate` in `price_staleness_from` and at every explicit call site
- `priced_entry_for` returns `None` instead of the underlying's price, and
  `stop-target-placer` skips the intent and counts
  `skipped_without_a_price_for_the_instrument`
- `instrument-selector` refuses a choice whose **chosen contract** cannot be
  priced, instead of gating on the intent's symbol — an option chain's underlying
  always prints, so the old gate passed exactly when the contract could not be
  priced
