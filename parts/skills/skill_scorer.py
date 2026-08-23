"""skill-scorer: which skills are actually worth their space.

A backtest says a skill would have helped. This says whether it did -- on the
decisions that actually loaded it, which is a different and harder question,
because loading a section changes what the reader attends to whether or not the
section was right.

- **Scored per section, not per skill.** A skill whose risk section is excellent
  and whose entry section is noise scores mediocre overall, and the useful
  action is to keep one section and drop the other. Per-skill scoring cannot
  express that.
- **Against decisions the section was loaded into**, matched by identifier. A
  section credited for every trade in the period would be credited for the
  market, which is the flattering measurement.
- **Its cost is counted.** A section that helps slightly and costs a third of
  the budget is displacing something better, and usefulness that ignores cost
  always recommends loading everything.
- **A section never loaded is unscored, not useless.** They look identical in a
  usefulness ranking and mean opposite things: one has been tried, and the other
  is waiting.

**The scorer does not remove anything.** Removal is the pruner's, and a scorer
that could remove would optimise its own metric by deleting whatever it could
not measure.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "skill-scorer"

PART_DECLARATION = PartDeclaration(
    part_id="skill-scorer",
    consumes=("loaded-skill-section", "trade-episode", "skill-version"),
    produces=("skill-usefulness", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

USEFUL = "useful"
NOT_USEFUL = "measured-and-not-useful"
NEVER_LOADED = "never-loaded-so-unscored-rather-than-useless"
TOO_FEW_DECISIONS = "too-few-decisions-loaded-it-to-say"


@dataclass(frozen=True)
class SkillUsefulness:
    """Whether one section earned its space, per version."""

    skill_id: str
    section: str
    version: str
    state: str
    usefulness: float | None
    hit_rate: Estimate
    decisions_loaded_into: int
    characters_per_load: float
    usefulness_per_character: float | None
    reason: str
    scored_at_ns: int

    @property
    def is_measured(self) -> bool:
        return self.state in (USEFUL, NOT_USEFUL)

    @property
    def earns_its_space(self) -> bool:
        return self.state == USEFUL


@dataclass
class ScorerStanding:
    sections_scored: int = 0
    useful: int = 0
    not_useful: int = 0
    never_loaded: int = 0
    too_few_decisions: int = 0
    decisions_recorded: int = 0
    characters_loaded: int = 0
    by_skill: dict = field(default_factory=dict)


class SkillScorer:
    """Scores each section on the decisions it was actually loaded into, against its cost."""

    def __init__(
        self,
        minimum_decisions: int,
        usefulness_threshold: float,
        base_rate: float,
        prior_weight: float,
        half_life_observations: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < usefulness_threshold < 1.0:
            raise ValueError("the usefulness bar is a rate inside (0, 1)")
        self._minimum_decisions = minimum_decisions
        self._threshold = usefulness_threshold
        self._base_rate = base_rate
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._now_ns = now_ns
        self._loads: dict[tuple[str, str, str], list] = {}
        self._outcomes: dict[tuple[str, str, str], RateEstimator] = {}
        self._decision_loads: dict[str, list] = {}
        self._known_sections: set = set()
        self.standing = ScorerStanding()

    def observe_section_exists(self, skill_id: str, section: str, version: str) -> None:
        """A section that exists but has never been loaded is unscored, not useless."""
        self._known_sections.add((skill_id, section, version))

    def observe_load(
        self, decision_id: str, skill_id: str, section: str, version: str, characters: int
    ) -> None:
        """This section was loaded into this decision. Matched by identifier, never by period."""
        key = (skill_id, section, version)
        self._known_sections.add(key)
        self._loads.setdefault(key, []).append(characters)
        self._decision_loads.setdefault(decision_id, []).append(key)
        self.standing.characters_loaded += characters

    def observe_decision_outcome(self, decision_id: str, was_good: bool) -> None:
        """What happened to a decision, credited only to the sections it actually loaded."""
        self.standing.decisions_recorded += 1
        for key in self._decision_loads.get(decision_id, ()):
            self._outcome_for(key).observe(was_good)

    def score(self, skill_id: str, section: str, version: str) -> SkillUsefulness:
        key = (skill_id, section, version)
        self.standing.sections_scored += 1
        self.standing.by_skill[skill_id] = self.standing.by_skill.get(skill_id, 0) + 1

        loads = self._loads.get(key, [])
        estimate = self._outcome_for(key).estimate(self._minimum_decisions)
        characters = sum(loads) / len(loads) if loads else 0.0

        if not loads:
            # Never loaded and useless look identical in a ranking and mean
            # opposite things: one has been tried, the other is waiting.
            self.standing.never_loaded += 1
            return self._usefulness(
                skill_id, section, version, NEVER_LOADED, None, estimate, 0, 0.0, None,
                "nothing has ever loaded this section, so it is unscored rather than useless "
                "-- the two look identical in a ranking and mean opposite things",
            )

        if not estimate.is_fitted:
            self.standing.too_few_decisions += 1
            return self._usefulness(
                skill_id, section, version, TOO_FEW_DECISIONS, None, estimate, len(loads),
                characters, None,
                f"{estimate.observations} decision(s) of the {self._minimum_decisions} needed "
                f"loaded it",
            )

        usefulness = estimate.value - self._base_rate
        # Cost counted: usefulness that ignores cost always recommends loading
        # everything.
        per_character = usefulness / characters if characters else None

        state = USEFUL if estimate.value >= self._threshold else NOT_USEFUL
        if state == USEFUL:
            self.standing.useful += 1
        else:
            self.standing.not_useful += 1

        return self._usefulness(
            skill_id, section, version, state, usefulness, estimate, len(loads),
            characters, per_character,
            f"loaded into {len(loads)} decision(s), of which {estimate.value:.0%} went well "
            f"against a {self._base_rate:.0%} base rate -- {usefulness:+.1%} of usefulness at "
            f"{characters:,.0f} character(s) per load"
            + (
                f", {per_character:.2e} per character. A section that helps slightly and "
                f"costs a third of the budget is displacing something better"
                if per_character is not None
                else ""
            )
            + ". Scored per section rather than per skill, because a skill with an excellent "
            "risk section and a noisy entry section scores mediocre overall and the useful "
            "action is to keep one and drop the other",
        )

    def score_all(self) -> tuple:
        return tuple(
            self.score(skill_id, section, version)
            for skill_id, section, version in sorted(self._known_sections)
        )

    def _outcome_for(self, key) -> RateEstimator:
        estimator = self._outcomes.get(key)
        if estimator is None:
            estimator = RateEstimator(
                prior=self._base_rate, prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            )
            self._outcomes[key] = estimator
        return estimator

    def _usefulness(
        self, skill_id, section, version, state, usefulness, estimate, loads,
        characters, per_character, reason,
    ) -> SkillUsefulness:
        return SkillUsefulness(
            skill_id=skill_id,
            section=section,
            version=version,
            state=state,
            usefulness=usefulness,
            hit_rate=estimate,
            decisions_loaded_into=loads,
            characters_per_load=characters,
            usefulness_per_character=per_character,
            reason=reason,
            scored_at_ns=self._now_ns(),
        )


def describe_skill_scoring(scorer: SkillScorer) -> dict:
    return {
        "part_id": PART_ID,
        "sections_scored": scorer.standing.sections_scored,
        "useful": scorer.standing.useful,
        "not_useful": scorer.standing.not_useful,
        "never_loaded": scorer.standing.never_loaded,
        "too_few_decisions": scorer.standing.too_few_decisions,
        "decisions_recorded": scorer.standing.decisions_recorded,
        "characters_loaded": scorer.standing.characters_loaded,
        "by_skill": dict(sorted(scorer.standing.by_skill.items())),
        "removes_anything": False,
    }


def run_skill_scorer(
    scorer: SkillScorer, control_socket, read_loads_and_outcomes, publish_usefulness,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_loads_and_outcomes(scorer)
        publish_usefulness(scorer.score_all())

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

    A load is observed under the question it answered as the decision it
    went into; a version names the sections it recorded. A closed episode
    is a decision's outcome, and nothing on this part's inputs ties an
    episode to the question a load answered, so outcomes are read and
    drained and every loaded section is scored on its loads alone --
    stated here rather than joined by guesswork. Scores go out once per
    health interval.
    """
    import time as _time

    from runtime.input_assembly import Batch

    loads = Batch(read=context.bus.reader("loaded-skill-section"))
    episodes = Batch(read=context.bus.reader("trade-episode"))
    versions = Batch(read=context.bus.reader("skill-version"))
    publish_usefulness = context.bus.publisher_for("skill-usefulness")
    scorer = SkillScorer(
        minimum_decisions=int(context.number("decoding_minimum_trades")),
        usefulness_threshold=context.number("skill_useful_threshold"),
        base_rate=context.number("learning_prior_hit_rate"),
        prior_weight=context.number("learning_prior_weight"),
        half_life_observations=context.number("learning_half_life_observations"),
    )
    current_version: dict[str, str] = {}
    last_publish = [float("-inf")]

    def read_loads_and_outcomes(_scorer) -> None:
        episodes.payloads()
        for version in versions.payloads():
            if version.is_current:
                current_version[version.skill_id] = version.version
        for load in loads.payloads():
            for section in load.sections:
                version = current_version.get(section.skill_id, "1")
                scorer.observe_section_exists(section.skill_id, section.section, version)
                scorer.observe_load(load.question, section.skill_id, section.section, version, int(section.characters))

    def publish(items) -> None:
        now = _time.monotonic()
        if now - last_publish[0] < context.health_interval_seconds:
            return
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_usefulness(kept)
            last_publish[0] = now

    return run_skill_scorer(
        scorer=scorer,
        control_socket=context.control_socket,
        read_loads_and_outcomes=read_loads_and_outcomes,
        publish_usefulness=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
