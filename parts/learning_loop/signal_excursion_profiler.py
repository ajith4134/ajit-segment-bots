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

import pathlib
import time
from collections import deque
from dataclasses import dataclass, field

from runtime.learning_types import THE_SETUP_WAS_RIGHT
from runtime.market_signal import LONG, SHORT
from runtime.part_declaration import PartDeclaration
from runtime.learned_state import (
    STARTED_COLD_UNREADABLE,
    CheckpointSchedule,
    LearnedStateStore,
)
from runtime.part_process import run_part
from runtime.trade_profiles import FROM_SETTLED_CLAIMS, ExcursionProfile, quantile_of

PART_ID = "signal-excursion-profiler"

# What this part stores under its own name in the learned-state directory.
COMPONENT = "excursions"

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
    # Where this process's measurements came from, and what it has written
    # since. On the standing rather than kept privately because "it started
    # cold again" is exactly the fact an operator needs and the one a
    # restart hides.
    checkpoint_verdict: str | None = None
    checkpoint_detail: str | None = None
    checkpoint_saved_at_ns: int | None = None
    checkpoints_written: int = 0


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

    # -- carrying what was measured across the off switch --------------------

    @property
    def claims_recorded(self) -> int:
        return self.standing.claims_recorded

    @property
    def closest_to_fitted(self) -> tuple[str, int] | None:
        """The symbol and side nearest to having a usable profile, and its count.

        Reported because "how far is the bot from its first exit plan" is otherwise
        invisible: the gate is per symbol and per side, so a total that looks large
        can be thirty symbols with four claims each. A board reading only the total
        would say the wait is nearly over when it has barely started (Rule 8).
        """
        if not self._adverse:
            return None
        key = max(self._adverse, key=lambda name: len(self._adverse[name]))
        venue_id, symbol, side = key
        return f"{venue_id} {symbol} {side}", len(self._adverse[key])

    @property
    def keys_fitted(self) -> int:
        return sum(
            1 for observations in self._adverse.values()
            if len(observations) >= self._minimum_claims
        )

    def learned_settings(self) -> dict:
        """What gives the stored excursions their meaning.

        The window is the span they were kept over and the minimum decides whether
        they may be used; the quantiles only decide what is read off them, so
        changing a quantile is ordinary tuning and must not discard a day of
        measurement.
        """
        return {"window": self._window, "minimum_claims": self._minimum_claims}

    def state(self) -> dict:
        return {
            "adverse": {
                "|".join(key): list(values) for key, values in sorted(self._adverse.items())
            },
            "favourable": {
                "|".join(key): list(values) for key, values in sorted(self._favourable.items())
            },
            "claims_recorded": self.standing.claims_recorded,
            "claims_that_came_right": self.standing.claims_that_came_right,
            "claims_that_came_wrong": self.standing.claims_that_came_wrong,
        }

    def restore_state(self, state: dict) -> None:
        self._adverse = {
            tuple(key.split("|")): deque(values, maxlen=self._window)
            for key, values in state["adverse"].items()
        }
        self._favourable = {
            tuple(key.split("|")): deque(values, maxlen=self._window)
            for key, values in state["favourable"].items()
        }
        self.standing.claims_recorded = int(state["claims_recorded"])
        self.standing.claims_that_came_right = int(state["claims_that_came_right"])
        self.standing.claims_that_came_wrong = int(state["claims_that_came_wrong"])
        self.standing.keys_tracked = len(self._adverse)


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
        "keys_fitted": profiler.keys_fitted,
        "closest_to_fitted": profiler.closest_to_fitted,
    }



def restore_or_start_cold(profiler, store, part_id: str) -> None:
    """Adopt the previous process's measurements, or record why this one starts cold.

    Never raises past a part's start: a checkpoint that cannot be adopted is a
    reason to begin measuring again, not a reason to refuse to run. What it costs
    is real -- the gate is per key and a restart that lost the counts would put
    every symbol back to nothing -- so the reason is put on the standing rather
    than swallowed.
    """
    restoration = store.restore(part_id, COMPONENT, profiler.learned_settings())
    profiler.standing.checkpoint_saved_at_ns = restoration.saved_at_ns
    if not restoration.was_restored:
        profiler.standing.checkpoint_verdict = restoration.verdict
        profiler.standing.checkpoint_detail = restoration.detail
        return
    try:
        profiler.restore_state(restoration.state)
    except (KeyError, TypeError, ValueError) as refusal:
        profiler.standing.checkpoint_verdict = STARTED_COLD_UNREADABLE
        profiler.standing.checkpoint_detail = (
            f"{restoration.detail}, but it could not be adopted: {refusal}"
        )
        return
    profiler.standing.checkpoint_verdict = restoration.verdict
    profiler.standing.checkpoint_detail = restoration.detail

