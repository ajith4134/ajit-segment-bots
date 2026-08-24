"""stop-placement-auditor: was the stop a risk control or a scheduled exit.

A stop placed inside a symbol's ordinary movement is not protection -- it is an
appointment to be taken out by noise. It is also the single most common avoidable
loss in a systematic system, because it looks disciplined: tight stops feel like
good risk management and produce a steady stream of small losses that never appear
as a defect.

The audit measures the stop's distance in units of the symbol's own typical movement
rather than in percent, because a distance that is generous for one instrument is
inside the spread for another. Four verdicts, each pointing at a different fix:

- **Inside the noise** -- normal movement reaches it. The fix is a wider stop or a
  smaller position, and it is checked by asking whether the price recovered after
  the stop was hit.
- **Too wide** -- a small loss was allowed to become a large one. The fix is the
  opposite, and confusing these two is how a system oscillates.
- **Well placed and hit** -- the stop did its job. This must be a possible verdict,
  or every stop that ever triggered is recorded as a mistake and the system stops
  using stops.
- **Never approached** -- the stop was never tested, so this trade says nothing
  about it. Recording that as approval is how an untested placement acquires a track
  record.

Whether the price recovered is measured from data after the exit, and is labelled as
hindsight: it says the stop was in the wrong place, not that holding would have been
the right decision.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.trade_decoding_types import StopAudit
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "stop-placement-auditor"

PART_DECLARATION = PartDeclaration(
    part_id="stop-placement-auditor",
    consumes=("closed-trade", "peak-excursion", "stop-target-plan", "market-data"),
    produces=("stop-audit", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

INSIDE_THE_NOISE = "inside-the-symbols-ordinary-movement"
TOO_WIDE = "wide-enough-to-turn-a-small-loss-into-a-large-one"
WELL_PLACED_AND_HIT = "well-placed-and-it-did-its-job"
WELL_PLACED_AND_NOT_HIT = "well-placed-and-never-tested-by-this-trade"
NEVER_APPROACHED = "the-price-never-came-near-it"
NO_STOP = "no-stop-was-placed"
NOT_MEASURABLE = "the-symbols-typical-movement-has-never-been-measured"


@dataclass(frozen=True)
class AuditOutcome:
    trade_id: str
    state: str
    audit: StopAudit | None
    reason: str
    audited_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.audit is not None


@dataclass
class AuditorStanding:
    stops_audited: int = 0
    inside_the_noise: int = 0
    too_wide: int = 0
    well_placed_and_hit: int = 0
    well_placed_and_not_hit: int = 0
    never_approached: int = 0
    trades_without_a_stop: int = 0
    unmeasurable: int = 0
    stopped_out_and_recovered: int = 0


class StopPlacementAuditor:
    """Measures stop distance in the symbol's own movement and names the fix."""

    def __init__(
        self,
        inside_the_noise_below: float,
        too_wide_above: float,
        approach_fraction: float,
        now_ns=time.time_ns,
    ) -> None:
        if inside_the_noise_below <= 0 or too_wide_above <= inside_the_noise_below:
            raise ValueError(
                "the two bounds are in typical movements and must leave a band between "
                "them where a stop is simply well placed; without that band every stop "
                "is a mistake and the system stops using stops"
            )
        if not 0.0 < approach_fraction <= 1.0:
            raise ValueError(
                "the approach fraction is how close the price must come for the stop to "
                "count as tested"
            )
        self._inside_below = inside_the_noise_below
        self._too_wide_above = too_wide_above
        self._approach_fraction = approach_fraction
        self._now_ns = now_ns
        self._typical_movement: dict[tuple, float] = {}
        self._stops: dict[str, float] = {}
        self._prices_after: dict[str, list] = {}
        self.standing = AuditorStanding()

    def observe_typical_movement(self, venue_id: str, symbol: str, movement: float) -> None:
        self._typical_movement[(venue_id, symbol)] = movement

    def observe_stop(self, trade_id: str, stop_price: float) -> None:
        self._stops[trade_id] = stop_price

    def observe_price_after_exit(self, trade_id: str, price: float) -> None:
        """Hindsight, and labelled as such: it says the stop was in the wrong place,
        not that holding would have been the right decision."""
        self._prices_after.setdefault(trade_id, []).append(price)

    def audit(self, trade_id: str, closed_trade, worst_price: float) -> AuditOutcome:
        self.standing.stops_audited += 1
        stop_price = self._stops.get(trade_id)
        if stop_price is None:
            self.standing.trades_without_a_stop += 1
            return self._outcome(
                trade_id, NO_STOP,
                self._audit(trade_id, None, None, None, None, False, None, NO_STOP,
                            False,
                            "no stop was placed, so the position's loss was bounded only "
                            "by the exit decision"),
                "no stop was placed",
            )

        typical = self._typical_movement.get((closed_trade.venue_id, closed_trade.symbol))
        if not typical or typical <= 0:
            self.standing.unmeasurable += 1
            return self._outcome(
                trade_id, NOT_MEASURABLE,
                self._audit(
                    trade_id, stop_price, None, None, None, False, None, NOT_MEASURABLE,
                    False,
                    "this symbol's typical movement has never been measured, and a stop "
                    "distance in percent means different things in different instruments",
                ),
                "no typical movement measured",
            )

        is_long = closed_trade.direction == "long"
        distance = abs(closed_trade.entry_price - stop_price)
        in_movements = distance / typical

        # Did the price come near enough for this trade to say anything about it?
        adverse_reach = abs(closed_trade.entry_price - worst_price)
        was_hit = (
            worst_price <= stop_price if is_long else worst_price >= stop_price
        )
        approached = adverse_reach >= distance * self._approach_fraction

        recovered = None
        if was_hit and trade_id in self._prices_after:
            after = self._prices_after[trade_id]
            recovered = (
                max(after) > closed_trade.entry_price
                if is_long
                else min(after) < closed_trade.entry_price
            )
            if recovered:
                self.standing.stopped_out_and_recovered += 1

        if in_movements < self._inside_below:
            verdict = INSIDE_THE_NOISE
            self.standing.inside_the_noise += 1
            reason = (
                f"{in_movements:.2f} typical movement(s) from entry, below the "
                f"{self._inside_below:.2f} floor. Normal movement reaches this: it is an "
                f"appointment to be taken out by noise rather than a risk control. The fix "
                f"is a wider stop or a smaller position"
                + (
                    ", and the price did recover afterwards, which says the stop was in "
                    "the wrong place -- not that holding would have been right"
                    if recovered
                    else ""
                )
            )
        elif in_movements > self._too_wide_above:
            verdict = TOO_WIDE
            self.standing.too_wide += 1
            reason = (
                f"{in_movements:.2f} typical movement(s) from entry, above the "
                f"{self._too_wide_above:.2f} ceiling. This lets a small loss become a "
                f"large one. The fix is the opposite of the previous case, and confusing "
                f"the two is how a system oscillates"
            )
        elif was_hit:
            verdict = WELL_PLACED_AND_HIT
            self.standing.well_placed_and_hit += 1
            reason = (
                f"{in_movements:.2f} typical movement(s) and it triggered. The stop did "
                f"its job: this must be a possible verdict, or every stop that ever "
                f"triggered is recorded as a mistake"
            )
        elif approached:
            verdict = WELL_PLACED_AND_NOT_HIT
            self.standing.well_placed_and_not_hit += 1
            reason = (
                f"{in_movements:.2f} typical movement(s), approached but not hit. The "
                f"placement was tested and held"
            )
        else:
            verdict = NEVER_APPROACHED
            self.standing.never_approached += 1
            reason = (
                f"the price never came within {self._approach_fraction:.0%} of it, so this "
                f"trade says nothing about the placement. Recording that as approval is "
                f"how an untested stop acquires a track record"
            )

        return self._outcome(
            trade_id, verdict,
            self._audit(
                trade_id, stop_price, distance, typical, in_movements, was_hit,
                recovered, verdict, verdict in (INSIDE_THE_NOISE, TOO_WIDE), reason,
            ),
            reason,
        )

    def _audit(
        self, trade_id, stop_price, distance, typical, in_movements, was_hit, recovered,
        verdict, is_a_defect, reason,
    ) -> StopAudit:
        return StopAudit(
            trade_id=trade_id, stop_price=stop_price, distance=distance,
            typical_movement=typical, distance_in_typical_movements=in_movements,
            was_hit=was_hit, would_have_recovered=recovered, verdict=verdict,
            is_measurable=typical is not None, reason=reason,
            audited_at_ns=self._now_ns(),
        )

    def _outcome(self, trade_id, state, audit, reason) -> AuditOutcome:
        return AuditOutcome(
            trade_id=trade_id, state=state, audit=audit, reason=reason,
            audited_at_ns=self._now_ns(),
        )


