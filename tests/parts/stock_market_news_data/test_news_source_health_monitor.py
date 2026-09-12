"""news-source-health-monitor, driven by real arrivals.

RL-063: the arrival times here are the real publish stamps of the 42 rows
captured from Upstox on 2026-09-12 for the 30 underlyings the option segments
actually track — 31 distinct stories across 167.08 hours, so 30 real gaps. A
fixture with regular one-minute arrivals would make a silence bound look easy to
set; the real thing has a median gap of 1.73 hours, a 95th percentile of 17.92
and a longest of 28.37, which is the whole difficulty.

**This suite is the reason `news_source_minimum_gaps_to_state_a_habit` is 20.**
Written first against the narrower 2-underlying capture, it failed: 13 gaps give
a nearest-rank 95th percentile *equal to* the maximum, so `QUIETER_THAN_USUAL`
could never be rendered and silence jumped from delivering straight to not
delivering. Rule 8 refuses a display whose middle state is unreachable. The
separation first holds at 20 observations, which is arithmetic, and this capture
has 30.
"""

from __future__ import annotations

import json
import math
import pathlib

import pytest

from parts.stock_market_news_data.broker_news_reader import (
    BrokerNewsReader,
    SOURCE_ID,
)
from parts.stock_market_news_data.news_source_health_monitor import (
    CORPORATE_ACTION_SOURCE_ID,
    NewsSourceHealthMonitor,
    describe_monitoring,
    latest_delivery_per_source,
)
from runtime.news_types import Delivery

CAPTURE = (
    pathlib.Path(__file__).resolve().parents[3]
    / "tests/captured/upstox/2026-09-12-news-for-thirty-underlyings.json"
)
# The narrower capture, kept for the one test that proves why it is not enough.
CAPTURE_OF_TWO_UNDERLYINGS = (
    pathlib.Path(__file__).resolve().parents[3]
    / "tests/captured/upstox/2026-09-12-news-for-two-underlyings.json"
)

NANOSECONDS_PER_SECOND = 1_000_000_000
# As `settings/runtime.example.toml` derives them.
WINDOW = 500
MINIMUM_GAPS = 20


@pytest.fixture(scope="module")
def real_publish_times() -> tuple[int, ...]:
    """When the real stories were actually published, oldest first.

    Used as the arrival sequence: a source's rhythm is the rhythm of its
    stories, and these are the only real ones this project has.
    """
    return _publish_times_of(CAPTURE)


def _nearest_rank(quantile: float, count: int) -> int:
    """The index QuantileEstimator would take, so this suite checks its rule."""
    return min(count - 1, max(0, math.ceil(quantile * count) - 1))


def _gaps_seconds(arrivals) -> list[float]:
    return sorted(
        (later - earlier) / NANOSECONDS_PER_SECOND
        for earlier, later in zip(arrivals, arrivals[1:])
    )


def _publish_times_of(capture: pathlib.Path) -> tuple[int, ...]:
    reader = BrokerNewsReader(fetch=lambda url, token: {}, now_ns=lambda: 0)
    return tuple(
        sorted({item.published_at_ns for item in reader.items_in(json.loads(capture.read_text()))})
    )


def a_monitor(now_ns, minimum_gaps: int = MINIMUM_GAPS) -> NewsSourceHealthMonitor:
    return NewsSourceHealthMonitor(
        window=WINDOW, minimum_gaps_to_state_a_habit=minimum_gaps, now_ns=now_ns
    )


def test_the_capture_is_the_shape_this_suite_claims(real_publish_times):
    """31 real stories, 30 gaps — just past the 20 the middle state needs."""
    assert len(real_publish_times) == 31
    assert len(real_publish_times) - 1 == 30 >= MINIMUM_GAPS


def test_all_three_delivery_states_are_reachable_on_this_source(real_publish_times):
    """Rule 8: a board with no way to render its middle state is untested.

    The 2-underlying capture cannot do this — 13 gaps put the 95th percentile
    exactly on the maximum — and that is what set the minimum at 20.
    """
    gaps = _gaps_seconds(real_publish_times)
    usual = gaps[_nearest_rank(0.5, len(gaps))]
    unusually_quiet = gaps[_nearest_rank(0.95, len(gaps))]
    longest = gaps[-1]
    assert usual < unusually_quiet < longest

    narrow = _publish_times_of(CAPTURE_OF_TWO_UNDERLYINGS)
    narrow_gaps = _gaps_seconds(narrow)
    assert len(narrow_gaps) == 13
    assert (
        narrow_gaps[_nearest_rank(0.95, len(narrow_gaps))] == narrow_gaps[-1]
    ), "13 gaps cannot separate the 95th percentile from the maximum"


