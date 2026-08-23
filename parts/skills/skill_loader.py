"""skill-loader: only the section a question needs, never the whole skill.

The reason a 400-page book is affordable to consult. Loading a whole skill for
every question is the obvious implementation and the one that makes skills
unusable: the context is spent, the relevant paragraph is buried, and the model
reading it attends to whatever was longest rather than whatever was relevant.

So loading is by section, and the discipline is about what does not get loaded:

- **The question decides the section**, matched on the section's own name and the
  rules it holds. A loader that loaded everything and let the reader filter has
  moved the problem, not solved it.
- **Only usable skills are loaded.** An untested or contradicted skill stays out
  entirely -- offering it with a caveat means it gets used, because a caveat is
  a sentence and the advice is a rule.
- **A budget, enforced.** Sections are loaded in order of fit until the budget is
  spent, and what did not fit is named. A silent truncation is the same failure
  as a silent recall truncation: it looks like the skill said nothing.
- **Anti-patterns load first when the question is about risk.** They are the most
  useful part of any skill and the part that loses every ranking based on
  keyword overlap, because they are phrased as negations.

**What was loaded is recorded**, so a decision made with a skill can be traced to
the section that informed it -- and so the scorer can say which sections are worth
their space.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "skill-loader"

PART_DECLARATION = PartDeclaration(
    part_id="skill-loader",
    consumes=("available-skill",),
    produces=("loaded-skill-section", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

LOADED = "loaded"
NOTHING_FITS = "no-section-answers-this-question"
NO_USABLE_SKILL = "every-skill-that-might-help-is-untested-or-contradicted"
BUDGET_SPENT = "the-budget-was-spent-before-everything-that-fit-was-loaded"

ABOUT_RISK = "risk"


@dataclass(frozen=True)
class LoadedSection:
    """One section, why it was chosen, and what it cost."""

    skill_id: str
    section: str
    content: str
    fit: float
    characters: int
    is_anti_pattern_section: bool
    reason: str


@dataclass(frozen=True)
class Load:
    """What was loaded for one question, and what was left out."""

    question: str
    state: str
    sections: tuple
    characters_used: int
    budget: int
    sections_that_fit_but_did_not_load: tuple
    skills_excluded: dict
    reason: str
    loaded_at_ns: int

    @property
    def loaded_anything(self) -> bool:
        return self.state in (LOADED, BUDGET_SPENT)

    @property
    def was_truncated(self) -> bool:
        return bool(self.sections_that_fit_but_did_not_load)


@dataclass
class LoaderStanding:
    loads: int = 0
    sections_loaded: int = 0
    loads_with_nothing_fitting: int = 0
    loads_with_no_usable_skill: int = 0
    truncated_loads: int = 0
    anti_patterns_loaded_first: int = 0
    characters_loaded: int = 0
    by_section: dict = field(default_factory=dict)


class SkillLoader:
    """Loads the section a question needs, within a budget, and names what it left."""

    def __init__(
        self,
        character_budget: int,
        minimum_fit: float,
        now_ns=time.time_ns,
    ) -> None:
        if character_budget < 1:
            raise ValueError(
                "a loader with no budget loads everything, which is the implementation that "
                "makes skills unusable"
            )
        if not 0.0 < minimum_fit <= 1.0:
            raise ValueError("fit is a fraction and its floor must be inside (0, 1]")
        self._budget = character_budget
        self._minimum_fit = minimum_fit
        self._now_ns = now_ns
        self._sections: dict[tuple[str, str], str] = {}
        self._usable: dict[str, bool] = {}
        self._excluded: dict[str, str] = {}
        self.standing = LoaderStanding()

    def observe_available_skill(self, entry, sections: dict) -> None:
        """One indexed skill and its sections. Unusable ones are recorded and not loaded."""
        self._usable[entry.skill_id] = entry.is_usable
        if not entry.is_usable:
            # Offering it with a caveat means it gets used: a caveat is a
            # sentence and the advice is a rule.
            self._excluded[entry.skill_id] = entry.state
            return
        for name, content in sections.items():
            self._sections[(entry.skill_id, name)] = content

    def fit_of(self, question: str, skill_id: str, section: str) -> float:
        """How well one section answers one question, on its name and its content."""
        content = self._sections.get((skill_id, section), "")
        words = {word.lower() for word in question.split() if len(word) > 3}
        if not words:
            return 0.0
        haystack = f"{section} {content}".lower()
        return sum(1 for word in words if word in haystack) / len(words)

    def load(self, question: str, is_about_risk: bool = False) -> Load:
        """The sections that answer this question, in order of fit, within the budget."""
        self.standing.loads += 1

        if not self._sections:
            self.standing.loads_with_no_usable_skill += 1
            return self._load(
                question, NO_USABLE_SKILL, (), 0, (), 
                f"every skill that might help is untested or contradicted: "
                + ", ".join(
                    f"{skill_id} ({state})" for skill_id, state in sorted(self._excluded.items())
                ),
            )

        candidates = []
        for (skill_id, section), content in self._sections.items():
            fit = self.fit_of(question, skill_id, section)
            if fit < self._minimum_fit:
                continue
            is_anti_pattern = "anti-pattern" in content.lower() or "anti" in section.lower()
            candidates.append((fit, is_anti_pattern, skill_id, section, content))

        if not candidates:
            self.standing.loads_with_nothing_fitting += 1
            return self._load(
                question, NOTHING_FITS, (), 0, (),
                f"no section reaches the {self._minimum_fit:.0%} fit this loader requires. "
                f"That is a real answer: the skills held do not address this",
            )

        # Anti-patterns first when the question is about risk: they are the most
        # useful part of any skill and they lose every keyword ranking because
        # they are phrased as negations.
        candidates.sort(
            key=lambda entry: (
                -(entry[1] and is_about_risk),
                -entry[0],
                entry[2],
                entry[3],
            )
        )

        loaded = []
        used = 0
        skipped = []
        for fit, is_anti_pattern, skill_id, section, content in candidates:
            if used + len(content) > self._budget:
                skipped.append(f"{skill_id}:{section}")
                continue
            used += len(content)
            if is_anti_pattern and is_about_risk:
                self.standing.anti_patterns_loaded_first += 1
            loaded.append(
                LoadedSection(
                    skill_id=skill_id,
                    section=section,
                    content=content,
                    fit=fit,
                    characters=len(content),
                    is_anti_pattern_section=is_anti_pattern,
                    reason=(
                        f"{fit:.0%} fit for this question"
                        + (
                            ", and loaded first because it is an anti-pattern section and the "
                            "question is about risk"
                            if is_anti_pattern and is_about_risk
                            else ""
                        )
                    ),
                )
            )
            self.standing.by_section[f"{skill_id}:{section}"] = (
                self.standing.by_section.get(f"{skill_id}:{section}", 0) + 1
            )

        self.standing.sections_loaded += len(loaded)
        self.standing.characters_loaded += used
        if skipped:
            self.standing.truncated_loads += 1

        return self._load(
            question, BUDGET_SPENT if skipped else LOADED, tuple(loaded), used,
            tuple(skipped),
            f"{len(loaded)} section(s), {used:,} of {self._budget:,} character(s)"
            + (
                f"; {len(skipped)} section(s) fit the question and did not fit the budget: "
                f"{', '.join(skipped)}. Named rather than dropped quietly -- a silent "
                f"truncation looks like the skill said nothing"
                if skipped
                else ""
            ),
        )

    def _load(self, question, state, sections, used, skipped, reason) -> Load:
        return Load(
            question=question,
            state=state,
            sections=sections,
            characters_used=used,
            budget=self._budget,
            sections_that_fit_but_did_not_load=skipped,
            skills_excluded=dict(self._excluded),
            reason=reason,
            loaded_at_ns=self._now_ns(),
        )


def describe_loading(loader: SkillLoader) -> dict:
    return {
        "part_id": PART_ID,
        "loads": loader.standing.loads,
        "sections_loaded": loader.standing.sections_loaded,
        "loads_with_nothing_fitting": loader.standing.loads_with_nothing_fitting,
        "loads_with_no_usable_skill": loader.standing.loads_with_no_usable_skill,
        "truncated_loads": loader.standing.truncated_loads,
        "anti_patterns_loaded_first": loader.standing.anti_patterns_loaded_first,
        "characters_loaded": loader.standing.characters_loaded,
        "by_section": dict(sorted(loader.standing.by_section.items())),
        "budget": loader._budget,
        "loads_whole_skills": False,
    }


def run_skill_loader(
    loader: SkillLoader, control_socket, read_questions, publish_sections,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        questions = read_questions(loader)
        publish_sections(
            tuple(loader.load(question, about_risk) for question, about_risk in questions)
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
