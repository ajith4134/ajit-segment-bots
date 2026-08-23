"""llm-model-picker: which model, chosen from what each has actually done here.

Model choice is usually made once, by a person, from a benchmark that measures
something else. This part makes it a measured, per-purpose decision, because the
ranking is not global: a model that writes better prose can be worse at returning a
strict structure, and the purpose decides which of those matters.

So quality is tracked per (model, purpose) pair from outcomes this system observed:
did the answer pass the enforcer, and did it agree with what happened. Three
consequences that a single global ranking cannot express:

- **The cheapest model that clears the purpose's bar wins.** Not the best one. A
  purpose with a low bar answered by an expensive model is money spent on headroom
  nobody uses.
- **An unmeasured model is tried deliberately rather than assumed good or bad.** A
  small share of calls goes to unmeasured models, because a model never tried is a
  model whose quality is a guess forever -- and that exploration is a named setting
  with its own share, not a random accident.
- **A model that got worse loses its place.** Providers change models under their
  own names, so quality is estimated with decay and a model's history stops
  protecting it.

The picker states a choice and its evidence. It does not call anything, and it does
not know what a call costs to make -- the router decides which pocket pays, and
keeping those separate means a model can be preferred without implying who funds it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import RateEstimator
from runtime.llm_types import LlmModelChoice, LOCAL, METERED, SUBSCRIPTION
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "llm-model-picker"

PART_DECLARATION = PartDeclaration(
    part_id="llm-model-picker",
    consumes=("llm-request", "llm-call-record"),
    produces=("llm-model-choice", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

CHOSEN = "chosen"
EXPLORING = "chosen-to-measure-a-model-nothing-is-known-about"
NO_MODEL_CLEARS_THE_BAR = "no-model-has-shown-it-can-do-this-purpose-well-enough"
NO_MODELS = "no-model-is-declared"


@dataclass(frozen=True)
class ModelChoice:
    request_id: str
    state: str
    choice: LlmModelChoice | None
    considered: tuple
    reason: str
    chosen_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state in (CHOSEN, EXPLORING) and self.choice is not None


@dataclass
class PickerStanding:
    choices_made: int = 0
    exploratory_choices: int = 0
    refused_no_model_clears_the_bar: int = 0
    outcomes_observed: int = 0
    models_declared: int = 0
    times_the_cheapest_qualifying_model_won: int = 0
    times_a_model_lost_its_place: int = 0


class LlmModelPicker:
    """Picks the cheapest model whose measured quality clears the purpose's bar."""

    def __init__(
        self,
        exploration_share: float,
        minimum_observations: int,
        prior_quality: float,
        prior_weight: float,
        half_life_observations: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 <= exploration_share < 1.0:
            raise ValueError(
                "the exploration share is the fraction of calls spent measuring unmeasured "
                "models, and it is a named setting rather than a random accident"
            )
        if minimum_observations < 1:
            raise ValueError("a quality fitted on zero calls is the prior")
        self._exploration_share = exploration_share
        self._minimum_observations = minimum_observations
        self._prior_quality = prior_quality
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._now_ns = now_ns
        self._models: dict[str, dict] = {}
        self._quality: dict[tuple, RateEstimator] = {}
        self._latency: dict[str, float] = {}
        self._best_of: dict[str, str] = {}
        self._call_count = 0
        self.standing = PickerStanding()

    def declare_model(
        self, model_id: str, payment_kind: str, cost_per_call: float,
        typical_latency_seconds: float,
    ) -> None:
        if payment_kind not in (SUBSCRIPTION, METERED, LOCAL):
            raise ValueError(f"{payment_kind!r} is not a pocket")
        if model_id not in self._models:
            self.standing.models_declared += 1
        self._models[model_id] = {
            "payment_kind": payment_kind,
            "cost_per_call": cost_per_call,
            "latency": typical_latency_seconds,
        }

    def observe_outcome(self, model_id: str, purpose: str, was_good: bool) -> None:
        """What this model actually did on this purpose, in this system."""
        self.standing.outcomes_observed += 1
        self._quality.setdefault(
            (model_id, purpose),
            RateEstimator(
                prior=self._prior_quality,
                prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            ),
        ).observe(was_good)

    def quality_of(self, model_id: str, purpose: str) -> tuple:
        estimator = self._quality.get((model_id, purpose))
        if estimator is None:
            return self._prior_quality, False, 0
        estimate = estimator.estimate(self._minimum_observations)
        return estimate.value, estimate.is_fitted, estimator.observations

    def pick(self, request_id: str, purpose: str, quality_bar: float) -> ModelChoice:
        if not self._models:
            return self._choice(
                request_id, NO_MODELS, None, (),
                "no model is declared. Picking from an empty set would mean falling back "
                "to whatever was hardcoded",
            )

        self._call_count += 1
        considered = []
        qualifying = []
        unmeasured = []

        for model_id, facts in self._models.items():
            quality, is_fitted, observations = self.quality_of(model_id, purpose)
            considered.append((model_id, quality, is_fitted, observations))
            if not is_fitted:
                unmeasured.append((model_id, facts))
            elif quality >= quality_bar:
                qualifying.append((model_id, facts, quality))

        # Exploration is deliberate and bounded: a model never tried is a model whose
        # quality stays a guess forever.
        should_explore = (
            unmeasured
            and self._exploration_share > 0
            and (self._call_count % max(int(1 / self._exploration_share), 1) == 0)
        )
        if should_explore:
            model_id, facts = unmeasured[self._call_count % len(unmeasured)]
            self.standing.exploratory_choices += 1
            self.standing.choices_made += 1
            return self._choice(
                request_id, EXPLORING,
                self._as_choice(request_id, purpose, model_id, facts, self._prior_quality, False,
                                "measuring a model nothing is known about yet"),
                tuple(considered),
                f"{model_id} chosen to measure it. This is {self._exploration_share:.0%} of "
                f"calls by setting, not an accident",
            )

        if not qualifying:
            self.standing.refused_no_model_clears_the_bar += 1
            return self._choice(
                request_id, NO_MODEL_CLEARS_THE_BAR, None, tuple(considered),
                f"no model has shown it can do {purpose} at {quality_bar:.0%}. Answering "
                f"with the least-bad one would be a downgrade nobody chose",
            )

        # The cheapest that clears the bar, not the best: headroom nobody uses is
        # money spent for nothing.
        model_id, facts, quality = min(
            qualifying, key=lambda entry: (entry[1]["cost_per_call"], -entry[2])
        )
        best_quality = max(entry[2] for entry in qualifying)
        if quality < best_quality:
            self.standing.times_the_cheapest_qualifying_model_won += 1

        previous_best = self._best_of.get(purpose)
        if previous_best is not None and previous_best != model_id:
            self.standing.times_a_model_lost_its_place += 1
        self._best_of[purpose] = model_id

        self.standing.choices_made += 1
        return self._choice(
            request_id, CHOSEN,
            self._as_choice(
                request_id, purpose, model_id, facts, quality, True,
                f"cheapest model clearing the {quality_bar:.0%} bar for {purpose}",
            ),
            tuple(considered),
            f"{model_id} at {quality:.0%} measured quality and "
            f"{facts['cost_per_call']:.4f} per call"
            + (
                f", chosen over a {best_quality:.0%} model because the extra quality is "
                f"headroom this purpose does not use"
                if quality < best_quality
                else ""
            ),
        )

    def _as_choice(
        self, request_id, purpose, model_id, facts, quality, is_fitted, reason,
    ) -> LlmModelChoice:
        return LlmModelChoice(
            request_id=request_id,
            purpose=purpose,
            model_id=model_id,
            payment_kind=facts["payment_kind"],
            expected_cost=facts["cost_per_call"],
            expected_latency_seconds=facts["latency"],
            quality_on_this_purpose=quality,
            is_fitted=is_fitted,
            reason=reason,
            chosen_at_ns=self._now_ns(),
        )

    def _choice(self, request_id, state, choice, considered, reason) -> ModelChoice:
        return ModelChoice(
            request_id=request_id, state=state, choice=choice, considered=considered,
            reason=reason, chosen_at_ns=self._now_ns(),
        )


def describe_model_picking(picker: LlmModelPicker) -> dict:
    return {
        "part_id": PART_ID,
        "choices_made": picker.standing.choices_made,
        "exploratory_choices": picker.standing.exploratory_choices,
        "refused_no_model_clears_the_bar": (
            picker.standing.refused_no_model_clears_the_bar
        ),
        "outcomes_observed": picker.standing.outcomes_observed,
        "models_declared": picker.standing.models_declared,
        "times_the_cheapest_qualifying_model_won": (
            picker.standing.times_the_cheapest_qualifying_model_won
        ),
        "times_a_model_lost_its_place": picker.standing.times_a_model_lost_its_place,
        "ranks_models_globally": False,
        "always_picks_the_best_model": False,
        "makes_a_call": False,
    }


def run_llm_model_picker(
    picker: LlmModelPicker, control_socket, read_requests, publish_choices,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for request_id, purpose, quality_bar in read_requests():
            choice = picker.pick(request_id, purpose, quality_bar)
            if choice.is_usable:
                publish_choices(choice.choice)

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

    No model is declared on this box -- no subscription session, no paid
    endpoint, no local weights -- so every request is answered NO_MODELS by
    name and nothing is chosen. The quality a model shows is learned from
    call records, so the moment a model is declared its record starts.
    """
    from runtime.input_assembly import Batch

    requests = Batch(read=context.bus.reader("llm-request"))
    records = Batch(read=context.bus.reader("llm-call-record"))
    publish_choices = context.bus.publisher_for("llm-model-choice")
    picker = LlmModelPicker(
        exploration_share=context.number("llm_exploration_share"),
        minimum_observations=int(context.number("decoding_minimum_trades")),
        prior_quality=context.number("learning_prior_hit_rate"),
        prior_weight=context.number("learning_prior_weight"),
        half_life_observations=context.number("learning_half_life_observations"),
    )
    quality_bar = context.number("llm_quality_bar")

    def read_requests():
        for record in records.payloads():
            picker.observe_outcome(record.model_id, record.purpose, bool(record.succeeded))
        return tuple(
            (f"{request.purpose}:{request.venue_id}:{request.symbol}:{request.requested_at_ns}", request.purpose, quality_bar)
            for request in requests.payloads()
        )

    return run_llm_model_picker(
        picker=picker,
        control_socket=context.control_socket,
        read_requests=read_requests,
        publish_choices=lambda choice: publish_choices((choice,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
