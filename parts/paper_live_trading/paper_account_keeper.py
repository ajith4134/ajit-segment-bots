"""paper-account-keeper: the paper balance, as paper fills land against the allotment.

The paper account exists so that a strategy can be judged on money rather than on
signals. That only works if the balance is kept the way a real one would be, so
this keeps the two things a naive simulator conflates:

- **Cash**, which moves on fees and realised results only.
- **Equity**, which is cash plus what open positions are currently worth.

A simulator that tracked one number would let a strategy hold a losing position
indefinitely while its balance looked untouched -- and that is exactly the
behaviour paper trading is supposed to expose.

**It starts at the allotment and never invents money.** A paper account that
cannot afford a fill records the shortfall rather than going negative quietly: a
strategy that overspends its allocation on paper would do the same live, where
the venue would reject it, and the paper record must show the same refusal.

Only paper fills are admitted. A live fill reaching this part would corrupt the
paper record with real money, and the two must stay separable forever.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import BUY, LONG, SHORT

PART_ID = "paper-account-keeper"

PART_DECLARATION = PartDeclaration(
    part_id="paper-account-keeper",
    consumes=("fill", "capital-allotment", "money-mode", "paper-currency-rate"),
    produces=("account-balance", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

APPLIED = "applied"

# What this part's checkpoint is called under `position_state_root`, beside the
# lot books and the resting stops.
CHECKPOINT_COMPONENT = "paper-account"
REFUSED_LIVE_FILL = "refused-live-fill"
REFUSED_DUPLICATE = "refused-duplicate-fill"
REFUSED_INSUFFICIENT = "refused-insufficient-paper-balance"


@dataclass(frozen=True)
class PaperBalance:
    """The paper account, with cash and equity kept apart."""

    segment: str
    currency: str
    starting_balance: float
    cash: float
    unrealised: float
    equity: float
    realised_total: float
    fees_total: float
    open_positions: int
    fills_applied: int
    reason: str
    read_at_ns: int

    @property
    def free_cash(self) -> float:
        return max(0.0, self.cash)


@dataclass
class _PaperPosition:
    quantity: float
    average_price: float


@dataclass
class KeeperStanding:
    fills_applied: int = 0
    live_fills_refused: int = 0
    duplicates_refused: int = 0
    insufficient_refused: int = 0
    realised_total: float = 0.0
    fees_total: float = 0.0
    lowest_cash: float | None = None
    # What came back from the checkpoint, and what the store made of the file.
    # Named `restored_symbols` because that is the field
    # `restore_and_arm_checkpoint` sets, and the shape is what the substrate
    # knows this part by (T-4).
    restored_symbols: int = 0
    checkpoint_verdict: str = "no checkpoint has been read yet"


class PaperAccountKeeper:
    """Applies paper fills to a paper balance that starts at the segment's allotment."""

    def __init__(self, segment: str, currency: str = "USDT", now_ns=time.time_ns) -> None:
        self._segment = segment
        self._currency = currency
        self._now_ns = now_ns
        self._starting = 0.0
        self._cash = 0.0
        self._positions: dict[tuple[str, str], _PaperPosition] = {}
        self._marks: dict[tuple[str, str], float] = {}
        self._seen_fills: set[str] = set()
        self.standing = KeeperStanding()

    def read_checkpoint_state(self) -> dict:
        """The account, as the next process needs to find it.

        Cash, what is held, and what has already been realised. Without this the
        paper account started every run at the full allotment while
        `fill-reconciler` restored the positions that had spent it: measured on
        the live spine at 16:10 on 2026-08-26, eight positions open against a
        keeper reporting `fills_applied` 0, `open_positions` 0 and equity exactly
        10,000. Every risk cap in the segment is a fraction of that number.
        """
        return {
            "starting": self._starting,
            "cash": self._cash,
            "realised_total": self.standing.realised_total,
            "fees_total": self.standing.fees_total,
            "fills_applied": self.standing.fills_applied,
            "positions": {
                f"{venue_id}|{symbol}": {
                    "quantity": position.quantity,
                    "average_price": position.average_price,
                }
                for (venue_id, symbol), position in self._positions.items()
            },
            # The fills already applied, so a fill redelivered across a restart is
            # still refused as a duplicate rather than spending the cash twice.
            "seen_fills": sorted(self._seen_fills),
        }

    def restore_from_checkpoint(self, state: dict) -> int:
        """Bring the account back, and answer how many positions came with it."""
        self._starting = float(state.get("starting", 0.0))
        self._cash = float(state.get("cash", 0.0))
        self.standing.realised_total = float(state.get("realised_total", 0.0))
        self.standing.fees_total = float(state.get("fees_total", 0.0))
        self.standing.fills_applied = int(state.get("fills_applied", 0))
        self._positions = {}
        for key, held in (state.get("positions") or {}).items():
            venue_id, _, symbol = key.partition("|")
            quantity = float(held["quantity"])
            if quantity == 0:
                continue
            self._positions[(venue_id, symbol)] = _PaperPosition(
                quantity, float(held["average_price"])
            )
        self._seen_fills = set(state.get("seen_fills") or ())
        return len(self._positions)

    def set_allotment(self, allotted: float) -> None:
        """The paper account starts at what the operator allocated, and only there.

        Re-setting adjusts the starting point and the cash by the same amount, so
        an operator raising an allocation mid-run adds capital rather than
        erasing the results so far.
        """
        difference = allotted - self._starting
        self._starting = allotted
        self._cash += difference

    def observe_mark_price(self, venue_id: str, symbol: str, price: float) -> None:
        self._marks[(venue_id, symbol)] = price

    def apply_fill(self, fill) -> str:
        """Apply one paper fill to the balance, or say why it was refused."""
        if not fill.is_paper:
            self.standing.live_fills_refused += 1
            return REFUSED_LIVE_FILL

        if fill.fill_id in self._seen_fills:
            self.standing.duplicates_refused += 1
            return REFUSED_DUPLICATE

        key = (fill.venue_id, fill.symbol)
        held = self._positions.get(key)
        cost = fill.quantity * fill.price
        opening = held is None or held.quantity == 0 or (held.quantity > 0) == (fill.side == BUY)

        if opening and cost + fill.fee > self._cash:
            # A paper account that went negative would let a strategy spend money
            # a venue would have refused it, and the paper record would show a
            # trade that could not have happened.
            self.standing.insufficient_refused += 1
            return REFUSED_INSUFFICIENT

        self._seen_fills.add(fill.fill_id)
        self._cash -= fill.fee
        self.standing.fees_total += fill.fee

        signed = fill.signed_quantity
        if held is None:
            self._positions[key] = _PaperPosition(signed, fill.price)
            self._cash -= cost if signed > 0 else -cost
        else:
            self._apply_to_position(key, held, fill, signed, cost)

        self.standing.fills_applied += 1
        if self.standing.lowest_cash is None or self._cash < self.standing.lowest_cash:
            self.standing.lowest_cash = self._cash
        return APPLIED

    def _apply_to_position(self, key, held, fill, signed, cost) -> None:
        increasing = held.quantity == 0 or (held.quantity > 0) == (signed > 0)
        if increasing:
            total = abs(held.quantity) + abs(signed)
            held.average_price = (
                abs(held.quantity) * held.average_price + abs(signed) * fill.price
            ) / total
            held.quantity += signed
            self._cash -= cost if signed > 0 else -cost
            return

        closed = min(abs(held.quantity), abs(signed))
        gain = (fill.price - held.average_price) * closed
        realised = gain if held.quantity > 0 else -gain
        self._cash += realised
        # Returning the capital the closed portion had committed.
        self._cash += closed * held.average_price if held.quantity > 0 else -closed * held.average_price
        self.standing.realised_total += realised

        held.quantity += signed
        if abs(signed) > closed:
            held.average_price = fill.price
            self._cash -= abs(held.quantity) * fill.price if held.quantity > 0 else -abs(held.quantity) * fill.price
        if held.quantity == 0:
            del self._positions[key]

    def read_balance(self) -> PaperBalance:
        unrealised = 0.0
        for (venue_id, symbol), position in self._positions.items():
            mark = self._marks.get((venue_id, symbol))
            if mark is None:
                continue
            gain = (mark - position.average_price) * abs(position.quantity)
            unrealised += gain if position.quantity > 0 else -gain

        committed = sum(
            abs(position.quantity) * position.average_price for position in self._positions.values()
        )
        equity = self._cash + committed + unrealised

        return PaperBalance(
            segment=self._segment,
            currency=self._currency,
            starting_balance=self._starting,
            cash=self._cash,
            unrealised=unrealised,
            equity=equity,
            realised_total=self.standing.realised_total,
            fees_total=self.standing.fees_total,
            open_positions=len(self._positions),
            fills_applied=self.standing.fills_applied,
            reason=(
                f"{self._cash:,.2f} cash, {committed:,.2f} committed, "
                f"{unrealised:,.2f} unrealised across {len(self._positions)} position(s)"
            ),
            read_at_ns=self._now_ns(),
        )


