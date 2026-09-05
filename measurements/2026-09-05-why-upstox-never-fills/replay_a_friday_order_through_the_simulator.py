"""Why 325 Upstox order-requests on 2026-09-04 produced zero fills.

25,446 fills are recorded in the trade journal and every one of them is from the
crypto era. Since the pivot to Indian markets the chain reaches `order-request`
and stops: on Friday 2026-09-04 the bots routed 325 orders to the paper book and
the book filled none of them. `paper-fill-simulator` was alive for both order
bursts (its systemd scope ran 06:51:59-08:36:29 and 08:36:53-10:21:01), so it saw
those orders and refused each one.

Which refusal is not in any log. The part counts every branch it takes on its own
`standing`, but that standing rides on `part-health` into a heartbeat table that
is overwritten in place, so Friday's counts are gone and Monday's would be too.

This drives the real thing instead: `PaperFillSimulator`, built from the same
settings the spine builds it from, handed one of Friday's actual order-requests
read out of the journal, priced from that contract's own prints on Friday's tape.
Nothing here is invented -- the order, the price and the settings are all the
ones production used (RL-063).

It runs the order once per precondition, because a single refusal names only
itself: the ladder shows which of the three things production could have been
missing -- a measured session, a price for the symbol, a latency release --
actually reproduces "routed, never filled".
"""

from __future__ import annotations

import collections
import datetime
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from parts.paper_live_trading.paper_fill_simulator import PaperFillSimulator
from runtime.market_conditions import MarketSessionState, SessionKind
from runtime.tape import TradeFidelity, read_tape_index
from runtime.settings_reader import load_settings_document, settings_directory

DAY = "2026-09-04"
JOURNAL = pathlib.Path.home() / ".local/share/ajit-segment-bots/journal.trade-lifecycle-recorder.sqlite"
TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
MASTERS = pathlib.Path(__file__).resolve().parents[2] / "tests/captured/upstox"
# How far back into the 11 GB journal to read. Friday's entries are its tail.
JOURNAL_TAIL_BYTES = 8_000_000


def friday_order_requests() -> list[dict]:
    """Every order-request the bots routed on Friday, as it was journalled."""
    size = JOURNAL.stat().st_size
    routed = []
    with open(JOURNAL, "rb") as journal:
        journal.seek(max(size - JOURNAL_TAIL_BYTES, 0))
        journal.readline()
        for line in journal:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("kind") != "order-request":
                continue
            at = datetime.datetime.fromtimestamp(
                record.get("recorded_at_ns", 0) / 1e9, datetime.UTC
            )
            if at.strftime("%Y-%m-%d") == DAY:
                routed.append(record["payload"])
    return routed


def instrument_key_by_trading_symbol() -> dict[str, str]:
    """The captured NSE masters, read as the bridge reads them."""
    mapping = {}
    for master in sorted(MASTERS.glob("*instrument-master*.json")) + sorted(
        MASTERS.glob("*nse-master*.json")
    ):
        for row in json.loads(master.read_text()):
            symbol = row.get("trading_symbol")
            key = row.get("instrument_key")
            if symbol and key:
                mapping[symbol] = key
    return mapping


def last_traded_price(instrument_key: str) -> float | None:
    """The last price this contract actually printed on Friday's tape."""
    directory = TAPE / instrument_key
    index_path = directory / f"{DAY}.index"
    if not index_path.exists():
        return None
    records = read_tape_index(index_path)
    if len(records) == 0:
        return None
    with open(directory / f"{DAY}.blob", "rb") as blob:
        for record in reversed(records[-400:]):
            blob.seek(int(record["blob_offset"]))
            try:
                payload = json.loads(blob.read(int(record["blob_length"])))
            except ValueError:
                continue
            price = payload.get("price") or payload.get("last_traded_price")
            if price:
                return float(price)
    return None


