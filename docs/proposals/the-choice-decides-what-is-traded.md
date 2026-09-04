# The instrument choice decides what is traded, and a sell becomes the opposite buy

**Proposed 2026-09-04. Origin: the user's decision on how a sell signal is
expressed while this segment is buy-only, plus a hole found while tracing where
that decision would have to land.**

## Two defects, one path

### 1. Nothing acts on the choice

`instrument-selector` exists to "choose the contract, strike, expiry or pair that
expresses a trade intent in this segment". `instrument-choice` has exactly two
consumers, and `position-sizer` -- the only one that builds an order -- reads it
**only for `reference_price`**. It never reads `chosen.contract_symbol`. The order
it builds carries `intent.symbol` and `order_side_for(intent.side)`.

So the selector's decision has never determined what is traded. Worse, an open
does not require a choice at all: `entry_price_for` prefers a `stop-target-plan`'s
entry price, so a plan alone is enough to open a position in an instrument nobody
selected. That is how, on 2026-09-04, every intent was refused by the selector
(983 of 983) and orders were still placed.

### 2. Which produced orders that write options

This segment is buy-only. `settings/segments/index-options.toml` says so in its
own note: "Phase A is buy-only index/stock options -- a bought option has no
leverage dial and cannot be liquidated." Nothing in the code enforced it.

Because orders were built from the intent's own side, a bearish intent on a call
became a **sell-to-open on that call** -- writing a naked call, whose loss is
unbounded and whose margin this account does not have:

    intent_id  "upstox|NIFTY 24000 CE 08 SEP 26|short|open"    side: "sell"

31 of 63 contract-named intents were `short`. None reached a fill only because
the feed-jump bar and the tick-unit defect refused them first, and both of those
are now fixed.

## What an intent names, and what to do about it

Measured over the intents actually formed: **63 of 69 name an option contract**,
6 name a cash equity, and **none names an index underlying**. The selector's
registry is keyed by underlying, which is why every lookup missed.

A contract-named intent is directional information about that contract's
underlying, so the selector resolves it and re-expresses it. While the segment is
buy-only, a sell is converted rather than refused -- **the user's decision**, and
the better one: refusing would discard a bearish belief the bot genuinely holds.

| intent | view on the underlying | expressed as |
|---|---|---|
| long CE | bullish | buy ATM CE |
| short CE | bearish | **buy ATM PE** |
| long PE | bearish | buy ATM PE |
| short PE | bullish | **buy ATM CE** |

One rule: `wants_bullish = (is_call == intent.is_long)`, then always buy.

**This preserves direction, not economics, and that is deliberate.** Selling a
call collects premium and earns time decay; buying a put pays premium and suffers
it. The converted trade needs the underlying to actually move where the original
would have profited from it standing still. Accepted knowingly while buy-only,
and the conversion is counted so its cost can be read later rather than inferred.

Until a sell-options bot exists, that is the whole of it. When one does, this is
the single place that changes.

## The change

- `InstrumentChoice` gains `order_side`: the side the chosen contract is traded
  on to open the intended view. For an option it is always `buy` while this
  segment is buy-only; for a perpetual it follows the view, so the field
  generalises rather than hard-coding one segment's rule.
- The selector resolves a contract-named intent to its underlying and judges the
  instruments against the converted view, not against the intent's raw side.
- An intent naming something with no option chain in this segment is refused as
  `this-underlying-has-no-option-chain-in-this-segment`, which is a different
  fact from "nothing is listed" and points at a different thing to go and fix.
- `position-sizer` builds an **open** from the choice: the chosen contract's
  symbol and the choice's `order_side`. An open with no actionable choice is
  refused by name instead of falling back to the intent's own symbol and side.
  `reduce` and `close` are untouched -- those act on the contract actually held,
  which is not a fresh selection.

No part's `consumes` or `produces` changes. What changes is which part's decision
reaches the venue, and that is what this edit records.
