"""stop-target-placer: put the initial stop away from where the crowd's stops are.

The crowd's liquidation levels are not incidental to where price goes -- they are
where it is *pulled*. A cluster of leveraged longs at a level is a pool of forced
selling waiting to be triggered, and price reaches for it. A stop placed inside
that pool is not a stop, it is a donation.

So placement is: start from what volatility says the trade needs to breathe, then
**move away from the nearest liquidation cluster** rather than through it, then
check the result is still worth taking.

The distance is learned where the evidence exists (RL-060). `excursion-profile`
records how far trades on this symbol actually went against themselves before
working out, and a stop inside that distance is one that gets hit by the ordinary
noise of a trade that would have won. Until there is enough of that history the
volatility forecast stands in, and the part says which it used.

**A stop is never widened past what the risk limit can pay for.** A wider stop is
a smaller position, not a bigger loss -- that is the sizer's job -- but a stop so
wide the minimum tradeable size breaches the limit means the trade cannot be
taken, and saying so is better than taking it with a stop that is decorative.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, QuantileEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import BUY, LONG, SELL

PART_ID = "stop-target-placer"

PART_DECLARATION = PartDeclaration(
    part_id="stop-target-placer",
    consumes=(
        "trade-intent", "volatility-forecast", "liquidation-map", "symbol-profile",
        "stop-audit", "excursion-profile", "bull-exit-plan", "bear-exit-plan", "tail-exit-plan",
    ),
    produces=("stop-target-plan", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PLACED = "placed"
MOVED_CLEAR_OF_CLUSTER = "moved-clear-of-liquidation-cluster"
REFUSED_NO_DISTANCE = "refused-no-volatility-or-history"
REFUSED_REWARD_TOO_THIN = "refused-reward-does-not-justify-risk"

# The quantile of adverse excursions a stop must sit beyond. High, because a stop
# inside the ordinary noise of a winning trade converts winners into losers, and
# that failure is invisible in the equity curve -- it looks like a bad strategy.
ADVERSE_EXCURSION_QUANTILE = 0.85


@dataclass(frozen=True)
class StopTargetPlan:
    """Where the stop and target go, and what put them there."""

    venue_id: str
    symbol: str
    side: str
    entry_price: float
    stop_price: float | None
    target_price: float | None
    outcome: str
    stop_distance_fraction: float | None
    reward_to_risk: float | None
    nearest_cluster_price: float | None
    distance_estimate: Estimate | None
    reason: str
    planned_at_ns: int

    @property
    def is_placeable(self) -> bool:
        return self.outcome in (PLACED, MOVED_CLEAR_OF_CLUSTER) and self.stop_price is not None


@dataclass
class PlacerStanding:
    plans: int = 0
    moved_clear: int = 0
    refused_no_distance: int = 0
    refused_thin_reward: int = 0
    excursions_learned: int = 0
    symbols_fitted: int = 0
    widest_stop_fraction: float = 0.0


class StopTargetPlacer:
    """Places a stop beyond ordinary adverse movement and clear of liquidation pools."""

    def __init__(
        self,
        minimum_reward_to_risk: float,
        cluster_clearance_fraction: float,
        maximum_stop_fraction: float,
        minimum_observations: int,
        window: int,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_reward_to_risk <= 0:
            raise ValueError("a trade must be allowed to aim for more than nothing")
        self._minimum_reward = minimum_reward_to_risk
        self._clearance = cluster_clearance_fraction
        self._maximum_stop = maximum_stop_fraction
        self._minimum_observations = minimum_observations
        self._window = window
        self._now_ns = now_ns
        self._excursions: dict[tuple[str, str], QuantileEstimator] = {}
        self._clusters: dict[tuple[str, str], tuple[float, ...]] = {}
        self.standing = PlacerStanding()

    def observe_adverse_excursion(self, venue_id: str, symbol: str, fraction: float) -> None:
        """How far a trade on this symbol went against itself before working out."""
        self._estimator_for((venue_id, symbol), fraction).observe(abs(fraction))
        self.standing.excursions_learned += 1

    def set_liquidation_clusters(self, venue_id: str, symbol: str, prices: tuple[float, ...]) -> None:
        """Where the crowd's forced exits sit, from the liquidation map upstream."""
        self._clusters[(venue_id, symbol)] = tuple(sorted(prices))

    def place(
        self,
        venue_id: str,
        symbol: str,
        side: str,
        entry_price: float,
        volatility_forecast: float | None,
        target_price: float | None = None,
        proposed_stop_distance_fraction: float | None = None,
    ) -> StopTargetPlan:
        """Where risk puts the stop, given what the bot proposed and what is known.

        `proposed_stop_distance_fraction` is the distance the bot's own exit plan
        arrived at, as a fraction of entry. Risk does not adopt it and does not
        ignore it: it is used only when this part has neither enough measured
        excursions nor a volatility forecast, it is capped by `maximum_stop`
        exactly as any other distance is, and it is still moved clear of any
        liquidation cluster it sits inside. That is the division the blueprint
        draws by having this part consume `bull-exit-plan` -- the bot says where
        it thinks the trade is wrong, and risk says where a stop may go.
        """
        self.standing.plans += 1
        key = (venue_id, symbol)
        estimate = self._distance_estimate(key, volatility_forecast, proposed_stop_distance_fraction)

        if estimate is None:
            self.standing.refused_no_distance += 1
            return self._plan(
                venue_id, symbol, side, entry_price, None, None, REFUSED_NO_DISTANCE,
                None, None, None, None,
                "neither a volatility forecast nor enough excursion history to place a stop",
            )

        distance = min(estimate.value, self._maximum_stop)
        stop = entry_price * (1 - distance) if side == BUY else entry_price * (1 + distance)

        if stop <= 0 or (side == BUY and stop >= entry_price) or (
            side != BUY and stop <= entry_price
        ):
            # A stop that does not sit beyond the entry is not a stop, whatever
            # produced the distance. Refused here rather than passed on, because
            # the sizer downstream can only report it as an untradeable order and
            # by then the decision has already been made.
            self.standing.refused_no_distance += 1
            return self._plan(
                venue_id, symbol, side, entry_price, None, None, REFUSED_NO_DISTANCE,
                None, None, None, estimate,
                f"the distance measured for {symbol} puts the stop at {stop:.8g} against an "
                f"entry of {entry_price:.8g}, which is not beyond it",
            )

        moved, nearest = self._clear_of_clusters(key, side, entry_price, stop)
        outcome = PLACED
        if moved != stop:
            outcome = MOVED_CLEAR_OF_CLUSTER
            self.standing.moved_clear += 1
            stop = moved
            distance = abs(entry_price - stop) / entry_price

        self.standing.widest_stop_fraction = max(self.standing.widest_stop_fraction, distance)

        reward_to_risk = None
        if target_price is not None:
            reward = abs(target_price - entry_price)
            risk = abs(entry_price - stop)
            reward_to_risk = reward / risk if risk > 0 else None
            if reward_to_risk is not None and reward_to_risk < self._minimum_reward:
                self.standing.refused_thin_reward += 1
                return self._plan(
                    venue_id, symbol, side, entry_price, stop, target_price,
                    REFUSED_REWARD_TOO_THIN, distance, reward_to_risk, nearest, estimate,
                    f"reward-to-risk of {reward_to_risk:.2f} is below the {self._minimum_reward:.2f} "
                    f"this trade must clear to be worth its stop",
                )

        return self._plan(
            venue_id, symbol, side, entry_price, stop, target_price, outcome,
            distance, reward_to_risk, nearest, estimate,
            f"stop {distance:.2%} away, from "
            f"{'measured excursions' if estimate.is_fitted else 'the volatility forecast'}"
            + (f"; moved clear of the cluster at {nearest:g}" if outcome == MOVED_CLEAR_OF_CLUSTER else ""),
        )

    def _distance_estimate(
        self, key, volatility_forecast: float | None,
        proposed_stop_distance_fraction: float | None = None,
    ) -> Estimate | None:
        """Measured excursions first, then a forecast, then the bot's own proposal.

        In that order, and the order is the point. A measured excursion quantile
        is what this symbol has actually done; a volatility forecast is a model's
        view of the same thing; the bot's proposal is the least independent of the
        three, because the bot also decided to take the trade. It is used because
        the alternative on a cold system is no stop at all, and a position with no
        stop is the one thing risk exists to prevent -- but it never displaces a
        measurement, and it is marked unfitted so nothing downstream reads it as one.
        """
        estimator = self._excursions.get(key)
        if estimator is not None:
            estimate = estimator.estimate(
                ADVERSE_EXCURSION_QUANTILE, self._minimum_observations,
                bound_low=0.0, bound_high=self._maximum_stop,
            )
            if estimate.is_fitted:
                return estimate
        observations = estimator.observations if estimator else 0
        if volatility_forecast is not None and volatility_forecast > 0:
            return Estimate(
                value=min(volatility_forecast, self._maximum_stop),
                is_fitted=False,
                observations=observations,
                prior=volatility_forecast,
                was_clamped=volatility_forecast > self._maximum_stop,
                bound_low=0.0,
                bound_high=self._maximum_stop,
                reason="the volatility forecast, until this symbol has enough excursion history",
            )
        if proposed_stop_distance_fraction is not None and proposed_stop_distance_fraction > 0:
            return Estimate(
                value=min(proposed_stop_distance_fraction, self._maximum_stop),
                is_fitted=False,
                observations=observations,
                prior=proposed_stop_distance_fraction,
                was_clamped=proposed_stop_distance_fraction > self._maximum_stop,
                bound_low=0.0,
                bound_high=self._maximum_stop,
                reason=(
                    "the distance the bot's own exit plan arrived at, capped by the maximum "
                    "stop; no excursion history and no volatility forecast exist for this "
                    "symbol yet, and the alternative to this is a position with no stop"
                ),
            )
        return None

    def _clear_of_clusters(self, key, side, entry_price, stop) -> tuple[float, float | None]:
        """Move a stop that sits inside a liquidation pool to the far side of it.

        Beyond rather than short of: a stop placed just before the pool is hit
        first by the very move the pool causes.
        """
        clusters = self._clusters.get(key, ())
        if not clusters:
            return stop, None

        low, high = (min(stop, entry_price), max(stop, entry_price))
        inside = [price for price in clusters if low <= price <= high]
        if not inside:
            return stop, None

        if side == BUY:
            nearest = min(inside)
            return nearest * (1 - self._clearance), nearest
        nearest = max(inside)
        return nearest * (1 + self._clearance), nearest

    def _estimator_for(self, key, prior: float) -> QuantileEstimator:
        estimator = self._excursions.get(key)
        if estimator is None:
            estimator = QuantileEstimator(window=self._window, prior=abs(prior))
            self._excursions[key] = estimator
        return estimator

    def _plan(
        self, venue_id, symbol, side, entry, stop, target, outcome,
        distance, reward_to_risk, nearest, estimate, reason
    ) -> StopTargetPlan:
        return StopTargetPlan(
            venue_id=venue_id, symbol=symbol, side=side, entry_price=entry,
            stop_price=stop, target_price=target, outcome=outcome,
            stop_distance_fraction=distance, reward_to_risk=reward_to_risk,
            nearest_cluster_price=nearest, distance_estimate=estimate,
            reason=reason, planned_at_ns=self._now_ns(),
        )


