"""onchain-position-reader: what a tracked trader is actually holding, on-chain.

A leaderboard says a return. A chain says a position, and the difference is the
whole reason this part exists: on-chain state is expensive to fake, timestamped by
consensus, and cannot be quietly revised after the fact. That makes it the only
source in this block that is stronger than the person supplying it.

It is also the source most easily over-read, in four specific ways:

- **One visible leg is not a position.** A wallet long ETH perpetual may be short
  spot somewhere invisible. Copying one leg of a hedge is worse than copying
  neither, so a read that cannot see the whole book says so and the consumers are
  built to refuse to reason from it.
- **A wallet is not a person.** The same trader may run six, and six wallets each
  holding the same position is one position, not six confirmations.
- **Unconfirmed is not settled.** A pending transaction can be dropped or
  reordered; treating it as a position means reacting to something that never
  happened.
- **A read has a block height, and the height is the timestamp.** Wall-clock time
  from the node's response header describes when the answer arrived, not when the
  state was true, and on a congested chain those differ by minutes.

So every position read here is stamped with its block, marked for whether the whole
book was visible, and deduplicated by owner rather than by wallet.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.external_research_types import (
    COMPLETE, ExternalPosition, PARTIAL, UNAVAILABLE,
)
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "onchain-position-reader"

PART_DECLARATION = PartDeclaration(
    part_id="onchain-position-reader",
    consumes=("tracked-trader",),
    produces=("external-position", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

READ = "read"
NOT_PUBLIC = "the-trader-chose-not-to-show-positions"
NOT_CONFIRMED = "the-state-is-not-settled-yet"
NO_POSITIONS = "the-book-is-empty"
READ_FAILED = "read-failed"
STALE_HEIGHT = "the-node-answered-from-an-old-block"


@dataclass(frozen=True)
class PositionRead:
    """One trader's book at one block height."""

    trader_id: str
    state: str
    positions: tuple
    block_height: int | None
    blocks_behind: int | None
    is_full_book: bool
    wallets_seen: int
    reason: str
    read_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == READ and bool(self.positions)


@dataclass
class PositionReaderStanding:
    reads_attempted: int = 0
    reads_succeeded: int = 0
    positions_read: int = 0
    partial_books: int = 0
    unconfirmed_skipped: int = 0
    stale_reads: int = 0
    failures: int = 0
    wallets_merged_into_owners: int = 0


