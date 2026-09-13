#!/usr/bin/env python3
"""Option pullbacks before a new high, on ~50 sessions of Upstox history per contract.

The operator, 2026-09-13: "use past historic prices of lots of days ... instead of
only trades on the bot". `measure_retracements.py` beside this read the two sessions
the tape holds (2026-09-07, 2026-09-08). This reads Upstox's one-minute candles for
the contracts NSE says traded most, from 2026-07-01 to 2026-09-11.

Which contracts: the 30 stock options and 10 index options with the highest traded
volume expiring 2026-09-29, from NSE's own F&O bhavcopy of 2026-09-04 -- chosen by the
exchange's volume, not by this project, so the sample is where the market traded.

The series is each one-minute bar's close (see `operate/historical_prints.py` for why
the close and not the high or low), reset every session. Same definition as the tape
measurement: each new session high closes an episode, and its retracement is the
deepest fall below the previous high; only pullbacks that ended in a new high count.
Bar closes miss pullbacks inside a minute, so these understate the tape's by
construction -- both are reported.

    .venv/bin/python measurements/2026-09-13-indian-option-retracements/measure_retracements_from_history.py
"""
import collections
import datetime
import gzip
import json
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from operate.historical_prints import historical_candles, upstox_access_token  # noqa: E402
from operate.nse_fo_bhavcopy import contracts_in  # noqa: E402

STATE = pathlib.Path.home() / ".local/share/ajit-segment-bots"
BHAVCOPY = STATE / "history/nse-fo-bhavcopy/20260904.csv"
MASTER = STATE / "instrument-master/complete.json.gz"
EXPIRY = "2026-09-29"
RANGES = (("2026-07-01", "2026-07-31"), ("2026-08-01", "2026-08-31"), ("2026-09-01", "2026-09-11"))
STOCK_CONTRACTS, INDEX_CONTRACTS = 30, 10
INDEX_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def quantile(values, q):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def main() -> int:
    chosen = [c for c in contracts_in(BHAVCOPY.read_text()) if c.expiry == EXPIRY and c.traded]
    stocks = sorted((c for c in chosen if c.underlying not in INDEX_UNDERLYINGS), key=lambda c: -c.volume)
    indices = sorted((c for c in chosen if c.underlying in INDEX_UNDERLYINGS), key=lambda c: -c.volume)
    wanted = stocks[:STOCK_CONTRACTS] + indices[:INDEX_CONTRACTS]

    master = json.load(gzip.open(MASTER))
    expiry_text = "29 SEP 26"
    key_of = {
        row.get("trading_symbol"): row.get("instrument_key")
        for row in master if row.get("segment") == "NSE_FO"
    }
    token = upstox_access_token()
    by_kind = collections.defaultdict(list)
    sessions = collections.Counter()
    missing, refused = [], []
    for contract in wanted:
        symbol = f"{contract.underlying} {contract.strike:g} {contract.option_type} {expiry_text}"
        key = key_of.get(symbol)
        if key is None:
            missing.append(symbol)
            continue
        kind = "index" if contract.underlying in INDEX_UNDERLYINGS else "stock"
        bars = []
        for start, end in RANGES:
            try:
                bars.extend(historical_candles(key, start, end, token))
            except RuntimeError as refusal:
                refused.append(f"{symbol} {start}: {refusal}")
        by_day = collections.defaultdict(list)
        for bar in sorted(bars, key=lambda bar: bar.bar_time_ms):
            if bar.close:
                day = datetime.datetime.fromtimestamp(bar.bar_time_ms / 1000, IST).date()
                by_day[day].append(float(bar.close))
        for day, closes in by_day.items():
            if len(closes) < 3:
                continue
            sessions[kind] += 1
            high, deepest = closes[0], 0.0
            for price in closes[1:]:
                if price > high:
                    if deepest > 0:
                        by_kind[kind].append(deepest)
                    high, deepest = price, 0.0
                else:
                    deepest = max(deepest, (high - price) / high)

    print(f"Upstox one-minute closes, {RANGES[0][0]}..{RANGES[-1][1]}; contracts from NSE bhavcopy "
          f"2026-09-04, expiry {EXPIRY}, top by traded volume")
    if missing:
        print(f"not in the instrument master ({len(missing)}): {', '.join(missing[:5])}")
    if refused:
        print(f"refused requests ({len(refused)}): {refused[0]}")
    everything = [r for values in by_kind.values() for r in values]
    for kind, values in (("stock", by_kind["stock"]), ("index", by_kind["index"]), ("all", everything)):
        if not values:
            continue
        print(f"\n{kind}: {sessions[kind] if kind != 'all' else sum(sessions.values())} sessions, "
              f"{len(values):,} continuation pullbacks")
        for q in (0.50, 0.80, 0.90, 0.95, 0.99):
            print(f"  p{int(q * 100):<3d} {quantile(values, q):.4%}")
        print(f"  deeper than the crypto prior 1%: {sum(r > 0.01 for r in values) / len(values):.1%}; "
              f"deeper than 10%: {sum(r > 0.10 for r in values) / len(values):.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
