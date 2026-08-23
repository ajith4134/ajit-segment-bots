"""ban-signal-detector: 418s, 429s, 403s and withheld streams into venue standing."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.venues.venue_adapter import BanSignal, VenueAdapter

PART_ID = "ban-signal-detector"

PART_DECLARATION = PartDeclaration(
    part_id="ban-signal-detector",
    consumes=("market-data", "feed-gap", "venue-rate-budget"),
    produces=("venue-standing", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

SERVING = "serving"
THROTTLED = "throttled"
BANNED = "banned"
UNKNOWN = "unknown"

# Fraction of a venue's stated rate budget above which it is treated as
# throttled rather than serving. Not a venue figure: it is how close this system
# is willing to run to a limit whose breach costs an IP ban outlasting any part.
THROTTLE_AT_BUDGET_FRACTION = 0.8


@dataclass(frozen=True)
class VenueStanding:
    """What a venue is currently willing to serve us, and why we think so."""

    venue_id: str
    state: str
    reason: str
    observed_at_ns: int
    banned_until_ns: int | None = None
    budget_used_fraction: float | None = None
    consecutive_gaps: int = 0


@dataclass
class _VenueState:
    banned_until_ns: int | None = None
    last_signal: BanSignal | None = None
    budget_used_fraction: float | None = None
    consecutive_gaps: int = 0
    observations: int = 0


@dataclass
class DetectorStanding:
    http_signals: int = 0
    stream_signals: int = 0
    bans_declared: int = 0
    throttles_declared: int = 0
    venues_seen: set[str] = field(default_factory=set)


class BanSignalDetector:
    """Turns whatever a venue says about refusing us into one standing per venue.

    Three sources, because a venue refuses in three ways: an HTTP status, an
    error over the websocket, and silence where data should be. The third is the
    one no venue reports, which is why feed gaps feed this part.

    Both venues ban per IP, so a ban outlives the part that caused it and holding
    no API key exempts nothing.
    """

    def __init__(
        self,
        adapters: dict[str, VenueAdapter],
        gaps_before_withheld: int,
        now_ns=time.time_ns,
    ) -> None:
        self._adapters = adapters
        self._gaps_before_withheld = gaps_before_withheld
        self._now_ns = now_ns
        self._states: dict[str, _VenueState] = {venue: _VenueState() for venue in adapters}
        self.standing = DetectorStanding()

    def observe_http_response(self, venue_id: str, status_code: int, headers: dict) -> BanSignal | None:
        adapter = self._adapters.get(venue_id)
        if adapter is None:
            return None
        signal = adapter.read_http_ban_signal(status_code, headers)
        self.standing.http_signals += 1 if signal else 0
        return self._apply(venue_id, signal)

    def observe_stream_payload(self, venue_id: str, payload: bytes) -> BanSignal | None:
        adapter = self._adapters.get(venue_id)
        if adapter is None:
            return None
        signal = adapter.read_stream_ban_signal(payload)
        self.standing.stream_signals += 1 if signal else 0
        return self._apply(venue_id, signal)

    def observe_rate_budget(self, venue_id: str, used: float, limit: float) -> None:
        """What the venue itself reports it has taken from us this window."""
        state = self._state(venue_id)
        state.budget_used_fraction = (used / limit) if limit else None
        state.observations += 1

    def observe_feed_gap(self, venue_id: str) -> None:
        """Silence counts. A withheld stream is a refusal nothing else reports."""
        self._state(venue_id).consecutive_gaps += 1

    def observe_data(self, venue_id: str) -> None:
        """Data arriving clears the silence count; it does not clear a ban."""
        self._state(venue_id).consecutive_gaps = 0
        self._state(venue_id).observations += 1

    def _state(self, venue_id: str) -> _VenueState:
        self.standing.venues_seen.add(venue_id)
        return self._states.setdefault(venue_id, _VenueState())

    def _apply(self, venue_id: str, signal: BanSignal | None) -> BanSignal | None:
        state = self._state(venue_id)
        if signal is None:
            return None
        state.last_signal = signal
        if signal.retry_after_seconds is not None:
            state.banned_until_ns = self._now_ns() + int(signal.retry_after_seconds * 1_000_000_000)
        else:
            # No stated duration is not "no ban". The backoff decides the wait,
            # and the standing stays banned until data proves otherwise.
            state.banned_until_ns = None
        return signal

    def read_standing(self, venue_id: str) -> VenueStanding:
        state = self._state(venue_id)
        now = self._now_ns()

        if state.banned_until_ns is not None and now < state.banned_until_ns:
            return self._standing(venue_id, BANNED, state, state.last_signal.reason if state.last_signal else "banned")
        if state.last_signal is not None and state.banned_until_ns is None:
            return self._standing(venue_id, BANNED, state, state.last_signal.reason)
        if state.consecutive_gaps >= self._gaps_before_withheld:
            return self._standing(
                venue_id, THROTTLED, state, f"{state.consecutive_gaps} consecutive feed gaps"
            )
        if state.budget_used_fraction is not None and state.budget_used_fraction >= THROTTLE_AT_BUDGET_FRACTION:
            return self._standing(
                venue_id, THROTTLED, state, f"{state.budget_used_fraction:.0%} of the venue's stated budget used"
            )
        if state.observations == 0:
            return self._standing(venue_id, UNKNOWN, state, "nothing observed from this venue yet")
        return self._standing(venue_id, SERVING, state, "serving, no refusal observed")

    def _standing(self, venue_id, state_name, state, reason) -> VenueStanding:
        if state_name == BANNED:
            self.standing.bans_declared += 1
        elif state_name == THROTTLED:
            self.standing.throttles_declared += 1
        return VenueStanding(
            venue_id=venue_id,
            state=state_name,
            reason=reason,
            observed_at_ns=self._now_ns(),
            banned_until_ns=state.banned_until_ns,
            budget_used_fraction=state.budget_used_fraction,
            consecutive_gaps=state.consecutive_gaps,
        )

    def clear_expired_bans(self) -> None:
        now = self._now_ns()
        for state in self._states.values():
            if state.banned_until_ns is not None and now >= state.banned_until_ns:
                state.banned_until_ns = None
                state.last_signal = None

    def read_all_standings(self) -> tuple[VenueStanding, ...]:
        self.clear_expired_bans()
        return tuple(self.read_standing(venue_id) for venue_id in sorted(self._states))


def describe_standings(detector: BanSignalDetector) -> dict:
    return {
        "part_id": PART_ID,
        "http_signals": detector.standing.http_signals,
        "stream_signals": detector.standing.stream_signals,
        "bans_declared": detector.standing.bans_declared,
        "throttles_declared": detector.standing.throttles_declared,
        "standings": [standing.__dict__ for standing in detector.read_all_standings()],
    }


def run_ban_signal_detector(
    detector: BanSignalDetector, control_socket, read_signals, publish_standings,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_signals(detector)
        publish_standings(detector.read_all_standings())

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

    Three signals, all from the bus: data arriving clears a venue's silence
    count, a feed gap raises it, and a rate budget says how much of the venue's
    own allowance is spent. HTTP responses and raw stream payloads are observed
    by the parts that hold those sockets, which publish what they see; this part
    reads the standing out of what reached it.
    """
    from runtime.input_assembly import Batch
    from runtime.part_context import RUNTIME_SCOPE
    from runtime.venues.adapter_registry import load_captured_venue_adapters

    market_data = Batch(read=context.bus.reader("market-data"))
    gaps = Batch(read=context.bus.reader("feed-gap"))
    budgets = Batch(read=context.bus.reader("venue-rate-budget"))
    publish_standings = context.bus.publisher_for("venue-standing")
    adapters = {
        adapter.venue_id: adapter
        for adapter in load_captured_venue_adapters(context.settings[RUNTIME_SCOPE])
    }
    detector = BanSignalDetector(
        adapters=adapters,
        gaps_before_withheld=int(context.number("venue_gaps_before_throttled")),
    )

    def read_signals(_detector) -> None:
        seen: set[str] = set()
        for item in market_data.payloads():
            seen.add(item.venue_id)
        for venue_id in seen:
            detector.observe_data(venue_id)
        for gap in gaps.payloads():
            detector.observe_feed_gap(gap.venue_id)
        for budget in budgets.payloads():
            detector.observe_rate_budget(budget.venue_id, budget.spent, budget.limit)

    return run_ban_signal_detector(
        detector=detector,
        control_socket=context.control_socket,
        read_signals=read_signals,
        publish_standings=publish_standings,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