def test_a_source_is_not_measured_until_it_has_shown_enough_gaps(real_publish_times):
    """Rule 8: unmeasured is its own state, and it is not a healthy one."""
    clock = {"now": real_publish_times[0]}
    monitor = a_monitor(now_ns=lambda: clock["now"])
    for arrival in real_publish_times[:4]:
        monitor.observe_delivery(SOURCE_ID, arrival)
    clock["now"] = real_publish_times[3]
    standing = monitor.standing_of(SOURCE_ID)
    assert standing.delivery is Delivery.NOT_MEASURED
    assert standing.typical_seconds_between_items is None
    assert standing.items_seen == 4


def test_a_source_delivering_at_its_own_rhythm_reads_delivering(real_publish_times):
    clock = {"now": real_publish_times[-1]}
    monitor = a_monitor(now_ns=lambda: clock["now"])
    for arrival in real_publish_times:
        monitor.observe_delivery(SOURCE_ID, arrival)
    standing = monitor.standing_of(SOURCE_ID)
    assert standing.delivery is Delivery.DELIVERING
    assert standing.seconds_since_last_item == 0.0
    assert standing.typical_seconds_between_items > 0
    assert standing.items_seen == 31


def test_the_typical_gap_is_this_source_own_median_gap(real_publish_times):
    clock = {"now": real_publish_times[-1]}
    monitor = a_monitor(now_ns=lambda: clock["now"])
    for arrival in real_publish_times:
        monitor.observe_delivery(SOURCE_ID, arrival)
    gaps = _gaps_seconds(real_publish_times)
    # Nearest-rank median of the 30 observed gaps, the same rule
    # QuantileEstimator uses -- an answer that was actually observed, never an
    # interpolation between two that were not. Measured: 6,242.3 seconds,
    # which is 1.73 hours between stories across 30 NSE option underlyings.
    assert monitor.standing_of(SOURCE_ID).typical_seconds_between_items == pytest.approx(
        gaps[_nearest_rank(0.5, len(gaps))]
    )
    assert gaps[_nearest_rank(0.5, len(gaps))] == pytest.approx(6242.3, abs=0.1)


def test_quiet_past_the_95th_percentile_but_inside_the_record_is_a_finding(
    real_publish_times,
):
    """QUIETER_THAN_USUAL is a finding, not a fault. Indian news stops overnight."""
    clock = {"now": real_publish_times[-1]}
    monitor = a_monitor(now_ns=lambda: clock["now"])
    for arrival in real_publish_times:
        monitor.observe_delivery(SOURCE_ID, arrival)
    gaps = _gaps_seconds(real_publish_times)
    longest = gaps[-1]
    unusually_quiet = gaps[_nearest_rank(0.95, len(gaps))]
    assert unusually_quiet < longest, "the real gaps must have a tail for this to mean anything"

    clock["now"] = real_publish_times[-1] + int(
        (unusually_quiet + longest) / 2 * NANOSECONDS_PER_SECOND
    )
    assert monitor.standing_of(SOURCE_ID).delivery is Delivery.QUIETER_THAN_USUAL


def test_quiet_longer_than_it_has_ever_been_is_not_delivering(real_publish_times):
    """The bound is the source's own record, so no multiple and no literal."""
    clock = {"now": real_publish_times[-1]}
    monitor = a_monitor(now_ns=lambda: clock["now"])
    for arrival in real_publish_times:
        monitor.observe_delivery(SOURCE_ID, arrival)
    longest_gap_ns = max(
        later - earlier for earlier, later in zip(real_publish_times, real_publish_times[1:])
    )

    clock["now"] = real_publish_times[-1] + longest_gap_ns + NANOSECONDS_PER_SECOND
    standing = monitor.standing_of(SOURCE_ID)
    assert standing.delivery is Delivery.NOT_DELIVERING
    assert standing.seconds_since_last_item > standing.typical_seconds_between_items


def test_the_overnight_gap_folds_itself_into_the_record(real_publish_times):
    """A bound learned from the source cannot fire every morning.

    The real capture already contains overnight gaps -- the stories run 05:11 to
    14:27 UTC across six days -- so once they are in the record, a silence the
    length of a night is inside it and reads as quiet rather than as dead.
    """
    clock = {"now": real_publish_times[-1]}
    monitor = a_monitor(now_ns=lambda: clock["now"])
    for arrival in real_publish_times:
        monitor.observe_delivery(SOURCE_ID, arrival)
    overnight_seconds = 14 * 3600
    clock["now"] = real_publish_times[-1] + overnight_seconds * NANOSECONDS_PER_SECOND
    assert monitor.standing_of(SOURCE_ID).delivery is not Delivery.NOT_DELIVERING


def test_every_source_that_feeds_this_block_is_watched(real_publish_times):
    """Three wires, three kinds of source, one per key."""
    clock = {"now": real_publish_times[-1]}
    monitor = a_monitor(now_ns=lambda: clock["now"])
    monitor.observe_delivery(SOURCE_ID, real_publish_times[-1])
    monitor.observe_delivery(CORPORATE_ACTION_SOURCE_ID, real_publish_times[-1])
    monitor.observe_delivery("nse-fo-secban", real_publish_times[-1])

    standings = monitor.every_standing()
    assert [s.source_id for s in standings] == sorted(
        [SOURCE_ID, CORPORATE_ACTION_SOURCE_ID, "nse-fo-secban"]
    )
    assert all(s.delivery is Delivery.NOT_MEASURED for s in standings)
    assert monitor.standing.sources_not_measured == 3
    assert monitor.standing.sources_delivering == 0


