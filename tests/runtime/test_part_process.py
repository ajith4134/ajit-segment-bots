"""A part is a process, and off means the process does not exist.

T-3: an off part releases its CPU and RAM. A thread that turns off still holds its
share of a shared heap, so only a process exiting gives memory back. These tests
measure that rather than assuming it -- off-state-verifier does the same thing in
phase 2.
"""

import os
import socket
import struct
import time

import pytest

from runtime.control_channel import COMMAND_TURN_OFF, create_control_socket_pair, send_command
from runtime.forkserver_launcher import spawn_part, start_forkserver
from runtime.part_declaration import PartDeclaration, RateRisk, ResourceClass, SkippedTickEffect
from runtime.part_process import FULL_RATE_RATIO, PartHealth, compute_tick_interval, run_part

_LENGTH_PREFIX = struct.Struct("!I")

HEALTH_INTERVAL = 0.05


def test_compute_tick_interval_at_full_rate_is_the_base_interval():
    assert compute_tick_interval(HEALTH_INTERVAL, FULL_RATE_RATIO) == HEALTH_INTERVAL


def test_compute_tick_interval_at_quarter_rate_is_four_times_the_base():
    assert compute_tick_interval(HEALTH_INTERVAL, 0.25) == pytest.approx(HEALTH_INTERVAL * 4)


def test_compute_tick_interval_refuses_a_zero_rate_ratio():
    with pytest.raises(ValueError):
        compute_tick_interval(HEALTH_INTERVAL, 0.0)


def test_compute_tick_interval_refuses_a_negative_rate_ratio():
    with pytest.raises(ValueError):
        compute_tick_interval(HEALTH_INTERVAL, -0.5)


def test_compute_tick_interval_refuses_a_rate_ratio_above_full_rate():
    with pytest.raises(ValueError):
        compute_tick_interval(HEALTH_INTERVAL, FULL_RATE_RATIO + 0.5)


def _declaration() -> PartDeclaration:
    return PartDeclaration(
        part_id="substrate-test-part",
        consumes=(),
        produces=("part-health",),
        resource_class=ResourceClass.IO_BOUND,
        rate_risk=RateRisk.LATENCY_ONLY,
        skipped_tick_effect=SkippedTickEffect.DELAYS,
    )


def run_counting_part(control_socket, health_queue, tick_queue) -> None:
    """Module-level on purpose: forkserver pickles the target by qualified name."""
    ticks = {"count": 0}

    def do_one_tick() -> None:
        ticks["count"] += 1
        tick_queue.put(ticks["count"])

    run_part(
        declaration=_declaration(),
        control_socket=control_socket,
        do_one_tick=do_one_tick,
        emit_health=health_queue.put,
        health_interval_seconds=HEALTH_INTERVAL,
    )


def run_throttled_counting_part(control_socket, tick_timestamp_queue, rate_ratio, health_queue) -> None:
    """Module-level on purpose: forkserver pickles the target by qualified name.

    Records when each tick actually happened rather than how many, so the test
    can measure the real gap between ticks instead of trusting the arithmetic
    that produced the interval in isolation. Also emits health onto health_queue
    -- degradation must be visible, so a throttled part's own reported rate_ratio
    has to be inspected too, not only the timing of its ticks.
    """

    def do_one_tick() -> None:
        tick_timestamp_queue.put(time.monotonic())

    run_part(
        declaration=_declaration(),
        control_socket=control_socket,
        do_one_tick=do_one_tick,
        emit_health=health_queue.put,
        health_interval_seconds=HEALTH_INTERVAL,
        rate_ratio=rate_ratio,
    )


