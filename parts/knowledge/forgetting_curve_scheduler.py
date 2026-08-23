"""forgetting-curve-scheduler: how confident to be in a fact that has not been rechecked.

Facts do not stay true. A tick size changes, a symbol's typical spread doubles,
a venue's funding interval is revised -- and the system goes on believing all of
it, because nothing in a store expires. That is the failure this part addresses,
and it addresses it by lowering confidence rather than by deleting:

- **Confidence decays with time since last confirmation**, on a half-life. Not a
  cliff: a cliff makes every decision lurch on the day a fact crosses it, and the
  lurch has nothing to do with the market.
- **The half-life depends on what kind of fact it is.** A tick size changes
  rarely and a typical spread changes constantly, and one decay rate for both
  either forgets the stable facts too fast or trusts the volatile ones too long.
- **Confirmation resets it.** A fact rechecked today is as good as new whenever
  it was established, which is what makes rechecking worth doing.
- **A derived fact decays at least as fast as its weakest ancestor.** Otherwise a
  system that keeps recomputing a derived fact from a stale one grows more
  confident in it over time, which is precisely backwards.

**It never deletes.** Deleting is the pruner's decision and needs evidence of
uselessness, not merely of age -- an old fact that is still true is still true.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.knowledge_types import (
    CORRELATION_GROUP, FACT_KEYS, FUNDING_INTERVAL, LIQUIDATION_BEHAVIOUR, LISTING_AGE,
    NORMAL_MOVE, TICK_SIZE, TYPICAL_SPREAD, TYPICAL_VOLUME, VENUE_QUIRK,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "forgetting-curve-scheduler"

PART_DECLARATION = PartDeclaration(
    part_id="forgetting-curve-scheduler",
    consumes=("fact-provenance", "semantic-fact"),
    produces=("fact-confidence", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

SCHEDULED = "scheduled"
NEEDS_RECHECKING = "its-confidence-has-fallen-far-enough-to-be-worth-rechecking"
CANNOT_BE_RECHECKED = "its-source-is-gone-so-confirmation-is-not-available"
NO_PROVENANCE = "nothing-records-where-this-came-from"

# How long each kind of fact stays worth believing without confirmation, in
# seconds. One rate for all of them either forgets the stable facts too fast or
# trusts the volatile ones far too long.
HALF_LIFE_SECONDS = {
    TICK_SIZE: 365 * 86400.0,
    FUNDING_INTERVAL: 180 * 86400.0,
    LISTING_AGE: 365 * 86400.0,
    LIQUIDATION_BEHAVIOUR: 90 * 86400.0,
    VENUE_QUIRK: 90 * 86400.0,
    CORRELATION_GROUP: 30 * 86400.0,
    NORMAL_MOVE: 14 * 86400.0,
    TYPICAL_VOLUME: 7 * 86400.0,
    TYPICAL_SPREAD: 3 * 86400.0,
}


@dataclass(frozen=True)
class FactConfidence:
    """How much to believe one fact now, and whether it is worth rechecking."""

    fact_key: str
    kind: str
    state: str
    confidence: float
    initial_confidence: float
    half_life_seconds: float
    seconds_since_confirmed: float
    limited_by_ancestor: str | None
    reason: str
    scheduled_at_ns: int

    @property
    def should_be_rechecked(self) -> bool:
        return self.state == NEEDS_RECHECKING


@dataclass
class SchedulerStanding:
    facts_scheduled: int = 0
    needing_recheck: int = 0
    cannot_be_rechecked: int = 0
    without_provenance: int = 0
    limited_by_an_ancestor: int = 0
    lowest_confidence_seen: float | None = None
    by_kind: dict = field(default_factory=dict)


class ForgettingCurveScheduler:
    """Lowers confidence with age, per kind of fact, and never deletes."""

    def __init__(
        self,
        recheck_below: float,
        default_half_life_seconds: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < recheck_below < 1.0:
            raise ValueError(
                "the recheck threshold is a confidence and must be inside (0, 1); at zero "
                "nothing is ever rechecked and at one everything always is"
            )
        if default_half_life_seconds <= 0:
            raise ValueError("a half-life of zero forgets everything instantly")
        self._recheck_below = recheck_below
        self._default_half_life = default_half_life_seconds
        self._now_ns = now_ns
        self._provenance: dict[str, object] = {}
        self._kinds: dict[str, str] = {}
        self._initial: dict[str, float] = {}
        self.standing = SchedulerStanding()

    def observe_provenance(self, provenance) -> None:
        self._provenance[provenance.fact_key] = provenance

    def observe_fact(self, fact_key: str, kind: str, initial_confidence: float) -> None:
        self._kinds[fact_key] = kind
        self._initial[fact_key] = initial_confidence

    def half_life_for(self, kind: str) -> float:
        """How long this kind of fact stays worth believing without confirmation."""
        return HALF_LIFE_SECONDS.get(kind, self._default_half_life)

    def confidence_now(self, fact_key: str) -> FactConfidence:
        """One fact's confidence, decayed by how long since it was last confirmed."""
        self.standing.facts_scheduled += 1
        kind = self._kinds.get(fact_key, "unknown")
        initial = self._initial.get(fact_key, 1.0)
        half_life = self.half_life_for(kind)
        self.standing.by_kind[kind] = self.standing.by_kind.get(kind, 0) + 1

        provenance = self._provenance.get(fact_key)
        if provenance is None:
            self.standing.without_provenance += 1
            return self._confidence(
                fact_key, kind, NO_PROVENANCE, 0.0, initial, half_life, 0.0, None,
                "nothing records where this came from, so there is no last-confirmed time to "
                "decay from and no source to recheck against",
            )

        now = self._now_ns()
        last_confirmed = provenance.last_confirmed_at_ns or provenance.established_at_ns
        elapsed = max(0.0, (now - last_confirmed) / 1e9)

        # A half-life rather than a cliff: a cliff makes every decision lurch on
        # the day a fact crosses it, for reasons that have nothing to do with
        # the market.
        confidence = initial * 0.5 ** (elapsed / half_life)

        # A derived fact cannot be more confident than its weakest ancestor, or
        # recomputing it from a stale ancestor makes the system more confident
        # over time.
        limited_by = None
        weakest = getattr(provenance, "weakest_ancestor", None)
        if weakest is not None:
            ancestor = self.confidence_now(weakest) if weakest in self._kinds else None
            if ancestor is not None and ancestor.confidence < confidence:
                confidence = ancestor.confidence
                limited_by = weakest
                self.standing.limited_by_an_ancestor += 1

        if (
            self.standing.lowest_confidence_seen is None
            or confidence < self.standing.lowest_confidence_seen
        ):
            self.standing.lowest_confidence_seen = confidence

        if confidence < self._recheck_below:
            if not getattr(provenance, "source_still_reachable", True):
                self.standing.cannot_be_rechecked += 1
                state = CANNOT_BE_RECHECKED
            else:
                self.standing.needing_recheck += 1
                state = NEEDS_RECHECKING
        else:
            state = SCHEDULED

        return self._confidence(
            fact_key, kind, state, confidence, initial, half_life, elapsed, limited_by,
            f"{elapsed / 86400:.1f} day(s) since it was last confirmed, against a "
            f"{half_life / 86400:.0f}-day half-life for {kind}: confidence {confidence:.2f}"
            + (
                f", limited by its ancestor {limited_by} -- a derived fact cannot be more "
                f"confident than what it rests on, or recomputing it from a stale ancestor "
                f"makes this system more confident over time"
                if limited_by
                else ""
            )
            + (
                f". Below the {self._recheck_below:.2f} that makes rechecking worthwhile"
                if state == NEEDS_RECHECKING
                else ""
            )
            + (
                ". It has decayed past the recheck threshold and its source is gone, so "
                "confirmation is not available -- which is different from it being wrong"
                if state == CANNOT_BE_RECHECKED
                else ""
            )
            + ". Confidence is lowered, never deleted: an old fact that is still true is "
            "still true, and deleting is the pruner's decision",
        )

    def all_confidences(self) -> tuple:
        return tuple(self.confidence_now(fact_key) for fact_key in sorted(self._kinds))

    def _confidence(
        self, fact_key, kind, state, confidence, initial, half_life, elapsed, limited_by, reason
    ) -> FactConfidence:
        return FactConfidence(
            fact_key=fact_key,
            kind=kind,
            state=state,
            confidence=max(0.0, min(1.0, confidence)),
            initial_confidence=initial,
            half_life_seconds=half_life,
            seconds_since_confirmed=elapsed,
            limited_by_ancestor=limited_by,
            reason=reason,
            scheduled_at_ns=self._now_ns(),
        )