def test_an_arrival_stamped_backwards_is_counted_and_does_not_move_the_clock(
    real_publish_times,
):
    """One reordered message must not make a dead source look fresh."""
    clock = {"now": real_publish_times[-1]}
    monitor = a_monitor(now_ns=lambda: clock["now"])
    for arrival in real_publish_times:
        monitor.observe_delivery(SOURCE_ID, arrival)
    monitor.observe_delivery(SOURCE_ID, real_publish_times[0])

    assert monitor.standing.arrivals_out_of_order == 1
    assert monitor.standing_of(SOURCE_ID).last_item_observed_at_ns == real_publish_times[-1]


def test_a_source_that_has_never_spoken_is_not_known_at_all():
    """A source with no arrivals is absent, not silently healthy."""
    monitor = a_monitor(now_ns=lambda: 1)
    monitor.observe_delivery("", 1)
    assert monitor.sources_known == 0
    assert monitor.every_standing() == ()
    assert monitor.standing.deliveries_seen == 0


def test_the_standing_reports_what_it_measured(real_publish_times):
    clock = {"now": real_publish_times[-1]}
    monitor = a_monitor(now_ns=lambda: clock["now"])
    for arrival in real_publish_times:
        monitor.observe_delivery(SOURCE_ID, arrival)
    monitor.every_standing()
    standing = describe_monitoring(monitor)
    assert standing["part_id"] == "news-source-health-monitor"
    assert standing["deliveries_seen"] == 31
    assert standing["reports_read"] == 0, "rows are counted by the tick, not by the monitor"
    assert standing["sources_known"] == 1
    assert standing["sources_delivering"] == 1
    assert standing["arrivals_out_of_order"] == 0


@pytest.mark.parametrize("window, minimum", [(0, 10), (500, 0), (-5, 10)])
def test_a_setting_that_cannot_work_is_refused_at_construction(window, minimum):
    with pytest.raises(ValueError):
        NewsSourceHealthMonitor(window=window, minimum_gaps_to_state_a_habit=minimum)


def test_one_poll_of_a_two_hundred_name_ban_list_is_one_delivery():
    """The live spine's own defect, 2026-09-12, as a test.

    This part counted rows for its first three minutes on the live spine and two
    sources read NOT_DELIVERING on a perfectly healthy system: 244 restriction
    reports arrived in one poll, which as rows looks like a source delivering
    twice a second, and thirty seconds of ordinary quiet then beat every gap it
    had ever shown.

    `start_part` records at most one delivery per source per tick, so the burst
    is one delivery and the record stays empty until the source polls again.
    """
    start_ns = 1_789_223_000_000_000_000
    clock = {"now": start_ns}
    monitor = a_monitor(now_ns=lambda: clock["now"])
    monitor.observe_delivery("nse-fo-secban", start_ns)

    clock["now"] = start_ns + 30 * NANOSECONDS_PER_SECOND
    standing = monitor.standing_of("nse-fo-secban")
    assert standing.items_seen == 1
    assert standing.seconds_since_last_item == 30.0
    # One delivery is no gaps at all, so there is nothing to judge the silence
    # against and the part says exactly that rather than calling it dead.
    assert standing.delivery is Delivery.NOT_MEASURED
    assert standing.typical_seconds_between_items is None


def test_the_tick_records_one_delivery_per_source_however_many_rows_arrive():
    """The rule that fixes it, exercised through the part's own function."""
    start_ns = 1_789_223_000_000_000_000
    clock = {"now": start_ns}
    monitor = a_monitor(now_ns=lambda: clock["now"])

    # Two polls of a 200-name ban list, a minute apart, as the real producer
    # restates it -- every row carrying that poll's own observation time.
    for poll in range(2):
        observed_at_ns = start_ns + poll * 60 * NANOSECONDS_PER_SECOND
        rows = [("nse-fo-secban", observed_at_ns) for _ in range(200)]
        monitor.standing.reports_read += len(rows)
        for source_id, when in latest_delivery_per_source(rows).items():
            monitor.observe_delivery(source_id, when)

    assert monitor.standing.reports_read == 400
    assert monitor.standing.deliveries_seen == 2
    assert monitor.standing_of("nse-fo-secban").items_seen == 2


def test_rows_from_several_sources_in_one_tick_are_one_delivery_each():
    rows = [
        ("upstox-news-api", 10),
        ("nse-fo-secban", 20),
        ("upstox-news-api", 30),
        ("", 40),
        ("nse-corporate-actions", 50),
    ]
    assert latest_delivery_per_source(rows) == {
        "upstox-news-api": 30,
        "nse-fo-secban": 20,
        "nse-corporate-actions": 50,
    }
