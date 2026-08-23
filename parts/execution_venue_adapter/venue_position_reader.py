"""venue-position-reader: what the venue says is held.

The counterpart to `fill-reconciler`, and deliberately independent of it. That
part builds a position from the fills it saw; this one asks the venue. Neither is
allowed to correct the other here -- reconciliation is a separate part, and a
reader that quietly adopted the venue's number would destroy the only evidence
that a fill was missed.

The distinction that matters most is between **flat** and **unknown**. A venue
reporting no position for a symbol means flat; a venue that could not be reached
means nothing at all. Collapsing the two would let a failed request read as a
closed position, and a system that believes it is flat when it is not will happily
open the same position again.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import FLAT, LONG, SHORT

PART_ID = "venue-position-reader"

PART_DECLARATION = PartDeclaration(
    part_id="venue-position-reader",
    consumes=("key-standing",),
    produces=("venue-position-report", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

REPORTED = "reported"
UNREACHABLE = "unreachable"
NO_KEY = "no-key"


@dataclass(frozen=True)
class VenuePositionReport:
    """What one venue says about one symbol, or that it could not be asked."""

    venue_id: str
    symbol: str
    state: str
    quantity: float | None
    entry_price: float | None
    liquidation_price: float | None
    unrealised_pnl: float | None
    leverage: float | None
    reason: str
    read_at_ns: int

    @property
    def direction(self) -> str | None:
        if self.quantity is None:
            return None
        if self.quantity > 0:
            return LONG
        if self.quantity < 0:
            return SHORT
        return FLAT

    @property
    def is_known(self) -> bool:
        return self.state == REPORTED


@dataclass
class PositionReaderStanding:
    reads: int = 0
    failures: int = 0
    refused_no_key: int = 0
    symbols_reported: int = 0
    open_positions_seen: int = 0
    last_failure: str | None = None


class VenuePositionReader:
    """Fetches the venue's own view of what is held, without reconciling it."""

    def __init__(self, clients: dict[str, object], read_key_standing, now_ns=time.time_ns) -> None:
        self._clients = clients
        self._read_key_standing = read_key_standing
        self._now_ns = now_ns
        self.standing = PositionReaderStanding()

    def read(self, venue_id: str, symbols: tuple[str, ...] | None = None) -> tuple[VenuePositionReport, ...]:
        """Every position this venue reports, or one unreachable report per symbol."""
        if self._read_key_standing(venue_id) is None:
            self.standing.refused_no_key += 1
            return self._unknown(venue_id, symbols, NO_KEY, "no key is available for this venue")

        client = self._clients.get(venue_id)
        if client is None:
            return self._unknown(venue_id, symbols, UNREACHABLE, f"no client is configured for {venue_id}")

        self.standing.reads += 1
        try:
            raw = client.fetch_positions(symbols) if symbols else client.fetch_positions()
        except Exception as failure:
            self.standing.failures += 1
            self.standing.last_failure = f"{venue_id}: {type(failure).__name__}: {failure}"
            return self._unknown(
                venue_id, symbols, UNREACHABLE,
                f"{type(failure).__name__}: {failure}; unknown is not flat",
            )

        reports = []
        for entry in raw:
            quantity = self._signed_quantity(entry)
            if quantity:
                self.standing.open_positions_seen += 1
            reports.append(
                VenuePositionReport(
                    venue_id=venue_id,
                    symbol=entry.get("symbol", ""),
                    state=REPORTED,
                    quantity=quantity,
                    entry_price=_as_float(entry.get("entryPrice")),
                    liquidation_price=_as_float(entry.get("liquidationPrice")),
                    unrealised_pnl=_as_float(entry.get("unrealizedPnl")),
                    leverage=_as_float(entry.get("leverage")),
                    reason="as the venue reported it",
                    read_at_ns=self._now_ns(),
                )
            )
        self.standing.symbols_reported = len(reports)
        return tuple(reports)

    def _signed_quantity(self, entry: dict) -> float:
        """The venue's size, signed by its side.

        Venues report a positive size and a separate side rather than a signed
        quantity, and reading the size alone turns every short into a long.
        """
        size = _as_float(entry.get("contracts")) or _as_float(entry.get("contractSize")) or 0.0
        side = str(entry.get("side") or "").lower()
        return -abs(size) if side == "short" else abs(size)

    def _unknown(self, venue_id, symbols, state, reason) -> tuple[VenuePositionReport, ...]:
        return tuple(
            VenuePositionReport(
                venue_id=venue_id, symbol=symbol, state=state, quantity=None,
                entry_price=None, liquidation_price=None, unrealised_pnl=None,
                leverage=None, reason=reason, read_at_ns=self._now_ns(),
            )
            for symbol in (symbols or ("",))
        )


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def describe_positions(reader: VenuePositionReader) -> dict:
    return {
        "part_id": PART_ID,
        "reads": reader.standing.reads,
        "failures": reader.standing.failures,
        "refused_no_key": reader.standing.refused_no_key,
        "symbols_reported": reader.standing.symbols_reported,
        "open_positions_seen": reader.standing.open_positions_seen,
        "last_failure": reader.standing.last_failure,
    }


def run_venue_position_reader(
    reader: VenuePositionReader, control_socket, read_venues, publish_reports,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        reports = []
        for venue_id, symbols in read_venues():
            reports.extend(reader.read(venue_id, symbols))
        publish_reports(tuple(reports))

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

    No venue client is held in phase 1; a venue named by a key standing is
    read and refused for no key, and nothing is invented about positions.
    """
    from runtime.input_assembly import LatestByKey

    keys = LatestByKey(read=context.bus.reader("key-standing"), key_of=lambda k: (k.venue_id, k.key_id))
    publish_reports = context.bus.publisher_for("venue-position-report")
    reader = VenuePositionReader(
        clients={},
        read_key_standing=lambda venue_id: next(
            (s for (v, _k), s in keys.mapping().items() if v == venue_id and s.state == "serving"), None
        ),
    )

    def read_venues():
        return tuple((venue_id, None) for (venue_id, _k) in keys.mapping())

    def publish(reports) -> None:
        if reports:
            publish_reports(reports)

    return run_venue_position_reader(
        reader=reader,
        control_socket=context.control_socket,
        read_venues=read_venues,
        publish_reports=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
