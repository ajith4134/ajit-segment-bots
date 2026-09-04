#!/usr/bin/env python3
"""Trade a captured session through the real parts, and report what closed.

Phase A asks for "paper trading on historic data first" (docs/goal.md), and
until 2026-09-04 there was no way to do it: the only options open-and-close
proof this project had was
`tests/integration/test_an_options_position_opens_and_closes.py`, whose own
docstring says its premium walk is "a stated, documented sequence rather than a
captured print series ... This test should move to a captured run the moment one
exists." One exists now -- 6,644 Upstox streams for 2026-09-04, carrying trades,
option greeks, the book and candles per contract.

**This is not the live spine and must never be mistaken for it (RL-071).** A
tape replay is never what a live run's decisions or learning are made from, so
this writes to its own state root, reads nothing the spine wrote, and every
trade it produces is stamped `replay` with the day it replayed. The dashboard
renders those separately for the same reason the board stopped showing the
crypto era's closed trades as this segment's: a board that answers a question
about live trading with replayed rows is worse than an empty one.

What is real here, and what is not:

- **Real**: every price is a captured Upstox print, in the order and at the
  times it printed. The fills, the fees (Upstox's own charge stack on both
  legs), the lot book, the excursion tracking and the close detection are the
  same parts the live spine runs, not reimplementations.
- **Stated**: where the entry sits and how far the stop and target sit from it.
  The live path gets those from `stop-target-placer`, which needs a volatility
  forecast or excursion history that a cold replay does not have. They are
  derived here from the contract's own captured move over the session and the
  provenance is printed with the results, so nothing reads as measured that was
  not.
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from parts.paper_live_trading.paper_fill_simulator import FILLED, PaperFillSimulator
from parts.paper_live_trading.stop_order_manager import (
    PLACE_NEW, PLACE_TARGET, StopOrderManager, as_order_request,
)
from parts.portfolio_state.cost_basis_tracker import CostBasisTracker
from parts.portfolio_state.fill_reconciler import FillReconciler
from parts.portfolio_state.peak_excursion_tracker import PeakExcursionTracker
from parts.portfolio_state.position_close_detector import PositionCloseDetector
from parts.risk_capital_allocation.exit_order_chainer import ExitOrderChainer
from runtime.market_conditions import MarketSessionState, SessionKind
from runtime.settings_reader import load_settings_document, settings_directory
from runtime.tape import read_tape_index
from runtime.trading_types import BUY, LONG, MARKET, SELL

# Where a replay's own results live. Deliberately not under the live spine's
# state root: nothing here may be read by a part, and nothing a part wrote may
# be read by this.
REPLAY_ROOT = pathlib.Path.home() / ".local/share/ajit-segment-bots/replay"
TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
VENUE = "upstox"


def number(document, name: str) -> float:
    """One setting's value, by the same name the live part reads it under.

    Read from the operator's own settings rather than restated here, so a replay
    charges exactly what the live path charges. `read_value` refuses a name
    nobody declared, which is what stops a replay inventing a fee.
    """
    return float(document.read_value(name))


def prints_of(directory: pathlib.Path, day: str) -> list[tuple[int, float]]:
    """Every captured trade print for one instrument, in the order it printed."""
    index_path = directory / f"{day}.index"
    if not index_path.exists():
        return []
    records = read_tape_index(index_path)
    prints: list[tuple[int, float]] = []
    with open(directory / f"{day}.blob", "rb") as blob:
        for record in records:
            blob.seek(int(record["blob_offset"]))
            try:
                payload = json.loads(blob.read(int(record["blob_length"])))
            except ValueError:
                continue
            price = payload.get("price") or payload.get("last_traded_price")
            if price:
                prints.append((int(record["venue_time_ns"] or record["received_at_ns"]),
                               float(price)))
    return prints


# The only instrument master on this machine is the slice captured for the
# universe-bridge tests. It covers NIFTY, so most replayed contracts resolve to
# the name they trade under and the rest keep the venue's instrument key. A key
# shown as though it were a trading symbol would be a board naming something
# that does not exist.
CAPTURED_MASTER = (
    pathlib.Path(__file__).resolve().parents[1]
    / "tests/captured/upstox/2026-09-04-nse-instrument-master-nifty-slice.json"
)


def trading_symbols() -> dict[str, str]:
    """instrument_key -> the name it trades under, for what the master covers."""
    try:
        rows = json.loads(CAPTURED_MASTER.read_text())
    except (OSError, ValueError):
        return {}
    return {
        row["instrument_key"]: row["trading_symbol"]
        for row in rows
        if row.get("instrument_key") and row.get("trading_symbol")
    }


def busiest_option_contracts(day: str, wanted: int) -> list[tuple[str, list]]:
    """The contracts that actually traded, most prints first.

    Chosen by how much they printed rather than by name: a contract picked by
    hand is a contract picked because it worked.
    """
    scored = []
    for directory in sorted(TAPE.glob("NSE_FO|*")):
        prints = prints_of(directory, day)
        if len(prints) < 200:
            continue
        scored.append((directory.name, prints))
    scored.sort(key=lambda entry: -len(entry[1]))
    return scored[:wanted]


class ReplayChain:
    """The six parts that turn a fill into a closed trade, wired as the live
    spine wires them. Mirrors the integration test's own chain deliberately --
    this is a proof about captured data, not a second implementation."""

    def __init__(self, settings, quantity_increment: float, replayed_day: str) -> None:
        self.book = PaperFillSimulator(
            taker_fee_rate=number(settings, "taker_fee_rate"),
            maker_fee_rate=number(settings, "maker_fee_rate"),
            options_flat_brokerage=number(settings, "options_flat_brokerage"),
            options_stt_sell_rate=number(settings, "options_stt_sell_rate"),
            options_exchange_transaction_charge_rate=number(
                settings, "options_exchange_transaction_charge_rate"),
            options_ipft_charge_rate=number(settings, "options_ipft_charge_rate"),
            options_stamp_duty_buy_rate=number(settings, "options_stamp_duty_buy_rate"),
            options_gst_rate=number(settings, "options_gst_rate"),
        )
        self.book.observe_session(
            MarketSessionState(
                segment="NSE_FO",
                kind=SessionKind.OPEN,
                as_of_date=datetime.date.fromisoformat(replayed_day),
                # The session is stated as open because the captured prints are
                # themselves the evidence it was: a print exists only because the
                # market traded. Same reasoning paper-fill-simulator already
                # applies to a historical bar close, and the date it is stated
                # for is the day being replayed, never today.
                reason=f"replaying the captured session of {replayed_day}",
                observed_at_ns=0,
            )
        )
        self.reconciler = FillReconciler(quantity_tolerance=quantity_increment)
        self.cost_basis = CostBasisTracker(quantity_increment)
        self.excursions = PeakExcursionTracker()
        self.chainer = ExitOrderChainer()
        self.stops = StopOrderManager()
        self.closes = PositionCloseDetector(quantity_increment)
        self.closed_trades = []
        self.exit_orders_sent = []
        self.positions_held: dict = {}

    def apply_fill(self, fill) -> None:
        position = self.reconciler.observe_fill(fill)
        self.cost_basis.observe_fill(fill)
        self.excursions.observe_position(position)
        key = (fill.venue_id, fill.symbol)
        was_held = self.positions_held.get(key, 0.0)
        self.positions_held[key] = position.quantity

        trade = self.closes.observe_fill(fill)
        if trade is not None:
            self.closed_trades.append(trade)

        exits = self.chainer.observe_entry_fill(
            fill_id=fill.fill_id,
            entry_order_id=fill.order_id or fill.fill_id,
            venue_id=fill.venue_id,
            symbol=fill.symbol,
            entry_side=fill.side,
            filled_quantity=fill.quantity,
        )
        if exits is not None and exits.should_be_sent:
            self.send_exits(exits)

        if was_held and position.is_flat:
            for cancel in self.stops.observe_position_closed(*key):
                self.exit_orders_sent.append(cancel)
                self.book.simulate(**as_paper_order(as_order_request(cancel)))

    def send_exits(self, exits) -> None:
        direction = LONG if exits.exit_side == SELL else "short"
        for action in (
            self.stops.apply_adjustment(
                venue_id=exits.venue_id, symbol=exits.symbol, direction=direction,
                quantity=exits.quantity, stop_price=exits.stop_price, money_mode=PaperMode(),
            ),
            self.stops.place_target(
                venue_id=exits.venue_id, symbol=exits.symbol, direction=direction,
                quantity=exits.quantity, target_price=exits.target_price, money_mode=PaperMode(),
            ),
        ):
            if not action.is_actionable:
                continue
            self.exit_orders_sent.append(action)
            self.book.simulate(**as_paper_order(as_order_request(action)))

    def observe_price(self, symbol: str, price: float, at_ns: int) -> None:
        self.excursions.observe_price(VENUE, symbol, price, observed_at_ns=at_ns)
        for result in self.book.evaluate_resting({(VENUE, symbol): price}):
            if result.did_fill:
                excursion = self.excursions.read(VENUE, symbol)
                if excursion is not None:
                    self.closes.observe_excursion(
                        VENUE, symbol, excursion.best_unrealised, excursion.worst_unrealised,
                    )
                self.apply_fill(result.fill)


class PaperMode:
    mode = "paper"


def as_paper_order(request) -> dict:
    return {
        "client_order_id": request.client_order_id,
        "venue_id": request.venue_id,
        "symbol": request.symbol,
        "side": request.side,
        "quantity": request.quantity,
        "order_type": request.order_type,
        "limit_price": request.limit_price or None,
        "money_mode": "paper",
        "is_in_flight": False,
        "fill_price_estimate": None,
        "market_price": None,
        "stop_price": request.trigger_price,
        "cancels_client_order_id": request.cancels_client_order_id,
    }


def stop_and_target_for(prints: list[tuple[int, float]], entry: float,
                        stop_multiple: float, target_multiple: float) -> tuple[float, float]:
    """Where the exits sit, as multiples of this contract's own typical move.

    The live path takes these from `stop-target-placer`, which needs a
    volatility forecast or excursion history that a cold replay does not have.
    Derived instead from the contract's own captured session so the distance is
    a property of the instrument being traded rather than a number chosen to
    make the replay close -- an option whose premium swings 8% a minute and one
    that barely moves must not be given the same stop.

    The typical move is the median absolute change between consecutive prints,
    not the mean: a single fat print would drag a mean and quietly widen every
    exit that followed it.
    """
    moves = [abs(later - earlier) for (_, earlier), (_, later) in zip(prints, prints[1:])]
    moves = [move for move in moves if move > 0]
    if not moves:
        raise ValueError("this contract never changed price; there is nothing to trade against")
    typical = statistics.median(moves)
    return entry - stop_multiple * typical, entry + target_multiple * typical


def replay_one_contract(name: str, prints: list, settings, lot_size: float,
                        stop_multiple: float, target_multiple: float, day: str) -> dict:
    """Open at the first print, rest the exits, then walk every later print."""
    chain = ReplayChain(settings, quantity_increment=lot_size, replayed_day=day)
    entry_at_ns, entry_price = prints[0]
    stop_price, target_price = stop_and_target_for(
        prints, entry_price, stop_multiple, target_multiple)

    chain.chainer.register_plan(VENUE, name, BUY, stop_price, target_price)
    opened = chain.book.simulate(
        client_order_id=f"replay-{name}", venue_id=VENUE, symbol=name, side=BUY,
        quantity=lot_size, order_type=MARKET, limit_price=None, money_mode="paper",
        is_in_flight=False, fill_price_estimate=None, market_price=entry_price,
        stop_price=None,
    )
    if opened.outcome != FILLED:
        return {"symbol": name, "opened": False, "why": opened.reason}
    chain.apply_fill(opened.fill)

    walked = 0
    for at_ns, price in prints[1:]:
        chain.observe_price(name, price, at_ns)
        walked += 1
        if chain.closed_trades:
            break

    if not chain.closed_trades:
        return {
            "symbol": name, "opened": True, "closed": False, "prints_walked": walked,
            "entry_price": entry_price, "stop_price": stop_price,
            "target_price": target_price,
            "why": "neither the stop nor the target was reached in the captured session",
        }

    closed = chain.closed_trades[0]
    return {
        "symbol": name,
        "opened": True,
        "closed": True,
        "prints_walked": walked,
        "quantity": closed.quantity,
        "entry_price": closed.entry_price,
        "exit_price": closed.exit_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "fees_paid": closed.fees_paid,
        "realised_pnl": closed.realised_pnl,
        "net_pnl": closed.realised_pnl - closed.fees_paid,
        "direction": closed.direction,
        "opened_at_ns": entry_at_ns,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", default=time.strftime("%Y-%m-%d"),
                        help="the captured day to replay, YYYY-MM-DD")
    parser.add_argument("--contracts", type=int, default=12,
                        help="how many of the busiest contracts to replay")
    parser.add_argument("--lot-size", type=float, default=75.0,
                        help="quantity per trade; one NIFTY lot by default")
    parser.add_argument("--stop-multiple", type=float, default=8.0,
                        help="stop distance, in multiples of the contract's own typical move")
    parser.add_argument("--target-multiple", type=float, default=12.0,
                        help="target distance, in the same units")
    arguments = parser.parse_args()

    settings = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    contracts = busiest_option_contracts(arguments.day, arguments.contracts)
    if not contracts:
        print(f"no contract on {arguments.day} printed enough to replay -- nothing was traded.")
        return 0

    named = trading_symbols()
    results = []
    for name, prints in contracts:
        try:
            replayed = replay_one_contract(
                name, prints, settings, arguments.lot_size,
                arguments.stop_multiple, arguments.target_multiple, arguments.day)
            replayed["instrument_key"] = name
            replayed["trading_symbol"] = named.get(name)
            replayed["name_is_the_venues_key"] = name not in named
            results.append(replayed)
        except ValueError as refused:
            results.append({"symbol": name, "opened": False, "why": str(refused)})

    closed = [r for r in results if r.get("closed")]
    opened = [r for r in results if r.get("opened")]

    print(f"Replayed {len(contracts)} contracts from the captured tape for {arguments.day}.")
    print(f"  opened {len(opened)}   closed {len(closed)}\n")
    if closed:
        header = (f"{'contract':<28}{'entry':>9}{'exit':>9}{'qty':>7}"
                  f"{'fees':>10}{'net':>12}{'prints':>8}")
        print(header)
        print("-" * len(header))
        for trade in closed:
            shown = trade.get("trading_symbol") or trade["symbol"]
            print(f"{shown:<28}{trade['entry_price']:>9.2f}{trade['exit_price']:>9.2f}"
                  f"{trade['quantity']:>7.0f}{trade['fees_paid']:>10.2f}"
                  f"{trade['net_pnl']:>12.2f}{trade['prints_walked']:>8}")
        print("-" * len(header))
        won = sum(1 for t in closed if t["net_pnl"] > 0)
        print(f"{'':<28}{'':>9}{'':>9}{'':>7}"
              f"{sum(t['fees_paid'] for t in closed):>10.2f}"
              f"{sum(t['net_pnl'] for t in closed):>12.2f}")
        print(f"\n{won} of {len(closed)} closed in profit, net of Upstox's real charge stack.")
    for trade in results:
        if not trade.get("closed"):
            print(f"  {trade['symbol']:<28} did not close: {trade.get('why')}")

    REPLAY_ROOT.mkdir(parents=True, exist_ok=True)
    written = REPLAY_ROOT / f"{arguments.day}.closed-trades.json"
    written.write_text(json.dumps({
        "replayed_day": arguments.day,
        "is_a_replay": True,
        "source": "captured Upstox tape",
        "stop_multiple_of_typical_move": arguments.stop_multiple,
        "target_multiple_of_typical_move": arguments.target_multiple,
        "lot_size": arguments.lot_size,
        "replayed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "trades": results,
    }, indent=2) + "\n")
    print(f"\nwrote {written}")
    print("Stamped `is_a_replay` and kept out of the spine's state root: these are not "
          "live trades and no board may show them as though they were (RL-071, Rule 8).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
