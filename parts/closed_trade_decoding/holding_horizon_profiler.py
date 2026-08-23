"""holding-horizon-profiler: how long an edge survives, per setup.

Holding periods are usually inherited from a habit -- intraday because the strategy
is "intraday" -- rather than measured. This part measures them: across many trades of
the same setup, what would the payoff have been at each horizon, and where does it
stop improving.

Built from counterfactual exits, which makes one property non-negotiable: **the
horizon that looks best on the trades already seen is not the best horizon.** It is
the argmax of a noisy curve, and picking argmax is how a system ends up holding for
exactly forty-seven minutes because that happened to work twice. So:

- **A horizon wins only by a stated margin over its neighbours.** A curve whose peak
  is inside the noise of the curve is reported as flat, which is the honest answer
  and the common one.
- **Decay is what is actually looked for.** The useful finding is not the peak but
  the point after which the payoff falls -- that is where an edge is spent and
  holding longer is paying costs for nothing.
- **Sample size gates the whole profile.** Twelve trades produce twelve-trade
  curves, and a curve is reported unfitted rather than smoothed into confidence.

The profiler names a horizon; nothing here changes an exit. Exits are decided by the
bots with the position in front of them, and a profile is an input to that rather
than an override.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

from runtime.trade_decoding_types import HorizonProfile
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "holding-horizon-profiler"

PART_DECLARATION = PartDeclaration(
    part_id="holding-horizon-profiler",
    consumes=("trade-episode", "exit-counterfactual"),
    produces=("horizon-profile", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PROFILED = "profiled"
FLAT = "no-horizon-beats-its-neighbours-by-enough-to-matter"
TOO_FEW_TRADES = "too-few-trades-for-a-curve"
NO_HORIZONS = "no-horizon-has-been-measured-for-this-setup"


@dataclass(frozen=True)
class HorizonOutcome:
    setup: str
    state: str
    profile: HorizonProfile
    curve: dict
    reason: str
    measured_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state in (PROFILED, FLAT)


@dataclass
class ProfilerStanding:
    observations: int = 0
    setups_seen: int = 0
    profiles_built: int = 0
    flat_curves: int = 0
    thin_samples: int = 0
    setups_with_a_decay_point: int = 0
    changes_an_exit: int = 0


class HoldingHorizonProfiler:
    """Builds a payoff-by-horizon curve per setup and finds where the edge decays."""

    def __init__(
        self,
        horizons_seconds,
        minimum_trades: int,
        winning_margin: float,
        decay_fraction: float,
        now_ns=time.time_ns,
    ) -> None:
        horizons = tuple(sorted(horizons_seconds))
        if len(horizons) < 2:
            raise ValueError("a curve needs more than one horizon")
        if minimum_trades < 2:
            raise ValueError(
                "twelve trades produce twelve-trade curves; a curve from fewer than two "
                "is a line through one point"
            )
        if winning_margin <= 0:
            raise ValueError(
                "picking the argmax of a noisy curve is how a system ends up holding for "
                "exactly forty-seven minutes because that happened to work twice"
            )
        if not 0.0 < decay_fraction < 1.0:
            raise ValueError(
                "decay is the fraction of the peak payoff below which the edge is spent"
            )
        self._horizons = horizons
        self._minimum_trades = minimum_trades
        self._winning_margin = winning_margin
        self._decay_fraction = decay_fraction
        self._now_ns = now_ns
        self._payoffs: dict[tuple, list] = {}
        self._setups: set = set()
        self.standing = ProfilerStanding()

    def observe_counterfactual(
        self, setup: str, horizon_seconds: float, realised: float,
    ) -> None:
        """One trade's payoff had it been held to this horizon."""
        if horizon_seconds not in self._horizons:
            raise ValueError(
                f"{horizon_seconds} is not one of the declared horizons; adding horizons "
                f"after seeing results is how a curve is fitted to its own data"
            )
        if setup not in self._setups:
            self._setups.add(setup)
            self.standing.setups_seen += 1
        self._payoffs.setdefault((setup, horizon_seconds), []).append(realised)
        self.standing.observations += 1

    def curve_for(self, setup: str) -> dict:
        curve = {}
        for horizon in self._horizons:
            payoffs = self._payoffs.get((setup, horizon), [])
            if payoffs:
                curve[horizon] = statistics.mean(payoffs)
        return curve

    def trades_for(self, setup: str) -> int:
        counts = [
            len(self._payoffs.get((setup, horizon), [])) for horizon in self._horizons
        ]
        return min(counts) if counts else 0

    def profile(self, setup: str) -> HorizonOutcome:
        curve = self.curve_for(setup)
        trades = self.trades_for(setup)

        if not curve:
            return self._outcome(
                setup, NO_HORIZONS,
                self._empty(setup, 0, {}),
                {},
                "no horizon has been measured for this setup",
            )

        if trades < self._minimum_trades:
            self.standing.thin_samples += 1
            return self._outcome(
                setup, TOO_FEW_TRADES,
                self._empty(setup, trades, curve), curve,
                f"{trades} trade(s) at every horizon, below the {self._minimum_trades} "
                f"needed. The curve is reported unfitted rather than smoothed into "
                f"confidence",
            )

        best_horizon = max(curve, key=lambda horizon: curve[horizon])
        best_payoff = curve[best_horizon]
        others = [payoff for horizon, payoff in curve.items() if horizon != best_horizon]
        margin = best_payoff - max(others) if others else 0.0

        # Where the payoff falls away from the peak: that is where the edge is spent.
        decays_after = None
        for horizon in sorted(curve):
            if horizon <= best_horizon:
                continue
            if curve[horizon] < best_payoff * self._decay_fraction:
                decays_after = horizon
                break
        if decays_after is not None:
            self.standing.setups_with_a_decay_point += 1

        if margin < self._winning_margin:
            self.standing.flat_curves += 1
            return self._outcome(
                setup, FLAT,
                HorizonProfile(
                    setup=setup, trades=trades, best_horizon_seconds=None,
                    payoff_by_horizon=dict(curve), decays_after_seconds=decays_after,
                    is_fitted=True,
                    reason=(
                        f"the best horizon beats its neighbours by {margin:+.4f}, inside "
                        f"the {self._winning_margin:.4f} margin. The curve is flat, which "
                        f"is the honest answer and the common one"
                    ),
                    measured_at_ns=self._now_ns(),
                ),
                curve,
                "flat curve",
            )

        self.standing.profiles_built += 1
        return self._outcome(
            setup, PROFILED,
            HorizonProfile(
                setup=setup, trades=trades, best_horizon_seconds=best_horizon,
                payoff_by_horizon=dict(curve), decays_after_seconds=decays_after,
                is_fitted=True,
                reason=(
                    f"{best_horizon / 60.0:.0f} minute(s) pays {best_payoff:+.4f} on "
                    f"average across {trades} trade(s), {margin:+.4f} ahead of the next "
                    f"horizon"
                    + (
                        f". The payoff falls away after {decays_after / 60.0:.0f} "
                        f"minute(s) -- that is where the edge is spent and holding longer "
                        f"pays costs for nothing"
                        if decays_after
                        else ". No decay point is visible within the measured horizons"
                    )
                ),
                measured_at_ns=self._now_ns(),
            ),
            curve,
            f"best at {best_horizon / 60.0:.0f} minute(s)",
        )

    def _empty(self, setup, trades, curve) -> HorizonProfile:
        return HorizonProfile(
            setup=setup, trades=trades, best_horizon_seconds=None,
            payoff_by_horizon=dict(curve), decays_after_seconds=None, is_fitted=False,
            reason="not enough evidence for a curve", measured_at_ns=self._now_ns(),
        )

    def _outcome(self, setup, state, profile, curve, reason) -> HorizonOutcome:
        return HorizonOutcome(
            setup=setup, state=state, profile=profile, curve=dict(curve), reason=reason,
            measured_at_ns=self._now_ns(),
        )


def describe_horizon_profiling(profiler: HoldingHorizonProfiler) -> dict:
    return {
        "part_id": PART_ID,
        "observations": profiler.standing.observations,
        "setups_seen": profiler.standing.setups_seen,
        "profiles_built": profiler.standing.profiles_built,
        "flat_curves": profiler.standing.flat_curves,
        "thin_samples": profiler.standing.thin_samples,
        "setups_with_a_decay_point": profiler.standing.setups_with_a_decay_point,
        "horizons_seconds": list(profiler._horizons),
        "picks_the_argmax": False,
        "changes_an_exit": False,
    }


def run_holding_horizon_profiler(
    profiler: HoldingHorizonProfiler, control_socket, read_counterfactuals,
    publish_profiles, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for setup, horizon, realised in read_counterfactuals():
            profiler.observe_counterfactual(setup, horizon, realised)
        for setup in sorted(profiler._setups):
            outcome = profiler.profile(setup)
            if outcome.is_usable:
                publish_profiles(outcome.profile)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