def test_a_part_ticks_while_it_is_on_and_stops_when_told_off():
    context = start_forkserver(preload_modules=("numpy",))
    governor_end, part_end = create_control_socket_pair()
    health_queue, tick_queue = context.Queue(), context.Queue()

    process = spawn_part(
        context, entry_point=run_counting_part,
        arguments=(part_end, health_queue, tick_queue), thread_ceiling=1,
    )
    assert tick_queue.get(timeout=10) >= 1

    send_command(governor_end, COMMAND_TURN_OFF, {"reason": "test"})
    process.join(timeout=10)

    assert process.exitcode == 0
    assert process.is_alive() is False

    # The parent's own copies of the socketpair outlive the child (which reclaimed
    # its duplicate by exiting, per T-3) and must be closed explicitly here.
    governor_end.close()
    part_end.close()


def test_the_process_is_gone_after_off_so_its_memory_is_back():
    context = start_forkserver(preload_modules=("numpy",))
    governor_end, part_end = create_control_socket_pair()
    health_queue, tick_queue = context.Queue(), context.Queue()

    process = spawn_part(
        context, entry_point=run_counting_part,
        arguments=(part_end, health_queue, tick_queue), thread_ceiling=1,
    )
    tick_queue.get(timeout=10)
    pid = process.pid

    send_command(governor_end, COMMAND_TURN_OFF, {"reason": "test"})
    process.join(timeout=10)

    # T-3, measured rather than asserted: the kernel no longer has this process.
    assert not os.path.exists(f"/proc/{pid}/stat")

    # The parent's own copies of the socketpair outlive the child (which reclaimed
    # its duplicate by exiting, per T-3) and must be closed explicitly here.
    governor_end.close()
    part_end.close()


def test_a_garbage_control_frame_does_not_kill_the_part_and_shows_up_in_its_health():
    # The cross-task gap: receive_command already refuses a malformed frame
    # (ControlFrameRefused) instead of raising a JSON decode error past its own
    # boundary, but nothing in run_part ever caught it -- so the refusal escaped
    # the loop and terminated the part anyway. Reproduced directly against a real
    # socketpair before this fix: the part thread died and the refusal was the
    # uncaught exception, not a value on health.
    context = start_forkserver(preload_modules=("numpy",))
    governor_end, part_end = create_control_socket_pair()
    health_queue, tick_queue = context.Queue(), context.Queue()

    process = spawn_part(
        context, entry_point=run_counting_part,
        arguments=(part_end, health_queue, tick_queue), thread_ceiling=1,
    )
    assert tick_queue.get(timeout=10) >= 1

    # A frame whose body is not JSON: 4-byte length prefix, then garbage bytes.
    garbage_body = b"this is not json!"
    governor_end.sendall(_LENGTH_PREFIX.pack(len(garbage_body)) + garbage_body)

    # The part must still be alive and still ticking after the refusal -- a dead
    # process would never put another tick on the queue.
    assert tick_queue.get(timeout=10) >= 1
    assert process.is_alive() is True

    # The refusal must be visible on health, not silently swallowed. This
    # unthrottled part emits health on every tick, most of it with
    # refused_control_frame=None (there is no way to know in advance whether
    # the report carrying the refusal lands before or after an innocent one
    # already in flight when the garbage frame was sent), so this scans the
    # health this part actually produced for the one report that does carry a
    # refusal, rather than assuming it is the very next item -- and fails
    # loudly, not by hanging, if none ever does.
    deadline = time.monotonic() + 10
    refusing_health = None
    while time.monotonic() < deadline:
        health = health_queue.get(timeout=max(0.1, deadline - time.monotonic()))
        assert isinstance(health, PartHealth)
        if health.refused_control_frame is not None:
            refusing_health = health
            break

    assert refusing_health is not None, "no health report ever carried the refusal"
    assert f"{len(garbage_body)} bytes" in refusing_health.refused_control_frame
    assert "JSON" in refusing_health.refused_control_frame

    # And the part still turns off cleanly on a real command afterward.
    send_command(governor_end, COMMAND_TURN_OFF, {"reason": "test"})
    process.join(timeout=10)
    assert process.exitcode == 0
    assert process.is_alive() is False

    governor_end.close()
    part_end.close()