def describe_paper_account(keeper: PaperAccountKeeper) -> dict:
    balance = keeper.read_balance()
    return {
        "part_id": PART_ID,
        "segment": balance.segment,
        "starting_balance": balance.starting_balance,
        "cash": balance.cash,
        "equity": balance.equity,
        "realised_total": balance.realised_total,
        "fees_total": balance.fees_total,
        "open_positions": balance.open_positions,
        "fills_applied": keeper.standing.fills_applied,
        "live_fills_refused": keeper.standing.live_fills_refused,
        "duplicates_refused": keeper.standing.duplicates_refused,
        "insufficient_refused": keeper.standing.insufficient_refused,
        "lowest_cash": keeper.standing.lowest_cash,
    }


def run_paper_account_keeper(
    keeper: PaperAccountKeeper, control_socket, read_fills, publish_balance,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
    write_checkpoint=None,
) -> int:
    def tick() -> None:
        applied = 0
        for fill in read_fills(keeper):
            if keeper.apply_fill(fill) == APPLIED:
                applied += 1
        if applied and write_checkpoint is not None:
            # After the balance has changed and before anybody acts on it. A
            # checkpoint written on a tick that changed nothing would rewrite the
            # file at the fill stream's rate to record an account that had not
            # moved -- the level-on-every-tick defect, one layer down in the
            # filesystem.
            write_checkpoint(keeper.standing.fills_applied)
        publish_balance(keeper.read_balance())

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_paper_account(keeper),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The paper account starts at what the operator allocated and only there --
    `capital-allotment` is where that number comes from, and until it arrives the
    account has no money and every fill is refused for insufficient cash. That is
    the correct behaviour for an account nobody has funded, and it is why the
    allotment reader is started before this part.

    Mark prices come off `market-data` so unrealised profit is measured against
    what the market is doing rather than against the entry price, which would make
    every open position look flat forever.
    """
    import pathlib

    from runtime.durable_state import (
        CheckpointSchedule,
        DurableStateStore,
        restore_and_arm_checkpoint,
    )
    from runtime.input_assembly import Batch, LatestValue

    fills = Batch(read=context.bus.reader("fill"))
    allotments = LatestValue(read=context.bus.reader("capital-allotment"))
    modes = Batch(read=context.bus.reader("money-mode"))
    rates = Batch(read=context.bus.reader("paper-currency-rate"))
    publish_balance = context.bus.publisher_for("account-balance")
    segment = str(context.setting("segment_id").value)
    keeper = PaperAccountKeeper(
        segment=segment,
        # The currency comes from that segment's own capital settings, which the
        # context loaded beside the runtime scope. A currency in the runtime scope
        # would be one currency for every segment.
        currency=str(context.setting("quote_currency", scope=segment).value),
    )
    # Restored before the allotment is applied, so `set_allotment` sees the
    # starting balance this account already had and adds nothing. Without the
    # checkpoint every restart handed the strategy a fresh 10,000 while
    # fill-reconciler restored the positions that had already spent it: measured
    # on the live spine at 16:10 on 2026-08-26, eight positions open and this part
    # reporting `fills_applied` 0, `open_positions` 0 and equity exactly the
    # starting balance. Every risk cap in the segment is a fraction of that
    # number, so an account that forgets is an account whose caps are fiction.
    #
    # Beside the lot books and the resting stops, under position_state_root: it is
    # the same fact about the same positions.
    store = DurableStateStore(
        pathlib.Path(str(context.setting("position_state_root").value)).expanduser()
    )
    store.root.mkdir(parents=True, exist_ok=True)
    write_checkpoint = restore_and_arm_checkpoint(
        store,
        # Every fill, for the same reason the lot books use: a fill changes what
        # the account holds and losing one costs a position its cash.
        CheckpointSchedule(1),
        PART_ID,
        CHECKPOINT_COMPONENT,
        keeper,
        {},
    )
    funded_at = [None]

    def read_fills(_keeper):
        allotment = allotments.value()
        if allotment is not None and allotment.allotted != funded_at[0]:
            keeper.set_allotment(allotment.allotted)
            funded_at[0] = allotment.allotted
        modes.payloads()
        rates.payloads()
        return fills.payloads()

    return run_paper_account_keeper(
        keeper=keeper,
        control_socket=context.control_socket,
        read_fills=read_fills,
        publish_balance=lambda balance: publish_balance([balance]),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
        write_checkpoint=write_checkpoint,
    )
