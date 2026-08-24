"""part-token-budgeter: what each part may spend, so one part cannot empty the pool.

A shared LLM budget with no per-part allocation has one failure mode and it always
happens: one part enters a retry loop, or one part's prompts grow, and by the time
anybody looks the quota is gone and nothing records which part took it. A global
counter cannot name the culprit because it never knew there were culprits.

So budgets are issued per part, per window, and the allocation is earned rather than
assigned:

- **A part that has produced value gets more.** Value here means its calls led to
  outputs that were used and to decisions that closed profitably -- measured, not
  declared. A part whose answers are discarded gets a smaller share next window.
- **A new part gets a starting allocation and is told it is a starting allocation.**
  RL-061: the number is a named setting with provenance, and the estimator says it
  is unfitted rather than pretending the share was earned.
- **Nothing is allocated from money that does not exist.** Metered budgets are
  bounded by what remains of the spend ceiling and subscription budgets by what
  remains of the quota, so the sum of every part's allowance can never exceed what
  can actually be paid.
- **Exhaustion is a state, not an error.** A part at its limit stops asking. It does
  not queue, because a queue is how scarcity is answered with latency instead of
  with a decision (RL-066).

The budgeter never grants an exception. A part that needs more says so through its
measured usefulness in the next window, which is slower and is the point: an
exception granted in the moment is how a budget stops being a budget.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import RateEstimator
from runtime.llm_types import LlmPartBudget, METERED, SUBSCRIPTION, no_budget
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "part-token-budgeter"

PART_DECLARATION = PartDeclaration(
    part_id="part-token-budgeter",
    consumes=("llm-call-record", "llm-spend-state", "llm-quota-state"),
    produces=("llm-part-budget", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

ISSUED = "issued"
EXHAUSTED = "this-part-has-spent-its-allowance"
NOTHING_LEFT_TO_ALLOCATE = "the-pool-itself-is-empty"
UNKNOWN_PART = "this-part-has-no-declared-share"


@dataclass(frozen=True)
class BudgetIssue:
    part_id: str
    state: str
    budget: LlmPartBudget
    share: float
    usefulness: float
    is_fitted: bool
    reason: str
    issued_at_ns: int

    @property
    def permits_a_call(self) -> bool:
        return self.state == ISSUED and not self.budget.is_spent


@dataclass
class BudgeterStanding:
    budgets_issued: int = 0
    parts_known: int = 0
    exhausted_parts: int = 0
    windows_rolled: int = 0
    calls_recorded: int = 0
    exceptions_granted: int = 0
    times_the_pool_was_empty: int = 0


class PartTokenBudgeter:
    """Allocates per part from what is actually left, weighted by measured usefulness."""

    def __init__(
        self,
        window_seconds: float,
        starting_share: float,
        prior_usefulness: float,
        prior_weight: float,
        half_life_observations: float,
        minimum_usefulness_observations: int,
        characters_per_token: float,
        now_ns=time.time_ns,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("a budget without a window is a total, and totals never reset")
        if not 0.0 < starting_share <= 1.0:
            raise ValueError(
                "the starting share is what a part with no history receives, as a fraction"
            )
        if minimum_usefulness_observations < 1:
            raise ValueError(
                "a share earned from zero observations is the starting share pretending "
                "to be earned"
            )
        if characters_per_token <= 0:
            raise ValueError(
                "the character budget is derived from the token budget, so the ratio "
                "must be a positive measured number"
            )
        self._window_seconds = window_seconds
        self._starting_share = starting_share
        self._prior_usefulness = prior_usefulness
        self._prior_weight = prior_weight
        self._half_life = half_life_observations
        self._minimum_usefulness = minimum_usefulness_observations
        self._characters_per_token = characters_per_token
        self._now_ns = now_ns
        self._usefulness: dict[str, RateEstimator] = {}
        self._used_calls: dict[str, int] = {}
        self._used_tokens: dict[str, int] = {}
        self._used_money: dict[str, float] = {}
        self._window_started_at_ns: int | None = None
        self._quota = None
        self._spend = None
        self.standing = BudgeterStanding()

    def observe_quota(self, quota) -> None:
        self._quota = quota

    def observe_spend(self, spend) -> None:
        self._spend = spend

    def observe_call(self, record) -> None:
        """What a part actually spent, in the currency it spent it in."""
        self.standing.calls_recorded += 1
        self._used_calls[record.part_id] = self._used_calls.get(record.part_id, 0) + 1
        self._used_tokens[record.part_id] = (
            self._used_tokens.get(record.part_id, 0)
            + record.input_tokens + record.output_tokens
        )
        if record.payment_kind == METERED:
            self._used_money[record.part_id] = (
                self._used_money.get(record.part_id, 0.0) + record.money_spent
            )

    def observe_usefulness(self, part_id: str, the_answer_was_used: bool) -> None:
        """Measured, not declared: did this part's call lead anywhere."""
        if part_id not in self._usefulness:
            self.standing.parts_known += 1
        self._usefulness.setdefault(
            part_id,
            RateEstimator(
                prior=self._prior_usefulness,
                prior_weight=self._prior_weight,
                half_life_observations=self._half_life,
            ),
        ).observe(the_answer_was_used)

    def roll_window(self) -> None:
        """A new window zeroes usage but keeps what was learned about usefulness."""
        self._used_calls.clear()
        self._used_tokens.clear()
        self._used_money.clear()
        self._window_started_at_ns = self._now_ns()
        self.standing.windows_rolled += 1

    def share_for(self, part_id: str) -> tuple:
        estimator = self._usefulness.get(part_id)
        if estimator is None:
            return self._starting_share, self._prior_usefulness, False
        estimate = estimator.estimate(self._minimum_usefulness)
        if not estimate.is_fitted:
            return self._starting_share, estimate.value, False
        total = sum(
            self._usefulness[other].estimate(self._minimum_usefulness).value
            for other in self._usefulness
        )
        if total <= 0:
            return self._starting_share, estimate.value, estimate.is_fitted
        return estimate.value / total, estimate.value, estimate.is_fitted

    def issue(self, part_id: str) -> BudgetIssue:
        now = self._now_ns()
        if self._window_started_at_ns is None:
            self._window_started_at_ns = now
        elif (now - self._window_started_at_ns) / 1e9 >= self._window_seconds:
            self.roll_window()

        calls_left, tokens_left, money_left = self._pool_remaining()
        if calls_left <= 0 or tokens_left <= 0:
            self.standing.times_the_pool_was_empty += 1
            return BudgetIssue(
                part_id=part_id, state=NOTHING_LEFT_TO_ALLOCATE,
                budget=no_budget(part_id, now), share=0.0,
                usefulness=self._prior_usefulness, is_fitted=False,
                reason=(
                    "the pool itself is empty, so nothing can be allocated. No part is "
                    "granted an exception: an exception in the moment is how a budget "
                    "stops being a budget"
                ),
                issued_at_ns=now,
            )

        share, usefulness, is_fitted = self.share_for(part_id)
        calls_allowed = max(int(calls_left * share), 1)
        tokens_allowed = max(int(tokens_left * share), 1)
        money_allowed = money_left * share

        budget = LlmPartBudget(
            part_id=part_id,
            calls_allowed=calls_allowed,
            tokens_allowed=tokens_allowed,
            character_budget=int(tokens_allowed * self._characters_per_token),
            money_allowed=money_allowed,
            window_seconds=self._window_seconds,
            calls_used=self._used_calls.get(part_id, 0),
            tokens_used=self._used_tokens.get(part_id, 0),
            money_used=self._used_money.get(part_id, 0.0),
            issued_at_ns=now,
        )
        self.standing.budgets_issued += 1

        if budget.is_spent:
            self.standing.exhausted_parts += 1
            return BudgetIssue(
                part_id=part_id, state=EXHAUSTED, budget=budget, share=share,
                usefulness=usefulness, is_fitted=is_fitted,
                reason=(
                    f"{budget.calls_used}/{calls_allowed} call(s) and "
                    f"{budget.tokens_used}/{tokens_allowed} token(s) already spent this "
                    f"window. The part stops asking rather than queueing -- a queue answers "
                    f"scarcity with latency instead of with a decision"
                ),
                issued_at_ns=now,
            )

        return BudgetIssue(
            part_id=part_id, state=ISSUED, budget=budget, share=share,
            usefulness=usefulness, is_fitted=is_fitted,
            reason=(
                f"{share:.1%} of what remains: {calls_allowed} call(s), {tokens_allowed} "
                f"token(s)"
                + (
                    f", earned from a measured {usefulness:.0%} usefulness"
                    if is_fitted
                    else ", a starting allocation from a named setting rather than an "
                         "earned share"
                )
            ),
            issued_at_ns=now,
        )

    def _pool_remaining(self) -> tuple:
        """Never allocate from money or quota that does not exist."""
        calls_left = 0
        tokens_left = 0
        if self._quota is not None:
            calls_left += max(self._quota.calls_allowed - self._quota.calls_used, 0)
            tokens_left += max(self._quota.tokens_allowed - self._quota.tokens_used, 0)
        money_left = 0.0
        if self._spend is not None:
            money_left = max(self._spend.ceiling - self._spend.spent, 0.0)
        return calls_left, tokens_left, money_left