QUARTER_RATE = 0.25
TICKS_TO_OBSERVE = 3
# How much of a real gap the loop's own overhead (scheduling, the select() call
# itself, IPC) may eat into the measured interval without that being the throttle
# failing. One-sided: this only ever forgives a gap being a little short, never
# a gap being long, so it cannot hide the bug this test exists to catch.
CLOCK_GRANULARITY_ALLOWANCE_SECONDS = 0.01


@pytest.mark.slow
def test_a_throttled_part_ticks_no_faster_than_its_computed_interval():
    # Measured, not assumed: prove the throttle reaches the loop rather than
    # trusting the arithmetic that produces the interval in isolation. One-sided
    # on purpose -- has_pending_command blocks for at least tick_interval when no
    # command is pending, so a healthy loop cannot tick faster than that, but a
    # loaded box can always make it tick slower without that being a bug. Do not
    # assert an upper bound here.
    context = start_forkserver(preload_modules=("numpy",))
    governor_end, part_end = create_control_socket_pair()
    timestamp_queue, health_queue = context.Queue(), context.Queue()

    process = spawn_part(
        context, entry_point=run_throttled_counting_part,
        arguments=(part_end, timestamp_queue, QUARTER_RATE, health_queue), thread_ceiling=1,
    )
    timestamps = [timestamp_queue.get(timeout=10) for _ in range(TICKS_TO_OBSERVE)]

    # Section 6, Rule 8: degradation must be visible on the board, not only
    # reachable by timing the ticks by hand. A quarter-rate part must say so on
    # its own health report -- the exact value, not merely a number in (0, 1].
    health = health_queue.get(timeout=10)
    assert isinstance(health, PartHealth)
    assert health.rate_ratio == QUARTER_RATE

    send_command(governor_end, COMMAND_TURN_OFF, {"reason": "test"})
    process.join(timeout=10)

    # A quarter of full rate is a tick interval four times the base -- inlined
    # here rather than imported so this test also pins the relationship
    # compute_tick_interval must satisfy, independent of that function's own
    # implementation.
    expected_interval = HEALTH_INTERVAL / QUARTER_RATE
    observed_gaps = [second - first for first, second in zip(timestamps, timestamps[1:])]
    for gap in observed_gaps:
        assert gap >= expected_interval - CLOCK_GRANULARITY_ALLOWANCE_SECONDS

    governor_end.close()
    part_end.close()


def test_a_part_publishes_its_rate_ratio_and_staleness_on_every_health_report():
    # Section 6: degradation must be visible. A part running at quarter rate is
    # shown as such, so the rate ratio rides on the output rather than being inferred.
    context = start_forkserver(preload_modules=("numpy",))
    governor_end, part_end = create_control_socket_pair()
    health_queue, tick_queue = context.Queue(), context.Queue()

    process = spawn_part(
        context, entry_point=run_counting_part,
        arguments=(part_end, health_queue, tick_queue), thread_ceiling=1,
    )
    health = health_queue.get(timeout=10)
    send_command(governor_end, COMMAND_TURN_OFF, {"reason": "test"})
    process.join(timeout=10)

    assert isinstance(health, PartHealth)
    assert health.part_id == "substrate-test-part"
    assert health.state == "on"
    assert 0.0 < health.rate_ratio <= 1.0
    assert health.staleness_seconds >= 0.0
    assert health.observed_at_ns > 0

    # The parent's own copies of the socketpair outlive the child (which reclaimed
    # its duplicate by exiting, per T-3) and must be closed explicitly here.
    governor_end.close()
    part_end.close()


# --- The readiness waiter: a part that answers its inputs, not only its clock ---

# Long enough that a tick inside these tests can only have come from data arriving.
SLOW_TIMER_INTERVAL_SECONDS = 5.0
TICK_FLOOR_SECONDS = 0.3
PATIENCE_SECONDS = 2.0


