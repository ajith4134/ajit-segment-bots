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
from runtime.segment_settings import built_segments, read_segment_setting
from runtime.part_process import run_part
from runtime.trading_types import BUY, LONG, SHORT, capital_committed_by, leverage_behind

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
# Kept as a name for readers of old standings. Since 2026-09-13 nothing returns it:
# an executed fill is applied whatever the cash, and the shortfall is counted.
REFUSED_INSUFFICIENT = "refused-insufficient-paper-balance"
APPLIED_BEYOND_CASH = "applied-beyond-the-paper-cash"


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
    # The cash this position actually took out of the account and still holds --
    # its notional over the leverage it was opened at, not its notional. Kept as
    # the number that was posted rather than recomputed from the average price,
    # because a position increased twice at two different leverages has no single
    # leverage to divide by, and the cash that left the account is a fact either
    # way. Returned in proportion as the position is closed.
    margin_posted: float = 0.0


@dataclass
class KeeperStanding:
    fills_applied: int = 0
    live_fills_refused: int = 0
    duplicates_refused: int = 0
    insufficient_refused: int = 0
    # Fills applied although the account's cash did not cover them, and by how
    # much in total. Each is an order the pre-trade guards (`fund-lock-ledger`,
    # `position-sizer`, `trade-capital-bounds-gate`) let through that they should
    # have stopped: the defect is upstream, and this is where it becomes visible.
    fills_applied_beyond_cash: int = 0
    cash_shortfall_total: float = 0.0
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

    def __init__(self, segment: str, currency: str = "INR", now_ns=time.time_ns) -> None:
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
                    "margin_posted": position.margin_posted,
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
            average_price = float(held["average_price"])
            self._positions[(venue_id, symbol)] = _PaperPosition(
                quantity,
                average_price,
                # A checkpoint written before margin was tracked was written by an
                # account that had debited the whole notional, so the unlevered
                # reading is what its cash figure is consistent with. Restoring
                # zero would hand that cash back at the next close.
                float(held.get("margin_posted", abs(quantity) * average_price)),
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
        # What this fill ties up. **The notional over the leverage it was sized
        # at**, which is the same figure `trade-capital-bounds-gate` admitted the
        # trade on: an account that debited the notional while the gate measured
        # the commitment would refuse for want of cash trades the operator's own
        # bounds had just passed, and would report an equity ten times too small
        # for every cap computed off it.
        margin = capital_committed_by(fill.quantity, fill.price, leverage_behind(fill))
        opening = held is None or held.quantity == 0 or (held.quantity > 0) == (fill.side == BUY)

        # **An executed fill is never refused here** (2026-09-13). This refused a
        # fill when the cash did not cover it -- after `paper-fill-simulator` had
        # already executed it and every other book had applied it. On 2026-09-07
        # it refused 114 such fills while `fill-reconciler`, `cost-basis-tracker`
        # and `position-close-detector` applied all of them, and refusing the buys
        # turned the later sells into shorts in this account alone: two books of
        # one portfolio that disagreed on sixteen positions. A venue refuses the
        # *order*; that refusal is `fund-lock-ledger`'s, before execution. A fill
        # the cash did not cover is applied and counted, so the upstream gap is a
        # number rather than a divergence.
        beyond_cash = opening and margin + fill.fee > self._cash
        if beyond_cash:
            self.standing.fills_applied_beyond_cash += 1
            self.standing.cash_shortfall_total += margin + fill.fee - max(self._cash, 0.0)

        self._seen_fills.add(fill.fill_id)
        self._cash -= fill.fee
        self.standing.fees_total += fill.fee

        signed = fill.signed_quantity
        if held is None:
            # Margin leaves the account on both sides. A short posts margin the
            # same way a long does -- it does not hand the account the sale
            # proceeds, which is what a spot short would do and what this did
            # until 2026-08-26: shorting *raised* free cash, and every exposure
            # cap computed as a fraction of equity grew by opening a short.
            self._positions[key] = _PaperPosition(signed, fill.price, margin)
            self._cash -= margin
        else:
            self._apply_to_position(key, held, fill, signed, margin)

        self.standing.fills_applied += 1
        if self.standing.lowest_cash is None or self._cash < self.standing.lowest_cash:
            self.standing.lowest_cash = self._cash
        return APPLIED_BEYOND_CASH if beyond_cash else APPLIED

    def _apply_to_position(self, key, held, fill, signed, margin) -> None:
        increasing = held.quantity == 0 or (held.quantity > 0) == (signed > 0)
        if increasing:
            total = abs(held.quantity) + abs(signed)
            held.average_price = (
                abs(held.quantity) * held.average_price + abs(signed) * fill.price
            ) / total
            held.quantity += signed
            held.margin_posted += margin
            self._cash -= margin
            return

        closed = min(abs(held.quantity), abs(signed))
        gain = (fill.price - held.average_price) * closed
        realised = gain if held.quantity > 0 else -gain
        # The margin the closed portion had posted, returned in the proportion it
        # is being closed in. Taken from what the position actually posted rather
        # than recomputed from its average price: the two differ by exactly the
        # leverage it opened at, and recomputing would hand back money that never
        # left the account.
        returned = held.margin_posted * (closed / abs(held.quantity))
        held.margin_posted -= returned
        self._cash += realised + returned
        self.standing.realised_total += realised

        held.quantity += signed
        if abs(signed) > closed:
            # Straight through flat and out the other side. What is left is a new
            # position the other way, posting its own margin at this fill's
            # leverage -- the old position's is already back in cash.
            held.average_price = fill.price
            held.margin_posted = capital_committed_by(
                abs(held.quantity), fill.price, leverage_behind(fill)
            )
            self._cash -= held.margin_posted
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

        # What the open positions are holding of the account's own money. The
        # margin they posted, not their notional: cash was only ever reduced by
        # the margin, so adding the notional back would report an equity that grew
        # with leverage -- and every risk cap in the segment is a fraction of it.
        committed = sum(position.margin_posted for position in self._positions.values())
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
        "fills_applied_beyond_cash": keeper.standing.fills_applied_beyond_cash,
        "cash_shortfall_total": keeper.standing.cash_shortfall_total,
        "lowest_cash": keeper.standing.lowest_cash,
    }


