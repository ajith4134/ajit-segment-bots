"""leverage-selector: leverage per trade from volatility and funding (RL-041, RL-053).

Leverage is not a setting to max out; it is the multiplier on how quickly a
position can be taken away. Two things decide how much is prudent:

- **Volatility.** The more a symbol moves, the closer any leverage puts the
  liquidation price to the market. So the choice is inverted against forecast
  volatility, targeting a constant *distance to liquidation* rather than a
  constant multiplier -- a 10x position in a calm symbol and a 3x position in a
  violent one carry the same real risk, and only the second number looks careful.
- **Funding.** A leveraged perpetual pays funding on the whole notional, three
  times a day. A position that would be marginally profitable unlevered can be
  reliably unprofitable at 10x purely through carry, so a high funding rate cuts
  the leverage rather than being noticed afterwards in the PnL.

**Never above the operator's ceiling** (RL-053). The ceiling is not advice, and a
selector that could exceed it would make the setting decorative.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "leverage-selector"

PART_DECLARATION = PartDeclaration(
    part_id="leverage-selector",
    consumes=("trade-intent", "volatility-forecast", "funding-forecast", "leverage-ceiling"),
    produces=("leverage-choice", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

CHOSEN = "chosen"
AT_CEILING = "held-at-operator-ceiling"
AT_FLOOR = "held-at-floor"
UNLEVERAGED_NO_FORECAST = "unleveraged-no-volatility-forecast"

# The smallest leverage there is. Unlevered is always available and always safe,
# which is why an absent forecast falls back to it rather than to a guess.
NO_LEVERAGE = 1.0


@dataclass(frozen=True)
class LeverageChoice:
    """One trade's leverage, and every input that produced it."""

    venue_id: str
    symbol: str
    leverage: float
    outcome: str
    ceiling: float
    volatility_forecast: float | None
    funding_forecast: float | None
    volatility_implied_leverage: float | None
    funding_penalty: float
    reason: str
    chosen_at_ns: int


@dataclass
class SelectorStanding:
    choices: int = 0
    held_at_ceiling: int = 0
    unleveraged_for_want_of_a_forecast: int = 0
    funding_reduced: int = 0
    highest_chosen: float = 0.0
    average_chosen: float = 0.0