def run_signal_excursion_profiler(
    profiler: SignalExcursionProfiler, control_socket, read_labels, publish_profiles,
    health_interval_seconds: float, emit_health, checkpoint=None,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    """Observe what arrived, and publish the profiles that moved because of it.

    A profile is a level: "this symbol travels this far against a correct long
    call" is true until a new label changes it. Measured on the live spine at
    10:26 on 2026-08-26, the full set of 403 profiles went out on every tick --
    381 messages a second built from 0 training labels a second, describing
    nothing that had changed since the tick before.

    Profiling every key stays on every tick. It is the publish that is skipped,
    not the measurement: a profiler that stopped profiling would answer the next
    label with a stale distribution.
    """

    def tick() -> None:
        for label in read_labels():
            profiler.observe_label(label)
        for profile in profiler.profile_all():
            publish_profiles(profile)
        if checkpoint is not None:
            checkpoint(profiler)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_excursion_profiling(profiler),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The favourable quantiles are the ones `bull_exit_target_quantiles` already
    names, read here rather than given their own setting: a profile that priced
    quantiles nobody asked for would be measuring one thing while being read for
    another, and the proposer looks up its targets by exact quantile.
    """
    from runtime.input_assembly import Batch

    from runtime.level_publishing import LevelPublisherByKey

    labels = Batch(read=context.bus.reader("training-label"))
    # Keyed by exactly what makes one profile a different profile, so a label for
    # one symbol does not restate the other four hundred.
    profile_levels = LevelPublisherByKey(
        publish=context.bus.publisher_for("excursion-profile"),
        refresh_interval_seconds=context.number("level_refresh_interval_seconds"),
    )

    def publish_profiles(profile) -> None:
        profile_levels.publish_level(
            (profile.venue_id, profile.symbol, profile.side), (profile,)
        )

    # The quantiles the proposer looks its targets up by, exactly. It reads them
    # from `favourable_quantiles` by key, so a profile that priced 0.5 when the
    # proposer asks for 0.50000001 would silently price no target at all.
    favourable = tuple(
        float(quantile) for quantile in context.setting("bull_exit_target_quantiles").value
    )

    profiler = SignalExcursionProfiler(
        window=int(context.number("signal_excursion_window")),
        minimum_claims=int(context.number("signal_excursion_minimum_claims")),
        adverse_quantile=context.number("signal_excursion_adverse_quantile"),
        favourable_quantiles=favourable,
    )

    # Measurements carried across the off switch, in the same place the conviction
    # model keeps its coefficients. The gate here is per symbol and per side, so a
    # restart that lost the counts would put every symbol back to nothing -- and
    # the checkpoint is also what lets a board say how far the bot is from its
    # first exit plan, which is otherwise invisible (Rule 8).
    store = LearnedStateStore(
        pathlib.Path(str(context.setting("learned_state_root").value)).expanduser()
    )
    store.root.mkdir(parents=True, exist_ok=True)
    restore_or_start_cold(profiler, store, PART_ID)
    schedule = CheckpointSchedule(int(context.number("learned_state_checkpoint_interval")))

    def checkpoint(profiler: SignalExcursionProfiler) -> None:
        recorded = profiler.claims_recorded
        if not schedule.is_due(recorded):
            return
        store.save(PART_ID, COMPONENT, profiler.state(), profiler.learned_settings())
        schedule.record_written(recorded)
        profiler.standing.checkpoints_written += 1

    return run_signal_excursion_profiler(
        profiler=profiler,
        control_socket=context.control_socket,
        read_labels=labels.payloads,
        publish_profiles=publish_profiles,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
        checkpoint=checkpoint,
    )
