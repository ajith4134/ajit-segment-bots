"""regret-tracker: what the system would have made by deciding differently.

A record of decisions taken says nothing about the decisions available. Without
regret, a bot whose good opinions were all overruled looks identical to a bot
with no good opinions, and the fix for each is opposite: one needs more weight,
the other needs less.

Regret here is **counterfactual, not hindsight**. The distinction matters:

- **Counterfactual regret** compares against alternatives that were actually
  available at the time -- the other bot's opinion, standing aside, the same
  trade at a different size. That is learnable from.
- **Hindsight regret** compares against the best possible trade in the period,
  which was never on offer. It is unbounded, always large, and teaches nothing
  except that the system is not omniscient.

**Regret is signed and both signs are kept.** Negative regret -- the alternative
would have been worse -- is what says a refusal was right, and a tracker that
only recorded missed profit would push the system toward taking everything.

**Per regime.** A bot with regret concentrated in one regime is a bot being
overruled in the wrong market, and the aggregate hides which.

**Costs are charged to the alternative.** Otherwise every untaken trade looks
better than the taken one, and the system learns to blame the desk.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate, QuantileEstimator
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "regret-tracker"

PART_DECLARATION = PartDeclaration(
    part_id="regret-tracker",
    consumes=("trade-intent", "counterfactual-outcome", "trade-episode", "market-regime"),
    produces=("bot-regret", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

MEASURED = "measured"
NOT_MEASURED = "too-few-counterfactuals-to-say"

OVERRULED = "its-opinion-was-overruled"
NOT_ACTED_ON = "its-opinion-was-not-acted-on"
SIZED_DOWN = "its-opinion-was-acted-on-smaller"


@dataclass(frozen=True)
class BotRegret:
    """What this bot's ignored opinions would have produced, in one regime."""

    bot: str
    regime: str
    state: str
    total_regret: float
    median_regret: float | None
    opinions_passed_over: int
    times_it_would_have_been_right: int
    times_it_would_have_been_worse: int
    by_cause: dict
    reason: str
    tracked_at_ns: int

    @property
    def is_measured(self) -> bool:
        return self.state == MEASURED

    @property
    def was_ignored_wrongly(self) -> bool:
        """Positive regret: it was right and was not acted on."""
        return self.total_regret > 0

    @property
    def refusals_were_right(self) -> bool:
        """Negative regret is what says a refusal was correct."""
        return self.total_regret < 0


@dataclass
class TrackerStanding:
    counterfactuals_recorded: int = 0
    positive_regret_recorded: int = 0
    negative_regret_recorded: int = 0
    bots_tracked: int = 0
    largest_single_regret: float | None = None
    by_cause: dict = field(default_factory=dict)


class RegretTracker:
    """Records what each bot's ignored opinions would have produced, against real alternatives."""

    def __init__(
        self,
        window: int,
        minimum_observations: int,
        cost_fraction: float,
        now_ns=time.time_ns,
    ) -> None:
        if cost_fraction < 0:
            raise ValueError("costs cannot be negative")
        self._window = window
        self._minimum = minimum_observations
        self._cost = cost_fraction
        self._now_ns = now_ns
        self._regret_estimators: dict[tuple[str, str], QuantileEstimator] = {}
        self._totals: dict[tuple[str, str], float] = {}
        self._right: dict[tuple[str, str], int] = {}
        self._worse: dict[tuple[str, str], int] = {}
        self._causes: dict[tuple[str, str], dict] = {}
        self.standing = TrackerStanding()

    def record(
        self, bot: str, regime: str, cause: str, what_was_done: float, what_it_wanted: float
    ) -> float:
        """One passed-over opinion, against what was actually done.

        Counterfactual rather than hindsight: compared against an alternative
        that was on offer, not against the best trade in the period, which was
        never available and would teach only that the system is not omniscient.
        """
        self.standing.counterfactuals_recorded += 1
        key = (bot, regime)

        # Costs charged to the alternative: otherwise every untaken trade looks
        # better and the system learns to blame the desk.
        regret = (what_it_wanted - self._cost) - what_was_done

        self._regret_for(key).observe(regret)
        self._totals[key] = self._totals.get(key, 0.0) + regret
        self._causes.setdefault(key, {})
        self._causes[key][cause] = self._causes[key].get(cause, 0) + 1
        self.standing.by_cause[cause] = self.standing.by_cause.get(cause, 0) + 1

        if regret > 0:
            self._right[key] = self._right.get(key, 0) + 1
            self.standing.positive_regret_recorded += 1
        else:
            # Both signs kept: negative regret is what says a refusal was right,
            # and a tracker that recorded only missed profit would push the
            # system toward taking everything.
            self._worse[key] = self._worse.get(key, 0) + 1
            self.standing.negative_regret_recorded += 1

        if (
            self.standing.largest_single_regret is None
            or abs(regret) > abs(self.standing.largest_single_regret)
        ):
            self.standing.largest_single_regret = regret

        self.standing.bots_tracked = len({name for name, _ in self._regret_estimators})
        return regret

    def regret_for(self, bot: str, regime: str) -> BotRegret:
        key = (bot, regime)
        estimator = self._regret_for(key)
        median = estimator.estimate(0.5, self._minimum)
        passed_over = estimator.observations
        total = self._totals.get(key, 0.0)
        right = self._right.get(key, 0)
        worse = self._worse.get(key, 0)
        causes = dict(self._causes.get(key, {}))

        if not median.is_fitted:
            return self._regret(
                bot, regime, NOT_MEASURED, total, None, passed_over, right, worse, causes,
                f"{passed_over} passed-over opinion(s) of the {self._minimum} needed in "
                f"{regime}",
            )

        return self._regret(
            bot, regime, MEASURED, total, median.value, passed_over, right, worse, causes,
            f"{bot} in {regime}: {total:+.4f} total regret over {passed_over} passed-over "
            f"opinion(s), median {median.value:+.4f}. It would have been right {right} time(s) "
            f"and worse {worse} time(s)"
            + (
                f"; mostly because {max(causes, key=causes.get)}"
                if causes
                else ""
            )
            + (
                ". Positive regret says it was ignored wrongly and needs more weight"
                if total > 0
                else ". Negative regret says the refusals were right, which is the half a "
                "missed-profit-only tracker would never record"
            ),
        )

    def all_regret(self) -> tuple:
        return tuple(self.regret_for(bot, regime) for bot, regime in sorted(self._regret_estimators))

    def _regret_for(self, key) -> QuantileEstimator:
        estimator = self._regret_estimators.get(key)
        if estimator is None:
            estimator = QuantileEstimator(window=self._window, prior=0.0)
            self._regret_estimators[key] = estimator
        return estimator

    def _regret(
        self, bot, regime, state, total, median, passed_over, right, worse, causes, reason
    ) -> BotRegret:
        return BotRegret(
            bot=bot,
            regime=regime,
            state=state,
            total_regret=total,
            median_regret=median,
            opinions_passed_over=passed_over,
            times_it_would_have_been_right=right,
            times_it_would_have_been_worse=worse,
            by_cause=causes,
            reason=reason,
            tracked_at_ns=self._now_ns(),
        )


