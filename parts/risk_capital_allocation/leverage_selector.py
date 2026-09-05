"""leverage-selector: leverage per trade from volatility and the cost of borrowing (RL-041, RL-053).

Leverage is not a setting to max out; it is the multiplier on how quickly a
position can be taken away. Two things decide how much is prudent:

- **Volatility.** The more a symbol moves, the closer any leverage puts the
  liquidation price to the market. So the choice is inverted against forecast
  volatility, targeting a constant *distance to liquidation* rather than a
  constant multiplier -- a 10x position in a calm symbol and a 3x position in a
  violent one carry the same real risk, and only the second number looks careful.
- **Carry.** Borrowing costs money for as long as it is borrowed. This was
  written for perpetual funding and is now the Indian analogue -- the broker's
  own daily rate on the part of the position its margin does not cover, which
  the position's own broker-margin-requirement already states. Replaced
  2026-09-05: `funding-forecast` had no Indian producer, and an absent one
  returned a penalty of 0.5, so every leverage this project chose on an Indian
  segment was silently halved for a cost nobody charges.

  The crypto reasoning it replaces, kept because the shape is the same:
  a leveraged perpetual pays funding on the whole notional, three
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
    consumes=(
        "trade-intent", "volatility-forecast", "leverage-ceiling",
        # What the broker will actually lend against this instrument. A hard cap,
        # not a preference: the operator's ceiling and the volatility-implied
        # leverage are both this system's opinions, and the broker's limit is
        # not -- an order above it is rejected, not trimmed.
        "broker-margin-requirement",
    ),
    produces=("leverage-choice", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

CHOSEN = "chosen"
AT_CEILING = "held-at-operator-ceiling"
AT_FLOOR = "held-at-floor"
# The broker lends less than this system would have used. Distinct from
# AT_CEILING, which is the operator's own limit: one is a policy this project
# chose and the other is a fact about the account, and a board that showed them
# as one number could not tell you which to change.
AT_BROKER_LIMIT = "held-at-broker-limit"
UNLEVERAGED_NO_BROKER_QUOTE = "unleveraged-no-broker-margin-quote"
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
    # What borrowing to the chosen leverage costs per day, as a fraction of
    # notional -- None where the broker has not said what it lends. Was
    # `funding_forecast`, a perpetual's rate, until 2026-09-05.
    carry_cost_per_day: float | None
    volatility_implied_leverage: float | None
    carry_penalty: float
    reason: str
    chosen_at_ns: int
    # What the broker said it would lend, or None where it was never asked or
    # never answered. None is not "no limit" -- see `choose`. Last and
    # defaulted so every existing construction of this payload still holds.
    broker_available_leverage: float | None = None
    # Which segment this leverage is for. One underlying can belong to two
    # segments at once -- RELIANCE is a stock option and a cash-equity share --
    # and they do not share a ceiling: options are unlevered by construction and
    # cash equity intraday borrows at up to 5x. Published one per segment since
    # 2026-09-05, and position-sizer takes the one matching the segment the
    # instrument choice landed in. Before this the ceiling came from the spine's
    # single `segment_id`, so bot 3 would have been sized at the index segment's
    # 1.0 while its own settings said five.
    segment: str = ""


@dataclass
class SelectorStanding:
    choices: int = 0
    held_at_ceiling: int = 0
    held_at_broker_limit: int = 0
    unleveraged_for_want_of_a_broker_quote: int = 0
    unleveraged_for_want_of_a_forecast: int = 0
    carry_reduced: int = 0
    highest_chosen: float = 0.0
    average_chosen: float = 0.0


class LeverageSelector:
    """Chooses leverage to hold a constant distance to liquidation, then pays to borrow."""

    def __init__(
        self,
        target_liquidation_distance: float,
        volatility_horizons_to_survive: float,
        carry_tolerance_per_day: float,
        daily_borrowing_rate: float,
        maintenance_margin_rate: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < target_liquidation_distance < 1.0:
            raise ValueError("the target distance to liquidation must be a fraction in (0, 1)")
        if volatility_horizons_to_survive <= 0:
            raise ValueError("a position must survive at least one horizon of volatility")
        self._target_distance = target_liquidation_distance
        self._horizons = volatility_horizons_to_survive
        self._carry_tolerance = carry_tolerance_per_day
        self._daily_borrowing_rate = daily_borrowing_rate
        self._maintenance = maintenance_margin_rate
        self._now_ns = now_ns
        self.standing = SelectorStanding()

    def choose(
        self,
        venue_id: str,
        symbol: str,
        ceiling: float,
        volatility_forecast: float | None,
        broker_available_leverage: float | None = None,
        a_broker_quote_is_required: bool = False,
        segment: str = "",
    ) -> LeverageChoice:
        """Leverage for one trade. `volatility_forecast` is a fractional move per horizon.

        `broker_available_leverage` is what the broker said it would lend against
        this instrument, from `broker-margin-requirement`. It is a **hard cap**
        and never a preference: the ceiling and the volatility-implied figure are
        both this system's opinions, and an order above what the broker allows is
        rejected outright rather than trimmed.

        `a_broker_quote_is_required` says whether this segment's leverage is the
        broker's to grant at all. On a segment that borrows -- cash equity
        intraday -- a missing quote means unlevered, because the alternative is
        sizing against a permission nobody gave. On a segment that does not
        borrow, a bought option has no margin to quote and its absence is
        correct rather than missing, which is the same not-a-gap reading three
        audits already reached about this part.
        """
        self.standing.choices += 1
        ceiling = max(NO_LEVERAGE, ceiling)

        if a_broker_quote_is_required and broker_available_leverage is None:
            self.standing.unleveraged_for_want_of_a_broker_quote += 1
            return self._choice(
                venue_id, symbol, NO_LEVERAGE, UNLEVERAGED_NO_BROKER_QUOTE, ceiling,
                volatility_forecast, None, None, 1.0, None,
                "the broker has not said what it will lend against this instrument; "
                "unlevered, because a leverage nobody granted is not one to size against",
                segment,
            )

        if volatility_forecast is None or volatility_forecast <= 0:
            self.standing.unleveraged_for_want_of_a_forecast += 1
            return self._choice(
                venue_id, symbol, NO_LEVERAGE, UNLEVERAGED_NO_FORECAST, ceiling,
                volatility_forecast,
                self.carry_cost_per_day(broker_available_leverage), None, 1.0,
                broker_available_leverage,
                "no volatility forecast; unlevered is the only size that needs no forecast",
                segment,
            )

        # The move the position must survive, and the leverage whose liquidation
        # sits the target distance beyond it.
        survivable_move = volatility_forecast * self._horizons
        distance_needed = survivable_move + self._target_distance
        implied = 1.0 / (distance_needed + self._maintenance)

        carry = self.carry_cost_per_day(broker_available_leverage)
        penalty = self._carry_penalty(carry)
        if penalty < 1.0:
            self.standing.carry_reduced += 1
        chosen = implied * penalty

        outcome = CHOSEN
        if chosen >= ceiling:
            chosen = ceiling
            outcome = AT_CEILING
            self.standing.held_at_ceiling += 1
        # The broker's limit is applied after the operator's, so whichever is
        # smaller is the one that shows in the outcome -- and a board can tell
        # a policy this project chose from a fact about the account.
        if broker_available_leverage is not None and chosen > broker_available_leverage:
            chosen = max(NO_LEVERAGE, broker_available_leverage)
            outcome = AT_BROKER_LIMIT
            self.standing.held_at_broker_limit += 1
        if chosen <= NO_LEVERAGE:
            chosen = NO_LEVERAGE
            outcome = AT_FLOOR

        self.standing.highest_chosen = max(self.standing.highest_chosen, chosen)
        self.standing.average_chosen += (chosen - self.standing.average_chosen) / self.standing.choices

        return self._choice(
            venue_id, symbol, chosen, outcome, ceiling, volatility_forecast, carry,
            implied, penalty, broker_available_leverage,
            f"volatility {volatility_forecast:.2%} over {self._horizons:g} horizon(s) implies "
            f"{implied:.1f}x for a {self._target_distance:.1%} cushion"
            + (f", cut to {penalty:.2f} of it by the cost of borrowing" if penalty < 1.0 else "")
            + (f"; held at the {ceiling:g}x ceiling" if outcome == AT_CEILING else "")
            + (
                f"; held at the {broker_available_leverage:.2f}x the broker lends"
                if outcome == AT_BROKER_LIMIT else ""
            ),
            segment,
        )

    def carry_cost_per_day(self, broker_available_leverage: float | None) -> float | None:
        """What borrowing to this leverage costs per day, as a fraction of notional.

        The Indian analogue of a perpetual's funding, and it is computed rather
        than forecast: what is borrowed is the part of the position the broker's
        own margin does not cover, and the daily rate is the broker's published
        one.

            borrowed fraction = 1 - (margin required / notional) = 1 - 1/leverage
            carry per day     = borrowed fraction x the daily rate

        None where the broker has not said what it lends -- which is a different
        answer from zero, and the caller refuses rather than assuming either.
        """
        if broker_available_leverage is None or broker_available_leverage <= 0:
            return None
        borrowed_fraction = max(0.0, 1.0 - (1.0 / broker_available_leverage))
        return borrowed_fraction * self._daily_borrowing_rate

    def _carry_penalty(self, carry_per_day: float | None) -> float:
        """How much of the volatility-implied leverage the carry cost leaves.

        **Zero carry is not an unknown cost, and this is the difference that
        matters.** The crypto version of this method returned 0.5 for a missing
        funding rate, because a perpetual always pays funding and not knowing it
        was a risk. An intraday equity position squared off in the same session
        borrows for hours and, at Upstox's published intraday rate, pays nothing
        for it -- so a penalty of 0.5 here would have halved every leverage this
        segment ever chose, for a cost that is not charged.

        A missing cost is still refused rather than assumed free: None comes only
        from a broker that has not said what it lends, and the caller has already
        turned that into UNLEVERAGED_NO_BROKER_QUOTE before reaching here.
        """
        if carry_per_day is None:
            return 1.0
        daily = abs(carry_per_day)
        if daily <= 0 or self._carry_tolerance <= 0:
            return 1.0
        return min(1.0, self._carry_tolerance / daily)

    def _choice(
        self, venue_id, symbol, leverage, outcome, ceiling, volatility, carry, implied,
        penalty, broker_available, reason, segment=""
    ) -> LeverageChoice:
        return LeverageChoice(
            venue_id=venue_id,
            symbol=symbol,
            leverage=leverage,
            outcome=outcome,
            ceiling=ceiling,
            volatility_forecast=volatility,
            carry_cost_per_day=carry,
            volatility_implied_leverage=implied,
            carry_penalty=penalty,
            broker_available_leverage=broker_available,
            reason=reason,
            chosen_at_ns=self._now_ns(),
            segment=segment,
        )


def describe_leverage(selector: LeverageSelector) -> dict:
    return {
        "part_id": PART_ID,
        "choices": selector.standing.choices,
        "held_at_ceiling": selector.standing.held_at_ceiling,
        "unleveraged_for_want_of_a_forecast": selector.standing.unleveraged_for_want_of_a_forecast,
        "carry_reduced": selector.standing.carry_reduced,
        "held_at_broker_limit": selector.standing.held_at_broker_limit,
        "unleveraged_for_want_of_a_broker_quote": (
            selector.standing.unleveraged_for_want_of_a_broker_quote
        ),
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
        read_standing=lambda: describe_leverage(selector),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A choice per actionable intent, from the latest volatility forecast for that
    symbol, what the broker says it will lend against it, and the segment's own
    leverage ceiling. No forecast means the selector's own refusal, which it
    states; no ceiling yet means no choice, since a leverage chosen against a
    ceiling nobody has read is a leverage chosen against nothing.

    **Whether a broker quote is required is the segment's own statement.** A
    segment that borrows (`positions_are_squared_off_daily`, cash equity
    intraday) must not size against a permission nobody gave, so a missing quote
    is unlevered. A segment that does not borrow -- a bought option has no margin
    to quote -- is unaffected by its absence, which is the not-a-gap reading
    three earlier audits reached about this part.
    """
    from runtime.input_assembly import Batch, LatestByKey

    intents = Batch(read=context.bus.reader("trade-intent"))
    volatility = LatestByKey(read=context.bus.reader("volatility-forecast"), key_of=lambda f: (f.venue_id, f.symbol))
    requirements = LatestByKey(
        read=context.bus.reader("broker-margin-requirement"),
        key_of=lambda r: (r.venue_id, r.symbol),
        maximum_age_seconds=context.number("broker_margin_requirement_maximum_age_seconds"),
    )
    ceilings = LatestByKey(read=context.bus.reader("leverage-ceiling"), key_of=lambda a: a.segment)
    publish_choices = context.bus.publisher_for("leverage-choice")
    # A segment that squares off daily is one that borrows: its leverage is the
    # broker's to grant, so a missing quote must mean unlevered rather than the
    # ceiling. Silence means it does not borrow -- both options segments say
    # nothing and buy contracts outright. Read per segment since 2026-09-05,
    # because all three run on this spine and only one of them borrows.
    from runtime.segment_settings import (
        SegmentSettingMissing, read_segment_setting, segments_trading_underlying,
    )

    def a_broker_quote_is_required_for(segment: str) -> bool:
        try:
            return bool(
                read_segment_setting(segment, "positions_are_squared_off_daily").value
            )
        except (SegmentSettingMissing, OSError, ValueError):
            return False

    borrows = {}
    selector = LeverageSelector(
        target_liquidation_distance=context.number("leverage_target_liquidation_distance"),
        volatility_horizons_to_survive=context.number("leverage_volatility_horizons_to_survive"),
        carry_tolerance_per_day=context.number("leverage_carry_tolerance_per_day"),
        daily_borrowing_rate=context.number("intraday_borrowing_daily_interest_rate"),
        maintenance_margin_rate=context.number("maintenance_margin_rate"),
    )

    def read_intents():
        ceiling_by_segment = ceilings.mapping()
        vol_by_symbol = volatility.mapping()
        requirement_by_symbol = requirements.mapping()
        requests = []
        for intent in intents.payloads():
            if not intent.is_actionable:
                continue
            key = (intent.venue_id, intent.symbol)
            forecast = vol_by_symbol.get(key)
            requirement = requirement_by_symbol.get(key)
            # One answer per segment that trades this underlying. An intent names
            # an asset, not a contract, so which segment will carry it is not
            # known yet -- RELIANCE could become a stock option or a share bought
            # on margin, and those do not share a ceiling. The sizer takes the
            # one matching the instrument the selector actually chose.
            for segment in segments_trading_underlying(intent.symbol, context):
                allotment = ceiling_by_segment.get(segment)
                if allotment is None:
                    continue
                if segment not in borrows:
                    borrows[segment] = a_broker_quote_is_required_for(segment)
                requests.append({
                    "venue_id": intent.venue_id,
                    "symbol": intent.symbol,
                    "segment": segment,
                    "ceiling": allotment.leverage_ceiling,
                    "volatility_forecast": (
                        None if forecast is None else forecast.expected_volatility
                    ),
                    "broker_available_leverage": (
                        None if requirement is None else requirement.leverage_available
                    ),
                    "a_broker_quote_is_required": borrows[segment],
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
