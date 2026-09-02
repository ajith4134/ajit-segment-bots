"""price-level-sampler: one frame per venue per tick, not one message per trade.

The part this system needed before it could grow. On 2026-08-23 every one of 193
prints a second was handed to 66 parts -- 12,707 deliveries a second -- and that
total scales with trading volume, which is what grows when the universe grows.
Thirty-seven of those parts read nothing from a trade but the symbol, the price
and the moment it printed.

What the frame must get right is exactly what the last two days were about: a
level travels with the moment it printed, never with the moment it was published.
A frame is not a claim that every symbol in it just traded -- it is a claim about
what each symbol last did, and when.
"""

from __future__ import annotations

import pytest

from parts.market_data_feed.price_level_sampler import (
    PART_DECLARATION,
    PART_ID,
    PriceLevelSampler,
    describe_sampling,
)
from runtime.bus import encode_frame
from runtime.part_declaration import load_declaration_from_blueprint

SECOND_NS = 1_000_000_000
VENUE = "binance-usdm"
# The operator's own values, restated here so what the test exercises is visible.
CONFIGURED_MAXIMUM_SYMBOLS = 2_000
MAXIMUM_MESSAGE_BYTES = 131_072


class Trade:
    """A normalised trade, as this part reads it."""

    def __init__(self, symbol, price, at_ns, venue_id=VENUE):
        self.venue_id = venue_id
        self.symbol = symbol
        self.price = price
        self.venue_time_ns = at_ns


def a_sampler(cadence=0.25, maximum_symbols_per_frame=2_000):
    return PriceLevelSampler(
        cadence_seconds=cadence,
        maximum_symbols_per_frame=maximum_symbols_per_frame,
        maximum_frame_bytes=MAXIMUM_MESSAGE_BYTES,
    )


def test_the_built_declaration_equals_the_blueprint():
    assert PART_DECLARATION == load_declaration_from_blueprint(PART_ID)


def test_a_frame_carries_each_symbol_s_last_price_and_when_it_printed():
    subject = a_sampler()
    subject.observe_trade(Trade("BTCUSDT", 77_000.0, 1 * SECOND_NS))
    subject.observe_trade(Trade("BTCUSDT", 77_100.0, 2 * SECOND_NS))
    subject.observe_trade(Trade("ENAUSDT", 0.17019, 1 * SECOND_NS))

    frames = subject.frames_due(now_ns=10 * SECOND_NS)

    assert len(frames) == 1
    levels = {level.symbol: level for level in frames[0].levels}
    assert levels["BTCUSDT"].price == 77_100.0
    assert levels["BTCUSDT"].observed_at_ns == 2 * SECOND_NS
    assert levels["ENAUSDT"].observed_at_ns == 1 * SECOND_NS


def test_a_frame_is_not_a_claim_that_every_symbol_just_traded():
    """The whole point. A frame published now carries prices from whenever each
    symbol last printed, and every reader's staleness bound is measured against
    those, not against the frame."""
    subject = a_sampler()
    subject.observe_trade(Trade("BTCUSDT", 77_000.0, 1 * SECOND_NS))
    subject.observe_trade(Trade("QUIETUSDT", 4.0, 1 * SECOND_NS))
    subject.observe_trade(Trade("BTCUSDT", 77_100.0, 3_000 * SECOND_NS))

    frame = subject.frames_due(now_ns=3_000 * SECOND_NS)[0]

    levels = {level.symbol: level for level in frame.levels}
    assert levels["QUIETUSDT"].observed_at_ns == 1 * SECOND_NS, (
        "a symbol that has not traded in an hour must say so, not inherit the frame's time"
    )
    assert frame.published_at_ns == 3_000 * SECOND_NS
    assert frame.published_at_ns != levels["QUIETUSDT"].observed_at_ns


def test_one_frame_per_venue():
    """A frame is per venue because the bus refuses a datagram over 128 KiB and
    the full universe does not fit in one."""
    subject = a_sampler()
    subject.observe_trade(Trade("BTCUSDT", 77_000.0, SECOND_NS, venue_id="binance-usdm"))
    subject.observe_trade(Trade("BTCUSDT", 77_020.0, SECOND_NS, venue_id="bybit-linear"))

    frames = subject.frames_due(now_ns=10 * SECOND_NS)

    assert {frame.venue_id for frame in frames} == {"binance-usdm", "bybit-linear"}
    assert all(len(frame.levels) == 1 for frame in frames)


def test_nothing_is_due_before_the_cadence_elapses():
    subject = a_sampler(cadence=0.25)
    subject.observe_trade(Trade("BTCUSDT", 77_000.0, SECOND_NS))
    assert subject.frames_due(now_ns=SECOND_NS)

    assert subject.frames_due(now_ns=SECOND_NS + 100_000_000) == ()
    assert subject.frames_due(now_ns=SECOND_NS + 250_000_000)


def test_a_venue_that_has_printed_nothing_publishes_no_frame():
    """An empty frame is not a fact about the market; it is a fact about the feed,
    and feed-gap-detector is the part that owns that."""
    assert a_sampler().frames_due(now_ns=SECOND_NS) == ()


