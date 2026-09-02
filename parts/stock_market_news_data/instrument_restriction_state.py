"""instrument-restriction-state: state whether an instrument may be traded now.

The level behind every refusal in the hard channel. Reports arrive per source;
this merges them per symbol and lets each source's claim expire on its own age
bound.

**The expiry is the whole point.** NSE does not publish an un-ban -- a symbol
whose ban lifts simply stops appearing in fo_secban.csv. A level that never
expired would therefore ban a name permanently the first day it was banned, and
nothing would report it: the board would show a working part, the bot would just
never trade that symbol again. That is the same shape as the 214 phantom running
parts of 2026-08-26 and the fifty-six minute price of 2026-08-23, and it is why
the bound comes from a setting rather than being optional.

Absence is absence: an unrestricted symbol has *no* restriction rather than a
restriction saying nothing is wrong, so a consumer's "do I have one" is the
check that fires.
"""

from __future__ import annotations

from runtime.market_conditions import InstrumentRestriction
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "instrument-restriction-state"

PART_DECLARATION = PartDeclaration(
    part_id=PART_ID,
    consumes=("instrument-restriction-report",),
    produces=("instrument-restriction", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

NANOSECONDS_PER_SECOND = 1_000_000_000


class InstrumentRestrictionState:
    """Every source's live claim per symbol, each expiring on its own age.

    Claims are held keyed by source, so a poll restating what it said last time
    replaces that source's claim rather than adding a second copy of it. Sources
    and kinds are reported in sorted order for the same reason the symbol list
    is: `LevelPublisher` digests the whole level, and an order that wandered
    between ticks would republish an unchanged level while the skip counter
    claimed it had not.
    """

    def __init__(self, maximum_age_seconds: float) -> None:
        if not maximum_age_seconds > 0:
            raise ValueError(
                "maximum_age_seconds must be positive: it is how long a claim stands "
                f"after its source last said it, and NSE publishes no un-ban. Got "
                f"{maximum_age_seconds!r}."
            )
        self._maximum_age_ns = int(maximum_age_seconds * NANOSECONDS_PER_SECOND)
        self._claims: dict[str, dict[str, object]] = {}
        self._reports_seen = 0

    def observe(self, report) -> None:
        self._reports_seen += 1
        self._claims.setdefault(report.symbol, {})[report.source] = report

    def _live_claims(self, symbol: str, now_ns: int) -> tuple:
        oldest_believable_ns = now_ns - self._maximum_age_ns
        live = [
            report for report in self._claims.get(symbol, {}).values()
            if report.observed_at_ns >= oldest_believable_ns
        ]
        return tuple(sorted(live, key=lambda report: report.source))

    def restriction_for(self, symbol: str, now_ns: int) -> InstrumentRestriction | None:
        live = self._live_claims(symbol, now_ns)
        if not live:
            return None
        newest = max(live, key=lambda report: report.observed_at_ns)
        return InstrumentRestriction(
            symbol=symbol,
            kinds=tuple(report.kind for report in live),
            sources=tuple(report.source for report in live),
            stated_for=newest.stated_for,
            observed_at_ns=newest.observed_at_ns,
        )

    def restrictions(self, now_ns: int) -> tuple:
        found = (self.restriction_for(symbol, now_ns) for symbol in sorted(self._claims))
        return tuple(restriction for restriction in found if restriction is not None)

    @property
    def reports_seen(self) -> int:
        return self._reports_seen


def describe_state(state: InstrumentRestrictionState, now_ns: int) -> dict:
    return {
        "part_id": PART_ID,
        "reports_seen": state.reports_seen,
        "symbols_restricted": len(state.restrictions(now_ns)),
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    `without_observation_time` is not optional here. Every restriction carries
    `observed_at_ns`, so compared whole two statements of an unchanged level are
    never equal: nothing would ever be skipped, and the skip counter would read
    zero while the level publisher appeared to be working. That is exactly what
    the first version of level_publishing.py did to PartFault.
    """
    import time

    from runtime.input_assembly import Batch
    from runtime.level_publishing import LevelPublisher, without_observation_time

    reports = Batch(read=context.bus.reader("instrument-restriction-report"))
    state = InstrumentRestrictionState(
        maximum_age_seconds=context.number("instrument_restriction_maximum_age_seconds"),
    )
    publisher = LevelPublisher(
        publish=context.bus.publisher_for("instrument-restriction"),
        refresh_interval_seconds=context.number(
            "instrument_restriction_refresh_interval_seconds"
        ),
        identity_of=without_observation_time,
    )

    def tick() -> None:
        for report in reports.payloads():
            state.observe(report)
        publisher.publish_level(state.restrictions(time.time_ns()))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_state(state, time.time_ns()),
    )


__all__ = [
    "InstrumentRestrictionState",
    "PART_DECLARATION",
    "PART_ID",
    "describe_state",
    "start_part",
]
