"""luck-skill-separator: is this outcome distinguishable from the symbol moving.

A +2% trade in something that routinely moves 5% in a day is not a result. It is one
draw from a wide distribution, and a system that learns from it is fitting noise --
which is the failure mode that destroys systematic strategies, because noise is
abundant and always available to confirm whatever was just done.

So every outcome is standardised against what the symbol does anyway over the same
holding period. Three things this requires, and each is where a naive version breaks:

- **The comparison is horizon-matched.** Daily volatility applied to a four-minute
  trade overstates the noise enormously and makes every result insignificant; the
  same number applied to a week-long hold does the opposite. Volatility is scaled by
  the square root of the holding time.
- **The comparison is per symbol.** One threshold across a portfolio calls every
  move in a quiet major insignificant and every move in a volatile alt a triumph.
- **Significance is not success.** A large loss can be highly significant, and that
  is exactly when it is worth learning from. Significance says the outcome was
  unlikely to be noise, not that it was good.

Sample size travels with the verdict. One significant trade is still one trade, and
the part reports how many comparable outcomes exist so nothing downstream can treat
a single draw as an established effect.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.trade_decoding_types import OutcomeSignificance
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "luck-skill-separator"

PART_DECLARATION = PartDeclaration(
    part_id="luck-skill-separator",
    consumes=("closed-trade", "volatility-forecast", "symbol-profile"),
    produces=("outcome-significance", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

SIGNIFICANT = "unlikely-to-be-noise"
INDISTINGUISHABLE = "indistinguishable-from-the-symbol-moving"
NO_VOLATILITY = "this-symbols-volatility-has-never-been-measured"
NO_HOLDING_TIME = "the-holding-period-is-zero-or-unknown"

SECONDS_IN_A_DAY = 86_400.0


@dataclass(frozen=True)
class SignificanceOutcome:
    trade_id: str
    state: str
    significance: OutcomeSignificance
    holding_seconds: float
    reason: str
    assessed_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state in (SIGNIFICANT, INDISTINGUISHABLE)


@dataclass
class SeparatorStanding:
    outcomes_assessed: int = 0
    significant: int = 0
    indistinguishable: int = 0
    unmeasurable: int = 0
    significant_losses: int = 0
    largest_standardised: float = 0.0
    symbols_seen: int = 0


class LuckSkillSeparator:
    """Standardises each outcome against the symbol's own horizon-matched movement."""

    def __init__(
        self,
        significance_threshold: float,
        minimum_comparable_outcomes: int,
        now_ns=time.time_ns,
    ) -> None:
        if significance_threshold <= 0:
            raise ValueError(
                "a threshold of zero calls every outcome significant, which is the "
                "failure this part exists to prevent"
            )
        if minimum_comparable_outcomes < 1:
            raise ValueError(
                "sample size travels with the verdict: one significant trade is still "
                "one trade"
            )
        self._threshold = significance_threshold
        self._minimum_comparable = minimum_comparable_outcomes
        self._now_ns = now_ns
        self._daily_volatility: dict[tuple, float] = {}
        self._outcomes: dict[tuple, int] = {}
        self.standing = SeparatorStanding()

    def observe_daily_volatility(self, venue_id: str, symbol: str, volatility: float) -> None:
        """Per symbol: one portfolio-wide threshold flatters volatile instruments."""
        key = (venue_id, symbol)
        if key not in self._daily_volatility:
            self.standing.symbols_seen += 1
        self._daily_volatility[key] = volatility

    def expected_noise(self, venue_id: str, symbol: str, holding_seconds: float) -> float | None:
        """Volatility scaled to the horizon actually held, by the square root of time."""
        daily = self._daily_volatility.get((venue_id, symbol))
        if daily is None or daily <= 0 or holding_seconds <= 0:
            return None
        return daily * math.sqrt(holding_seconds / SECONDS_IN_A_DAY)

    def assess(self, trade_id: str, closed_trade) -> SignificanceOutcome:
        self.standing.outcomes_assessed += 1
        holding = closed_trade.holding_seconds
        key = (closed_trade.venue_id, closed_trade.symbol)
        self._outcomes[key] = self._outcomes.get(key, 0) + 1
        comparable = self._outcomes[key]

        if holding <= 0:
            self.standing.unmeasurable += 1
            return self._outcome(
                trade_id, NO_HOLDING_TIME, closed_trade, None, None, False, holding,
                comparable,
                "the holding period is zero, so there is no horizon to scale the symbol's "
                "movement to",
            )

        noise = self.expected_noise(
            closed_trade.venue_id, closed_trade.symbol, holding
        )
        if noise is None:
            self.standing.unmeasurable += 1
            return self._outcome(
                trade_id, NO_VOLATILITY, closed_trade, None, None, False, holding,
                comparable,
                f"{closed_trade.symbol} has no measured volatility, so this outcome "
                f"cannot be told apart from the symbol simply moving",
            )

        # Return relative to the capital at risk, so a large position does not read
        # as a significant decision.
        notional = abs(closed_trade.entry_price * closed_trade.quantity)
        relative = closed_trade.realised_pnl / notional if notional > 0 else 0.0
        standardised = relative / noise if noise > 0 else 0.0
        self.standing.largest_standardised = max(
            self.standing.largest_standardised, abs(standardised)
        )

        is_significant = abs(standardised) >= self._threshold
        if is_significant:
            self.standing.significant += 1
            if closed_trade.realised_pnl < 0:
                self.standing.significant_losses += 1
        else:
            self.standing.indistinguishable += 1

        return self._outcome(
            trade_id, SIGNIFICANT if is_significant else INDISTINGUISHABLE,
            closed_trade, noise, standardised, is_significant, holding, comparable,
            f"{relative:+.2%} over {holding / 60.0:.1f} minute(s) against an expected "
            f"{noise:.2%} of movement at that horizon: {standardised:+.2f} standard "
            f"move(s)"
            + (
                ". Unlikely to be noise"
                + (
                    ", and it is a loss -- which is exactly when an outcome is worth "
                    "learning from"
                    if closed_trade.realised_pnl < 0
                    else ""
                )
                if is_significant
                else ". Indistinguishable from the symbol moving, so nothing should be "
                     "learned from it"
            )
            + (
                f". Only {comparable} comparable outcome(s) exist for this symbol, below "
                f"the {self._minimum_comparable} that would make an effect established"
                if comparable < self._minimum_comparable
                else ""
            ),
        )

    def _outcome(
        self, trade_id, state, closed_trade, noise, standardised, is_significant,
        holding, comparable, reason,
    ) -> SignificanceOutcome:
        return SignificanceOutcome(
            trade_id=trade_id, state=state,
            significance=OutcomeSignificance(
                trade_id=trade_id,
                realised=closed_trade.realised_pnl,
                expected_noise=noise,
                standardised=standardised,
                is_significant=is_significant,
                is_measurable=noise is not None,
                sample_size=comparable,
                reason=reason,
                assessed_at_ns=self._now_ns(),
            ),
            holding_seconds=holding, reason=reason, assessed_at_ns=self._now_ns(),
        )