def describe_forgetting_curve(scheduler: ForgettingCurveScheduler) -> dict:
    return {
        "part_id": PART_ID,
        "facts_scheduled": scheduler.standing.facts_scheduled,
        "needing_recheck": scheduler.standing.needing_recheck,
        "cannot_be_rechecked": scheduler.standing.cannot_be_rechecked,
        "without_provenance": scheduler.standing.without_provenance,
        "limited_by_an_ancestor": scheduler.standing.limited_by_an_ancestor,
        "lowest_confidence_seen": scheduler.standing.lowest_confidence_seen,
        "by_kind": dict(sorted(scheduler.standing.by_kind.items())),
        "half_lives_days": {
            kind: seconds / 86400 for kind, seconds in sorted(HALF_LIFE_SECONDS.items())
        },
        "deletes_facts": False,
    }


def run_forgetting_curve_scheduler(
    scheduler: ForgettingCurveScheduler, control_socket, read_provenance, publish_confidences,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_provenance(scheduler)
        publish_confidences(scheduler.all_confidences())

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

    A fact's kind is its key, and its initial confidence is the one the
    store estimated. Confidences go out once per health interval.
    """
    import time as _time

    from runtime.input_assembly import Batch

    provenance = Batch(read=context.bus.reader("fact-provenance"))
    facts = Batch(read=context.bus.reader("semantic-fact"))
    publish_confidences = context.bus.publisher_for("fact-confidence")
    scheduler = ForgettingCurveScheduler(
        recheck_below=context.number("fact_recheck_below"),
        default_half_life_seconds=context.number("fact_default_half_life_seconds"),
    )
    last_publish = [float("-inf")]

    def read_provenance(_scheduler) -> None:
        for record in provenance.payloads():
            scheduler.observe_provenance(record)
        for fact in facts.payloads():
            scheduler.observe_fact(f"{fact.venue_id}:{fact.symbol}:{fact.key}", fact.key, float(fact.confidence.value))

    def publish(items) -> None:
        now = _time.monotonic()
        if now - last_publish[0] < context.health_interval_seconds:
            return
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_confidences(kept)
            last_publish[0] = now

    return run_forgetting_curve_scheduler(
        scheduler=scheduler,
        control_socket=context.control_socket,
        read_provenance=read_provenance,
        publish_confidences=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
