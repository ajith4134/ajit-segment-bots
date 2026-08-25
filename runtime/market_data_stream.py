"""Reading one kind of thing out of `market-data`, which carries several.

`market-data` is not one shape. `venue-trade-stream-reader` publishes trades and
`ccxt-venue-reader` publishes candles, and the blueprint has always said so --
both declare `produces: market-data`, and `kline-window-builder` has always
filtered the stream for candles because it only wants those.

Every other reader was written when trades were the only thing on it. On
2026-08-25 the candle reader was switched on for the first time, and
`peak-excursion-tracker` immediately began crash-looping on

    AttributeError: 'NormalisedCandle' object has no attribute 'price'

857 restarts before it was noticed, on the part whose whole output is the extreme
a position reached -- which is what stop placement is learned from.

**The rule, already written down elsewhere in this system and not followed here.**
`runtime/price_frames.levels_in` says it: *anything that is not a frame is skipped
rather than raised on. An inbox carries what the wiring delivers, and a part that
died on an unexpected shape would be a part the wiring could kill.* A reader that
assumes its inbox holds only what it happens to want is a reader that a new
producer can kill from a distance, without touching it.

So this is the door, and it is shared rather than repeated: a part that wants
trades says so, and gets trades.

**Skipped, never counted as absent.** A candle arriving where a trade was wanted
is not a gap in the trade stream -- it is a different fact on the same wire -- so
these skip quietly rather than reporting loss. What a part must never do is treat
the filtered result as the whole stream when deciding it saw nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator


def trades_in(updates: Iterable) -> Iterator:
    """Every trade in a market-data batch, and nothing else.

    Identified by the fields a trade carries rather than by its class: the tape
    replays through a venue adapter and a test builds its own, and a check on the
    type name would pass one and refuse the other for no reason a reader cares
    about.
    """
    for update in updates:
        if getattr(update, "price", None) is None:
            continue
        if getattr(update, "symbol", None) is None:
            continue
        yield update


def candles_in(updates: Iterable) -> Iterator:
    """Every candle in a market-data batch, and nothing else.

    A candle has an open and a close where a trade has one price. Kept beside
    `trades_in` so the two halves of this stream are named in one place, and a
    part that wants candles never has to work out how to tell them apart.
    """
    for update in updates:
        if getattr(update, "close", None) is None:
            continue
        if getattr(update, "open_time_ns", None) is None:
            continue
        yield update
