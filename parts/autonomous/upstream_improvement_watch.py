"""upstream-improvement-watch: something outside changed that this depends on.

Every dependency this system has is a moving target maintained by somebody else. A
venue changes a field, a library changes a default, a model is deprecated and silently
routed to its successor. None of these announce themselves in a way this system reads
by default, and all of them change behaviour without a single line here changing.

The failure this prevents is the worst kind of debugging session: behaviour changed,
nothing in the codebase changed, and nobody thinks to look outside.

What it watches for, and why each is different:

- **Breaking changes** -- a field renamed, an endpoint removed, a parameter now
  required. These fail loudly and quickly, which makes them the least dangerous.
- **Silent behaviour changes** -- a default altered, a rounding rule changed, a model
  updated behind its own version string. They fail quietly and are attributed to the
  market for weeks.
- **Deprecations with a date** -- a thing still working that will stop. The only class
  where acting early is strictly cheaper than acting late.
- **Genuine improvements** -- a faster method, a better model, a new endpoint that
  removes a workaround. Watching only for breakage means the system never gets better
  at anything it already does.

**A change is recorded against the parts it affects**, computed from what they
declare rather than guessed, so "this venue changed" becomes "these four parts read
that field". A notice nobody can route is a notice nobody acts on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.autonomy_types import UpstreamChange
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "upstream-improvement-watch"

PART_DECLARATION = PartDeclaration(
    part_id="upstream-improvement-watch",
    consumes=("research-finding", "part-health"),
    produces=("upstream-change", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

NOTICED = "noticed"
ALREADY_KNOWN = "already-noticed"
AFFECTS_NOTHING = "nothing-here-depends-on-it"
NOT_A_CHANGE = "not-a-kind-of-change-this-part-tracks"

BREAKING = "breaking"
SILENT_BEHAVIOUR_CHANGE = "silent-behaviour-change"
DEPRECATION = "deprecation"
IMPROVEMENT = "improvement"

CHANGE_KINDS = (BREAKING, SILENT_BEHAVIOUR_CHANGE, DEPRECATION, IMPROVEMENT)


@dataclass(frozen=True)
class WatchOutcome:
    subject: str
    state: str
    change: UpstreamChange | None
    affected_parts: tuple
    reason: str
    noticed_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.change is not None


@dataclass
class WatchStanding:
    changes_noticed: int = 0
    duplicates: int = 0
    affecting_nothing: int = 0
    by_kind: dict = field(default_factory=dict)
    breaking_changes: int = 0
    silent_changes: int = 0
    improvements_noticed: int = 0
    dependencies_declared: int = 0


class UpstreamImprovementWatch:
    """Notices outside changes and routes them to the parts that actually depend on them."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._dependencies: dict[str, set] = {}
        self._seen: set = set()
        self.standing = WatchStanding()

    def declare_dependency(self, part_id: str, subject: str) -> None:
        """Computed routing rather than guessed: 'these four parts read that field'."""
        if part_id not in {
            member for members in self._dependencies.values() for member in members
        }:
            self.standing.dependencies_declared += 1
        self._dependencies.setdefault(subject, set()).add(part_id)

    def parts_depending_on(self, subject: str) -> tuple:
        return tuple(sorted(self._dependencies.get(subject, set())))

    def notice(
        self, subject: str, kind: str, detail: str, source_reference: str,
        effective_at_ns: int | None = None,
    ) -> WatchOutcome:
        if kind not in CHANGE_KINDS:
            return self._outcome(
                subject, NOT_A_CHANGE, None, (),
                f"{kind!r} is not a kind of change this part tracks",
            )

        key = f"{subject}:{kind}:{detail}"
        if key in self._seen:
            self.standing.duplicates += 1
            return self._outcome(
                subject, ALREADY_KNOWN, None, self.parts_depending_on(subject),
                "already noticed",
            )

        affected = self.parts_depending_on(subject)
        if not affected:
            self.standing.affecting_nothing += 1
            return self._outcome(
                subject, AFFECTS_NOTHING, None, (),
                f"nothing here declares a dependency on {subject}. A notice nobody can "
                f"route is a notice nobody acts on",
            )

        self._seen.add(key)
        self.standing.changes_noticed += 1
        self.standing.by_kind[kind] = self.standing.by_kind.get(kind, 0) + 1
        if kind == BREAKING:
            self.standing.breaking_changes += 1
        elif kind == SILENT_BEHAVIOUR_CHANGE:
            self.standing.silent_changes += 1
        elif kind == IMPROVEMENT:
            self.standing.improvements_noticed += 1

        change = UpstreamChange(
            subject=subject,
            kind=kind,
            detail=detail,
            affects_parts=affected,
            is_breaking=kind in (BREAKING, SILENT_BEHAVIOUR_CHANGE),
            source_reference=source_reference,
            noticed_at_ns=self._now_ns(),
        )
        return self._outcome(
            subject, NOTICED, change, affected,
            f"{kind} in {subject}: {detail}. Affects {len(affected)} part(s): "
            f"{', '.join(affected)}"
            + {
                BREAKING: ". This fails loudly and quickly, which makes it the least "
                          "dangerous kind",
                SILENT_BEHAVIOUR_CHANGE: ". This fails quietly and gets attributed to the "
                                         "market for weeks -- behaviour changed and "
                                         "nothing in the codebase did",
                DEPRECATION: ". Still working and will stop. This is the only class where "
                             "acting early is strictly cheaper than acting late",
                IMPROVEMENT: ". Watching only for breakage means the system never gets "
                             "better at anything it already does",
            }[kind],
        )

    def _outcome(self, subject, state, change, affected, reason) -> WatchOutcome:
        return WatchOutcome(
            subject=subject, state=state, change=change, affected_parts=affected,
            reason=reason, noticed_at_ns=self._now_ns(),
        )


