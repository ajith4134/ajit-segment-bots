# A conviction floor charges the cost of the symbol its plan was written on

**2026-09-08.** Asked for by the user: *"fix all the design questions and errors
of no new trades opening in 3 segments"*. This is the design question the
2026-09-07 session raised and deliberately left open.

## What was wrong

`runtime/edge_arithmetic.py` computes the probability a trade has to be right
with before it is worth taking:

    p* = (1 + c) / (1 + R)      c = the round trip, in units of the risk

`c` is a **ratio of two fractions**, and it means nothing unless both are
fractions of the same instrument's price. Every gate passed
`per_side_trading_cost_fraction` — 0.4266%, derived in
`measurements/2026-09-07-indian-price-staleness/` for an **NSE option premium** —
while `risk_fraction` came from `bull-exit-plan-proposer`, which measures the
range of whatever symbol the candidate named. Three of the four live detectors
name **underlyings**:

| detector | symbols it names |
|---|---|
| `volatility-gap-detector` | `ICICIBANK`, `ITC`, `KOTAKBANK`, `SENSEX` |
| `mean-reversion-detector` | `LT`, `HINDUNILVR`, `NIFTY 23500 PE 08 SEP 26` |
| `spread-reversion-detector` | `ICICIBANK`, `BOMDYEING`, `MARUTI 13600 CE 29 SEP 26` |
| `expiry-day-zero-to-hero-detector` | option contracts only |

A stock's range over a detector's horizon is a much smaller number than an
option's. Charged an option's cost, the ratio blew up, and the floor reached its
1.0 cap — a probability nothing can cross.

Measured on the live spine that day: **9,085 trade intents formed and every
single one was `stand-aside`**, 100% for `conviction-below-threshold`, both bots.
`bull-opinion-composer.last_floor` read 1.0 and its
`widest_live_range_fraction` across 837 plans was 1.01%, so even its widest stop
gave a floor of 55.8% against a model reporting 50.0%. No trade could open in any
segment, and nothing downstream of the arbiter was exercised at all.

## What it is now

`ConvictionFloor.for_plan` takes an optional measured round trip for the plan's
own symbol. `runtime/symbol_round_trip_cost.py` supplies it:

    liquidity-grade.round_trip_cost_fraction     spread crossed once + the walk
                                                 on each side, from that symbol's
                                                 own book (liquidity-grader)
  + broker_charge_stack_round_trip_fraction      what a book cannot see

Both terms are fractions of that symbol's own price, so the ratio is meaningful
for a stock and for an option alike, and an illiquid contract is charged the wide
spread it really has rather than an average.

Three properties worth keeping:

- **A grade expires.** `liquidity_grade_maximum_age_seconds` (60 s, twelve
  refreshes) bounds it, and past that the gate falls back to the setting and says
  so on its standing. A level with no age bound is the shape this project has
  already paid for three times.
- **The fallback is unchanged.** A symbol nothing has graded is charged exactly
  what it was charged before, so nothing silently becomes cheaper.
- **A book that costs 192% to cross still pins the floor at 1.0**, and that is
  correct: `liquidity-grader` measured exactly that on a real contract this
  session. Refusing to trade it is the answer, not a bug to clamp away.

## What it does not settle

`broker_charge_stack_round_trip_fraction` is the **options** charge stack applied
to every symbol. On a cash equity that overstates the charges — intraday STT is
0.025% on the sell side against options' 0.1% — which raises the floor and so
refuses rather than over-trades. It stops being an approximation the moment
`liquidity-grade` carries the instrument kind and the grader can bill the right
stack; `runtime/indian_equity_fee_model.py` already exists for that.
