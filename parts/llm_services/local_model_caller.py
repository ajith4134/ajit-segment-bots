"""local-model-caller: the model that runs here, competing for this machine's cores.

A local model is the one place where an LLM call is not an I/O wait but a compute
job on the same hardware every other part needs. That changes what the caller must
protect against: not a bill and not a rate limit, but starving the rest of the system
while one answer is generated.

So this part is written to the transistor rules rather than to a provider's API:

- **It declares itself compute-bound**, which means the governor pins its threads and
  it never sets its own parallelism (T-2, RL-066). A local model that helpfully uses
  every core is a part reaching around the governor.
- **It refuses rather than queues when the machine is busy.** A queue here would hold
  a decision behind a generation that takes thirty seconds, and by the time it ran
  the facts would be stale anyway.
- **It has a hard wall-clock limit per call.** Local generation has no rate limit to
  stop it, so an unbounded call is an unbounded hold on pinned cores.
- **It reports its own load so the router can stop offering it work**, rather than
  accepting everything and degrading quietly.

Quality is not assumed to be lower or higher. It is recorded like any other model's,
and the picker decides -- because "local means worse" is exactly the kind of
untested assumption that keeps money being spent on calls a smaller model answers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.llm_types import LlmCallRecord, LlmResponse, LOCAL
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "local-model-caller"

PART_DECLARATION = PartDeclaration(
    part_id="local-model-caller",
    consumes=("local-llm-request",),
    produces=("llm-response", "llm-call-record", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

ANSWERED = "answered"
MACHINE_IS_BUSY = "the-machine-has-no-room-for-another-generation"
TOOK_TOO_LONG = "the-generation-exceeded-its-wall-clock-limit"
NO_MODEL_LOADED = "no-local-model-is-loaded"
GENERATION_FAILED = "the-generation-failed"


@dataclass(frozen=True)
class LocalOutcome:
    routed_id: str
    state: str
    response: LlmResponse | None
    record: LlmCallRecord | None
    seconds: float
    concurrent_generations: int
    reason: str
    called_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == ANSWERED and self.response is not None


@dataclass
class LocalStanding:
    calls_made: int = 0
    answered: int = 0
    refused_machine_busy: int = 0
    exceeded_the_time_limit: int = 0
    failures: int = 0
    total_seconds_generating: float = 0.0
    peak_concurrent: int = 0
    times_it_set_its_own_thread_count: int = 0


class LocalModelCaller:
    """Generates locally under a concurrency cap and a hard wall-clock limit."""

    def __init__(
        self,
        maximum_concurrent_generations: int,
        maximum_seconds_per_call: float,
        tokens_per_second: float,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        if maximum_concurrent_generations < 1:
            raise ValueError(
                "a cap of zero means the part is off, which is the governor's decision "
                "rather than a configuration"
            )
        if maximum_seconds_per_call <= 0:
            raise ValueError(
                "local generation has no rate limit to stop it, so an unbounded call is "
                "an unbounded hold on pinned cores"
            )
        if tokens_per_second <= 0:
            raise ValueError(
                "the token rate is measured on this machine, not assumed from a "
                "benchmark elsewhere"
            )
        self._maximum_concurrent = maximum_concurrent_generations
        self._maximum_seconds = maximum_seconds_per_call
        self._tokens_per_second = tokens_per_second
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._generate = None
        self._model_id: str | None = None
        self._in_flight = 0
        self._sequence = 0
        self.standing = LocalStanding()

    def load_model(self, model_id: str, generate) -> None:
        """`generate(text, deadline_seconds) -> (answer, finish_reason, tokens)`."""
        self._model_id = model_id
        self._generate = generate

    def is_available(self) -> bool:
        """What the router asks before offering work, rather than being told after."""
        return self._generate is not None and self._in_flight < self._maximum_concurrent

    def load(self) -> float:
        return self._in_flight / self._maximum_concurrent

    def call(self, routed) -> LocalOutcome:
        if self._generate is None or self._model_id is None:
            return self._outcome(
                routed, NO_MODEL_LOADED, None, 0.0,
                "no local model is loaded, which is a fact about this machine rather than "
                "a failure of the request",
            )

        if self._in_flight >= self._maximum_concurrent:
            self.standing.refused_machine_busy += 1
            return self._outcome(
                routed, MACHINE_IS_BUSY, None, 0.0,
                f"{self._in_flight}/{self._maximum_concurrent} generation(s) already "
                f"running. This is refused rather than queued: a queue holds a decision "
                f"behind a thirty-second generation, and by the time it ran the facts "
                f"would be stale",
            )

        self._in_flight += 1
        self.standing.peak_concurrent = max(
            self.standing.peak_concurrent, self._in_flight
        )
        self.standing.calls_made += 1
        started = self._monotonic()
        try:
            text, finish_reason, output_tokens = self._generate(
                routed.text, self._maximum_seconds
            )
        except TimeoutError:
            seconds = self._monotonic() - started
            self.standing.exceeded_the_time_limit += 1
            self.standing.total_seconds_generating += seconds
            return self._outcome(
                routed, TOOK_TOO_LONG, None, seconds,
                f"stopped at {seconds:.1f}s against a {self._maximum_seconds:.1f}s limit. "
                f"The cores are released rather than held for an answer nobody is still "
                f"waiting for",
            )
        except Exception as failure:
            seconds = self._monotonic() - started
            self.standing.failures += 1
            self.standing.total_seconds_generating += seconds
            return self._outcome(
                routed, GENERATION_FAILED, None, seconds,
                f"the generation failed ({type(failure).__name__})",
            )
        finally:
            self._in_flight -= 1

        seconds = self._monotonic() - started
        self.standing.total_seconds_generating += seconds
        self.standing.answered += 1
        self._sequence += 1

        response = LlmResponse(
            response_id=f"resp-local-{self._sequence}",
            rendered_id=routed.rendered_id,
            version_id=routed.version_id,
            model_id=self._model_id,
            text=text,
            finish_reason=finish_reason,
            input_tokens=int(len(routed.text) / 4),
            output_tokens=output_tokens,
            latency_seconds=seconds,
            payment_kind=LOCAL,
            was_cached=False,
            responded_at_ns=self._now_ns(),
        )
        record = LlmCallRecord(
            call_id=f"call-{routed.routed_id}",
            part_id=routed.part_id,
            purpose=routed.purpose,
            version_id=routed.version_id,
            model_id=self._model_id,
            payment_kind=LOCAL,
            input_tokens=response.input_tokens,
            output_tokens=output_tokens,
            money_spent=0.0,
            quota_spent=0.0,
            latency_seconds=seconds,
            was_cached=False,
            succeeded=True,
            called_at_ns=self._now_ns(),
        )
        return LocalOutcome(
            routed_id=routed.routed_id, state=ANSWERED, response=response, record=record,
            seconds=seconds, concurrent_generations=self._in_flight,
            reason=(
                f"{output_tokens} token(s) in {seconds:.2f}s on this machine's own cores. "
                f"Quality is recorded like any other model's -- 'local means worse' is the "
                f"assumption that keeps money being spent on calls a smaller model answers"
            ),
            called_at_ns=self._now_ns(),
        )

    def _outcome(self, routed, state, response, seconds, reason) -> LocalOutcome:
        record = LlmCallRecord(
            call_id=f"call-{routed.routed_id}",
            part_id=routed.part_id,
            purpose=routed.purpose,
            version_id=routed.version_id,
            model_id=self._model_id or "none",
            payment_kind=LOCAL,
            input_tokens=0,
            output_tokens=0,
            money_spent=0.0,
            quota_spent=0.0,
            latency_seconds=seconds,
            was_cached=False,
            succeeded=False,
            called_at_ns=self._now_ns(),
        )
        return LocalOutcome(
            routed_id=routed.routed_id, state=state, response=response, record=record,
            seconds=seconds, concurrent_generations=self._in_flight, reason=reason,
            called_at_ns=self._now_ns(),
        )


def describe_local_calling(caller: LocalModelCaller) -> dict:
    return {
        "part_id": PART_ID,
        "calls_made": caller.standing.calls_made,
        "answered": caller.standing.answered,
        "refused_because_the_machine_was_busy": caller.standing.refused_machine_busy,
        "exceeded_the_time_limit": caller.standing.exceeded_the_time_limit,
        "failures": caller.standing.failures,
        "total_seconds_generating": caller.standing.total_seconds_generating,
        "peak_concurrent_generations": caller.standing.peak_concurrent,
        "load": caller.load(),
        "is_available": caller.is_available(),
        "sets_its_own_thread_count": False,
        "times_it_set_its_own_thread_count": (
            caller.standing.times_it_set_its_own_thread_count
        ),
        "queues_when_busy": False,
    }


def run_local_model_caller(
    caller: LocalModelCaller, control_socket, read_requests, publish_responses,
    publish_records, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for routed in read_requests():
            outcome = caller.call(routed)
            if outcome.record is not None:
                publish_records(outcome.record)
            if outcome.is_usable:
                publish_responses(outcome.response)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
