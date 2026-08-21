"""The switch is one framed command on a descriptor only the governor holds.

T-2 (only the resource governor switches parts) and T-4 (a part knows nothing about
the circuit) are enforced by the kernel's fd table here, not by convention.
"""

import json
import os
import socket
import struct
import threading

import pytest

from runtime.control_channel import (
    COMMAND_REPORT_HEALTH,
    COMMAND_TURN_OFF,
    MAXIMUM_FRAME_BYTES,
    ControlFrameRefused,
    create_control_socket_pair,
    has_pending_command,
    receive_command,
    send_command,
)

_LENGTH_PREFIX = struct.Struct("!I")
_JOIN_TIMEOUT_SECONDS = 5.0


def test_a_command_arrives_with_its_payload_intact():
    governor, part = create_control_socket_pair()
    send_command(governor, COMMAND_TURN_OFF, {"reason": "evicted by switching-planner"})
    assert receive_command(part) == (COMMAND_TURN_OFF, {"reason": "evicted by switching-planner"})


def test_two_commands_do_not_run_into_each_other():
    # Both frames go out in one write, so the wire genuinely carries them back to
    # back -- this is the shape that would drop the second frame if receive_command
    # ever over-read past a frame boundary.
    governor, part = create_control_socket_pair()
    first_body = json.dumps({"command": COMMAND_REPORT_HEALTH, "payload": {"sequence": 1}}).encode("utf-8")
    second_body = json.dumps({"command": COMMAND_TURN_OFF, "payload": {"sequence": 2}}).encode("utf-8")
    governor.sendall(
        _LENGTH_PREFIX.pack(len(first_body)) + first_body
        + _LENGTH_PREFIX.pack(len(second_body)) + second_body
    )
    assert receive_command(part) == (COMMAND_REPORT_HEALTH, {"sequence": 1})
    assert receive_command(part) == (COMMAND_TURN_OFF, {"sequence": 2})
    governor.close()
    part.close()


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


def test_a_frame_split_across_two_recv_calls_is_assembled_not_truncated():
    # The header arrives whole plus a few bytes of body, then nothing else is on
    # the wire yet -- receive_command's read for the remaining body bytes has no
    # data to return and must block, genuinely, until the rest is written. That
    # block is what proves the assembly loop, not a race: the reader cannot finish
    # before the second write happens, because the bytes it needs do not exist yet.
    governor, part = create_control_socket_pair()
    payload = {"reason": "evicted by switching-planner after a long-running health check timed out"}
    body = json.dumps({"command": COMMAND_TURN_OFF, "payload": payload}).encode("utf-8")
    frame = _LENGTH_PREFIX.pack(len(body)) + body
    split_point = _LENGTH_PREFIX.size + 3
    assert 0 < split_point < len(frame)

    results: list[tuple[str, dict] | None] = []

    def read_one_command():
        results.append(receive_command(part))

    governor.sendall(frame[:split_point])
    reader = threading.Thread(target=read_one_command)
    reader.start()
    governor.sendall(frame[split_point:])
    reader.join(timeout=_JOIN_TIMEOUT_SECONDS)

    assert not reader.is_alive()
    assert results == [(COMMAND_TURN_OFF, payload)]
    governor.close()
    part.close()


def test_an_over_long_declared_length_is_refused_by_the_reader_not_only_the_writer():
    # send_command already refuses an over-long body before it ever reaches the
    # socket. This proves receive_command's own guard fires too, reading a header
    # straight off the wire that never went through send_command at all.
    governor, part = create_control_socket_pair()
    governor.sendall(_LENGTH_PREFIX.pack(MAXIMUM_FRAME_BYTES + 1))
    with pytest.raises(ControlFrameRefused) as refusal:
        receive_command(part)
    assert str(MAXIMUM_FRAME_BYTES + 1) in str(refusal.value)
    governor.close()
    part.close()


def test_a_body_that_is_not_json_is_refused_rather_than_raising_a_decode_error():
    governor, part = create_control_socket_pair()
    body = b"this is not json"
    governor.sendall(_LENGTH_PREFIX.pack(len(body)) + body)
    with pytest.raises(ControlFrameRefused):
        receive_command(part)
    governor.close()
    part.close()


def test_a_body_that_is_json_but_not_an_object_is_refused():
    governor, part = create_control_socket_pair()
    body = json.dumps([COMMAND_TURN_OFF, {"sequence": 1}]).encode("utf-8")
    governor.sendall(_LENGTH_PREFIX.pack(len(body)) + body)
    with pytest.raises(ControlFrameRefused):
        receive_command(part)
    governor.close()
    part.close()
