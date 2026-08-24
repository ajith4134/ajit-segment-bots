"""capability-gap-finder: what this system cannot do, stated so it can be built.

A gap is only worth recording if it is nameable, evidenced and reachable. Most
statements about what a system lacks fail at least one of those -- "we need better
signals" names nothing, "the bots are underperforming" has no evidence attached to a
missing capability, and "we would do better with more capital" is not reachable from
here. A backlog full of those is indistinguishable from an empty one.

So a gap has to come from something measured, and there are three sources that
qualify:

- **A block that is entirely dark.** From the folded circuit view: a whole capability
  that does not exist, which is the strongest kind of gap because nothing has to be
  inferred.
- **A bot losing consistently on a nameable condition.** From the scorecards: not
  "the bear bot is bad" but "the bear bot loses on low-volatility reversals", which
  is a missing capability wearing the shape of a poor result.
- **A finding this system cannot test.** From research: a mechanism that needs data
  nothing here records is a gap in the data path, not a rejected idea.

**A gap that is not reachable is recorded with its blocker rather than dropped.** The
distinction matters: recording it means it stops being rediscovered every week, and
naming the blocker means that if the blocker ever clears, the gap is already written
down. Dropping it silently means the same discovery happens forever.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.autonomy_types import CapabilityGap
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "capability-gap-finder"

PART_DECLARATION = PartDeclaration(
    part_id="capability-gap-finder",
    consumes=("bot-scorecard", "research-finding", "part-health", "folded-circuit-map"),
    produces=("capability-gap", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FOUND = "found"
ALREADY_KNOWN = "already-recorded"
NOT_NAMEABLE = "it-does-not-name-a-capability"
NO_EVIDENCE = "nothing-measured-supports-it"
NOT_REACHABLE = "recorded-with-its-blocker-rather-than-dropped"

# Where a gap may come from. Anything else is an opinion about the system.
A_DARK_BLOCK = "a-whole-block-has-nothing-running"
A_LOSING_CONDITION = "a-bot-loses-consistently-on-a-nameable-condition"
AN_UNTESTABLE_FINDING = "a-finding-needs-data-this-system-does-not-record"

GAP_SOURCES = (A_DARK_BLOCK, A_LOSING_CONDITION, AN_UNTESTABLE_FINDING)

# Blockers that make a gap unreachable from here. Named so it is not rediscovered.
NEEDS_A_VENUE_ACCOUNT = "there-is-no-account-on-that-venue"
NEEDS_HARDWARE = "the-machine-cannot-run-it"
NEEDS_CAPITAL = "it-only-works-at-a-size-this-system-does-not-run"
NEEDS_A_HUMAN = "it-requires-a-decision-a-person-has-to-make"


@dataclass(frozen=True)
class GapOutcome:
    gap_id: str
    state: str
    gap: CapabilityGap | None
    source: str | None
    reason: str
    found_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.gap is not None


@dataclass
class FinderStanding:
    gaps_found: int = 0
    reachable_gaps: int = 0
    unreachable_gaps: int = 0
    duplicates: int = 0
    refused_not_nameable: int = 0
    refused_no_evidence: int = 0
    by_source: dict = field(default_factory=dict)


class CapabilityGapFinder:
    """Turns measured weakness into gaps that can be built, or names why they cannot."""

    def __init__(self, minimum_evidence: int, now_ns=time.time_ns) -> None:
        if minimum_evidence < 1:
            raise ValueError(
                "a gap with no evidence is an opinion about the system, and a backlog of "
                "those is indistinguishable from an empty one"
            )
        self._minimum_evidence = minimum_evidence
        self._now_ns = now_ns
        self._blockers: dict[str, str] = {}
        self._known: dict[str, CapabilityGap] = {}
        self._recorded_data_kinds: set = set()
        self.standing = FinderStanding()

    def declare_blocker(self, description: str, blocker: str) -> None:
        self._blockers[description] = blocker

    def declare_recorded_data(self, kinds) -> None:
        self._recorded_data_kinds = set(kinds)

    def from_a_dark_block(self, block: str, parts_in_block: int) -> GapOutcome:
        return self._record(
            gap_id=f"gap:dark:{block}",
            description=f"the {block} block has nothing running",
            evidence=(f"{parts_in_block} part(s) declared, none running",),
            blocks_what=f"everything that depends on {block}",
            would_be_a_new_part=False,
            source=A_DARK_BLOCK,
        )

    def from_a_losing_condition(
        self, bot: str, condition: str, trades: int, win_rate: float,
    ) -> GapOutcome:
        if not condition:
            self.standing.refused_not_nameable += 1
            return self._outcome(
                f"gap:losing:{bot}", NOT_NAMEABLE, None, A_LOSING_CONDITION,
                f"'{bot} is underperforming' names no capability. A gap has to say what "
                f"is missing, not that a result is bad",
            )
        return self._record(
            gap_id=f"gap:losing:{bot}:{condition}",
            description=f"{bot} loses on {condition}",
            evidence=(f"{trades} trade(s) at a {win_rate:.0%} win rate on {condition}",),
            blocks_what=f"{bot} trading {condition} profitably",
            would_be_a_new_part=True,
            source=A_LOSING_CONDITION,
        )

    def from_an_untestable_finding(self, finding_id: str, needs_data) -> GapOutcome:
        missing = tuple(sorted(set(needs_data) - self._recorded_data_kinds))
        if not missing:
            return self._outcome(
                f"gap:data:{finding_id}", NO_EVIDENCE, None, AN_UNTESTABLE_FINDING,
                "this finding needs only data the system already records, so it is a "
                "rejected idea rather than a gap",
            )
        return self._record(
            gap_id=f"gap:data:{','.join(missing)}",
            description=f"nothing records {', '.join(missing)}",
            evidence=(f"{finding_id} cannot be tested without it",),
            blocks_what="testing findings that depend on it",
            would_be_a_new_part=True,
            source=AN_UNTESTABLE_FINDING,
        )

    def _record(
        self, gap_id, description, evidence, blocks_what, would_be_a_new_part, source,
    ) -> GapOutcome:
        if gap_id in self._known:
            self.standing.duplicates += 1
            return self._outcome(
                gap_id, ALREADY_KNOWN, self._known[gap_id], source,
                "already recorded. Recording it once is what stops it being rediscovered "
                "every week",
            )

        if len(evidence) < self._minimum_evidence:
            self.standing.refused_no_evidence += 1
            return self._outcome(
                gap_id, NO_EVIDENCE, None, source,
                f"{len(evidence)} piece(s) of evidence, below the "
                f"{self._minimum_evidence} needed",
            )

        blocker = self._blockers.get(description)
        gap = CapabilityGap(
            gap_id=gap_id,
            description=description,
            evidence=tuple(evidence),
            blocks_what=blocks_what,
            would_be_a_new_part=would_be_a_new_part,
            is_reachable=blocker is None,
            blocked_by=blocker,
            found_at_ns=self._now_ns(),
        )
        self._known[gap_id] = gap
        self.standing.gaps_found += 1
        self.standing.by_source[source] = self.standing.by_source.get(source, 0) + 1
        if gap.is_reachable:
            self.standing.reachable_gaps += 1
        else:
            self.standing.unreachable_gaps += 1

        return self._outcome(
            gap_id, FOUND if gap.is_reachable else NOT_REACHABLE, gap, source,
            gap.description
            + (
                f", blocked by {blocker}. It is recorded rather than dropped: if the "
                f"blocker clears, the gap is already written down"
                if blocker
                else ". Reachable from here, so it is worth building"
            ),
        )

    def gaps(self) -> tuple:
        return tuple(self._known.values())

    def _outcome(self, gap_id, state, gap, source, reason) -> GapOutcome:
        return GapOutcome(
            gap_id=gap_id, state=state, gap=gap, source=source, reason=reason,
            found_at_ns=self._now_ns(),
        )


def describe_gap_finding(finder: CapabilityGapFinder) -> dict:
    return {
        "part_id": PART_ID,
        "gaps_found": finder.standing.gaps_found,
        "reachable_gaps": finder.standing.reachable_gaps,
        "unreachable_gaps": finder.standing.unreachable_gaps,
        "duplicates": finder.standing.duplicates,
        "refused_not_nameable": finder.standing.refused_not_nameable,
        "refused_no_evidence": finder.standing.refused_no_evidence,
        "by_source": dict(finder.standing.by_source),
        "gap_sources": list(GAP_SOURCES),
        "records_a_gap_without_evidence": False,
        "drops_unreachable_gaps": False,
    }


def run_capability_gap_finder(
    finder: CapabilityGapFinder, control_socket, read_signals, publish_gaps,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for job in read_signals(finder):
            outcome = job(finder)
            if outcome.is_usable:
                publish_gaps(outcome.gap)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_gap_finding(finder),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A dark block is read off the folded circuit map, with the block's own part
    count as the evidence. A losing condition is read off a bot's scorecard,
    per regime, once the regime has enough closed trades for the win rate to
    mean anything. A research finding carries whether it is testable here but
    not which data kind is missing, so an untestable finding cannot yet become
    a named data gap and is passed over rather than guessed at; part-health is
    consumed as the wake signal for the fold that follows it.
    """
    from runtime.input_assembly import Batch, LatestValue

    scorecards = Batch(read=context.bus.reader("bot-scorecard"))
    findings = Batch(read=context.bus.reader("research-finding"))
    health = Batch(read=context.bus.reader("part-health"))
    maps = LatestValue(read=context.bus.reader("folded-circuit-map"))
    publish_gaps = context.bus.publisher_for("capability-gap")

    finder = CapabilityGapFinder(
        minimum_evidence=int(context.number("gap_minimum_evidence"))
    )
    finder.declare_recorded_data(
        str(kind) for kind in context.setting("gap_recorded_data_kinds").value
    )
    losing_below = context.number("gap_losing_win_rate_below")
    minimum_trades = int(context.number("gap_losing_minimum_trades"))

    def read_signals(_finder):
        health.payloads()
        findings.payloads()
        jobs = []
        circuit = maps.value()
        if circuit is not None:
            for block in circuit.blocks_entirely_dark:
                parts_in_block = circuit.blocks.get(block, {}).get("parts", 0)
                jobs.append(
                    lambda f, b=block, n=parts_in_block: f.from_a_dark_block(b, n)
                )
        for scorecard in scorecards.payloads():
            for regime, record in scorecard.describe()["by_regime"].items():
                trades = record["trades"]
                if trades < minimum_trades:
                    continue
                win_rate = record["wins"] / trades
                if win_rate < losing_below:
                    jobs.append(
                        lambda f, bot=scorecard.bot, r=regime, t=trades, w=win_rate: (
                            f.from_a_losing_condition(bot, r, t, w)
                        )
                    )
        return tuple(jobs)

    return run_capability_gap_finder(
        finder=finder,
        control_socket=context.control_socket,
        read_signals=read_signals,
        publish_gaps=lambda gap: publish_gaps((gap,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