def _run_part_in_a_thread(control_socket, tick_times, input_descriptors, tick_floor_seconds):
    """Run one part on a thread so the test can drive its inbox from the other side.

    A thread rather than a process because what is under test is the wait itself,
    and the test has to hold both ends: the socket it publishes into and the clock
    it measures against.
    """
    import threading

    def body() -> None:
        run_part(
            declaration=_declaration(),
            control_socket=control_socket,
            do_one_tick=lambda: tick_times.append(time.monotonic()),
            emit_health=lambda health: None,
            health_interval_seconds=SLOW_TIMER_INTERVAL_SECONDS,
            input_descriptors=input_descriptors,
            tick_floor_seconds=tick_floor_seconds,
        )

    thread = threading.Thread(target=body, name="part-under-test", daemon=True)
    thread.start()
    return thread


@pytest.fixture
def one_inbox():
    """A real bound inbox, on the filesystem the bus actually uses."""
    import pathlib
    import shutil

    from runtime.bus import Inbox

    root = pathlib.Path(os.environ["XDG_RUNTIME_DIR"]) / "part-process-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    inbox = Inbox(
        part_id="substrate-test-part",
        data_type="market-data",
        address=root / "substrate-test-part.market-data",
        receive_buffer_bytes=212_992,
    )
    yield inbox
    inbox.close()
    shutil.rmtree(root, ignore_errors=True)


def _publish(inbox, payload) -> bool:
    """Send one frame, returning whether it was accepted.

    A refusal is not a test failure: the part under test never drains its inbox, so
    the flood test fills it on purpose, and EAGAIN there is the bus behaving as it
    is meant to rather than the test going wrong.
    """
    from runtime.bus import encode_frame

    sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sender.setblocking(False)
    try:
        sender.sendto(
            encode_frame(
                data_type="market-data",
                producer_part_id="venue-trade-stream-reader",
                sequence=payload,
                published_at_ns=time.time_ns(),
                payload=payload,
                maximum_message_bytes=131_072,
            ),
            inbox.address,
        )
    except BlockingIOError:
        return False
    finally:
        sender.close()
    return True


def test_a_part_wakes_when_its_data_arrives_not_when_its_timer_expires(one_inbox):
    governor_end, part_end = create_control_socket_pair()
    tick_times = []
    thread = _run_part_in_a_thread(part_end, tick_times, (one_inbox.fileno(),), 0.0)
    try:
        started = time.monotonic()
        _publish(one_inbox, 0)
        deadline = started + PATIENCE_SECONDS
        while not tick_times and time.monotonic() < deadline:
            time.sleep(0.01)
        assert tick_times, f"no tick within {PATIENCE_SECONDS}s of data arriving"
        assert tick_times[0] - started < SLOW_TIMER_INTERVAL_SECONDS, (
            "the part ticked on its timer, not on its data"
        )
    finally:
        send_command(governor_end, COMMAND_TURN_OFF, {"reason": "test"})
        thread.join(timeout=10)
        governor_end.close()
        part_end.close()


def test_a_flood_of_data_cannot_tick_a_part_faster_than_its_floor(one_inbox):
    governor_end, part_end = create_control_socket_pair()
    tick_times = []
    thread = _run_part_in_a_thread(part_end, tick_times, (one_inbox.fileno(),), TICK_FLOOR_SECONDS)
    try:
        started = time.monotonic()
        observing_for = TICK_FLOOR_SECONDS * 3
        sent = 0
        while time.monotonic() - started < observing_for:
            _publish(one_inbox, sent)
            sent += 1
            time.sleep(0.001)
        ticks_in_the_window = [at for at in list(tick_times) if at - started < observing_for]
    finally:
        send_command(governor_end, COMMAND_TURN_OFF, {"reason": "test"})
        thread.join(timeout=10)
        governor_end.close()
        part_end.close()

    assert sent > len(ticks_in_the_window), "the test did not out-send the part"
    highest_possible = int(observing_for / TICK_FLOOR_SECONDS) + 1
    assert len(ticks_in_the_window) <= highest_possible, (
        f"{len(ticks_in_the_window)} ticks in {observing_for}s with a "
        f"{TICK_FLOOR_SECONDS}s floor -- at most {highest_possible} are permitted"
    )