def describe_regret(tracker: RegretTracker) -> dict:
    return {
        "part_id": PART_ID,
        "counterfactuals_recorded": tracker.standing.counterfactuals_recorded,
        "positive_regret_recorded": tracker.standing.positive_regret_recorded,
        "negative_regret_recorded": tracker.standing.negative_regret_recorded,
        "bots_tracked": tracker.standing.bots_tracked,
        "largest_single_regret": tracker.standing.largest_single_regret,
        "by_cause": dict(sorted(tracker.standing.by_cause.items())),
        "measures_hindsight_regret": False,
    }


def run_regret_tracker(
    tracker: RegretTracker, control_socket, read_counterfactuals, publish_regret,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_counterfactuals(tracker)
        publish_regret(tracker.all_regret())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_regret(tracker),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    An intent that stood aside with dissenting bots, or acted on fewer than
    had a view, is a passed-over opinion per bot; the counterfactual for that
    symbol says what acting on it would have made, and the episode says what
    was done. Regret per bot per regime goes out once per health interval.
    """
    import time as _time

    from runtime.input_assembly import Batch, LatestByKey

    intents = Batch(read=context.bus.reader("trade-intent"))
    counterfactuals = Batch(read=context.bus.reader("counterfactual-outcome"))
    episodes = Batch(read=context.bus.reader("trade-episode"))
    regimes = LatestByKey(read=context.bus.reader("market-regime"), key_of=lambda r: (r.venue_id, r.symbol))
    publish_regret = context.bus.publisher_for("bot-regret")
    tracker = RegretTracker(
        window=int(context.number("learning_window")),
        minimum_observations=int(context.number("learning_minimum_observations")),
        cost_fraction=context.number("regret_cost_fraction"),
    )
    passed_over: dict[tuple[str, str], list] = {}
    done: dict[tuple[str, str], float] = {}
    last_publish = [float("-inf")]

    def read_counterfactuals(_tracker) -> None:
        regime_by_symbol = regimes.mapping()
        for intent in intents.payloads():
            key = (intent.venue_id, intent.symbol)
            cause = NOT_ACTED_ON if not intent.is_actionable else OVERRULED
            for bot in intent.dissenting_bots:
                passed_over.setdefault(key, []).append((bot, cause))
        for episode in episodes.payloads():
            done[(episode.venue_id, episode.symbol)] = episode.realised
        for outcome in counterfactuals.payloads():
            key = (outcome.venue_id, outcome.symbol)
            if outcome.realised_fraction is None:
                continue
            regime = regime_by_symbol.get(key)
            regime_name = regime.regime if regime is not None else "unclassified"
            for bot, cause in passed_over.pop(key, []):
                tracker.record(bot, regime_name, cause, done.get(key, 0.0), outcome.realised_fraction)

    def tick() -> None:
        read_counterfactuals(tracker)
        now = _time.monotonic()
        if now - last_publish[0] < context.health_interval_seconds:
            return
        regret = tracker.all_regret()
        if regret:
            publish_regret(regret)
        last_publish[0] = now

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
    )
