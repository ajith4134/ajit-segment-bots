"""A level that has not changed must not go onto the bus again.

The failure this file exists to prevent was measured on the live spine at 10:14
on 2026-08-26. Every part publishes health once a second, so each of the eleven
parts consuming `part-health` woke 327 times a second, and each of those wakes
ran a tick that ended in an unconditional publish of a level nobody had changed:

    failing-part-detector    26,575 msg/s published from    283 msg/s received
    part-restart-budgeter    17,106 msg/s published from  6,963 msg/s received

89,747 messages a second across the spine, load average 28.7 on twelve cores,
and seven parts reading as silent because they could not get enough CPU to send
the heartbeat that proves they are alive. The storm fed itself: a starved part
misses a heartbeat, the detector republishes that fault on every tick, the warden
escalates on every fault, and all of it is CPU the starved part needed.

Two properties have to hold together, and either one alone is a defect:

  * an unchanged level is not republished -- otherwise this class does nothing;
  * an unchanged level IS republished once per refresh interval -- otherwise a
    consumer that starts after the last change never learns the level at all, and
    a consumer bounding its inputs by `maximum_age_seconds` ages out a level that
    is still true. That is the shape this project has already paid for once.

Levels here are built from real captured trades (RL-063). A test that invented
its own payloads would be testing that two dicts written by hand compare equal,
which is not the question -- the question is whether the digest tells apart two
levels computed from market data that really did move.
"""

from __future__ import annotations

import pytest

from runtime.level_publishing import (
    LevelPublisher,
    describe_level_publishing,
    digest_of,
)