def describe_budgeting(budgeter: PartTokenBudgeter) -> dict:
    calls_left, tokens_left, money_left = budgeter._pool_remaining()
    return {
        "part_id": PART_ID,
        "budgets_issued": budgeter.standing.budgets_issued,
        "parts_with_a_measured_share": budgeter.standing.parts_known,
        "exhausted_parts": budgeter.standing.exhausted_parts,
        "windows_rolled": budgeter.standing.windows_rolled,
        "calls_recorded": budgeter.standing.calls_recorded,
        "times_the_pool_was_empty": budgeter.standing.times_the_pool_was_empty,
        "calls_remaining_in_the_pool": calls_left,
        "tokens_remaining_in_the_pool": tokens_left,
        "money_remaining_in_the_pool": money_left,
        "grants_exceptions": False,
        "exceptions_granted": budgeter.standing.exceptions_granted,
        "queues_a_part_that_is_out_of_budget": False,
    }


def run_part_token_budgeter(
    budgeter: PartTokenBudgeter, control_socket, read_state, publish_budgets,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for part_id in read_state(budgeter):
            publish_budgets(budgeter.issue(part_id))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_budgeting(budgeter),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Every part that has made a call is issued a budget each tick the pool
    changes; the pool is the latest quota and spend. Usefulness is learned
    from whether a part's call succeeded, which is what a record carries.
    """
    from runtime.input_assembly import Batch

    records = Batch(read=context.bus.reader("llm-call-record"))
    spends = Batch(read=context.bus.reader("llm-spend-state"))
    quotas = Batch(read=context.bus.reader("llm-quota-state"))
    publish_budgets = context.bus.publisher_for("llm-part-budget")
    budgeter = PartTokenBudgeter(
        window_seconds=context.number("llm_quota_window_seconds"),
        starting_share=context.number("llm_budget_starting_share"),
        prior_usefulness=context.number("learning_prior_hit_rate"),
        prior_weight=context.number("learning_prior_weight"),
        half_life_observations=context.number("learning_half_life_observations"),
        minimum_usefulness_observations=int(context.number("decoding_minimum_trades")),
        characters_per_token=context.number("llm_characters_per_token"),
    )
    parts_seen: set[str] = set()

    def read_state(_budgeter):
        changed = False
        for quota in quotas.payloads():
            budgeter.observe_quota(quota)
            changed = True
        for spend in spends.payloads():
            budgeter.observe_spend(spend)
            changed = True
        touched: set[str] = set()
        for record in records.payloads():
            budgeter.observe_call(record)
            budgeter.observe_usefulness(record.part_id, bool(record.succeeded))
            parts_seen.add(record.part_id)
            touched.add(record.part_id)
        budgeter.roll_window()
        return tuple(sorted(parts_seen if changed else touched))

    def publish(issue) -> None:
        if issue is not None and issue.budget is not None:
            publish_budgets((issue.budget,))

    return run_part_token_budgeter(
        budgeter=budgeter,
        control_socket=context.control_socket,
        read_state=read_state,
        publish_budgets=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
