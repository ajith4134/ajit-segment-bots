"""regime-break-detector: when the market stopped being the market the models learned.

Every model in this system was fitted on a period, and every model keeps producing
confident output after that period ends. That is the failure this part exists to
catch, and it is invisible from inside any single model: a forecaster's accuracy
falls slowly and noisily, and by the time the fall is statistically clear the
regime has been over for weeks.

So the break is detected on the **market**, not on the models:

- **The volatility level shifts.** Not "volatility is high" -- a level that has
  moved to a different level and stayed there. A spike is not a break; a step is.
- **The correlation structure changes.** In a break, things that moved
  independently start moving together. That is the most reliable single signal
  and the one that arrives first, because it is what a liquidity event does.
- **A market event.** A venue delisting, a halt, a fork: an event that changes the
  rules is a break by construction, and waiting for the statistics to confirm it
  is waiting to be told what is already known.

**A break is declared once and held, not re-detected every tick.** A regime that
flickers between broken and intact is worse than either, because every part
downstream re-derives its response each time.

**It says which regime broke.** "Something changed" is not actionable; "the
reverting regime these models were fitted on has ended" tells the arbiter, the
watchers and the conflict resolver exactly what to drop.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.rolling_statistics import RollingWindow, correlation

PART_ID = "regime-break-detector"

PART_DECLARATION = PartDeclaration(
    part_id="regime-break-detector",
    consumes=("market-data", "journal-entry", "market-event"),
    produces=("regime-break-alert", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

INTACT = "intact"
VOLATILITY_STEPPED = "the-volatility-level-has-stepped-and-stayed"
CORRELATIONS_CONVERGED = "things-that-moved-independently-now-move-together"
MARKET_EVENT = "an-event-changed-the-rules"
NOT_ENOUGH_HISTORY = "too-little-history-to-say-what-this-regime-was"


@dataclass(frozen=True)
class RegimeBreakAlert:
    """One regime that has ended, on what evidence, and what to stop trusting."""

    regime: str
    state: str
    has_broken: bool
    established_volatility: float | None
    recent_volatility: float | None
    established_correlation: float | None
    recent_correlation: float | None
    triggering_event: str | None
    symbols_affected: tuple
    reason: str
    raised_at_ns: int


@dataclass
class DetectorStanding:
    checks: int = 0
    breaks_declared: int = 0
    already_broken: int = 0
    by_cause: dict = field(default_factory=dict)
    not_enough_history: int = 0
    largest_volatility_step: float | None = None
    largest_correlation_jump: float | None = None


class RegimeBreakDetector:
    """Watches the market for a step change, and declares each break once."""

    def __init__(
        self,
        established_window: int,
        recent_window: int,
        minimum_observations: int,
        volatility_step_multiple: float,
        correlation_jump: float,
        persistence_observations: int,
        now_ns=time.time_ns,
    ) -> None:
        if recent_window >= established_window:
            raise ValueError(
                "the recent window must be shorter than the established one, or there is "
                "nothing to compare against"
            )
        if volatility_step_multiple <= 1.0:
            raise ValueError(
                "a step is a move to a different level; a multiple at or below one would "
                "call every tick a break"
            )
        if persistence_observations < 2:
            raise ValueError(
                "a spike is not a break -- a step has to persist to be one, and that needs "
                "more than one observation"
            )
        self._established_window = established_window
        self._recent_window = recent_window
        self._minimum = minimum_observations
        self._volatility_multiple = volatility_step_multiple
        self._correlation_jump = correlation_jump
        self._persistence = persistence_observations
        self._now_ns = now_ns
        self._returns: dict[str, RollingWindow] = {}
        self._broken: dict[str, RegimeBreakAlert] = {}
        self._elevated_for: dict[str, int] = {}
        self._events: list = []
        self.standing = DetectorStanding()

    def observe_price(self, symbol: str, price: float) -> None:
        window = self._returns.get(symbol)
        if window is None:
            window = RollingWindow(length=self._established_window)
            self._returns[symbol] = window
        window.observe(price)

    def observe_market_event(self, event: str, symbols=()) -> None:
        """A delisting, a halt, a fork: an event that changes the rules.

        A break by construction, because waiting for the statistics to confirm
        it is waiting to be told what is already known.
        """
        self._events.append((event, tuple(symbols)))

    def is_broken(self, regime: str) -> bool:
        return regime in self._broken

    def clear(self, regime: str) -> None:
        """A new regime has been established. Only whoever established it may say so."""
        self._broken.pop(regime, None)
        self._elevated_for.pop(regime, None)

    def check(self, regime: str) -> RegimeBreakAlert:
        self.standing.checks += 1

        if regime in self._broken:
            # Declared once and held. A regime that flickers between broken and
            # intact is worse than either, because everything downstream
            # re-derives its response each time.
            self.standing.already_broken += 1
            return self._broken[regime]

        if self._events:
            event, symbols = self._events.pop(0)
            return self._declare(
                regime, MARKET_EVENT, None, None, None, None, event, symbols,
                f"{event}: an event that changes the rules is a break by construction, and "
                f"waiting for statistics to confirm it is waiting to be told what is known",
            )

        volatilities = {
            symbol: self._volatility_pair(window) for symbol, window in self._returns.items()
        }
        usable = {
            symbol: pair for symbol, pair in volatilities.items() if pair[0] and pair[1]
        }

        if not usable:
            self.standing.not_enough_history += 1
            return self._alert(
                regime, NOT_ENOUGH_HISTORY, False, None, None, None, None, None, (),
                f"no symbol has {self._minimum} observation(s) in both windows yet",
            )

        established = sum(pair[0] for pair in usable.values()) / len(usable)
        recent = sum(pair[1] for pair in usable.values()) / len(usable)
        step = recent / established if established > 0 else 1.0

        if step > self._volatility_multiple:
            self._elevated_for[regime] = self._elevated_for.get(regime, 0) + 1
            if (
                self.standing.largest_volatility_step is None
                or step > self.standing.largest_volatility_step
            ):
                self.standing.largest_volatility_step = step
            if self._elevated_for[regime] >= self._persistence:
                return self._declare(
                    regime, VOLATILITY_STEPPED, established, recent, None, None, None,
                    tuple(sorted(usable)),
                    f"volatility has been {step:.1f}x its established level for "
                    f"{self._elevated_for[regime]} consecutive check(s): "
                    f"{recent:.4%} against {established:.4%}. A spike is not a break; a step "
                    f"that persists is",
                )
        else:
            self._elevated_for[regime] = 0

        established_correlation, recent_correlation = self._correlation_pair()
        if (
            established_correlation is not None
            and recent_correlation is not None
            and recent_correlation - established_correlation > self._correlation_jump
        ):
            jump = recent_correlation - established_correlation
            if (
                self.standing.largest_correlation_jump is None
                or jump > self.standing.largest_correlation_jump
            ):
                self.standing.largest_correlation_jump = jump
            return self._declare(
                regime, CORRELATIONS_CONVERGED, established, recent,
                established_correlation, recent_correlation, None, tuple(sorted(usable)),
                f"average pairwise correlation has risen from {established_correlation:.2f} to "
                f"{recent_correlation:.2f}. Things that moved independently now move together, "
                f"which is what a liquidity event does and arrives before the volatility step",
            )

        return self._alert(
            regime, INTACT, False, established, recent, established_correlation,
            recent_correlation, None, tuple(sorted(usable)),
            f"volatility is {step:.2f}x its established level and correlation is "
            + (
                f"{recent_correlation:.2f} against {established_correlation:.2f}"
                if recent_correlation is not None
                else "not yet measurable across enough symbols"
            )
            + f"; nothing has stepped across {len(usable)} symbol(s)",
        )

    def _volatility_pair(self, window: RollingWindow) -> tuple:
        returns = window.returns()
        if len(returns) < self._minimum + self._recent_window:
            return None, None
        established = returns[: -self._recent_window]
        recent = returns[-self._recent_window :]
        return self._deviation(established), self._deviation(recent)

    def _deviation(self, values) -> float | None:
        if len(values) < 2:
            return None
        mean = sum(values) / len(values)
        return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))

    def _correlation_pair(self) -> tuple:
        """Average pairwise correlation, established against recent."""
        series = {
            symbol: window.returns()
            for symbol, window in self._returns.items()
            if window.count > self._minimum + self._recent_window
        }
        if len(series) < 2:
            return None, None

        symbols = sorted(series)
        established_values = []
        recent_values = []
        for index, left in enumerate(symbols):
            for right in symbols[index + 1 :]:
                length = min(len(series[left]), len(series[right]))
                left_series = series[left][-length:]
                right_series = series[right][-length:]
                established = correlation(
                    left_series[: -self._recent_window], right_series[: -self._recent_window]
                )
                recent = correlation(
                    left_series[-self._recent_window :], right_series[-self._recent_window :]
                )
                if established is not None:
                    established_values.append(abs(established))
                if recent is not None:
                    recent_values.append(abs(recent))

        return (
            sum(established_values) / len(established_values) if established_values else None,
            sum(recent_values) / len(recent_values) if recent_values else None,
        )

    def _declare(
        self, regime, state, established_volatility, recent_volatility,
        established_correlation, recent_correlation, event, symbols, reason,
    ) -> RegimeBreakAlert:
        self.standing.breaks_declared += 1
        self.standing.by_cause[state] = self.standing.by_cause.get(state, 0) + 1
        alert = self._alert(
            regime, state, True, established_volatility, recent_volatility,
            established_correlation, recent_correlation, event, symbols, reason,
        )
        self._broken[regime] = alert
        return alert

    def _alert(
        self, regime, state, has_broken, established_volatility, recent_volatility,
        established_correlation, recent_correlation, event, symbols, reason,
    ) -> RegimeBreakAlert:
        return RegimeBreakAlert(
            regime=regime,
            state=state,
            has_broken=has_broken,
            established_volatility=established_volatility,
            recent_volatility=recent_volatility,
            established_correlation=established_correlation,
            recent_correlation=recent_correlation,
            triggering_event=event,
            symbols_affected=symbols,
            reason=reason,
            raised_at_ns=self._now_ns(),
        )


def describe_regime_breaks(detector: RegimeBreakDetector) -> dict:
    return {
        "part_id": PART_ID,
        "checks": detector.standing.checks,
        "breaks_declared": detector.standing.breaks_declared,
        "checks_on_an_already_broken_regime": detector.standing.already_broken,
        "by_cause": dict(sorted(detector.standing.by_cause.items())),
        "checks_with_too_little_history": detector.standing.not_enough_history,
        "largest_volatility_step": detector.standing.largest_volatility_step,
        "largest_correlation_jump": detector.standing.largest_correlation_jump,
        "regimes_currently_broken": sorted(detector._broken),
        "symbols_watched": len(detector._returns),
    }


def run_regime_break_detector(
    detector: RegimeBreakDetector, control_socket, read_market_and_events, publish_alerts,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        regimes = read_market_and_events(detector)
        publish_alerts(tuple(detector.check(regime) for regime in regimes))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