class FakeClock:
    """Monotonic time the test advances by hand, so no test waits on a real second."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _recording_publisher(refresh_interval_seconds: float = 1.0):
    sent: list[tuple] = []
    clock = FakeClock()
    publisher = LevelPublisher(
        publish=sent.append,
        refresh_interval_seconds=refresh_interval_seconds,
        monotonic=clock,
    )
    return publisher, sent, clock


# How many prints make up the level these tests publish. A rolling window of the
# most recent prices is the shape most levels in this system actually have -- a
# detector's window, a sampler's tail -- and it is a level whose ordering matters,
# which a last-price-per-symbol map would not have been: the captured run is one
# symbol, so that map held a single entry and could not tell orderings apart.
LEVEL_PRINTS = 20


def _price_level(trades) -> tuple:
    """The shape a level actually has here: the most recent prints, as a window holds them."""
    return tuple(float(trade.price) for trade in trades[-LEVEL_PRINTS:])


# ---- the two properties, against levels built from real market data -----------


def test_an_unchanged_level_is_published_once_and_then_skipped(read_captured_trades):
    """The storm, reduced: the same level offered on every tick goes out once."""
    trades = read_captured_trades(limit=200)
    level = _price_level(trades)
    publisher, sent, _clock = _recording_publisher(refresh_interval_seconds=1.0)

    # 327 ticks is one second of wakes for a part consuming part-health, which is
    # exactly the rate the live spine measured.
    went = [publisher.publish_level(level) for _ in range(327)]

    assert sum(went) == 1, "an unchanged level went onto the bus more than once"
    assert len(sent) == 1
    assert publisher.standing.unchanged_publishes_skipped == 326
    assert publisher.standing.changes == 1
    assert publisher.standing.refreshes == 0


def test_a_level_that_moved_is_published_again(read_captured_trades):
    """Real prices that really moved must not be mistaken for the same level."""
    trades = read_captured_trades(limit=400)
    first = _price_level(trades[:200])
    second = _price_level(trades)
    assert first != second, (
        "the captured window did not move, so this test would pass without "
        "the publisher distinguishing anything"
    )

    publisher, sent, _clock = _recording_publisher(refresh_interval_seconds=1.0)
    assert publisher.publish_level(first) is True
    assert publisher.publish_level(first) is False
    assert publisher.publish_level(second) is True

    assert len(sent) == 2
    assert publisher.standing.changes == 2


def test_an_unchanged_level_is_refreshed_once_per_interval(read_captured_trades):
    """A consumer that starts late still learns a level that stopped changing."""
    level = _price_level(read_captured_trades(limit=200))
    publisher, sent, clock = _recording_publisher(refresh_interval_seconds=1.0)

    publisher.publish_level(level)
    # 327 wakes is one second of part-health for a part consuming it. The clock is
    # advanced past the interval rather than exactly onto it: 327 additions of
    # 1/327 land just under 1.0 in binary floating point, and a test that turned
    # on which side of that it fell would be testing the arithmetic, not the rule.
    for _ in range(327):
        clock.advance(1.0 / 327.0)
        publisher.publish_level(level)
    clock.advance(1.0 / 327.0)
    publisher.publish_level(level)

    assert len(sent) == 2, (
        f"one second of unchanged ticks should refresh exactly once, got {len(sent)}"
    )
    assert publisher.standing.refreshes == 1
    assert publisher.standing.changes == 1


def test_a_change_restarts_the_refresh_interval(read_captured_trades):
    """A level said because it changed is not also said again as a refresh."""
    trades = read_captured_trades(limit=400)
    first, second = _price_level(trades[:200]), _price_level(trades)
    publisher, sent, clock = _recording_publisher(refresh_interval_seconds=1.0)

    publisher.publish_level(first)
    clock.advance(0.9)
    publisher.publish_level(second)   # a change, 0.9s in
    clock.advance(0.5)
    publisher.publish_level(second)   # 1.4s since the first, 0.5s since the change

    assert len(sent) == 2
    assert publisher.standing.refreshes == 0


# ---- empty, which is a value and not an absence -------------------------------


def test_a_level_becoming_empty_is_said_once(read_captured_trades):
    """A withdrawn level must reach readers; a level empty all along must not repeat."""
    level = _price_level(read_captured_trades(limit=200))
    publisher, sent, _clock = _recording_publisher(refresh_interval_seconds=1.0)

    publisher.publish_level(level)
    assert publisher.publish_level(()) is True, "a level that became empty was never said"
    assert publisher.publish_level(()) is False, "empty was repeated on the next tick"
    assert len(sent) == 2
    assert sent[-1] == ()


def test_the_first_publish_always_goes_out(read_captured_trades):
    """Nothing has been said yet, so there is no unchanged level to compare against."""
    publisher, sent, _clock = _recording_publisher(refresh_interval_seconds=1.0)
    assert publisher.publish_level(()) is True
    assert len(sent) == 1


# ---- the digest ---------------------------------------------------------------


def test_the_digest_separates_levels_that_differ_only_in_order(read_captured_trades):
    """Two orderings are two wire payloads, and a reader can tell them apart."""
    level = _price_level(read_captured_trades(limit=200))
    assert len(set(level)) > 1, "need more than one distinct print for order to mean anything"
    assert digest_of(level) != digest_of(tuple(reversed(level)))


def test_the_digest_is_stable_across_equal_rebuilds(read_captured_trades):
    """The same trades, decoded twice, fingerprint identically -- or nothing is skipped."""
    trades = read_captured_trades(limit=200)
    assert digest_of(_price_level(trades)) == digest_of(_price_level(list(trades)))


# ---- refusals -----------------------------------------------------------------


@pytest.mark.parametrize("interval", [0.0, -1.0])
def test_a_non_positive_refresh_interval_is_refused(interval):
    """Zero republishes on every tick while claiming to have stopped doing so."""
    with pytest.raises(ValueError, match="refresh_interval_seconds must be positive"):
        LevelPublisher(publish=lambda _items: None, refresh_interval_seconds=interval)


# ---- what a part's health carries ---------------------------------------------


def test_standing_names_each_data_type_and_totals_them(read_captured_trades):
    """A part publishing two levels must be readable one level at a time."""
    trades = read_captured_trades(limit=400)
    first, second = _price_level(trades[:200]), _price_level(trades)
    one, _sent_one, _c1 = _recording_publisher()
    two, _sent_two, _c2 = _recording_publisher()

    one.publish_level(first)
    one.publish_level(first)
    two.publish_level(second)

    described = describe_level_publishing({"part-fault": one, "restart-budget": two})
    assert described["part-fault_published"] == 1.0
    assert described["part-fault_unchanged_skipped"] == 1.0
    assert described["restart-budget_published"] == 1.0
    assert described["levels_published"] == 2.0
    assert described["unchanged_publishes_skipped"] == 1.0


# ---- one level per key --------------------------------------------------------


def test_one_key_changing_does_not_republish_the_others(read_captured_trades):
    """The storm with one more step in it: a shared digest would resend everything."""
    from runtime.level_publishing import LevelPublisherByKey

    trades = read_captured_trades(limit=400)
    first, second = _price_level(trades[:200]), _price_level(trades)
    sent: list[tuple] = []
    clock = FakeClock()
    publisher = LevelPublisherByKey(
        publish=sent.append, refresh_interval_seconds=1.0, monotonic=clock
    )

    for part_id in ("bull-bot", "bear-bot", "tailgater"):
        publisher.publish_level(part_id, first)
    assert len(sent) == 3

    publisher.publish_level("bull-bot", second)
    for part_id in ("bear-bot", "tailgater"):
        publisher.publish_level(part_id, first)

    assert len(sent) == 4, "a change under one key republished the others"
    assert publisher.standing.unchanged_publishes_skipped == 2


def test_each_key_keeps_its_own_refresh_clock(read_captured_trades):
    """Keys first said at different moments must not line up into one burst."""
    from runtime.level_publishing import LevelPublisherByKey

    level = _price_level(read_captured_trades(limit=200))
    sent: list[tuple] = []
    clock = FakeClock()
    publisher = LevelPublisherByKey(
        publish=sent.append, refresh_interval_seconds=1.0, monotonic=clock
    )

    publisher.publish_level("bull-bot", level)
    clock.advance(0.5)
    publisher.publish_level("bear-bot", level)
    assert len(sent) == 2

    clock.advance(0.6)  # 1.1s for bull-bot, 0.6s for bear-bot
    publisher.publish_level("bull-bot", level)
    publisher.publish_level("bear-bot", level)
    assert len(sent) == 3, "both keys refreshed at once despite starting half a second apart"


def test_a_forgotten_key_is_no_longer_held(read_captured_trades):
    """A subject that is gone for good must not be remembered for the run's length."""
    from runtime.level_publishing import LevelPublisherByKey

    level = _price_level(read_captured_trades(limit=200))
    publisher = LevelPublisherByKey(
        publish=lambda _items: None, refresh_interval_seconds=1.0, monotonic=FakeClock()
    )
    publisher.publish_level("bull-bot", level)
    assert publisher.keys_held == 1
    publisher.forget("bull-bot")
    assert publisher.keys_held == 0


