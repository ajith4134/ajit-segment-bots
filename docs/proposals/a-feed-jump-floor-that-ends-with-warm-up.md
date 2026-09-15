# A feed-jump floor that ends with warm-up

**Origin:** 2026-09-13, from the drift guard's remainder. The operator, asked which of
the open design items to take: "You decide the best ways and option and direction".
Taken first because it is the one that blocks trades: `paper-fill-simulator` refuses
to fill a symbol `feed-jump-detector` has flagged.

## What was measured

`measurements/2026-09-13-feed-jump-floor/compared-on-the-tape.txt` replays the I1 bars
`broker-candle-bridge` republishes for 2026-09-07/08 (1,206 and 1,148 instruments)
under each rule. Share of ordinary bars flagged / share of bars following a tape
recording hole flagged (a recording hole is a real discontinuity in what the feed
delivered, and the one kind the tape can prove without an invented fixture):

| rule | NSE_FO contracts | NSE_EQ shares | NSE_INDEX |
|---|---|---|---|
| live: floor 0.005, then max(floor, 2.8 x own p99), no ticks | **24.07%** / 85.4% | 2.06% / 45.3% | 0.00% / 0.0% |
| ticks fed in, same rule (floor 20 ticks) | 4.62% / 48.2% | 4.79% / 64.6% | 0.00% / 0.0% |
| **warm-up max(2 ticks, 0.05); after, max(2 ticks, 2.8 x own p99)** | **1.02%** / 51.5% | **1.50%** / 37.0% | **0.80%** / 100% (2 bars) |

The live rule flags a quarter of ordinary option bars, and is blind on an index,
because one number does two jobs: it is the whole bound for a symbol's first eight
moves, and the permanent minimum after. A floor that suits a contract's warm-up is far
above anything an index does, so the index can never be flagged; a floor that suits an
index flags every contract.

## What changes

1. **The fraction floor ends with warm-up.** Until a symbol has shown
   `feed_jump_moves_needed` of its own moves, the bound is `max(ticks floor, warm-up
   floor)`. After, it is `max(ticks floor, patience x own p99)` -- the symbol's own
   rhythm, with no fraction beneath it. The setting is renamed
   `feed_jump_threshold_fraction` -> `feed_jump_warmup_floor_fraction` because its
   meaning changed (Rule 7), and set to 0.05.

2. **Ticks come in.** `feed-jump-detector` consumes `price-increment`, which
   `tick-size-resolver` already publishes from the broker catalogue (1,974 Upstox
   symbols declared on 2026-09-13). The ticks floor is the permanent minimum, so a flat
   contract whose own p99 is zero is not flagged on a one-tick move.
   `feed_jump_threshold_increments` 20 -> 2: a move of two ticks or less is never a
   jump. Nothing called `set_price_increment` before, so that setting was inert.

## What it does not do

It does not decide what a jump means downstream -- `paper-fill-simulator` still refuses
a flagged symbol and is released by the next continuous bar. It does not change the
patience multiple, the moves needed, or the history length.

About half of post-hole contract bars still pass unflagged under the chosen rule, the
same order as the live rule's ticks variant: a hole whose price did not move is not a
discontinuity a stop could fall into, and flagging it would only refuse fills.
