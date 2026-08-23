"""skill-version-keeper: every version of every skill, and what changed between them.

A skill that is refreshed silently is a skill whose behaviour changes without
anyone being able to say when or why. Every decision made against version 3 was
made against version 3, and reviewing it against version 7 is the same error as
reviewing a decision against knowledge it never had.

- **Versions are immutable and append-only.** A version that could be edited
  makes the whole record worthless in exactly the case it exists for.
- **Every version names what changed.** Rules added, rules removed, rules whose
  text changed. A list of versions with no diff is a list, not a history.
- **A version that changes nothing is not recorded.** Re-ingesting an identical
  source should not produce a version, or the archive fills with duplicates and
  the diffs become meaningless.
- **A rule that disappears is recorded as removed, never as absent.** A skill
  that quietly loses its strongest anti-pattern in a refresh is the failure this
  catches, and it is invisible from the current version alone.

**Rollback is by promoting an old version**, not by editing the current one. The
old version already exists and was already scored, which makes reverting a
one-step decision rather than a recovery.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "skill-version-keeper"

PART_DECLARATION = PartDeclaration(
    part_id="skill-version-keeper",
    consumes=("skill",),
    produces=("skill-version", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

RECORDED = "recorded"
UNCHANGED = "identical-to-the-current-version"
ROLLED_BACK = "an-earlier-version-was-promoted"


@dataclass(frozen=True)
class SkillVersion:
    """One version of one skill, with what changed from the last."""

    skill_id: str
    version: str
    state: str
    digest: str
    rules_added: tuple
    rules_removed: tuple
    rules_changed: tuple
    anti_patterns_added: tuple
    anti_patterns_removed: tuple
    previous_version: str | None
    is_current: bool
    reason: str
    recorded_at_ns: int

    @property
    def changed_anything(self) -> bool:
        return bool(
            self.rules_added or self.rules_removed or self.rules_changed
            or self.anti_patterns_added or self.anti_patterns_removed
        )

    @property
    def lost_an_anti_pattern(self) -> bool:
        """The failure that is invisible from the current version alone."""
        return bool(self.anti_patterns_removed)


@dataclass
class KeeperStanding:
    versions_offered: int = 0
    versions_recorded: int = 0
    unchanged_skipped: int = 0
    rollbacks: int = 0
    anti_patterns_lost: int = 0
    rules_removed_total: int = 0
    by_skill: dict = field(default_factory=dict)


class SkillVersionKeeper:
    """Keeps every version, names every change, and rolls back by promoting."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._versions: dict[str, list] = {}
        self._contents: dict[tuple[str, str], tuple] = {}
        self._current: dict[str, str] = {}
        self.standing = KeeperStanding()

    def digest_of(self, rules, anti_patterns) -> str:
        joined = "\n".join(sorted(rules) + sorted(anti_patterns))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]

    def record(self, skill) -> tuple[SkillVersion | None, str]:
        """One version. A version that changes nothing is not recorded."""
        self.standing.versions_offered += 1
        skill_id = skill.skill_id
        rules = tuple(skill.decision_rules)
        anti_patterns = tuple(skill.anti_patterns)
        digest = self.digest_of(rules, anti_patterns)

        current_version = self._current.get(skill_id)
        if current_version is not None:
            current = self._version_named(skill_id, current_version)
            if current is not None and current.digest == digest:
                # Re-ingesting an identical source should not produce a version,
                # or the archive fills with duplicates and the diffs stop meaning
                # anything.
                self.standing.unchanged_skipped += 1
                return None, UNCHANGED

        previous_rules, previous_anti = self._contents.get(
            (skill_id, current_version or ""), ((), ())
        )

        added = tuple(sorted(set(rules) - set(previous_rules)))
        removed = tuple(sorted(set(previous_rules) - set(rules)))
        anti_added = tuple(sorted(set(anti_patterns) - set(previous_anti)))
        anti_removed = tuple(sorted(set(previous_anti) - set(anti_patterns)))
        changed = self._changed_rules(previous_rules, rules)

        version = f"v{len(self._versions.get(skill_id, [])) + 1}"
        record = SkillVersion(
            skill_id=skill_id,
            version=version,
            state=RECORDED,
            digest=digest,
            rules_added=added,
            rules_removed=removed,
            rules_changed=changed,
            anti_patterns_added=anti_added,
            anti_patterns_removed=anti_removed,
            previous_version=current_version,
            is_current=True,
            reason=(
                f"{skill_id} {version}: {len(added)} rule(s) added, {len(removed)} removed, "
                f"{len(changed)} changed; {len(anti_added)} anti-pattern(s) added, "
                f"{len(anti_removed)} removed"
                + (
                    f". It lost {len(anti_removed)} anti-pattern(s) in this refresh, which is "
                    f"invisible from the current version alone and is the failure this keeps "
                    f"a record for"
                    if anti_removed
                    else ""
                )
            ),
            recorded_at_ns=self._now_ns(),
        )

        # Append-only: a version that could be edited makes the whole record
        # worthless in exactly the case it exists for.
        self._versions.setdefault(skill_id, []).append(record)
        self._contents[(skill_id, version)] = (rules, anti_patterns)
        self._current[skill_id] = version
        self.standing.versions_recorded += 1
        self.standing.rules_removed_total += len(removed)
        self.standing.by_skill[skill_id] = len(self._versions[skill_id])
        if anti_removed:
            self.standing.anti_patterns_lost += len(anti_removed)
        return record, RECORDED

    def roll_back(self, skill_id: str, to_version: str) -> tuple[SkillVersion | None, str]:
        """Promote an earlier version. It already exists and was already scored."""
        record = self._version_named(skill_id, to_version)
        if record is None:
            return None, f"{skill_id} has no version {to_version}"
        self._current[skill_id] = to_version
        self.standing.rollbacks += 1
        return (
            SkillVersion(
                skill_id=record.skill_id, version=record.version, state=ROLLED_BACK,
                digest=record.digest, rules_added=record.rules_added,
                rules_removed=record.rules_removed, rules_changed=record.rules_changed,
                anti_patterns_added=record.anti_patterns_added,
                anti_patterns_removed=record.anti_patterns_removed,
                previous_version=record.previous_version, is_current=True,
                reason=(
                    f"{to_version} promoted. Rolling back is a one-step decision rather than a "
                    f"recovery, because that version already exists and was already scored"
                ),
                recorded_at_ns=self._now_ns(),
            ),
            ROLLED_BACK,
        )

    def current_version(self, skill_id: str) -> str | None:
        return self._current.get(skill_id)

    def versions_of(self, skill_id: str) -> tuple:
        return tuple(self._versions.get(skill_id, ()))

    def contents_at(self, skill_id: str, version: str) -> tuple:
        return self._contents.get((skill_id, version), ((), ()))

    def _version_named(self, skill_id: str, version: str) -> SkillVersion | None:
        for record in self._versions.get(skill_id, ()):
            if record.version == version:
                return record
        return None

    def _changed_rules(self, previous, current) -> tuple:
        """Rules whose text changed but whose opening words are the same."""
        changed = []
        for rule in current:
            if rule in previous:
                continue
            prefix = " ".join(rule.split()[:4])
            for old in previous:
                if old in current:
                    continue
                if " ".join(old.split()[:4]) == prefix:
                    changed.append(f"{old} -> {rule}")
                    break
        return tuple(changed)


def describe_versions(keeper: SkillVersionKeeper) -> dict:
    return {
        "part_id": PART_ID,
        "versions_offered": keeper.standing.versions_offered,
        "versions_recorded": keeper.standing.versions_recorded,
        "unchanged_skipped": keeper.standing.unchanged_skipped,
        "rollbacks": keeper.standing.rollbacks,
        "anti_patterns_lost_in_refreshes": keeper.standing.anti_patterns_lost,
        "rules_removed_total": keeper.standing.rules_removed_total,
        "by_skill": dict(sorted(keeper.standing.by_skill.items())),
        "versions_can_be_edited": False,
    }


def run_skill_version_keeper(
    keeper: SkillVersionKeeper, control_socket, read_skills, publish_versions,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        versions = []
        for skill in read_skills(keeper):
            version, _ = keeper.record(skill)
            if version is not None:
                versions.append(version)
        publish_versions(tuple(versions))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