def test_a_level_stays_in_the_frame_until_it_is_replaced():
    """A level is what is true until it changes -- that is what makes it a level.
    Every reader bounds its age for itself, from that symbol's own moves."""
    subject = a_sampler()
    subject.observe_trade(Trade("BTCUSDT", 77_000.0, SECOND_NS))
    subject.frames_due(now_ns=SECOND_NS)

    later = subject.frames_due(now_ns=3_000 * SECOND_NS)[0]
    assert later.levels[0].price == 77_000.0
    assert later.levels[0].observed_at_ns == SECOND_NS


def test_a_frame_is_split_when_it_would_be_too_large_for_the_bus():
    """At 2,590 symbol-venue pairs one frame is about 181 KB against a 131,072-byte
    ceiling. Split rather than dropped, and counted so the split is visible."""
    subject = a_sampler(maximum_symbols_per_frame=100)
    for index in range(250):
        subject.observe_trade(Trade(f"SYM{index}USDT", 1.0 + index, SECOND_NS))

    frames = subject.frames_due(now_ns=10 * SECOND_NS)

    assert len(frames) == 3
    assert [len(frame.levels) for frame in frames] == [100, 100, 50]
    assert sum(len(frame.levels) for frame in frames) == 250
    assert subject.standing.frames_split == 1


def test_a_split_frame_says_which_part_of_the_venue_it_is():
    subject = a_sampler(maximum_symbols_per_frame=2)
    for index in range(5):
        subject.observe_trade(Trade(f"SYM{index}USDT", 1.0, SECOND_NS))

    frames = subject.frames_due(now_ns=10 * SECOND_NS)

    assert [(frame.part_number, frame.of_parts) for frame in frames] == [(1, 3), (2, 3), (3, 3)]
    assert all(frame.venue_id == VENUE for frame in frames)


def test_the_real_tape_becomes_frames(read_captured_trades):
    """Against what the venue actually sent (RL-063).

    The captured run is one symbol over about half a minute, so the measurement it
    supports is the one that matters here: how many messages the readers stop
    receiving.
    """
    trades = read_captured_trades()
    subject = a_sampler(cadence=0.25)
    frames = []
    for trade in trades:
        subject.observe_trade(trade)
        frames.extend(subject.frames_due(now_ns=trade.venue_time_ns))

    assert frames, "the captured run spans several cadences and must produce frames"
    assert len(frames) < len(trades), (
        f"{len(trades)} prints became {len(frames)} frames -- the whole point is that "
        f"this is fewer"
    )
    for frame in frames:
        for level in frame.levels:
            assert level.observed_at_ns <= frame.published_at_ns, (
                "a level cannot have printed after the frame carrying it was published"
            )


def test_a_cadence_that_is_not_positive_is_refused():
    with pytest.raises(ValueError):
        a_sampler(cadence=0.0)
    with pytest.raises(ValueError):
        a_sampler(cadence=-1.0)


def test_a_frame_bound_below_one_symbol_is_refused():
    with pytest.raises(ValueError):
        a_sampler(maximum_symbols_per_frame=0)


def test_what_it_says_about_itself_is_countable():
    subject = a_sampler()
    subject.observe_trade(Trade("BTCUSDT", 77_000.0, SECOND_NS))
    subject.frames_due(now_ns=10 * SECOND_NS)

    described = describe_sampling(subject)
    assert described["part_id"] == PART_ID
    assert described["symbols_tracked"] == 1
    assert described["frames_published"] == 1
    assert described["trades_observed"] == 1


def test_every_frame_fits_the_bus_at_the_configured_cap():
    """A count cap cannot know how large a frame is, measured 2026-09-02.

    `price_frame_maximum_symbols` is 2000 and is read by this part and by
    quote-level-sampler alike, but a price level encodes to about 53 bytes and a
    quote level to 86 -- so one number cannot bound both, and for the quote frame
    2000 symbols is 172,280 bytes against a 131,072-byte ceiling. A frame over
    the ceiling is not split, it is refused whole by `encode_frame`, and the part
    goes on ticking while publishing nothing.

    The bound that matters is the one the bus actually enforces, so the frame is
    measured with the bus's own encoder rather than counted.
    """
    subject = a_sampler(maximum_symbols_per_frame=CONFIGURED_MAXIMUM_SYMBOLS)
    for index in range(CONFIGURED_MAXIMUM_SYMBOLS):
        subject.observe_trade(Trade(f"SYMBOL{index:05d}USDT", 1.0 + index, SECOND_NS))

    frames = subject.frames_due(now_ns=10 * SECOND_NS)

    assert frames, "the sampler saw the whole universe and must publish it"
    for number, frame in enumerate(frames, start=1):
        encode_frame(
            data_type="price-frame",
            producer_part_id=PART_ID,
            sequence=number,
            published_at_ns=10 * SECOND_NS,
            payload=frame,
            maximum_message_bytes=MAXIMUM_MESSAGE_BYTES,
        )
    assert sum(len(frame.levels) for frame in frames) == CONFIGURED_MAXIMUM_SYMBOLS, (
        "splitting must not lose a symbol"
    )
