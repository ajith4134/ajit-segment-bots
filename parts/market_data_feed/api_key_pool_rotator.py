"""api-key-pool-rotator: rotate private calls across a venue's keys, by standing.

Phase 1 holds no keys at all. The rotation logic here is complete and the key set
is honestly empty (RL-062): an empty pool routes nowhere and says so, rather than
pretending a key exists.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "api-key-pool-rotator"

PART_DECLARATION = PartDeclaration(
    part_id="api-key-pool-rotator",
    consumes=("venue-standing", "key-standing"),
    produces=("key-standing", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

KEY_SERVING = "serving"
KEY_THROTTLED = "throttled"
KEY_REJECTED = "rejected"
KEY_UNKNOWN = "unknown"


@dataclass(frozen=True)
class KeyStanding:
    """One key's current standing. No secret is ever held here -- only its id."""

    venue_id: str
    key_id: str
    state: str
    reason: str
    calls_made: int
    rejections: int
    observed_at_ns: int


@dataclass
class _KeyState:
    state: str = KEY_UNKNOWN
    reason: str = "no call made with this key yet"
    calls_made: int = 0
    rejections: int = 0
    rested_until_ns: int | None = None


@dataclass
class PoolStanding:
    keys_registered: int = 0
    calls_routed: int = 0
    calls_unroutable: int = 0
    keys_rejected: int = 0


class ApiKeyPoolRotator:
    """Picks the least-used serving key for a venue, and rests a rejected one.

    Only key ids cross this boundary. A rotator that held secrets would put them
    in every health report and every crash dump; the caller resolves an id to a
    credential wherever credentials actually live.
    """

    def __init__(self, rejection_rest_seconds: float, now_ns=time.time_ns) -> None:
        self._rest_seconds = rejection_rest_seconds
        self._now_ns = now_ns
        self._keys: dict[str, dict[str, _KeyState]] = {}
        self._venue_standing: dict[str, str] = {}
        self.standing = PoolStanding()

    def register_key(self, venue_id: str, key_id: str) -> None:
        self._keys.setdefault(venue_id, {})[key_id] = _KeyState()
        self.standing.keys_registered = sum(len(keys) for keys in self._keys.values())

    def set_venue_standing(self, venue_id: str, state: str) -> None:
        self._venue_standing[venue_id] = state

    def next_key(self, venue_id: str) -> str | None:
        """The least-used key that is serving, or None when the pool cannot serve."""
        if self._venue_standing.get(venue_id) == "banned":
            self.standing.calls_unroutable += 1
            return None
        keys = self._keys.get(venue_id, {})
        now = self._now_ns()
        usable = [
            (state.calls_made, key_id)
            for key_id, state in keys.items()
            if state.rested_until_ns is None or now >= state.rested_until_ns
        ]
        if not usable:
            self.standing.calls_unroutable += 1
            return None
        _, key_id = min(usable)
        keys[key_id].calls_made += 1
        keys[key_id].state = KEY_SERVING
        keys[key_id].reason = "serving"
        self.standing.calls_routed += 1
        return key_id

    def record_rejection(self, venue_id: str, key_id: str, reason: str) -> None:
        state = self._keys.setdefault(venue_id, {}).setdefault(key_id, _KeyState())
        state.rejections += 1
        state.state = KEY_REJECTED
        state.reason = reason
        state.rested_until_ns = self._now_ns() + int(self._rest_seconds * 1_000_000_000)
        self.standing.keys_rejected += 1

    def read_standings(self) -> tuple[KeyStanding, ...]:
        now = self._now_ns()
        standings = []
        for venue_id, keys in sorted(self._keys.items()):
            for key_id, state in sorted(keys.items()):
                resting = state.rested_until_ns is not None and now < state.rested_until_ns
                standings.append(
                    KeyStanding(
                        venue_id=venue_id,
                        key_id=key_id,
                        state=KEY_THROTTLED if resting else state.state,
                        reason=state.reason,
                        calls_made=state.calls_made,
                        rejections=state.rejections,
                        observed_at_ns=now,
                    )
                )
        return tuple(standings)


def describe_pool(rotator: ApiKeyPoolRotator) -> dict:
    return {
        "part_id": PART_ID,
        "keys_registered": rotator.standing.keys_registered,
        "calls_routed": rotator.standing.calls_routed,
        "calls_unroutable": rotator.standing.calls_unroutable,
        "keys_rejected": rotator.standing.keys_rejected,
        "standings": [standing.__dict__ for standing in rotator.read_standings()],
    }


def run_api_key_pool_rotator(
    rotator: ApiKeyPoolRotator, control_socket, read_events, publish_standings,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_events(rotator)
        publish_standings(rotator.read_standings())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    No keys are registered, because none are held (RL-062): a private call in
    phase 1 has nowhere to route and the standings this publishes are empty and
    say so. What it does consume is real -- a caller that was rejected publishes
    the key's standing back and the rotator rests it, and a venue's own standing
    withholds every key on it.
    """
    from runtime.input_assembly import Batch

    venues = Batch(read=context.bus.reader("venue-standing"))
    rejections = Batch(read=context.bus.reader("key-standing"))
    publish_standings = context.bus.publisher_for("key-standing")
    rotator = ApiKeyPoolRotator(rejection_rest_seconds=context.number("api_key_rejection_rest"))

    def read_events(_rotator) -> None:
        for standing in venues.payloads():
            rotator.set_venue_standing(standing.venue_id, standing.state)
        for standing in rejections.payloads():
            # Only a rejection from a caller is news to the pool; its own
            # standings do not come back to it (a part never receives its own
            # message), and another pool's serving key is not this pool's.
            if standing.state == KEY_REJECTED:
                rotator.record_rejection(standing.venue_id, standing.key_id, standing.reason)

    return run_api_key_pool_rotator(
        rotator=rotator,
        control_socket=context.control_socket,
        read_events=read_events,
        publish_standings=publish_standings,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
