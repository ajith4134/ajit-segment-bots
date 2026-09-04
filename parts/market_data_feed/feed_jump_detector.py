"""feed-jump-detector: whether a symbol's prices are continuous, bar to bar.

A candle whose open does not meet the prior close is a discontinuity: a stop
sitting in that gap still counts as crossed, so this is a correctness fact for
later phases rather than a data-quality nicety.

**It states continuity in both directions** (2026-09-04). It reported only the
break before, and `paper-fill-simulator` -- the one part that acts on it, by
refusing to fill anything on a flagged symbol -- had no way to learn the break
was over: its `clear_feed_jump` existed with no caller anywhere in the
repository, so a symbol flagged once could never fill again for the life of the
process. On the 2026-09-04 tape 993 of 1,474 streams cross the threshold at
least once, so two thirds of everything tradeable was unfillable within minutes
of a spine start, and nothing reported a fault -- every one of those refusals
was a decision the simulator was entitled to make. Only the return was missing.

**The bound is estimated per symbol** (2026-09-04, same reasoning and the same
rule as `market-anomaly-detector.silence_bound_ns` and
`RollingWindow._gap_bound_seconds`). The stated threshold is a floor; once a
symbol has shown enough of its own bar-to-bar moves, the bound is the larger of
that floor and a multiple of the symbol's own p99 move. The floor alone is a
statement about one market: `feed_jump_threshold_fraction` was fitted to crypto
perpetuals on 2026-08-23 and its own note says to re-measure when the universe
widens. Measured on Upstox `I1` bars for 2026-09-04
(`measurements/2026-09-04-why-no-paper-order-ever-fills/`), NSE_FO option
contracts move p50 0.208% / p95 2.85% / p99 5.69% between one bar's close and
the next bar's open, so **38.1% of them were called a feed artefact**, while
NSE_INDEX never crossed the threshold at all (p99 0.056%). An option's premium
is small and levered: the same move in the underlying is percent-scale in the
contract, and that is a real price move.

Both halves are needed for a fill. Without continuity the bound only delays the
poison; without a fitted bound the symbol re-poisons on the next ordinary tick.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "feed-jump-detector"

PART_DECLARATION = PartDeclaration(
    part_id="feed-jump-detector",
    consumes=("candle",),
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
    """One symbol's price continuity, as of its last closed candle.

    `is_continuous` false is the discontinuity this part is named for; true is
    that discontinuity being over. It is a level and not an event -- a symbol's
    continuity is true until it changes -- which is why both are said.

    The prices, times and the bound that judged them travel with it so a
    consumer refusing to fill can say what it refused on, rather than only that
    something was once wrong with this symbol.
    """

    venue_id: str
    symbol: str
    is_continuous: bool
    previous_close: float
    next_open: float
    gap_fraction: float
    gap_increments: float | None
    bound_fraction: float
    bound_is_the_symbols_own: bool
    previous_close_time_ns: int
    next_open_time_ns: int
    detected_at_ns: int


def continuity_of(jumps) -> tuple:
    """What actually changes about these levels, for the change check.

    Every FeedJump carries the two bar times it compared and they move on every
    bar, so two statements of one unchanged continuity are never equal when
    compared whole -- nothing would ever be skipped and the skip counter would
    read zero while the pacing appeared to be in place. That is the trap this
    project has already been bitten by (2026-08-26), and the fixed
    `without_observation_time` list does not catch it, because a bar time is
    content rather than a note of when this part looked.

    A symbol that jumps again while already discontinuous is not a change: the
    level is the same and the consumer's behaviour is identical. The gap that
    produced it stays in `last_jump` and on the wire when it is published.
    """
    return tuple((jump.venue_id, jump.symbol, jump.is_continuous) for jump in jumps)


@dataclass
class JumpStanding:
    candles_seen: int = 0
    jumps_found: int = 0
    continuity_restored: int = 0
    symbols_tracked: int = 0
    symbols_discontinuous_now: int = 0
    # How many symbols have shown enough of their own moves to be judged against
    # their own rhythm rather than against the stated floor. A symbol earns that
    # by printing, so the busy ones earn it first and the sparse contracts that
    # most need it take longest -- do not read a fresh restart as the mechanism
    # being broken (the lesson of the 2026-09-04 staleness bound).
    symbols_with_a_measured_rhythm: int = 0
    checks_inside_a_widened_bound: int = 0
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
        patience_multiple: float | None = None,
        moves_needed: int = 8,
        moves_remembered: int = 256,
        now_ns=time.time_ns,
    ) -> None:
        if patience_multiple is not None and patience_multiple <= 0:
            raise ValueError(
                "the patience is a positive multiple of a symbol's own p99 bar-to-bar "
                f"move, or None for the stated floor alone; got {patience_multiple!r}"
            )
        if moves_needed < 2:
            raise ValueError(
                "a p99 estimated from fewer than two moves is one move wearing a "
                f"percentile; got {moves_needed!r}"
            )
        if moves_remembered < moves_needed:
            raise ValueError(
                "a symbol cannot be judged against more of its own moves than it is "
                f"allowed to remember; got {moves_remembered!r} remembered and "
                f"{moves_needed!r} needed"
            )
        self._threshold_increments = jump_threshold_increments
        self._threshold_fraction = jump_threshold_fraction
        self._increments = dict(price_increments or {})
        self._patience_multiple = patience_multiple
        self._moves_needed = moves_needed
        self._moves_remembered = moves_remembered
        self._now_ns = now_ns
        self._previous: dict[tuple[str, str], Candle] = {}
        # Each symbol's own recent bar-to-bar moves, bounded so the p99 describes
        # a recent stretch rather than the whole run: an estimate that never
        # forgets lets one early session decide what ordinary looks like forever.
        self._moves: dict[tuple[str, str], deque] = {}
        self._discontinuous: set[tuple[str, str]] = set()
        self.standing = JumpStanding()

    def bound_fraction(self, key, floor_fraction: float) -> tuple[float, bool]:
        """How far this symbol's open may sit from its prior close, right now.

        The stated floor until this symbol has shown enough of its own moves to
        be measured against, then the larger of the floor and a multiple of its
        own p99 move. Returns the bound and whether it is the symbol's own, so a
        judgement made against an estimate is never reported as one made against
        the declared threshold.

        The same rule -- and the same reason -- `RollingWindow._gap_bound_seconds`
        and `market-anomaly-detector.silence_bound_ns` keep: an estimate from a
        handful of observations lets one early stretch decide what ordinary looks
        like forever, so a symbol earns its own bound only by printing enough.
        """
        if self._patience_multiple is None:
            return floor_fraction, False
        moves = self._moves.get(key)
        if moves is None or len(moves) < self._moves_needed:
            return floor_fraction, False
        ordered = sorted(moves)
        p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
        widened = self._patience_multiple * p99
        if widened > floor_fraction:
            self.standing.checks_inside_a_widened_bound += 1
            return widened, True
        return floor_fraction, False

    def set_price_increment(self, venue_id: str, symbol: str, increment: float | None) -> None:
        if increment:
            self._increments[(venue_id, symbol)] = increment

    def observe_closed_candle(self, candle: Candle) -> FeedJump | None:
        """This symbol's continuity as of this bar, or None if it cannot be judged.

        None means no comparison was possible -- the first bar for a symbol, or a
        previous close of zero -- and is not a statement that the symbol is
        continuous. A judgement is returned in both directions, because the
        consumer that stops filling on a break is the same one that needs to know
        the break is over.
        """
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

        # Both thresholds are floors expressed as a fraction of price, so one
        # comparison judges every symbol. Where a tick is declared the floor is
        # that many ticks at this price, because a one-tick difference is the
        # market and not a jump and a tick is worth a different fraction on every
        # symbol; where none is, the stated fraction stands and `gap_increments`
        # is None, so an inferred judgement is never mistaken for a declared one.
        floor_fraction = (
            self._threshold_increments * increment / previous.close_price
            if increment
            else self._threshold_fraction
        )
        bound, bound_is_the_symbols_own = self.bound_fraction(key, floor_fraction)
        crossed = fraction > bound

        self.standing.largest_gap_fraction = max(self.standing.largest_gap_fraction, fraction)

        # A move is remembered only when it is one the symbol was allowed to
        # make. Feeding a break back into the estimate teaches the symbol that
        # breaks are ordinary, and a feed that gaps repeatedly would widen its
        # own bound until nothing could ever be a jump again.
        if not crossed:
            moves = self._moves.get(key)
            if moves is None:
                moves = self._moves[key] = deque(maxlen=self._moves_remembered)
            had_enough = len(moves) >= self._moves_needed
            moves.append(fraction)
            if not had_enough and len(moves) >= self._moves_needed:
                self.standing.symbols_with_a_measured_rhythm += 1

        was_discontinuous = key in self._discontinuous
        if crossed:
            self._discontinuous.add(key)
            self.standing.jumps_found += 1
        else:
            self._discontinuous.discard(key)
            if was_discontinuous:
                self.standing.continuity_restored += 1
        self.standing.symbols_discontinuous_now = len(self._discontinuous)

        jump = FeedJump(
            venue_id=candle.venue_id,
            symbol=candle.symbol,
            is_continuous=not crossed,
            previous_close=previous.close_price,
            next_open=candle.open_price,
            gap_fraction=fraction,
            gap_increments=increments,
            bound_fraction=bound,
            bound_is_the_symbols_own=bound_is_the_symbols_own,
            previous_close_time_ns=previous.open_time_ns,
            next_open_time_ns=candle.open_time_ns,
            detected_at_ns=self._now_ns(),
        )
        if crossed:
            self.standing.last_jump = jump
        return jump


def describe_jumps(detector: FeedJumpDetector, levels=None) -> dict:
    """This part's standing, and what its level publisher actually did.

    `unchanged_levels_skipped` is on health deliberately: a change check whose
    skip count reads zero is a change check doing nothing, and it looks exactly
    like one that is working. That is not hypothetical -- it is how the first
    version of `runtime/level_publishing.py` behaved against two payloads that
    carried a timestamp, and the tests caught it only because they asserted the
    skip count rather than the absence of a crash.
    """
    standing = detector.standing
    published = {} if levels is None else {
        "levels_published": levels.standing.publishes,
        "unchanged_levels_skipped": levels.standing.unchanged_publishes_skipped,
        "level_refreshes": levels.standing.refreshes,
        "level_changes": levels.standing.changes,
        "symbols_held": levels.keys_held,
    }
    return {
        **published,
        "part_id": PART_ID,
        "candles_seen": standing.candles_seen,
        "symbols_tracked": standing.symbols_tracked,
        "jumps_found": standing.jumps_found,
        "continuity_restored": standing.continuity_restored,
        "symbols_discontinuous_now": standing.symbols_discontinuous_now,
        "symbols_with_a_measured_rhythm": standing.symbols_with_a_measured_rhythm,
        "checks_inside_a_widened_bound": standing.checks_inside_a_widened_bound,
        "largest_gap_fraction": standing.largest_gap_fraction,
        "last_jump": standing.last_jump.__dict__ if standing.last_jump else None,
    }


def run_feed_jump_detector(
    detector: FeedJumpDetector, control_socket, read_closed_candles, publish_jump,
    health_interval_seconds: float, emit_health,
    restatement_interval_seconds: float,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    """The part's loop, paced the same way `start_part` paces it.

    `restatement_interval_seconds` has no default on purpose. Continuity is
    judged on every closed candle and most of those judgements say what was
    already true, so an unpaced publisher here would restate one level per
    symbol per bar for every symbol -- the storm shape of 2026-08-26, in the one
    path nothing currently calls. A caller has to say how often an unchanged
    level may be repeated, because there is no interval that is right by default.
    """
    from runtime.level_publishing import LevelPublisherByKey

    continuity = LevelPublisherByKey(
        publish=publish_jump,
        refresh_interval_seconds=restatement_interval_seconds,
        identity_of=continuity_of,
    )

    def tick() -> None:
        for candle in read_closed_candles():
            jump = detector.observe_closed_candle(candle)
            if jump is not None:
                continuity.publish_level((jump.venue_id, jump.symbol), (jump,))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_jumps(detector),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    `candle` carries candles only since 2026-08-25; this part reads the
    candles and only the closed ones, since a jump is a closed bar's open
    against the previous closed bar's close and an open bar has no close yet.
    A trade on the same type is not an error, it is simply not a candle.
    """
    from runtime.input_assembly import Batch
    from runtime.venues.venue_adapter import NormalisedCandle

    from runtime.level_publishing import LevelPublisherByKey

    updates = Batch(read=context.bus.reader("candle"))
    detector = FeedJumpDetector(
        jump_threshold_increments=context.number("feed_jump_threshold_increments"),
        jump_threshold_fraction=context.number("feed_jump_threshold_fraction"),
        patience_multiple=context.number("feed_jump_patience_multiple"),
        moves_needed=int(context.number("feed_jump_moves_needed")),
        moves_remembered=int(context.number("feed_jump_moves_remembered")),
    )
    # Continuity is a level per symbol, so one symbol breaking does not restate
    # the other 487 (2026-08-26). Keyed on the symbol for that reason, and
    # compared on `continuity_of` because every FeedJump carries the bar times it
    # judged, which move on every bar -- compared whole nothing would ever be
    # skipped, and the skip counter would say so while reading zero.
    continuity = LevelPublisherByKey(
        publish=context.bus.publisher_for("feed-jump"),
        refresh_interval_seconds=context.number("feed_jump_restatement_interval_seconds"),
        identity_of=continuity_of,
    )

    def tick() -> None:
        for update in updates.payloads():
            if not isinstance(update, NormalisedCandle) or not update.is_closed:
                continue
            jump = detector.observe_closed_candle(
                Candle(
                    venue_id=update.venue_id,
                    symbol=update.symbol,
                    open_time_ns=update.open_time_ns,
                    open_price=update.open,
                    close_price=update.close,
                    high_price=update.high,
                    low_price=update.low,
                )
            )
            if jump is not None:
                continuity.publish_level((jump.venue_id, jump.symbol), (jump,))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_jumps(detector, continuity),
    )
