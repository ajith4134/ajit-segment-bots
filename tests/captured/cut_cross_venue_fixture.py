"""Cut one symbol's prints from both venues over the same window, out of the tape.

The basis rule is about two venues at once, and every fixture here so far is one
venue at a time. Cut rather than connected, because the two connections would be
opened at different moments and a basis measured across them would be measuring
the capture rather than the market.
"""
from __future__ import annotations

import json
import pathlib
import sys

PROJECT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests" / "captured"))

from capture_venue_payloads import record_in_manifest, write_payload_lines  # noqa: E402
from runtime.tape import read_payload, read_tape_index  # noqa: E402
from runtime.venues.adapter_registry import load_venue_adapter  # noqa: E402

TAPE_ROOT = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape"
HERE = pathlib.Path(__file__).resolve().parent
RECORDS_PER_VENUE = 1200


def cut(venue_id: str, symbol: str, day: str, name: str) -> dict:
    index_path = TAPE_ROOT / venue_id / symbol / f"{day}.index"
    blob_path = TAPE_ROOT / venue_id / symbol / f"{day}.blob"
    adapter = load_venue_adapter(venue_id)
    records, prices, times = [], [], []
    for record in read_tape_index(index_path):
        payload = read_payload(blob_path, record)
        trades = adapter.read_trades(payload)
        if not trades:
            continue
        records.append((int(record["received_at_ns"]), payload.decode("utf-8")))
        for trade in trades:
            prices.append(trade.price)
            times.append(trade.venue_time_ns)
        if len(records) >= RECORDS_PER_VENUE:
            break
    path = HERE / venue_id / name
    write_payload_lines(path, records)
    return {
        "path": str(path.relative_to(HERE)),
        "venue": venue_id,
        "source": f"the tape this project records: {venue_id}/{symbol}/{day}",
        "captured_on": day,
        "records": len(records),
        "trades": len(prices),
        "seconds_spanned": round((max(times) - min(times)) / 1e9, 3),
        "price_range": [min(prices), max(prices)],
        "how": (
            f"the first {len(records)} trade frames of {symbol} on {venue_id}, cut out of "
            f"~/.local/share/ajit-segment-bots/tape with runtime.tape.read_tape_index and "
            f"read_payload, each payload kept verbatim. Cut alongside the same window on the "
            f"other venue so the pair can be replayed against each other: market-anomaly-"
            f"detector judges a print against what the other venue said at that moment, and "
            f"two fixtures captured at different times would measure the capture"
        ),
    }


def main() -> None:
    day, symbol = sys.argv[1], sys.argv[2]
    entries = []
    for venue_id, name in (
        ("binance-usdm", f"{day}-{symbol.lower()}-trades-cross-venue.jsonl"),
        ("bybit-linear", f"{day}-{symbol.lower()}-trades-cross-venue.jsonl"),
    ):
        entry = cut(venue_id, symbol, day, name)
        record_in_manifest(entry)
        entries.append(entry)
        print(json.dumps(entry, indent=1))


if __name__ == "__main__":
    main()
