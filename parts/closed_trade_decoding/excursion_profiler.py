"""excursion-profiler: how far trades run before they resolve (RL-042).

A stop distance and a target distance are usually guessed at, or copied from a
tutorial, or set as a round percentage. This part replaces the guess with the
distribution: across many trades in a symbol and regime, how far did the position go
in favour before it resolved, and how far did it go against.

The distribution is the deliverable, not its mean. Three consequences:

- **Quantiles, not averages.** A stop set at the mean adverse excursion is hit by
  half of all trades that eventually worked. The useful number is the upper quantile
  -- the distance that all but a small fraction of winners stay inside.
- **Split by regime.** Excursions in a trending market and a choppy one are different
  distributions, and pooling them produces a number that fits neither. A profile
  without a regime label is reported as a pooled profile, explicitly.
- **Winners and losers are profiled separately where possible.** The adverse
  excursion of trades that eventually won is exactly what a stop must accommodate;
  including the trades that just kept going against makes it far too wide.

Sample size gates everything. A profile from eight trades is a description of eight
trades, and the part reports `is_fitted` false rather than a confident quantile --
because a stop set from a thin sample is a guess with a decimal point.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.trade_decoding_types import PeakExcursionProfile
from runtime.rolling_statistics import RollingWindow
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "excursion-profiler"

PART_DECLARATION = PartDeclaration(
    part_id="excursion-profiler",
    consumes=("peak-excursion", "trade-episode"),
    produces=("excursion-profile", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PROFILED = "profiled"
TOO_FEW_TRADES = "too-few-trades-for-a-distribution"
NOTHING_RECORDED = "no-excursion-has-been-recorded-for-this-key"

POOLED = "pooled-across-regimes"


@dataclass(frozen=True)
class ProfileOutcome:
    venue_id: str
    symbol: str
    regime: str | None
    state: str
    profile: PeakExcursionProfile
    winners_only: bool
    reason: str
    measured_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == PROFILED


@dataclass
class ProfilerStanding:
    excursions_recorded: int = 0
    profiles_built: int = 0
    profiles_refused_thin: int = 0
    keys_seen: int = 0
    winners_recorded: int = 0
    losers_recorded: int = 0


class ExcursionProfiler:
    """Builds favourable and adverse excursion distributions per symbol and regime."""

    def __init__(
        self,
        window: int,
        quantile: float,
        minimum_trades: int,
        now_ns=time.time_ns,
    ) -> None:
        if window < 2:
            raise ValueError("a distribution over one observation is that observation")
        if not 0.0 < quantile < 1.0:
            raise ValueError(
                "the quantile is what a stop must accommodate; the mean adverse "
                "excursion is hit by half of all trades that eventually worked"
            )
        if minimum_trades < 2:
            raise ValueError(
                "a stop set from a thin sample is a guess with a decimal point"
            )
        self._window = window
        self._quantile = quantile
        self._minimum_trades = minimum_trades
        self._now_ns = now_ns
        self._favourable: dict[tuple, RollingWindow] = {}
        self._adverse: dict[tuple, RollingWindow] = {}
        self._counts: dict[tuple, int] = {}
        self.standing = ProfilerStanding()

    def observe_excursion(
        self, venue_id: str, symbol: str, regime: str | None,
        favourable: float, adverse: float, was_a_winner: bool,
    ) -> None:
        """Recorded under three keys: pooled, per regime, and winners-only."""
        self.standing.excursions_recorded += 1
        if was_a_winner:
            self.standing.winners_recorded += 1
        else:
            self.standing.losers_recorded += 1

        keys = [(venue_id, symbol, None, False), (venue_id, symbol, regime, False)]
        if was_a_winner:
            keys.append((venue_id, symbol, regime, True))
            keys.append((venue_id, symbol, None, True))

        for key in keys:
            if key not in self._counts:
                self.standing.keys_seen += 1
            self._favourable.setdefault(key, RollingWindow(self._window)).observe(favourable)
            self._adverse.setdefault(key, RollingWindow(self._window)).observe(adverse)
            self._counts[key] = self._counts.get(key, 0) + 1

    def profile(
        self, venue_id: str, symbol: str, regime: str | None = None,
        winners_only: bool = False,
    ) -> ProfileOutcome:
        key = (venue_id, symbol, regime, winners_only)
        count = self._counts.get(key, 0)
        favourable = self._favourable.get(key)
        adverse = self._adverse.get(key)

        if count == 0 or favourable is None or adverse is None:
            return self._outcome(
                venue_id, symbol, regime, NOTHING_RECORDED,
                self._empty_profile(venue_id, symbol, regime, 0), winners_only,
                "no excursion has been recorded for this key, which is not the same as "
                "trades running no distance",
            )

        if count < self._minimum_trades:
            self.standing.profiles_refused_thin += 1
            return self._outcome(
                venue_id, symbol, regime, TOO_FEW_TRADES,
                self._empty_profile(venue_id, symbol, regime, count), winners_only,
                f"{count} trade(s), below the {self._minimum_trades} needed. This is a "
                f"description of {count} trade(s) rather than a distribution",
            )

        self.standing.profiles_built += 1
        return self._outcome(
            venue_id, symbol, regime, PROFILED,
            PeakExcursionProfile(
                venue_id=venue_id,
                symbol=symbol,
                regime=regime,
                trades=count,
                median_favourable=favourable.quantile(0.5, self._minimum_trades),
                median_adverse=adverse.quantile(0.5, self._minimum_trades),
                favourable_quantile=favourable.quantile(self._quantile, self._minimum_trades),
                adverse_quantile=adverse.quantile(self._quantile, self._minimum_trades),
                quantile=self._quantile,
                is_fitted=True,
                measured_at_ns=self._now_ns(),
            ),
            winners_only,
            f"{count} trade(s)"
            + (f" in the {regime} regime" if regime else f" {POOLED}")
            + (
                ", winners only -- which is what a stop must accommodate, since including "
                "trades that just kept going against makes it far too wide"
                if winners_only
                else ""
            ),
        )

    def _empty_profile(self, venue_id, symbol, regime, count) -> PeakExcursionProfile:
        return PeakExcursionProfile(
            venue_id=venue_id, symbol=symbol, regime=regime, trades=count,
            median_favourable=None, median_adverse=None, favourable_quantile=None,
            adverse_quantile=None, quantile=self._quantile, is_fitted=False,
            measured_at_ns=self._now_ns(),
        )

    def _outcome(
        self, venue_id, symbol, regime, state, profile, winners_only, reason,
    ) -> ProfileOutcome:
        return ProfileOutcome(
            venue_id=venue_id, symbol=symbol, regime=regime, state=state,
            profile=profile, winners_only=winners_only, reason=reason,
            measured_at_ns=self._now_ns(),
        )


def describe_excursion_profiling(profiler: ExcursionProfiler) -> dict:
    return {
        "part_id": PART_ID,
        "excursions_recorded": profiler.standing.excursions_recorded,
        "profiles_built": profiler.standing.profiles_built,
        "profiles_refused_for_a_thin_sample": profiler.standing.profiles_refused_thin,
        "keys_seen": profiler.standing.keys_seen,
        "winners_recorded": profiler.standing.winners_recorded,
        "losers_recorded": profiler.standing.losers_recorded,
        "quantile": profiler._quantile,
        "reports_a_mean_as_the_stop_distance": False,
        "pools_regimes_silently": False,
    }


def run_excursion_profiler(
    profiler: ExcursionProfiler, control_socket, read_excursions, publish_profiles,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        for job in read_excursions():
            profiler.observe_excursion(**job)
        for venue_id, symbol, regime, winners_only in profiler._counts:
            outcome = profiler.profile(venue_id, symbol, regime, winners_only)
            if outcome.is_usable:
                publish_profiles(outcome.profile)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
