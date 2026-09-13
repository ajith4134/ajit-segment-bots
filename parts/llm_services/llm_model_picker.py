"""llm-model-picker: which model, chosen from what each has actually done here.

Model choice is usually made once, by a person, from a benchmark that measures
something else. This part makes it a measured, per-purpose decision, because the
ranking is not global: a model that writes better prose can be worse at returning a
strict structure, and the purpose decides which of those matters.

So quality is tracked per (model, purpose) pair from outcomes this system observed:
did the answer pass the enforcer -- read from `llm-answer-verdict` since
2026-09-13; before that from `llm-call-record.succeeded`, which is only "the call
returned", so a rejected answer counted as a good one. Three consequences that a
single global ranking cannot express:

- **The cheapest model that clears the purpose's bar wins.** Not the best one. A
  purpose with a low bar answered by an expensive model is money spent on headroom
  nobody uses.
- **An unmeasured model is tried deliberately rather than assumed good or bad.** A
  small share of calls goes to unmeasured models, because a model never tried is a
  model whose quality is a guess forever -- and that exploration is a named setting
  with its own share, not a random accident.
- **A purpose no model has been measured on is explored, never refused.** Refusal
  stops a downgrade -- answering with a model measured below the bar -- and with
  nothing measured there is nothing to downgrade from. Until 2026-09-13 it refused
  anyway: one model declared, nothing measured, and 474 of 526 live requests were
  discarded while the exploration share let every tenth through, which protected
  nothing and starved the only thing that could ever produce a measurement.
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
    consumes=("llm-request", "llm-call-record", "llm-answer-verdict"),
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
    # Chosen because no model had been measured on the purpose at all. Counted
    # apart from exploratory_choices: those are a share taken from a purpose that
    # already has a measured model, these are the only way a purpose gets one.
    choices_for_a_purpose_nothing_is_measured_on: int = 0
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
        self._calls_by_purpose: dict[str, int] = {}
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
        calls_for_purpose = self._calls_by_purpose.get(purpose, 0) + 1
        self._calls_by_purpose[purpose] = calls_for_purpose
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

        # Nothing measured on this purpose: nothing to downgrade from, so the
        # cheapest unmeasured model answers and starts the purpose's record.
        if unmeasured and len(unmeasured) == len(considered):
            model_id, facts = min(unmeasured, key=lambda entry: entry[1]["cost_per_call"])
            self.standing.choices_for_a_purpose_nothing_is_measured_on += 1
            self.standing.exploratory_choices += 1
            self.standing.choices_made += 1
            return self._choice(
                request_id, EXPLORING,
                self._as_choice(request_id, purpose, model_id, facts, self._prior_quality, False,
                                "no model is measured on this purpose yet; cheapest one measures it"),
                tuple(considered),
                f"{model_id} chosen because no model has been measured on {purpose}. Refusing "
                f"would protect against a downgrade from nothing, and would stop the purpose "
                f"ever being measured",
            )

        # Exploration is deliberate and bounded: a model never tried is a model whose
        # quality stays a guess forever. Counted per purpose, so whether a purpose's
        # request lands on an exploring turn depends on that purpose's own traffic
        # rather than on how it interleaves with every other part's.
        should_explore = (
            unmeasured
            and self._exploration_share > 0
            and (calls_for_purpose % max(int(1 / self._exploration_share), 1) == 0)
        )
        if should_explore:
            model_id, facts = unmeasured[calls_for_purpose % len(unmeasured)]
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
        "choices_for_a_purpose_nothing_is_measured_on": (
            picker.standing.choices_for_a_purpose_nothing_is_measured_on
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


def observe_outcomes(picker: LlmModelPicker, records, verdicts) -> None:
    """Each answered call counted once, by whether its answer was usable.

    A call that returned is judged by `structured-output-enforcer`, and its
    verdict is the outcome. A call that failed outright never reaches the
    enforcer, so its record is the only evidence and it counts as bad. Reading
    a succeeded record as good as well would count every answered call twice,
    once as good whatever the enforcer later said.
    """
    for record in records:
        if not bool(record.succeeded):
            picker.observe_outcome(record.model_id, record.purpose, False)
    for verdict in verdicts:
        picker.observe_outcome(verdict.model_id, verdict.purpose, bool(verdict.was_usable))


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
        read_standing=lambda: describe_model_picking(picker),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    **Models are declared since 2026-09-07**, from the same two places the
    callers take their transports from, so a model this part offers is one
    something can actually answer:

    - the Claude subscription, whichever `llm_subscription_model` names, declared
      only because `claude -p` runs on the operator's own login and needs no key
    - every `llm_providers` row whose API key is really in the encrypted store --
      a row with a missing or `PLACEHOLDER_` key is not declared, because a model
      nothing can call is a choice this part would make and no caller could honour

    So with no provider key installed exactly one model is declared, and with
    none of either this part answers NO_MODELS by name as it always did. The
    quality a model shows is still learned from call records; declaring it only
    starts its record.
    """
    from runtime.input_assembly import Batch

    requests = Batch(read=context.bus.reader("llm-request"))
    records = Batch(read=context.bus.reader("llm-call-record"))
    verdicts = Batch(read=context.bus.reader("llm-answer-verdict"))
    publish_choices = context.bus.publisher_for("llm-model-choice")
    picker = LlmModelPicker(
        exploration_share=context.number("llm_exploration_share"),
        minimum_observations=int(context.number("llm_picker_minimum_verdicts")),
        prior_quality=context.number("learning_prior_hit_rate"),
        prior_weight=context.number("learning_prior_weight"),
        half_life_observations=context.number("learning_half_life_observations"),
    )
    quality_bar = context.number("llm_quality_bar")

    from runtime.secrets_reader import read_secret_field

    # The subscription always answers: `claude -p` runs on the operator's own
    # login. Its cost per call is the measured floor on this box -- about four
    # cents on Haiku, dominated by the ~20,000 cache-creation tokens that
    # ~/.claude/CLAUDE.md costs whatever is asked.
    picker.declare_model(
        model_id=str(context.setting("llm_subscription_model").value),
        payment_kind=SUBSCRIPTION,
        cost_per_call=context.number("llm_subscription_measured_cost_per_call"),
        typical_latency_seconds=context.number("llm_subscription_measured_latency_seconds"),
    )
    for row in context.setting("llm_providers").value:
        fields = str(row).split("|")
        if len(fields) != 7:
            continue
        _, _, model_id, secret, price_in, price_out, latency = fields
        group, _, field = secret.partition(".")
        if read_secret_field(group, field) is None:
            # No usable key, so nothing could answer a request routed here.
            continue
        picker.declare_model(
            model_id=model_id,
            payment_kind=METERED,
            # Priced at this part's own estimate of a call rather than per token,
            # which is what `cost_per_call` means here. A free tier is zero, and
            # zero is a price.
            cost_per_call=(
                float(price_in) * context.number("llm_estimated_input_tokens_per_call")
                + float(price_out) * context.number("llm_estimated_output_tokens")
            ),
            typical_latency_seconds=float(latency),
        )

    def read_requests():
        observe_outcomes(picker, records.payloads(), verdicts.payloads())
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
