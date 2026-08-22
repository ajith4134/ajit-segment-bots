"""skill-index: which skills exist, which are usable, and which are contradicted.

A pile of skills with nothing to arbitrate between them is worse than no skills:
whichever one is looked at first becomes the answer, and two skills contradicting
each other are indistinguishable from one skill with an ambiguity.

So the index holds every skill and its standing, and a skill is available only
when every one of these holds:

- **It survived conflict detection.** A skill contradicting another one, with
  neither backtested, is a coin flip in the shape of advice.
- **It is not stale.** A source about a market structure that has changed is
  worse than nothing, because it is confidently wrong.
- **It has been backtested against real episodes**, or it is marked as untested.
  A skill nobody has tested is a book, and a book is a claim.
- **Its provenance is recorded.** A skill whose source cannot be named cannot be
  rechecked when it turns out to be wrong.

**The index does not rank skills against each other.** It says which are usable
and why; choosing among the usable ones is the loader's problem, informed by what
the question is.

**A superseded version stays in the index, marked.** Removing it makes the same
source look new the next time it is fetched, and loses the reason the older
version was replaced.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "skill-index"

PART_DECLARATION = PartDeclaration(
    part_id="skill-index",
    consumes=(
        "skill", "skill-conflict", "stale-knowledge", "skill-backtest", "skill-provenance",
        "skill-version",
    ),
    produces=("available-skill", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

AVAILABLE = "available"
CONTRADICTED = "it-contradicts-another-skill-and-neither-is-tested"
STALE = "its-source-is-about-a-market-that-has-changed"
UNTESTED = "nothing-has-tested-it-against-real-episodes"
NO_PROVENANCE = "its-source-cannot-be-named"
SUPERSEDED = "a-newer-version-of-this-skill-exists"


@dataclass(frozen=True)
class AvailableSkill:
    """One skill and its standing, with every reason it is or is not usable."""

    skill_id: str
    title: str
    version: str
    state: str
    sections: tuple
    decision_rules: int
    anti_patterns: int
    backtest_score: float | None
    conflicts_with: tuple
    source_reference: str | None
    reason: str
    indexed_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == AVAILABLE

    @property
    def is_tested(self) -> bool:
        return self.backtest_score is not None


@dataclass
class IndexStanding:
    skills_indexed: int = 0
    available: int = 0
    contradicted: int = 0
    stale: int = 0
    untested: int = 0
    without_provenance: int = 0
    superseded: int = 0
    by_state: dict = field(default_factory=dict)


class SkillIndex:
    """Holds every skill with its standing, and does not rank them against each other."""

    def __init__(self, minimum_backtest_score: float, now_ns=time.time_ns) -> None:
        if not 0.0 <= minimum_backtest_score <= 1.0:
            raise ValueError("the backtest bar is a score in [0, 1]")
        self._minimum_backtest = minimum_backtest_score
        self._now_ns = now_ns
        self._skills: dict[str, object] = {}
        self._versions: dict[str, str] = {}
        self._conflicts: dict[str, set] = {}
        self._stale: set[str] = set()
        self._backtests: dict[str, float] = {}
        self._provenance: dict[str, str] = {}
        self.standing = IndexStanding()

    def observe_skill(self, skill) -> None:
        self._skills[skill.skill_id] = skill
        current = self._versions.get(skill.skill_id)
        if current is None or skill.version > current:
            self._versions[skill.skill_id] = skill.version

    def observe_conflict(self, skill_id: str, conflicts_with: str) -> None:
        self._conflicts.setdefault(skill_id, set()).add(conflicts_with)
        self._conflicts.setdefault(conflicts_with, set()).add(skill_id)

    def observe_stale(self, skill_id: str, is_stale: bool) -> None:
        if is_stale:
            self._stale.add(skill_id)
        else:
            self._stale.discard(skill_id)

    def observe_backtest(self, skill_id: str, score: float) -> None:
        """A skill nobody has tested is a book, and a book is a claim."""
        self._backtests[skill_id] = score

    def observe_provenance(self, skill_id: str, source_reference: str) -> None:
        self._provenance[skill_id] = source_reference

    def standing_of(self, skill_id: str) -> AvailableSkill:
        """One skill's standing, with every condition checked."""
        skill = self._skills.get(skill_id)
        if skill is None:
            return self._entry(
                skill_id, "", "", NO_PROVENANCE, (), 0, 0, None, (), None,
                "nothing with this identifier has been indexed",
            )

        conflicts = tuple(sorted(self._conflicts.get(skill_id, ())))
        backtest = self._backtests.get(skill_id)
        provenance = self._provenance.get(skill_id) or skill.source_reference
        latest = self._versions.get(skill_id)

        state = AVAILABLE
        reason_parts = []

        if latest is not None and skill.version != latest:
            state = SUPERSEDED
            reason_parts.append(
                f"version {skill.version} is superseded by {latest}, and it stays indexed so "
                f"the same source does not look new the next time it is fetched"
            )
        elif provenance is None:
            state = NO_PROVENANCE
            reason_parts.append(
                "its source cannot be named, so it cannot be rechecked when it turns out wrong"
            )
        elif skill_id in self._stale:
            state = STALE
            reason_parts.append(
                "its source is about a market structure that has changed, which is worse than "
                "nothing because it is confidently wrong"
            )
        elif conflicts and (backtest is None or backtest < self._minimum_backtest):
            state = CONTRADICTED
            reason_parts.append(
                f"it contradicts {', '.join(conflicts)} and is not backtested above "
                f"{self._minimum_backtest:.2f}; two skills contradicting each other with "
                f"neither tested is a coin flip in the shape of advice"
            )
        elif backtest is None:
            state = UNTESTED
            reason_parts.append(
                "nothing has tested it against real episodes, so it is a book, and a book is "
                "a claim"
            )
        else:
            reason_parts.append(
                f"backtested at {backtest:.2f} over real episodes, no unresolved conflicts, "
                f"provenance recorded, and it is the current version"
            )

        return self._entry(
            skill_id, skill.title, skill.version, state, tuple(sorted(skill.sections)),
            len(skill.decision_rules), len(skill.anti_patterns), backtest, conflicts,
            provenance, "; ".join(reason_parts),
        )

    def available_skills(self) -> tuple:
        """Every usable skill. Not ranked -- choosing among them is the loader's problem."""
        entries = tuple(
            self.standing_of(skill_id) for skill_id in sorted(self._skills)
        )
        self.standing.skills_indexed = len(entries)
        for entry in entries:
            self.standing.by_state[entry.state] = self.standing.by_state.get(entry.state, 0) + 1
        self.standing.available = sum(1 for entry in entries if entry.is_usable)
        self.standing.contradicted = sum(1 for entry in entries if entry.state == CONTRADICTED)
        self.standing.stale = sum(1 for entry in entries if entry.state == STALE)
        self.standing.untested = sum(1 for entry in entries if entry.state == UNTESTED)
        self.standing.without_provenance = sum(
            1 for entry in entries if entry.state == NO_PROVENANCE
        )
        self.standing.superseded = sum(1 for entry in entries if entry.state == SUPERSEDED)
        return entries

    def _entry(
        self, skill_id, title, version, state, sections, rules, anti_patterns,
        backtest, conflicts, provenance, reason,
    ) -> AvailableSkill:
        return AvailableSkill(
            skill_id=skill_id,
            title=title,
            version=version,
            state=state,
            sections=sections,
            decision_rules=rules,
            anti_patterns=anti_patterns,
            backtest_score=backtest,
            conflicts_with=conflicts,
            source_reference=provenance,
            reason=reason,
            indexed_at_ns=self._now_ns(),
        )


def describe_index(index: SkillIndex) -> dict:
    index.available_skills()
    return {
        "part_id": PART_ID,
        "skills_indexed": index.standing.skills_indexed,
        "available": index.standing.available,
        "contradicted": index.standing.contradicted,
        "stale": index.standing.stale,
        "untested": index.standing.untested,
        "without_provenance": index.standing.without_provenance,
        "superseded": index.standing.superseded,
        "by_state": dict(sorted(index.standing.by_state.items())),
        "ranks_skills": False,
    }


def run_skill_index(
    index: SkillIndex, control_socket, read_skills, publish_available,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        read_skills(index)
        publish_available(index.available_skills())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
