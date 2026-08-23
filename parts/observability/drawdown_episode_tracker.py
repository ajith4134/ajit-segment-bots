"""drawdown-episode-tracker: each drawdown from peak through recovery, as its own record.

A single "worst drawdown" number hides everything that matters about how a
strategy loses. Two systems with the same 20% worst drawdown are completely
different businesses if one recovered in a day and the other took four months --
and only the second one gets turned off by a human.

So each episode is a record: when it started, how deep it went, how long it has
been going, and whether it ever recovered. An episode still open is reported as
open with its duration so far, because the drawdown a person needs to know about
is the one happening now, not the worst one in history.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "drawdown-episode-tracker"

PART_DECLARATION = PartDeclaration(
    part_id="drawdown-episode-tracker",
    consumes=("account-balance",),
    produces=("drawdown-episode", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

OPEN = "open"
RECOVERED = "recovered"


@dataclass(frozen=True)
class DrawdownEpisode:
    """One fall from a peak, and its life so far."""

    episode_id: int
    state: str
    peak_equity: float
    trough_equity: float
    current_equity: float
    depth_fraction: float
    started_at_ns: int
    trough_at_ns: int
    recovered_at_ns: int | None
    duration_seconds: float
    time_to_trough_seconds: float
    recovery_seconds: float | None

    @property
    def is_open(self) -> bool:
        return self.state == OPEN


@dataclass
class TrackerStanding:
    observations: int = 0
    episodes_started: int = 0
    episodes_recovered: int = 0
    deepest_fraction: float = 0.0
    longest_seconds: float = 0.0
    current_peak: float = 0.0
    open_episode: bool = False


class DrawdownEpisodeTracker:
    """Opens an episode when equity falls from a peak and closes it when it recovers."""

    def __init__(
        self, minimum_depth_fraction: float, monotonic=time.monotonic, now_ns=time.time_ns
    ) -> None:
        if not 0.0 <= minimum_depth_fraction < 1.0:
            raise ValueError("the minimum depth is a fraction of the peak in [0, 1)")
        self._minimum_depth = minimum_depth_fraction
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._peak: float | None = None
        self._episodes: list[dict] = []
        self._open: dict | None = None
        self.standing = TrackerStanding()

    def observe_equity(self, equity: float) -> DrawdownEpisode | None:
        """One equity reading; returns the open episode if there is one."""
        self.standing.observations += 1

        if self._peak is None or equity > self._peak:
            recovered = self._close_open_episode(equity)
            self._peak = equity
            self.standing.current_peak = equity
            return recovered

        depth = (self._peak - equity) / self._peak if self._peak > 0 else 0.0
        if depth < self._minimum_depth:
            return self._open_episode_record() if self._open else None

        now_monotonic = self._monotonic()
        if self._open is None:
            self._open = {
                "id": len(self._episodes) + 1,
                "peak": self._peak,
                "trough": equity,
                "started_monotonic": now_monotonic,
                "started_ns": self._now_ns(),
                "trough_monotonic": now_monotonic,
                "trough_ns": self._now_ns(),
            }
            self.standing.episodes_started += 1
            self.standing.open_episode = True
        elif equity < self._open["trough"]:
            self._open["trough"] = equity
            self._open["trough_monotonic"] = now_monotonic
            self._open["trough_ns"] = self._now_ns()

        self.standing.deepest_fraction = max(self.standing.deepest_fraction, depth)
        self.standing.longest_seconds = max(
            self.standing.longest_seconds, now_monotonic - self._open["started_monotonic"]
        )
        return self._open_episode_record(equity)

    def _close_open_episode(self, equity: float) -> DrawdownEpisode | None:
        if self._open is None:
            return None
        episode = self._open
        self._open = None
        self.standing.open_episode = False
        self.standing.episodes_recovered += 1
        now_monotonic = self._monotonic()
        record = DrawdownEpisode(
            episode_id=episode["id"],
            state=RECOVERED,
            peak_equity=episode["peak"],
            trough_equity=episode["trough"],
            current_equity=equity,
            depth_fraction=(episode["peak"] - episode["trough"]) / episode["peak"],
            started_at_ns=episode["started_ns"],
            trough_at_ns=episode["trough_ns"],
            recovered_at_ns=self._now_ns(),
            duration_seconds=now_monotonic - episode["started_monotonic"],
            time_to_trough_seconds=episode["trough_monotonic"] - episode["started_monotonic"],
            recovery_seconds=now_monotonic - episode["trough_monotonic"],
        )
        self._episodes.append(episode)
        return record

    def _open_episode_record(self, equity: float | None = None) -> DrawdownEpisode | None:
        if self._open is None:
            return None
        episode = self._open
        current = equity if equity is not None else episode["trough"]
        now_monotonic = self._monotonic()
        return DrawdownEpisode(
            episode_id=episode["id"],
            state=OPEN,
            peak_equity=episode["peak"],
            trough_equity=episode["trough"],
            current_equity=current,
            depth_fraction=(episode["peak"] - episode["trough"]) / episode["peak"],
            started_at_ns=episode["started_ns"],
            trough_at_ns=episode["trough_ns"],
            recovered_at_ns=None,
            duration_seconds=now_monotonic - episode["started_monotonic"],
            time_to_trough_seconds=episode["trough_monotonic"] - episode["started_monotonic"],
            recovery_seconds=None,
        )

    @property
    def open_episode(self) -> DrawdownEpisode | None:
        return self._open_episode_record()


def describe_drawdowns(tracker: DrawdownEpisodeTracker) -> dict:
    return {
        "part_id": PART_ID,
        "observations": tracker.standing.observations,
        "episodes_started": tracker.standing.episodes_started,
        "episodes_recovered": tracker.standing.episodes_recovered,
        "open_episode": tracker.standing.open_episode,
        "deepest_fraction": tracker.standing.deepest_fraction,
        "longest_seconds": tracker.standing.longest_seconds,
        "current_peak": tracker.standing.current_peak,
    }


def run_drawdown_episode_tracker(
    tracker: DrawdownEpisodeTracker, control_socket, read_equity, publish_episode,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for equity in read_equity():
            episode = tracker.observe_equity(equity)
            if episode is not None:
                publish_episode(episode)

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
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    balances = Batch(read=context.bus.reader("account-balance"))
    publish_episodes = context.bus.publisher_for("drawdown-episode")
    segment = str(context.setting("segment_id").value)
    tracker = DrawdownEpisodeTracker(minimum_depth_fraction=context.number("drawdown_episode_minimum_depth"))

    def read_equity():
        return tuple(
            balance.equity for balance in balances.payloads()
            if getattr(balance, "segment", None) == segment
        )

    return run_drawdown_episode_tracker(
        tracker=tracker,
        control_socket=context.control_socket,
        read_equity=read_equity,
        publish_episode=lambda episode: publish_episodes((episode,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
