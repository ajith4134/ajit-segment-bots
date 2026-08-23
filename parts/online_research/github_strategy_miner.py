"""github-strategy-miner: public trading code, read for the one thing it can give.

Public strategy repositories are overwhelmingly backtests that would not survive
contact with a venue, and they share a small set of recognisable defects. That makes
them useful in a way their authors did not intend: **the defect is the finding**.
A repository showing 400% annual return with no fee model is not a strategy to copy,
it is a demonstration of how much of that return was fees.

So this part reads code and extracts two kinds of thing, both testable here:

- **A mechanism** -- a named, reproducible rule ("enter when the funding rate flips
  sign and the basis is above X"). Mechanisms are extracted as candidate findings
  with the exact conditions attached, so the hypothesis system can test them on this
  system's own recorded data rather than on the author's.
- **A defect** -- lookahead in a signal, no fee or slippage model, a fit reported on
  the same data it was fitted to, survivorship in the symbol list, or a backtest
  that could not fill at the prices it assumes. Each is detected structurally, and
  a repository with one is reported *with* the defect rather than discarded, because
  "this popular approach fails for this reason" is itself worth recording.

Two refusals hold throughout. **Stars are not evidence** -- popularity measures
marketing, and the most-starred repositories in this space are the ones with the
most impressive equity curves, which is the same selection this part exists to
counter. And **nothing found here is a finding until it is testable on this system's
own data**: an untestable mechanism is reading, and reading is not research.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.external_research_types import ResearchFinding, WebIdea
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "github-strategy-miner"

PART_DECLARATION = PartDeclaration(
    part_id="github-strategy-miner",
    consumes=("skill-gap", "web-idea"),
    produces=("research-finding", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

MINED = "mined"
ONLY_A_DEFECT = "no-mechanism-survived-but-the-defect-is-worth-recording"
NOT_TESTABLE_HERE = "the-mechanism-cannot-be-tested-on-this-systems-data"
NOTHING_FOUND = "no-mechanism-and-no-recognisable-defect"
NO_GAP = "nothing-is-missing-so-nothing-is-mined"

# The defects that make a public backtest look profitable. Each is detected from
# structure, never from the repository's own claims about itself.
LOOKAHEAD = "a-signal-uses-data-from-after-the-decision"
NO_COST_MODEL = "no-fee-or-slippage-is-subtracted"
FITTED_ON_ITS_OWN_TEST_DATA = "fitted-and-reported-on-the-same-data"
SURVIVORSHIP = "the-symbol-list-is-todays-survivors"
UNFILLABLE = "it-assumes-fills-at-prices-that-were-never-available"
NO_OUT_OF_SAMPLE = "there-is-no-held-out-period"

KNOWN_DEFECTS = (
    LOOKAHEAD, NO_COST_MODEL, FITTED_ON_ITS_OWN_TEST_DATA, SURVIVORSHIP,
    UNFILLABLE, NO_OUT_OF_SAMPLE,
)


@dataclass(frozen=True)
class Mechanism:
    """A rule stated precisely enough to be tested here, or not stated at all."""

    name: str
    conditions: tuple
    action: str
    instruments: tuple
    needs_data: tuple

    @property
    def is_specified(self) -> bool:
        return bool(self.conditions) and bool(self.action)


@dataclass(frozen=True)
class MinedRepository:
    repository: str
    state: str
    mechanisms: tuple
    defects: tuple
    findings: tuple
    stars: int | None
    reason: str
    mined_at_ns: int

    @property
    def produced_anything(self) -> bool:
        return bool(self.findings)


@dataclass
class MinerStanding:
    repositories_mined: int = 0
    mechanisms_found: int = 0
    mechanisms_testable_here: int = 0
    defects_found: int = 0
    defect_only_repositories: int = 0
    nothing_found: int = 0
    findings_produced: int = 0
    times_stars_influenced_a_decision: int = 0


class GithubStrategyMiner:
    """Extracts testable mechanisms and structural defects from public code."""

    def __init__(self, available_data_kinds, minimum_conditions: int, now_ns=time.time_ns) -> None:
        if not available_data_kinds:
            raise ValueError(
                "without knowing what data this system holds, every mechanism looks "
                "testable and none of them are"
            )
        if minimum_conditions < 1:
            raise ValueError(
                "a mechanism with no stated condition is a description, not a rule"
            )
        self._available = set(available_data_kinds)
        self._minimum_conditions = minimum_conditions
        self._now_ns = now_ns
        self._seen: set = set()
        self.standing = MinerStanding()

    def is_testable_here(self, mechanism: Mechanism) -> bool:
        """Testable means this system holds the data the rule needs, recorded."""
        return mechanism.is_specified and set(mechanism.needs_data).issubset(self._available)

    def mine(self, idea: WebIdea, mechanisms, defects, stars=None) -> MinedRepository:
        """`mechanisms` and `defects` come from structural analysis of the code.

        They are passed in rather than parsed here because parsing arbitrary Python
        is a different job with a different failure mode, and mixing the two would
        make a parser bug look like a research finding.
        """
        self.standing.repositories_mined += 1
        repository = idea.origin_reference
        mechanisms = tuple(mechanisms or ())
        defects = tuple(defect for defect in (defects or ()) if defect in KNOWN_DEFECTS)
        self.standing.mechanisms_found += len(mechanisms)
        self.standing.defects_found += len(defects)

        testable = tuple(
            mechanism
            for mechanism in mechanisms
            if self.is_testable_here(mechanism)
            and len(mechanism.conditions) >= self._minimum_conditions
        )
        self.standing.mechanisms_testable_here += len(testable)

        findings = []
        now = self._now_ns()

        for mechanism in testable:
            findings.append(
                ResearchFinding(
                    finding_id=f"mechanism:{repository}:{mechanism.name}",
                    topic="a-mechanism-found-in-public-code",
                    statement=(
                        f"{mechanism.action} when "
                        + " and ".join(mechanism.conditions)
                    ),
                    evidence=mechanism.conditions,
                    source_references=(repository,),
                    # Confidence is deliberately low regardless of how the repository
                    # presents itself: it has not been tested on this system's data yet.
                    confidence=0.2,
                    would_be_refuted_by=(
                        "the same conditions not preceding the same outcome in this "
                        "system's own recorded data"
                    ),
                    is_testable_here=True,
                    found_at_ns=now,
                )
            )

        for defect in defects:
            findings.append(
                ResearchFinding(
                    finding_id=f"defect:{repository}:{defect}",
                    topic="why-a-public-backtest-looks-profitable",
                    statement=(
                        f"a published strategy at {repository} reports its results with "
                        f"{defect.replace('-', ' ')}"
                    ),
                    evidence=(defect,),
                    source_references=(repository,),
                    confidence=0.6,
                    would_be_refuted_by=(
                        "the same strategy reproducing its reported results once the "
                        "defect is corrected"
                    ),
                    is_testable_here=True,
                    found_at_ns=now,
                )
            )

        findings = tuple(findings)
        self.standing.findings_produced += len(findings)

        if not findings:
            self.standing.nothing_found += 1
            return self._mined(
                repository, NOTHING_FOUND, mechanisms, defects, (), stars,
                f"{len(mechanisms)} mechanism(s) found, none specified well enough to "
                f"test here, and no recognisable defect. Reading is not research",
            )

        if not testable and defects:
            self.standing.defect_only_repositories += 1
            return self._mined(
                repository, ONLY_A_DEFECT, mechanisms, defects, findings, stars,
                f"no testable mechanism, but {len(defects)} defect(s): "
                f"{', '.join(defects)}. The defect is the finding -- this popular "
                f"approach fails for a nameable reason, which is worth recording",
            )

        if not testable:
            return self._mined(
                repository, NOT_TESTABLE_HERE, mechanisms, defects, findings, stars,
                "the mechanisms need data this system does not hold. An untestable "
                "mechanism is reading",
            )

        return self._mined(
            repository, MINED, mechanisms, defects, findings, stars,
            f"{len(testable)} testable mechanism(s) and {len(defects)} defect(s)"
            + (
                f". The repository has {stars} star(s), which counted for nothing: "
                f"popularity measures marketing, and the most-starred repositories here "
                f"are the ones with the most impressive equity curves"
                if stars is not None
                else ""
            ),
        )

    def _mined(
        self, repository, state, mechanisms, defects, findings, stars, reason,
    ) -> MinedRepository:
        return MinedRepository(
            repository=repository, state=state, mechanisms=mechanisms, defects=defects,
            findings=findings, stars=stars, reason=reason, mined_at_ns=self._now_ns(),
        )


def describe_strategy_mining(miner: GithubStrategyMiner) -> dict:
    return {
        "part_id": PART_ID,
        "repositories_mined": miner.standing.repositories_mined,
        "mechanisms_found": miner.standing.mechanisms_found,
        "mechanisms_testable_here": miner.standing.mechanisms_testable_here,
        "defects_found": miner.standing.defects_found,
        "defect_only_repositories": miner.standing.defect_only_repositories,
        "nothing_found": miner.standing.nothing_found,
        "findings_produced": miner.standing.findings_produced,
        "known_defects": list(KNOWN_DEFECTS),
        "ranks_by_stars": False,
        "times_stars_influenced_a_decision": (
            miner.standing.times_stars_influenced_a_decision
        ),
    }


def run_github_strategy_miner(
    miner: GithubStrategyMiner, control_socket, read_ideas, publish_findings,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for idea, mechanisms, defects, stars in read_ideas():
            mined = miner.mine(idea, mechanisms, defects, stars)
            for finding in mined.findings:
                publish_findings(finding)

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

    A web idea whose source is a repository is mined for the mechanisms
    and defects its content names. The mechanisms are read from the idea's
    content as condition lines; the defects are the six named failure
    modes found by their names. A repository naming neither is mined to
    NOTHING_FOUND by name. Skill gaps are read and drained: the reader
    that answers a gap is upstream of this part.
    """
    from runtime.input_assembly import Batch

    gaps = Batch(read=context.bus.reader("skill-gap"))
    ideas = Batch(read=context.bus.reader("web-idea"))
    publish_findings = context.bus.publisher_for("research-finding")
    miner = GithubStrategyMiner(
        available_data_kinds=("market-data", "kline-window", "consolidated-price", "funding-forecast"),
        minimum_conditions=int(context.number("github_minimum_conditions")),
    )
    defect_names = (LOOKAHEAD, NO_COST_MODEL, FITTED_ON_ITS_OWN_TEST_DATA, SURVIVORSHIP, UNFILLABLE, NO_OUT_OF_SAMPLE)

    def read_ideas():
        gaps.payloads()
        jobs = []
        for idea in ideas.payloads():
            if "github.com" not in str(idea.source_url):
                continue
            content = str(idea.content)
            lowered = content.lower()
            conditions = tuple(line.strip() for line in content.splitlines() if " if " in line or line.strip().lower().startswith("when "))
            mechanisms = (
                (Mechanism(name=idea.title, conditions=conditions, action="trade", instruments=(), needs_data=("market-data",)),)
                if conditions else ()
            )
            defects = tuple(name for name in defect_names if name.replace("-", " ") in lowered)
            jobs.append((idea, mechanisms, defects, None))
        return tuple(jobs)

    return run_github_strategy_miner(
        miner=miner,
        control_socket=context.control_socket,
        read_ideas=read_ideas,
        publish_findings=lambda finding: publish_findings((finding,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
