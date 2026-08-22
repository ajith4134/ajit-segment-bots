"""feed-jump-detector: a candle whose open does not meet the prior close.

A stop sitting in that gap still counts as crossed, so this is a correctness
fact for later phases rather than a data-quality nicety.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "feed-jump-detector"

PART_DECLARATION = PartDeclaration(
    part_id="feed-jump-detector",
    consumes=("market-data",),
    produces=("feed-jump", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)


@dataclass(frozen=True)
class Candle:
    """One closed candle, in the vocabulary this part reasons in."""

    venue_id: str
    symbol: str
    open_time_ns: int
    open_price: float
    close_price: float
    high_price: float
    low_price: float


@dataclass(frozen=True)
class FeedJump:
    """A discontinuity between one candle's close and the next one's open."""

    venue_id: str
    symbol: str
    previous_close: float
    next_open: float
    gap_fraction: float
    gap_increments: float | None
    previous_close_time_ns: int
    next_open_time_ns: int
    detected_at_ns: int


@dataclass
class JumpStanding:
    candles_seen: int = 0
    jumps_found: int = 0
    symbols_tracked: int = 0
    largest_gap_fraction: float = 0.0
    last_jump: FeedJump | None = None


class FeedJumpDetector:
    """Compares each closed candle's open against the previous close.

    The threshold is in price increments where the symbol declares one, because
    a one-tick difference is the market and not a jump, and a tick is worth a
    different fraction on every symbol. Where no increment is known the fraction
    threshold is used and that is reported, so an inferred judgement is never
    mistaken for a declared one.
    """

    def __init__(
        self,
        jump_threshold_increments: float,
        jump_threshold_fraction: float,
        price_increments: dict[tuple[str, str], float] | None = None,
        now_ns=time.time_ns,
    ) -> None:
        self._threshold_increments = jump_threshold_increments
        self._threshold_fraction = jump_threshold_fraction
        self._increments = dict(price_increments or {})
        self._now_ns = now_ns
        self._previous: dict[tuple[str, str], Candle] = {}
        self.standing = JumpStanding()

    def set_price_increment(self, venue_id: str, symbol: str, increment: float | None) -> None:
        if increment:
            self._increments[(venue_id, symbol)] = increment

    def observe_closed_candle(self, candle: Candle) -> FeedJump | None:
        self.standing.candles_seen += 1
        key = (candle.venue_id, candle.symbol)
        previous = self._previous.get(key)
        self._previous[key] = candle
        self.standing.symbols_tracked = len(self._previous)
        if previous is None or previous.close_price <= 0:
            return None

        difference = abs(candle.open_price - previous.close_price)
        fraction = difference / previous.close_price
        increment = self._increments.get(key)
        increments = difference / increment if increment else None

        if increments is not None:
            crossed = increments > self._threshold_increments
        else:
            crossed = fraction > self._threshold_fraction
        self.standing.largest_gap_fraction = max(self.standing.largest_gap_fraction, fraction)
        if not crossed:
            return None

        jump = FeedJump(
            venue_id=candle.venue_id,
            symbol=candle.symbol,
            previous_close=previous.close_price,
            next_open=candle.open_price,
            gap_fraction=fraction,
            gap_increments=increments,
            previous_close_time_ns=previous.open_time_ns,
            next_open_time_ns=candle.open_time_ns,
            detected_at_ns=self._now_ns(),
        )
        self.standing.jumps_found += 1
        self.standing.last_jump = jump
        return jump


def describe_jumps(detector: FeedJumpDetector) -> dict:
    standing = detector.standing
    return {
        "part_id": PART_ID,
        "candles_seen": standing.candles_seen,
        "symbols_tracked": standing.symbols_tracked,
        "jumps_found": standing.jumps_found,
        "largest_gap_fraction": standing.largest_gap_fraction,
        "last_jump": standing.last_jump.__dict__ if standing.last_jump else None,
    }


def run_feed_jump_detector(
    detector: FeedJumpDetector, control_socket, read_closed_candles, publish_jump,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        for candle in read_closed_candles():
            jump = detector.observe_closed_candle(candle)
            if jump is not None:
                publish_jump(jump)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