def test_a_part_holding_its_floor_is_still_switchable(one_inbox):
    """T-2: the floor is waited out on the control socket, never by going deaf."""
    governor_end, part_end = create_control_socket_pair()
    tick_times = []
    long_floor = PATIENCE_SECONDS * 3
    thread = _run_part_in_a_thread(part_end, tick_times, (one_inbox.fileno(),), long_floor)
    try:
        _publish(one_inbox, 0)
        time.sleep(0.05)  # let it wake on the data and start holding the floor
        switched_off_at = time.monotonic()
        send_command(governor_end, COMMAND_TURN_OFF, {"reason": "test"})
        thread.join(timeout=long_floor)
        took = time.monotonic() - switched_off_at
    finally:
        governor_end.close()
        part_end.close()

    assert not thread.is_alive(), "the part did not stop while holding its tick floor"
    assert took < long_floor, f"switching off waited out the floor: {took:.2f}s"


# ---- what a part says about its own work -------------------------------------

def test_a_standing_is_flattened_to_countable_facts():
    """Health carries numbers, not a part's whole inner state.

    The bus refuses a datagram over 128 KiB, and one part's standing already holds
    a per-symbol map that would grow with the universe. So what rides on health is
    the countable part of it: how many times this part refused, lost, cleared or
    fired. A string or a nested structure is left behind rather than truncated,
    because a number that arrived half-serialised is worse than one that did not
    arrive.

    A **bounded** map of counters does ride, flattened one level, since
    2026-08-26: every part here that refuses things counts them by reason, and
    dropping those left a gate refusing 100% of what it saw showing a total on the
    board with no way to see which condition did it.
    """
    from runtime.part_process import countable_standing

    flattened = dict(countable_standing({
        "part_id": "instrument-selector",
        "refused_for_a_stale_price": 12,
        "chosen": 481,
        "widest_z": 3.25,
        "is_learning": True,
        "refused_by_reason": {"nothing-listed": 3},
        "price_staleness": {"believable_age_seconds": {"binance-usdm|BTCUSDT": 8.6}},
    }))

    assert flattened == {
        "refused_for_a_stale_price": 12.0,
        "chosen": 481.0,
        "widest_z": 3.25,
        "is_learning": 1.0,
        # Bounded and countable, so it rides.
        "refused_by_reason.nothing-listed": 3.0,
        # A map whose values are themselves maps is not a counter map, and
        # nothing in it is a number to flatten.
    }


def test_a_standing_is_capped_so_one_part_cannot_fill_a_datagram():
    from runtime.part_process import countable_standing

    huge = {f"counter_{index}": index for index in range(500)}
    flattened = countable_standing(huge)

    assert len(flattened) == 32
    assert flattened == tuple(sorted(flattened)), "capped by name, so the same keys survive twice"


def test_a_part_with_nothing_to_say_about_itself_says_nothing():
    from runtime.part_process import countable_standing

    assert countable_standing(None) == ()
    assert countable_standing({}) == ()
    assert countable_standing({"part_id": "x", "notes": "words"}) == ()


# ---- a refusal nobody can trace to a reason is a number nobody can act on -----

def test_a_bounded_counter_map_reaches_the_board_one_level_flattened():
    """Measured 2026-08-26: instruction-writer reported requests 494, refused 494,
    written 0 -- and its by_failing_condition reached nothing, so which of its
    eight conditions refused everything was invisible on every board. Rule 8.
    """
    from runtime.part_process import countable_standing

    reported = dict(countable_standing({
        "requests": 494,
        "refused": 494,
        "by_failing_condition": {"nothing-tried-to-break-it": 300, "no-regime-tag": 194},
    }))

    assert reported["by_failing_condition.nothing-tried-to-break-it"] == 300.0
    assert reported["by_failing_condition.no-regime-tag"] == 194.0
    assert reported["refused"] == 494.0


