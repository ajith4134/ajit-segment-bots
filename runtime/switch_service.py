"""How the part that decides reaches the component that switches.

T-2 says only the resource governor switches parts, and the runtime spec says the
switch travels the control plane, never the data plane. Those two together produce
a problem worth stating plainly: `gate-actuator` is a part, so it is a process of
its own with no descriptor onto any other part's control socket -- that is exactly
what makes the rule hold -- and yet it is the part whose whole job is flipping other
parts on and off.

So it asks. The launcher, which holds every control socket, binds one request
endpoint and serves it. `gate-actuator` sends `{part_id, action}` and is told what
happened. Nothing else can: the endpoint's address is handed to one part, chosen not
by name but by contract -- whoever the blueprint says turns a `switch-plan` into
`switch-record`s. A second actuator would be a second governor, and the lookup
refuses rather than picking one.

This is a request path, not a data path. It carries no market data, no opinions and
no fills, and no part ever receives one of its frames on an inbox.
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import struct
from collections.abc import Callable
from dataclasses import dataclass

# The endpoint's name. Not `<part>.<type>`, deliberately: it is not an inbox, and a
# name in the inbox shape would be one derive_wiring could collide with.
SWITCH_ENDPOINT_FILENAME = "switch-requests.control"

ACTION_TURN_ON = "on"
ACTION_TURN_OFF = "off"
KNOWN_ACTIONS = frozenset({ACTION_TURN_ON, ACTION_TURN_OFF})

OUTCOME_FLIPPED = "flipped"
OUTCOME_REFUSED = "refused"
OUTCOME_FAILED = "failed"

_LENGTH_PREFIX = struct.Struct("!I")

# A request is a part id, an action and a reason. Nothing here is large, and a
# ceiling means a malformed length cannot make the reader allocate.
MAXIMUM_FRAME_BYTES = 8192

# The data type whose producer and consumer identify the one part allowed to switch.
SWITCH_PLAN_TYPE = "switch-plan"
SWITCH_RECORD_TYPE = "switch-record"


class SwitchRequestRefused(ValueError):
    """A request was not acted on, and the reason is the message."""


class NoSingleSwitchActuator(LookupError):
    """The blueprint does not name exactly one part that switches parts."""


@dataclass(frozen=True)
class SwitchRequest:
    part_id: str
    action: str
    reason: str


@dataclass(frozen=True)
class SwitchOutcome:
    part_id: str
    action: str
    outcome: str
    detail: str


def find_switch_actuator(blueprint: dict) -> str:
    """The one part the blueprint says turns a switch plan into switch records.

    By contract rather than by name: the substrate must not carry a hardcoded part
    id, and R-01 already says a part is identified by the data it consumes and
    produces. If the blueprint ever declares two, this refuses -- T-2 permits one
    governor, and choosing between two candidates would be the substrate making a
    governance decision.
    """
    candidates = [
        feature["id"]
        for feature in blueprint["features"]
        if SWITCH_PLAN_TYPE in feature["consumes"] and SWITCH_RECORD_TYPE in feature["produces"]
    ]
    if len(candidates) != 1:
        raise NoSingleSwitchActuator(
            f"the blueprint declares {len(candidates)} parts that consume '{SWITCH_PLAN_TYPE}' and "
            f"produce '{SWITCH_RECORD_TYPE}': {candidates}. T-2 permits one part that switches "
            f"parts, and the substrate will not choose between candidates."
        )
    return candidates[0]


def switch_endpoint_address(runtime_directory: pathlib.Path) -> pathlib.Path:
    return runtime_directory / SWITCH_ENDPOINT_FILENAME


def _send_frame(sock: socket.socket, body: dict) -> None:
    encoded = json.dumps(body).encode()
    sock.sendall(_LENGTH_PREFIX.pack(len(encoded)) + encoded)


def _receive_frame(sock: socket.socket) -> dict | None:
    header = _receive_exactly(sock, _LENGTH_PREFIX.size)
    if header is None:
        return None
    (length,) = _LENGTH_PREFIX.unpack(header)
    if length > MAXIMUM_FRAME_BYTES:
        raise SwitchRequestRefused(
            f"a frame claiming {length} bytes was refused; the ceiling is {MAXIMUM_FRAME_BYTES}"
        )
    body = _receive_exactly(sock, length)
    if body is None:
        return None
    return json.loads(body)


def _receive_exactly(sock: socket.socket, count: int) -> bytes | None:
    chunks = []
    remaining = count
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class SwitchService:
    """The launcher's request endpoint. Serves; never initiates.

    It applies what it is asked and reports what happened -- including that it
    refused. A refusal is an outcome the actuator records as a switch-record, which
    is how a switch that did not take effect reaches the board instead of being
    inferred from a part that is mysteriously still running.
    """

    def __init__(
        self,
        address: pathlib.Path,
        turn_on: Callable[[str], str],
        turn_off: Callable[[str], str],
        backlog: int,
    ) -> None:
        self.address = str(address)
        self._turn_on = turn_on
        self._turn_off = turn_off
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            try:
                os.unlink(self.address)
            except FileNotFoundError:
                pass
            self._socket.bind(self.address)
            self._socket.listen(backlog)
            self._socket.setblocking(False)
        except OSError:
            self._socket.close()
            raise
        self.requests_served = 0
        self.requests_refused = 0

    def fileno(self) -> int:
        return self._socket.fileno()

    def serve_pending(self) -> tuple[SwitchOutcome, ...]:
        """Answer every request waiting right now. Never blocks."""
        served = []
        while True:
            try:
                connection, _peer = self._socket.accept()
            except BlockingIOError:
                return tuple(served)
            with connection:
                connection.setblocking(True)
                try:
                    frame = _receive_frame(connection)
                except (SwitchRequestRefused, json.JSONDecodeError, OSError) as refusal:
                    self.requests_refused += 1
                    continue
                if frame is None:
                    continue
                outcome = self._apply(frame)
                served.append(outcome)
                try:
                    _send_frame(connection, outcome.__dict__)
                except OSError:
                    pass  # the caller went away; the switch still happened

    def _apply(self, frame: dict) -> SwitchOutcome:
        part_id = str(frame.get("part_id", ""))
        action = str(frame.get("action", ""))
        reason = str(frame.get("reason", ""))
        if action not in KNOWN_ACTIONS:
            self.requests_refused += 1
            return SwitchOutcome(
                part_id=part_id,
                action=action,
                outcome=OUTCOME_REFUSED,
                detail=f"unknown action '{action}'; this endpoint knows {sorted(KNOWN_ACTIONS)}",
            )
        try:
            detail = self._turn_on(part_id) if action == ACTION_TURN_ON else self._turn_off(part_id)
        except Exception as failure:
            self.requests_refused += 1
            return SwitchOutcome(
                part_id=part_id,
                action=action,
                outcome=OUTCOME_FAILED,
                detail=f"{type(failure).__name__}: {failure}",
            )
        self.requests_served += 1
        return SwitchOutcome(part_id=part_id, action=action, outcome=OUTCOME_FLIPPED, detail=detail or reason)

    def close(self) -> None:
        self._socket.close()
        try:
            os.unlink(self.address)
        except FileNotFoundError:
            pass

    def __enter__(self) -> SwitchService:
        return self

    def __exit__(self, *_exception) -> None:
        self.close()


def open_switch_client(address: str, timeout_seconds: float) -> socket.socket:
    """Connect to the endpoint. The caller owns closing it."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout_seconds)
    try:
        client.connect(address)
    except OSError:
        client.close()
        raise
    return client


def send_switch_request(client: socket.socket, part_id: str, action: str, reason: str) -> None:
    _send_frame(client, {"part_id": part_id, "action": action, "reason": reason})


def read_switch_outcome(client: socket.socket, address: str = "") -> SwitchOutcome:
    reply = _receive_frame(client)
    if reply is None:
        raise SwitchRequestRefused(
            f"the switch endpoint at {address or 'the launcher'} closed without answering"
        )
    return SwitchOutcome(**reply)


def request_switch(address: str, part_id: str, action: str, reason: str, timeout_seconds: float) -> SwitchOutcome:
    """Ask the launcher to switch a part. Used only by the blueprint's one actuator.

    Raises rather than returning a verdict when the endpoint cannot be reached at
    all: an actuator that could not deliver its plan has not switched anything, and
    reporting that as an outcome would be reporting a switch that never happened.

    Split into three so a caller inside a loop can send without blocking on the
    answer -- which is also the only way to exercise this from a single-threaded
    test, since the launcher process must stay single-threaded to fork at all.
    """
    client = open_switch_client(address, timeout_seconds)
    try:
        send_switch_request(client, part_id, action, reason)
        return read_switch_outcome(client, address)
    finally:
        client.close()
