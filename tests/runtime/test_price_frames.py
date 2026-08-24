"""Reading a frame back into the levels a part actually works with.

Thirty-seven parts stopped receiving every trade and started receiving a frame of
every symbol's latest price. What they do with a price did not change, so what
they read has to arrive in the same shape -- one level at a time, carrying the
venue, the symbol, the price and the moment it printed.

The one thing that must never be lost in the translation is the moment. A level
read out of a frame carries the time the market made it, never the time the frame
was published.
"""

from __future__ import annotations

from parts.market_data_feed.price_level_sampler import SymbolPriceFrame, SymbolPriceLevel
from runtime.price_frames import levels_in

SECOND_NS = 1_000_000_000


def a_frame(venue_id="binance-usdm", levels=(), published_at_ns=100 * SECOND_NS):
    return SymbolPriceFrame(
        venue_id=venue_id,
        levels=tuple(levels),
        published_at_ns=published_at_ns,
        part_number=1,
        of_parts=1,
    )


def test_a_frame_becomes_one_level_per_symbol():
    frame = a_frame(levels=[
        SymbolPriceLevel("BTCUSDT", 77_000.0, 1 * SECOND_NS),
        SymbolPriceLevel("ENAUSDT", 0.17019, 2 * SECOND_NS),
    ])

    levels = list(levels_in([frame]))

    assert [(level.venue_id, level.symbol, level.price) for level in levels] == [
        ("binance-usdm", "BTCUSDT", 77_000.0),
        ("binance-usdm", "ENAUSDT", 0.17019),
    ]


def test_a_level_keeps_the_moment_the_market_made_it():
    """Never the moment the frame was published. That substitution is the whole
    defect this project spent two days removing."""
    frame = a_frame(
        levels=[SymbolPriceLevel("QUIETUSDT", 4.0, 1 * SECOND_NS)],
        published_at_ns=3_000 * SECOND_NS,
    )

    level = next(iter(levels_in([frame])))

    assert level.observed_at_ns == 1 * SECOND_NS
    assert level.observed_at_ns != frame.published_at_ns


def test_every_venue_s_frame_is_read():
    frames = [
        a_frame("binance-usdm", [SymbolPriceLevel("BTCUSDT", 77_000.0, SECOND_NS)]),
        a_frame("bybit-linear", [SymbolPriceLevel("BTCUSDT", 77_020.0, SECOND_NS)]),
    ]

    assert {level.venue_id for level in levels_in(frames)} == {"binance-usdm", "bybit-linear"}


def test_a_split_frame_reads_as_the_levels_it_carries():
    """Nothing downstream has to know a frame was split; it reads levels."""
    first = SymbolPriceFrame("binance-usdm", (SymbolPriceLevel("A", 1.0, SECOND_NS),),
                             100 * SECOND_NS, 1, 2)
    second = SymbolPriceFrame("binance-usdm", (SymbolPriceLevel("B", 2.0, SECOND_NS),),
                              100 * SECOND_NS, 2, 2)

    assert [level.symbol for level in levels_in([first, second])] == ["A", "B"]


def test_anything_that_is_not_a_frame_is_skipped():
    """A part's inbox carries what its wiring delivers, and a shape it does not
    recognise is not a reason to die -- the same rule every other reader keeps."""
    frame = a_frame(levels=[SymbolPriceLevel("BTCUSDT", 77_000.0, SECOND_NS)])

    assert len(list(levels_in([frame, object(), None]))) == 1


def test_no_frames_is_no_levels():
    assert list(levels_in([])) == []
