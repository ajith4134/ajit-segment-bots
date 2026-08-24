"""Starting a real part as a real process, and stopping it.

These run against `hardware-scanner` -- an actual part, with the entry point every
part carries -- rather than a stand-in, because what is under test is whether a
part the blueprint declares can be started by a launcher that knows nothing about
it. A fake part would have proved only that the fake was startable.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import time

import pytest

from runtime.bus import Inbox, decode_frame
from runtime.part_launcher import (
    PartAlreadyRunning,
    PartHasNoModule,
    PartLauncher,
    resolve_part_module,
)
from runtime.scope_placer import ScopeLimits
from runtime.wiring_plan import derive_wiring

THREAD_CEILING = 1
PLACEMENT_DEADLINE_SECONDS = 0.5
PLACEMENT_POLL_SECONDS = 0.002
STOP_DEADLINE_SECONDS = 10.0
PATIENCE_SECONDS = 15.0

# Generous, because what is being bounded here is a test process, not a part whose
# appetite the governor has measured. Real limits come from part-appetite-meter.
TEST_SCOPE_LIMITS = ScopeLimits(memory_max_bytes=512 * 1024 * 1024, cpu_weight=100, pids_max=64)


@pytest.fixture
def bus_root():
    """A short scratch root: every byte of it competes with the kernel's 107."""
    root = pathlib.Path(os.environ["XDG_RUNTIME_DIR"]) / "launcher-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    root.chmod(0o700)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def launcher(bus_root):
    started = PartLauncher(
        place_in_scope=False,
        thread_ceiling=THREAD_CEILING,
        placement_confirmation_deadline_seconds=PLACEMENT_DEADLINE_SECONDS,
        placement_confirmation_poll_interval_seconds=PLACEMENT_POLL_SECONDS,
        runtime_directory=bus_root,
    )
    yield started
    started.stop_all(STOP_DEADLINE_SECONDS)
    started.close()


def a_consumer_inbox_for(bus_root, data_type: str, consumer_part_id: str) -> Inbox:
    """Bind the inbox of a real consumer, so the part's output has somewhere to land."""
    wiring = derive_wiring(runtime_directory=bus_root)[consumer_part_id]
    return Inbox(
        part_id=consumer_part_id,
        data_type=data_type,
        address=wiring.inboxes[data_type],
        receive_buffer_bytes=212_992,
    )


def wait_for(condition, patience_seconds: float = PATIENCE_SECONDS) -> bool:
    deadline = time.monotonic() + patience_seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_a_part_resolves_to_the_file_named_for_it():
    assert resolve_part_module("hardware-scanner") == "parts.resource_governor.hardware_scanner"
    assert resolve_part_module("paper-fill-simulator") == "parts.paper_live_trading.paper_fill_simulator"


def test_a_part_with_no_file_is_refused_by_name():
    with pytest.raises(PartHasNoModule) as refusal:
        resolve_part_module("a-part-nobody-wrote")
    assert "a-part-nobody-wrote" in str(refusal.value)


def test_a_started_part_runs_and_publishes_what_the_blueprint_says_it_produces(launcher, bus_root):
    """The whole substrate in one test: fork, bus, settings, entry point, output."""
    consumers_of_capacity = [
        part_id
        for part_id, wiring in derive_wiring(runtime_directory=bus_root).items()
        if "hardware-capacity" in wiring.inboxes
    ]
    assert consumers_of_capacity, "the blueprint must declare a consumer of hardware-capacity"
    inbox = a_consumer_inbox_for(bus_root, "hardware-capacity", consumers_of_capacity[0])
    try:
        launched = launcher.start("hardware-scanner")
        assert launched.is_running

        received = []
        assert wait_for(lambda: received.extend(inbox.drain()) or bool(received)), (
            "hardware-scanner published nothing its consumer could read"
        )
    finally:
        inbox.close()

    first = received[0]
    assert first.producer_part_id == "hardware-scanner"
    assert first.data_type == "hardware-capacity"
    assert first.payload.facts.physical_cores >= 1


def test_a_started_part_emits_health_onto_the_same_bus(launcher, bus_root):
    inbox = a_consumer_inbox_for(bus_root, "part-health", "heartbeat-collector")
    try:
        launcher.start("hardware-scanner")
        received = []
        assert wait_for(lambda: received.extend(inbox.drain()) or bool(received)), (
            "no part-health arrived; health rides the same bus as everything else"
        )
    finally:
        inbox.close()

    health = received[0].payload
    assert health.part_id == "hardware-scanner"
    assert health.state == "on"
    assert health.input_loss == (), "a part with no inputs cannot have lost any"


