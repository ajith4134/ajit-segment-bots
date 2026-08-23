"""bull-position-invalidation-watcher: when the reason for being long stopped being true.

Every other part in this bot decides whether to open something. This one watches
what is already open, and it exists because the two are different questions
answered from different evidence. An entry is judged on a setup; a held position
is judged on whether the conditions that justified it still hold -- and those can
fail long before the stop is reached.

Three ways a long stops being justified, and none of them is "it is losing":

1. **The features moved back.** The book imbalance that made the entry a long has
   flipped, the volatility that made the target reachable has collapsed. The
   thesis has expired even though the price has not moved much.
2. **The regime broke.** `regime-break-alert` says the market this trade was
   entered into is not the market it is in now, and every model that formed the
   entry was fitted on the old one.
3. **The horizon ran out.** A trade past the horizon it was given is not a trade,
   it is a position nobody decided to hold.

It produces `directional-opinion` -- the same type as the composer, on purpose.
A bot changing its mind is an opinion the arbiter weighs, not a control message
that bypasses it (T-2): the risk gate and the arbiter stay in the path, because a
part that could close a position directly would be a second, hidden execution
route with none of the first one's checks.

**Reduce is a real answer.** Between "hold" and "close" sits "this is less true
than it was", and a watcher that could only do the two extremes would either hold
losing theses or dump good positions on one noisy tick.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import (
    CLOSE_POSITION, LONG, REDUCE_POSITION, STAND_DOWN, DirectionalOpinion,
)
from runtime.learned_estimator import Estimate, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "bull-position-invalidation-watcher"
BOT = "bull-bot"

PART_DECLARATION = PartDeclaration(
    part_id="bull-position-invalidation-watcher",
    consumes=("position", "market-data", "bull-feature-vector", "regime-break-alert"),
    produces=("directional-opinion", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

STILL_VALID = "thesis-still-holds"
FEATURES_REVERSED = "entry-features-have-reversed"
REGIME_BROKEN = "regime-this-was-entered-into-has-broken"
HORIZON_EXPIRED = "past-the-horizon-it-was-given"
NO_ENTRY_RECORD = "no-entry-features-recorded-for-this-position"


@dataclass
class HeldThesis:
    """What was true when this position was opened, kept so it can be checked.

    Kept per position rather than recomputed: the question is whether the
    entry's reasons still hold, and that cannot be asked without the reasons.
    """

    venue_id: str
    symbol: str
    entry_features: dict
    entry_regime: str
    entry_price: float
    horizon_seconds: float
    opened_at_ns: int
    detector: str


@dataclass
class WatcherStanding:
    positions_watched: int = 0
    checks: int = 0
    close_calls: int = 0
    reduce_calls: int = 0
    held: int = 0
    by_reason: dict = field(default_factory=dict)


class BullPositionInvalidationWatcher:
    """Checks open longs against the reasons they were opened for."""

    def __init__(
        self,
        reversal_fraction_to_reduce: float,
        reversal_fraction_to_close: float,
        prior_invalidation_hit_rate: float,
        prior_weight: float,
        half_life_observations: float,
        minimum_observations: int,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < reversal_fraction_to_reduce <= reversal_fraction_to_close <= 1.0:
            raise ValueError(
                "the reduce threshold must be reached before the close threshold, or the "
                "watcher can only ever do the extreme"
            )
        self._reduce_at = reversal_fraction_to_reduce
        self._close_at = reversal_fraction_to_close
        self._minimum = minimum_observations
        self._now_ns = now_ns
        self._theses: dict[tuple[str, str], HeldThesis] = {}
        self._broken_regimes: set[str] = set()
        self._was_right = RateEstimator(
            prior=prior_invalidation_hit_rate, prior_weight=prior_weight,
            half_life_observations=half_life_observations,
        )
        self.standing = WatcherStanding()

    def record_entry(self, thesis: HeldThesis) -> None:
        """What justified this position, kept for as long as it is held."""
        self._theses[(thesis.venue_id, thesis.symbol)] = thesis
        self.standing.positions_watched = len(self._theses)

    def forget_position(self, venue_id: str, symbol: str) -> None:
        """A closed position releases its thesis. T-3: nothing accumulates for ever."""
        self._theses.pop((venue_id, symbol), None)
        self.standing.positions_watched = len(self._theses)

    def observe_regime_break(self, regime: str, has_broken: bool) -> None:
        if has_broken:
            self._broken_regimes.add(regime)
        else:
            self._broken_regimes.discard(regime)

    def observe_outcome(self, was_right: bool) -> None:
        """Whether closing early was the right call, so the watcher is judged too.

        Without this the watcher is the one part of the bot with no record, and
        a watcher that panics is indistinguishable from one that saves money.
        """
        self._was_right.observe(was_right)

    def check(self, position, vector) -> DirectionalOpinion:
        """One held long against the reasons it was opened for."""
        self.standing.checks += 1
        key = (position.venue_id, position.symbol)
        thesis = self._theses.get(key)

        if thesis is None:
            return self._opinion(
                position, STAND_DOWN, NO_ENTRY_RECORD, 0.0,
                "no entry features were recorded for this position, so there is nothing to "
                "compare against; this watcher will not invent a reason to close a trade it "
                "cannot judge",
                vector,
            )

        age_seconds = (self._now_ns() - thesis.opened_at_ns) / 1e9
        if age_seconds > thesis.horizon_seconds:
            return self._opinion(
                position, CLOSE_POSITION, HORIZON_EXPIRED, 1.0,
                f"open for {age_seconds:.0f}s against the {thesis.horizon_seconds:.0f}s this "
                f"trade was given; past its horizon it is not a trade any more, it is a "
                f"position nobody decided to hold",
                vector,
            )

        if thesis.entry_regime in self._broken_regimes:
            return self._opinion(
                position, CLOSE_POSITION, REGIME_BROKEN, 1.0,
                f"this long was entered in the {thesis.entry_regime} regime and that regime "
                f"has broken; every model that formed the entry was fitted on a market that "
                f"is no longer the one this position is in",
                vector,
            )

        reversed_fraction, reversed_features = self._reversal(thesis, vector)
        if reversed_fraction is None:
            return self._opinion(
                position, STAND_DOWN, NO_ENTRY_RECORD, 0.0,
                "none of the entry features can be compared now, so the thesis can be neither "
                "confirmed nor refuted",
                vector,
            )

        if reversed_fraction >= self._close_at:
            return self._opinion(
                position, CLOSE_POSITION, FEATURES_REVERSED, reversed_fraction,
                f"{reversed_fraction:.0%} of the features that made this a long have reversed "
                f"({', '.join(reversed_features)}), past the {self._close_at:.0%} this bot "
                f"treats as the thesis having expired",
                vector,
            )

        if reversed_fraction >= self._reduce_at:
            return self._opinion(
                position, REDUCE_POSITION, FEATURES_REVERSED, reversed_fraction,
                f"{reversed_fraction:.0%} of the entry features have reversed "
                f"({', '.join(reversed_features)}); less true than it was, so smaller rather "
                f"than gone",
                vector,
            )

        return self._opinion(
            position, STAND_DOWN, STILL_VALID, reversed_fraction,
            f"{reversed_fraction:.0%} of the entry features have reversed, inside the "
            f"{self._reduce_at:.0%} that would call for reducing; the reason for being long "
            f"still holds and being down on the trade is not one of the ways it stops holding",
            vector,
        )

    def _reversal(self, thesis: HeldThesis, vector) -> tuple[float | None, list]:
        """How much of the entry's evidence now points the other way.

        Sign change rather than magnitude: a feature that was +2 and is now +0.5
        has weakened, and one that was +2 and is now -0.5 has reversed. Only the
        second means the reason has stopped being true.
        """
        comparable = 0
        reversed_features = []
        for name, entry_value in sorted(thesis.entry_features.items()):
            current = vector.features.get(name)
            if current is None:
                continue
            comparable += 1
            if entry_value == 0:
                continue
            if (entry_value > 0) != (current > 0):
                reversed_features.append(name)
        if comparable == 0:
            return None, []
        return len(reversed_features) / comparable, reversed_features

    def _opinion(
        self, position, action, reason_code, reversed_fraction, reason, vector=None
    ) -> DirectionalOpinion:
        self.standing.by_reason[reason_code] = self.standing.by_reason.get(reason_code, 0) + 1
        if action == CLOSE_POSITION:
            self.standing.close_calls += 1
        elif action == REDUCE_POSITION:
            self.standing.reduce_calls += 1
        else:
            self.standing.held += 1

        record = self._was_right.estimate(self._minimum)
        return DirectionalOpinion(
            bot=BOT,
            side=LONG,
            venue_id=position.venue_id,
            symbol=position.symbol,
            action=action,
            conviction=Estimate(
                value=reversed_fraction,
                is_fitted=record.is_fitted,
                observations=record.observations,
                prior=record.prior,
                was_clamped=False,
                bound_low=None,
                bound_high=None,
                reason=(
                    f"this watcher has been right {record.value:.0%} of the times it called a "
                    f"position invalid, over {record.observations} judged call(s)"
                ),
            ),
            timing=None,
            exit_plan=None,
            features_summary=self._summarise(position, vector),
            refusal=None if action != STAND_DOWN else reason_code,
            reason=reason,
            formed_at_ns=self._now_ns(),
        )

    def _summarise(self, position, vector) -> dict:
        """What was true at entry against what is true now, side by side."""
        thesis = self._theses.get((position.venue_id, position.symbol))
        return {
            "entry": dict(sorted(thesis.entry_features.items())) if thesis else {},
            "now": dict(sorted(vector.features.items())) if vector is not None else {},
        }

    @property
    def invalidation_record(self) -> Estimate:
        return self._was_right.estimate(self._minimum)


def describe_invalidation_watching(watcher: BullPositionInvalidationWatcher) -> dict:
    record = watcher.invalidation_record
    return {
        "part_id": PART_ID,
        "positions_watched": watcher.standing.positions_watched,
        "checks": watcher.standing.checks,
        "close_calls": watcher.standing.close_calls,
        "reduce_calls": watcher.standing.reduce_calls,
        "held": watcher.standing.held,
        "by_reason": dict(watcher.standing.by_reason),
        "was_right_when_it_called_a_position_invalid": record.value,
        "record_is_measured": record.is_fitted,
    }


def run_bull_position_invalidation_watcher(
    watcher: BullPositionInvalidationWatcher, control_socket, read_positions_and_features,
    publish_opinions, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_opinions(
            tuple(
                watcher.check(position, vector)
                for position, vector in read_positions_and_features(watcher)
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

    A thesis is recorded the first time a long is seen open, from the latest
    feature vector for its symbol at that moment -- the reasons the bot had
    when it entered -- and forgotten when the position goes flat. Each open
    long is then checked against the vector that is current now. A regime
    break from the detector marks every thesis entered in that regime.
    """
    from runtime.input_assembly import Batch, LatestByKey

    positions = Batch(read=context.bus.reader("position"))
    trades = Batch(read=context.bus.reader("market-data"))
    vectors = LatestByKey(read=context.bus.reader("bull-feature-vector"), key_of=lambda v: (v.venue_id, v.symbol))
    breaks = Batch(read=context.bus.reader("regime-break-alert"))
    publish_opinions = context.bus.publisher_for("directional-opinion")
    watcher = BullPositionInvalidationWatcher(
        reversal_fraction_to_reduce=context.number("bull_invalidation_reversal_to_reduce"),
        reversal_fraction_to_close=context.number("bull_invalidation_reversal_to_close"),
        prior_invalidation_hit_rate=context.number("bull_invalidation_prior_hit_rate"),
        prior_weight=context.number("bull_invalidation_prior_weight"),
        half_life_observations=context.number("bull_feature_half_life_observations"),
        minimum_observations=int(context.number("bull_invalidation_minimum_observations")),
    )
    held: dict[tuple[str, str], object] = {}
    regimes: dict[tuple[str, str], str] = {}
    thesis_horizon = (
        context.number("spread_reversion_horizon")
        * context.number("bull_exit_conviction_horizon_multiple")
    )

    def read_positions_and_features(_watcher):
        trades.payloads()
        for alert in breaks.payloads():
            watcher.observe_regime_break(alert.regime, alert.has_broken)
        current = vectors.mapping()
        for position in positions.payloads():
            key = (position.venue_id, position.symbol)
            if position.is_flat or position.quantity < 0:
                if key in held:
                    held.pop(key)
                    watcher.forget_position(*key)
                continue
            if key not in held:
                vector = current.get(key)
                if vector is None:
                    continue  # no reasons on record yet; the check says so
                regime = vector.sources.get("regime") or vector.features.get("regime") or "unclassified"
                regimes[key] = str(regime)
                watcher.record_entry(
                    HeldThesis(
                        venue_id=key[0], symbol=key[1],
                        entry_features=dict(vector.features), entry_regime=str(regime),
                        entry_price=position.average_entry_price,
                        # The longest the bot's own plan could have given the
                        # trade: the detector's horizon at the conviction multiple.
                        # The plan itself is not an input of this part.
                        horizon_seconds=thesis_horizon,
                        opened_at_ns=position.opened_at_ns,
                        detector=str(vector.sources.get("detector_strength", "")).removeprefix("detector:"),
                    )
                )
            held[key] = position
        return tuple(
            (position, current[key]) for key, position in held.items() if key in current
        )

    def publish(opinions) -> None:
        if opinions:
            publish_opinions(opinions)

    return run_bull_position_invalidation_watcher(
        watcher=watcher,
        control_socket=context.control_socket,
        read_positions_and_features=read_positions_and_features,
        publish_opinions=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
