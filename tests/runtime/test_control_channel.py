"""The switch is one framed command on a descriptor only the governor holds.

T-2 (only the resource governor switches parts) and T-4 (a part knows nothing about
the circuit) are enforced by the kernel's fd table here, not by convention.
"""

import fcntl
import json
import os
import socket
import struct
import termios
import threading
import time

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
_DRAIN_DEADLINE_SECONDS = 2.0
_DRAIN_POLL_INTERVAL_SECONDS = 0.001


@pytest.fixture
def control_socket_pair():
    """A governor/part pair for one test, closed on teardown either way.

    A test that already closes one end itself (the clean-close test) does not
    need special-casing: socket.close() is idempotent, so the second close here
    is a no-op.
    """
    governor, part = create_control_socket_pair()
    yield governor, part
    governor.close()
    part.close()


def _pending_receive_bytes(sock: socket.socket) -> int:
    """How many bytes are sitting in sock's kernel receive buffer, unread."""
    raw = fcntl.ioctl(sock.fileno(), termios.FIONREAD, struct.pack("I", 0))
    return struct.unpack("I", raw)[0]


def _wait_until_drained(sock: socket.socket, deadline_seconds: float, poll_interval_seconds: float) -> None:
    """Block until sock's receive buffer reads empty, or fail loudly.

    Zero pending bytes means every byte written so far has already been pulled
    out by a recv() call on the reading end. If that happens before the rest of
    a frame has been written, the reader has nothing left to consume and is
    genuinely blocked inside recv(), waiting -- which is the exact state the
    assembly loop in _receive_exactly exists to survive. This is a kernel fact,
    read with FIONREAD, not a guess about scheduling.
    """
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if _pending_receive_bytes(sock) == 0:
            return
        time.sleep(poll_interval_seconds)
    raise AssertionError(
        f"{_pending_receive_bytes(sock)} bytes still pending on the socket after "
        f"{deadline_seconds}s of polling; the reader never drained the partial "
        f"frame, so this run never reached the blocked-mid-frame state it needs to."
    )


def test_a_command_arrives_with_its_payload_intact(control_socket_pair):
    governor, part = control_socket_pair
    send_command(governor, COMMAND_TURN_OFF, {"reason": "evicted by switching-planner"})
    assert receive_command(part) == (COMMAND_TURN_OFF, {"reason": "evicted by switching-planner"})


def test_two_commands_do_not_run_into_each_other(control_socket_pair):
    # Both frames go out in one write, so the wire genuinely carries them back to
    # back -- this is the shape that would drop the second frame if receive_command
    # ever over-read past a frame boundary.
    governor, part = control_socket_pair
    first_body = json.dumps({"command": COMMAND_REPORT_HEALTH, "payload": {"sequence": 1}}).encode("utf-8")
    second_body = json.dumps({"command": COMMAND_TURN_OFF, "payload": {"sequence": 2}}).encode("utf-8")
    governor.sendall(
        _LENGTH_PREFIX.pack(len(first_body)) + first_body
        + _LENGTH_PREFIX.pack(len(second_body)) + second_body
    )
    assert receive_command(part) == (COMMAND_REPORT_HEALTH, {"sequence": 1})
    assert receive_command(part) == (COMMAND_TURN_OFF, {"sequence": 2})


def test_a_clean_close_reads_as_no_command_rather_than_an_error(control_socket_pair):
    governor, part = control_socket_pair
    governor.close()
    assert receive_command(part) is None


def test_an_unknown_command_is_refused_rather_than_silently_ignored(control_socket_pair):
    governor, part = control_socket_pair
    with pytest.raises(ControlFrameRefused) as refusal:
        send_command(governor, "please-do-something-clever", {})
    assert "please-do-something-clever" in str(refusal.value)


def test_waiting_reports_whether_a_command_is_actually_there(control_socket_pair):
    governor, part = control_socket_pair
    assert has_pending_command(part, timeout_seconds=0.01) is False
    send_command(governor, COMMAND_TURN_OFF, {})
    assert has_pending_command(part, timeout_seconds=0.5) is True


