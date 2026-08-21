"""A part is a process, and off means the process does not exist.

T-3: an off part releases its CPU and RAM. A thread that turns off still holds its
share of a shared heap, so only a process exiting gives memory back. These tests
measure that rather than assuming it -- off-state-verifier does the same thing in
phase 2.
"""

import os
import time

import pytest

from runtime.control_channel import COMMAND_TURN_OFF, create_control_socket_pair, send_command
from runtime.forkserver_launcher import spawn_part, start_forkserver
from runtime.part_declaration import PartDeclaration, RateRisk, ResourceClass, SkippedTickEffect
from runtime.part_process import FULL_RATE_RATIO, PartHealth, compute_tick_interval, run_part

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


def run_throttled_counting_part(control_socket, tick_timestamp_queue, rate_ratio) -> None:
    """Module-level on purpose: forkserver pickles the target by qualified name.

    Records when each tick actually happened rather than how many, so the test
    can measure the real gap between ticks instead of trusting the arithmetic
    that produced the interval in isolation.
    """

    def do_one_tick() -> None:
        tick_timestamp_queue.put(time.monotonic())

    run_part(
        declaration=_declaration(),
        control_socket=control_socket,
        do_one_tick=do_one_tick,
        emit_health=lambda health: None,
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
    timestamp_queue = context.Queue()

    process = spawn_part(
        context, entry_point=run_throttled_counting_part,
        arguments=(part_end, timestamp_queue, QUARTER_RATE), thread_ceiling=1,
    )
    timestamps = [timestamp_queue.get(timeout=10) for _ in range(TICKS_TO_OBSERVE)]

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
