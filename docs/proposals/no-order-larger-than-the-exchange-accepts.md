# No order larger than the exchange accepts, and a size label that can be false

**2026-09-12.** Asked for by the user after reading the trade record: *"some are
very profitable and some are huge loss ... is there a way for the bot to pick
only profit trades and finding out the losing trades and avoiding them"*.

## What the trades actually said

910 closed trades across both `position-recorder` journals, net **−₹1,249,500**.
250 wins, 652 losses, 8 flat. The loss is not spread out:

    worst  1 trade  =   -342,876   27.4% of the total
    worst  3 trades =   -717,461   57.4%
    worst  7 trades = -1,141,558   91.4%
    worst 10 trades = -1,249,453  100.0%

Remove the worst seven and the remaining 903 net **−₹107,942** against
**₹106,600 of fees** — flat minus costs. No edge demonstrated, and no disaster
either. **The disaster was mechanical**, and all seven were the same shape:
NIFTY weeklies bought at 0.15–0.18 and exited at 0.10, which is NSE's minimum
option price rather than a market move.

## The mechanism, established from the journals

    bounded-order  capital_used 200000.0  quantity 2000000.0  entry_price 0.1
                   outcome "capped-at-maximum"
                   reason  "cut from 2,321,631.74 to the 200,000.00 that may
                            still be committed"
    fill           price 0.169539375      quantity 2000000.0

The capital ceiling **was** enforced. It was computed in capital, at the touch
price of 0.10, and then converted into a quantity of 2,000,000 units — **1,140
times the 1,755 that NSE accepts in a single order for that contract**. The
paper book, which reads no freeze quantity either, filled it by walking 69.5% up
its own depth curve, and the trade committed ₹339,079 against a ₹200,000
ceiling.

**A cap expressed in capital but enforced as a quantity is not a cap.** The
bound that was missing was never the desk's. It was the venue's, and nothing in
this project read it: `grep -rn freeze_quantity --include=*.py` hit the adapter
that parses it, and the tests, and nothing else.

Four separate defects, each independently sufficient:

| | what was wrong |
|---|---|
| **lot** | `trade-capital-bounds-gate` snapped only on its bump and cap paths. An order already inside the capital bounds was emitted untouched — which is how 712,985 and 559,703.18 units of a 65-unit contract reached the book |
| **freeze** | nothing read `freeze_quantity` at all |
| **the walk** | the ceiling was computed at a price the order's own size destroyed |
| **stacking** | at 09:42:00.309 four orders for one contract (`55d0de91`, `a0be0a0c`, `594fd4b9`, `679e2e0d`, eight fills) were bound in the same millisecond, each passing the position ceiling against a position record none of them had yet updated |

## What it is now

`freeze_quantity` travels the same road the lot size already travelled —
`CapturableSymbol` → `broker-symbol-universe-bridge` → `ListedInstrument` →
`InstrumentChoice` → `SizedOrder` → the gate. No new wire and no new part: it
was already in the master and already parsed.

`trade-capital-bounds-gate` gains one helper, `_tradeable_quantity`, on **every**
emitting path rather than two of them. Two limits, both the venue's:

1. a whole number of lots, refused by name when that is zero rather than cut to
   zero — the same argument `REFUSED_MAXIMUM_BUYS_NOTHING` already carries;
2. at or below the freeze quantity, **cut** rather than refused, because a
   smaller order is still the trade the operator asked for.

**A close is capped and never refused.** The unsnapped orders left positions
holding fractional quantities of lot-traded contracts; applying the part-lot
refusal to an exit would strand exactly those positions for ever, so what is
held leaves in pieces.

For the stacking, the gate now counts capital **it has itself let through and no
`position` message has reported back**, and that reserve expires
(`bound_capital_awaits_a_position_for_seconds`, 30 s). An unexpiring reserve is
the unbounded-`LatestByKey` trap this project has paid for three times.

`operate/replay_a_captured_session.py` had reproduced the same defect
independently in its own `size_for`: whole lots, no freeze. It reads it now.

## What this costs, measured