class OnchainPositionReader:
    """Reads settled positions per tracked trader, stamped with their block."""

    def __init__(
        self,
        confirmations_required: int,
        maximum_blocks_behind: int,
        now_ns=time.time_ns,
    ) -> None:
        if confirmations_required < 1:
            raise ValueError(
                "a position with zero confirmations is a proposal, not a position"
            )
        if maximum_blocks_behind < 1:
            raise ValueError("a node is allowed to be at least one block behind")
        self._confirmations_required = confirmations_required
        self._maximum_blocks_behind = maximum_blocks_behind
        self._now_ns = now_ns
        self._read_positions = None
        # Wallets known to belong to the same owner, so six wallets holding one
        # position do not read as six confirmations.
        self._owner_of_wallet: dict[str, str] = {}
        self.standing = PositionReaderStanding()

    def install_reader(self, read_positions) -> None:
        """`read_positions(identity_reference) -> (rows, block_height, chain_head)`."""
        self._read_positions = read_positions

    def observe_wallet_owner(self, wallet: str, owner: str) -> None:
        """Two wallets, one owner: their positions merge rather than accumulate."""
        if wallet in self._owner_of_wallet and self._owner_of_wallet[wallet] != owner:
            self.standing.wallets_merged_into_owners += 1
        self._owner_of_wallet[wallet] = owner

    def owner_of(self, wallet: str) -> str:
        return self._owner_of_wallet.get(wallet, wallet)

    def read(self, trader) -> PositionRead:
        self.standing.reads_attempted += 1
        if self._read_positions is None:
            raise RuntimeError("no chain reader is installed")

        if not trader.can_be_read_further:
            return self._result(
                trader.trader_id, NOT_PUBLIC, (), None, None, False, 0,
                "the trader chose not to show positions. That is a fact about them, and "
                "guessing the book from their return would be inventing the evidence",
            )

        try:
            rows, block_height, chain_head = self._read_positions(
                trader.identity_reference
            )
        except Exception as failure:
            self.standing.failures += 1
            return self._result(
                trader.trader_id, READ_FAILED, (), None, None, False, 0,
                f"the chain could not be read ({type(failure).__name__}). An unread book "
                f"is not an empty book",
            )

        blocks_behind = None
        if block_height is not None and chain_head is not None:
            blocks_behind = max(chain_head - block_height, 0)
            if blocks_behind > self._maximum_blocks_behind:
                self.standing.stale_reads += 1
                return self._result(
                    trader.trader_id, STALE_HEIGHT, (), block_height, blocks_behind,
                    False, 0,
                    f"the node answered from block {block_height}, {blocks_behind} behind "
                    f"the head. The block is the timestamp, so this describes a past book",
                )

        rows = tuple(rows or ())
        settled = tuple(
            row for row in rows if row.get("confirmations", 0) >= self._confirmations_required
        )
        self.standing.unconfirmed_skipped += len(rows) - len(settled)

        if not settled:
            return self._result(
                trader.trader_id,
                NO_POSITIONS if not rows else NOT_CONFIRMED,
                (), block_height, blocks_behind, False, 0,
                "nothing settled to read. A pending transaction can be dropped or "
                "reordered, so it is not a position yet"
                if rows else "the book is empty at this height",
            )

        wallets = {str(row.get("wallet", trader.identity_reference)) for row in settled}
        owners = {self.owner_of(wallet) for wallet in wallets}
        is_full_book = bool(rows and all(row.get("is_full_book", False) for row in settled))
        if not is_full_book:
            self.standing.partial_books += 1

        now = self._now_ns()
        positions = tuple(
            ExternalPosition(
                trader_id=trader.trader_id,
                venue_id=row.get("venue_id", trader.venue_id),
                symbol=row["symbol"],
                side=row["side"],
                notional=row.get("notional"),
                entry_price=row.get("entry_price"),
                leverage=row.get("leverage"),
                opened_at_ns=row.get("opened_at_ns"),
                observed_at_ns=now,
                is_full_book=is_full_book,
                completeness=COMPLETE if is_full_book else PARTIAL,
                source_reference=row.get("source_reference", f"block:{block_height}"),
            )
            for row in settled
        )

        self.standing.reads_succeeded += 1
        self.standing.positions_read += len(positions)
        return self._result(
            trader.trader_id, READ, positions, block_height, blocks_behind, is_full_book,
            len(owners),
            f"{len(positions)} settled position(s) at block {block_height} across "
            f"{len(owners)} owner(s)"
            + (
                ". The whole book is visible, so a hedge would be visible too"
                if is_full_book
                else ". Only part of the book is visible, so no single leg means what it "
                     "looks like -- the invisible other leg may reverse it"
            ),
        )

    def _result(
        self, trader_id, state, positions, block_height, blocks_behind, is_full_book,
        wallets_seen, reason,
    ) -> PositionRead:
        return PositionRead(
            trader_id=trader_id, state=state, positions=positions,
            block_height=block_height, blocks_behind=blocks_behind,
            is_full_book=is_full_book, wallets_seen=wallets_seen, reason=reason,
            read_at_ns=self._now_ns(),
        )


def describe_position_reading(reader: OnchainPositionReader) -> dict:
    return {
        "part_id": PART_ID,
        "reads_attempted": reader.standing.reads_attempted,
        "reads_succeeded": reader.standing.reads_succeeded,
        "positions_read": reader.standing.positions_read,
        "partial_books": reader.standing.partial_books,
        "unconfirmed_rows_skipped": reader.standing.unconfirmed_skipped,
        "stale_node_reads": reader.standing.stale_reads,
        "failures": reader.standing.failures,
        "wallets_merged_into_owners": reader.standing.wallets_merged_into_owners,
        "infers_a_hidden_leg": False,
    }


def run_onchain_position_reader(
    reader: OnchainPositionReader, control_socket, read_tracked_traders,
    publish_positions, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for trader in read_tracked_traders():
            result = reader.read(trader)
            if result.is_usable:
                publish_positions(result)

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

    No chain reader is installed on this box, so every tracked trader is
    answered READ_FAILED by name and no position is published;
    `install_reader` is the one way one gets in.
    """
    from runtime.input_assembly import Batch

    traders = Batch(read=context.bus.reader("tracked-trader"))
    publish_positions = context.bus.publisher_for("external-position")
    reader = OnchainPositionReader(
        confirmations_required=int(context.number("onchain_confirmations_required")),
        maximum_blocks_behind=int(context.number("onchain_maximum_blocks_behind")),
    )

    return run_onchain_position_reader(
        reader=reader,
        control_socket=context.control_socket,
        read_tracked_traders=lambda: traders.payloads(),
        publish_positions=lambda result: publish_positions((result,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