class SegmentPaperAccounts:
    """One paper account per segment this spine trades.

    Three segment bots run on one spine since 2026-09-05 and each has its own
    allocated balance. `AccountBalance` already named the segment it belonged to,
    so nothing downstream had to change shape; what was missing was a keeper that
    held more than one account. Sharing one account across three bots would let a
    cash-equity loss shrink the index bot's risk budget, and every risk cap in a
    segment is a fraction of its own equity.

    A fill that names no segment is applied to no account and counted, rather than
    applied to an arbitrary one: an unattributable fill is a defect upstream, and
    charging it to whichever account happened to be first would hide it.
    """

    def __init__(self, keepers: dict) -> None:
        if not keepers:
            raise ValueError(
                "a paper account keeper with no segment holds no money, and every "
                "fill would be refused for insufficient cash while the settings said "
                "the account was funded"
            )
        self.keepers = dict(keepers)
        self.fills_without_a_segment = 0
        self.fills_for_an_unknown_segment: dict[str, int] = {}
        self.allotments_for_a_segment_not_traded: dict[str, int] = {}

    def keeper_for(self, fill):
        segment = getattr(fill, "segment", "") or ""
        if not segment:
            # One account and an unattributed fill is not ambiguous: it can only
            # be that account's. Two or more and it is, so it is counted rather
            # than charged to whichever came first -- the rule
            # `runtime.input_assembly.level_for_segment` states.
            if len(self.keepers) == 1:
                return next(iter(self.keepers.values()))
            self.fills_without_a_segment += 1
            return None
        keeper = self.keepers.get(segment)
        if keeper is None:
            self.fills_for_an_unknown_segment[segment] = (
                self.fills_for_an_unknown_segment.get(segment, 0) + 1
            )
        return keeper


def fund_each_account_from_its_allotment(
    accounts: SegmentPaperAccounts, allotment_by_segment: dict, funded_at: dict,
) -> tuple[str, ...]:
    """Set each segment's starting balance from its own allotment, once.

    Named rather than left inside the part's tick closure so it can be tested:
    the defect it prevents is silent, because an account nobody funded refuses
    every fill for insufficient cash, which looks exactly like a strategy that
    found no trades. Returns the segments funded by this call.
    """
    funded = []
    for segment, allotment in allotment_by_segment.items():
        keeper = accounts.keepers.get(segment)
        if keeper is None:
            # An allotment for a segment this spine does not trade. Not an error:
            # the reader publishes what the operator listed, and this part keeps
            # accounts for what the spine actually runs.
            accounts.allotments_for_a_segment_not_traded[segment] = (
                accounts.allotments_for_a_segment_not_traded.get(segment, 0) + 1
            )
            continue
        if allotment.allotted != funded_at.get(segment):
            keeper.set_allotment(allotment.allotted)
            funded_at[segment] = allotment.allotted
            funded.append(segment)
    return tuple(funded)


def describe_segment_paper_accounts(accounts: SegmentPaperAccounts) -> dict:
    return {
        "part_id": PART_ID,
        "segments": sorted(accounts.keepers),
        # Both are defects upstream, not here, and both are silent without a
        # counter: a fill charged to no account leaves the equity behind the
        # positions the position keeper is holding.
        "fills_without_a_segment": accounts.fills_without_a_segment,
        "fills_for_an_unknown_segment": dict(
            sorted(accounts.fills_for_an_unknown_segment.items())
        ),
        "allotments_for_a_segment_not_traded": dict(
            sorted(accounts.allotments_for_a_segment_not_traded.items())
        ),
        # The one number that says whether each bot has money at all. An account
        # nobody funded refuses every fill for insufficient cash, which reads
        # exactly like a strategy that found no trades.
        "starting_balance_by_segment": {
            segment: keeper.read_balance().starting_balance
            for segment, keeper in sorted(accounts.keepers.items())
        },
        # Flat per-segment maps, because a standing reaches the heartbeat table
        # one level deep and `by_segment` below is two: a fill the cash did not
        # cover is the upstream guard failing, and must be on the board.
        "fills_applied_beyond_cash_by_segment": {
            segment: keeper.standing.fills_applied_beyond_cash
            for segment, keeper in sorted(accounts.keepers.items())
        },
        "cash_shortfall_total_by_segment": {
            segment: keeper.standing.cash_shortfall_total
            for segment, keeper in sorted(accounts.keepers.items())
        },
        "by_segment": {
            segment: describe_paper_account(keeper)
            for segment, keeper in sorted(accounts.keepers.items())
        },
    }