def test_a_part_holds_only_its_own_control_descriptor(control_socket_pair):
    # The structural claim: a part is handed one end and never sees any other.
    first_governor, first_part = control_socket_pair
    second_governor, second_part = create_control_socket_pair()
    try:
        held = {first_part.fileno()}
        assert second_part.fileno() not in held
        assert second_governor.fileno() not in held
        # And the governor's end is not inheritable by accident.
        assert os.get_inheritable(first_governor.fileno()) is False
    finally:
        second_governor.close()
        second_part.close()


def test_a_frame_split_across_two_recv_calls_is_assembled_not_truncated(control_socket_pair):
    # The header arrives whole plus a few bytes of body, then nothing else is on
    # the wire. The reader thread consumes what's there and, with more of the body
    # still to come, blocks inside recv(). We do not assume that block happened --
    # we poll the kernel's own byte count for the socket (FIONREAD) until it reads
    # zero, which is only possible once the reader has drained everything sent so
    # far and is waiting on the rest. Only then do we write the remainder. If the
    # drain is never observed, _wait_until_drained fails the test loudly instead of
    # letting it pass on an unproven assumption.
    governor, part = control_socket_pair
    payload = {"reason": "evicted by switching-planner after a long-running health check timed out"}
    body = json.dumps({"command": COMMAND_TURN_OFF, "payload": payload}).encode("utf-8")
    frame = _LENGTH_PREFIX.pack(len(body)) + body
    split_point = _LENGTH_PREFIX.size + 3
    assert 0 < split_point < len(frame)
    assert len(body) > 3  # there is genuinely more body left to receive after the split

    results: list[tuple[str, dict] | None] = []

    def read_one_command():
        results.append(receive_command(part))

    governor.sendall(frame[:split_point])
    reader = threading.Thread(target=read_one_command)
    reader.start()

    _wait_until_drained(part, deadline_seconds=_DRAIN_DEADLINE_SECONDS, poll_interval_seconds=_DRAIN_POLL_INTERVAL_SECONDS)

    governor.sendall(frame[split_point:])
    reader.join(timeout=_JOIN_TIMEOUT_SECONDS)

    assert not reader.is_alive()
    assert results == [(COMMAND_TURN_OFF, payload)]


def test_an_over_long_declared_length_is_refused_by_the_reader_not_only_the_writer(control_socket_pair):
    # send_command already refuses an over-long body before it ever reaches the
    # socket. This proves receive_command's own guard fires too, reading a header
    # straight off the wire that never went through send_command at all.
    governor, part = control_socket_pair
    governor.sendall(_LENGTH_PREFIX.pack(MAXIMUM_FRAME_BYTES + 1))
    with pytest.raises(ControlFrameRefused) as refusal:
        receive_command(part)
    assert str(MAXIMUM_FRAME_BYTES + 1) in str(refusal.value)


def test_a_body_that_is_not_json_is_refused_rather_than_raising_a_decode_error(control_socket_pair):
    governor, part = control_socket_pair
    body = b"this is not json"
    governor.sendall(_LENGTH_PREFIX.pack(len(body)) + body)
    with pytest.raises(ControlFrameRefused):
        receive_command(part)


def test_a_body_that_is_json_but_not_an_object_is_refused(control_socket_pair):
    governor, part = control_socket_pair
    body = json.dumps([COMMAND_TURN_OFF, {"sequence": 1}]).encode("utf-8")
    governor.sendall(_LENGTH_PREFIX.pack(len(body)) + body)
    with pytest.raises(ControlFrameRefused):
        receive_command(part)


def test_a_declared_length_of_zero_with_no_body_is_refused(control_socket_pair):
    governor, part = control_socket_pair
    governor.sendall(_LENGTH_PREFIX.pack(0))
    with pytest.raises(ControlFrameRefused):
        receive_command(part)
