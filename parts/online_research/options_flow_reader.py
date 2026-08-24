"""options-flow-reader: where large option risk was placed, kept as positioning.

The standard way this data is used -- a put/call ratio -- destroys the only useful
content in it. A ratio collapses strike, expiry and direction into one number, and
that number then gets read as directional conviction, which it is not: buying puts
is as often a hedge on a long book as a bet on a fall. A hedger and a speculator
produce the same tick.

So this part records positioning, not sentiment: which strike, which expiry, which
side, how much premium, and whether it was a block. It refuses to compute a ratio,
and refuses to say what the flow means.

The four properties it insists on, each because dropping it is a specific mistake:

- **Strike relative to spot decides everything.** A far out-of-the-money call in a
  week is a lottery ticket; the same notional at-the-money is a position. Recording
  contracts without moneyness makes those identical.
- **Time to expiry is not a footnote.** The same trade at 3 days and 90 days
  expresses different views, and averaging across tenors averages incompatible
  things.
- **Buy and sell are not symmetric.** Somebody selling calls has an obligation, not
  an opinion, and it usually sits against stock or perpetual they already hold.
- **A block is different from the same size in pieces.** A block trade is one
  decision by one participant; a hundred small orders may be a hundred, and the
  distinction is available in the feed and routinely discarded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.external_research_types import OptionsFlow
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "options-flow-reader"

PART_DECLARATION = PartDeclaration(
    part_id="options-flow-reader",
    consumes=("symbol-universe",),
    produces=("options-flow", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

CALL = "call"
PUT = "put"
BUY = "buy"
SELL = "sell"

RECORDED = "recorded"
TOO_SMALL = "below-the-premium-that-would-matter"
ALREADY_SEEN = "already-recorded"
EXPIRED = "the-expiry-has-already-passed"
NO_SPOT = "no-spot-price-to-place-the-strike-against"
UNDERLYING_NOT_TRADED = "the-underlying-is-not-in-the-universe"

# Where a strike sits relative to spot. This is the classification that a
# contracts-only reading throws away.
DEEP_IN_THE_MONEY = "deep-in-the-money"
IN_THE_MONEY = "in-the-money"
AT_THE_MONEY = "at-the-money"
OUT_OF_THE_MONEY = "out-of-the-money"
FAR_OUT_OF_THE_MONEY = "far-out-of-the-money"


@dataclass(frozen=True)
class FlowRead:
    trade_id: str
    state: str
    flow: OptionsFlow | None
    moneyness: str | None
    days_to_expiry: float | None
    is_an_obligation: bool
    reason: str
    read_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == RECORDED and self.flow is not None


@dataclass
class FlowReaderStanding:
    rows_seen: int = 0
    recorded: int = 0
    too_small: int = 0
    duplicates: int = 0
    expired: int = 0
    without_a_spot_price: int = 0
    outside_the_universe: int = 0
    blocks: int = 0
    obligations_written: int = 0


class OptionsFlowReader:
    """Records large option trades with strike, tenor, side and block status intact."""

    def __init__(
        self,
        minimum_premium: float,
        at_the_money_band: float,
        far_out_band: float,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_premium <= 0:
            raise ValueError(
                "a premium floor of zero records every retail lottery ticket as flow"
            )
        if not 0.0 < at_the_money_band < far_out_band:
            raise ValueError(
                "the at-the-money band must be narrower than the far-out band, in "
                "fractions of spot"
            )
        self._minimum_premium = minimum_premium
        self._at_the_money_band = at_the_money_band
        self._far_out_band = far_out_band
        self._now_ns = now_ns
        self._spot: dict[str, float] = {}
        self._universe: set = set()
        self._seen: set = set()
        self.standing = FlowReaderStanding()

    def observe_universe(self, underlyings) -> None:
        self._universe = set(underlyings)

    def observe_spot(self, underlying: str, price: float) -> None:
        self._spot[underlying] = price

    def moneyness_of(self, underlying: str, strike: float, option_kind: str) -> str | None:
        spot = self._spot.get(underlying)
        if spot is None or spot <= 0:
            return None
        distance = (strike - spot) / spot
        # For a put the same absolute distance sits on the other side of spot.
        if option_kind == PUT:
            distance = -distance
        if abs(distance) <= self._at_the_money_band:
            return AT_THE_MONEY
        if distance > self._far_out_band:
            return FAR_OUT_OF_THE_MONEY
        if distance > 0:
            return OUT_OF_THE_MONEY
        if distance < -self._far_out_band:
            return DEEP_IN_THE_MONEY
        return IN_THE_MONEY

    def read(self, row) -> FlowRead:
        self.standing.rows_seen += 1
        trade_id = str(row["trade_id"])

        if trade_id in self._seen:
            self.standing.duplicates += 1
            return self._read(
                trade_id, ALREADY_SEEN, None, None, None, False,
                "already recorded. One trade replayed is one trade",
            )

        underlying = row["underlying"]
        if self._universe and underlying not in self._universe:
            self.standing.outside_the_universe += 1
            return self._read(
                trade_id, UNDERLYING_NOT_TRADED, None, None, None, False,
                f"{underlying} is not in the declared universe. Positioning in something "
                f"this system cannot trade is reading, not information",
            )

        now = self._now_ns()
        expiry_ns = int(row["expiry_ns"])
        if expiry_ns <= now:
            self.standing.expired += 1
            self._seen.add(trade_id)
            return self._read(
                trade_id, EXPIRED, None, None, 0.0, False,
                "the expiry has already passed, so this describes risk that no longer exists",
            )

        premium = row.get("premium")
        if premium is not None and premium < self._minimum_premium:
            self.standing.too_small += 1
            self._seen.add(trade_id)
            return self._read(
                trade_id, TOO_SMALL, None, None, None, False,
                f"{premium:,.0f} premium, below the {self._minimum_premium:,.0f} floor",
            )

        option_kind = row["option_kind"]
        strike = float(row["strike"])
        moneyness = self.moneyness_of(underlying, strike, option_kind)
        if moneyness is None:
            self.standing.without_a_spot_price += 1

        side = row["side"]
        is_obligation = side == SELL
        if is_obligation:
            self.standing.obligations_written += 1

        flow = OptionsFlow(
            symbol=row["symbol"],
            underlying=underlying,
            expiry_ns=expiry_ns,
            strike=strike,
            option_kind=option_kind,
            side=side,
            contracts=float(row["contracts"]),
            premium=premium,
            implied_volatility=row.get("implied_volatility"),
            is_block=bool(row.get("is_block", False)),
            observed_at_ns=now,
            source_reference=row.get("source_reference", f"trade:{trade_id}"),
        )
        self._seen.add(trade_id)
        self.standing.recorded += 1
        if flow.is_block:
            self.standing.blocks += 1

        return self._read(
            trade_id, RECORDED, flow, moneyness, flow.days_to_expiry, is_obligation,
            f"{side} {flow.contracts:,.0f} {option_kind} at {strike:,.2f}"
            + (f" ({moneyness})" if moneyness else " (no spot price to place it against)")
            + f", {flow.days_to_expiry:.1f} day(s) to expiry"
            + (", block" if flow.is_block else ", not a block")
            + (
                ". A sold option is an obligation, not an opinion, and usually sits "
                "against something already held"
                if is_obligation
                else ""
            ),
        )

    def _read(
        self, trade_id, state, flow, moneyness, days, is_obligation, reason,
    ) -> FlowRead:
        return FlowRead(
            trade_id=trade_id, state=state, flow=flow, moneyness=moneyness,
            days_to_expiry=days, is_an_obligation=is_obligation, reason=reason,
            read_at_ns=self._now_ns(),
        )


def describe_options_flow_reading(reader: OptionsFlowReader) -> dict:
    return {
        "part_id": PART_ID,
        "rows_seen": reader.standing.rows_seen,
        "recorded": reader.standing.recorded,
        "below_premium_floor": reader.standing.too_small,
        "duplicates": reader.standing.duplicates,
        "already_expired": reader.standing.expired,
        "without_a_spot_price": reader.standing.without_a_spot_price,
        "outside_the_universe": reader.standing.outside_the_universe,
        "blocks": reader.standing.blocks,
        "obligations_written": reader.standing.obligations_written,
        "computes_a_put_call_ratio": False,
        "says_what_the_flow_means": False,
    }


def run_options_flow_reader(
    reader: OptionsFlowReader, control_socket, read_rows, publish_flow,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for row in read_rows():
            result = reader.read(row)
            if result.is_usable:
                publish_flow(result.flow)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_options_flow_reading(reader),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The universe is observed so a strike can be placed against an
    underlying this system trades. No options flow feed is connected on
    this box -- it is on no input this part declares -- so no row arrives
    and nothing is published.
    """
    from runtime.input_assembly import Batch

    universe = Batch(read=context.bus.reader("symbol-universe"))
    publish_flow = context.bus.publisher_for("options-flow")
    settlement = str(context.setting("settlement_currency").value)
    reader = OptionsFlowReader(
        minimum_premium=context.number("options_minimum_premium"),
        at_the_money_band=context.number("options_at_the_money_band"),
        far_out_band=context.number("options_far_out_band"),
    )

    def underlying_of(symbol: str) -> str:
        return symbol[: -len(settlement)] if settlement and symbol.endswith(settlement) and len(symbol) > len(settlement) else symbol

    def read_rows():
        for selection in universe.payloads():
            entries = selection if isinstance(selection, (tuple, list)) else (selection,)
            reader.observe_universe(tuple(sorted({underlying_of(entry.symbol) for entry in entries})))
        return ()

    return run_options_flow_reader(
        reader=reader,
        control_socket=context.control_socket,
        read_rows=read_rows,
        publish_flow=lambda flow: publish_flow((flow,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
