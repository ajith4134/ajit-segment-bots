# Sampled price levels, and a governor that acts

Proposed by Claude 2026-08-24. Agreed by the user the same day, both forks chosen
deliberately (see *The two decisions*, below).

## The problem, measured

The step from 30 symbols per venue to 100 on 2026-08-23 was rolled back the same
morning: the decision half could not keep up, symbols froze under input loss, and
a trade was decided at an ENAUSDT price fifty-six minutes old. The staleness half
of that is now fixed -- a price cannot be held without the moment it printed, and
a level too old to believe reads as absent. That stops the bot acting on a frozen
price. **It does not stop the freezing.**

What causes the freezing is fan-out, and the numbers are this system's own:

| | measured |
|---|---|
| prints on the tape, 2026-08-23 | 16 635 188 across 112 symbol-venue pairs |
| print rate | 193 per second |
| parts declaring `market-data` | 66 |
| deliveries per second if every consumer runs | **12 707** |

The feed is not the problem. 193 messages a second is nothing. The problem is that
each one is handed to 66 parts, and that the total scales with **trading volume**,
which is exactly what grows when the universe grows.

For comparison, read from the projects that do run large universes without this
failure. Freqtrade's documented `VolumePairList` example is 20 assets refreshed
every 30 minutes, its whole bot loops once every 5 seconds
(`internals.process_throttle_secs`), and it reasons over **closed candles, never
prints**. A thousand pairs on a 5-minute timeframe is 3.3 messages a second. It
does not solve our problem; it never has it. Its staleness guard
(`freqtrade/strategy/interface.py`, `timeframe * 2 + outdated_offset`) is the same
idea as ours and cruder, and it is safe there because its stops are not 0.19% wide.
NautilusTrader, the one comparable tick-level system, queues and monitors instead
of dropping -- `queue_depth_trigger`, `mean_dispatch_ns_trigger`,
`data_event_staleness_ns`, typed `QueueStateChanged` events on a 100 ms tick --
which this project deliberately does not do (RL-066: scarcity is never answered by
a queue).

## What the 66 parts actually read

Classified from their own source, by which fields of a trade they touch:

| what it needs | parts |
|---|---|
| **a price level only** -- `venue_id`, `symbol`, `price`, `venue_time_ns` | **40** |
| declares `market-data` and reads no field of a trade | 16 |
| genuinely needs the print (`quantity`, `quote_volume`, `sequence`, and closed-trade fields) | 10 |

Forty parts are being handed nine million trades a day to learn one number per
symbol that a single part could compute once.

## The two decisions

**One frame per tick, carrying every symbol.** Not one message per symbol: that
still scales with the universe (2 590 symbols at 1 Hz into 40 parts is 103 600
deliveries a second). One frame per tick into 40 parts is 160 deliveries a second
**and does not grow with the universe or with volume at all**. A frame is per venue.

The size was then measured rather than estimated, and the estimate was wrong. Real
frames serialise at **50.1 bytes per symbol**, not the ~70 assumed above, so the
largest frame the bus will carry under its 131 072-byte ceiling is **2 616
symbols** -- and the full universe on one venue, about 1 295 perpetuals, is 64 980
bytes. It fits with room to spare, and the split path exists for the case nobody
predicted rather than for the ordinary one. `price_frame_maximum_symbols` is set
to 2 000, which is 24% inside the measured ceiling and still above any real venue.
A split is counted in the part's standing, so a universe that has outgrown one
frame is visible rather than inferred.

The cadence is decided by the staleness bound, not chosen: a level must be
fresher than the tightest age any symbol will believe. That floor is 1.0 s
(`reference_price_minimum_age_seconds`), so the frame is published four times a
second, leaving a factor of four of headroom.

**`momentum-burst-detector` and `liquidation-cascade-detector` move to the fixed
time base with the rest.** Both compute a return between consecutive prints, and
on a sampled feed they compute it over a fixed interval instead. That is a change
in what they measure, and it is an improvement for the same reason
`PriceStalenessEstimator` scales every move to one second: a return per print
means different things on a symbol printing nine times a second and one printing
once a minute, and the two numbers cannot be compared or learned from together.
Their calibration restarts from the change, which is honest -- the old record
described a different measurement.

## What changes in the blueprint

One new part and one new data type. No part gains a capability; the work moves to
a part whose whole job it is (T-6, grow by adding parts).

    price-level-sampler   consumes market-data, produces symbol-price-frame

    symbol-price-frame    every symbol's latest price for one venue, each with the
                          moment it printed, published on a fixed cadence

Thirty-seven parts change `market-data` in their `consumes` to
`symbol-price-frame`. The sixteen that declare it and read no field are left alone
in this edit: an edge nothing reads is a separate question from an edge that is
expensive, and removing one is a claim about a part that has to be made per part.

**Three of the forty keep the print, and the classification that found them was
wrong about all three.** They read no field but `price`, so counting fields put
them with the rest; what they do with the price is what separates them.

    paper-fill-simulator          a resting stop or take-profit triggers when the
                                  market touches it. Sampled four times a second,
                                  a wick that touches the stop between frames
                                  never happened, and a paper fill that ignores it
                                  is a paper fill that is kinder than the venue --
                                  which is the one direction a simulator must
                                  never err in.

    paper-liquidation-simulator   the same, for the price that ends the position
                                  whether or not anyone was watching.

    peak-excursion-tracker        its whole output is the extreme. Four samples a
                                  second on a symbol printing nine times a second
                                  keeps roughly one print in thirty-seven, and the
                                  best and worst it reports would be the best and
                                  worst of the samples rather than of the market.
                                  Stop placement is learned from those numbers.

Ten more keep `market-data` because they read fields a level does not carry. So
thirteen parts stay on the print, and every one of them is a part where the print
itself is the fact.

## The governor

Separately, and with no blueprint change at all: the governor block is fourteen
parts, every one of them launchable, and **five of them run**. `switching-planner`
observes and publishes what it would switch. `gate-actuator` has never been
started, so nothing acts. That is the state RL-068 planned for -- the governor
spine before the vertical -- and the remaining work is to start the other nine and
let the actuator switch.

The sampler is what makes that safe to do. A governor that can turn parts off is
only useful if turning a part off is how scarcity is answered; while every part is
drowning in the same 12 707 deliveries a second, the governor's only available
answer is to switch parts off that the operator wants on.

## How this is verified

- deliveries per second, before and after, from the same measurement that produced
  the numbers above
- input loss per part from the heartbeat table, which already carries it, and
  which currently shows `regime-classifier` losing market-data in bursts **at 30
  symbols**
- decision freshness from the trade board, matched by trade id and direction
- the walk to 50 and beyond, re-measured at each step, which is the thing this
  work exists to unblock (RL-009)
