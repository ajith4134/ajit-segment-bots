"""The governor switching a part, through the endpoint that makes T-2 hold.

The last test is the one that matters: `gate-actuator` runs as its own process, is
handed a switch plan on the bus, and a different part is running afterwards --
without gate-actuator ever holding a descriptor onto that part's control socket. If
that works, the rule that only the governor switches parts is enforced by what each
process was given rather than by what its code refrains from doing.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import socket
import time

import pytest

from runtime.bus import Inbox, encode_frame
from runtime.part_launcher import PartLauncher
from runtime.switch_service import (
    ACTION_TURN_OFF,
    ACTION_TURN_ON,
    OUTCOME_FLIPPED,
    OUTCOME_REFUSED,
    NoSingleSwitchActuator,
    SwitchService,
    find_switch_actuator,
    open_switch_client,
    read_switch_outcome,
    send_switch_request,
)
from runtime.wiring_plan import derive_wiring, load_blueprint

THREAD_CEILING = 1
PLACEMENT_DEADLINE_SECONDS = 0.5
PLACEMENT_POLL_SECONDS = 0.002
STOP_DEADLINE_SECONDS = 10.0
REQUEST_TIMEOUT_SECONDS = 30.0
ENDPOINT_BACKLOG = 8
PATIENCE_SECONDS = 30.0
RECEIVE_BUFFER_BYTES = 212_992
MAXIMUM_MESSAGE_BYTES = 131_072


@pytest.fixture
def bus_root():
    root = pathlib.Path(os.environ["XDG_RUNTIME_DIR"]) / "switch-test"
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


def ask_and_serve(service: SwitchService, part_id: str, action: str):
    """Send a request, serve it, read the answer -- all on one thread.

    Deliberately not a thread: the launcher forks, and forking is refused above one
    kernel thread (a mutex held by another thread at the instant of fork is copied
    locked into the child). A test that asked from a second thread would make the
    launcher process unforkable and measure that instead of the endpoint.
    """
    client = open_switch_client(service.address, REQUEST_TIMEOUT_SECONDS)
    try:
        send_switch_request(client, part_id, action, "test")
        assert serve_until(service, lambda: True, patience_seconds=1.0)
        return read_switch_outcome(client, service.address)
    finally:
        client.close()


def wait_for_address(address: pathlib.Path, patience_seconds: float = PATIENCE_SECONDS) -> bool:
    """Wait until a part has bound its inbox. A part is not reachable before it has."""
    deadline = time.monotonic() + patience_seconds
    while time.monotonic() < deadline:
        if address.exists():
            return True
        time.sleep(0.02)
    return False


def serve_until(service: SwitchService, condition, patience_seconds: float = PATIENCE_SECONDS) -> bool:
    """Run the launcher's endpoint the way a supervisor would, until something happens."""
    deadline = time.monotonic() + patience_seconds
    while time.monotonic() < deadline:
        service.serve_pending()
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_the_blueprint_names_exactly_one_part_that_switches_parts():
    assert find_switch_actuator(load_blueprint()) == "gate-actuator"


def test_two_candidate_actuators_are_refused_rather_than_chosen_between():
    blueprint = load_blueprint()
    actuator = next(f for f in blueprint["features"] if f["id"] == "gate-actuator")
    impostor = {**actuator, "id": "a-second-actuator"}
    with pytest.raises(NoSingleSwitchActuator):
        find_switch_actuator({**blueprint, "features": [*blueprint["features"], impostor]})


def test_only_the_actuator_is_given_the_endpoint_address(launcher):
    launcher.open_switch_service(STOP_DEADLINE_SECONDS, ENDPOINT_BACKLOG)
    try:
        assert launcher._switch_endpoint_for("gate-actuator")
        assert launcher._switch_endpoint_for("hardware-scanner") is None
        assert launcher._switch_endpoint_for("paper-fill-simulator") is None
    finally:
        launcher.close()


def test_a_request_turns_a_part_on_and_the_launcher_says_what_happened(launcher):
    service = launcher.open_switch_service(STOP_DEADLINE_SECONDS, ENDPOINT_BACKLOG)
    outcome = ask_and_serve(service, "hardware-scanner", ACTION_TURN_ON)

    assert outcome.outcome == OUTCOME_FLIPPED
    assert launcher.is_running("hardware-scanner")
    assert service.requests_served == 1


def test_an_unknown_action_is_refused_and_nothing_is_switched(launcher):
    service = launcher.open_switch_service(STOP_DEADLINE_SECONDS, ENDPOINT_BACKLOG)
    outcome = ask_and_serve(service, "hardware-scanner", "sideways")

    assert outcome.outcome == OUTCOME_REFUSED
    assert launcher.is_running("hardware-scanner") is False
    assert service.requests_refused == 1