def run_paper_account_keeper(
    keeper: SegmentPaperAccounts, control_socket, read_fills, publish_balance,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
    write_checkpoint=None,
) -> int:
    def tick() -> None:
        applied: dict = {}
        for fill in read_fills(keeper):
            one = keeper.keeper_for(fill)
            if one is None:
                continue
            if one.apply_fill(fill) in (APPLIED, APPLIED_BEYOND_CASH):
                applied[one._segment] = applied.get(one._segment, 0) + 1
        for segment in applied:
            if write_checkpoint is not None:
                # After the balance has changed and before anybody acts on it, and
                # only for the account that moved. A checkpoint written on a tick
                # that changed nothing would rewrite the file at the fill stream's
                # rate to record an account that had not moved.
                write_checkpoint(segment, keeper.keepers[segment].standing.fills_applied)
        publish_balance([one.read_balance() for one in keeper.keepers.values()])

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_segment_paper_accounts(keeper),
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
    from runtime.input_assembly import Batch, LatestByKey

    fills = Batch(read=context.bus.reader("fill"))
    # One allotment per segment (2026-09-05). A LatestValue here would fund every
    # account from whichever segment's allotment arrived last, and every risk cap
    # in a segment is a fraction of its own equity. Age-bounded, because an
    # allotment that stopped being restated must stop funding an account rather
    # than standing forever (2026-08-26).
    allotments = LatestByKey(
        read=context.bus.reader("capital-allotment"),
        key_of=lambda allotment: allotment.segment,
        maximum_age_seconds=context.number("capital_bounds_maximum_age_seconds"),
    )
    modes = Batch(read=context.bus.reader("money-mode"))
    rates = Batch(read=context.bus.reader("paper-currency-rate"))
    publish_balance = context.bus.publisher_for("account-balance")

    # The currency comes from each segment's own capital settings, read from the
    # segment's file rather than through the context's loaded scope: the context
    # loads one segment's scope (the spine's `segment_id`) and this part now keeps
    # an account for every segment the spine trades.
    accounts = SegmentPaperAccounts(
        keepers={
            segment: PaperAccountKeeper(
                segment=segment,
                currency=str(
                    read_segment_setting(
                        segment, "quote_currency", context.settings_root
                    ).value
                ),
            )
            for segment in built_segments(context)
        }
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
    # One component per segment, because they are three separate accounts and one
    # file holding whichever wrote last would be worse than none. The unsuffixed
    # component this part wrote until 2026-09-05 is deliberately not migrated: it
    # was read on the day of the change and held cash 0.0, no positions and 0
    # fills applied, so there is nothing in it to carry forward -- index-options
    # had never completed a paper trade.
    writers = {
        segment: restore_and_arm_checkpoint(
            store,
            # Every fill, for the same reason the lot books use: a fill changes
            # what the account holds and losing one costs a position its cash.
            CheckpointSchedule(1),
            PART_ID,
            f"{CHECKPOINT_COMPONENT}-{segment}",
            keeper,
            {},
        )
        for segment, keeper in accounts.keepers.items()
    }

    def write_checkpoint(segment: str, observations: int) -> None:
        writers[segment](observations)

    def record_the_funding(segment: str) -> None:
        """Write this account down the moment it is funded.

        Not through the fill-counted writer: that one is due every fill, and
        funding happens at zero fills -- `CheckpointSchedule.is_due` compares
        against the count at the last write, so the arm-time write at zero makes
        the funding write not due. The result was a file saying `starting` 0.0
        for an account funded minutes earlier, and every board reading it showing
        a bot with no capital (Rule 8).
        """
        store.save(
            PART_ID,
            f"{CHECKPOINT_COMPONENT}-{segment}",
            accounts.keepers[segment].read_checkpoint_state(),
            {},
        )

    funded_at: dict = {}

    def read_fills(_accounts):
        # Funding is a change to the account, so it is checkpointed like one.
        # Without this the file said `starting` 0.0 for an account that had been
        # funded minutes earlier -- the checkpoint was written once at arm time,
        # before the allotment arrived, and again only on a fill. Every board
        # reading it (Rule 8: a display shows measured state) would have shown
        # three bots with no capital while all three were funded and trading.
        for segment in fund_each_account_from_its_allotment(
            accounts, allotments.mapping(), funded_at
        ):
            record_the_funding(segment)
        modes.payloads()
        rates.payloads()
        return fills.payloads()

    return run_paper_account_keeper(
        keeper=accounts,
        control_socket=context.control_socket,
        read_fills=read_fills,
        publish_balance=publish_balance,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
        write_checkpoint=write_checkpoint,
    )