def describe_luck_and_skill(separator: LuckSkillSeparator) -> dict:
    return {
        "part_id": PART_ID,
        "outcomes_assessed": separator.standing.outcomes_assessed,
        "significant": separator.standing.significant,
        "indistinguishable_from_noise": separator.standing.indistinguishable,
        "unmeasurable": separator.standing.unmeasurable,
        "significant_losses": separator.standing.significant_losses,
        "largest_standardised_move": separator.standing.largest_standardised,
        "symbols_with_measured_volatility": separator.standing.symbols_seen,
        "uses_one_threshold_for_every_symbol": False,
        "compares_a_four_minute_trade_against_daily_volatility": False,
        "treats_significance_as_success": False,
    }


def run_luck_skill_separator(
    separator: LuckSkillSeparator, control_socket, read_closed_trades,
    publish_significance, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for trade_id, closed_trade in read_closed_trades():
            outcome = separator.assess(trade_id, closed_trade)
            publish_significance(outcome.significance)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_luck_and_skill(separator),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The noise an outcome is judged against is the symbol's forecast
    volatility, scaled to a day; the profile is consumed so a symbol's
    listing age is known to the separator's record.
    """
    import math

    from runtime.input_assembly import Batch
    from runtime.trade_identity import closed_trade_id

    closed = Batch(read=context.bus.reader("closed-trade"))
    forecasts = Batch(read=context.bus.reader("volatility-forecast"))
    profiles = Batch(read=context.bus.reader("symbol-profile"))
    publish_significance = context.bus.publisher_for("outcome-significance")
    separator = LuckSkillSeparator(
        significance_threshold=context.number("luck_significance_threshold"),
        minimum_comparable_outcomes=int(context.number("luck_minimum_comparable_outcomes")),
    )
    day_seconds = 86400.0

    def read_closed_trades():
        profiles.payloads()
        for forecast in forecasts.payloads():
            if forecast.expected_volatility is not None and forecast.horizon_seconds > 0:
                daily = forecast.expected_volatility * math.sqrt(day_seconds / forecast.horizon_seconds)
                separator.observe_daily_volatility(forecast.venue_id, forecast.symbol, daily)
        return tuple((closed_trade_id(trade), trade) for trade in closed.payloads())

    return run_luck_skill_separator(
        separator=separator,
        control_socket=context.control_socket,
        read_closed_trades=read_closed_trades,
        publish_significance=lambda s: publish_significance((s,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
