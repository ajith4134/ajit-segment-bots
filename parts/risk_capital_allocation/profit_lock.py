"""profit-lock: raise the stop as a position gains, so a pullback keeps some of it.

The failure this exists for is the one that does not show up as a loss. A trade
that goes 5% in favour and closes flat is recorded as a scratch, and nothing in
the equity curve distinguishes it from a trade that never moved. It was a winner
that was given back.

Locking is a trade-off, not free: a stop raised too eagerly is stopped out by
ordinary noise on the way to a bigger move, and that also does not show up as a
loss -- it shows up as a small win where a large one was available.

So the pullback allowed is **learned from this symbol's own excursions** (RL-060):
how far winning trades on this symbol ordinarily retrace before continuing. A
stop trailing closer than that harvests noise; one trailing further gives back
more than it needs to.

Three rules that make it safe:

- **The stop only ever moves in the profitable direction.** A trailing stop that
  could move against the position is not a stop.
- **Nothing is locked before the position is ahead by more than the round trip
  costs**, or the lock guarantees a loss after fees.
- **The break-even step is separate** and comes first, because moving to
  break-even is the one adjustment with no downside beyond the noise stop-out.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.level_publishing import LevelPublisherByKey
from runtime.price_frames import levels_in
from runtime.learned_estimator import Estimate, QuantileEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import LONG, SHORT

PART_ID = "profit-lock"

PART_DECLARATION = PartDeclaration(
    part_id="profit-lock",
    consumes=("position", "symbol-price-frame", "excursion-profile"),
    produces=("stop-adjustment", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

HELD = "held"
MOVED_TO_BREAK_EVEN = "moved-to-break-even"
TRAILED = "trailed"
NOT_YET_PROFITABLE = "not-yet-past-costs"

# The quantile of ordinary retracements the trail must sit beyond. A trail inside
# this harvests the noise of trades that would have continued.
RETRACEMENT_QUANTILE = 0.80


@dataclass(frozen=True)
class StopAdjustment:
    """A new stop for an open position, or the reason it stayed where it was."""

    venue_id: str
    symbol: str
    direction: str
    entry_price: float
    current_price: float
    previous_stop: float
    new_stop: float
    outcome: str
    gain_fraction: float
    locked_fraction: float
    retracement_estimate: Estimate
    reason: str
    adjusted_at_ns: int

    @property
    def did_move(self) -> bool:
        return self.outcome in (MOVED_TO_BREAK_EVEN, TRAILED)


@dataclass
class LockStanding:
    adjustments: int = 0
    moved_to_break_even: int = 0
    trailed: int = 0
    held: int = 0
    retracements_learned: int = 0
    most_locked_fraction: float = 0.0


class ProfitLock:
    """Trails a stop behind the best price by this symbol's own ordinary retracement."""

    def __init__(
        self,
        break_even_trigger_fraction: float,
        round_trip_cost_fraction: float,
        prior_retracement_fraction: float,
        maximum_trail_fraction: float,
        minimum_observations: int,
        window: int,
        now_ns=time.time_ns,
    ) -> None:
        if break_even_trigger_fraction <= round_trip_cost_fraction:
            raise ValueError(
                "moving to break-even before the trade covers its costs locks in a loss"
            )
        self._break_even_trigger = break_even_trigger_fraction
        self._costs = round_trip_cost_fraction
        self._prior_retracement = prior_retracement_fraction
        self._maximum_trail = maximum_trail_fraction
        self._minimum_observations = minimum_observations
        self._window = window
        self._now_ns = now_ns
        self._retracements: dict[tuple[str, str], QuantileEstimator] = {}
        self._best_price: dict[tuple[str, str], float] = {}
        self.standing = LockStanding()

    def observe_retracement(self, venue_id: str, symbol: str, fraction: float) -> None:
        """How far a winning trade on this symbol pulled back before continuing."""
        self._estimator_for((venue_id, symbol)).observe(abs(fraction))
        self.standing.retracements_learned += 1

    def observe_position_closed(self, venue_id: str, symbol: str) -> None:
        self._best_price.pop((venue_id, symbol), None)

    def adjust(
        self,
        venue_id: str,
        symbol: str,
        direction: str,
        entry_price: float,
        current_price: float,
        current_stop: float,
    ) -> StopAdjustment:
        self.standing.adjustments += 1
        key = (venue_id, symbol)
        is_long = direction == LONG

        best = self._best_price.get(key)
        best = current_price if best is None else (max(best, current_price) if is_long else min(best, current_price))
        self._best_price[key] = best

        gain = (current_price - entry_price) / entry_price
        if not is_long:
            gain = -gain
        estimate = self._retracement_estimate(key)

        if gain <= self._costs:
            return self._adjustment(
                venue_id, symbol, direction, entry_price, current_price, current_stop,
                current_stop, NOT_YET_PROFITABLE, gain, estimate,
                f"{gain:.2%} ahead, inside the {self._costs:.2%} round-trip cost; locking now "
                f"would guarantee a loss after fees",
            )

        trail = min(estimate.value, self._maximum_trail)
        trailed_stop = best * (1 - trail) if is_long else best * (1 + trail)
        break_even = entry_price * (1 + self._costs) if is_long else entry_price * (1 - self._costs)

        candidate = trailed_stop
        outcome = TRAILED
        if gain >= self._break_even_trigger and (
            (is_long and break_even > trailed_stop) or (not is_long and break_even < trailed_stop)
        ):
            candidate = break_even
            outcome = MOVED_TO_BREAK_EVEN

        # Never against the position. A trailing stop that could retreat is not a stop.
        improves = candidate > current_stop if is_long else candidate < current_stop
        if not improves:
            self.standing.held += 1
            return self._adjustment(
                venue_id, symbol, direction, entry_price, current_price, current_stop,
                current_stop, HELD, gain, estimate,
                f"the trail sits at {candidate:g}, no better than the stop already at {current_stop:g}",
            )

        locked = (candidate - entry_price) / entry_price
        if not is_long:
            locked = -locked
        self.standing.most_locked_fraction = max(self.standing.most_locked_fraction, locked)
        if outcome == MOVED_TO_BREAK_EVEN:
            self.standing.moved_to_break_even += 1
        else:
            self.standing.trailed += 1

        return self._adjustment(
            venue_id, symbol, direction, entry_price, current_price, current_stop,
            candidate, outcome, gain, estimate,
            f"{gain:.2%} ahead; stop to {candidate:g}, locking {locked:.2%}, trailing "
            f"{trail:.2%} behind the best price of {best:g} "
            f"({'measured' if estimate.is_fitted else 'the operator prior'})",
        )

    def _retracement_estimate(self, key) -> Estimate:
        return self._estimator_for(key).estimate(
            RETRACEMENT_QUANTILE, self._minimum_observations,
            bound_low=0.0, bound_high=self._maximum_trail,
        )

    def _estimator_for(self, key) -> QuantileEstimator:
        estimator = self._retracements.get(key)
        if estimator is None:
            estimator = QuantileEstimator(window=self._window, prior=self._prior_retracement)
            self._retracements[key] = estimator
        return estimator

    def _adjustment(
        self, venue_id, symbol, direction, entry, current, previous_stop, new_stop,
        outcome, gain, estimate, reason
    ) -> StopAdjustment:
        locked = (new_stop - entry) / entry
        if direction != LONG:
            locked = -locked
        return StopAdjustment(
            venue_id=venue_id, symbol=symbol, direction=direction, entry_price=entry,
            current_price=current, previous_stop=previous_stop, new_stop=new_stop,
            outcome=outcome, gain_fraction=gain, locked_fraction=locked,
            retracement_estimate=estimate, reason=reason, adjusted_at_ns=self._now_ns(),
        )


