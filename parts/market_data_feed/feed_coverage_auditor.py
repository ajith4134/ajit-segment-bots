"""feed-coverage-auditor: per symbol, which venues supply what -- and where none does.

The Rule 8 part of this block. A symbol nothing covers must render as uncovered,
never be absent from the report.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.tape import StreamKind

PART_ID = "feed-coverage-auditor"

PART_DECLARATION = PartDeclaration(
    part_id="feed-coverage-auditor",
    consumes=("market-data", "order-book-snapshot", "symbol-universe", "venue-standing"),
    produces=("feed-coverage", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

COVERED = "covered"
PARTIAL = "partial"
UNCOVERED = "uncovered"

# How long since a stream last delivered before that stream counts as not
# supplying the symbol. Longer than the gap threshold on purpose: a gap is an
# alert about one stream, this is a statement about whether the symbol is
# covered at all, and it should not flap on a single quiet minute.
DEFAULT_COVERAGE_WINDOW_SECONDS = 300.0


@dataclass(frozen=True)
class SymbolCoverage:
    """What is known about one symbol right now, including nothing."""

    symbol: str
    state: str
    venues_by_stream: dict[str, tuple[str, ...]]
    missing_streams: tuple[str, ...]
    expected_venues: tuple[str, ...]
    silent_venues: tuple[str, ...]
    observed_at_ns: int


@dataclass
class CoverageStanding:
    symbols_expected: int = 0
    covered: int = 0
    partial: int = 0
    uncovered: int = 0
    observations: int = 0


class FeedCoverageAuditor:
    """Answers 'which symbols is this system actually seeing, and in what'.

    Built from the expected universe outward rather than from arriving data
    inward. A report assembled only from what arrived can never contain the
    symbol that arrived from nowhere, which is the one worth reporting.
    """

    def __init__(
        self,
        expected_streams: tuple[StreamKind, ...],
        coverage_window_seconds: float = DEFAULT_COVERAGE_WINDOW_SECONDS,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        self._expected_streams = tuple(expected_streams)
        self._window = coverage_window_seconds
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._expected: dict[str, set[str]] = {}
        self._last_seen: dict[tuple[str, str, StreamKind], float] = {}
        self._standing: dict[str, str] = {}
        self.standing = CoverageStanding()

    def expect_symbol(self, symbol: str, venue_id: str) -> None:
        self._expected.setdefault(symbol, set()).add(venue_id)
        self.standing.symbols_expected = len(self._expected)

    def set_venue_standing(self, venue_id: str, state: str) -> None:
        self._standing[venue_id] = state

    def observe(self, venue_id: str, symbol: str, stream_kind: StreamKind) -> None:
        self.standing.observations += 1
        self._last_seen[(venue_id, symbol, stream_kind)] = self._monotonic()

    def audit_symbol(self, symbol: str) -> SymbolCoverage:
        now = self._monotonic()
        expected_venues = tuple(sorted(self._expected.get(symbol, set())))
        venues_by_stream: dict[str, tuple[str, ...]] = {}
        missing: list[str] = []

        for stream_kind in self._expected_streams:
            supplying = tuple(
                sorted(
                    venue_id
                    for venue_id in expected_venues
                    if now - self._last_seen.get((venue_id, symbol, stream_kind), float("-inf"))
                    <= self._window
                )
            )
            venues_by_stream[stream_kind.name] = supplying
            if not supplying:
                missing.append(stream_kind.name)

        silent = tuple(
            venue_id
            for venue_id in expected_venues
            if not any(
                now - self._last_seen.get((venue_id, symbol, kind), float("-inf")) <= self._window
                for kind in self._expected_streams
            )
        )

        if not missing:
            state = COVERED
        elif len(missing) == len(self._expected_streams):
            state = UNCOVERED
        else:
            state = PARTIAL

        return SymbolCoverage(
            symbol=symbol,
            state=state,
            venues_by_stream=venues_by_stream,
            missing_streams=tuple(missing),
            expected_venues=expected_venues,
            silent_venues=silent,
            observed_at_ns=self._now_ns(),
        )

    def audit_all(self) -> tuple[SymbolCoverage, ...]:
        self.standing.covered = 0
        self.standing.partial = 0
        self.standing.uncovered = 0
        reports = []
        for symbol in sorted(self._expected):
            report = self.audit_symbol(symbol)
            if report.state == COVERED:
                self.standing.covered += 1
            elif report.state == PARTIAL:
                self.standing.partial += 1
            else:
                self.standing.uncovered += 1
            reports.append(report)
        return tuple(reports)


def describe_coverage(auditor: FeedCoverageAuditor) -> dict:
    standing = auditor.standing
    return {
        "part_id": PART_ID,
        "symbols_expected": standing.symbols_expected,
        "covered": standing.covered,
        "partial": standing.partial,
        "uncovered": standing.uncovered,
        "observations": standing.observations,
        "expected_streams": [kind.name for kind in auditor._expected_streams],
    }


def run_feed_coverage_auditor(
    auditor: FeedCoverageAuditor, control_socket, read_observations, publish_coverage,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        read_observations(auditor)
        publish_coverage(auditor.audit_all())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