def describe_upstream_watch(watch: UpstreamImprovementWatch) -> dict:
    return {
        "part_id": PART_ID,
        "changes_noticed": watch.standing.changes_noticed,
        "duplicates": watch.standing.duplicates,
        "notices_affecting_nothing": watch.standing.affecting_nothing,
        "by_kind": dict(watch.standing.by_kind),
        "breaking_changes": watch.standing.breaking_changes,
        "silent_behaviour_changes": watch.standing.silent_changes,
        "improvements_noticed": watch.standing.improvements_noticed,
        "dependencies_declared": watch.standing.dependencies_declared,
        "change_kinds": list(CHANGE_KINDS),
        "watches_only_for_breakage": False,
        "guesses_which_parts_are_affected": False,
    }


def run_upstream_improvement_watch(
    watch: UpstreamImprovementWatch, control_socket, read_notices, publish_changes,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for job in read_notices():
            outcome = watch.notice(**job)
            if outcome.is_usable:
                publish_changes(outcome.change)

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

    The routing table is computed from the blueprint's own dependency list:
    every part in a block a dependency names is declared as depending on it.
    A research finding becomes a notice when its topic names a declared
    dependency; its kind is read from the statement's own words -- removed,
    renamed or broke reads as breaking, deprecat as deprecation, behaviour
    changed as the silent kind, and anything else as an improvement, because
    a finding about a dependency that names no breakage is news that it got
    better, not a guess that it got worse. Part-health is the wake signal.
    """
    from runtime.input_assembly import Batch
    from runtime.wiring_plan import load_blueprint

    findings = Batch(read=context.bus.reader("research-finding"))
    health = Batch(read=context.bus.reader("part-health"))
    publish_changes = context.bus.publisher_for("upstream-change")

    watch = UpstreamImprovementWatch()
    blueprint = load_blueprint()
    parts_by_block: dict[str, list] = {}
    for feature in blueprint["features"]:
        parts_by_block.setdefault(feature["category"], []).append(feature["id"])
    subjects = set()
    for dependency in blueprint.get("upstream_dependencies", ()):
        subjects.add(dependency["id"])
        for block in dependency.get("used_by", ()):
            for part_id in parts_by_block.get(block, ()):
                watch.declare_dependency(part_id, dependency["id"])

    def kind_of(statement: str) -> str:
        lowered = statement.lower()
        if any(word in lowered for word in ("removed", "renamed", "broke", "breaking")):
            return BREAKING
        if "deprecat" in lowered:
            return DEPRECATION
        if "behaviour changed" in lowered or "behavior changed" in lowered:
            return SILENT_BEHAVIOUR_CHANGE
        return IMPROVEMENT

    def read_notices():
        health.payloads()
        jobs = []
        for finding in findings.payloads():
            if finding.topic not in subjects:
                continue
            jobs.append(
                {
                    "subject": finding.topic,
                    "kind": kind_of(finding.statement),
                    "detail": finding.statement,
                    "source_reference": (
                        finding.source_references[0]
                        if finding.source_references
                        else "unreferenced"
                    ),
                }
            )
        return tuple(jobs)

    return run_upstream_improvement_watch(
        watch=watch,
        control_socket=context.control_socket,
        read_notices=read_notices,
        publish_changes=lambda change: publish_changes((change,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