def test_turning_a_part_off_ends_its_process(launcher):
    launched = launcher.start("hardware-scanner")
    pid = launched.process.pid
    assert launched.is_running

    exit_code = launcher.stop("hardware-scanner", STOP_DEADLINE_SECONDS)

    assert exit_code == 0
    assert not launched.is_running
    assert launcher.is_running("hardware-scanner") is False
    assert launcher.report()["stops_that_needed_a_kill"] == 0
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_part_is_one_process(launcher):
    launcher.start("hardware-scanner")
    with pytest.raises(PartAlreadyRunning):
        launcher.start("hardware-scanner")


def test_a_part_that_is_not_in_the_blueprint_cannot_be_started(launcher):
    with pytest.raises(PartHasNoModule):
        launcher.start("a-part-nobody-declared")
    assert launcher.report()["refused_to_start"] == 1


def test_a_stale_address_from_a_previous_run_is_unlinked_before_the_part_binds(bus_root):
    """A socket file outlives its process, and only the launcher may remove one."""
    wiring = derive_wiring(runtime_directory=bus_root)
    stale_address = next(iter(wiring["off-state-verifier"].inboxes.values()))
    stale_address.parent.mkdir(parents=True, exist_ok=True)
    stale_address.write_bytes(b"")  # not even a socket: a file left where one was
    assert stale_address.exists()

    launcher = PartLauncher(
        place_in_scope=False,
        thread_ceiling=THREAD_CEILING,
        placement_confirmation_deadline_seconds=PLACEMENT_DEADLINE_SECONDS,
        placement_confirmation_poll_interval_seconds=PLACEMENT_POLL_SECONDS,
        runtime_directory=bus_root,
    )
    try:
        launcher._unlink_stale_addresses("off-state-verifier")
        assert not stale_address.exists()
    finally:
        launcher.close()


def test_a_launcher_asked_to_place_without_limits_refuses(bus_root):
    with pytest.raises(ValueError) as refusal:
        PartLauncher(
            place_in_scope=True,
            thread_ceiling=THREAD_CEILING,
            placement_confirmation_deadline_seconds=PLACEMENT_DEADLINE_SECONDS,
            placement_confirmation_poll_interval_seconds=PLACEMENT_POLL_SECONDS,
            limits_for=None,
            runtime_directory=bus_root,
        )
    assert "bounds nothing" in str(refusal.value)


def test_an_unplaced_part_is_reported_unbounded_never_quietly_fine(launcher):
    launched = launcher.start("hardware-scanner")
    report = launcher.report()

    assert launched.is_bounded is False
    assert report["running_unbounded"] == 1
    assert report["running"][0]["placement_refusal"]


@pytest.mark.cgroup
@pytest.mark.slow
def test_a_placed_part_lands_in_its_own_scope_and_the_launcher_reads_it_from_proc(bus_root):
    """The placement is confirmed from /proc, because the D-Bus call returns rc=0
    before the move is attempted -- phase 0 measured one silent failure in ninety."""
    import pathlib

    live_scope = pathlib.Path(
        "/sys/fs/cgroup/user.slice/user-1001.slice/user@1001.service/app.slice/"
        "hardware-scanner.scope"
    )
    if live_scope.is_dir():
        # Since 2026-08-24 the live spine places every part in a scope named for
        # it, and systemd refuses a second unit by the same name. Two claimants
        # to one scope name is the same one-writer rule the tape has, so this
        # test yields to the spine it would collide with.
        pytest.skip("the live spine holds hardware-scanner.scope; scope names are exclusive")
    launcher = PartLauncher(
        place_in_scope=True,
        thread_ceiling=THREAD_CEILING,
        placement_confirmation_deadline_seconds=PLACEMENT_DEADLINE_SECONDS,
        placement_confirmation_poll_interval_seconds=PLACEMENT_POLL_SECONDS,
        limits_for=lambda part_id: TEST_SCOPE_LIMITS,
        runtime_directory=bus_root,
    )
    try:
        launched = launcher.start("hardware-scanner")
        assert launched.is_bounded, f"placement refused: {launched.placement_refusal}"
        assert launched.scope_directory is not None
        assert launched.scope_directory.name == "hardware-scanner.scope"

        landed = pathlib.Path(f"/proc/{launched.process.pid}/cgroup").read_text()
        assert "hardware-scanner.scope" in landed
        assert (launched.scope_directory / "memory.max").read_text().strip() == str(
            TEST_SCOPE_LIMITS.memory_max_bytes
        )
        assert launcher.report()["running_unbounded"] == 0
    finally:
        launcher.stop_all(STOP_DEADLINE_SECONDS)
        launcher.close()


@pytest.mark.slow
def test_the_launcher_reports_what_it_measured_not_what_it_intended(launcher):
    launcher.start("hardware-scanner")
    launcher.stop("hardware-scanner", STOP_DEADLINE_SECONDS)
    report = launcher.report()

    assert report["started"] == 1
    assert report["stopped"] == 1
    assert report["running"] == []
    assert report["fork_milliseconds_median"] is not None
    assert report["fork_milliseconds_median"] < 100, report["fork_milliseconds_median"]
