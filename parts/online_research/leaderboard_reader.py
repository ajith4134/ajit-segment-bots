"""leaderboard-reader: who the venue says is winning, read as a claim not a fact.

A public leaderboard is the most misleading data source this system touches,
because it is real, it is published by the exchange, and it is selected. The
selection is what breaks naive use of it:

- **It is survivorship all the way down.** Everyone shown is there because they
  won recently. The people who took the same risk and lost are not on a list
  anywhere, so the ranking silently conditions on the outcome.
- **The window is chosen by the venue.** A 7-day board ranks by a week. Somebody
  at the top of a 7-day board can be down over 30 days, and often is, because the
  fastest way onto a 7-day board is leverage that eventually resolves the other
  way.
- **Ranking is by return, and return is a ratio.** A trader up 900% on 200 USDT
  outranks one up 40% on 4 million. The first cannot be copied at size and the
  second can.
- **Visibility is opt-in and the opt-in is strategic.** A trader who makes their
  positions public has a reason. Sometimes it is vanity, sometimes it is that
  being followed into an illiquid position is profitable for them.

So this part reads the board and writes down who was on it, with the window and
the rank kept attached. It does not rank, does not decide that a high rank means
skill, and does not drop the traders who fell off -- **a trader who was on the
board last week and is not on it this week is the most informative row on it**,
and a reader that only keeps the current page destroys exactly that evidence.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.external_research_types import (
    COMPLETE, PARTIAL, TrackedTrader, UNAVAILABLE, VENUE_PUBLISHED,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "leaderboard-reader"

PART_DECLARATION = PartDeclaration(
    part_id="leaderboard-reader",
    consumes=(),
    produces=("tracked-trader", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

READ = "read"
RATE_LIMITED = "rate-limited"
READ_FAILED = "read-failed"
EMPTY_BOARD = "the-board-returned-nobody"

# Why a trader stopped appearing. The distinction matters: falling off a board is
# evidence about the trader, while the board vanishing is evidence about the read.
FELL_OFF = "fell-off-the-board"
BOARD_UNAVAILABLE = "the-board-itself-could-not-be-read"


@dataclass(frozen=True)
class BoardRead:
    """One read of one board over one window."""

    venue_id: str
    period_days: int
    state: str
    traders: tuple
    disappeared: tuple
    rows_seen: int
    rows_claimed: int | None
    completeness: str
    reason: str
    read_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == READ and bool(self.traders)


@dataclass
class ReaderStanding:
    reads_attempted: int = 0
    reads_succeeded: int = 0
    rate_limited: int = 0
    failures: int = 0
    traders_first_seen: int = 0
    traders_that_fell_off: int = 0
    partial_reads: int = 0
    hidden_traders: int = 0


class LeaderboardReader:
    """Reads public leaderboards and records who was on them, with the window."""

    def __init__(
        self,
        reads_per_window: int,
        window_seconds: float,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        if reads_per_window < 1:
            raise ValueError("a reader allowed zero reads reads nothing")
        if window_seconds <= 0:
            raise ValueError("the rate-limit window is a positive number of seconds")
        self._reads_per_window = reads_per_window
        self._window_seconds = window_seconds
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._read_times: dict[str, list] = {}
        self._first_seen: dict[str, int] = {}
        # Per (venue, period): who was on the board the last time it was read.
        self._last_board: dict[tuple, set] = {}
        self._read_board = None
        self.standing = ReaderStanding()

    def install_reader(self, read_board) -> None:
        """`read_board(venue_id, period_days) -> (rows, rows_claimed)`.

        A row is a mapping with at least an identity reference; rank, return and
        visibility are optional because venues differ in what they publish, and a
        missing field is carried as None rather than filled in.
        """
        self._read_board = read_board

    def read(self, venue_id: str, period_days: int) -> BoardRead:
        self.standing.reads_attempted += 1
        if self._read_board is None:
            raise RuntimeError(
                "no reader is installed: this part does not know how to reach a venue, "
                "which is deliberate -- the venue adapter belongs outside it"
            )
        if not self._may_read(venue_id):
            self.standing.rate_limited += 1
            return self._read_result(
                venue_id, period_days, RATE_LIMITED, (), (), 0, None, UNAVAILABLE,
                f"{self._reads_per_window} read(s) per {self._window_seconds:.0f}s already "
                f"spent on {venue_id}. Retrying now is what turns a rate limit into a ban",
            )

        self._read_times.setdefault(venue_id, []).append(self._monotonic())
        try:
            rows, rows_claimed = self._read_board(venue_id, period_days)
        except Exception as failure:  # the venue is outside; it fails how it likes
            self.standing.failures += 1
            return self._read_result(
                venue_id, period_days, READ_FAILED, (), (), 0, None, UNAVAILABLE,
                f"the board could not be read ({type(failure).__name__}). Nothing is "
                f"concluded from a failed read -- an empty board and an unread board are "
                f"different facts",
            )

        rows = tuple(rows or ())
        if not rows:
            return self._read_result(
                venue_id, period_days, EMPTY_BOARD, (), (), 0, rows_claimed, UNAVAILABLE,
                "the board returned nobody. That is a fact about the read, not evidence "
                "that nobody is trading well",
            )

        now = self._now_ns()
        traders = tuple(self._as_trader(venue_id, period_days, row, now) for row in rows)
        completeness = (
            COMPLETE if rows_claimed is None or len(rows) >= rows_claimed else PARTIAL
        )
        if completeness == PARTIAL:
            self.standing.partial_reads += 1

        key = (venue_id, period_days)
        present = {trader.identity_reference for trader in traders}
        disappeared = tuple(sorted(self._last_board.get(key, set()) - present))
        # Only conclude a disappearance from a complete read: a truncated page
        # would otherwise report the whole tail of the board as having fallen off.
        if completeness != COMPLETE:
            disappeared = ()
        self.standing.traders_that_fell_off += len(disappeared)
        self._last_board[key] = present

        self.standing.reads_succeeded += 1
        self.standing.hidden_traders += sum(
            1 for trader in traders if not trader.can_be_read_further
        )
        return self._read_result(
            venue_id, period_days, READ, traders, disappeared, len(rows), rows_claimed,
            completeness,
            f"{len(rows)} row(s) over {period_days}d"
            + (f", {len(disappeared)} gone since the last read" if disappeared else "")
            + ". The ranking is kept as the venue's claim, not adopted as a judgement",
        )

    def who_fell_off(self, venue_id: str, period_days: int) -> tuple:
        """The rows that are no longer there, which is where the losses live."""
        return tuple(sorted(self._last_board.get((venue_id, period_days), set())))

    def first_seen_at_ns(self, identity_reference: str) -> int | None:
        return self._first_seen.get(identity_reference)

    def _as_trader(self, venue_id, period_days, row, now_ns) -> TrackedTrader:
        identity = str(row["identity_reference"])
        if identity not in self._first_seen:
            self._first_seen[identity] = now_ns
            self.standing.traders_first_seen += 1
        return TrackedTrader(
            trader_id=f"{venue_id}:{identity}",
            venue_id=venue_id,
            identity_reference=identity,
            source_kind=VENUE_PUBLISHED,
            rank=row.get("rank"),
            period_days=period_days,
            reported_return=row.get("reported_return"),
            is_public_by_choice=bool(row.get("is_public", False)),
            first_seen_at_ns=self._first_seen[identity],
            observed_at_ns=now_ns,
        )

    def _may_read(self, venue_id: str) -> bool:
        now = self._monotonic()
        recent = [
            stamp
            for stamp in self._read_times.get(venue_id, [])
            if now - stamp < self._window_seconds
        ]
        self._read_times[venue_id] = recent
        return len(recent) < self._reads_per_window

    def _read_result(
        self, venue_id, period_days, state, traders, disappeared, rows_seen,
        rows_claimed, completeness, reason,
    ) -> BoardRead:
        return BoardRead(
            venue_id=venue_id, period_days=period_days, state=state, traders=traders,
            disappeared=disappeared, rows_seen=rows_seen, rows_claimed=rows_claimed,
            completeness=completeness, reason=reason, read_at_ns=self._now_ns(),
        )


def describe_leaderboard_reading(reader: LeaderboardReader) -> dict:
    return {
        "part_id": PART_ID,
        "reads_attempted": reader.standing.reads_attempted,
        "reads_succeeded": reader.standing.reads_succeeded,
        "rate_limited": reader.standing.rate_limited,
        "failures": reader.standing.failures,
        "traders_first_seen": reader.standing.traders_first_seen,
        "traders_that_fell_off": reader.standing.traders_that_fell_off,
        "partial_reads": reader.standing.partial_reads,
        "traders_who_hide_their_positions": reader.standing.hidden_traders,
        "ranks_traders_itself": False,
        "treats_rank_as_skill": False,
    }


def run_leaderboard_reader(
    reader: LeaderboardReader, control_socket, boards_to_read, publish_traders,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for venue_id, period_days in boards_to_read():
            result = reader.read(venue_id, period_days)
            if result.is_usable:
                publish_traders(result)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_leaderboard_reading(reader),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    No board reader is installed on this box, so no board is read and
    nothing is published; `install_reader` is the one way one gets in, and
    the read budget applies from then. This part consumes nothing, so it
    ticks on its health interval alone.
    """
    publish_traders = context.bus.publisher_for("tracked-trader")
    reader = LeaderboardReader(
        reads_per_window=int(context.number("research_fetches_per_window")),
        window_seconds=context.number("research_fetch_window_seconds"),
    )

    def publish(result) -> None:
        if result is not None and result.traders:
            publish_traders(tuple(result.traders))

    return run_leaderboard_reader(
        reader=reader,
        control_socket=context.control_socket,
        boards_to_read=lambda: (),
        publish_traders=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
