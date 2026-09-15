"""loss-cause-classifier: the one thing that most explains a loss.

Every loss has a story available for it, and most of those stories are wrong in a way
that makes things worse when acted on. Widening a stop that was correctly placed
turns small losses into large ones. Tightening entry criteria because the regime
turned removes setups that were fine. So this part commits to a single cause, from a
closed set, on evidence -- and keeps the runners-up visible so a wrong call is
correctable rather than invisible.

The evidence for each cause is a different measurement, which is why this part
consumes so many:

- **The stop was inside the noise** -- from the stop audit, and confirmed by whether
  the price recovered afterwards.
- **The regime turned** -- from the transition flag, weighted by how much of the
  trade happened in the new regime.
- **Costs ate it** -- from the attribution: the direction term was positive and the
  costs exceeded it. This is the loss that looks like a bad call and is actually a
  bad instrument choice.
- **The exit was late** -- from the excursion: the position was meaningfully in
  profit and gave it all back.
- **The entry was early or late** -- from the excursion's shape: an early entry sits
  through an adverse move first, a late one enters after the move is largely spent.
- **The setup was wrong** -- what remains when the trade simply went the other way
  from the start.

**"It was just variance" must be reachable**, and it is the most important verdict
here: a classifier that always finds a fault produces a system that changes something
after every loss, which is how a working strategy is tuned to death. When the outcome
was not distinguishable from noise, that is the answer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import RateEstimator
from runtime.trade_decoding_types import (
    COSTS_ATE_IT, IT_WAS_JUST_VARIANCE, LOSS_CAUSES, LossCause, THE_ENTRY_WAS_EARLY,
    THE_ENTRY_WAS_LATE, THE_EXIT_WAS_LATE, THE_REGIME_TURNED, THE_SETUP_WAS_WRONG,
    THE_STOP_WAS_INSIDE_THE_NOISE, THE_STOP_WAS_TOO_WIDE,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "loss-cause-classifier"

PART_DECLARATION = PartDeclaration(
    part_id="loss-cause-classifier",
    consumes=(
        "trade-episode", "peak-excursion", "pnl-attribution", "regime-transition-flag",
        "stop-audit", "shortfall-breakdown",
    ),
    produces=("loss-cause", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

CLASSIFIED = "classified"
NOT_A_LOSS = "this-trade-did-not-lose"
NO_EVIDENCE = "nothing-measured-supports-any-cause"


@dataclass(frozen=True)
class Classification:
    trade_id: str
    state: str
    cause: LossCause | None
    scores: dict
    reason: str
    classified_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == CLASSIFIED and self.cause is not None


@dataclass
class ClassifierStanding:
    losses_examined: int = 0
    classified: int = 0
    not_losses: int = 0
    without_evidence: int = 0
    by_cause: dict = field(default_factory=dict)
    variance_verdicts: int = 0
    avoidable_losses: int = 0


class LossCauseClassifier:
    """Commits to one cause on evidence, keeps the runners-up, and can say variance."""

    def __init__(
        self,
        gave_back_threshold: float,
        regime_share_threshold: float,
        prior_correctness: float,
        prior_weight: float,
        half_life_observations: float,
        minimum_observations: int,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 < gave_back_threshold < 1.0:
            raise ValueError(
                "the give-back threshold is the fraction of a favourable move handed back"
            )
        if not 0.0 < regime_share_threshold <= 1.0:
            raise ValueError(
                "the regime share is how much of the trade must sit in the new regime "
                "before the change is the explanation rather than a coincidence"
            )
        self._gave_back_threshold = gave_back_threshold
        self._regime_share_threshold = regime_share_threshold
        self._minimum_observations = minimum_observations
        self._now_ns = now_ns
        # RL-060: a part that judges carries a learned component. Here it is how
        # often each cause, once acted on, actually improved later trades.
        self._cause_correctness = {
            cause: RateEstimator(
                prior=prior_correctness,
                prior_weight=prior_weight,
                half_life_observations=half_life_observations,
            )
            for cause in LOSS_CAUSES
        }
        self.standing = ClassifierStanding()

    def observe_cause_outcome(self, cause: str, acting_on_it_helped: bool) -> None:
        """Whether acting on this diagnosis actually improved anything afterwards."""
        if cause not in self._cause_correctness:
            raise ValueError(f"{cause!r} is not one of the causes this part can name")
        self._cause_correctness[cause].observe(acting_on_it_helped)

    def confidence_in(self, cause: str) -> tuple:
        estimate = self._cause_correctness[cause].estimate(self._minimum_observations)
        return estimate.value, estimate.is_fitted

    def classify(
        self, trade_id: str, closed_trade, stop_audit=None, regime_flag=None,
        attribution=None, exit_quality=None, entry_quality=None, significance=None,
    ) -> Classification:
        if closed_trade.realised_pnl >= 0:
            self.standing.not_losses += 1
            return self._classification(
                trade_id, NOT_A_LOSS, None, {},
                "this trade did not lose, so there is no loss to explain",
            )

        self.standing.losses_examined += 1
        scores: dict = {}
        evidence: dict = {}

        # Variance first: if the outcome is indistinguishable from the symbol
        # moving, every other explanation is a story fitted to noise.
        if significance is not None and significance.is_measurable and not significance.is_significant:
            scores[IT_WAS_JUST_VARIANCE] = 1.0
            evidence["standardised"] = significance.standardised

        if stop_audit is not None and stop_audit.is_measurable:
            if stop_audit.verdict.startswith("inside"):
                scores[THE_STOP_WAS_INSIDE_THE_NOISE] = (
                    0.9 if stop_audit.would_have_recovered else 0.7
                )
                evidence["stop_in_typical_movements"] = (
                    stop_audit.distance_in_typical_movements
                )
            elif stop_audit.verdict.startswith("wide"):
                scores[THE_STOP_WAS_TOO_WIDE] = 0.6
                evidence["stop_in_typical_movements"] = (
                    stop_audit.distance_in_typical_movements
                )

        if regime_flag is not None and regime_flag.changed:
            share = regime_flag.fraction_of_the_trade_in_the_new_regime or 0.0
            if share >= self._regime_share_threshold:
                scores[THE_REGIME_TURNED] = 0.5 + 0.4 * share
                evidence["fraction_in_the_new_regime"] = share

        if attribution is not None:
            direction = attribution.components.get("direction", 0.0)
            if direction > 0 and attribution.cost_share >= 1.0:
                # The move was real; the instrument was too expensive to trade.
                scores[COSTS_ATE_IT] = 0.85
                evidence["cost_share"] = attribution.cost_share

        if exit_quality is not None and exit_quality.captured_fraction is not None:
            if (
                exit_quality.captured_fraction < 1.0 - self._gave_back_threshold
                and exit_quality.gave_back
                and exit_quality.gave_back > 0
            ):
                scores[THE_EXIT_WAS_LATE] = 0.6 + 0.3 * (
                    1.0 - exit_quality.captured_fraction
                )
                evidence["captured_fraction"] = exit_quality.captured_fraction

        if entry_quality is not None and entry_quality.is_measurable:
            if entry_quality.was_chasing:
                scores[THE_ENTRY_WAS_LATE] = 0.7
                evidence["was_chasing"] = True
            elif entry_quality.percentile is not None and entry_quality.percentile < 0.2:
                scores[THE_ENTRY_WAS_EARLY] = 0.5
                evidence["entry_percentile"] = entry_quality.percentile

        if not scores:
            # Nothing else explains it: the trade went the wrong way from the start.
            if closed_trade.best_unrealised is not None and closed_trade.best_unrealised <= 0:
                scores[THE_SETUP_WAS_WRONG] = 0.5
                evidence["never_went_favourable"] = True
            else:
                self.standing.without_evidence += 1
                return self._classification(
                    trade_id, NO_EVIDENCE, None, {},
                    "nothing measured supports any cause. That is reported rather than "
                    "guessed: a classifier that always finds a fault produces a system "
                    "that changes something after every loss",
                )

        cause = max(scores, key=lambda name: scores[name])
        confidence, is_fitted = self.confidence_in(cause)
        runners_up = tuple(
            sorted(
                ((name, score) for name, score in scores.items() if name != cause),
                key=lambda pair: -pair[1],
            )
        )

        if cause == IT_WAS_JUST_VARIANCE:
            self.standing.variance_verdicts += 1
        self.standing.by_cause[cause] = self.standing.by_cause.get(cause, 0) + 1
        self.standing.classified += 1

        was_avoidable = cause != IT_WAS_JUST_VARIANCE
        if was_avoidable:
            self.standing.avoidable_losses += 1

        return self._classification(
            trade_id, CLASSIFIED,
            LossCause(
                trade_id=trade_id,
                cause=cause,
                confidence=confidence,
                is_fitted=is_fitted,
                runners_up=runners_up,
                evidence=evidence,
                was_avoidable=was_avoidable,
                reason=(
                    f"{cause}, scored {scores[cause]:.2f} on the measured evidence"
                    + (
                        f", ahead of {runners_up[0][0]} at {runners_up[0][1]:.2f}"
                        if runners_up
                        else ""
                    )
                    + (
                        f". Acting on this diagnosis has helped {confidence:.0%} of the "
                        f"time here"
                        if is_fitted
                        else ". No history yet of whether acting on this diagnosis helps"
                    )
                    + (
                        ". Nothing should be changed for it: the outcome was not "
                        "distinguishable from the symbol moving, and tuning after every "
                        "loss is how a working strategy is tuned to death"
                        if cause == IT_WAS_JUST_VARIANCE
                        else ""
                    )
                ),
                classified_at_ns=self._now_ns(),
            ),
            scores,
            f"{cause} with {len(runners_up)} runner(s)-up kept",
        )

    def _classification(self, trade_id, state, cause, scores, reason) -> Classification:
        return Classification(
            trade_id=trade_id, state=state, cause=cause, scores=dict(scores),
            reason=reason, classified_at_ns=self._now_ns(),
        )


def describe_loss_classification(classifier: LossCauseClassifier) -> dict:
    return {
        "part_id": PART_ID,
        "losses_examined": classifier.standing.losses_examined,
        "classified": classifier.standing.classified,
        "not_losses": classifier.standing.not_losses,
        "without_evidence": classifier.standing.without_evidence,
        "by_cause": dict(classifier.standing.by_cause),
        "variance_verdicts": classifier.standing.variance_verdicts,
        "avoidable_losses": classifier.standing.avoidable_losses,
        "causes": list(LOSS_CAUSES),
        "always_finds_a_fault": False,
        "discards_the_runners_up": False,
    }


def run_loss_cause_classifier(
    classifier: LossCauseClassifier, control_socket, read_losses, publish_causes,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for job in read_losses():
            classification = classifier.classify(**job)
            if classification.is_usable:
                publish_causes(classification.cause)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_loss_classification(classifier),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Every analysis of a trade arrives on its own type and names the trade;
    each is held until the episode that closes the trade arrives, and the
    loss is classified with whatever had landed. What had not is None, which
    the classifier reads as unmeasured rather than as absent evidence.
    """
    from runtime.input_assembly import Batch, LatestByKey
    from runtime.trading_types import ClosedTrade

    episodes = Batch(read=context.bus.reader("trade-episode"))
    excursions = LatestByKey(read=context.bus.reader("peak-excursion"), key_of=lambda e: (e.venue_id, e.symbol))
    # Keyed by trade_id, so the key space is every trade ever closed, and none of
    # these producers restates -- each publishes once per closed trade. Bounded
    # 2026-09-04 by how long a join over one closed trade may wait for the rest of
    # its facts; since that date an expired key is dropped, not merely hidden.
    join_age = context.number("closed_trade_join_maximum_age_seconds")
    attributions = LatestByKey(read=context.bus.reader("pnl-attribution"), key_of=lambda a: a.trade_id, maximum_age_seconds=join_age)
    flags = LatestByKey(read=context.bus.reader("regime-transition-flag"), key_of=lambda f: f.trade_id, maximum_age_seconds=join_age)
    audits = LatestByKey(read=context.bus.reader("stop-audit"), key_of=lambda a: a.trade_id, maximum_age_seconds=join_age)
    breakdowns = LatestByKey(read=context.bus.reader("shortfall-breakdown"), key_of=lambda b: b.trade_id, maximum_age_seconds=join_age)
    publish_causes = context.bus.publisher_for("loss-cause")
    classifier = LossCauseClassifier(
        gave_back_threshold=context.number("exit_quality_gave_back_threshold"),
        regime_share_threshold=context.number("loss_cause_regime_share_threshold"),
        prior_correctness=context.number("learning_prior_hit_rate"),
        prior_weight=context.number("learning_prior_weight"),
        half_life_observations=context.number("learning_half_life_observations"),
        minimum_observations=int(context.number("learning_minimum_observations")),
    )

    def read_losses():
        excursions.mapping()
        breakdowns.mapping()
        jobs = []
        for episode in episodes.payloads():
            if episode.realised >= 0:
                continue
            trade_id = episode.trade_id
            conditions = episode.conditions if isinstance(episode.conditions, dict) else {}
            closed_trade = ClosedTrade(
                venue_id=episode.venue_id, symbol=episode.symbol, direction=str(episode.action),
                quantity=float(conditions.get("quantity", 0.0) or 0.0),
                entry_price=float(conditions.get("entry_price", 0.0) or 0.0),
                exit_price=float(conditions.get("exit_price", 0.0) or 0.0),
                realised_pnl=episode.realised, fees_paid=float(conditions.get("fees_paid", 0.0) or 0.0),
                opened_at_ns=episode.opened_at_ns, closed_at_ns=episode.closed_at_ns,
            )
            jobs.append({
                "trade_id": trade_id, "closed_trade": closed_trade,
                "stop_audit": audits.mapping().get(trade_id) or conditions.get("stop_audit"),
                "regime_flag": flags.mapping().get(trade_id) or conditions.get("regime_flag"),
                "attribution": attributions.mapping().get(trade_id),
                "exit_quality": conditions.get("exit_quality"),
                "entry_quality": conditions.get("entry_quality"),
                "significance": conditions.get("significance"),
            })
        return tuple(jobs)

    return run_loss_cause_classifier(
        classifier=classifier,
        control_socket=context.control_socket,
        read_losses=read_losses,
        publish_causes=lambda cause: publish_causes((cause,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