class LeverageSelector:
    """Chooses leverage to hold a constant distance to liquidation, then pays for funding."""

    def __init__(
        self,
        target_liquidation_distance: float,
        volatility_horizons_to_survive: float,
        funding_tolerance_per_day: float,
        maintenance_margin_rate: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < target_liquidation_distance < 1.0:
            raise ValueError("the target distance to liquidation must be a fraction in (0, 1)")
        if volatility_horizons_to_survive <= 0:
            raise ValueError("a position must survive at least one horizon of volatility")
        self._target_distance = target_liquidation_distance
        self._horizons = volatility_horizons_to_survive
        self._funding_tolerance = funding_tolerance_per_day
        self._maintenance = maintenance_margin_rate
        self._now_ns = now_ns
        self.standing = SelectorStanding()

    def choose(
        self,
        venue_id: str,
        symbol: str,
        ceiling: float,
        volatility_forecast: float | None,
        funding_forecast: float | None = None,
    ) -> LeverageChoice:
        """Leverage for one trade. `volatility_forecast` is a fractional move per horizon."""
        self.standing.choices += 1
        ceiling = max(NO_LEVERAGE, ceiling)

        if volatility_forecast is None or volatility_forecast <= 0:
            self.standing.unleveraged_for_want_of_a_forecast += 1
            return self._choice(
                venue_id, symbol, NO_LEVERAGE, UNLEVERAGED_NO_FORECAST, ceiling,
                volatility_forecast, funding_forecast, None, 1.0,
                "no volatility forecast; unlevered is the only size that needs no forecast",
            )

        # The move the position must survive, and the leverage whose liquidation
        # sits the target distance beyond it.
        survivable_move = volatility_forecast * self._horizons
        distance_needed = survivable_move + self._target_distance
        implied = 1.0 / (distance_needed + self._maintenance)

        penalty = self._funding_penalty(funding_forecast)
        if penalty < 1.0:
            self.standing.funding_reduced += 1
        chosen = implied * penalty

        outcome = CHOSEN
        if chosen >= ceiling:
            chosen = ceiling
            outcome = AT_CEILING
            self.standing.held_at_ceiling += 1
        if chosen <= NO_LEVERAGE:
            chosen = NO_LEVERAGE
            outcome = AT_FLOOR

        self.standing.highest_chosen = max(self.standing.highest_chosen, chosen)
        self.standing.average_chosen += (chosen - self.standing.average_chosen) / self.standing.choices

        return self._choice(
            venue_id, symbol, chosen, outcome, ceiling, volatility_forecast, funding_forecast,
            implied, penalty,
            f"volatility {volatility_forecast:.2%} over {self._horizons:g} horizon(s) implies "
            f"{implied:.1f}x for a {self._target_distance:.1%} cushion"
            + (f", cut to {penalty:.2f} of it by funding" if penalty < 1.0 else "")
            + (f"; held at the {ceiling:g}x ceiling" if outcome == AT_CEILING else ""),
        )

    def _funding_penalty(self, funding_forecast: float | None) -> float:
        """How much of the volatility-implied leverage the funding cost leaves.

        None is not zero cost -- it is an unknown cost -- so an unknown funding
        rate is treated as being at the tolerance, which halves the leverage
        rather than assuming carry is free.
        """
        if funding_forecast is None:
            return 0.5
        daily = abs(funding_forecast)
        if daily <= 0 or self._funding_tolerance <= 0:
            return 1.0
        return min(1.0, self._funding_tolerance / daily)

    def _choice(
        self, venue_id, symbol, leverage, outcome, ceiling, volatility, funding, implied, penalty, reason
    ) -> LeverageChoice:
        return LeverageChoice(
            venue_id=venue_id,
            symbol=symbol,
            leverage=leverage,
            outcome=outcome,
            ceiling=ceiling,
            volatility_forecast=volatility,
            funding_forecast=funding,
            volatility_implied_leverage=implied,
            funding_penalty=penalty,
            reason=reason,
            chosen_at_ns=self._now_ns(),
        )


def describe_leverage(selector: LeverageSelector) -> dict:
    return {
        "part_id": PART_ID,
        "choices": selector.standing.choices,
        "held_at_ceiling": selector.standing.held_at_ceiling,
        "unleveraged_for_want_of_a_forecast": selector.standing.unleveraged_for_want_of_a_forecast,
        "funding_reduced": selector.standing.funding_reduced,
        "highest_chosen": selector.standing.highest_chosen,
        "average_chosen": selector.standing.average_chosen,
    }


def run_leverage_selector(
    selector: LeverageSelector, control_socket, read_intents, publish_choices,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_choices(tuple(selector.choose(**intent) for intent in read_intents()))

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

    A choice per actionable intent, from the latest volatility and funding
    forecast for that symbol and the segment's leverage ceiling. No forecast
    means the selector's own refusal, which it states; no ceiling yet means
    no choice, since a leverage chosen against a ceiling nobody has read is a
    leverage chosen against nothing.
    """
    from runtime.input_assembly import Batch, LatestByKey

    intents = Batch(read=context.bus.reader("trade-intent"))
    volatility = LatestByKey(read=context.bus.reader("volatility-forecast"), key_of=lambda f: (f.venue_id, f.symbol))
    funding = LatestByKey(read=context.bus.reader("funding-forecast"), key_of=lambda f: (f.venue_id, f.symbol))
    ceilings = LatestByKey(read=context.bus.reader("leverage-ceiling"), key_of=lambda a: a.segment)
    publish_choices = context.bus.publisher_for("leverage-choice")
    segment = str(context.setting("segment_id").value)
    selector = LeverageSelector(
        target_liquidation_distance=context.number("leverage_target_liquidation_distance"),
        volatility_horizons_to_survive=context.number("leverage_volatility_horizons_to_survive"),
        funding_tolerance_per_day=context.number("leverage_funding_tolerance_per_day"),
        maintenance_margin_rate=context.number("maintenance_margin_rate"),
    )

    def read_intents():
        allotment = ceilings.mapping().get(segment)
        vol_by_symbol = volatility.mapping()
        funding_by_symbol = funding.mapping()
        requests = []
        for intent in intents.payloads():
            if not intent.is_actionable or allotment is None:
                continue
            key = (intent.venue_id, intent.symbol)
            forecast = vol_by_symbol.get(key)
            rate = funding_by_symbol.get(key)
            requests.append({
                "venue_id": intent.venue_id,
                "symbol": intent.symbol,
                "ceiling": allotment.leverage_ceiling,
                "volatility_forecast": None if forecast is None else forecast.expected_volatility,
                "funding_forecast": None if rate is None else rate.predicted_rate,
            })
        return tuple(requests)

    def publish(choices) -> None:
        if choices:
            publish_choices(choices)

    return run_leverage_selector(
        selector=selector,
        control_socket=context.control_socket,
        read_intents=read_intents,
        publish_choices=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