# ---- a snapshot on a cadence, for a level that always differs -----------------


def test_a_snapshot_is_published_at_most_once_per_interval(read_captured_trades):
    """The heartbeat table changes every tick, so only a rate can bound it."""
    from runtime.level_publishing import PacedPublisher

    trades = read_captured_trades(limit=400)
    sent: list[tuple] = []
    clock = FakeClock()
    publisher = PacedPublisher(publish=sent.append, interval_seconds=1.0, monotonic=clock)

    # Every snapshot differs, exactly as a table of climbing counters does.
    for index in range(327):
        clock.advance(1.0 / 327.0)
        publisher.publish_snapshot((float(trades[index % len(trades)].price), index))

    assert len(sent) == 1, f"a snapshot went out {len(sent)} times in one second"
    assert publisher.standing.unchanged_publishes_skipped == 326


def test_is_due_lets_a_caller_skip_building_the_snapshot(read_captured_trades):
    """Building the table is the expensive half; refusing only to send still pays it."""
    from runtime.level_publishing import PacedPublisher

    clock = FakeClock()
    publisher = PacedPublisher(publish=lambda _items: None, interval_seconds=1.0, monotonic=clock)

    assert publisher.is_due() is True, "nothing has been published, so one is due"
    publisher.publish_snapshot(())
    assert publisher.is_due() is False
    clock.advance(1.0)
    assert publisher.is_due() is True


