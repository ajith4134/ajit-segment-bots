"""skill-gap-finder: what this system needed to know and did not.

The only part that produces the demand the readers act on. Without it, fetching
is browsing: the system reads what is available rather than what it lacks, and it
ends up knowing a great deal about whatever is written about most.

A gap is recorded when a question was asked and nothing usable answered it, and
the discipline is in which of those count:

- **The question has to have been real.** A question nobody asked twice is not a
  gap; it is a question. Gaps are raised on repetition, because the second time
  the same thing goes unanswered is when it becomes worth going to look.
- **A question answered by an unusable skill is a gap.** An untested or
  contradicted skill that would have answered it is not an answer -- and this is
  the case a naive finder misses, because something was there.
- **A question the loader truncated is a different gap.** The knowledge existed
  and did not fit; the fix is budget or better sectioning, not another book.
- **A gap names what would close it**, so a reader has a query rather than a
  topic.

**Gaps expire.** A question nobody has asked for a month is not a gap any more,
and a queue of stale gaps sends the readers after things nothing needs.

**Repeated failures on the same gap are counted and reported.** A gap that has
been fetched against three times and is still open is not a missing book -- it is
a question the system cannot express, and going back a fourth time will not help.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "skill-gap-finder"

PART_DECLARATION = PartDeclaration(
    part_id="skill-gap-finder",
    consumes=("loaded-skill-section", "llm-request"),
    produces=("skill-gap", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

OPEN = "open"
NOT_YET_A_GAP = "asked-once-is-a-question-rather-than-a-gap"
EXPIRED = "nothing-has-asked-this-for-long-enough-that-it-is-not-a-gap"
UNANSWERABLE = "fetched-against-repeatedly-and-still-open"
CLOSED = "something-usable-now-answers-it"

NOTHING_HELD = "nothing-in-the-index-addresses-it"
ONLY_UNUSABLE = "what-would-have-answered-it-is-untested-or-contradicted"
DID_NOT_FIT = "the-knowledge-existed-and-did-not-fit-the-budget"


@dataclass(frozen=True)
class SkillGap:
    """Something this system needed to know, with what would close it."""

    gap_id: str
    question: str
    state: str
    because: str
    times_asked: int
    fetches_attempted: int
    query: str
    unusable_skills_that_would_have_answered: tuple
    last_asked_at_ns: int
    reason: str
    raised_at_ns: int

    @property
    def is_open(self) -> bool:
        return self.state == OPEN

    @property
    def is_worth_fetching_against(self) -> bool:
        return self.state == OPEN and self.because != DID_NOT_FIT


@dataclass
class FinderStanding:
    questions_seen: int = 0
    gaps_open: int = 0
    gaps_closed: int = 0
    gaps_expired: int = 0
    gaps_unanswerable: int = 0
    budget_gaps: int = 0
    unusable_skill_gaps: int = 0
    by_reason: dict = field(default_factory=dict)


class SkillGapFinder:
    """Records what went unanswered, on repetition, with a query that would close it."""

    def __init__(
        self,
        asks_before_a_gap: int,
        expire_after_seconds: float,
        fetches_before_unanswerable: int,
        now_ns=time.time_ns,
    ) -> None:
        if asks_before_a_gap < 2:
            raise ValueError(
                "a question asked once is a question; a gap is what the second unanswered "
                "asking makes it"
            )
        if expire_after_seconds <= 0:
            raise ValueError(
                "a queue of stale gaps sends the readers after things nothing needs"
            )
        if fetches_before_unanswerable < 1:
            raise ValueError("a gap needs at least one attempt before it can be unanswerable")
        self._asks_before = asks_before_a_gap
        self._expire_after_ns = int(expire_after_seconds * 1e9)
        self._fetches_before_unanswerable = fetches_before_unanswerable
        self._now_ns = now_ns
        self._questions: dict[str, str] = {}
        self._asks: dict[str, int] = {}
        self._last_asked: dict[str, int] = {}
        self._because: dict[str, str] = {}
        self._unusable: dict[str, tuple] = {}
        self._fetches: dict[str, int] = {}
        self._closed: set[str] = set()
        self.standing = FinderStanding()

    def observe_unanswered(
        self, question: str, because: str, unusable_skills=()
    ) -> None:
        """One question nothing usable answered, and why nothing did."""
        self.standing.questions_seen += 1
        gap_id = self._gap_id(question)
        self._asks[gap_id] = self._asks.get(gap_id, 0) + 1
        self._last_asked[gap_id] = self._now_ns()
        self._because[gap_id] = because
        if unusable_skills:
            self._unusable[gap_id] = tuple(unusable_skills)
        self._closed.discard(gap_id)
        self.standing.by_reason[because] = self.standing.by_reason.get(because, 0) + 1

    def observe_answered(self, question: str) -> None:
        """Something usable now answers it."""
        gap_id = self._gap_id(question)
        if gap_id in self._asks:
            self._closed.add(gap_id)
            self.standing.gaps_closed += 1

    def observe_fetch_attempt(self, question: str) -> None:
        """A reader went looking for this. Counted, so repeated failure is visible."""
        gap_id = self._gap_id(question)
        self._fetches[gap_id] = self._fetches.get(gap_id, 0) + 1

    def gap_for(self, question: str) -> SkillGap:
        gap_id = self._gap_id(question)
        asks = self._asks.get(gap_id, 0)
        because = self._because.get(gap_id, NOTHING_HELD)
        unusable = self._unusable.get(gap_id, ())
        fetches = self._fetches.get(gap_id, 0)
        last_asked = self._last_asked.get(gap_id, 0)

        if gap_id in self._closed:
            return self._gap(
                gap_id, question, CLOSED, because, asks, fetches, unusable, last_asked,
                "something usable now answers this",
            )

        if asks < self._asks_before:
            return self._gap(
                gap_id, question, NOT_YET_A_GAP, because, asks, fetches, unusable, last_asked,
                f"asked {asks} time(s) of the {self._asks_before} that makes it a gap. The "
                f"second time the same thing goes unanswered is when it becomes worth going "
                f"to look",
            )

        if self._now_ns() - last_asked > self._expire_after_ns:
            self.standing.gaps_expired += 1
            return self._gap(
                gap_id, question, EXPIRED, because, asks, fetches, unusable, last_asked,
                f"nothing has asked this for "
                f"{(self._now_ns() - last_asked) / 86400e9:.1f} day(s). A queue of stale gaps "
                f"sends the readers after things nothing needs",
            )

        if fetches >= self._fetches_before_unanswerable:
            # Not a missing book: a question the system cannot express, and
            # going back a fourth time will not help.
            self.standing.gaps_unanswerable += 1
            return self._gap(
                gap_id, question, UNANSWERABLE, because, asks, fetches, unusable, last_asked,
                f"fetched against {fetches} time(s) and still open. That is not a missing "
                f"book, it is a question this system cannot express, and going back again "
                f"will not help",
            )

        self.standing.gaps_open += 1
        if because == DID_NOT_FIT:
            self.standing.budget_gaps += 1
        if unusable:
            self.standing.unusable_skill_gaps += 1

        return self._gap(
            gap_id, question, OPEN, because, asks, fetches, unusable, last_asked,
            f"asked {asks} time(s) and still unanswered: {because}"
            + (
                f". {', '.join(unusable)} would have answered it and is untested or "
                f"contradicted -- something was there, which is the case a naive finder misses"
                if unusable
                else ""
            )
            + (
                ". The knowledge existed and did not fit the budget, so the fix is budget or "
                "better sectioning rather than another book"
                if because == DID_NOT_FIT
                else ""
            ),
        )

    def open_gaps(self) -> tuple:
        """Every gap worth acting on, each with a query rather than a topic."""
        return tuple(
            gap
            for gap in (
                self.gap_for(question) for question in sorted(self._questions.values())
            )
            if gap.is_open
        )

    def _gap_id(self, question: str) -> str:
        """One identifier per question, so two spellings of it are one gap."""
        normalised = " ".join(question.lower().split())
        self._questions[normalised] = question
        return normalised

    def _query_for(self, question: str) -> str:
        """What a reader should go looking for: a query rather than a topic."""
        words = [word for word in question.split() if len(word) > 3]
        return " ".join(words[:8]) if words else question

    def _gap(
        self, gap_id, question, state, because, asks, fetches, unusable, last_asked, reason
    ) -> SkillGap:
        return SkillGap(
            gap_id=gap_id,
            question=question,
            state=state,
            because=because,
            times_asked=asks,
            fetches_attempted=fetches,
            query=self._query_for(question),
            unusable_skills_that_would_have_answered=unusable,
            last_asked_at_ns=last_asked,
            reason=reason,
            raised_at_ns=self._now_ns(),
        )


def describe_gaps(finder: SkillGapFinder) -> dict:
    return {
        "part_id": PART_ID,
        "questions_seen": finder.standing.questions_seen,
        "gaps_open": finder.standing.gaps_open,
        "gaps_closed": finder.standing.gaps_closed,
        "gaps_expired": finder.standing.gaps_expired,
        "gaps_that_fetching_will_not_close": finder.standing.gaps_unanswerable,
        "gaps_that_are_really_budget": finder.standing.budget_gaps,
        "gaps_where_an_unusable_skill_would_have_answered": finder.standing.unusable_skill_gaps,
        "by_reason": dict(sorted(finder.standing.by_reason.items())),
        "raises_a_gap_on_one_asking": False,
    }


def run_skill_gap_finder(
    finder: SkillGapFinder, control_socket, read_questions, publish_gaps,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_questions(finder)
        publish_gaps(finder.open_gaps())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_gaps(finder),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A load that answered its question closes the gap; a load that found
    nothing, nothing usable, or nothing that fit opens or widens one. An
    LLM request is a question asked, counted as unanswered until a load
    for it says otherwise.
    """
    from runtime.input_assembly import Batch

    loads = Batch(read=context.bus.reader("loaded-skill-section"))
    requests = Batch(read=context.bus.reader("llm-request"))
    publish_gaps = context.bus.publisher_for("skill-gap")
    finder = SkillGapFinder(
        asks_before_a_gap=int(context.number("skill_asks_before_a_gap")),
        expire_after_seconds=context.number("skill_gap_expire_after_seconds"),
        fetches_before_unanswerable=int(context.number("skill_fetches_before_unanswerable")),
    )
    because_of_state = {"no-section-answers-this-question": NOTHING_HELD,
                        "every-skill-that-might-help-is-untested-or-contradicted": ONLY_UNUSABLE,
                        "the-budget-was-spent-before-everything-that-fit-was-loaded": DID_NOT_FIT}

    def read_questions(_finder) -> None:
        for request in requests.payloads():
            finder.observe_unanswered(str(request.instruction), NOTHING_HELD)
        for load in loads.payloads():
            if load.loaded_anything:
                finder.observe_answered(load.question)
            else:
                excluded = tuple(load.skills_excluded) if isinstance(load.skills_excluded, dict) else ()
                finder.observe_unanswered(load.question, because_of_state.get(load.state, NOTHING_HELD), excluded)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_gaps(kept)

    return run_skill_gap_finder(
        finder=finder,
        control_socket=context.control_socket,
        read_questions=read_questions,
        publish_gaps=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
