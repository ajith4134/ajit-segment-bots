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
import collections
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
from runtime.trading_types import BUY, LONG, MARKET, OPTION, SELL, SPOT

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


# The real instrument master, fetched from Upstox's own published asset rather
# than the NIFTY-shaped test slice beside the tests. The slice covers 196 rows
# and no `lot_size` at all, so it can name a NIFTY contract and cannot size one,
# and it knows nothing of the stock-options or cash-equity universes -- which is
# to say it cannot answer the question this replay is being asked.
#
# Kept out of the repository (Rule 9: large re-fetchable media stays out) and
# refreshed by hand, because an instrument master is a fact about a trading day
# and a stale one silently prices the wrong contract.
REAL_MASTER = (
    pathlib.Path.home()
    / ".local/share/ajit-segment-bots/instrument-master/complete.json.gz"
)

# Upstox's own instrument_type spellings, mapped to the vocabulary the segment
# files are written in. Not a guess: `segment_instrument_types` holds "option"
# and "spot", and `segment_that_trades` compares against those.
KIND_OF_INSTRUMENT_TYPE = {"CE": OPTION, "PE": OPTION, "EQ": SPOT}


def instruments_by_key() -> dict:
    """Every instrument the master knows, keyed by the key the tape files use."""
    import gzip

    if not REAL_MASTER.exists():
        raise SystemExit(
            f"no instrument master at {REAL_MASTER}. Fetch it first:\n"
            f"  mkdir -p {REAL_MASTER.parent} && curl -sSL -o {REAL_MASTER} "
            f"https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz\n"
            f"Without it a replay cannot say which segment owns a contract, and "
            f"guessing from the tape's own key format would be this project "
            f"deciding what the broker already publishes."
        )
    rows = json.load(gzip.open(REAL_MASTER))
    return {row["instrument_key"]: row for row in rows if row.get("instrument_key")}


class SettingsContext:
    """The `.setting(name)` shape `runtime/segment_settings` reads machine scope
    through, over the operator's real runtime.toml.

    A replay must ask the *same* functions the live spine asks -- which segment
    claims an instrument is decided by `segment_that_trades` in both, so a
    replay that reimplemented the claim could agree with itself and disagree
    with the bot.
    """

    def __init__(self) -> None:
        self._document = load_settings_document(
            settings_directory() / "runtime.toml", "runtime"
        )

    def setting(self, name: str):
        entry = self._document.entries.get(name)
        if entry is None:
            raise KeyError(name)
        return entry


def cash_equity_eligible_symbols(master: dict) -> frozenset[str]:
    """Every ordinary NSE share no derivative in the real master is written on.

    The same exclusion `broker-symbol-universe-bridge` applies live
    (`EquityWithoutADerivative`, reused rather than restated -- T-6), replayed
    against this day's real instrument master. `segment_that_trades` asks this
    for cash-equity-intraday's membership because that segment's
    `segment_underlying_trading_symbols` is a 14-name legacy fallback,
    disconnected from the derived universe the bridge actually publishes
    (2026-09-05) -- passing the static list here would reproduce the same gap
    a replay exists to catch, not verify past it.
    """
    import types

    from parts.market_data_feed.broker_symbol_universe_bridge import EquityWithoutADerivative

    admits = EquityWithoutADerivative().admits
    derivative_underlying_keys = {
        row["underlying_key"] for row in master.values() if row.get("underlying_key")
    }
    return frozenset(
        row["trading_symbol"]
        for key, row in master.items()
        if key not in derivative_underlying_keys and admits(types.SimpleNamespace(**row))
    )


def contracts_for_each_segment(day: str, per_segment: int, minimum_prints: int) -> dict:
    """The busiest instruments each built segment actually owns, that day.

    Every tape directory is resolved through the real master to (kind,
    underlying), and the segment is then asked of `segment_that_trades` -- the
    live spine's own classifier, not a second copy of the rule. An instrument no
    segment claims is counted and dropped, which is the honest outcome for a
    commodity or currency contract the feed captured and no bot here trades.
    """
    from runtime.segment_settings import built_segments, segment_that_trades

    context = SettingsContext()
    master = instruments_by_key()
    derived_membership = {"cash-equity-intraday": cash_equity_eligible_symbols(master)}
    wanted = {segment: [] for segment in built_segments(context)}
    skipped = collections.Counter()

    for directory in TAPE.iterdir():
        if not (directory / f"{day}.index").exists():
            continue
        row = master.get(directory.name)
        if row is None:
            skipped["not in the instrument master"] += 1
            continue
        kind = KIND_OF_INSTRUMENT_TYPE.get(row.get("instrument_type"))
        if kind is None:
            skipped[f"instrument_type {row.get('instrument_type')}"] += 1
            continue
        underlying = row.get("underlying_symbol") or row.get("trading_symbol")
        try:
            segment = segment_that_trades(
                kind, underlying, context, derived_membership=derived_membership
            )
        except Exception as refusal:
            skipped[f"claim refused: {refusal}"] += 1
            continue
        if segment is None:
            skipped[f"no built segment trades {kind} on {underlying}"] += 1
            continue
        wanted[segment].append((directory, row))

    chosen = {}
    for segment, candidates in wanted.items():
        scored = []
        for directory, row in candidates:
            prints = prints_of(directory, day)
            if len(prints) < minimum_prints:
                continue
            scored.append((directory.name, row, prints))
        scored.sort(key=lambda entry: -len(entry[2]))
        chosen[segment] = scored[:per_segment]
    return chosen, skipped


