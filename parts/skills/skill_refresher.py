"""skill-refresher: which skills are worth going back to the source for.

Sources go stale, and re-reading everything periodically is both expensive and
wrong -- it spends the fetch budget on whatever was ingested longest ago rather
than on whatever matters most now. So refreshing is prioritised, and the priority
is about what a refresh would actually change:

- **A useful skill that has gone stale is first.** It is being loaded into real
  decisions and its source may have changed under it, which is the combination
  that does damage.
- **A useless skill that has gone stale is last.** Refreshing it produces a
  better version of something nothing loads.
- **A useful skill that is not stale is not refreshed at all.** Re-reading a
  source that has not changed produces an identical version, which the version
  keeper discards -- so the whole fetch was waste.
- **A skill whose source has gone is not refreshable**, and that is different
  from not needing a refresh. It should be replaced, and saying so is more useful
  than silently never refreshing it.

**A refresh that produced no change counts against the next one.** A source that
has been re-read three times without changing is a source that does not change,
and continuing to check it is spending the budget on a settled question.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "skill-refresher"

PART_DECLARATION = PartDeclaration(
    part_id="skill-refresher",
    consumes=("skill-usefulness", "stale-knowledge"),
    produces=("skill-refresh-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

REFRESH = "refresh"
NOT_STALE = "its-source-has-not-changed-so-a-refresh-would-produce-an-identical-version"
NOT_WORTH_IT = "nothing-loads-it-so-a-better-version-would-change-nothing"
SOURCE_IS_GONE = "it-cannot-be-refreshed-and-should-be-replaced"
SETTLED = "re-read-repeatedly-without-changing"


@dataclass(frozen=True)
class SkillRefreshRequest:
    """One skill, and whether going back to its source is worth the fetch."""

    skill_id: str
    source_reference: str | None
    state: str
    priority: float
    usefulness: float | None
    is_stale: bool
    refreshes_without_change: int
    reason: str
    requested_at_ns: int

    @property
    def should_be_refreshed(self) -> bool:
        return self.state == REFRESH

    @property
    def should_be_replaced(self) -> bool:
        """Different from not needing a refresh, and more useful to say."""
        return self.state == SOURCE_IS_GONE


@dataclass
class RefresherStanding:
    skills_considered: int = 0
    refreshes_requested: int = 0
    skipped_not_stale: int = 0
    skipped_not_useful: int = 0
    unrefreshable: int = 0
    settled: int = 0
    refreshes_that_changed_nothing: int = 0
    highest_priority_seen: float | None = None


class SkillRefresher:
    """Prioritises refreshes by what a refresh would actually change."""

    def __init__(
        self,
        useful_threshold: float,
        settled_after_unchanged: int,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < useful_threshold < 1.0:
            raise ValueError("the usefulness bar is a rate inside (0, 1)")
        if settled_after_unchanged < 1:
            raise ValueError(
                "a source re-read repeatedly without changing is a settled question, and "
                "checking it again spends the budget on nothing"
            )
        self._useful_threshold = useful_threshold
        self._settled_after = settled_after_unchanged
        self._now_ns = now_ns
        self._usefulness: dict[str, float] = {}
        self._stale: set[str] = set()
        self._sources: dict[str, str | None] = {}
        self._unchanged: dict[str, int] = {}
        self.standing = RefresherStanding()

    def observe_usefulness(self, skill_id: str, usefulness: float) -> None:
        self._usefulness[skill_id] = usefulness

    def observe_stale(self, skill_id: str, is_stale: bool) -> None:
        if is_stale:
            self._stale.add(skill_id)
        else:
            self._stale.discard(skill_id)

    def observe_source(self, skill_id: str, source_reference: str | None) -> None:
        """None means the source can no longer be reached."""
        self._sources[skill_id] = source_reference

    def observe_refresh_outcome(self, skill_id: str, changed_anything: bool) -> None:
        """A refresh that produced no change counts against the next one."""
        if changed_anything:
            self._unchanged[skill_id] = 0
        else:
            self._unchanged[skill_id] = self._unchanged.get(skill_id, 0) + 1
            self.standing.refreshes_that_changed_nothing += 1

    def priority_of(self, skill_id: str) -> float:
        """Usefulness times staleness: what a refresh would actually change."""
        usefulness = self._usefulness.get(skill_id, 0.0)
        return usefulness if skill_id in self._stale else 0.0

    def consider(self, skill_id: str) -> SkillRefreshRequest:
        self.standing.skills_considered += 1
        usefulness = self._usefulness.get(skill_id)
        is_stale = skill_id in self._stale
        source = self._sources.get(skill_id, "")
        unchanged = self._unchanged.get(skill_id, 0)
        priority = self.priority_of(skill_id)

        if skill_id in self._sources and source is None:
            self.standing.unrefreshable += 1
            return self._request(
                skill_id, None, SOURCE_IS_GONE, 0.0, usefulness, is_stale, unchanged,
                "its source can no longer be reached, so it cannot be refreshed and should be "
                "replaced. That is different from not needing a refresh, and saying so is more "
                "useful than silently never refreshing it",
            )

        if unchanged >= self._settled_after:
            self.standing.settled += 1
            return self._request(
                skill_id, source, SETTLED, 0.0, usefulness, is_stale, unchanged,
                f"re-read {unchanged} time(s) without changing. A source that does not change "
                f"is a settled question, and checking it again spends the budget on nothing",
            )

        if not is_stale:
            # An identical version is discarded by the version keeper, so the
            # whole fetch was waste.
            self.standing.skipped_not_stale += 1
            return self._request(
                skill_id, source, NOT_STALE, 0.0, usefulness, is_stale, unchanged,
                "its source has not changed, so a refresh would produce an identical version "
                "that the version keeper discards -- the whole fetch would be waste",
            )

        if usefulness is None or usefulness < self._useful_threshold:
            self.standing.skipped_not_useful += 1
            return self._request(
                skill_id, source, NOT_WORTH_IT, priority, usefulness, is_stale, unchanged,
                f"it is stale and "
                + (
                    f"{usefulness:.2f} useful, below the {self._useful_threshold:.2f} bar"
                    if usefulness is not None
                    else "has never been loaded"
                )
                + ". Refreshing it produces a better version of something nothing loads",
            )

        self.standing.refreshes_requested += 1
        if (
            self.standing.highest_priority_seen is None
            or priority > self.standing.highest_priority_seen
        ):
            self.standing.highest_priority_seen = priority

        return self._request(
            skill_id, source, REFRESH, priority, usefulness, is_stale, unchanged,
            f"stale and {usefulness:.2f} useful, priority {priority:.2f}. This is the "
            f"combination that does damage: it is being loaded into real decisions and its "
            f"source may have changed under it",
        )

    def requests_in_priority_order(self, skill_ids) -> tuple:
        requests = [self.consider(skill_id) for skill_id in skill_ids]
        return tuple(
            sorted(
                (request for request in requests if request.should_be_refreshed),
                key=lambda request: -request.priority,
            )
        )

    def _request(
        self, skill_id, source, state, priority, usefulness, is_stale, unchanged, reason
    ) -> SkillRefreshRequest:
        return SkillRefreshRequest(
            skill_id=skill_id,
            source_reference=source,
            state=state,
            priority=priority,
            usefulness=usefulness,
            is_stale=is_stale,
            refreshes_without_change=unchanged,
            reason=reason,
            requested_at_ns=self._now_ns(),
        )


def describe_refreshing(refresher: SkillRefresher) -> dict:
    return {
        "part_id": PART_ID,
        "skills_considered": refresher.standing.skills_considered,
        "refreshes_requested": refresher.standing.refreshes_requested,
        "skipped_not_stale": refresher.standing.skipped_not_stale,
        "skipped_nothing_loads_it": refresher.standing.skipped_not_useful,
        "unrefreshable_and_should_be_replaced": refresher.standing.unrefreshable,
        "settled_sources": refresher.standing.settled,
        "refreshes_that_changed_nothing": refresher.standing.refreshes_that_changed_nothing,
        "highest_priority_seen": refresher.standing.highest_priority_seen,
        "refreshes_on_a_timer": False,
    }


def run_skill_refresher(
    refresher: SkillRefresher, control_socket, read_skills, publish_requests,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        skill_ids = read_skills(refresher)
        publish_requests(refresher.requests_in_priority_order(skill_ids))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
