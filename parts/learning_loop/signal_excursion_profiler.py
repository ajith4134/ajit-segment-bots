"""signal-excursion-profiler: how far price travels around a claim the market settled.

**This part exists because the system could not otherwise place a stop.** Every
route to a trade needs an exit plan; the plan needs to know how far this symbol
normally moves against a call that turns out right; and the only measurement of
that was the excursion of a closed trade. A closed trade needs an open one, which
needs a stop. The full argument is in
`docs/proposals/live-excursion-and-horizon-profiling.md`.

Measured, not inferred: on the live run of 2026-08-23 the conviction model became
trained at 05:35 -- 102 labelled outcomes, both classes -- and the bot still formed
no opinion, because `bull-exit-plan-proposer` refuses without a fitted excursion
profile. 9 900 candidates, zero intents.

**A detector's claim already contains the measurement.** `signal-outcome-labeller`
tracks how far price ran for and against every open claim, on every tick, and
reports both on the label when the claim settles. This part is the distribution of
those numbers. It needs no trade, no position and no capital -- and it reads live
claims settled by live prices, never a replay (RL-071).

Three decisions about what is measured, and each one is a way of being wrong that
this avoids:

- **Only claims that came right feed the adverse quantile.** The stop's job is to
  survive the movement a working trade goes through. Including claims that were
  simply wrong measures how far price runs when the call was bad, which is
  unbounded, and would place every stop far enough away to guarantee the loss is
  large when it comes.
- **Favourable and adverse are kept per side.** An excursion means the opposite
  thing for a long and a short, and a profile that averaged them would describe
  neither.
- **The window is rolling.** A stop distance learned in a regime that has ended is
  a stop placed for a market that no longer exists.

What it will not do is report a distribution it cannot support. Below
`signal_excursion_minimum_claims` settled claims the profile is published with
`is_fitted` false and the proposer refuses it, which is the correct behaviour for a
symbol nobody has watched long enough.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from runtime.learning_types import THE_SETUP_WAS_RIGHT
from runtime.market_signal import LONG, SHORT
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trade_profiles import FROM_SETTLED_CLAIMS, ExcursionProfile, quantile_of

PART_ID = "signal-excursion-profiler"

PART_DECLARATION = PartDeclaration(
    part_id="signal-excursion-profiler",
    consumes=("training-label",),
    produces=("excursion-profile", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

PROFILED = "profiled"
TOO_FEW_CLAIMS = "too-few-settled-claims-to-take-a-quantile"
REFUSED_NO_DIRECTION = "the-label-names-no-tradeable-direction"
REFUSED_NO_SETUP_VERDICT = "the-label-says-nothing-about-whether-the-setup-was-right"


@dataclass
class ProfilerStanding:
    labels_seen: int = 0
    claims_recorded: int = 0
    claims_that_came_right: int = 0
    claims_that_came_wrong: int = 0
    profiles_published: int = 0
    profiles_fitted: int = 0
    refused: int = 0
    keys_tracked: int = 0
    widest_adverse_fraction: float = 0.0
    by_refusal: dict = field(default_factory=dict)


class SignalExcursionProfiler:
    """Turns settled claims into the distribution a stop and a target are set from."""

    def __init__(
        self,
        window: int,
        minimum_claims: int,
        adverse_quantile: float,
        favourable_quantiles: tuple,
        now_ns=time.time_ns,
    ) -> None:
        if window < 2:
            raise ValueError("a distribution over fewer than two observations is one observation")
        if minimum_claims < 2:
            raise ValueError(
                "a quantile taken over one claim is that claim, reported with the authority "
                "of a distribution"
            )
        if not 0.0 < adverse_quantile < 1.0:
            raise ValueError(
                "the adverse quantile names a point inside the distribution; 0 and 1 are the "
                "smallest and largest observation, which a sample says least about"
            )
        if not favourable_quantiles:
            raise ValueError("with no favourable quantile there is nowhere to put a target")
        self._window = window
        self._minimum_claims = minimum_claims
        self._adverse_quantile = adverse_quantile
        self._favourable_quantiles = tuple(favourable_quantiles)
        self._now_ns = now_ns
        # Adverse excursions of claims that came right, and favourable excursions
        # of the same claims, per venue, symbol and side.
        self._adverse: dict[tuple[str, str, str], deque] = {}
        self._favourable: dict[tuple[str, str, str], deque] = {}
        self.standing = ProfilerStanding()

    def observe_label(self, label) -> str:
        """Record one settled claim, or refuse it and say why."""
        self.standing.labels_seen += 1

        side = label.direction
        if side not in (LONG, SHORT):
            return self._refuse(REFUSED_NO_DIRECTION)

        came_right = label.label_for(THE_SETUP_WAS_RIGHT)
        if came_right is None:
            return self._refuse(REFUSED_NO_SETUP_VERDICT)

        if not came_right:
            # Counted, not recorded. How far price runs when a call was simply
            # wrong is unbounded, and a stop placed beyond it would make every
            # loss as large as the worst one ever seen.
            self.standing.claims_that_came_wrong += 1
            return PROFILED

        key = (label.venue_id, label.symbol, side)
        # Stored positive: an adverse excursion is movement against the claim, and
        # a sign that flips with the side is a number every reader has to
        # re-interpret.
        adverse = abs(min(0.0, label.worst_adverse_fraction))
        favourable = max(0.0, label.best_favourable_fraction)
        self._adverse.setdefault(key, deque(maxlen=self._window)).append(adverse)
        self._favourable.setdefault(key, deque(maxlen=self._window)).append(favourable)

        self.standing.claims_recorded += 1
        self.standing.claims_that_came_right += 1
        self.standing.keys_tracked = len(self._adverse)
        self.standing.widest_adverse_fraction = max(
            self.standing.widest_adverse_fraction, adverse
        )
        return PROFILED

    def profile(self, venue_id: str, symbol: str, side: str) -> ExcursionProfile:
        """The distribution for one symbol and side, fitted or honestly not."""
        key = (venue_id, symbol, side)
        adverse = self._adverse.get(key, ())
        favourable = self._favourable.get(key, ())
        observed = len(adverse)
        now = self._now_ns()

        if observed < self._minimum_claims:
            return ExcursionProfile(
                venue_id=venue_id,
                symbol=symbol,
                side=side,
                adverse_excursion=0.0,
                favourable_quantiles={},
                trades_observed=observed,
                is_fitted=False,
                source=FROM_SETTLED_CLAIMS,
                reason=(
                    f"{observed} settled claim(s) of the {self._minimum_claims} this needs "
                    f"before a quantile means anything; until then no stop may be placed from it"
                ),
                measured_at_ns=now,
            )

        adverse_excursion = quantile_of(adverse, self._adverse_quantile)
        quantiles = {}
        for quantile in self._favourable_quantiles:
            reached = quantile_of(favourable, quantile)
            if reached is not None and reached > 0:
                quantiles[quantile] = reached

        self.standing.profiles_fitted += 1
        return ExcursionProfile(
            venue_id=venue_id,
            symbol=symbol,
            side=side,
            adverse_excursion=adverse_excursion,
            favourable_quantiles=quantiles,
            trades_observed=observed,
            is_fitted=True,
            source=FROM_SETTLED_CLAIMS,
            reason=(
                f"over {observed} claim(s) this detector got right in {symbol}, the "
                f"{self._adverse_quantile:.0%} adverse excursion is {adverse_excursion:.2%} and "
                f"{len(quantiles)} favourable quantile(s) were reachable; measured from claims "
                f"the market settled, not from closed trades, which do not exist yet"
            ),
            measured_at_ns=now,
        )

    def profile_all(self) -> tuple[ExcursionProfile, ...]:
        """Every symbol and side this part has seen a claim for."""
        profiles = tuple(
            self.profile(venue_id, symbol, side)
            for venue_id, symbol, side in sorted(self._adverse)
        )
        self.standing.profiles_published += len(profiles)
        return profiles

    def _refuse(self, reason: str) -> str:
        self.standing.refused += 1
        self.standing.by_refusal[reason] = self.standing.by_refusal.get(reason, 0) + 1
        return reason


def describe_excursion_profiling(profiler: SignalExcursionProfiler) -> dict:
    return {
        "part_id": PART_ID,
        "labels_seen": profiler.standing.labels_seen,
        "claims_recorded": profiler.standing.claims_recorded,
        "claims_that_came_right": profiler.standing.claims_that_came_right,
        "claims_that_came_wrong": profiler.standing.claims_that_came_wrong,
        "profiles_published": profiler.standing.profiles_published,
        "profiles_fitted": profiler.standing.profiles_fitted,
        "keys_tracked": profiler.standing.keys_tracked,
        "widest_adverse_fraction": profiler.standing.widest_adverse_fraction,
        "refused": profiler.standing.refused,
        "by_refusal": dict(profiler.standing.by_refusal),
    }


def run_signal_excursion_profiler(
    profiler: SignalExcursionProfiler, control_socket, read_labels, publish_profiles,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        for label in read_labels():
            profiler.observe_label(label)
        publish_profiles(profiler.profile_all())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The favourable quantiles are the ones `bull_exit_target_quantiles` already
    names, read here rather than given their own setting: a profile that priced
    quantiles nobody asked for would be measuring one thing while being read for
    another, and the proposer looks up its targets by exact quantile.
    """
    from runtime.input_assembly import Batch

    labels = Batch(read=context.bus.reader("training-label"))
    publish_profiles = context.bus.publisher_for("excursion-profile")

    # The quantiles the proposer looks its targets up by, exactly. It reads them
    # from `favourable_quantiles` by key, so a profile that priced 0.5 when the
    # proposer asks for 0.50000001 would silently price no target at all.
    favourable = tuple(
        float(quantile) for quantile in context.setting("bull_exit_target_quantiles").value
    )

    return run_signal_excursion_profiler(
        profiler=SignalExcursionProfiler(
            window=int(context.number("signal_excursion_window")),
            minimum_claims=int(context.number("signal_excursion_minimum_claims")),
            adverse_quantile=context.number("signal_excursion_adverse_quantile"),
            favourable_quantiles=favourable,
        ),
        control_socket=context.control_socket,
        read_labels=labels.payloads,
        publish_profiles=publish_profiles,
        health_interval_seconds=context.health_interval_seconds,
        emit_health=context.emit_health,
    )
