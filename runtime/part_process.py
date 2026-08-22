"""The part template. One shape for every feature, no privileged parts (T-1).

A part's loop selects on {control fd, data inputs}. Turning it off is letting the
process exit -- there is no restore path, because startup already is one (section 4,
crash-only). A part owns no state that is not already durable outside it, so exiting
loses nothing.

Every output carries the part's current rate ratio and a staleness timestamp, so a
part running at reduced rate is visible as such on the board (section 6, Rule 8).
"""

from __future__ import annotations

import select
import time
from collections.abc import Callable
from dataclasses import dataclass

from runtime.control_channel import (
    COMMAND_REPORT_HEALTH,
    COMMAND_TURN_OFF,
    ControlFrameRefused,
    has_pending_command,
    receive_command,
)
from runtime.part_declaration import PartDeclaration

STATE_ON = "on"
STATE_OFF = "off"

EXIT_SWITCHED_OFF = 0
EXIT_CONTROL_CHANNEL_CLOSED = 0

# The full rate. A part that is not throttled reports this, so the board can tell
# "running normally" from "running at a quarter" without inferring either.
FULL_RATE_RATIO = 1.0

# No floor: a part woken by data ticks immediately. The floor is a setting the
# launcher supplies (spec section 7); zero here means the caller did not ask for one,
# which is also the behaviour of every part that has no inputs to be woken by.
NO_TICK_FLOOR = 0.0


@dataclass(frozen=True)
class PartHealth:
    """What a part says about itself, with the two facts section 6 requires.

    refused_control_frame carries the reason for the most recent control frame
    this part refused since its last health report, or None if it refused none.
    A part must not go quiet about a frame it refused -- health is the one
    outward channel it already has, so a refusal rides on it rather than being
    swallowed where only a crash would have shown it (the composition gap this
    field closes: receive_command already refuses a malformed frame instead of
    raising past run_part, but nothing carried that refusal to the governor).
    """

    part_id: str
    state: str
    rate_ratio: float
    staleness_seconds: float
    observed_at_ns: int
    refused_control_frame: str | None = None
    # What this part's inputs lost since it started, per data type, as (type, count)
    # pairs. Empty when nothing was lost. Loss rides on health because health is the
    # outward channel a part already has, and 223 parts declare that a skipped tick
    # corrupts their answer -- a part that lost input and stayed quiet would be
    # reporting an answer it cannot support.
    input_loss: tuple[tuple[str, int], ...] = ()


def compute_tick_interval(health_interval_seconds: float, rate_ratio: float) -> float:
    """How long the loop waits between ticks at a given fraction of full rate.

    Refuses a rate_ratio outside (0, FULL_RATE_RATIO]: zero or negative would
    divide by zero or run the part backwards, and above full rate is a speed the
    part never declared. Either is a bug in the caller, not a case to silently
    clamp -- a silent clamp is exactly how a throttle that does nothing goes
    unnoticed.
    """
    if not (0.0 < rate_ratio <= FULL_RATE_RATIO):
        raise ValueError(
            f"rate_ratio must be in (0, {FULL_RATE_RATIO}] -- a fraction of full rate -- "
            f"got {rate_ratio!r}. 0 or negative divides by zero or inverts the throttle; "
            f"anything above {FULL_RATE_RATIO} is a rate faster than full, which this part "
            f"never declared."
        )
    return health_interval_seconds / rate_ratio


@dataclass(frozen=True)
class Wake:
    """Why the loop stopped waiting. Control is answered first, always."""

    control_is_pending: bool
    data_arrived: bool


def wait_for_control_or_data(control_socket, input_descriptors, timeout_seconds: float) -> Wake:
    """Wait until a command arrives, data arrives, or the timeout expires.

    Waiting on both is what makes a part answer its inputs instead of its clock: a
    part that only woke on its timer would leave a fill sitting in an inbox until
    the interval came round. Control keeps its priority in the return value rather
    than in the wait, because the kernel reports both at once and a part that woke
    on data must still be switchable in the same pass (T-2).
    """
    watched = [control_socket, *input_descriptors]
    readable, _, _ = select.select(watched, [], [], max(0.0, timeout_seconds))
    return Wake(
        control_is_pending=control_socket in readable,
        data_arrived=any(descriptor is not control_socket for descriptor in readable),
    )


