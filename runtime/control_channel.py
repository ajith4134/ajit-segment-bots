"""One control socket per part, owned by the governor. That is the entire switch.

The part's loop selects on {control fd, data inputs}. gate-actuator writes one
framed command onto the control fd and nothing else does, because nothing else was
given the descriptor -- T-2 and T-4 are enforced by the kernel's fd table rather
than by code review.

Framing is a four-byte big-endian length followed by JSON. A stream socket has no
message boundaries of its own, so without a length prefix two commands sent quickly
arrive as one read and the second is lost.
"""

from __future__ import annotations

import json
import os
import select
import socket
import struct

COMMAND_TURN_ON = "on"
COMMAND_TURN_OFF = "off"
COMMAND_REPORT_HEALTH = "report-health"

KNOWN_COMMANDS = frozenset({COMMAND_TURN_ON, COMMAND_TURN_OFF, COMMAND_REPORT_HEALTH})

_LENGTH_PREFIX = struct.Struct("!I")
# A control frame carries a command and a small reason. Anything larger is a data
# payload arriving on the control path, which section 3 forbids outright.
MAXIMUM_FRAME_BYTES = 64 * 1024


class ControlFrameRefused(ValueError):
    """A frame was not a command this runtime knows, or was too large to be one."""


def create_control_socket_pair() -> tuple[socket.socket, socket.socket]:
    """Return (governor end, part end). Only the part end is inheritable."""
    governor_end, part_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    os.set_inheritable(governor_end.fileno(), False)
    os.set_inheritable(part_end.fileno(), True)
    return governor_end, part_end


def _receive_exactly(sock: socket.socket, count: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_command(sock: socket.socket, command: str, payload: dict) -> None:
    """Write one framed command. Refuses a command the runtime does not define."""
    if command not in KNOWN_COMMANDS:
        raise ControlFrameRefused(
            f"'{command}' is not a control command. T-5: states are explicit and countable, "
            f"and so are the commands that change them. Known: {sorted(KNOWN_COMMANDS)}."
        )
    body = json.dumps({"command": command, "payload": payload}).encode("utf-8")
    if len(body) > MAXIMUM_FRAME_BYTES:
        raise ControlFrameRefused(
            f"control frame is {len(body)} bytes, over the {MAXIMUM_FRAME_BYTES} limit. "
            f"A part never receives data on its control path (section 3)."
        )
    sock.sendall(_LENGTH_PREFIX.pack(len(body)) + body)


def receive_command(sock: socket.socket) -> tuple[str, dict] | None:
    """Read one framed command, or None when the governor closed the socket."""
    header = _receive_exactly(sock, _LENGTH_PREFIX.size)
    if header is None:
        return None
    (length,) = _LENGTH_PREFIX.unpack(header)
    if length > MAXIMUM_FRAME_BYTES:
        raise ControlFrameRefused(f"control frame claims {length} bytes; refusing to read it")
    body = _receive_exactly(sock, length)
    if body is None:
        return None
    frame = json.loads(body.decode("utf-8"))
    command = frame.get("command")
    if command not in KNOWN_COMMANDS:
        raise ControlFrameRefused(f"received unknown control command '{command}'")
    return command, frame.get("payload", {})


def has_pending_command(sock: socket.socket, timeout_seconds: float) -> bool:
    """Is there a command waiting? The part's loop selects on this and its data inputs."""
    readable, _, _ = select.select([sock], [], [], timeout_seconds)
    return bool(readable)