@pytest.mark.slow
def test_the_governor_switches_a_part_it_holds_no_socket_onto(launcher, bus_root):
    """T-2, end to end: a plan on the bus becomes a running process elsewhere."""
    from parts.resource_governor.switching_planner import SwitchDecision, SwitchPlan

    service = launcher.open_switch_service(STOP_DEADLINE_SECONDS, ENDPOINT_BACKLOG)
    wiring = derive_wiring(runtime_directory=bus_root)

    # A real consumer of switch-record, so the actuator's output has somewhere to go.
    record_reader = next(
        part_id for part_id, part in wiring.items() if "switch-record" in part.inboxes
    )
    records_inbox = Inbox(
        part_id=record_reader,
        data_type="switch-record",
        address=wiring[record_reader].inboxes["switch-record"],
        receive_buffer_bytes=RECEIVE_BUFFER_BYTES,
    )
    try:
        launcher.start("gate-actuator")
        assert wait_for_address(wiring["gate-actuator"].inboxes["switch-plan"]), (
            "gate-actuator never bound its switch-plan inbox"
        )
        plan = SwitchPlan(
            decisions=(SwitchDecision("hardware-scanner", ACTION_TURN_ON, "test asked for it", 50),),
            held=(),
            unplannable_reason=None,
            planned_at_ns=time.time_ns(),
        )

        sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sender.sendto(
                encode_frame(
                    data_type="switch-plan",
                    producer_part_id="switching-planner",
                    sequence=0,
                    published_at_ns=time.time_ns(),
                    payload=plan,
                    maximum_message_bytes=MAXIMUM_MESSAGE_BYTES,
                ),
                str(wiring["gate-actuator"].inboxes["switch-plan"]),
            )
        finally:
            sender.close()

        assert serve_until(service, lambda: launcher.is_running("hardware-scanner")), (
            "gate-actuator never asked the launcher to switch hardware-scanner on"
        )

        received = []
        assert serve_until(service, lambda: received.extend(records_inbox.drain()) or bool(received)), (
            "the switch happened and no switch-record was published for it"
        )
    finally:
        records_inbox.close()

    record = received[0].payload
    assert record.part_id == "hardware-scanner"
    assert record.action == ACTION_TURN_ON
    assert record.outcome == "flipped"
    assert service.requests_served >= 1


@pytest.mark.slow
def test_a_switch_the_launcher_could_not_make_is_recorded_as_failed_not_made(launcher, bus_root):
    """A refused switch must not be recorded as one that happened."""
    from parts.resource_governor.switching_planner import SwitchDecision, SwitchPlan

    service = launcher.open_switch_service(STOP_DEADLINE_SECONDS, ENDPOINT_BACKLOG)
    wiring = derive_wiring(runtime_directory=bus_root)
    record_reader = next(
        part_id for part_id, part in wiring.items() if "switch-record" in part.inboxes
    )
    records_inbox = Inbox(
        part_id=record_reader,
        data_type="switch-record",
        address=wiring[record_reader].inboxes["switch-record"],
        receive_buffer_bytes=RECEIVE_BUFFER_BYTES,
    )
    try:
        launcher.start("gate-actuator")
        assert wait_for_address(wiring["gate-actuator"].inboxes["switch-plan"]), (
            "gate-actuator never bound its switch-plan inbox"
        )
        plan = SwitchPlan(
            decisions=(SwitchDecision("a-part-nobody-wrote", ACTION_TURN_ON, "test", 50),),
            held=(),
            unplannable_reason=None,
            planned_at_ns=time.time_ns(),
        )
        sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sender.sendto(
                encode_frame(
                    data_type="switch-plan",
                    producer_part_id="switching-planner",
                    sequence=0,
                    published_at_ns=time.time_ns(),
                    payload=plan,
                    maximum_message_bytes=MAXIMUM_MESSAGE_BYTES,
                ),
                str(wiring["gate-actuator"].inboxes["switch-plan"]),
            )
        finally:
            sender.close()

        received = []
        assert serve_until(service, lambda: received.extend(records_inbox.drain()) or bool(received)), (
            "no switch-record was published for a switch that could not be made"
        )
    finally:
        records_inbox.close()

    record = received[0].payload
    assert record.part_id == "a-part-nobody-wrote"
    assert record.outcome == "failed"
    assert record.completed_at_ns is None
    assert "a-part-nobody-wrote" in record.reason or "PartHasNoModule" in record.reason


def test_stopping_through_the_endpoint_ends_the_process(launcher):
    service = launcher.open_switch_service(STOP_DEADLINE_SECONDS, ENDPOINT_BACKLOG)
    launched = launcher.start("hardware-scanner")
    outcome = ask_and_serve(service, "hardware-scanner", ACTION_TURN_OFF)

    assert outcome.outcome == OUTCOME_FLIPPED
    assert not launched.is_running
    assert launcher.is_running("hardware-scanner") is False
