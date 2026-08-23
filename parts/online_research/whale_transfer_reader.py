"""whale-transfer-reader: large on-chain movements, read for the one thing they mean.

Almost everything said publicly about whale transfers is narration. A large
movement is not bullish or bearish; it is a change in where coins are, and only one
consequence of that is real: **coins sitting at an exchange deposit address can be
sold there, and coins that are not cannot.** Everything else -- who owns the wallet,
what they intend, whether this is accumulation -- is a story attached afterwards.

So this part reads transfers and classifies endpoints, not motives. Four things it
deliberately gets right, because each is a way the naive version misleads:

- **Internal movements are not flow.** An exchange rotating between its own hot and
  cold wallets produces enormous transfers that change nothing about supply. They
  are the largest single source of false whale alerts and are classified out.
- **Direction is asymmetric in meaning.** An inflow creates the *option* to sell;
  an outflow removes it. Those are not mirror images, and treating them as +1/-1 of
  the same signal overstates the outflow case.
- **A transfer is not a trade.** Coins can sit at a deposit address for months.
  This part records that the option to sell was created, never that selling
  happened.
- **Confirmation depth decides existence.** A reorg-able transfer has not happened.
  Below the required depth nothing is emitted, rather than emitted with a caveat.

Size is measured in quote value where a price is available, and in units where it is
not -- because 400 units means different things for different assets, and a reader
that mixes the two ranks by asset rather than by size.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.external_research_types import WhaleTransfer
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "whale-transfer-reader"

PART_DECLARATION = PartDeclaration(
    part_id="whale-transfer-reader",
    consumes=(),
    produces=("whale-transfer", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

# What an endpoint is. This is the whole classification, and it is deliberately
# small: a category this part cannot verify is a category it will get wrong.
EXCHANGE_DEPOSIT = "exchange-deposit"
EXCHANGE_WITHDRAWAL = "exchange-withdrawal"
EXCHANGE_INTERNAL = "exchange-internal"
CUSTODY = "custody"
BRIDGE = "bridge"
CONTRACT = "contract"
UNKNOWN_WALLET = "unknown"

ENDPOINT_KINDS = (
    EXCHANGE_DEPOSIT, EXCHANGE_WITHDRAWAL, EXCHANGE_INTERNAL, CUSTODY, BRIDGE,
    CONTRACT, UNKNOWN_WALLET,
)

READ = "read"
NOT_LARGE_ENOUGH = "below-the-size-that-would-matter"
NOT_CONFIRMED = "not-settled-deep-enough-to-have-happened"
INTERNAL_ROTATION = "an-exchange-moving-its-own-coins"
ALREADY_SEEN = "already-recorded"
READ_FAILED = "read-failed"


@dataclass(frozen=True)
class TransferRead:
    transfer_id: str
    state: str
    transfer: WhaleTransfer | None
    reason: str
    read_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == READ and self.transfer is not None


@dataclass
class TransferReaderStanding:
    rows_seen: int = 0
    transfers_recorded: int = 0
    below_size: int = 0
    unconfirmed: int = 0
    internal_rotations: int = 0
    duplicates: int = 0
    failures: int = 0
    could_become_supply: int = 0
    left_the_market: int = 0
    priced_in_quote: int = 0
    unpriced: int = 0


class WhaleTransferReader:
    """Records large settled transfers and what their endpoints allow."""

    def __init__(
        self,
        minimum_quote_value: float,
        confirmations_required: int,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_quote_value <= 0:
            raise ValueError(
                "a size floor of zero makes every dust transaction a whale alert"
            )
        if confirmations_required < 1:
            raise ValueError("a reorg-able transfer has not happened yet")
        self._minimum_quote_value = minimum_quote_value
        self._confirmations_required = confirmations_required
        self._now_ns = now_ns
        self._known_addresses: dict[str, tuple] = {}
        self._seen: set = set()
        self._prices: dict[str, float] = {}
        self.standing = TransferReaderStanding()

    def observe_address(self, address: str, kind: str, venue_id: str | None = None) -> None:
        """What an address is. Unlabelled addresses stay unknown, not guessed."""
        if kind not in ENDPOINT_KINDS:
            raise ValueError(
                f"{kind!r} is not an endpoint kind this part can verify. A category it "
                f"cannot verify is a category it will get wrong"
            )
        self._known_addresses[address] = (kind, venue_id)

    def observe_price(self, asset: str, quote_price: float) -> None:
        self._prices[asset] = quote_price

    def kind_of(self, address: str) -> str:
        return self._known_addresses.get(address, (UNKNOWN_WALLET, None))[0]

    def read(self, row) -> TransferRead:
        self.standing.rows_seen += 1
        transfer_id = str(row["transfer_id"])

        if transfer_id in self._seen:
            self.standing.duplicates += 1
            return self._read(
                transfer_id, ALREADY_SEEN, None,
                "already recorded. The same transfer arriving twice is one movement, and "
                "counting it twice doubles an apparent flow",
            )

        if row.get("confirmations", 0) < self._confirmations_required:
            self.standing.unconfirmed += 1
            return self._read(
                transfer_id, NOT_CONFIRMED, None,
                f"{row.get('confirmations', 0)} confirmation(s), below "
                f"{self._confirmations_required}. A transfer that can still be reorganised "
                f"out has not happened",
            )

        from_kind, _ = self._known_addresses.get(
            row["from_address"], (UNKNOWN_WALLET, None)
        )
        to_kind, to_venue = self._known_addresses.get(
            row["to_address"], (UNKNOWN_WALLET, None)
        )

        # An exchange rotating its own coins is the largest source of false whale
        # alerts anywhere. It changes nothing about what can be sold.
        if EXCHANGE_INTERNAL in (from_kind, to_kind) or (
            from_kind == EXCHANGE_WITHDRAWAL and to_kind == EXCHANGE_DEPOSIT
            and row.get("same_venue", False)
        ):
            self.standing.internal_rotations += 1
            self._seen.add(transfer_id)
            return self._read(
                transfer_id, INTERNAL_ROTATION, None,
                "an exchange moving its own coins between wallets. Nothing about what can "
                "be sold has changed",
            )

        quantity = float(row["quantity"])
        price = self._prices.get(row["asset"])
        quote_value = quantity * price if price is not None else None
        if quote_value is None:
            self.standing.unpriced += 1
        else:
            self.standing.priced_in_quote += 1

        # Unpriced rows cannot be size-filtered without ranking by asset instead of
        # by size, so they are kept and marked rather than silently admitted or dropped.
        if quote_value is not None and quote_value < self._minimum_quote_value:
            self.standing.below_size += 1
            self._seen.add(transfer_id)
            return self._read(
                transfer_id, NOT_LARGE_ENOUGH, None,
                f"{quote_value:,.0f} quote, below the {self._minimum_quote_value:,.0f} "
                f"floor. Size is measured in quote value because units mean different "
                f"things for different assets",
            )

        transfer = WhaleTransfer(
            transfer_id=transfer_id,
            chain=row["chain"],
            asset=row["asset"],
            quantity=quantity,
            quote_value=quote_value,
            from_kind=from_kind,
            to_kind=to_kind,
            to_venue_id=to_venue,
            confirmed_at_ns=int(row["confirmed_at_ns"]),
            observed_at_ns=self._now_ns(),
            source_reference=row.get("source_reference", f"tx:{transfer_id}"),
        )
        self._seen.add(transfer_id)
        self.standing.transfers_recorded += 1
        if transfer.could_become_supply:
            self.standing.could_become_supply += 1
        if transfer.leaves_the_market:
            self.standing.left_the_market += 1

        return self._read(
            transfer_id, READ, transfer,
            f"{quantity:,.4f} {transfer.asset} from {from_kind} to {to_kind}"
            + (
                ". These coins can now be sold at that venue -- the option to sell was "
                "created, which is not the same as selling"
                if transfer.could_become_supply
                else ". These coins can no longer be sold where they were"
                if transfer.leaves_the_market
                else ". Neither endpoint changes what can be sold, so this is movement "
                     "without a consequence this part will claim"
            ),
        )

    def _read(self, transfer_id, state, transfer, reason) -> TransferRead:
        return TransferRead(
            transfer_id=transfer_id, state=state, transfer=transfer, reason=reason,
            read_at_ns=self._now_ns(),
        )


def describe_transfer_reading(reader: WhaleTransferReader) -> dict:
    return {
        "part_id": PART_ID,
        "rows_seen": reader.standing.rows_seen,
        "transfers_recorded": reader.standing.transfers_recorded,
        "below_size_floor": reader.standing.below_size,
        "unconfirmed": reader.standing.unconfirmed,
        "exchange_internal_rotations_excluded": reader.standing.internal_rotations,
        "duplicates": reader.standing.duplicates,
        "created_the_option_to_sell": reader.standing.could_become_supply,
        "removed_the_option_to_sell": reader.standing.left_the_market,
        "priced_in_quote": reader.standing.priced_in_quote,
        "unpriced": reader.standing.unpriced,
        "infers_intent": False,
        "claims_a_transfer_is_a_trade": False,
    }


def run_whale_transfer_reader(
    reader: WhaleTransferReader, control_socket, read_rows, publish_transfers,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for row in read_rows():
            result = reader.read(row)
            if result.is_usable:
                publish_transfers(result.transfer)

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

    This part consumes nothing: chain transfers reach it through no
    declared input, so no row is read and nothing is published. It ticks
    on its health interval and reports that state.
    """
    publish_transfers = context.bus.publisher_for("whale-transfer")
    reader = WhaleTransferReader(
        minimum_quote_value=context.number("whale_minimum_quote_value"),
        confirmations_required=int(context.number("onchain_confirmations_required")),
    )

    return run_whale_transfer_reader(
        reader=reader,
        control_socket=context.control_socket,
        read_rows=lambda: (),
        publish_transfers=lambda transfer: publish_transfers((transfer,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