def size_for(segment_settings, price: float, lot_size: float) -> tuple[float, str]:
    """How many units one trade takes, under this segment's own capital bounds.

    Whole lots, because a venue does not fill a third of one. The bounds are the
    segment's own `minimum_capital_per_trade` and `maximum_capital_per_trade` --
    the same two numbers `trade-capital-bounds-gate` refuses against live, read
    from the same files, so a replay cannot size a trade the live path would
    have refused.
    """
    minimum = float(segment_settings.read_value("minimum_capital_per_trade"))
    maximum = float(segment_settings.read_value("maximum_capital_per_trade"))
    cost_of_one_lot = price * lot_size
    if cost_of_one_lot > maximum:
        return 0.0, (
            f"one lot costs {cost_of_one_lot:,.2f}, above this segment's "
            f"maximum_capital_per_trade of {maximum:,.2f}"
        )
    lots = int(maximum // cost_of_one_lot)
    committed = lots * cost_of_one_lot
    if committed < minimum:
        return 0.0, (
            f"{lots} whole lot(s) commit {committed:,.2f}, below this segment's "
            f"minimum_capital_per_trade of {minimum:,.2f}"
        )
    return lots * lot_size, ""


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

    def __init__(self, settings, quantity_increment: float, replayed_day: str,
                 segment: str = "", kind: str = OPTION) -> None:
        # Which of Upstox's two charge stacks this segment's fills pay. Passed
        # the same way the live runner passes it, so a replayed cash-equity
        # trade is charged 0.025% STT on turnover rather than the options
        # stack's 0.1% on premium -- the defect this replay would otherwise
        # have reported as a real net.
        self.segment = segment
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
            equity_intraday_rates={
                "flat_brokerage": number(settings, "equity_intraday_flat_brokerage"),
                "brokerage_rate": number(settings, "equity_intraday_brokerage_rate"),
                "stt_sell_rate": number(settings, "equity_intraday_stt_sell_rate"),
                "exchange_transaction_charge_rate": number(
                    settings, "equity_intraday_exchange_transaction_charge_rate"),
                "ipft_charge_rate": number(settings, "equity_intraday_ipft_charge_rate"),
                "stamp_duty_buy_rate": number(
                    settings, "equity_intraday_stamp_duty_buy_rate"),
                "sebi_charge_rate": number(settings, "equity_intraday_sebi_charge_rate"),
                "gst_rate": number(settings, "equity_intraday_gst_rate"),
            },
            kinds_by_segment={segment: kind} if segment else None,
        )
        self.book.observe_session(
            MarketSessionState(
                segment="NSE_EQ" if kind == SPOT else "NSE_FO",
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
                self.book.simulate(segment=self.segment,
                                   **as_paper_order(as_order_request(cancel)))

    def send_exits(self, exits) -> None:
        # The closing leg pays the same stack as the opening one. Without the
        # segment on these orders the exit would fall through to the options
        # stack and a cash-equity round trip would be charged two different
        # fee models -- which is worse than being charged one wrong one,
        # because the error would not be a constant.
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
            self.book.simulate(segment=self.segment, **as_paper_order(as_order_request(action)))

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
                        stop_multiple: float, target_multiple: float, day: str,
                        segment: str = "", kind: str = OPTION,
                        quantity: float | None = None) -> dict:
    """Open at the first print, rest the exits, then walk every later print."""
    chain = ReplayChain(settings, quantity_increment=lot_size, replayed_day=day,
                        segment=segment, kind=kind)
    entry_at_ns, entry_price = prints[0]
    traded_quantity = lot_size if quantity is None else quantity
    stop_price, target_price = stop_and_target_for(
        prints, entry_price, stop_multiple, target_multiple)

    chain.chainer.register_plan(VENUE, name, BUY, stop_price, target_price)
    opened = chain.book.simulate(
        client_order_id=f"replay-{name}", venue_id=VENUE, symbol=name, side=BUY,
        quantity=traded_quantity, order_type=MARKET, limit_price=None, money_mode="paper",
        is_in_flight=False, fill_price_estimate=None, market_price=entry_price,
        stop_price=None, segment=segment,
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
    # Which of Upstox's two charge stacks actually priced these fills, carried
    # out with the result rather than trusted. A cash-equity trade priced by the
    # options stack still produces a plausible net -- twice the real cost on a
    # MARUTI round trip, measured 2026-09-05 -- so "the fees are right" is a
    # claim that has to be shown, not assumed from the kind being passed in.
    standing = chain.book.standing
    return {
        "priced_as_options": standing.fills_priced_as_options,
        "priced_as_equity": standing.fills_priced_as_equity,
        "priced_by_the_fallback_stack": standing.fills_priced_by_the_fallback_stack,
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
    parser.add_argument("--per-segment", type=int, default=8,
                        help="how many of each segment's busiest instruments to replay")
    parser.add_argument("--minimum-prints", type=int, default=200,
                        help="how many prints an instrument needs before it is worth replaying")
    parser.add_argument("--stop-multiple", type=float, default=8.0,
                        help="stop distance, in multiples of the instrument's own typical move")
    parser.add_argument("--target-multiple", type=float, default=12.0,
                        help="target distance, in the same units")
    arguments = parser.parse_args()

    settings = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    by_segment, skipped = contracts_for_each_segment(
        arguments.day, arguments.per_segment, arguments.minimum_prints)

    print(f"Replaying the captured tape for {arguments.day}, one bot per segment.")
    print("Which segment owns an instrument is asked of segment_that_trades -- the "
          "live spine's own classifier, not a copy of the rule.\n")

    results: dict[str, list] = {}
    for segment, instruments in by_segment.items():
        segment_settings = load_settings_document(
            settings_directory() / "segments" / f"{segment}.toml", segment)
        kind = KIND_OF_INSTRUMENT_TYPE.get(
            (instruments[0][1].get("instrument_type") if instruments else None), OPTION)
        rows = []
        for name, row, prints in instruments:
            lot_size = float(row.get("lot_size") or 1)
            entry_price = prints[0][1]
            quantity, refused = size_for(segment_settings, entry_price, lot_size)
            if quantity <= 0:
                rows.append({"symbol": name, "trading_symbol": row.get("trading_symbol"),
                             "opened": False, "why": refused})
                continue
            try:
                replayed = replay_one_contract(
                    name, prints, settings, lot_size,
                    arguments.stop_multiple, arguments.target_multiple, arguments.day,
                    segment=segment,
                    kind=KIND_OF_INSTRUMENT_TYPE.get(row.get("instrument_type"), OPTION),
                    quantity=quantity)
            except ValueError as refusal:
                replayed = {"symbol": name, "opened": False, "why": str(refusal)}
            replayed["instrument_key"] = name
            replayed["trading_symbol"] = row.get("trading_symbol")
            replayed["lot_size"] = lot_size
            replayed["quantity_traded"] = quantity
            replayed["capital_committed"] = quantity * entry_price
            rows.append(replayed)
        results[segment] = rows

    header = (f"{'instrument':<30}{'qty':>8}{'entry':>10}{'exit':>10}"
              f"{'capital':>12}{'fees':>9}{'net':>11}")
    for segment, rows in results.items():
        opened = [r for r in rows if r.get("opened")]
        closed = [r for r in rows if r.get("closed")]
        print(f"=== {segment}   {len(rows)} instrument(s) tried, "
              f"{len(opened)} opened, {len(closed)} closed")
        if not rows:
            print("    nothing on the tape this segment owns printed enough to replay\n")
            continue
        if closed:
            print("    " + header)
            print("    " + "-" * len(header))
            for trade in closed:
                shown = (trade.get("trading_symbol") or trade["symbol"])[:29]
                print(f"    {shown:<30}{trade['quantity_traded']:>8.0f}"
                      f"{trade['entry_price']:>10.2f}{trade['exit_price']:>10.2f}"
                      f"{trade['capital_committed']:>12.0f}{trade['fees_paid']:>9.2f}"
                      f"{trade['net_pnl']:>11.2f}")
            won = sum(1 for t in closed if t["net_pnl"] > 0)
            net = sum(t["net_pnl"] for t in closed)
            print("    " + "-" * len(header))
            print(f"    {won} of {len(closed)} closed in profit; net {net:,.2f} "
                  f"after Upstox's real charge stack for this segment")
        for trade in rows:
            if not trade.get("closed"):
                shown = (trade.get("trading_symbol") or trade["symbol"])[:29]
                print(f"    {shown:<30} did not close: {trade.get('why')}")
        print()

    traded = [s for s, rows in results.items() if any(r.get("closed") for r in rows)]
    print(f"Segments that opened AND closed a trade: {len(traded)} of {len(results)}"
          f"  {traded}")
    if skipped:
        print("\nInstruments on the tape that no built segment owns (the five "
              "largest reasons):")
        for reason, count in skipped.most_common(5):
            print(f"   {count:>6}  {reason}")

    REPLAY_ROOT.mkdir(parents=True, exist_ok=True)
    written = REPLAY_ROOT / f"{arguments.day}.by-segment.json"
    written.write_text(json.dumps({
        "replayed_day": arguments.day,
        "is_a_replay": True,
        "source": "captured Upstox tape, instruments resolved against Upstox's real master",
        "stop_multiple_of_typical_move": arguments.stop_multiple,
        "target_multiple_of_typical_move": arguments.target_multiple,
        "replayed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "segments_that_opened_and_closed": traded,
        "by_segment": results,
        "instruments_no_segment_owns": dict(skipped.most_common(20)),
    }, indent=2) + "\n")
    print(f"\nwrote {written}")
    print("Stamped `is_a_replay` and kept out of the spine's state root: these are not "
          "live trades and no board may show them as though they were (RL-071, Rule 8).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
