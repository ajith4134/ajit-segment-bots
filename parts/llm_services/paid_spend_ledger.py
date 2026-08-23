"""paid-spend-ledger: money spent on thinking, in the same currency as the results.

Quota comes back. Money does not, which is why this ledger is append-only and its
ceiling is not negotiable from inside the system. Everything here exists to make the
irreversible thing hard to lose track of.

- **Append-only, per call, with the part named.** A running total cannot answer "who
  spent it", and that is the only question worth asking after a surprise. Every
  entry names the part, the purpose and the prompt version.
- **The ceiling is a hard stop, not a target.** There is no method to raise it here.
  A budget that the spender can raise is a suggestion, and the moment it is raised
  under pressure is exactly when it should not be.
- **The period is calendar-shaped, not a rolling window.** Spend limits are agreed
  per day or per month, so the ledger resets on that boundary and reports the
  boundary, rather than on a sliding 24 hours that never lines up with a bill.
- **Results are in USDT (RL-028), and so is this.** Cost and PnL that are not in the
  same unit cannot be compared, and the comparison is the entire point.

The ledger reports; the router refuses. Keeping the refusal in one place means there
is one policy rather than two that can disagree.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.llm_types import LlmSpendState, METERED
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "paid-spend-ledger"

PART_DECLARATION = PartDeclaration(
    part_id="paid-spend-ledger",
    consumes=("llm-call-record",),
    produces=("llm-spend-state", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

RECORDED = "recorded"
NOT_METERED = "this-call-cost-no-money"
ALREADY_RECORDED = "already-in-the-ledger"
CEILING_REACHED = "the-period-ceiling-is-reached"

QUOTE_CURRENCY = "USDT"


@dataclass(frozen=True)
class LedgerEntry:
    """One irreversible payment, with everything needed to ask who made it."""

    entry_id: str
    call_id: str
    part_id: str
    purpose: str
    version_id: str
    model_id: str
    amount: float
    currency: str
    period_started_at_ns: int
    recorded_at_ns: int


@dataclass(frozen=True)
class LedgerOutcome:
    call_id: str
    state: str
    entry: LedgerEntry | None
    spend: LlmSpendState
    reason: str
    recorded_at_ns: int


@dataclass
class LedgerStanding:
    entries: int = 0
    duplicates: int = 0
    non_metered_ignored: int = 0
    periods_rolled: int = 0
    total_spent_all_time: float = 0.0
    times_the_ceiling_was_reached: int = 0
    ceiling_raises_attempted: int = 0


class PaidSpendLedger:
    """Append-only record of money spent, per period, per part."""

    def __init__(
        self,
        period_seconds: float,
        ceiling: float,
        period_started_at_ns: int | None = None,
        now_ns=time.time_ns,
    ) -> None:
        if period_seconds <= 0:
            raise ValueError(
                "spend limits are agreed per day or per month, so the period has a "
                "boundary; a sliding window never lines up with a bill"
            )
        if ceiling <= 0:
            raise ValueError("a ceiling of zero forbids every paid call, which is a policy "
                             "the router should state, not a ledger with no room")
        self._period_seconds = period_seconds
        self._ceiling = ceiling
        self._now_ns = now_ns
        self._period_started_at_ns = (
            period_started_at_ns if period_started_at_ns is not None else now_ns()
        )
        self._entries: list = []
        self._seen: set = set()
        self._spent = 0.0
        self._by_part: dict[str, float] = {}
        self._sequence = 0
        self.standing = LedgerStanding()

    def record(self, call_record) -> LedgerOutcome:
        self._roll_if_the_period_ended()

        if call_record.payment_kind != METERED or call_record.money_spent <= 0:
            self.standing.non_metered_ignored += 1
            return self._outcome(
                call_record.call_id, NOT_METERED, None,
                "this call cost no money. Quota and money are not netted: one comes back",
            )

        if call_record.call_id in self._seen:
            self.standing.duplicates += 1
            return self._outcome(
                call_record.call_id, ALREADY_RECORDED, None,
                "already in the ledger. The same call twice would double the spend",
            )

        self._sequence += 1
        entry = LedgerEntry(
            entry_id=f"spend-{self._sequence}",
            call_id=call_record.call_id,
            part_id=call_record.part_id,
            purpose=call_record.purpose,
            version_id=call_record.version_id,
            model_id=call_record.model_id,
            amount=call_record.money_spent,
            currency=QUOTE_CURRENCY,
            period_started_at_ns=self._period_started_at_ns,
            recorded_at_ns=self._now_ns(),
        )
        self._entries.append(entry)
        self._seen.add(call_record.call_id)
        self._spent += entry.amount
        self._by_part[entry.part_id] = self._by_part.get(entry.part_id, 0.0) + entry.amount
        self.standing.entries += 1
        self.standing.total_spent_all_time += entry.amount

        state = RECORDED
        reason = (
            f"{entry.amount:.4f} {QUOTE_CURRENCY} by {entry.part_id} for {entry.purpose}, "
            f"{self._spent:.4f}/{self._ceiling:.4f} this period"
        )
        if self._spent >= self._ceiling:
            state = CEILING_REACHED
            self.standing.times_the_ceiling_was_reached += 1
            reason += (
                ". The ceiling is reached. It cannot be raised from inside the system: a "
                "budget the spender can raise is a suggestion, and the moment it is raised "
                "under pressure is exactly when it should not be"
            )

        return self._outcome(call_record.call_id, state, entry, reason)

    def spend_state(self) -> LlmSpendState:
        self._roll_if_the_period_ended()
        return LlmSpendState(
            period_seconds=self._period_seconds,
            spent=self._spent,
            ceiling=self._ceiling,
            calls=len(self._entries),
            period_started_at_ns=self._period_started_at_ns,
            measured_at_ns=self._now_ns(),
        )

    def spent_by_part(self) -> dict:
        """The only question worth asking after a surprise."""
        return dict(sorted(self._by_part.items(), key=lambda item: -item[1]))

    def entries(self) -> tuple:
        return tuple(self._entries)

    def _roll_if_the_period_ended(self) -> None:
        elapsed = (self._now_ns() - self._period_started_at_ns) / 1e9
        if elapsed >= self._period_seconds:
            periods = int(elapsed // self._period_seconds)
            self._period_started_at_ns += int(periods * self._period_seconds * 1e9)
            self._spent = 0.0
            self._by_part.clear()
            self._entries.clear()
            self.standing.periods_rolled += 1

    def _outcome(self, call_id, state, entry, reason) -> LedgerOutcome:
        return LedgerOutcome(
            call_id=call_id, state=state, entry=entry, spend=LlmSpendState(
                period_seconds=self._period_seconds, spent=self._spent,
                ceiling=self._ceiling, calls=len(self._entries),
                period_started_at_ns=self._period_started_at_ns,
                measured_at_ns=self._now_ns(),
            ),
            reason=reason, recorded_at_ns=self._now_ns(),
        )


def describe_spend_ledger(ledger: PaidSpendLedger) -> dict:
    state = ledger.spend_state()
    return {
        "part_id": PART_ID,
        "entries_this_period": state.calls,
        "spent_this_period": state.spent,
        "ceiling": state.ceiling,
        "fraction_used": state.fraction_used,
        "currency": QUOTE_CURRENCY,
        "periods_rolled": ledger.standing.periods_rolled,
        "duplicates_refused": ledger.standing.duplicates,
        "total_spent_all_time": ledger.standing.total_spent_all_time,
        "spent_by_part": ledger.spent_by_part(),
        "times_the_ceiling_was_reached": ledger.standing.times_the_ceiling_was_reached,
        "can_raise_its_own_ceiling": False,
        "refuses_calls_itself": False,
    }


def run_paid_spend_ledger(
    ledger: PaidSpendLedger, control_socket, read_records, publish_spend,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for record in read_records():
            ledger.record(record)
        publish_spend(ledger.spend_state())

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
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    records = Batch(read=context.bus.reader("llm-call-record"))
    publish_spend = context.bus.publisher_for("llm-spend-state")
    ledger = PaidSpendLedger(
        period_seconds=context.number("llm_spend_period_seconds"),
        ceiling=context.number("llm_spend_ceiling"),
    )

    return run_paid_spend_ledger(
        ledger=ledger,
        control_socket=context.control_socket,
        read_records=lambda: records.payloads(),
        publish_spend=lambda spend: publish_spend((spend,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
