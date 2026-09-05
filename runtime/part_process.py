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
from typing import Mapping
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
    # The countable part of what this part says about its own work: how many times
    # it refused, fired, cleared a window, fell back to a prior. Carried because
    # every part in this system already computes exactly that in a describe_*
    # function and, until 2026-08-24, nothing read one -- so a part that had begun
    # refusing every decision looked identical to a part with nothing to decide.
    # Numbers only, and capped: see countable_standing.
    standing: tuple[tuple[str, float], ...] = ()
    # How many messages of each declared type this part has actually received and
    # published, as (type, count) pairs, zeros omitted. Carried because RL-072's
    # own words are that a part is done when it has been "observed running and
    # **exchanging its declared data** with a real neighbour" -- and until
    # 2026-08-25 the only thing measurable from outside was whether both ends were
    # alive. That reported 4,993 of 4,993 wires carrying within a minute of the
    # last block starting, which is a claim nobody had measured.
    messages_received: tuple[tuple[str, int], ...] = ()
    messages_published: tuple[tuple[str, int], ...] = ()
    # Per produced type, how many sends did not reach a consumer. The other half of
    # `messages_published`, which until 2026-09-02 counted these as publishes and so
    # showed a part sending into the void as one doing its job.
    messages_not_delivered: tuple[tuple[str, int], ...] = ()
    # What this part's work is made of, copied from its own declaration. Carried
    # because failing-part-detector's SUSPICIOUSLY_PERFECT rule reasons about
    # whether zero errors is suspicious, and that is only true of a part that
    # talks to something which can fail -- "in a system that talks to venues,
    # zero errors over a long run means errors are being swallowed". For a
    # compute-bound part zero errors is simply what working looks like, and until
    # 2026-09-05 the rule fired on all 229 of them: 16,877 of Friday's 48,084
    # escalations were this fault restated about parts that cannot have the
    # problem it describes. The detector reads part-health and had no other way
    # to know, so the fact travels with the health it is judged from.
    resource_class: str = ""


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


# How many of a part's own counters ride on one health report. The bus refuses a
# datagram over 128 KiB and health is published on every interval by every part,
# so this is a ceiling on what one part can spend of that. Thirty-two is more
# counters than any describe_* in this system currently produces.
MOST_STANDING_COUNTERS = 32
# How many keys a map in a part's standing may have before it is reported as a
# count instead of flattened. A refusal-by-reason map is a closed set of names --
# the largest in this codebase is under a dozen -- while a per-symbol map is the
# whole universe. Sixteen separates the two without needing either to declare
# which it is.
MOST_KEYS_IN_A_STANDING_MAP = 16

# A name ending `_at_ns` states a point in wall-clock time, and a point in time is
# not a counter. Measured 2026-08-27 on the live board: the busiest counter across
# the whole spine read `hardware-scanner capacity.measured_at_ns 900,251,934/s`,
# and nothing was working that hard. Every board here rates a standing counter as
# delta over elapsed seconds; a nanosecond clock advances a billion per second by
# definition, so a timestamp outranks every real counter by six orders of magnitude
# and wins "busiest" permanently. Worse, a part is judged WORKING when any counter
# moved, so a clock alone would paint a part green while it did nothing -- Rule 8's
# failure exactly.
#
# Left behind here rather than filtered where the rate is taken: every board that
# rates these counters would otherwise need the same rule, and the payload on the
# bus still carries the timestamp for any reader judging staleness.
#
# Deliberately a pattern here, where `level_publishing.OBSERVATION_TIME_FIELDS` is
# a fixed list, because the two ask different questions. There the question is
# whether a field says "still true" or says something a reader acts on, and
# `next_settlement_at_ns` is content -- so a pattern would have stopped publishing
# what a reader was waiting for. Here the question is only whether a number is a
# count, and no wall-clock reading is, content or not. The pattern is `_at_ns` and
# not `_ns`: cumulative time spent is a genuine counter whose rate is the fraction
# of a core a part is using, which is exactly what a board should show.
CLOCK_NAME_ENDINGS = ("_at_ns",)


def is_a_clock_rather_than_a_counter(name) -> bool:
    """Whether a standing key states a moment in time instead of a quantity.

    A key that is not a name is never a clock: a counter map may be keyed by
    anything a part counts by, and `duty-cycle-planner` keys one by an integer
    rung. Asking such a key how it ends crashed every part that had one.
    """
    return isinstance(name, str) and name.endswith(CLOCK_NAME_ENDINGS)


def countable_standing(standing) -> tuple[tuple[str, float], ...]:
    """The numeric facts in a part's standing, flattened, sorted and capped.

    Numbers only. A part's standing also holds names, reasons and per-symbol maps
    that grow with the universe, and none of those belong on a channel every part
    writes to on every interval. Left behind rather than truncated: a number that
    arrived half-serialised is worse than one that did not arrive.

    Sorted by name so the same keys survive from one report to the next -- a cap
    that dropped a different counter each time would make a rising count look like
    a falling one.

    **A small map of counters is flattened one level, as `name.key`** (2026-08-26).
    Every part in this system that refuses things counts them by reason, and every
    one of those counters was being dropped here -- so a gate refusing 100% of
    what it saw showed a refusal total on the board and no way to see which
    condition did it. Measured that day: `instruction-writer` reported
    `requests 494, refused 494, written 0` and its `by_failing_condition` reached
    nothing, which is Rule 8's failure exactly -- a number nobody can trace to a
    reason is a number nobody can act on.

    **A wall-clock timestamp is left behind whatever its level** (2026-08-27). A
    `*_at_ns` name states a moment, not a quantity, and every board here rates a
    standing counter as delta over elapsed seconds -- see `CLOCK_NAME_ENDINGS`.

    **A map larger than `MOST_KEYS_IN_A_STANDING_MAP` is still left behind**, and
    its size is reported in its place as `name.keys_not_reported`. That bound is
    what keeps a per-symbol map off this channel: at the full universe such a map
    is ~1,500 entries, and flattening one would evict every other counter the part
    has under the cap below. Reporting the count rather than nothing means an
    omission still shows up as an omission.
    """
    if not standing:
        return ()
    numeric = []
    for name, value in standing.items():
        if is_a_clock_rather_than_a_counter(name):
            continue
        if isinstance(value, bool):
            numeric.append((name, float(value)))
        elif isinstance(value, (int, float)):
            numeric.append((name, float(value)))
        elif isinstance(value, Mapping):
            numeric.extend(_flattened_counters(name, value))
    return tuple(sorted(numeric)[:MOST_STANDING_COUNTERS])


def _flattened_counters(name: str, mapping) -> list[tuple[str, float]]:
    """One level of a bounded counter map, or a count of what was left behind."""
    if len(mapping) > MOST_KEYS_IN_A_STANDING_MAP:
        return [(f"{name}.keys_not_reported", float(len(mapping)))]
    flattened = []
    for key, value in mapping.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if is_a_clock_rather_than_a_counter(key):
            continue
        flattened.append((f"{name}.{key}", float(value)))
    return flattened


def run_part(
    declaration: PartDeclaration,
    control_socket,
    do_one_tick: Callable[[], None],
    emit_health: Callable[[PartHealth], None],
    health_interval_seconds: float,
    rate_ratio: float = FULL_RATE_RATIO,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = NO_TICK_FLOOR,
    read_standing: Callable[[], dict] | None = None,
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
                    standing=countable_standing(read_standing() if read_standing else None),
                    resource_class=str(declaration.resource_class),
                )
            )
            last_health_at = now
            refused_control_frame = None
