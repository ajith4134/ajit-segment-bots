"""ground-truth-snapshot-builder: the numbers a model is given, never asked for.

This part is the boundary between what is known and what is generated. Every fact a
prompt may contain comes from here, and every number in an answer is matched back to
here. That makes its correctness properties unusually strict, because a wrong
snapshot does not produce a wrong answer -- it produces a *checkable* wrong answer,
which passes every downstream check.

Four rules:

- **A snapshot is complete or it is not a snapshot.** A prompt built on three of the
  four facts it declared produces an answer that reads exactly like one built on
  four, so a missing fact makes the whole snapshot unusable and names what is
  missing.
- **Every fact carries the moment it was measured, and the snapshot carries the
  oldest of them.** A snapshot is only as fresh as its stalest member; averaging
  ages, or stamping the assembly time, makes a four-minute-old price look current.
- **Derived facts are computed here, from measured ones, never fetched separately.**
  A mid price computed from a bid and ask that are themselves in the snapshot is
  consistent with them; one fetched independently can contradict them, and the model
  will faithfully reason over the contradiction.
- **Nothing is filled in.** A fact that could not be measured is absent and named,
  because a plausible substitute is the one error nothing downstream can catch.

The snapshot is immutable once built. Anything that changes needs a new snapshot,
which is what makes an answer traceable to the exact state of the market it was
reasoning about.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.llm_types import VerifiedSnapshot
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "ground-truth-snapshot-builder"

PART_DECLARATION = PartDeclaration(
    part_id="ground-truth-snapshot-builder",
    consumes=("market-data",),
    produces=("verified-snapshot", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

BUILT = "built"
INCOMPLETE = "a-declared-fact-could-not-be-measured"
TOO_STALE = "the-oldest-fact-in-it-is-too-old"
NOTHING_MEASURED = "no-market-data-has-arrived-for-this-symbol"
INCONSISTENT = "two-measured-facts-contradict-each-other"

# The facts a snapshot may contain, and which are derived rather than measured.
BID = "bid"
ASK = "ask"
MID = "mid"
SPREAD = "spread"
LAST_TRADE = "last-trade"
FUNDING_RATE = "funding-rate"
OPEN_INTEREST = "open-interest"
TICK_SIZE = "tick-size"

DERIVED_FACTS = {
    MID: (BID, ASK),
    SPREAD: (BID, ASK),
}


@dataclass(frozen=True)
class SnapshotOutcome:
    venue_id: str
    symbol: str
    state: str
    snapshot: VerifiedSnapshot | None
    missing: tuple
    oldest_fact: str | None
    staleness_seconds: float | None
    reason: str
    built_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == BUILT and self.snapshot is not None


@dataclass
class BuilderStanding:
    snapshots_built: int = 0
    refused_incomplete: int = 0
    refused_stale: int = 0
    refused_inconsistent: int = 0
    refused_nothing_measured: int = 0
    facts_derived: int = 0
    facts_filled_in: int = 0


class GroundTruthSnapshotBuilder:
    """Freezes measured market facts, complete and stamped, or refuses to."""

    def __init__(self, maximum_staleness_seconds: float, now_ns=time.time_ns) -> None:
        if maximum_staleness_seconds <= 0:
            raise ValueError(
                "a snapshot with no age limit lets a four-minute-old price be reasoned "
                "over as current"
            )
        self._maximum_staleness = maximum_staleness_seconds
        self._now_ns = now_ns
        self._facts: dict[tuple, dict] = {}
        self._sequence = 0
        self.standing = BuilderStanding()

    def observe_fact(
        self, venue_id: str, symbol: str, name: str, value: float, measured_at_ns: int,
    ) -> None:
        if name in DERIVED_FACTS:
            raise ValueError(
                f"{name} is derived from {', '.join(DERIVED_FACTS[name])} and is computed "
                f"here. A separately fetched {name} can contradict the facts it is "
                f"supposed to summarise, and the model would reason over the contradiction"
            )
        self._facts.setdefault((venue_id, symbol), {})[name] = (value, measured_at_ns)

    def build(self, venue_id: str, symbol: str, required_facts) -> SnapshotOutcome:
        required = tuple(required_facts)
        measured = self._facts.get((venue_id, symbol), {})

        if not measured:
            self.standing.refused_nothing_measured += 1
            return self._outcome(
                venue_id, symbol, NOTHING_MEASURED, None, required, None, None,
                "no market data has arrived for this symbol. Nothing is filled in: a "
                "plausible substitute is the one error nothing downstream can catch",
            )

        facts: dict = {}
        times: dict = {}
        missing = []
        for name in required:
            if name in DERIVED_FACTS:
                sources = DERIVED_FACTS[name]
                if not all(source in measured for source in sources):
                    missing.append(name)
                    continue
                bid, bid_at = measured[BID]
                ask, ask_at = measured[ASK]
                if ask < bid:
                    self.standing.refused_inconsistent += 1
                    return self._outcome(
                        venue_id, symbol, INCONSISTENT, None, (), None, None,
                        f"the ask ({ask}) is below the bid ({bid}). Two measured facts "
                        f"contradict each other, and a model given both would reason over "
                        f"the contradiction rather than notice it",
                    )
                facts[name] = (bid + ask) / 2.0 if name == MID else ask - bid
                times[name] = min(bid_at, ask_at)
                self.standing.facts_derived += 1
                continue

            if name not in measured:
                missing.append(name)
                continue
            value, measured_at = measured[name]
            facts[name] = value
            times[name] = measured_at

        if missing:
            self.standing.refused_incomplete += 1
            return self._outcome(
                venue_id, symbol, INCOMPLETE, None, tuple(sorted(missing)), None, None,
                f"{', '.join(sorted(missing))} could not be measured. A prompt built on "
                f"three of four declared facts produces an answer that reads exactly like "
                f"one built on four",
            )

        oldest_fact = min(times, key=lambda name: times[name])
        # A snapshot is only as fresh as its stalest member; averaging ages or
        # stamping the assembly time hides exactly the fact that has gone bad.
        staleness = (self._now_ns() - times[oldest_fact]) / 1e9

        if staleness > self._maximum_staleness:
            self.standing.refused_stale += 1
            return self._outcome(
                venue_id, symbol, TOO_STALE, None, (), oldest_fact, staleness,
                f"{oldest_fact} was measured {staleness:.1f}s ago, past the "
                f"{self._maximum_staleness:.1f}s limit. The snapshot is only as fresh as "
                f"its stalest member",
            )

        self._sequence += 1
        snapshot = VerifiedSnapshot(
            snapshot_id=f"snap-{self._sequence}",
            venue_id=venue_id,
            symbol=symbol,
            facts=facts,
            measured_at_ns=times[oldest_fact],
            staleness_seconds=staleness,
            is_complete=True,
            missing_facts=(),
        )
        self.standing.snapshots_built += 1
        return self._outcome(
            venue_id, symbol, BUILT, snapshot, (), oldest_fact, staleness,
            f"{len(facts)} fact(s), oldest {oldest_fact} at {staleness:.2f}s. Immutable: "
            f"anything that changes needs a new snapshot, which is what makes an answer "
            f"traceable to the market state it reasoned about",
        )

    def _outcome(
        self, venue_id, symbol, state, snapshot, missing, oldest, staleness, reason,
    ) -> SnapshotOutcome:
        return SnapshotOutcome(
            venue_id=venue_id, symbol=symbol, state=state, snapshot=snapshot,
            missing=missing, oldest_fact=oldest, staleness_seconds=staleness,
            reason=reason, built_at_ns=self._now_ns(),
        )


def describe_snapshot_building(builder: GroundTruthSnapshotBuilder) -> dict:
    return {
        "part_id": PART_ID,
        "snapshots_built": builder.standing.snapshots_built,
        "refused_incomplete": builder.standing.refused_incomplete,
        "refused_stale": builder.standing.refused_stale,
        "refused_inconsistent": builder.standing.refused_inconsistent,
        "refused_nothing_measured": builder.standing.refused_nothing_measured,
        "facts_derived_from_measured_ones": builder.standing.facts_derived,
        "facts_filled_in": builder.standing.facts_filled_in,
        "derived_facts": {name: list(sources) for name, sources in DERIVED_FACTS.items()},
        "fills_a_missing_fact": False,
        "stamps_the_assembly_time_instead_of_the_measurement_time": False,
    }


def run_ground_truth_snapshot_builder(
    builder: GroundTruthSnapshotBuilder, control_socket, read_requests,
    publish_snapshots, health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        for venue_id, symbol, required in read_requests(builder):
            outcome = builder.build(venue_id, symbol, required)
            if outcome.is_usable:
                publish_snapshots(outcome.snapshot)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