def run_part(
    declaration: PartDeclaration,
    control_socket,
    do_one_tick: Callable[[], None],
    emit_health: Callable[[PartHealth], None],
    health_interval_seconds: float,
    rate_ratio: float = FULL_RATE_RATIO,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = NO_TICK_FLOOR,
) -> int:
    """Run one part until the governor turns it off, then return.

    The tick and the control check share one loop deliberately: a part that blocked
    on its work and only read control between ticks would be a part the governor
    cannot switch, which is T-2 lost.

    input_descriptors are the part's inbox descriptors, from its bus. Given them,
    the loop wakes when data arrives rather than only when its timer expires; a part
    with no inputs -- twelve of them consume nothing -- passes none and behaves
    exactly as before.

    tick_floor_seconds is the fastest the part may be woken by data, so a producer
    with nothing to throttle it cannot spin a consumer. It is honoured without going
    deaf to control: the remainder is waited out on the control socket alone, which
    leaves the data queued and the switch still answerable.
    """
    last_tick_at = time.monotonic()
    last_health_at = 0.0
    tick_interval = compute_tick_interval(health_interval_seconds, rate_ratio)
    # The reason for the most recent control frame this part refused since its
    # last health report. A frame this part refuses is a fact the governor needs,
    # not a crash for it to infer one from -- so it rides the next health report
    # rather than being swallowed here.
    refused_control_frame: str | None = None

    def answer_control() -> int | None:
        """Read one waiting command. Returns an exit code when the part must stop."""
        nonlocal refused_control_frame, last_health_at
        try:
            frame = receive_command(control_socket)
        except ControlFrameRefused as refusal:
            # A malformed or unknown frame is a fact about one command, not a
            # reason for the part itself to exit -- the loop continues, and the
            # refusal surfaces through health rather than through a crash the
            # governor would otherwise have to read a refused frame as.
            refused_control_frame = str(refusal)
            last_health_at = 0.0  # force a health report that carries it
            return None
        if frame is None:
            # The governor closed the socket. A part whose governor is gone
            # turns off rather than running unsupervised.
            return EXIT_CONTROL_CHANNEL_CLOSED
        command, _payload = frame
        if command == COMMAND_TURN_OFF:
            return EXIT_SWITCHED_OFF
        if command == COMMAND_REPORT_HEALTH:
            last_health_at = 0.0  # force one out on the next pass
        return None

    while True:
        wake = wait_for_control_or_data(control_socket, input_descriptors, tick_interval)
        if wake.control_is_pending:
            exit_code = answer_control()
            if exit_code is not None:
                return exit_code

        if wake.data_arrived and tick_floor_seconds > NO_TICK_FLOOR:
            waited_since_tick = time.monotonic() - last_tick_at
            if waited_since_tick < tick_floor_seconds:
                # Hold the floor on the control socket alone: the data stays queued
                # in the inbox, and the part stays switchable while it waits.
                if has_pending_command(control_socket, tick_floor_seconds - waited_since_tick):
                    exit_code = answer_control()
                    if exit_code is not None:
                        return exit_code

        do_one_tick()
        now = time.monotonic()
        staleness = now - last_tick_at
        last_tick_at = now

        if now - last_health_at >= health_interval_seconds:
            emit_health(
                PartHealth(
                    part_id=declaration.part_id,
                    state=STATE_ON,
                    rate_ratio=rate_ratio,
                    staleness_seconds=staleness,
                    observed_at_ns=time.time_ns(),
                    refused_control_frame=refused_control_frame,
                )
            )
            last_health_at = now
            refused_control_frame = None