def test_a_map_that_grows_with_the_universe_is_counted_rather_than_flattened():
    """Flattening one would evict every other counter the part has under the cap.

    Reported as a count rather than dropped silently, so an omission still shows
    up as an omission.
    """
    from runtime.part_process import MOST_KEYS_IN_A_STANDING_MAP, countable_standing

    per_symbol = {f"SYM{index}": index for index in range(MOST_KEYS_IN_A_STANDING_MAP + 1)}
    reported = dict(countable_standing({"observed": 7, "by_symbol": per_symbol}))

    assert reported["by_symbol.keys_not_reported"] == float(len(per_symbol))
    assert reported["observed"] == 7.0
    assert not any(name.startswith("by_symbol.SYM") for name in reported)


def test_a_map_of_names_is_still_left_behind():
    """Numbers only -- a reason string on this channel is a reason on every tick."""
    from runtime.part_process import countable_standing

    reported = dict(countable_standing({
        "last_refusal": "a string",
        "by_venue": {"binance-usdm": "healthy"},
        "counted": 3,
    }))

    assert reported == {"counted": 3.0}


# ---- a clock is not a counter -----------------------------------------------

def test_a_wall_clock_timestamp_never_reaches_the_standing_channel():
    """Measured 2026-08-27 on the live board: the busiest counter in the whole
    spine read `hardware-scanner capacity.measured_at_ns 900,251,934/s`.

    Nothing was working that hard. A rate is delta over elapsed seconds, and a
    nanosecond clock advances a billion per second by definition, so a timestamp
    on this channel outranks every real counter by six orders of magnitude and
    wins "busiest" forever. Worse, `judge_is_working` calls a part WORKING when
    any counter moved -- so a clock alone would paint a part green while it did
    nothing, which is Rule 8's failure exactly.

    Left behind on the write side rather than special-cased on the read side:
    every board that rates these counters would otherwise need the same rule, and
    the payload on the bus still carries the timestamp for any reader judging
    staleness.
    """
    from runtime.part_process import countable_standing

    reported = dict(countable_standing({
        "readings": 2068,
        "is_complete": True,
        "capacity": {"measured_at_ns": 1_787_813_045_707_939_000, "logical_cpus": 12},
        "checkpoint_saved_at_ns": 1_787_810_413_530_649_900,
        "checkpoints_written": 9,
        "last_published_at_ns": 1_787_813_002_495_597_800,
    }))

    assert reported == {
        "readings": 2068.0,
        "is_complete": 1.0,
        "capacity.logical_cpus": 12.0,
        "checkpoints_written": 9.0,
    }


def test_a_nanosecond_duration_is_a_counter_and_still_rides():
    """The rule is about `_at_ns` -- a point in time -- not about nanoseconds.

    Cumulative time spent is a genuine counter: its rate is the fraction of a
    core the part is using, which is exactly what the board should show. A rule
    over every name ending `_ns` would have thrown that away.
    """
    from runtime.part_process import countable_standing

    reported = dict(countable_standing({
        "cpu_time_ns": 4_200_000_000,
        "slowest_tick_ns": 1_300_000,
        "next_settlement_at_ns": 1_787_813_045_707_939_000,
    }))

    assert reported == {"cpu_time_ns": 4.2e9, "slowest_tick_ns": 1.3e6}


def test_a_counter_map_keyed_by_something_other_than_a_name_still_flattens():
    """A counter map may be keyed by whatever the part counts by.

    `duty-cycle-planner` keys one by an integer rung, and the first version of
    the clock rule asked every key how it ended -- so every part holding such a
    map crash-looped on its first health report. A key that is not a name cannot
    be a clock.
    """
    from runtime.part_process import countable_standing

    reported = dict(countable_standing({"by_rung": {1: 4, 2: 9}, "planned": 13}))

    assert reported == {"by_rung.1": 4.0, "by_rung.2": 9.0, "planned": 13.0}
