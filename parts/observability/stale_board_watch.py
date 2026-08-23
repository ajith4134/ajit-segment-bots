"""stale-board-watch: alert when the published board is older than its source.

This part exists because of something that actually happened here: the boards
were regenerated on disk and the published links were not, so a person reading
them saw a system with nothing built while eight blocks were built and millions
of records were on the tape. Nothing was broken. Everything was stale.

A stale board is worse than no board, because it is convincing. It reassures at
exactly the moment attention was required, and it does so with the authority of
something that was correct once.

Two different failures, reported separately:

- **The link lags the source.** A snapshot was built and never published.
- **The source itself has stopped.** Nothing is building snapshots at all, so
  the link cannot lag -- and a watch that only compared the two would call that
  perfectly in step.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "stale-board-watch"

PART_DECLARATION = PartDeclaration(
    part_id="stale-board-watch",
    consumes=("board-link", "board-snapshot"),
    produces=("alert", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

IN_STEP = "in-step"
LINK_STALE = "link-behind-its-source"
SOURCE_STOPPED = "no-snapshot-is-being-built"
NEVER_PUBLISHED = "never-published"

SEVERITY_HIGH = "high"


@dataclass(frozen=True)
class StalenessReading:
    """Whether what a person can see matches what the system knows."""

    state: str
    link_digest: str | None
    source_digest: str | None
    link_age_seconds: float | None
    source_age_seconds: float | None
    reason: str
    observed_at_ns: int

    @property
    def is_current(self) -> bool:
        return self.state == IN_STEP


@dataclass(frozen=True)
class Alert:
    source: str
    severity: str
    subject: str
    message: str
    proof: str
    raised_at_ns: int


@dataclass
class WatchStanding:
    checks: int = 0
    stale_links: int = 0
    stopped_sources: int = 0
    alerts_raised: int = 0
    longest_lag_seconds: float = 0.0
    state: str = NEVER_PUBLISHED


class StaleBoardWatch:
    """Compares what is at the link with what was last built, and watches both clocks."""

    def __init__(
        self,
        maximum_lag_seconds: float,
        source_stopped_after_seconds: float,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        if maximum_lag_seconds <= 0 or source_stopped_after_seconds <= 0:
            raise ValueError("both thresholds must be positive to mean anything")
        self._maximum_lag = maximum_lag_seconds
        self._source_stopped_after = source_stopped_after_seconds
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._source_digest: str | None = None
        self._source_at: float | None = None
        self._link_digest: str | None = None
        self._link_at: float | None = None
        self.standing = WatchStanding()

    def observe_snapshot(self, digest: str) -> None:
        self._source_digest = digest
        self._source_at = self._monotonic()

    def observe_link(self, digest: str | None) -> None:
        self._link_digest = digest
        self._link_at = self._monotonic()

    def check(self) -> StalenessReading:
        self.standing.checks += 1
        now = self._monotonic()
        source_age = None if self._source_at is None else now - self._source_at
        link_age = None if self._link_at is None else now - self._link_at

        if self._source_at is None or source_age > self._source_stopped_after:
            # The link cannot lag a source that has stopped, and a watch that
            # only compared the two would call this perfectly in step.
            self.standing.stopped_sources += 1
            self.standing.state = SOURCE_STOPPED
            return self._reading(
                SOURCE_STOPPED, link_age, source_age,
                (
                    "no snapshot has ever been built"
                    if self._source_at is None
                    else f"the last snapshot was built {source_age:.0f}s ago, past "
                    f"{self._source_stopped_after:.0f}s; nothing is measuring this system"
                ),
            )

        if self._link_digest is None:
            self.standing.state = NEVER_PUBLISHED
            return self._reading(
                NEVER_PUBLISHED, link_age, source_age,
                "snapshots are being built and none has ever reached the link; a person looking "
                "at the published board is looking at nothing",
            )

        if self._link_digest != self._source_digest:
            # How long a newer snapshot has existed unpublished -- which is the
            # age of the source, not the difference between the two clocks. The
            # first version compared link_age to source_age and reported a board
            # two hundred seconds stale as in step, in the one part written to
            # catch exactly that.
            lag = source_age
            self.standing.longest_lag_seconds = max(self.standing.longest_lag_seconds, lag)
            if lag > self._maximum_lag:
                self.standing.stale_links += 1
                self.standing.state = LINK_STALE
                return self._reading(
                    LINK_STALE, link_age, source_age,
                    f"the published board is {lag:.0f}s behind the snapshot that exists, past "
                    f"{self._maximum_lag:.0f}s; a stale board is worse than none because it is "
                    f"convincing",
                )

        self.standing.state = IN_STEP
        return self._reading(
            IN_STEP, link_age, source_age,
            f"the link carries the current snapshot, built {source_age:.0f}s ago",
        )

    def alert_for(self, reading: StalenessReading) -> Alert | None:
        if reading.is_current:
            return None
        self.standing.alerts_raised += 1
        return Alert(
            source=PART_ID,
            severity=SEVERITY_HIGH,
            subject=f"the board is {reading.state}",
            message=reading.reason,
            proof=(
                f"link digest {reading.link_digest}, source digest {reading.source_digest}, "
                f"link age {reading.link_age_seconds}, source age {reading.source_age_seconds}"
            ),
            raised_at_ns=self._now_ns(),
        )

    def _reading(self, state, link_age, source_age, reason) -> StalenessReading:
        return StalenessReading(
            state=state, link_digest=self._link_digest, source_digest=self._source_digest,
            link_age_seconds=link_age, source_age_seconds=source_age,
            reason=reason, observed_at_ns=self._now_ns(),
        )


def describe_staleness(watch: StaleBoardWatch) -> dict:
    return {
        "part_id": PART_ID,
        "state": watch.standing.state,
        "checks": watch.standing.checks,
        "stale_links": watch.standing.stale_links,
        "stopped_sources": watch.standing.stopped_sources,
        "alerts_raised": watch.standing.alerts_raised,
        "longest_lag_seconds": watch.standing.longest_lag_seconds,
    }


def run_stale_board_watch(
    watch: StaleBoardWatch, control_socket, read_link_and_snapshot, publish_alerts,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_link_and_snapshot(watch)
        alert = watch.alert_for(watch.check())
        publish_alerts((alert,) if alert is not None else ())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
