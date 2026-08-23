"""funding-settlement-recorder: each perpetual funding payment as its own event.

Funding is not fill PnL and must never be booked as it. A perpetual holder pays
or receives funding every eight hours purely for holding, regardless of whether
the position moved -- so a strategy that is profitable on price and loses to
funding is a completely different problem from one with no edge, and a single
netted number cannot tell them apart. RL-028's USDT statement keeps them separate
for exactly this reason, and this is the part that supplies the funding half.

**Idempotent by construction.** A funding payment has a natural identity: the
venue, the symbol, and the settlement time. Booking one twice would silently
double a cost that recurs three times a day for as long as a position is held,
and nothing downstream could distinguish the doubled figure from a real one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from runtime.journal import Journal, JournalEntry
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import LONG

PART_ID = "funding-settlement-recorder"

PART_DECLARATION = PartDeclaration(
    part_id="funding-settlement-recorder",
    consumes=("position", "market-data"),
    produces=("funding-settlement", "journal-entry", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

FUNDING_SETTLEMENT = "funding-settlement"


@dataclass(frozen=True)
class FundingSettlement:
    """One funding payment, signed from this position's point of view.

    Negative is paid, positive is received. A long pays when the rate is
    positive; a short receives it. Getting that sign wrong inverts the cost of
    every carry trade the system will ever evaluate.
    """

    venue_id: str
    symbol: str
    settlement_id: str
    funding_rate: float
    position_quantity: float
    mark_price: float
    amount_quote: float
    direction: str
    settled_at_ns: int


@dataclass
class FundingStanding:
    settlements_booked: int = 0
    duplicates_ignored: int = 0
    without_position: int = 0
    total_paid: float = 0.0
    total_received: float = 0.0
    by_symbol: dict = field(default_factory=dict)
    # Positions held while no funding source was on any declared input. The
    # blueprint gives this part position and market-data, and market-data
    # carries trades, candles and books -- never a funding settlement. Counted
    # so the gap is a number on the board, not a silence (RL-062).
    positions_held_without_a_funding_source: int = 0


class FundingSettlementRecorder:
    """Books each funding payment once, against the position that actually held."""

    def __init__(self, journal: Journal) -> None:
        self._journal = journal
        self._positions: dict[tuple[str, str], object] = {}
        self._booked: set[str] = set()
        self.standing = FundingStanding()

    def observe_position(self, position) -> None:
        key = (position.venue_id, position.symbol)
        if position.is_flat:
            self._positions.pop(key, None)
        else:
            self._positions[key] = position

    def settlement_id(self, venue_id: str, symbol: str, settled_at_ns: int) -> str:
        """The natural identity of one funding payment.

        Derived rather than supplied, so two callers reporting the same
        settlement cannot give it two identities and book it twice.
        """
        return f"{venue_id}:{symbol}:{settled_at_ns}"

    def record_funding(
        self, venue_id: str, symbol: str, funding_rate: float, mark_price: float, settled_at_ns: int
    ) -> FundingSettlement | None:
        """Book one funding payment, or ignore it as already booked.

        Returns None when there was no position to charge -- funding is only owed
        by a holder, and inventing a settlement for a symbol nothing was holding
        would put a cost against a strategy that never took the risk.
        """
        identity = self.settlement_id(venue_id, symbol, settled_at_ns)
        if identity in self._booked:
            self.standing.duplicates_ignored += 1
            return None

        position = self._positions.get((venue_id, symbol))
        if position is None or position.is_flat:
            self.standing.without_position += 1
            return None

        # A long with a positive rate pays; a short with a positive rate
        # receives; a negative rate reverses both. Signing the amount from the
        # position's point of view keeps every downstream sum a plain addition.
        #
        # The rate's own sign carries the reversal, so there is no second branch
        # for a negative rate -- one was written here and inverted three of the
        # four cases before its test caught it.
        amount = abs(position.quantity) * mark_price * funding_rate
        amount = -amount if position.direction == LONG else amount

        self._booked.add(identity)
        self.standing.settlements_booked += 1
        if amount < 0:
            self.standing.total_paid += -amount
        else:
            self.standing.total_received += amount
        self.standing.by_symbol[symbol] = self.standing.by_symbol.get(symbol, 0.0) + amount

        settlement = FundingSettlement(
            venue_id=venue_id,
            symbol=symbol,
            settlement_id=identity,
            funding_rate=funding_rate,
            position_quantity=position.quantity,
            mark_price=mark_price,
            amount_quote=amount,
            direction=position.direction,
            settled_at_ns=settled_at_ns,
        )
        self._journal.append(
            kind=FUNDING_SETTLEMENT,
            part_id=PART_ID,
            payload={
                "settlement_id": identity,
                "venue_id": venue_id,
                "symbol": symbol,
                "funding_rate": funding_rate,
                "position_quantity": position.quantity,
                "direction": position.direction,
                "mark_price": mark_price,
                "amount_quote": amount,
                "settled_at_ns": settled_at_ns,
            },
        )
        return settlement

    @property
    def net_funding(self) -> float:
        """Received less paid, across everything booked."""
        return self.standing.total_received - self.standing.total_paid


def describe_funding(recorder: FundingSettlementRecorder) -> dict:
    return {
        "part_id": PART_ID,
        "settlements_booked": recorder.standing.settlements_booked,
        "duplicates_ignored": recorder.standing.duplicates_ignored,
        "settlements_without_position": recorder.standing.without_position,
        "total_paid": recorder.standing.total_paid,
        "total_received": recorder.standing.total_received,
        "net_funding": recorder.net_funding,
        "by_symbol": dict(recorder.standing.by_symbol),
    }


def run_funding_settlement_recorder(
    recorder: FundingSettlementRecorder, control_socket, read_positions_and_funding, publish_settlements,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        positions, funding_events = read_positions_and_funding()
        for position in positions:
            recorder.observe_position(position)
        settlements = [
            settlement
            for venue_id, symbol, rate, mark, at_ns in funding_events
            if (settlement := recorder.record_funding(venue_id, symbol, rate, mark, at_ns))
        ]
        publish_settlements(tuple(settlements))

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

    Positions are observed so a settlement, when one arrives, is charged to
    the holder. No settlement arrives: the blueprint gives this part position
    and market-data, and market-data carries trades, candles and books, never
    a funding payment. The positions held meanwhile are counted on the
    standing as held without a funding source (RL-062); the fix is a
    blueprint edit that declares where a settlement comes from, not a rate
    read from a type this part does not consume.
    """
    from runtime.input_assembly import Batch
    import pathlib as _pathlib

    from runtime.journal import Journal, journal_path_for, read_journal_tail

    journal_path = journal_path_for(
        _pathlib.Path(str(context.setting("journal_path").value)).expanduser(), PART_ID
    )
    journal_path.parent.mkdir(parents=True, exist_ok=True)

    def append_line(line: str) -> None:
        with open(journal_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()

    journal = Journal(append_line=append_line, continues_from=read_journal_tail(journal_path))

    positions = Batch(read=context.bus.reader("position"))
    market_data = Batch(read=context.bus.reader("market-data"))
    publish_settlements = context.bus.publisher_for("funding-settlement")
    context.bus.publisher_for("journal-entry")
    recorder = FundingSettlementRecorder(journal=journal)

    def read_positions_and_funding():
        seen = tuple(positions.payloads())
        market_data.payloads()
        recorder.standing.positions_held_without_a_funding_source += sum(
            1 for position in seen if not position.is_flat
        )
        return seen, ()

    def publish(settlements) -> None:
        if settlements:
            publish_settlements(settlements)

    return run_funding_settlement_recorder(
        recorder=recorder,
        control_socket=context.control_socket,
        read_positions_and_funding=read_positions_and_funding,
        publish_settlements=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