def describe_placement(placer: StopTargetPlacer) -> dict:
    fitted = sum(
        1
        for key, estimator in placer._excursions.items()
        if estimator.estimate(ADVERSE_EXCURSION_QUANTILE, placer._minimum_observations).is_fitted
    )
    return {
        "part_id": PART_ID,
        "plans": placer.standing.plans,
        "moved_clear_of_clusters": placer.standing.moved_clear,
        "refused_no_distance": placer.standing.refused_no_distance,
        "refused_thin_reward": placer.standing.refused_thin_reward,
        "excursions_learned": placer.standing.excursions_learned,
        "symbols_with_a_fitted_distance": fitted,
        "widest_stop_fraction": placer.standing.widest_stop_fraction,
    }


def run_stop_target_placer(
    placer: StopTargetPlacer, control_socket, read_intents, publish_plans,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        publish_plans(tuple(placer.place(**intent) for intent in read_intents()))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Risk's own answer to where the stop goes, which is not the bot's. The bot says
    where its reason for the trade stops being true; this part caps that distance,
    moves it clear of any liquidation pool it sits inside, and refuses a trade
    whose reward does not justify the risk it would take.

    **Where the entry price comes from.** A `trade-intent` deliberately carries no
    price -- naming one would be an order decision made by a part that does not
    make orders. The exit plan carries both the stop and `risk_fraction`, the
    fraction of entry that stop sits below, so the entry it was computed against
    is `stop_price / (1 - risk_fraction)` for a long and the mirror for a short.
    Recovered rather than assumed, and it agrees with the price the bot used to
    within the tick the stop was rounded to.

    **One target, not the plan's ladder.** The exit plan names several targets with
    fractions to scale out at, and the nearest is used here for the whole quantity.
    Scaling out needs one resting order per rung and something to size each rung
    against the quantity still held; `participation-capped-order-splitter` is the
    part for that and it is not running. Taking the nearest target for everything
    closes earlier than the plan intends -- it understates what a winner makes,
    which is the direction an unbuilt part should err in.
    """
    from runtime.input_assembly import Batch, LatestByKey

    intents = Batch(read=context.bus.reader("trade-intent"))
    bull_plans = LatestByKey(
        read=context.bus.reader("bull-exit-plan"),
        key_of=lambda plan: (plan.venue_id, plan.symbol),
    )
    bear_plans = LatestByKey(
        read=context.bus.reader("bear-exit-plan"),
        key_of=lambda plan: (plan.venue_id, plan.symbol),
    )
    tail_plans = LatestByKey(
        read=context.bus.reader("tail-exit-plan"),
        key_of=lambda plan: (plan.venue_id, plan.symbol),
    )
    forecasts = LatestByKey(
        read=context.bus.reader("volatility-forecast"),
        key_of=lambda forecast: (forecast.venue_id, forecast.symbol),
    )
    maps = Batch(read=context.bus.reader("liquidation-map"))
    profiles = Batch(read=context.bus.reader("symbol-profile"))
    audits = Batch(read=context.bus.reader("stop-audit"))
    excursions = Batch(read=context.bus.reader("excursion-profile"))
    publish_plans = context.bus.publisher_for("stop-target-plan")

    placer = StopTargetPlacer(
        # Risk's own gate, not the bull bot's. A limit that read the bot's
        # setting would be the bot checking itself.
        minimum_reward_to_risk=context.number("risk_minimum_reward_to_risk"),
        cluster_clearance_fraction=context.number("stop_cluster_clearance_fraction"),
        maximum_stop_fraction=context.number("risk_maximum_stop_fraction"),
        minimum_observations=int(context.number("stop_excursion_minimum_observations")),
        window=int(context.number("stop_excursion_window")),
    )

    def entry_price_of(plan) -> float | None:
        """The price the bot's stop was computed against, recovered from the plan."""
        if not plan.risk_fraction or plan.risk_fraction >= 1.0:
            return None
        if plan.side in (LONG, BUY):
            return plan.stop_price / (1.0 - plan.risk_fraction)
        return plan.stop_price / (1.0 + plan.risk_fraction)

    def read_intents():
        for cluster_map in maps.payloads():
            placer.set_liquidation_clusters(
                cluster_map.venue_id, cluster_map.symbol, cluster_map.cluster_prices
            )
        for profile in excursions.payloads():
            # Only a fitted profile carries a number. An unfitted one reports
            # `adverse_excursion = 0.0` because it has nothing to report -- and
            # feeding those zeros into the quantile estimator taught it that this
            # symbol never moves against a winner, which put the stop on the entry
            # price and made every order untradeable. Measured on the live run of
            # 2026-08-23 08:34: the bull bot's first real decision to open a trade
            # died here, with "a buy stop at 0.065531 is on the wrong side of an
            # entry at 0.065531".
            if not profile.is_fitted:
                continue
            placer.observe_adverse_excursion(
                profile.venue_id, profile.symbol, profile.adverse_excursion
            )
        # Declared, drained, and not yet used: `symbol-profile` and `stop-audit`
        # have no producer running, and a stop widened by an audit nobody made
        # would be a stop widened by nothing.
        profiles.payloads()
        audits.payloads()

        by_symbol = {}
        for mapping in (tail_plans.mapping(), bear_plans.mapping(), bull_plans.mapping()):
            by_symbol.update(mapping)
        forecast_by_symbol = forecasts.mapping()

        requests = []
        for intent in intents.payloads():
            key = (intent.venue_id, intent.symbol)
            plan = by_symbol.get(key)
            if plan is None:
                # No bot published an exit plan for this intent, so there is no
                # entry price to place a stop against. Skipped rather than
                # guessed: the alternative is a stop placed around a price this
                # part invented.
                continue
            entry_price = entry_price_of(plan)
            if entry_price is None:
                continue
            targets = tuple(sorted(
                (target.price for target in plan.targets),
                reverse=intent.is_short,
            ))
            forecast = forecast_by_symbol.get(key)
            requests.append({
                "venue_id": intent.venue_id,
                "symbol": intent.symbol,
                "side": BUY if intent.is_long else SELL,
                "entry_price": entry_price,
                "volatility_forecast": getattr(forecast, "expected_move_fraction", None),
                "target_price": targets[0] if targets else None,
                "proposed_stop_distance_fraction": plan.risk_fraction,
            })
        return tuple(requests)

    return run_stop_target_placer(
        placer=placer,
        control_socket=context.control_socket,
        read_intents=read_intents,
        publish_plans=publish_plans,
        health_interval_seconds=context.health_interval_seconds,
        emit_health=context.emit_health,
    )
