"""What crossing an NSE option's touch spread costs -- the one quantity seven
settings still take from the crypto venues.

docs/settings-fitted-to-crypto-the-guard-cannot-see.md, "Costs and spreads taken
from the crypto venues". Each of those notes rests on a basis-point figure read
off BTCUSDT-class books ("a spread and a fee on a liquid symbol", "the liquid
captured symbols on 2026-08-22", "ten spreads on the liquid symbols"). This
measures the same thing on this project's own Upstox book tape.

Three views, because they answer different questions:

    every snapshot       the figure the 2026-09-07 staleness study took (0.3175%
                         p50 over 23,606 snapshots) -- repeated so the two agree
    per contract         one median per contract, so a contract whose book
                         updates a thousand times is not a thousand votes
    print-weighted       each contract's median weighted by how often it traded:
                         the spread the flow actually meets, which is the one a
                         fill of this desk's pays

Only in-session snapshots (09:15-15:30 IST), and only books with both sides.
Split by the master's underlying_type, since index and stock options are
separate segments with one setting between them.

Run:  .venv/bin/python measurements/2026-09-13-indian-option-spreads/measure_indian_option_spreads.py
"""

from __future__ import annotations

import gzip
import json
import pathlib
import sys

import numpy

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from runtime.indian_options_fee_model import upstox_options_order_cost
from runtime.tape import read_payload, read_tape_index
from runtime.trading_types import BUY, SELL

TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
MASTER = pathlib.Path.home() / ".local/share/ajit-segment-bots/instrument-master/complete.json.gz"

# The two sessions with a book tape. 2026-09-09..11 the VM was off; 2026-09-12 is
# a Saturday and its books are the Friday close restated.
DAYS = ("2026-09-07", "2026-09-08")
SAMPLE_EVERY = 10
SESSION_OPEN_UTC_SECONDS = 3 * 3600 + 45 * 60
SESSION_CLOSE_UTC_SECONDS = 10 * 3600
MINIMUM_SNAPSHOTS = 5
DESK_PREMIUM = 150_000.0      # refit script's DESK_ORDER_SIZE
QUANTILES = (0.50, 0.80, 0.95)


def is_in_session(at_ns: int) -> bool:
    seconds_of_day = (at_ns // 1_000_000_000) % 86400
    return SESSION_OPEN_UTC_SECONDS <= seconds_of_day < SESSION_CLOSE_UTC_SECONDS


def half_spreads_for_contract(directory: pathlib.Path, day: str) -> list[float]:
    index_path = directory / f"{day}.book.index"
    index = read_tape_index(index_path)
    blob = directory / f"{day}.book.blob"
    out = []
    for position in range(0, len(index), SAMPLE_EVERY):
        record = index[position]
        if not is_in_session(int(record[0])):
            continue
        try:
            payload = json.loads(read_payload(blob, record))
        except Exception:
            continue
        levels = payload.get("levels") or []
        if not levels:
            continue
        bid, ask = levels[0].get("bid_price"), levels[0].get("ask_price")
        if not bid or not ask or ask <= bid:
            continue
        mid = (bid + ask) / 2.0
        out.append((ask - bid) / 2.0 / mid)
    return out


def print_count(directory: pathlib.Path, day: str) -> int:
    index_path = directory / f"{day}.index"
    if not index_path.exists():
        return 0
    return sum(1 for record in read_tape_index(index_path) if is_in_session(int(record[0])))


def weighted_quantile(values, weights, quantile: float) -> float:
    order = numpy.argsort(values)
    values, weights = numpy.asarray(values)[order], numpy.asarray(weights, dtype=float)[order]
    cumulative = numpy.cumsum(weights) / weights.sum()
    return float(values[numpy.searchsorted(cumulative, quantile)])


def charge_round_trip_fraction(premium_value: float) -> float:
    rates = dict(
        flat_brokerage=20.0, stt_sell_rate=0.001,
        exchange_transaction_charge_rate=0.0003503, ipft_charge_rate=0.000005,
        stamp_duty_buy_rate=0.00003, gst_rate=0.18,
    )
    buy = upstox_options_order_cost(premium_value, BUY, **rates).total
    sell = upstox_options_order_cost(premium_value, SELL, **rates).total
    return (buy + sell) / premium_value


def main() -> int:
    kind_of = {
        row["instrument_key"]: row.get("underlying_type")
        for row in json.load(gzip.open(MASTER)) if row.get("segment") in ("NSE_FO", "BSE_FO")
    }
    snapshots = {"INDEX": [], "EQUITY": []}
    contracts = {"INDEX": [], "EQUITY": []}   # (median half spread, prints)
    for day in DAYS:
        for directory in sorted(TAPE.glob("NSE_FO*")):
            if not (directory / f"{day}.book.index").exists():
                continue
            kind = kind_of.get(directory.name.replace("|", "|", 1))
            if kind not in snapshots:
                continue
            spreads = half_spreads_for_contract(directory, day)
            if len(spreads) < MINIMUM_SNAPSHOTS:
                continue
            snapshots[kind].extend(spreads)
            contracts[kind].append((float(numpy.median(spreads)), print_count(directory, day)))

    charges = charge_round_trip_fraction(DESK_PREMIUM)
    print(f"days {', '.join(DAYS)}; in-session book snapshots, every {SAMPLE_EVERY}th\n")
    print(f"Upstox charge stack, round trip at Rs{DESK_PREMIUM:,.0f}: {charges:.6f} ({charges:.4%})\n")
    pooled_snapshots = snapshots["INDEX"] + snapshots["EQUITY"]
    pooled_contracts = contracts["INDEX"] + contracts["EQUITY"]
    for label, snaps, pairs in (
        ("index options", snapshots["INDEX"], contracts["INDEX"]),
        ("stock options", snapshots["EQUITY"], contracts["EQUITY"]),
        ("both", pooled_snapshots, pooled_contracts),
    ):
        medians = [median for median, _ in pairs]
        traded = [(median, prints) for median, prints in pairs if prints > 0]
        print(f"-- {label}: {len(snaps):,} snapshots, {len(pairs):,} contract-days, "
              f"{len(traded):,} of them traded, {sum(p for _, p in traded):,} prints --")
        for quantile in QUANTILES:
            every = float(numpy.quantile(snaps, quantile))
            per_contract = float(numpy.quantile(medians, quantile))
            weighted = weighted_quantile([m for m, _ in traded], [p for _, p in traded], quantile)
            print(f"  half spread p{int(quantile * 100)}   every snapshot {every:.6f}"
                  f"   per contract {per_contract:.6f}   print-weighted {weighted:.6f}"
                  f"   -> round trip {charges + 2 * weighted:.6f}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
