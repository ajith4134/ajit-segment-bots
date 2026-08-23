"""reward-shaper: what the system is actually being taught to maximise.

The most consequential and least visible choice in a learning system. Whatever
this part rewards is what the system will produce, including consequences nobody
intended -- and the naive reward, realised profit, teaches three specific bad
habits:

- **Take enormous risk.** Profit rewards a trade that made 3% and says nothing
  about the 40% adverse excursion it survived to get there. So the reward is
  scaled by how much risk was taken to earn it: the same profit from a trade that
  never went against is worth more than one that nearly liquidated.
- **Hold losers.** A position that eventually recovers pays the same as one that
  worked immediately, so the system learns that time is free. Time is not free --
  it is capital that could not be used elsewhere -- and the reward decays with the
  horizon the trade was given.
- **Confuse luck with skill.** A trade that made money because the market moved
  for reasons unrelated to the setup teaches the setup. The reward is scaled by
  outcome significance, which is what says whether the result was distinguishable
  from noise.

**Rewards are in USDT** (RL-028), and every conversion carries the rate it used
(RL-029). A reward denominated in whatever the position happened to settle in
teaches the system to prefer the currency that appreciated.

**The reward is bounded.** An unbounded one lets a single extraordinary trade
dominate everything the system learns, and the trade most likely to be
extraordinary is the one that was mismeasured.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "reward-shaper"

PART_DECLARATION = PartDeclaration(
    part_id="reward-shaper",
    consumes=(
        "closed-trade", "usdt-pnl-statement", "peak-excursion", "pnl-attribution",
        "outcome-significance",
    ),
    produces=("learning-reward", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

SHAPED = "shaped"
NO_USDT_STATEMENT = "no-usdt-denominated-result-for-this-trade"
NO_EXCURSION = "no-excursion-record-to-scale-risk-by"

FOR_RISK_TAKEN = "risk-taken-to-earn-it"
FOR_TIME_HELD = "capital-tied-up"
FOR_SIGNIFICANCE = "distinguishable-from-noise"
FOR_ATTRIBUTION = "how-much-came-from-the-setup"


@dataclass(frozen=True)
class LearningReward:
    """What the system is being taught by one trade, and why it is that much."""

    venue_id: str
    symbol: str
    detector: str
    state: str
    reward: float | None
    raw_usdt: float | None
    conversion_rate: float | None
    components: dict
    was_bounded: bool
    reason: str
    shaped_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == SHAPED and self.reward is not None


@dataclass
class ShaperStanding:
    trades_seen: int = 0
    rewards_shaped: int = 0
    refused_no_usdt: int = 0
    refused_no_excursion: int = 0
    bounded_at_the_cap: int = 0
    largest_reward: float | None = None
    largest_risk_discount: float | None = None
    by_component: dict = field(default_factory=dict)


class RewardShaper:
    """Shapes the reward so the system is taught what it should be, not what is easy."""

    def __init__(
        self,
        maximum_reward: float,
        risk_reference_fraction: float,
        horizon_half_life_seconds: float,
        minimum_significance: float,
        now_ns=time.time_ns,
    ) -> None:
        if maximum_reward <= 0:
            raise ValueError(
                "an unbounded reward lets one extraordinary trade dominate everything the "
                "system learns, and the extraordinary trade is the one most likely mismeasured"
            )
        if risk_reference_fraction <= 0:
            raise ValueError(
                "risk must be measured against something, or profit alone teaches the system "
                "to take enormous risk"
            )
        if horizon_half_life_seconds <= 0:
            raise ValueError(
                "without a time cost the system learns that holding a loser is free"
            )
        self._maximum = maximum_reward
        self._risk_reference = risk_reference_fraction
        self._horizon_half_life = horizon_half_life_seconds
        self._minimum_significance = minimum_significance
        self._now_ns = now_ns
        self._usdt_results: dict[tuple[str, str, int], tuple] = {}
        self._excursions: dict[tuple[str, str, int], float] = {}
        self._attributions: dict[tuple[str, str, int], float] = {}
        self._significance: dict[tuple[str, str, int], float] = {}
        self.standing = ShaperStanding()

    def observe_usdt_result(
        self, venue_id: str, symbol: str, opened_at_ns: int, usdt: float, conversion_rate: float
    ) -> None:
        """The result in USDT with the rate it was converted at (RL-028, RL-029).

        Rewarding whatever the position settled in teaches the system to prefer
        the currency that appreciated.
        """
        self._usdt_results[(venue_id, symbol, opened_at_ns)] = (usdt, conversion_rate)

    def observe_peak_adverse_excursion(
        self, venue_id: str, symbol: str, opened_at_ns: int, fraction: float
    ) -> None:
        self._excursions[(venue_id, symbol, opened_at_ns)] = abs(fraction)

    def observe_attribution(
        self, venue_id: str, symbol: str, opened_at_ns: int, fraction_from_the_setup: float
    ) -> None:
        """How much of the result came from the setup rather than from the market."""
        self._attributions[(venue_id, symbol, opened_at_ns)] = fraction_from_the_setup

    def observe_significance(
        self, venue_id: str, symbol: str, opened_at_ns: int, significance: float
    ) -> None:
        self._significance[(venue_id, symbol, opened_at_ns)] = significance

    def shape(
        self, venue_id: str, symbol: str, detector: str, opened_at_ns: int, seconds_held: float
    ) -> LearningReward:
        self.standing.trades_seen += 1
        key = (venue_id, symbol, opened_at_ns)

        result = self._usdt_results.get(key)
        if result is None:
            self.standing.refused_no_usdt += 1
            return self._reward(
                venue_id, symbol, detector, NO_USDT_STATEMENT, None, None, None, {}, False,
                "no USDT-denominated result for this trade; rewarding whatever it settled in "
                "would teach the system to prefer the currency that appreciated (RL-028)",
            )

        usdt, conversion_rate = result
        excursion = self._excursions.get(key)
        if excursion is None:
            self.standing.refused_no_excursion += 1
            return self._reward(
                venue_id, symbol, detector, NO_EXCURSION, None, usdt, conversion_rate, {}, False,
                "no excursion record, so how much risk was taken to earn this cannot be "
                "measured -- and profit alone teaches the system to take enormous risk",
            )

        components: dict[str, float] = {}

        # Risk: the same profit from a trade that never went against is worth
        # more than one that nearly liquidated.
        risk_multiple = self._risk_reference / max(self._risk_reference, excursion)
        components[FOR_RISK_TAKEN] = risk_multiple
        if (
            self.standing.largest_risk_discount is None
            or risk_multiple < self.standing.largest_risk_discount
        ):
            self.standing.largest_risk_discount = risk_multiple

        # Time: capital tied up is capital that could not be used elsewhere.
        time_multiple = 0.5 ** (max(0.0, seconds_held) / self._horizon_half_life)
        components[FOR_TIME_HELD] = time_multiple

        significance = self._significance.get(key, 1.0)
        if significance < self._minimum_significance:
            # A result indistinguishable from noise teaches noise.
            components[FOR_SIGNIFICANCE] = significance / self._minimum_significance
        else:
            components[FOR_SIGNIFICANCE] = 1.0

        attribution = self._attributions.get(key)
        if attribution is not None:
            # A trade that made money because the market moved for unrelated
            # reasons should not teach the setup.
            components[FOR_ATTRIBUTION] = max(0.0, min(1.0, attribution))

        reward = usdt
        for multiple in components.values():
            reward *= multiple

        bounded = abs(reward) > self._maximum
        if bounded:
            self.standing.bounded_at_the_cap += 1
            reward = math.copysign(self._maximum, reward)

        self.standing.rewards_shaped += 1
        if self.standing.largest_reward is None or abs(reward) > abs(self.standing.largest_reward):
            self.standing.largest_reward = reward
        for name in components:
            self.standing.by_component[name] = self.standing.by_component.get(name, 0) + 1

        return self._reward(
            venue_id, symbol, detector, SHAPED, reward, usdt, conversion_rate, components, bounded,
            f"{usdt:+,.2f} USDT shaped to {reward:+,.4f}: "
            + ", ".join(f"{name} x{value:.3f}" for name, value in sorted(components.items()))
            + f". Converted at {conversion_rate:.6g} (RL-029)"
            + (
                f"; bounded at {self._maximum:,.2f} so one extraordinary trade cannot dominate "
                f"everything the system learns"
                if bounded
                else ""
            ),
        )

    def _reward(
        self, venue_id, symbol, detector, state, reward, usdt, rate, components, bounded, reason
    ) -> LearningReward:
        return LearningReward(
            venue_id=venue_id,
            symbol=symbol,
            detector=detector,
            state=state,
            reward=reward,
            raw_usdt=usdt,
            conversion_rate=rate,
            components=dict(components),
            was_bounded=bounded,
            reason=reason,
            shaped_at_ns=self._now_ns(),
        )


def describe_reward_shaping(shaper: RewardShaper) -> dict:
    return {
        "part_id": PART_ID,
        "trades_seen": shaper.standing.trades_seen,
        "rewards_shaped": shaper.standing.rewards_shaped,
        "refused_no_usdt_statement": shaper.standing.refused_no_usdt,
        "refused_no_excursion_record": shaper.standing.refused_no_excursion,
        "bounded_at_the_cap": shaper.standing.bounded_at_the_cap,
        "largest_reward": shaper.standing.largest_reward,
        "largest_risk_discount": shaper.standing.largest_risk_discount,
        "by_component": dict(sorted(shaper.standing.by_component.items())),
        "denominated_in": "USDT",
    }


def run_reward_shaper(
    shaper: RewardShaper, control_socket, read_closed_trades, publish_rewards,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_rewards(
            tuple(
                shaper.shape(venue_id, symbol, detector, opened_at_ns, seconds_held)
                for venue_id, symbol, detector, opened_at_ns, seconds_held in read_closed_trades(shaper)
            )
        )

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
    """The one entry point every part carries (T-1).

    A closed trade is shaped once its USDT statement has arrived, keyed by
    venue, symbol and opening time; the excursion, attribution and
    significance that arrive for the same trade refine it. The detector
    that raised the trade is not on the closed trade, so it is "unknown"
    here; the scorekeeper attributes rewards by what it knows.
    """
    from runtime.input_assembly import Batch

    closed = Batch(read=context.bus.reader("closed-trade"))
    statements = Batch(read=context.bus.reader("usdt-pnl-statement"))
    excursions = Batch(read=context.bus.reader("peak-excursion"))
    attributions = Batch(read=context.bus.reader("pnl-attribution"))
    significances = Batch(read=context.bus.reader("outcome-significance"))
    publish_rewards = context.bus.publisher_for("learning-reward")
    shaper = RewardShaper(
        maximum_reward=context.number("reward_maximum"),
        risk_reference_fraction=context.number("reward_risk_reference_fraction"),
        horizon_half_life_seconds=context.number("reward_horizon_half_life"),
        minimum_significance=context.number("reward_minimum_significance"),
    )
    opened_at: dict[tuple[str, str], int] = {}
    held_for: dict[tuple[str, str, int], float] = {}
    last_excursion: dict[tuple[str, str], object] = {}

    def read_closed_trades(_shaper):
        for excursion in excursions.payloads():
            last_excursion[(excursion.venue_id, excursion.symbol)] = excursion
        ready = []
        for trade in closed.payloads():
            key = (trade.venue_id, trade.symbol, trade.opened_at_ns)
            opened_at[(trade.venue_id, trade.symbol)] = trade.opened_at_ns
            held_for[key] = (trade.closed_at_ns - trade.opened_at_ns) / 1e9
            excursion = last_excursion.get((trade.venue_id, trade.symbol))
            if excursion is not None and trade.entry_price:
                shaper.observe_peak_adverse_excursion(
                    trade.venue_id, trade.symbol, trade.opened_at_ns,
                    abs(excursion.worst_price - trade.entry_price) / trade.entry_price,
                )
        for statement in statements.payloads():
            at = opened_at.get((statement.venue_id, statement.symbol))
            if at is None:
                continue
            shaper.observe_usdt_result(
                statement.venue_id, statement.symbol, at, statement.net_pnl_usdt, statement.conversion_rate
            )
            ready.append((statement.venue_id, statement.symbol, "unknown", at, held_for.get((statement.venue_id, statement.symbol, at), statement.holding_seconds)))
        for attribution in attributions.payloads():
            at = opened_at.get((attribution.venue_id, attribution.symbol))
            setup = attribution.components.get("setup") if isinstance(attribution.components, dict) else None
            if at is not None and setup is not None and attribution.realised_pnl:
                shaper.observe_attribution(attribution.venue_id, attribution.symbol, at, float(setup) / attribution.realised_pnl)
        significances.payloads()
        return tuple(ready)

    def publish(rewards) -> None:
        if rewards:
            publish_rewards(rewards)

    return run_reward_shaper(
        shaper=shaper,
        control_socket=context.control_socket,
        read_closed_trades=read_closed_trades,
        publish_rewards=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
