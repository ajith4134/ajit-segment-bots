"""Placement returns rc=0 before it is attempted, so rc=0 is not evidence.

Section 3: StartTransientUnit queues an asynchronous job and returns its object
path. Observed once in 88 attempts, a placement returned rc=0 while the process
stayed in its old cgroup with memory.max unset. These tests hold the confirmation.
"""

import os
import signal
import subprocess
import sys
import time

import pytest

from runtime.scope_placer import (
    PlacementNotConfirmed,
    ScopeLimits,
    has_process_landed_in_scope,
    place_process_in_scope,
    read_process_cgroup,
    read_scope_limits_in_effect,
)

DEADLINE = 0.5
POLL = 0.002
MEGABYTE = 1024 * 1024


@pytest.fixture
def sleeping_process():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    yield process
    if process.poll() is None:
        process.send_signal(signal.SIGKILL)
    process.wait()


def test_reads_the_cgroup_a_process_is_actually_in(sleeping_process):
    assert read_process_cgroup(sleeping_process.pid).startswith("/")


@pytest.mark.cgroup
def test_places_a_process_and_the_limits_are_really_in_effect(sleeping_process):
    scope = f"placer-test-{os.getpid()}-{sleeping_process.pid}"
    limits = ScopeLimits(memory_max_bytes=256 * MEGABYTE, cpu_weight=137, pids_max=64)

    directory = place_process_in_scope(
        sleeping_process.pid, scope, limits,
        confirmation_deadline_seconds=DEADLINE, poll_interval_seconds=POLL,
    )

    assert has_process_landed_in_scope(sleeping_process.pid, scope) is True
    in_effect = read_scope_limits_in_effect(directory)
    assert in_effect["memory.max"] == str(256 * MEGABYTE)
    assert in_effect["cpu.weight"] == "137"
    # pids is one of the three controllers actually delegated on this box
    # (cpu, memory, pids), so this is a real, checkable limit, not a fiction.
    assert in_effect["pids.max"] == "64"
    # The per-part scarcity signal section 5 depends on. The system-wide 'full' line
    # is zero by definition, so only the per-cgroup file is usable.
    assert "full" in in_effect["cpu.pressure"]


@pytest.mark.cgroup
def test_refuses_to_report_success_when_the_pid_never_existed():
    # A PID that has already exited is a *different* failure from the one this
    # module exists to catch: busctl rejects it synchronously (a nonzero rc,
    # verified by hand: rc=1, "Call failed: ... No such process"), and no unit
    # is ever created. This is the reproducible-but-different case -- confirmation
    # here is refusing on a real, current absence of the process, which is why the
    # assertion below names "no longer exists" rather than the generic "/proc"
    # substring every PlacementNotConfirmed message shares.
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    with pytest.raises(PlacementNotConfirmed) as refusal:
        place_process_in_scope(
            dead.pid, f"placer-dead-{os.getpid()}",
            ScopeLimits(memory_max_bytes=64 * MEGABYTE, cpu_weight=100),
            confirmation_deadline_seconds=DEADLINE, poll_interval_seconds=POLL,
        )
    assert "no longer exists" in str(refusal.value)


@pytest.mark.cgroup
@pytest.mark.slow
def test_placement_is_reliable_and_fast_enough_to_be_on_the_switch_on_path():
    # Section 3 quotes 5.6 ms to place. If this regresses, switch-on regressed.
    latencies = []
    for attempt in range(10):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
        scope = f"placer-rate-{os.getpid()}-{attempt}"
        started = time.perf_counter()
        place_process_in_scope(
            process.pid, scope,
            ScopeLimits(memory_max_bytes=64 * MEGABYTE, cpu_weight=100),
            confirmation_deadline_seconds=DEADLINE, poll_interval_seconds=POLL,
        )
        latencies.append(time.perf_counter() - started)
        process.send_signal(signal.SIGKILL)
        process.wait()

    latencies.sort()
    assert latencies[len(latencies) // 2] < 0.1, f"median placement {latencies[len(latencies)//2]:.4f}s"
