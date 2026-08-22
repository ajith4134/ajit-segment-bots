"""venue-outage-rider: what is true about a venue that has gone quiet.

A silent feed is not an absence of information, and treating it as one is how a
system holds a position through a crash it never saw. Silence has three possible
causes and they demand opposite responses:

- **The market is quiet.** Nothing is wrong; a thin symbol at 4am produces long gaps
  and reconnecting achieves nothing.
- **The connection is broken.** The venue is fine and this system is deaf. Reconnect,
  and treat every cached price as stale in the meantime.
- **The venue itself is down.** Nothing will help until it is back, and the positions
  held there are still open, still moving, and still liquidatable.

Distinguishing them is why this part exists, and it does it by comparing the silent
symbol against the rest of the venue: one symbol quiet while others flow is that
symbol; every symbol quiet at once is the connection or the venue.

**The state that matters most is unreachable-with-exposure.** A venue this system
cannot see but has positions on is the only situation in this block that is worse than
being halted, and it is reported as its own state rather than as a degraded case of
being offline.

Nothing here reconnects or trades. It reports what may be believed, and the parts that
act on it -- the halt decider, the exposure view -- decide what to do, because a rider
that also acted would be making the same judgement twice with different information.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.autonomy_types import OutageState
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "venue-outage-rider"

PART_DECLARATION = PartDeclaration(
    part_id="venue-outage-rider",
    consumes=("market-data", "part-health", "feed-gap"),
    produces=("outage-state", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FLOWING = "data-is-arriving"
QUIET_MARKET = "this-symbol-is-quiet-and-the-venue-is-not"
CONNECTION_LOST = "this-system-is-deaf-and-the-venue-is-probably-fine"
VENUE_DOWN = "the-venue-itself-is-not-answering"
UNREACHABLE_WITH_EXPOSURE = "positions-are-open-somewhere-this-system-cannot-see"
NOT_MEASURED = "no-message-has-ever-arrived-from-this-venue"


@dataclass(frozen=True)
class OutageReading:
    venue_id: str
    state: str
    outage: OutageState
    symbols_silent: int
    symbols_total: int
    reason: str
    measured_at_ns: int

    @property
    def needs_a_human(self) -> bool:
        return self.state == UNREACHABLE_WITH_EXPOSURE


@dataclass
class RiderStanding:
    readings: int = 0
    flowing: int = 0
    quiet_markets: int = 0
    connections_lost: int = 0
    venues_down: int = 0
    unreachable_with_exposure: int = 0
    reconnects_attempted: int = 0
    longest_silence_seconds: float = 0.0


class VenueOutageRider:
    """Tells a quiet market apart from a broken connection apart from a dead venue."""

    def __init__(
        self,
        silence_seconds: float,
        venue_wide_fraction: float,
        failures_before_venue_down: int,
        now_ns=time.time_ns,
    ) -> None:
        if silence_seconds <= 0:
            raise ValueError(
                "silence needs a length before it means anything; a thin symbol at 4am "
                "produces long gaps and nothing is wrong"
            )
        if not 0.0 < venue_wide_fraction <= 1.0:
            raise ValueError(
                "one symbol quiet while others flow is that symbol; the fraction is how "
                "many must go quiet together for it to be the venue"
            )
        if failures_before_venue_down < 1:
            raise ValueError("one failed request is not a dead venue")
        self._silence_seconds = silence_seconds
        self._venue_wide_fraction = venue_wide_fraction
        self._failures_before_down = failures_before_venue_down
        self._now_ns = now_ns
        self._last_message: dict[tuple, int] = {}
        self._symbols: dict[str, set] = {}
        self._request_failures: dict[str, int] = {}
        self._exposure: dict[str, bool] = {}
        self.standing = RiderStanding()

    def observe_message(self, venue_id: str, symbol: str, at_ns: int) -> None:
        self._symbols.setdefault(venue_id, set()).add(symbol)
        self._last_message[(venue_id, symbol)] = at_ns
        self._request_failures[venue_id] = 0

    def observe_request_failure(self, venue_id: str) -> None:
        """A failed request separates a deaf reader from a dead venue."""
        self._request_failures[venue_id] = self._request_failures.get(venue_id, 0) + 1

    def observe_exposure(self, venue_id: str, has_open_exposure: bool) -> None:
        self._exposure[venue_id] = has_open_exposure

    def silent_symbols(self, venue_id: str) -> tuple:
        now = self._now_ns()
        silent = []
        for symbol in self._symbols.get(venue_id, set()):
            last = self._last_message.get((venue_id, symbol))
            if last is None:
                continue
            if (now - last) / 1e9 >= self._silence_seconds:
                silent.append(symbol)
        return tuple(sorted(silent))

    def measure(self, venue_id: str) -> OutageReading:
        self.standing.readings += 1
        symbols = self._symbols.get(venue_id, set())
        has_exposure = self._exposure.get(venue_id, False)

        if not symbols:
            return self._reading(
                venue_id, NOT_MEASURED, False, 0.0, has_exposure, 0, 0,
                "no message has ever arrived from this venue, which is not the same as "
                "the venue being down",
            )

        silent = self.silent_symbols(venue_id)
        now = self._now_ns()
        oldest = min(
            (now - self._last_message[(venue_id, symbol)]) / 1e9
            for symbol in symbols
            if (venue_id, symbol) in self._last_message
        )
        longest = max(
            (now - self._last_message[(venue_id, symbol)]) / 1e9
            for symbol in symbols
            if (venue_id, symbol) in self._last_message
        )
        self.standing.longest_silence_seconds = max(
            self.standing.longest_silence_seconds, longest
        )

        failures = self._request_failures.get(venue_id, 0)
        silent_fraction = len(silent) / len(symbols)

        if not silent:
            self.standing.flowing += 1
            return self._reading(
                venue_id, FLOWING, True, oldest, has_exposure, 0, len(symbols),
                f"data is arriving on all {len(symbols)} symbol(s)",
            )

        if silent_fraction < self._venue_wide_fraction:
            self.standing.quiet_markets += 1
            return self._reading(
                venue_id, QUIET_MARKET, True, longest, has_exposure, len(silent),
                len(symbols),
                f"{len(silent)} of {len(symbols)} symbol(s) are quiet while the rest "
                f"flow. That is those symbols, not the venue, and reconnecting would "
                f"achieve nothing",
            )

        # Everything is silent. Failed requests separate a dead venue from a deaf reader.
        is_venue_down = failures >= self._failures_before_down
        state = VENUE_DOWN if is_venue_down else CONNECTION_LOST
        if is_venue_down:
            self.standing.venues_down += 1
        else:
            self.standing.connections_lost += 1

        if has_exposure:
            self.standing.unreachable_with_exposure += 1
            return self._reading(
                venue_id, UNREACHABLE_WITH_EXPOSURE, False, longest, True, len(silent),
                len(symbols),
                f"every symbol has been silent for {longest:.0f}s and positions are open "
                f"here. They are still moving and still liquidatable, and this system "
                f"cannot see them. This is the state that has to reach a person",
            )

        return self._reading(
            venue_id, state, False, longest, False, len(silent), len(symbols),
            f"every symbol silent for {longest:.0f}s with {failures} failed request(s). "
            + (
                "The venue itself is not answering, so nothing will help until it is back"
                if is_venue_down
                else "Requests still work, so this system is deaf rather than the venue "
                     "being down -- every cached price is stale until it reconnects"
            ),
        )

    def _reading(
        self, venue_id, state, is_reachable, silent_seconds, has_exposure, silent,
        total, reason,
    ) -> OutageReading:
        return OutageReading(
            venue_id=venue_id, state=state,
            outage=OutageState(
                venue_id=venue_id, is_reachable=is_reachable,
                last_message_at_ns=self._now_ns() - int(silent_seconds * 1e9),
                silent_seconds=silent_seconds, has_open_exposure=has_exposure,
                consecutive_failures=self._request_failures.get(venue_id, 0),
                state=state, reason=reason, measured_at_ns=self._now_ns(),
            ),
            symbols_silent=silent, symbols_total=total, reason=reason,
            measured_at_ns=self._now_ns(),
        )


def describe_outage_riding(rider: VenueOutageRider) -> dict:
    return {
        "part_id": PART_ID,
        "readings": rider.standing.readings,
        "flowing": rider.standing.flowing,
        "quiet_markets": rider.standing.quiet_markets,
        "connections_lost": rider.standing.connections_lost,
        "venues_down": rider.standing.venues_down,
        "unreachable_with_exposure": rider.standing.unreachable_with_exposure,
        "longest_silence_seconds": rider.standing.longest_silence_seconds,
        "reconnects": False,
        "reconnects_attempted": rider.standing.reconnects_attempted,
        "treats_silence_as_no_information": False,
    }


def run_venue_outage_rider(
    rider: VenueOutageRider, control_socket, read_messages, publish_states,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        for venue_id, symbol, at_ns in read_messages():
            rider.observe_message(venue_id, symbol, at_ns)
        for venue_id in list(rider._symbols):
            publish_states(rider.measure(venue_id).outage)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
