"""The part template. One shape for every feature, no privileged parts (T-1).

A part's loop selects on {control fd, data inputs}. Turning it off is letting the
process exit -- there is no restore path, because startup already is one (section 4,
crash-only). A part owns no state that is not already durable outside it, so exiting
loses nothing.

Every output carries the part's current rate ratio and a staleness timestamp, so a
part running at reduced rate is visible as such on the board (section 6, Rule 8).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from runtime.control_channel import (
    COMMAND_REPORT_HEALTH,
    COMMAND_TURN_OFF,
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


@dataclass(frozen=True)
class PartHealth:
    """What a part says about itself, with the two facts section 6 requires."""

    part_id: str
    state: str
    rate_ratio: float
    staleness_seconds: float
    observed_at_ns: int


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


def run_part(
    declaration: PartDeclaration,
    control_socket,
    do_one_tick: Callable[[], None],
    emit_health: Callable[[PartHealth], None],
    health_interval_seconds: float,
    rate_ratio: float = FULL_RATE_RATIO,
) -> int:
    """Run one part until the governor turns it off, then return.

    The tick and the control check share one loop deliberately: a part that blocked
    on its work and only read control between ticks would be a part the governor
    cannot switch, which is T-2 lost.
    """
    last_tick_at = time.monotonic()
    last_health_at = 0.0
    tick_interval = compute_tick_interval(health_interval_seconds, rate_ratio)

    while True:
        if has_pending_command(control_socket, timeout_seconds=tick_interval):
            frame = receive_command(control_socket)
            if frame is None:
                # The governor closed the socket. A part whose governor is gone turns
                # off rather than running unsupervised.
                return EXIT_CONTROL_CHANNEL_CLOSED
            command, _payload = frame
            if command == COMMAND_TURN_OFF:
                return EXIT_SWITCHED_OFF
            if command == COMMAND_REPORT_HEALTH:
                last_health_at = 0.0  # force one out on the next pass

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
                )
            )
            last_health_at = now
