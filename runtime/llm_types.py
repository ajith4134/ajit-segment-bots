"""The vocabulary the two LLM blocks pass between themselves.

A language model is the least trustworthy component in this system: it is
non-deterministic, it is charged for, it changes underneath its own version
number, and it is fluent enough that a wrong answer reads exactly like a right
one. Every shape in this module exists to make one of those properties visible
where a naive design would hide it.

Four ideas run through all of them:

- **A request and its answer are separate objects with a versioned prompt between
  them.** Anything a model produced can be traced to the exact prompt version, the
  exact model, and the context it was given. Without that, a quality change cannot
  be attributed to a prompt edit, a model update, or luck.
- **Nothing a model writes is admitted before it is checked.** `LlmResponse` is
  raw; `ValidatedLlmOutput` is what survived structural and factual checking, and
  they are deliberately different types so raw text cannot be passed where checked
  text is expected.
- **Every call has a price, and the price is in two currencies.** Subscription
  calls spend quota that refills; metered calls spend money that does not. Mixing
  them into one "cost" number makes the irreversible one invisible.
- **A budget is per part.** One part looping on a retry can consume everything a
  whole segment needs. The budget therefore belongs to the part, and the part that
  exhausted it is nameable afterwards.

`LlmRequest` itself lives in `runtime.claim_verification`, because a request
carries the facts its answer will be checked against and the checking is that
module's job. Everything downstream of a request is here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Where a call is paid from. These do not net against each other.
SUBSCRIPTION = "subscription"          # quota that refills on a clock
METERED = "metered"                    # money, spent and gone
LOCAL = "local"                        # this machine's own compute and electricity

PAYMENT_KINDS = (SUBSCRIPTION, METERED, LOCAL)

# What a rendered request is allowed to become when nothing can serve it.
DEFERRED = "deferred"
REFUSED = "refused"


@dataclass(frozen=True)
class PromptTemplate:
    """An unversioned draft of a prompt, before anything has been proven about it."""

    template_id: str
    purpose: str
    instruction: str
    required_context_kinds: tuple
    output_schema: dict
    written_at_ns: int
    written_by: str
    derived_from: tuple = ()

    @property
    def declares_its_output_shape(self) -> bool:
        """A prompt without a declared output shape cannot be enforced against."""
        return bool(self.output_schema)


@dataclass(frozen=True)
class PromptVersion:
    """A template pinned to an immutable version, which is what actually runs.

    Templates are edited; versions never are. Every piece of generated text in this
    system names the version that produced it, so a change in answer quality can be
    attributed rather than guessed at.
    """

    version_id: str
    template_id: str
    purpose: str
    instruction: str
    output_schema: dict
    required_context_kinds: tuple
    is_active: bool
    promoted_at_ns: int | None
    created_at_ns: int
    supersedes: str | None = None


@dataclass(frozen=True)
class Embedding:
    """One vector, and enough to know what it is a vector of.

    The model identity is part of the record because vectors from two models are
    not comparable, and a mixed index silently returns nonsense rather than failing.
    """

    embedding_id: str
    source_kind: str
    source_reference: str
    text: str
    vector: tuple
    model_id: str
    dimensions: int
    embedded_at_ns: int


@dataclass(frozen=True)
class RetrievalQuery:
    """What is being looked for, and how much of the answer may be returned."""

    query_id: str
    request_id: str
    text: str
    wanted_kinds: tuple
    maximum_hits: int
    minimum_similarity: float
    asked_at_ns: int


@dataclass(frozen=True)
class RetrievalHit:
    """One retrieved passage, with its similarity kept rather than discarded.

    Similarity is carried through to the assembler because a weak hit and a strong
    hit look identical once they are pasted into a prompt, and the weak one is how
    an unrelated document ends up being treated as evidence.
    """

    hit_id: str
    query_id: str
    source_kind: str
    source_reference: str
    text: str
    similarity: float
    characters: int
    embedded_at_ns: int
    retrieved_at_ns: int


@dataclass(frozen=True)
class RetrievalScore:
    """Whether retrieved passages actually helped the answers they were put into."""

    source_reference: str
    times_retrieved: int
    times_the_answer_was_good: int
    usefulness: float
    is_fitted: bool
    scored_at_ns: int


@dataclass(frozen=True)
class PromptContext:
    """Everything a prompt will be given, already inside the budget it must fit.

    Assembly is where a prompt silently becomes too long and the last section --
    usually the measured facts -- falls off the end. So what was dropped is part of
    the object rather than lost, and the caller can see the answer was formed
    without it.
    """

    context_id: str
    request_id: str
    sections: tuple
    characters: int
    character_budget: int
    dropped_sections: tuple
    verified_facts: dict
    assembled_at_ns: int

    @property
    def was_truncated(self) -> bool:
        return bool(self.dropped_sections)


@dataclass(frozen=True)
class RenderedLlmRequest:
    """A prompt version plus its context, frozen into exactly what will be sent."""

    rendered_id: str
    request_id: str
    version_id: str
    purpose: str
    text: str
    output_schema: dict
    facts: dict
    context_id: str | None
    characters: int
    fingerprint: str
    rendered_at_ns: int
    # Carried through from the request that asked. `llm-request-router` needs it
    # to find the asking part's budget: until 2026-09-12 it looked the budget up
    # by `str(context_id)`, which is the id of an assembled context and never a
    # part id, so the lookup could not have succeeded even once budgets existed.
    asked_by: str = ""
    # Carried through from the request so `structured-output-enforcer` can build a
    # repair that is the same question -- same subject, same sentence bound, same
    # asker -- and count it against the chain it belongs to (2026-09-13).
    venue_id: str = ""
    symbol: str = ""
    maximum_sentences: int = 0
    repair_of: str = ""
    repair_attempt: int = 0


@dataclass(frozen=True)
class LlmResponse:
    """Raw output. Deliberately a different type from anything usable."""

    response_id: str
    rendered_id: str
    version_id: str
    model_id: str
    text: str
    finish_reason: str
    input_tokens: int
    output_tokens: int
    latency_seconds: float
    payment_kind: str
    was_cached: bool
    responded_at_ns: int

    @property
    def was_cut_off(self) -> bool:
        """A truncated answer parses as a complete one surprisingly often."""
        return self.finish_reason == "length"


@dataclass(frozen=True)
class ValidatedLlmOutput:
    """What survived structural and factual checking, and what did not."""

    output_id: str
    response_id: str
    version_id: str
    purpose: str
    value: dict
    text: str
    removed_sentences: tuple
    unsupported_claims: tuple
    repair_attempts: int
    validated_at_ns: int

    @property
    def is_fully_supported(self) -> bool:
        return not self.unsupported_claims


@dataclass(frozen=True)
class LlmAnswerVerdict:
    """Whether one model answer was usable, as `structured-output-enforcer` judged it.

    What `llm-model-picker` learns a model's quality on a purpose from. Added
    2026-09-13: the picker learned from `LlmCallRecord.succeeded`, which is "the
    call returned", so an answer the enforcer threw away counted as a good one.
    `was_usable` is the enforcer's own answer to the question the picker's
    docstring always asked. A response sent back for repair is not usable: a
    model that needed a second try did worse on that purpose than one that did not.
    """

    response_id: str
    rendered_id: str
    version_id: str
    purpose: str
    model_id: str
    was_usable: bool
    state: str
    judged_at_ns: int


@dataclass(frozen=True)
class LlmCallRecord:
    """One call as an accounting fact: who asked, what it cost, in which currency."""

    call_id: str
    part_id: str
    purpose: str
    version_id: str
    model_id: str
    payment_kind: str
    input_tokens: int
    output_tokens: int
    money_spent: float
    quota_spent: float
    latency_seconds: float
    was_cached: bool
    succeeded: bool
    called_at_ns: int

    @property
    def spent_something_irreversible(self) -> bool:
        return self.payment_kind == METERED and self.money_spent > 0


@dataclass(frozen=True)
class LlmQuotaState:
    """Subscription quota: how much is left and when it comes back."""

    window_seconds: float
    calls_used: int
    calls_allowed: int
    tokens_used: int
    tokens_allowed: int
    resets_at_ns: int
    measured_at_ns: int

    @property
    def fraction_used(self) -> float:
        by_calls = self.calls_used / self.calls_allowed if self.calls_allowed else 0.0
        by_tokens = self.tokens_used / self.tokens_allowed if self.tokens_allowed else 0.0
        return max(by_calls, by_tokens)

    @property
    def is_exhausted(self) -> bool:
        return self.fraction_used >= 1.0


@dataclass(frozen=True)
class LlmSpendState:
    """Metered spend: money gone, against a ceiling that is not negotiable."""

    period_seconds: float
    spent: float
    ceiling: float
    calls: int
    period_started_at_ns: int
    measured_at_ns: int

    @property
    def fraction_used(self) -> float:
        return self.spent / self.ceiling if self.ceiling else 1.0

    @property
    def is_exhausted(self) -> bool:
        return self.spent >= self.ceiling


@dataclass(frozen=True)
class LlmPartBudget:
    """What one part may spend before it must stop asking.

    Per part rather than global, because one part retrying in a loop is the failure
    that empties a shared budget, and a global number cannot name it.
    """

    part_id: str
    calls_allowed: int
    tokens_allowed: int
    character_budget: int
    money_allowed: float
    window_seconds: float
    calls_used: int
    tokens_used: int
    money_used: float
    issued_at_ns: int

    @property
    def calls_remaining(self) -> int:
        return max(self.calls_allowed - self.calls_used, 0)

    @property
    def is_spent(self) -> bool:
        return (
            self.calls_used >= self.calls_allowed
            or self.tokens_used >= self.tokens_allowed
            or (self.money_allowed > 0 and self.money_used >= self.money_allowed)
        )


@dataclass(frozen=True)
class LlmModelChoice:
    """Which model should answer this, and what that choice is based on."""

    request_id: str
    purpose: str
    model_id: str
    payment_kind: str
    expected_cost: float
    expected_latency_seconds: float
    quality_on_this_purpose: float
    is_fitted: bool
    reason: str
    chosen_at_ns: int


@dataclass(frozen=True)
class LlmBackpressure:
    """How hard the whole LLM subsystem is being told to slow down.

    One number with a reason, because the alternative -- each caller inspecting
    quota, spend and survival tier itself -- produces callers that disagree about
    whether the system is out of budget.
    """

    admit_fraction: float
    reason: str
    quota_fraction_used: float
    spend_fraction_used: float
    survival_tier: str | None
    measured_at_ns: int

    @property
    def is_stopped(self) -> bool:
        return self.admit_fraction <= 0.0


@dataclass(frozen=True)
class GoldenCase:
    """A real past decision whose right answer is known, kept to test prompts on.

    Golden cases come from closed trades, so the answer is what happened rather
    than what somebody thought should happen. That is the difference between
    evaluating a prompt and confirming a preference.
    """

    case_id: str
    purpose: str
    facts: dict
    context_sections: tuple
    expected_value: dict
    outcome_was_known_at_ns: int
    source_reference: str
    added_at_ns: int


@dataclass(frozen=True)
class PromptScore:
    """How a prompt version did on the golden set, per dimension."""

    version_id: str
    purpose: str
    cases_run: int
    schema_valid_fraction: float
    factually_supported_fraction: float
    agreement_with_outcome: float
    mean_output_tokens: float
    mean_latency_seconds: float
    is_fitted: bool
    scored_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.is_fitted and self.cases_run > 0


@dataclass(frozen=True)
class PromptPromotion:
    """A decision that one version replaces another, with what it beat."""

    version_id: str
    template_id: str
    purpose: str
    replaces: str | None
    margin: float
    cases_run: int
    reason: str
    promoted_at_ns: int


@dataclass(frozen=True)
class DecisionCost:
    """What one decision cost to make, against what it made.

    The comparison nobody runs: a system can spend more reasoning about a trade
    than the trade returns, and nothing in a normal PnL statement shows it.
    """

    decision_id: str
    purpose: str
    calls: int
    money_spent: float
    quota_spent: float
    tokens: float
    realised_pnl: float | None
    is_settled: bool
    measured_at_ns: int

    @property
    def cost_exceeded_the_gain(self) -> bool:
        return self.is_settled and self.realised_pnl is not None and (
            self.money_spent > max(self.realised_pnl, 0.0)
        )


@dataclass(frozen=True)
class VerifiedSnapshot:
    """Measured market facts, frozen, for a prompt to be checked against.

    A model must never be asked to recall a number. It is given the numbers, and
    every number in its answer is matched back to this snapshot -- so the snapshot
    is the boundary between what is known and what is generated.
    """

    snapshot_id: str
    venue_id: str
    symbol: str
    facts: dict
    measured_at_ns: int
    staleness_seconds: float
    is_complete: bool
    missing_facts: tuple

    @property
    def can_be_used_as_ground_truth(self) -> bool:
        return self.is_complete and bool(self.facts)


def no_budget(part_id: str, issued_at_ns: int) -> LlmPartBudget:
    """A budget that permits nothing, which is what an unknown part gets."""
    return LlmPartBudget(
        part_id=part_id, calls_allowed=0, tokens_allowed=0, character_budget=0,
        money_allowed=0.0, window_seconds=0.0, calls_used=0, tokens_used=0,
        money_used=0.0, issued_at_ns=issued_at_ns,
    )


@dataclass(frozen=True)
class RoutedLlmRequest:
    """A rendered request assigned to one place that will actually pay for it.

    The router produces three named data types -- subscription, paid, local -- and
    they are the same shape because the difference that matters is not structural,
    it is who is billed. Keeping one shape means a caller cannot be written to work
    on only one of them by accident, and keeping the field means nothing can lose
    track of which pocket the money came out of.
    """

    routed_id: str
    rendered_id: str
    request_id: str
    version_id: str
    purpose: str
    part_id: str
    payment_kind: str
    model_id: str
    text: str
    output_schema: dict
    facts: dict
    fingerprint: str
    admitted_fraction: float
    reason: str
    routed_at_ns: int

    @property
    def costs_money(self) -> bool:
        return self.payment_kind == METERED
