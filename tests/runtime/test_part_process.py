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
from runtime.part_process import PartHealth, run_part

HEALTH_INTERVAL = 0.05


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