def describe_profit_lock(lock: ProfitLock) -> dict:
    fitted = sum(1 for key in lock._retracements if lock._retracement_estimate(key).is_fitted)
    return {
        "part_id": PART_ID,
        "adjustments": lock.standing.adjustments,
        "moved_to_break_even": lock.standing.moved_to_break_even,
        "trailed": lock.standing.trailed,
        "held": lock.standing.held,
        "retracements_learned": lock.standing.retracements_learned,
        "symbols_with_a_fitted_trail": fitted,
        "most_locked_fraction": lock.standing.most_locked_fraction,
    }


def decide_every_stop(lock: ProfitLock, positions) -> tuple[StopAdjustment, ...]:
    """Where every open position's stop is now, whether or not this tick moved it.

    A stop is a level. It was published only when it moved, and a position under
    water never moves its stop, so nothing downstream was ever told where the
    stops were: measured on the live spine at 15:34 on 2026-08-26, 140,924
    adjustments decided, 0 published, stop-order-manager holding 0 stops against
    12 open positions, and exposure-limiter -- which counts a position with no
    stop at its full notional -- reading the book at 199% of a 5% cap and allowing
    nothing new on any symbol.

    `did_move` is still on the payload and still means what it said; what changed
    is that it is no longer what decides whether anybody hears about the stop.
    """
    return tuple(lock.adjust(**position) for position in positions)


