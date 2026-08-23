"""fund-lock-ledger: reserve capital the instant an order is sent.

Two orders in one tick must never spend the same balance. Without a lock taken
before the order leaves, both see the same free balance and both are sized
against it -- and the second one is only discovered when the venue rejects it, or
worse, does not.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "fund-lock-ledger"

PART_DECLARATION = PartDeclaration(
    part_id="fund-lock-ledger",
    consumes=("bounded-order", "fill", "account-balance"),
    produces=("locked-allocation", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

LOCKED = "locked"
REFUSED = "refused"
RELEASED = "released"


@dataclass(frozen=True)
class LockedAllocation:
    order_id: str
    venue_id: str
    symbol: str
    amount: float
    state: str
    free_balance_after: float
    reason: str
    decided_at_ns: int


@dataclass
class LockStanding:
    locks_taken: int = 0
    locks_refused: int = 0
    locks_released: int = 0
    double_locks_prevented: int = 0
    locked_total: float = 0.0
    balance: float = 0.0


class FundLockLedger:
    """Holds capital against live orders and refuses what the balance cannot cover.

    Refusing is the whole job. Sizing every order against the same free balance
    is how an account ends up committed to more than it holds, and the venue is
    not a safety net -- on a leveraged account it may accept every one of them.

    A lock is released when the order fills or is cancelled, and releasing an
    unknown order is a no-op rather than an error: a cancel arriving twice is
    ordinary, and refusing it would leave capital locked against nothing.
    """

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._balance = 0.0
        self._locks: dict[str, tuple[str, str, float]] = {}
        self.standing = LockStanding()

    def set_account_balance(self, balance: float) -> None:
        self._balance = balance
        self.standing.balance = balance

    @property
    def locked_total(self) -> float:
        return sum(amount for _, _, amount in self._locks.values())

    @property
    def free_balance(self) -> float:
        return self._balance - self.locked_total

    def lock(self, order_id: str, venue_id: str, symbol: str, amount: float) -> LockedAllocation:
        if order_id in self._locks:
            self.standing.double_locks_prevented += 1
            held = self._locks[order_id]
            return self._allocation(order_id, venue_id, symbol, held[2], LOCKED, "already locked for this order")

        if amount > self.free_balance:
            self.standing.locks_refused += 1
            return self._allocation(
                order_id, venue_id, symbol, amount, REFUSED,
                f"needs {amount} against {self.free_balance} free of {self._balance}",
            )

        self._locks[order_id] = (venue_id, symbol, amount)
        self.standing.locks_taken += 1
        self.standing.locked_total = self.locked_total
        return self._allocation(order_id, venue_id, symbol, amount, LOCKED, "held against this order")

    def release(self, order_id: str) -> LockedAllocation | None:
        held = self._locks.pop(order_id, None)
        if held is None:
            return None
        self.standing.locks_released += 1
        self.standing.locked_total = self.locked_total
        return self._allocation(order_id, held[0], held[1], held[2], RELEASED, "order finished")

    def _allocation(self, order_id, venue_id, symbol, amount, state, reason) -> LockedAllocation:
        return LockedAllocation(
            order_id=order_id,
            venue_id=venue_id,
            symbol=symbol,
            amount=amount,
            state=state,
            free_balance_after=self.free_balance,
            reason=reason,
            decided_at_ns=self._now_ns(),
        )

    def read_locks(self) -> tuple[LockedAllocation, ...]:
        return tuple(
            self._allocation(order_id, venue, symbol, amount, LOCKED, "currently held")
            for order_id, (venue, symbol, amount) in sorted(self._locks.items())
        )


def describe_locks(ledger: FundLockLedger) -> dict:
    return {
        "part_id": PART_ID,
        "balance": ledger.standing.balance,
        "locked_total": ledger.locked_total,
        "free_balance": ledger.free_balance,
        "locks_taken": ledger.standing.locks_taken,
        "locks_refused": ledger.standing.locks_refused,
        "locks_released": ledger.standing.locks_released,
        "double_locks_prevented": ledger.standing.double_locks_prevented,
    }


def run_fund_lock_ledger(
    ledger: FundLockLedger, control_socket, read_events, publish_locks,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        read_events(ledger)
        publish_locks(ledger.read_locks())

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

    A bounded order locks its capital under the decision that produced it;
    a fill carrying that order id releases it. The free balance it locks
    against is the segment's cash as the account keeper publishes it.
    """
    from runtime.input_assembly import Batch, LatestByKey

    orders = Batch(read=context.bus.reader("bounded-order"))
    fills = Batch(read=context.bus.reader("fill"))
    balances = LatestByKey(read=context.bus.reader("account-balance"), key_of=lambda b: b.segment)
    publish_locks = context.bus.publisher_for("locked-allocation")
    segment = str(context.setting("segment_id").value)
    ledger = FundLockLedger()

    def read_events(_ledger) -> None:
        balance = balances.mapping().get(segment)
        if balance is not None:
            ledger.set_account_balance(balance.cash)
        for order in orders.payloads():
            if order.quantity > 0 and order.intent_id:
                ledger.lock(order.intent_id, order.venue_id, order.symbol, order.capital_used)
        for fill in fills.payloads():
            if fill.order_id:
                ledger.release(fill.order_id)

    return run_fund_lock_ledger(
        ledger=ledger,
        control_socket=context.control_socket,
        read_events=read_events,
        publish_locks=publish_locks,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