def build_simulator_the_way_the_spine_does() -> PaperFillSimulator:
    """Same eight numbers `run_part` reads, from the operator's own settings."""
    document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    number = lambda name: float(document.read_value(name))
    return PaperFillSimulator(
        taker_fee_rate=number("taker_fee_rate"),
        maker_fee_rate=number("maker_fee_rate"),
        options_flat_brokerage=number("options_flat_brokerage"),
        options_stt_sell_rate=number("options_stt_sell_rate"),
        options_exchange_transaction_charge_rate=number(
            "options_exchange_transaction_charge_rate"
        ),
        options_ipft_charge_rate=number("options_ipft_charge_rate"),
        options_stamp_duty_buy_rate=number("options_stamp_duty_buy_rate"),
        options_gst_rate=number("options_gst_rate"),
    )


OPEN_FO_SESSION = MarketSessionState(
    segment="FO",
    kind=SessionKind.OPEN,
    as_of_date=datetime.date(2026, 9, 4),
    reason="within stated session hours",
    observed_at_ns=0,
)


def run_one(order: dict, price: float | None, session, in_flight: bool, drift=None):
    """One order through a fresh simulator, so the counters name this run only."""
    simulator = build_simulator_the_way_the_spine_does()
    simulator.observe_session(session)
    result = simulator.simulate(
        client_order_id=order["client_order_id"],
        venue_id=order["venue_id"],
        symbol=order["symbol"],
        side=order["side"],
        quantity=order["quantity"],
        order_type=order.get("order_type", "market"),
        limit_price=order.get("limit_price") or None,
        money_mode="paper",
        is_in_flight=in_flight,
        market_price=price,
        stop_price=order.get("stop_price"),
        decided_at_price=order.get("decided_at_price"),
        maximum_decision_drift=drift,
        price_fidelity=TradeFidelity.LAST_TRADED_PRICE_ONLY,
        segment=order.get("segment", "") or "",
    )
    moved = {
        name: value
        for name, value in vars(simulator.standing).items()
        if isinstance(value, (int, float)) and value and name != "orders_seen"
    }
    return result, moved


def main() -> int:
    routed = friday_order_requests()
    print(f"order-requests journalled on {DAY}: {len(routed)}")
    if not routed:
        print("NOT MEASURED: no Friday order-request found in the journal tail")
        return 1

    keys = instrument_key_by_trading_symbol()
    print(f"trading symbols in the captured NSE masters: {len(keys)}\n")

    # How many of the ordered names the feed actually streamed. A symbol with no
    # print on Friday's tape had no price to fill against, whatever else was true.
    ordered = collections.Counter(order["symbol"] for order in routed)
    streamed, unknown, silent = [], [], []
    for symbol in ordered:
        key = keys.get(symbol)
        if key is None:
            unknown.append(symbol)
        elif (TAPE / key / f"{DAY}.index").exists():
            streamed.append(symbol)
        else:
            silent.append(symbol)
    print(f"ordered symbols: {len(ordered)}")
    print(f"  streamed on Friday's tape : {len(streamed)}")
    print(f"  subscribed but silent     : {len(silent)}  {silent[:5]}")
    print(f"  not in the captured master: {len(unknown)}  {unknown[:5]}\n")

    chosen = None
    for order in routed:
        key = keys.get(order["symbol"])
        if key is None:
            continue
        price = last_traded_price(key)
        if price:
            chosen = (order, price)
            break
    if chosen is None:
        print("NOT MEASURED: no Friday order names a contract with a price on the tape")
        return 1

    order, price = chosen
    print(f"replaying: {order['side']} {order['quantity']} {order['symbol']}")
    print(f"  decided at {order.get('decided_at_price')}, tape's last print {price}\n")

    ladder = (
        ("everything present", price, OPEN_FO_SESSION, False),
        ("no session measured", price, None, False),
        ("no price for the symbol", None, OPEN_FO_SESSION, False),
        ("still in flight", price, OPEN_FO_SESSION, True),
    )
    document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    drift = float(document.read_value("maximum_decision_price_drift"))
    ladder = ladder + (
        (f"the drift bound ({drift:.1%})", price, OPEN_FO_SESSION, False, drift),
    )
    for rung in ladder:
        label, its_price, session, in_flight = rung[:4]
        drift_bound = rung[4] if len(rung) > 4 else None
        result, moved = run_one(order, its_price, session, in_flight, drift_bound)
        print(f"  {label:26} -> {result.outcome}")
        print(f"  {'':26}    {result.reason[:96]}")
        if moved:
            print(f"  {'':26}    counters: {moved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