def describe_stop_auditing(auditor: StopPlacementAuditor) -> dict:
    return {
        "part_id": PART_ID,
        "stops_audited": auditor.standing.stops_audited,
        "inside_the_noise": auditor.standing.inside_the_noise,
        "too_wide": auditor.standing.too_wide,
        "well_placed_and_hit": auditor.standing.well_placed_and_hit,
        "well_placed_and_not_hit": auditor.standing.well_placed_and_not_hit,
        "never_approached": auditor.standing.never_approached,
        "trades_without_a_stop": auditor.standing.trades_without_a_stop,
        "unmeasurable": auditor.standing.unmeasurable,
        "stopped_out_and_the_price_recovered": (
            auditor.standing.stopped_out_and_recovered
        ),
        "measures_distance_in_percent": False,
        "treats_every_triggered_stop_as_a_mistake": False,
    }


def run_stop_placement_auditor(
    auditor: StopPlacementAuditor, control_socket, read_trades, publish_audits,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for trade_id, closed_trade, worst_price in read_trades():
            outcome = auditor.audit(trade_id, closed_trade, worst_price)
            if outcome.is_usable:
                publish_audits(outcome.audit)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_stop_auditing(auditor),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch, LatestByKey
    from runtime.trade_identity import closed_trade_id
    from runtime.venues.venue_adapter import NormalisedTrade

    closed = Batch(read=context.bus.reader("closed-trade"))
    excursions = LatestByKey(read=context.bus.reader("peak-excursion"), key_of=lambda e: (e.venue_id, e.symbol))
    plans = LatestByKey(read=context.bus.reader("stop-target-plan"), key_of=lambda p: (p.venue_id, p.symbol))
    trades = Batch(read=context.bus.reader("market-data"))
    publish_audits = context.bus.publisher_for("stop-audit")
    auditor = StopPlacementAuditor(
        inside_the_noise_below=context.number("stop_audit_inside_noise_below"),
        too_wide_above=context.number("stop_audit_too_wide_above"),
        approach_fraction=context.number("stop_audit_approach_fraction"),
    )
    last_price: dict[tuple[str, str], float] = {}
    awaiting_exit_price: dict[tuple[str, str], str] = {}

    def read_trades():
        for trade in trades.payloads():
            if not isinstance(trade, NormalisedTrade):
                continue
            key = (trade.venue_id, trade.symbol)
            previous = last_price.get(key)
            if previous:
                auditor.observe_typical_movement(trade.venue_id, trade.symbol, abs(trade.price / previous - 1.0))
            last_price[key] = trade.price
            trade_id = awaiting_exit_price.pop(key, None)
            if trade_id is not None:
                auditor.observe_price_after_exit(trade_id, trade.price)
        latest_plans = plans.mapping()
        latest_excursions = excursions.mapping()
        jobs = []
        for trade in closed.payloads():
            trade_id = closed_trade_id(trade)
            key = (trade.venue_id, trade.symbol)
            plan = latest_plans.get(key)
            if plan is not None:
                auditor.observe_stop(trade_id, plan.stop_price)
            record = latest_excursions.get(key)
            worst = record.worst_price if record is not None else trade.exit_price
            awaiting_exit_price[key] = trade_id
            jobs.append((trade_id, trade, worst))
        return tuple(jobs)

    return run_stop_placement_auditor(
        auditor=auditor,
        control_socket=context.control_socket,
        read_trades=read_trades,
        publish_audits=lambda audit: publish_audits((audit,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
