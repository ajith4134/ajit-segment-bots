"""knowledge-snapshot-versioner: what the system knew at the moment it decided.

A decision made last month was made against last month's knowledge, and by the
time anyone reviews it the knowledge has changed. Reviewing a past decision
against present knowledge is how a system concludes it was obviously wrong when
it was entirely reasonable -- and then changes something that was working.

So the state of the knowledge base is versioned, and a decision records which
version it was made against:

- **A snapshot is a manifest, not a copy.** Copying every fact for every decision
  is unaffordable; a content hash per store plus the identifiers that changed is
  enough to answer "was this fact in place then" and cheap enough to take often.
- **Snapshots are immutable and append-only.** A snapshot that could be edited
  would make the whole record worthless in exactly the case it exists for.
- **Every snapshot names what changed since the last one.** That is what turns a
  list of versions into a history somebody can read.
- **A decision without a snapshot is recorded as unreviewable.** It is a real
  state and a common one, and pretending the current knowledge applies is the
  error this part prevents.

**Snapshots are taken on change, not on a timer.** A timer either misses the
change that mattered or fills the archive with identical versions.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "knowledge-snapshot-versioner"

PART_DECLARATION = PartDeclaration(
    part_id="knowledge-snapshot-versioner",
    consumes=("semantic-fact", "playbook-rule", "instruction-history", "available-skill"),
    produces=("knowledge-snapshot", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

TAKEN = "taken"
UNCHANGED = "nothing-changed-since-the-last-snapshot"
UNREVIEWABLE = "this-decision-was-made-against-no-recorded-snapshot"

FACTS = "semantic-facts"
RULES = "playbook-rules"
INSTRUCTIONS = "instruction-history"
SKILLS = "available-skills"

STORES = (FACTS, RULES, INSTRUCTIONS, SKILLS)


@dataclass(frozen=True)
class KnowledgeSnapshot:
    """What the system knew at one moment, as a manifest rather than a copy."""

    version: str
    state: str
    digests: dict
    counts: dict
    added: dict
    removed: dict
    changed: dict
    previous_version: str | None
    reason: str
    taken_at_ns: int

    @property
    def was_taken(self) -> bool:
        return self.state == TAKEN

    def contained(self, store: str, identifier: str) -> bool | None:
        """Whether one thing was in place at this version, when that is recorded."""
        added = self.added.get(store, ())
        removed = self.removed.get(store, ())
        if identifier in removed:
            return False
        if identifier in added:
            return True
        return None


@dataclass
class VersionerStanding:
    snapshots_taken: int = 0
    snapshots_skipped_unchanged: int = 0
    decisions_stamped: int = 0
    unreviewable_decisions: int = 0
    largest_change: int = 0
    by_store: dict = field(default_factory=dict)


class KnowledgeSnapshotVersioner:
    """Versions the knowledge base on change, and stamps decisions with the version."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._contents: dict[str, set] = {store: set() for store in STORES}
        self._snapshots: list = []
        self._decision_versions: dict[str, str] = {}
        self.standing = VersionerStanding()

    def observe_contents(self, store: str, identifiers) -> None:
        """What one store holds now. Compared against the last snapshot to find changes."""
        if store not in STORES:
            raise ValueError(f"{store!r} is not one of {', '.join(STORES)}")
        self._contents[store] = set(identifiers)

    def digest_of(self, store: str) -> str:
        """A content hash: enough to say the store changed, cheap enough to take often."""
        joined = "\n".join(sorted(self._contents.get(store, ())))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]

    def take(self) -> KnowledgeSnapshot:
        """One snapshot, taken on change rather than on a timer."""
        digests = {store: self.digest_of(store) for store in STORES}
        previous = self._snapshots[-1] if self._snapshots else None

        if previous is not None and previous.digests == digests:
            # A timer either misses the change that mattered or fills the
            # archive with identical versions.
            self.standing.snapshots_skipped_unchanged += 1
            return KnowledgeSnapshot(
                version=previous.version, state=UNCHANGED, digests=digests,
                counts={store: len(self._contents[store]) for store in STORES},
                added={}, removed={}, changed={},
                previous_version=previous.previous_version,
                reason=(
                    f"nothing has changed since {previous.version}, so no new version is "
                    f"recorded"
                ),
                taken_at_ns=self._now_ns(),
            )

        added = {}
        removed = {}
        changed = {}
        for store in STORES:
            before = self._snapshot_contents(previous, store)
            now = self._contents[store]
            store_added = tuple(sorted(now - before))
            store_removed = tuple(sorted(before - now))
            if store_added:
                added[store] = store_added
            if store_removed:
                removed[store] = store_removed
            if store_added or store_removed:
                changed[store] = len(store_added) + len(store_removed)
                self.standing.by_store[store] = self.standing.by_store.get(store, 0) + 1

        total_changed = sum(changed.values())
        self.standing.largest_change = max(self.standing.largest_change, total_changed)

        version = f"knowledge-{len(self._snapshots) + 1:06d}"
        snapshot = KnowledgeSnapshot(
            version=version,
            state=TAKEN,
            digests=digests,
            counts={store: len(self._contents[store]) for store in STORES},
            added=added,
            removed=removed,
            changed=changed,
            previous_version=previous.version if previous else None,
            reason=(
                f"{version}: {total_changed} change(s) across "
                f"{len(changed)} store(s)"
                + (
                    "; " + ", ".join(f"{store} {count}" for store, count in sorted(changed.items()))
                    if changed
                    else ""
                )
                + ". A manifest rather than a copy: enough to answer whether a fact was in "
                "place then, and cheap enough to take on every change"
            ),
            taken_at_ns=self._now_ns(),
        )

        # Append-only: a snapshot that could be edited makes the whole record
        # worthless in exactly the case it exists for.
        self._snapshots.append(snapshot)
        self.standing.snapshots_taken += 1
        return snapshot

    def stamp_decision(self, decision_id: str) -> str | None:
        """Record which version a decision was made against."""
        if not self._snapshots:
            self.standing.unreviewable_decisions += 1
            return None
        version = self._snapshots[-1].version
        self._decision_versions[decision_id] = version
        self.standing.decisions_stamped += 1
        return version

    def knowledge_behind(self, decision_id: str) -> tuple:
        """The snapshot a decision was made against, so it is reviewed against that."""
        version = self._decision_versions.get(decision_id)
        if version is None:
            return None, (
                f"{decision_id} was made against no recorded snapshot, so it cannot be "
                f"reviewed fairly. Reviewing it against present knowledge is how a system "
                f"concludes a reasonable decision was obviously wrong, and then changes "
                f"something that was working"
            )
        for snapshot in self._snapshots:
            if snapshot.version == version:
                return snapshot, f"{decision_id} was made against {version}"
        return None, f"{version} is no longer in the archive"

    def history(self) -> tuple:
        return tuple(self._snapshots)

    def _snapshot_contents(self, snapshot, store: str) -> set:
        """What a store held at a snapshot, rebuilt from the change lists."""
        if snapshot is None:
            return set()
        contents: set = set()
        for recorded in self._snapshots:
            contents |= set(recorded.added.get(store, ()))
            contents -= set(recorded.removed.get(store, ()))
            if recorded.version == snapshot.version:
                break
        return contents


def describe_snapshots(versioner: KnowledgeSnapshotVersioner) -> dict:
    return {
        "part_id": PART_ID,
        "snapshots_taken": versioner.standing.snapshots_taken,
        "snapshots_skipped_unchanged": versioner.standing.snapshots_skipped_unchanged,
        "decisions_stamped": versioner.standing.decisions_stamped,
        "unreviewable_decisions": versioner.standing.unreviewable_decisions,
        "largest_change": versioner.standing.largest_change,
        "by_store": dict(sorted(versioner.standing.by_store.items())),
        "stores": list(STORES),
        "snapshots_can_be_edited": False,
    }


def run_knowledge_snapshot_versioner(
    versioner: KnowledgeSnapshotVersioner, control_socket, read_stores, publish_snapshots,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_stores(versioner)
        publish_snapshots((versioner.take(),))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
