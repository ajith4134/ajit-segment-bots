"""subscription-quota-watch: how much of the window is left, measured not assumed.

Quota is the resource this system runs out of first, and it is invisible: nothing
bills for it, nothing errors until it is gone, and then everything errors at once.
The watcher's job is to make the boundary visible before it arrives.

It counts from call records rather than trusting a provider header, for a reason
that matters: headers describe the provider's view at the moment of one response,
they arrive only when a call succeeds, and they are missing entirely on the failures
that also consumed quota. Counting locally means the number is always available and
always includes what the headers omit. When a header *is* available it is compared,
and a divergence is reported rather than silently reconciled -- a local count that
has drifted from the provider's is evidence about which calls are being counted, and
overwriting it destroys that evidence.

The window is a real rolling window with a reset instant. Two things follow, and
both are wrong in the naive version:

- **Quota does not refill gradually.** It resets. A part told "60% used" ten seconds
  before a reset should behave differently from one told the same thing at the start
  of a window, so the time to reset travels with every reading.
- **Usage is counted per window, not since start.** A running total is a number that
  only grows, and a system that throttles on it eventually throttles forever.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.llm_types import LlmQuotaState, SUBSCRIPTION
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "subscription-quota-watch"

PART_DECLARATION = PartDeclaration(
    part_id="subscription-quota-watch",
    consumes=("llm-call-record",),
    produces=("llm-quota-state", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

MEASURED = "measured"
EXHAUSTED = "the-window-is-spent"
DIVERGED_FROM_THE_PROVIDER = "the-local-count-and-the-provider-disagree"


@dataclass(frozen=True)
class QuotaReading:
    state: str
    quota: LlmQuotaState
    seconds_to_reset: float
    provider_calls_used: int | None
    divergence: int | None
    reason: str
    measured_at_ns: int

    @property
    def is_close_to_the_boundary(self) -> bool:
        return self.quota.fraction_used >= 0.8


@dataclass
class WatchStanding:
    records_counted: int = 0
    failed_calls_counted: int = 0
    windows_reset: int = 0
    readings: int = 0
    times_exhausted: int = 0
    divergences_reported: int = 0
    largest_divergence: int = 0
    provider_headers_seen: int = 0


class SubscriptionQuotaWatch:
    """Counts quota locally, compares with the provider, and reports divergence."""

    def __init__(
        self,
        window_seconds: float,
        calls_allowed: int,
        tokens_allowed: int,
        divergence_tolerance: int,
        now_ns=time.time_ns,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError(
                "a running total only grows, and a system that throttles on it "
                "eventually throttles forever"
            )
        if calls_allowed < 1 or tokens_allowed < 1:
            raise ValueError("a window that permits nothing is not a window")
        if divergence_tolerance < 0:
            raise ValueError("the tolerance is a non-negative number of calls")
        self._window_seconds = window_seconds
        self._calls_allowed = calls_allowed
        self._tokens_allowed = tokens_allowed
        self._divergence_tolerance = divergence_tolerance
        self._now_ns = now_ns
        self._window_started_at_ns = now_ns()
        self._calls_used = 0
        self._tokens_used = 0
        self._provider_calls_used: int | None = None
        self.standing = WatchStanding()

    def observe_record(self, record) -> None:
        """Every attempt counts, including the ones that failed."""
        if record.payment_kind != SUBSCRIPTION:
            return
        self._roll_if_the_window_ended()
        self.standing.records_counted += 1
        if not record.succeeded:
            self.standing.failed_calls_counted += 1
        self._calls_used += 1
        self._tokens_used += record.input_tokens + record.output_tokens

    def observe_provider_header(self, calls_used: int) -> None:
        """Compared, never trusted over the local count."""
        self._provider_calls_used = calls_used
        self.standing.provider_headers_seen += 1

    def seconds_to_reset(self) -> float:
        elapsed = (self._now_ns() - self._window_started_at_ns) / 1e9
        return max(self._window_seconds - elapsed, 0.0)

    def measure(self) -> QuotaReading:
        self._roll_if_the_window_ended()
        self.standing.readings += 1
        now = self._now_ns()
        seconds_left = self.seconds_to_reset()

        quota = LlmQuotaState(
            window_seconds=self._window_seconds,
            calls_used=self._calls_used,
            calls_allowed=self._calls_allowed,
            tokens_used=self._tokens_used,
            tokens_allowed=self._tokens_allowed,
            resets_at_ns=int(now + seconds_left * 1e9),
            measured_at_ns=now,
        )

        divergence = None
        if self._provider_calls_used is not None:
            divergence = self._calls_used - self._provider_calls_used
            if abs(divergence) > self._divergence_tolerance:
                self.standing.divergences_reported += 1
                self.standing.largest_divergence = max(
                    self.standing.largest_divergence, abs(divergence)
                )
                return QuotaReading(
                    state=DIVERGED_FROM_THE_PROVIDER, quota=quota,
                    seconds_to_reset=seconds_left,
                    provider_calls_used=self._provider_calls_used, divergence=divergence,
                    reason=(
                        f"counted {self._calls_used} locally against "
                        f"{self._provider_calls_used} from the provider, {divergence:+d}. "
                        f"That is reported rather than reconciled: which calls are being "
                        f"counted is the question, and overwriting the local count "
                        f"destroys the evidence"
                    ),
                    measured_at_ns=now,
                )

        if quota.is_exhausted:
            self.standing.times_exhausted += 1
            return QuotaReading(
                state=EXHAUSTED, quota=quota, seconds_to_reset=seconds_left,
                provider_calls_used=self._provider_calls_used, divergence=divergence,
                reason=(
                    f"{quota.fraction_used:.0%} of the window spent, with "
                    f"{seconds_left:.0f}s until it resets. Quota does not refill "
                    f"gradually -- nothing more is available until that instant"
                ),
                measured_at_ns=now,
            )

        return QuotaReading(
            state=MEASURED, quota=quota, seconds_to_reset=seconds_left,
            provider_calls_used=self._provider_calls_used, divergence=divergence,
            reason=(
                f"{self._calls_used}/{self._calls_allowed} call(s) and "
                f"{self._tokens_used}/{self._tokens_allowed} token(s) used, "
                f"{seconds_left:.0f}s to reset"
                + (
                    f", of which {self.standing.failed_calls_counted} call(s) failed and "
                    f"were counted anyway"
                    if self.standing.failed_calls_counted
                    else ""
                )
            ),
            measured_at_ns=now,
        )

    def _roll_if_the_window_ended(self) -> None:
        elapsed = (self._now_ns() - self._window_started_at_ns) / 1e9
        if elapsed >= self._window_seconds:
            windows = int(elapsed // self._window_seconds)
            self._window_started_at_ns += int(
                windows * self._window_seconds * 1e9
            )
            self._calls_used = 0
            self._tokens_used = 0
            self._provider_calls_used = None
            self.standing.windows_reset += 1


def describe_quota_watching(watch: SubscriptionQuotaWatch) -> dict:
    return {
        "part_id": PART_ID,
        "records_counted": watch.standing.records_counted,
        "failed_calls_counted": watch.standing.failed_calls_counted,
        "windows_reset": watch.standing.windows_reset,
        "readings": watch.standing.readings,
        "times_exhausted": watch.standing.times_exhausted,
        "provider_headers_seen": watch.standing.provider_headers_seen,
        "divergences_reported": watch.standing.divergences_reported,
        "largest_divergence": watch.standing.largest_divergence,
        "trusts_the_provider_header_over_the_local_count": False,
        "counts_since_start_instead_of_per_window": False,
    }


def run_subscription_quota_watch(
    watch: SubscriptionQuotaWatch, control_socket, read_records, publish_quota,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for record in read_records():
            watch.observe_record(record)
        publish_quota(watch.measure().quota)

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

    The window allows what the settings say, which on this box is nothing:
    no subscription session is installed, and the watch reports the window
    spent rather than a quota nobody has.
    """
    from runtime.input_assembly import Batch

    records = Batch(read=context.bus.reader("llm-call-record"))
    publish_quota = context.bus.publisher_for("llm-quota-state")
    watch = SubscriptionQuotaWatch(
        window_seconds=context.number("llm_quota_window_seconds"),
        calls_allowed=int(context.number("llm_quota_calls_allowed")),
        tokens_allowed=int(context.number("llm_quota_tokens_allowed")),
        divergence_tolerance=int(context.number("llm_quota_divergence_tolerance")),
    )

    return run_subscription_quota_watch(
        watch=watch,
        control_socket=context.control_socket,
        read_records=lambda: records.payloads(),
        publish_quota=lambda quota: publish_quota((quota,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
