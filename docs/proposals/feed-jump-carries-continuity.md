# `feed-jump` states continuity in both directions

**Proposed 2026-09-04. Origin: designed by Claude; the user chose this shape over
a timer and over a second copy of the criterion in the fill part.**

## The defect

`paper-fill-simulator` refuses to fill any order on a symbol `feed-jump-detector`
has flagged. Measured live on 2026-09-04, every order it had ever seen was
refused or waiting:

    orders_seen 111 = held_in_flight 57 + refused_feed_jump 54,  filled 0

`clear_feed_jump` exists on the simulator and **has no caller anywhere in the
repository**, tests included. `_jumped_symbols` is a set that only ever grows, so
one flag bars that symbol from filling for the life of the process.

On the captured Upstox tape for 2026-09-04, **993 of 1,474 streams (67%) cross
the jump threshold at least once** — so within minutes of a spine start, two
thirds of everything tradeable is permanently unfillable. Nothing reports it as a
fault: every one of those refusals is a decision the simulator is entitled to
make. Only the return was missing. That is the same shape as the governor defect
of 2026-08-26, one subsystem along.

## Why the wire, and not a timer

A jump's danger is filling *across* the discontinuity. The evidence that the
discontinuity is over is a bar that is continuous with the one before it — which
`feed-jump-detector` already computes on every closed candle and currently throws
away when the answer is "no jump".

A timer in the simulator would clear the bar on a clock rather than on evidence,
and would need a number nobody has measured (RL-061). A second copy of the jump
criterion inside the fill part would let two copies drift apart, and grows a part
rather than the circuit (T-6).

So `feed-jump` stops meaning "a jump happened" and starts meaning "this symbol's
continuity, as of this bar". The part that measures continuity is the part that
reports it, and the simulator names data rather than a part (T-4).

## The change

`feed-jump` gains `is_continuous`. `feed-jump-detector` publishes on every closed
candle it can compare — `is_continuous=False` when the move exceeds the bound,
`True` when it does not. `paper-fill-simulator` calls `observe_feed_jump` on the
first and `clear_feed_jump` on the second.

This is a level, not an event: a symbol's continuity is true until it changes.
It is published through `LevelPublisherByKey` keyed on `(venue_id, symbol)`, so
one symbol changing does not restate the other 487 — the lesson of 2026-08-26.

No part's `consumes` or `produces` changes; the data type's meaning does, which
is why this is a blueprint edit before it is code.

## What it does not fix

The bound itself is wrong for this segment, and that is a separate change with
its own measurement: `feed_jump_threshold_fraction` (0.5%) was fitted to crypto
perpetuals on 2026-08-23 and its own note says to re-measure when the universe
widens. On Upstox `I1` bars, NSE_FO option contracts move p50 0.208% / p95 2.85%
/ p99 5.69% between a bar's close and the next bar's open, so **38.1% of them are
flagged**, while NSE_INDEX never crosses it at all (p99 0.056%). An option's
premium is small and levered: the same move in the underlying is percent-scale in
the contract, and that is a real price move, not a feed artefact.

Both are needed for a fill. Without continuity the bound only delays the poison;
without a fitted bound the symbol re-poisons on the next ordinary tick.

Measurement: `measurements/2026-09-04-why-no-paper-order-ever-fills/`.
