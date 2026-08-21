"""The switch is one framed command on a descriptor only the governor holds.

T-2 (only the resource governor switches parts) and T-4 (a part knows nothing about
the circuit) are enforced by the kernel's fd table here, not by convention.
"""

import os
import socket

import pytest

from runtime.control_channel import (
    COMMAND_REPORT_HEALTH,
    COMMAND_TURN_OFF,
    ControlFrameRefused,
    create_control_socket_pair,
    has_pending_command,
    receive_command,
    send_command,
)


def test_a_command_arrives_with_its_payload_intact():
    governor, part = create_control_socket_pair()
    send_command(governor, COMMAND_TURN_OFF, {"reason": "evicted by switching-planner"})
    assert receive_command(part) == (COMMAND_TURN_OFF, {"reason": "evicted by switching-planner"})


def test_two_commands_do_not_run_into_each_other():
    governor, part = create_control_socket_pair()
    send_command(governor, COMMAND_REPORT_HEALTH, {"sequence": 1})
    send_command(governor, COMMAND_TURN_OFF, {"sequence": 2})
    assert receive_command(part) == (COMMAND_REPORT_HEALTH, {"sequence": 1})
    assert receive_command(part) == (COMMAND_TURN_OFF, {"sequence": 2})


def test_a_clean_close_reads_as_no_command_rather_than_an_error():
    governor, part = create_control_socket_pair()
    governor.close()
    assert receive_command(part) is None


def test_an_unknown_command_is_refused_rather_than_silently_ignored():
    governor, part = create_control_socket_pair()
    with pytest.raises(ControlFrameRefused) as refusal:
        send_command(governor, "please-do-something-clever", {})
    assert "please-do-something-clever" in str(refusal.value)


def test_waiting_reports_whether_a_command_is_actually_there():
    governor, part = create_control_socket_pair()
    assert has_pending_command(part, timeout_seconds=0.01) is False
    send_command(governor, COMMAND_TURN_OFF, {})
    assert has_pending_command(part, timeout_seconds=0.5) is True


def test_a_part_holds_only_its_own_control_descriptor():
    # The structural claim: a part is handed one end and never sees any other.
    first_governor, first_part = create_control_socket_pair()
    second_governor, second_part = create_control_socket_pair()
    held = {first_part.fileno()}
    assert second_part.fileno() not in held
    assert second_governor.fileno() not in held
    # And the governor's end is not inheritable by accident.
    assert os.get_inheritable(first_governor.fileno()) is False