@pytest.mark.parametrize("interval", [0.0, -1.0])
def test_a_non_positive_snapshot_interval_is_refused(interval):
    from runtime.level_publishing import PacedPublisher

    with pytest.raises(ValueError, match="interval_seconds must be positive"):
        PacedPublisher(publish=lambda _items: None, interval_seconds=interval)


# ---- the "when I looked" fields, which defeat a whole-payload comparison -------


def test_a_payload_stamped_fresh_every_read_is_still_recognised_as_unchanged():
    """The defect that made the first version of this module do nothing at all.

    Nearly every payload here carries `measured_at_ns` or `observed_at_ns`,
    stamped at the moment the part looked. Compared whole, two statements of one
    unchanged level are never equal -- so nothing is skipped, the storm survives,
    and the skip counter says zero while claiming the fix is in.
    """
    import dataclasses

    from runtime.level_publishing import without_observation_time

    @dataclasses.dataclass(frozen=True)
    class Profile:
        symbol: str
        adverse_excursion: float
        measured_at_ns: int

    publisher, sent, _clock = _recording_publisher(refresh_interval_seconds=1.0)
    publisher.identity_of = without_observation_time

    for stamp in range(1, 328):
        publisher.publish_level((Profile("BTCUSDT", 0.0019, stamp),))

    assert len(sent) == 1, f"a level restamped on every read went out {len(sent)} times"
    assert publisher.standing.unchanged_publishes_skipped == 326
    # What went on the wire keeps its timestamp: a reader judging staleness needs
    # the moment the part actually looked.
    assert sent[0][0].measured_at_ns == 1


def test_a_real_change_still_gets_through_with_the_stamp_stripped():
    """Stripping the noticing must not strip the finding."""
    import dataclasses

    from runtime.level_publishing import without_observation_time

    @dataclasses.dataclass(frozen=True)
    class Profile:
        symbol: str
        adverse_excursion: float
        measured_at_ns: int

    publisher, sent, _clock = _recording_publisher(refresh_interval_seconds=1.0)
    publisher.identity_of = without_observation_time

    publisher.publish_level((Profile("BTCUSDT", 0.0019, 1),))
    publisher.publish_level((Profile("BTCUSDT", 0.0019, 2),))
    publisher.publish_level((Profile("BTCUSDT", 0.0031, 3),))

    assert len(sent) == 2
    assert sent[-1][0].adverse_excursion == 0.0031


def test_a_timestamp_that_is_content_is_not_stripped():
    """`next_settlement_at_ns` is when the venue will charge, not when we looked."""
    import dataclasses

    from runtime.level_publishing import without_observation_time

    @dataclasses.dataclass(frozen=True)
    class Premium:
        symbol: str
        mark_price: float
        next_settlement_at_ns: int
        measured_at_ns: int

    publisher, sent, _clock = _recording_publisher(refresh_interval_seconds=1.0)
    publisher.identity_of = without_observation_time

    publisher.publish_level((Premium("BTCUSDT", 79041.5, 1_000, 1),))
    publisher.publish_level((Premium("BTCUSDT", 79041.5, 1_000, 2),))   # only we moved
    publisher.publish_level((Premium("BTCUSDT", 79041.5, 2_000, 3),))   # the venue moved

    assert len(sent) == 2, "a settlement time moving is a change a reader acts on"


def test_a_payload_that_is_not_a_dataclass_is_left_alone():
    """Nothing to strip, and nothing to guess about."""
    from runtime.level_publishing import without_observation_time

    assert without_observation_time((1, "two", (3,))) == (1, "two", (3,))
