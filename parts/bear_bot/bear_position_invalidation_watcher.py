"""bear-position-invalidation-watcher: when a short stops being justified, and it is urgent.

The bull's watcher checks a held long against the reasons it was opened for. This
does the same for shorts and adds the two things that only matter on this side:

- **A squeeze is not a slow invalidation.** The bull watcher can afford to reduce
  on partial reversal and reconsider next tick. A short whose offer side has been
  eaten while volatility rises is in the state that ends positions, and this
  watcher closes on that shape directly rather than waiting for enough individual
  features to flip. By the time a majority of them have, the exit is expensive.
- **Carry is a clock.** A short that has been open long enough for funding to
  have eaten the move it was expecting has been invalidated by cost rather than
  by price. That is invisible to any feature comparison, so it is measured
  explicitly: what has been paid so far against what the thesis was worth.

It produces `directional-opinion`, the same type as the composer, so the arbiter
and the risk gate stay in the path (T-2). A part that could close a position
directly would be a second execution route with none of the first one's checks.

**Reduce is still a real answer**, but the thresholds are tighter than the bull's,
and the squeeze shape and the carry clock bypass them entirely.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.bot_opinion import (
    CLOSE_POSITION, REDUCE_POSITION, SHORT, STAND_DOWN, DirectionalOpinion,
)
from runtime.learned_estimator import Estimate, RateEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "bear-position-invalidation-watcher"
BOT = "bear-bot"

PART_DECLARATION = PartDeclaration(
    part_id="bear-position-invalidation-watcher",
    consumes=("position", "market-data", "bear-feature-vector", "regime-break-alert"),
    produces=("directional-opinion", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

STILL_VALID = "thesis-still-holds"
FEATURES_REVERSED = "entry-features-have-reversed"
SQUEEZE_FORMING = "offer-side-eaten-while-volatility-rises"
CARRY_ATE_THE_THESIS = "funding-paid-has-eaten-what-the-thesis-was-worth"
REGIME_BROKEN = "regime-this-was-entered-into-has-broken"
HORIZON_EXPIRED = "past-the-horizon-it-was-given"
NO_ENTRY_RECORD = "no-entry-features-recorded-for-this-position"


@dataclass
class HeldThesis:
    """What was true when this short was opened, and what it was expected to be worth."""

    venue_id: str
    symbol: str
    entry_features: dict
    entry_regime: str
    entry_price: float
    expected_move_fraction: float
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
    squeezes_caught: int = 0
    carry_closes: int = 0
    by_reason: dict = field(default_factory=dict)


class BearPositionInvalidationWatcher:
    """Checks open shorts against their reasons, their carry, and the squeeze shape."""

    def __init__(
        self,
        reversal_fraction_to_reduce: float,
        reversal_fraction_to_close: float,
        squeeze_room_collapse_fraction: float,
        volatility_rise_fraction: float,
        carry_fraction_of_expected_move: float,
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
        if not 0.0 < carry_fraction_of_expected_move <= 1.0:
            raise ValueError(
                "the carry clock is the fraction of the expected move that funding may eat "
                "before the thesis has been paid away; it must be inside (0, 1]"
            )
        self._reduce_at = reversal_fraction_to_reduce
        self._close_at = reversal_fraction_to_close
        self._room_collapse = squeeze_room_collapse_fraction
        self._volatility_rise = volatility_rise_fraction
        self._carry_limit = carry_fraction_of_expected_move
        self._minimum = minimum_observations
        self._now_ns = now_ns
        self._theses: dict[tuple[str, str], HeldThesis] = {}
        self._carry_paid: dict[tuple[str, str], float] = {}
        self._broken_regimes: set[str] = set()
        self._was_right = RateEstimator(
            prior=prior_invalidation_hit_rate, prior_weight=prior_weight,
            half_life_observations=half_life_observations,
        )
        self.standing = WatcherStanding()

    def record_entry(self, thesis: HeldThesis) -> None:
        key = (thesis.venue_id, thesis.symbol)
        self._theses[key] = thesis
        self._carry_paid.setdefault(key, 0.0)
        self.standing.positions_watched = len(self._theses)

    def observe_funding_settlement(self, venue_id: str, symbol: str, paid_fraction: float) -> None:
        """One settlement's carry, as a fraction of notional. Positive means paid out."""
        key = (venue_id, symbol)
        if key in self._theses:
            self._carry_paid[key] = self._carry_paid.get(key, 0.0) + paid_fraction

    def forget_position(self, venue_id: str, symbol: str) -> None:
        """A closed short releases its thesis and its carry. T-3."""
        key = (venue_id, symbol)
        self._theses.pop(key, None)
        self._carry_paid.pop(key, None)
        self.standing.positions_watched = len(self._theses)

    def observe_regime_break(self, regime: str, has_broken: bool) -> None:
        if has_broken:
            self._broken_regimes.add(regime)
        else:
            self._broken_regimes.discard(regime)

    def observe_outcome(self, was_right: bool) -> None:
        self._was_right.observe(was_right)

    def check(self, position, vector) -> DirectionalOpinion:
        self.standing.checks += 1
        key = (position.venue_id, position.symbol)
        thesis = self._theses.get(key)

        if thesis is None:
            return self._opinion(
                position, STAND_DOWN, NO_ENTRY_RECORD, 0.0,
                "no entry features were recorded for this short, so there is nothing to compare "
                "against; this watcher will not invent a reason to close a trade it cannot judge",
                vector,
            )

        squeeze = self._squeeze_shape(thesis, vector)
        if squeeze is not None:
            self.standing.squeezes_caught += 1
            return self._opinion(position, CLOSE_POSITION, SQUEEZE_FORMING, 1.0, squeeze, vector)

        carry = self._carry_paid.get(key, 0.0)
        if thesis.expected_move_fraction > 0 and carry >= (
            self._carry_limit * thesis.expected_move_fraction
        ):
            self.standing.carry_closes += 1
            return self._opinion(
                position, CLOSE_POSITION, CARRY_ATE_THE_THESIS, 1.0,
                f"funding has cost {carry:.3%} against an expected move of "
                f"{thesis.expected_move_fraction:.3%}; the thesis has been paid away rather "
                f"than proved wrong, which no feature comparison can see",
                vector,
            )

        age_seconds = (self._now_ns() - thesis.opened_at_ns) / 1e9
        if age_seconds > thesis.horizon_seconds:
            return self._opinion(
                position, CLOSE_POSITION, HORIZON_EXPIRED, 1.0,
                f"open for {age_seconds:.0f}s against the {thesis.horizon_seconds:.0f}s this "
                f"short was given; past its horizon it is an exposure nobody decided to hold, "
                f"still paying carry",
                vector,
            )

        if thesis.entry_regime in self._broken_regimes:
            return self._opinion(
                position, CLOSE_POSITION, REGIME_BROKEN, 1.0,
                f"this short was entered in the {thesis.entry_regime} regime and that regime has "
                f"broken; every model that formed the entry was fitted on a market that is no "
                f"longer the one this position is in",
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
                f"{reversed_fraction:.0%} of the features that made this a short have reversed "
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
            f"{self._reduce_at:.0%} that would call for reducing; carry so far is {carry:.3%} of "
            f"an expected {thesis.expected_move_fraction:.3%}",
            vector,
        )

    def _squeeze_shape(self, thesis: HeldThesis, vector) -> str | None:
        """Offer side eaten while volatility rises -- the state that ends short positions.

        Checked against this position's own entry rather than against a learned
        normal, because what matters is that the book this short was sized
        against is no longer there.
        """
        entry_room = thesis.entry_features.get("squeeze_room")
        entry_volatility = thesis.entry_features.get("realised_volatility_fraction")
        room = vector.features.get("squeeze_room")
        volatility = vector.features.get("realised_volatility_fraction")
        if None in (entry_room, entry_volatility, room, volatility):
            return None
        if entry_room <= 0 or entry_volatility <= 0:
            return None
        room_left = room / entry_room
        volatility_now = volatility / entry_volatility
        if room_left <= self._room_collapse and volatility_now >= self._volatility_rise:
            return (
                f"the offer side this short was sized against is {room_left:.0%} of what it was "
                f"while volatility is {volatility_now:.1f}x entry. That is the shape a squeeze "
                f"takes, and waiting for a majority of features to flip would mean exiting "
                f"after it"
            )
        return None

    def _reversal(self, thesis: HeldThesis, vector) -> tuple[float | None, list]:
        """How much of the entry's evidence now points the other way, by sign change."""
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
        self, position, action, reason_code, reversed_fraction, reason, vector
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
            side=SHORT,
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
                    f"short invalid, over {record.observations} judged call(s)"
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
        key = (position.venue_id, position.symbol)
        thesis = self._theses.get(key)
        return {
            "entry": dict(sorted(thesis.entry_features.items())) if thesis else {},
            "now": dict(sorted(vector.features.items())) if vector is not None else {},
            "carry_paid_fraction": self._carry_paid.get(key, 0.0),
        }

    @property
    def invalidation_record(self) -> Estimate:
        return self._was_right.estimate(self._minimum)


def describe_invalidation_watching(watcher: BearPositionInvalidationWatcher) -> dict:
    record = watcher.invalidation_record
    return {
        "part_id": PART_ID,
        "positions_watched": watcher.standing.positions_watched,
        "checks": watcher.standing.checks,
        "close_calls": watcher.standing.close_calls,
        "reduce_calls": watcher.standing.reduce_calls,
        "held": watcher.standing.held,
        "squeezes_caught": watcher.standing.squeezes_caught,
        "closed_because_carry_ate_the_thesis": watcher.standing.carry_closes,
        "by_reason": dict(watcher.standing.by_reason),
        "was_right_when_it_called_a_short_invalid": record.value,
        "record_is_measured": record.is_fitted,
    }


def run_bear_position_invalidation_watcher(
    watcher: BearPositionInvalidationWatcher, control_socket, read_positions_and_features,
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
