"""board-publisher: push the snapshot to the link a person can open.

The board only exists once someone can see it. This project's own history is the
argument: four boards sat correct on disk and two days stale at the published
link, and the person looking at them was reading a system that no longer existed.

So publishing is **verified, not assumed**. A push that returned without error is
not a published board; the publisher records the digest it sent and the link it
sent to, so `stale-board-watch` can compare what is live against what was built.

A failed publish keeps the previous link and says so. Dropping the link on a
transient failure would leave a person with nothing at exactly the moment the
system was having trouble.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "board-publisher"

PART_DECLARATION = PartDeclaration(
    part_id="board-publisher",
    consumes=("board-snapshot",),
    produces=("board-link", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

PUBLISHED = "published"
UNCHANGED = "unchanged-not-republished"
FAILED = "failed"
NEVER_PUBLISHED = "never-published"


@dataclass(frozen=True)
class BoardLink:
    """Where the board is, what is at it, and when that arrived."""

    url: str | None
    state: str
    snapshot_digest: str | None
    snapshot_built_at_ns: int | None
    published_at_ns: int | None
    reason: str

    @property
    def is_live(self) -> bool:
        return self.state in (PUBLISHED, UNCHANGED) and self.url is not None


@dataclass
class PublisherStanding:
    publishes: int = 0
    unchanged_skipped: int = 0
    failures: int = 0
    last_failure: str | None = None
    last_published_at_ns: int | None = None
    live_digest: str | None = None


class BoardPublisher:
    """Publishes a changed snapshot and records exactly what is live at the link."""

    def __init__(self, url: str, publish, now_ns=time.time_ns) -> None:
        if not url:
            raise ValueError("a publisher with no url has nowhere to publish to")
        self._url = url
        self._publish = publish
        self._now_ns = now_ns
        self._live_digest: str | None = None
        self._live_built_at: int | None = None
        self.standing = PublisherStanding()

    def digest_of(self, snapshot) -> str:
        """A digest over what the board actually shows, not over when it was built.

        Built-at is excluded deliberately: a snapshot rebuilt with identical
        content is the same board, and republishing it would make every board
        look freshly changed and hide the ones that really did.
        """
        body = "|".join(
            f"{tile.label}:{tile.state}:{tile.value}:{tile.proof}" for tile in snapshot.tiles
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def publish_snapshot(self, snapshot) -> BoardLink:
        digest = self.digest_of(snapshot)

        if digest == self._live_digest:
            self.standing.unchanged_skipped += 1
            return self._link(
                UNCHANGED, digest, snapshot.built_at_ns,
                "the board is unchanged since the last publish; republishing it would hide "
                "which boards really did change",
            )

        try:
            self._publish(self._url, snapshot)
        except Exception as failure:
            self.standing.failures += 1
            self.standing.last_failure = f"{type(failure).__name__}: {failure}"
            return self._link(
                FAILED, self._live_digest, self._live_built_at,
                f"the publish failed ({type(failure).__name__}: {failure}); the previous board "
                f"is still at the link and is now older than its source",
            )

        self._live_digest = digest
        self._live_built_at = snapshot.built_at_ns
        self.standing.publishes += 1
        self.standing.last_published_at_ns = self._now_ns()
        self.standing.live_digest = digest
        return self._link(
            PUBLISHED, digest, snapshot.built_at_ns,
            f"published {len(snapshot.tiles)} tiles to {self._url}",
        )

    @property
    def live_digest(self) -> str | None:
        """What is actually at the link, for the staleness watch to compare against."""
        return self._live_digest

    def _link(self, state, digest, built_at, reason) -> BoardLink:
        return BoardLink(
            url=self._url if state != NEVER_PUBLISHED else None,
            state=state,
            snapshot_digest=digest,
            snapshot_built_at_ns=built_at,
            published_at_ns=self.standing.last_published_at_ns,
            reason=reason,
        )


def describe_publishing(publisher: BoardPublisher) -> dict:
    return {
        "part_id": PART_ID,
        "url": publisher._url,
        "publishes": publisher.standing.publishes,
        "unchanged_skipped": publisher.standing.unchanged_skipped,
        "failures": publisher.standing.failures,
        "last_failure": publisher.standing.last_failure,
        "last_published_at_ns": publisher.standing.last_published_at_ns,
        "live_digest": publisher.standing.live_digest,
    }


def run_board_publisher(
    publisher: BoardPublisher, control_socket, read_snapshot, publish_link,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        snapshot = read_snapshot()
        if snapshot is not None:
            publish_link(publisher.publish_snapshot(snapshot))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