def run_profit_lock(
    lock: ProfitLock, control_socket, read_positions, publish_adjustments,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        # Every open position's stop, not only the ones that moved. Where a
        # position's stop is, is a level: it stays true until something moves it,
        # and the parts downstream need it whether or not this tick changed it.
        #
        # Measured on the live spine at 15:34 on 2026-08-26: 140,924 adjustments
        # decided and none published, because none had moved -- the positions were
        # all under water, so every one of them held. stop-order-manager had
        # placed 0 stops against 12 open positions, and exposure-limiter, which
        # counts a position with no stop at its full notional, read the book at
        # 199% of a 5% cap and allowed nothing new anywhere.
        publish_adjustments(decide_every_stop(lock, read_positions()))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_profit_lock(lock),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The stop this part trails from is the last one it published for the
    position, and before it has published one, the widest stop the risk
    settings allow below the entry -- the most conservative assumption, and
    safe because stop-order-manager never widens a stop, so an adjustment
    computed from a stop wider than the real one can only be a tightening or
    a hold. Retracements are learned from the excursion profiles the
    profilers measure, as their adverse excursion.
    """
    from runtime.input_assembly import Batch, LatestByKey

    positions = LatestByKey(read=context.bus.reader("position"), key_of=lambda p: (p.venue_id, p.symbol))
    trades = Batch(read=context.bus.reader("symbol-price-frame"))
    profiles = Batch(read=context.bus.reader("excursion-profile"))
    publish_adjustments = context.bus.publisher_for("stop-adjustment")
    widest_stop = context.number("risk_maximum_stop_fraction")
    lock = ProfitLock(
        break_even_trigger_fraction=context.number("profit_lock_break_even_trigger"),
        round_trip_cost_fraction=context.number("reference_price_materiality_fraction"),
        prior_retracement_fraction=context.number("profit_lock_prior_retracement"),
        maximum_trail_fraction=context.number("profit_lock_maximum_trail"),
        minimum_observations=int(context.number("profit_lock_minimum_observations")),
        window=int(context.number("profit_lock_window")),
    )
    prices: dict[tuple[str, str], float] = {}
    stops: dict[tuple[str, str], float] = {}

    def read_positions():
        for trade in levels_in(trades.payloads()):
                prices[(trade.venue_id, trade.symbol)] = trade.price
        for profile in profiles.payloads():
            adverse = getattr(profile, "adverse_excursion", None)
            if adverse is None:
                adverse = getattr(profile, "median_adverse", None)
            if adverse is not None and adverse > 0:
                lock.observe_retracement(profile.venue_id, profile.symbol, float(adverse))
        requests = []
        for key, position in positions.mapping().items():
            if position.quantity == 0:
                if key in stops:
                    stops.pop(key, None)
                    lock.observe_position_closed(*key)
                continue
            price = prices.get(key)
            if price is None:
                continue
            direction = LONG if position.quantity > 0 else SHORT
            entry = position.average_entry_price
            if key not in stops:
                stops[key] = entry * (1.0 - widest_stop) if direction == LONG else entry * (1.0 + widest_stop)
            requests.append({
                "venue_id": key[0], "symbol": key[1], "direction": direction,
                "entry_price": entry, "current_price": price, "current_stop": stops[key],
            })
        return tuple(requests)

    stated = LevelPublisherByKey(
        publish=publish_adjustments,
        refresh_interval_seconds=context.number("level_refresh_interval_seconds"),
        # What makes two statements the same standing stop: where it is, and why
        # it is there. Deliberately not the whole payload -- `current_price` and
        # `gain_fraction` move on every print, so comparing those would republish
        # a stop that has not moved on every tick, which is the storm this shape
        # exists to prevent.
        identity_of=lambda items: tuple(
            (item.venue_id, item.symbol, item.direction, item.new_stop, item.outcome)
            for item in items
        ),
    )

    def publish(adjustments) -> None:
        for adjustment in adjustments:
            stops[(adjustment.venue_id, adjustment.symbol)] = adjustment.new_stop
            # Per position, so one symbol's stop moving does not restate the other
            # eleven, and so a position whose stop is holding still gets said
            # again on the refresh rather than going silent.
            stated.publish_level((adjustment.venue_id, adjustment.symbol), (adjustment,))

    return run_profit_lock(
        lock=lock,
        control_socket=context.control_socket,
        read_positions=read_positions,
        publish_adjustments=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