Replaying the real 2026-09-08 tape afterwards, index-options can open **4 of 8**
contracts instead of 8, and its net falls from **+₹36,329 to +₹5,514**. The old
figure was profit priced at fills no venue would have given.

The four it can no longer open are refused for a reason worth stating plainly:

    NIFTY 23600 PE  27 whole lot(s) commit 20,182.50, below this segment's
                    minimum_capital_per_trade of 100,000.00 -- capped at the
                    1755 units this exchange takes in one order

**`minimum_capital_per_trade` and the freeze quantity together impose a floor on
the option premium this segment can trade**: ₹100,000 ÷ 1,755 ≈ **₹57**. Below
that, one order cannot reach the minimum. That is a real constraint and not a
bug, but it is the operator's to know about, because the contracts it excludes
are exactly the cheap out-of-the-money ones that lost all the money — and
because the two settings were each chosen without the other in view.

## The learning half: a label that could never be false

`label-builder` turns a closed trade into the four components a model can learn
from, and its own docstring is right about why: *"Profit is not a label."* One of
the four is `THE_SIZE_WAS_RIGHT`, which is precisely what these seven trades got
wrong.

    THE_SIZE_WAS_RIGHT: (
        excursion.peak_adverse_fraction * multiple <= abs(self._stop_distance(trade))
        if self._stop_distance(trade) else True )

    def _stop_distance(self, trade): return getattr(trade, "stop_distance_fraction", 0.0)

`ClosedTradeRecord` never declared `stop_distance_fraction`. The `getattr`
default therefore returned 0.0 for every trade, 0.0 is falsy, and the `else
True` branch fired every time: **the part asserted that the size was right on
every label it could ever produce**. A component that is always true is
indistinguishable from a component that is never wrong, and only reading the
code apart from its output tells the two apart.

It is a declared field now, and an unknown stop distance **omits** the component
rather than asserting it — Rule 8 applied to a label — with
`labels_whose_size_could_not_be_judged` counting how often.

## What is NOT fixed, and is the next thing

1. **Nothing consumes `THE_SIZE_WAS_RIGHT`.** `grep -rn THE_SIZE_WAS_RIGHT`
   finds its definition and this part, and no reader. The component is honest
   now and still goes nowhere; wiring a consumer is a blueprint edit.
2. **No closed trade carries its stop.** `ClosedTrade` has no stop field and
   `label-builder` consumes `closed-trade`, `peak-excursion` and `cost-estimate`
   — none of which carries one. So the component is *judgeable* but not yet
   *judged* on the live path.
3. **The component asks the wrong question for this failure anyway.** It compares
   a fractional excursion against a fractional stop. These seven trades were the
   right fraction and the wrong *rupees*: a position 5x too large is not visible
   in a ratio. A size label that could have caught them has to compare committed
   capital against the segment's own ceiling.
4. **`paper-fill-simulator` still reads neither lot nor freeze**, so an order
   reaching it from a path that does not cross this gate — `stop-order-manager`
   places its own — is still filled at any size. That needs either a new consume
   (a blueprint edit) or the limits carried on the order request.
5. **The conviction models never see any of this.** All 98,862 of
   `bull-conviction-model`'s labels come from `signal-outcome-labeller`, which
   asks only whether price moved after a signal. `label-builder` has built zero
   labels ever — it joined the live spine at 09:56 on 2026-09-08, after the last
   trade of that session closed.

## Verification

- `tests/parts/risk_capital_allocation/test_the_gate_refuses_a_size_no_exchange_would_take.py`
  — 11 cases against the real orders of 2026-09-08 and the real master. Six of
  them fail when `_tradeable_quantity` is made a passthrough.
- `tests/parts/learning_loop/test_label_builder_on_real_closed_trades.py` — the
  95 real closed trades of 2026-09-07/08 with their own excursions, captured to
  `tests/captured/upstox/2026-09-08-closed-trades-and-excursions.jsonl`. The
  regression case fails against the pre-2026-09-12 behaviour.
- 1,584 tests pass across the affected suites; all four checkers pass.
- The spine restarts clean and all five changed parts report their new counters.
- The replay above is the end-to-end measurement.
