"""Capture real venue payloads, so adapter tests run on what a venue actually sent.

RL-063: tests run on real captured crypto data, never invented fixtures. A hand-
written message is a test of what the author believed the venue sends, which is
exactly the belief the test was supposed to check -- and every venue oddity worth
having an adapter for is one nobody would have invented.

This script is the generator; the files beside it are its output, and
`capture-manifest.json` records when each was captured, from which URL, and how.
Re-run it to refresh a fixture:

    .venv/bin/python tests/captured/capture_venue_payloads.py binance-usdm

Nothing here is edited afterwards. The REST catalogue is subset -- the full
response is over a megabyte and would be permanent weight in every future clone
-- but each entry it keeps is kept verbatim, and the manifest says which rule
selected them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time
import urllib.request
from typing import Callable

HERE = pathlib.Path(__file__).resolve().parent
MANIFEST_PATH = HERE / "capture-manifest.json"

# Run as a script, sys.path[0] is this directory, so `runtime` would not import.
# The capture builds its frames from the real adapter, so it needs the package.
PROJECT = HERE.parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

# How many messages is enough to be a fixture: enough to see a stream's shape
# change (a candle that closes, a book update following another) without turning
# the repository into a tape. The tape is where volume belongs.
STREAM_MESSAGE_COUNT = 40
STREAM_TIMEOUT_SECONDS = 120.0

# The one symbol every venue in phase 1 lists, so a fixture is comparable across
# them. Adapter behaviour is per-venue; the symbol is not the thing under test.
CAPTURE_SYMBOL = "BTCUSDT"
# What a fixture is captured at, matching what the shipped settings ask for: the
# 1-minute candle of spec section 5, and the `book_depth_levels` default of 20.
# The adapter maps the depth onto its own ladder, so this is the request rather
# than the level the venue ends up serving.
CAPTURE_CANDLE_INTERVAL = "1m"
CAPTURE_BOOK_DEPTH_LEVELS = 20


def write_payload_lines(path: pathlib.Path, records: list[tuple[int, str]]) -> None:
    """One captured message per line: when it arrived, and exactly what arrived.

    JSON-encoding the payload as a string rather than writing it raw keeps a
    payload containing a newline from becoming two records -- these two venues
    send single-line JSON today, and a fixture format that quietly depends on
    that would break on the venue that does not.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for received_at_ns, payload in records:
            handle.write(json.dumps({"received_at_ns": received_at_ns, "payload": payload}) + "\n")


def read_payload_lines(path: pathlib.Path) -> list[tuple[int, bytes]]:
    """The captured messages, as the bytes the venue sent."""
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        records.append((int(record["received_at_ns"]), record["payload"].encode("utf-8")))
    return records


async def capture_stream_payloads(
    url: str,
    subscribe_frame: bytes,
    message_count: int,
    keep_going_until: "Callable[[str], bool] | None" = None,
    messages_after_that: int = 0,
) -> list[tuple[int, str]]:
    """Connect, subscribe, and keep what comes back -- acknowledgement included.

    The subscribe acknowledgement is kept on purpose. A control frame that is not
    a tape record is a case the adapter has to get right, and it is the only case
    that arrives without asking.

    `keep_going_until` is for the event that is rare in message count but certain
    in wall-clock time. A minute candle closes once a minute, and 40 messages of
    a busy symbol is ten seconds -- so a fixture sized by message count can miss
    the closed-candle flag entirely, which is the one field of that stream worth
    testing. Given a predicate, capture runs past `message_count` until it fires
    and then keeps `messages_after_that` more.
    """
    import websockets

    records: list[tuple[int, str]] = []
    remaining_after_trigger: int | None = None
    async with websockets.connect(url, open_timeout=30, close_timeout=5) as connection:
        # Sent as text, not as the bytes it is. Measured 2026-08-22: Binance closes
        # the connection with 1008 "Invalid request" on a binary frame carrying
        # exactly the same JSON that works as text. websockets picks the frame type
        # from the argument's type, so passing bytes here is the same mistake in a
        # form that looks like it is being careful about encoding.
        await connection.send(subscribe_frame.decode("utf-8"))
        while True:
            message = await asyncio.wait_for(connection.recv(), timeout=STREAM_TIMEOUT_SECONDS)
            if isinstance(message, bytes):
                message = message.decode("utf-8")
            records.append((time.time_ns(), message))

            if remaining_after_trigger is not None:
                remaining_after_trigger -= 1
                if remaining_after_trigger <= 0:
                    break
                continue
            if keep_going_until is not None and keep_going_until(message):
                remaining_after_trigger = messages_after_that
                if remaining_after_trigger <= 0:
                    break
                continue
            if keep_going_until is None and len(records) >= message_count:
                break
    return records


def fetch_json_over_rest(url: str) -> tuple[dict, dict[str, str]]:
    """One REST call, returning the body and the headers it came with.

    The headers matter as much as the body: Binance reports remaining weight in
    `X-MBX-USED-WEIGHT-1m`, which is what a rate budget is read from rather than
    counted locally.
    """
    with urllib.request.urlopen(url, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))
        headers = {name.lower(): value for name, value in response.headers.items()}
    return body, headers


def subset_binance_exchange_info(catalogue: dict, keep_per_kind: int) -> dict:
    """Keep a few real entries of each kind, verbatim, and every rate limit.

    The selection rule is the point: one of each (contractType, status) pair the
    venue returned, so the fixture contains a TRADIFI_PERPETUAL and a SETTLING
    contract rather than whichever symbols happened to sort first. Those two are
    exactly what the capturable-symbol rule turns on, and a fixture of the first
    ten symbols alphabetically would very likely contain neither.
    """
    kept: dict[tuple[str, str], list[dict]] = {}
    for symbol in catalogue["symbols"]:
        key = (symbol.get("contractType", ""), symbol.get("status", ""))
        bucket = kept.setdefault(key, [])
        if len(bucket) < keep_per_kind:
            bucket.append(symbol)
    return {
        "timezone": catalogue.get("timezone"),
        "serverTime": catalogue.get("serverTime"),
        "rateLimits": catalogue.get("rateLimits"),
        "symbols": [symbol for bucket in kept.values() for symbol in bucket],
    }


def record_in_manifest(entry: dict) -> None:
    """Append what was captured, so a fixture can never be a file of unknown origin."""
    manifest = json.loads(MANIFEST_PATH.read_text()) if MANIFEST_PATH.exists() else {"captures": []}
    manifest["captures"] = [
        existing for existing in manifest["captures"] if existing["path"] != entry["path"]
    ]
    manifest["captures"].append(entry)
    manifest["captures"].sort(key=lambda existing: existing["path"])
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")


def capture_binance_usdm(day: str) -> None:
    """Every fixture the Binance USDⓈ-M adapter's tests are checked against.

    The URLs, topics and subscribe frames come from the adapter itself rather
    than being written again here. A fixture captured with a hand-built frame
    would be evidence about that frame, not about the code that will run -- and
    the first thing it would stop catching is a wrong routed path, which is the
    one mistake on this venue that produces a healthy connection and no data.
    """
    from runtime.tape import StreamKind
    from runtime.venues.adapter_registry import load_venue_adapter
    from runtime.venues.venue_adapter import StreamRequest

    venue = "binance-usdm"
    adapter = load_venue_adapter(venue)
    venue_directory = HERE / venue

    def topic(stream_kind: StreamKind, **parameters) -> str:
        return adapter.subscription_topic(
            StreamRequest(stream_kind=stream_kind, symbol=CAPTURE_SYMBOL, **parameters)
        )

    market_url = adapter.stream_endpoint_url(StreamKind.TRADE)
    market_topics = [
        topic(StreamKind.TRADE),
        topic(StreamKind.CANDLE, candle_interval=CAPTURE_CANDLE_INTERVAL),
    ]
    market_path = venue_directory / f"{day}-market-ws-aggtrade-kline.jsonl"
    write_payload_lines(
        market_path,
        asyncio.run(
            capture_stream_payloads(
                market_url, adapter.subscribe_frame(market_topics), STREAM_MESSAGE_COUNT
            )
        ),
    )
    record_in_manifest(
        {
            "path": str(market_path.relative_to(HERE)),
            "venue": venue,
            "source": market_url,
            "subscribed": market_topics,
            "captured_on": day,
            "how": "connect with the adapter's own url and subscribe frame, keep every frame "
            "received including the acknowledgement",
        }
    )

    # A minute candle closes once a minute, so a fixture sized by message count
    # can contain no closed candle at all -- 40 messages of BTCUSDT is ten
    # seconds. This one runs until the venue sends `x: true` and keeps a few
    # after it, so the closed-candle flag is tested against a real close rather
    # than against one someone typed.
    candle_topics = [topic(StreamKind.CANDLE, candle_interval=CAPTURE_CANDLE_INTERVAL)]
    candle_path = venue_directory / f"{day}-market-ws-kline-through-close.jsonl"
    write_payload_lines(
        candle_path,
        asyncio.run(
            capture_stream_payloads(
                adapter.stream_endpoint_url(StreamKind.CANDLE),
                adapter.subscribe_frame(candle_topics),
                message_count=0,
                keep_going_until=lambda text: '"x":true' in text.replace(" ", ""),
                messages_after_that=4,
            )
        ),
    )
    record_in_manifest(
        {
            "path": str(candle_path.relative_to(HERE)),
            "venue": venue,
            "source": adapter.stream_endpoint_url(StreamKind.CANDLE),
            "subscribed": candle_topics,
            "captured_on": day,
            "how": "connect with the adapter's own url and subscribe frame, keep every frame "
            "until one carries a closed candle (k.x true), then four more",
        }
    )

    book_url = adapter.stream_endpoint_url(StreamKind.BOOK)
    book_topics = [topic(StreamKind.BOOK, book_depth_levels=CAPTURE_BOOK_DEPTH_LEVELS)]
    book_path = venue_directory / f"{day}-public-ws-depth20.jsonl"
    write_payload_lines(
        book_path,
        asyncio.run(
            capture_stream_payloads(
                book_url, adapter.subscribe_frame(book_topics), STREAM_MESSAGE_COUNT
            )
        ),
    )
    record_in_manifest(
        {
            "path": str(book_path.relative_to(HERE)),
            "venue": venue,
            "source": book_url,
            "subscribed": book_topics,
            "captured_on": day,
            "how": "connect with the adapter's own url and subscribe frame, keep every frame "
            "received including the acknowledgement",
        }
    )

    catalogue_url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    catalogue, headers = fetch_json_over_rest(catalogue_url)
    kind_counts: dict[str, int] = {}
    for symbol in catalogue["symbols"]:
        key = f"{symbol.get('contractType', '')}/{symbol.get('status', '')}"
        kind_counts[key] = kind_counts.get(key, 0) + 1
    keep_per_kind = 3
    catalogue_path = venue_directory / f"{day}-exchange-info-subset.json"
    catalogue_path.write_text(
        json.dumps(subset_binance_exchange_info(catalogue, keep_per_kind), indent=1) + "\n"
    )
    record_in_manifest(
        {
            "path": str(catalogue_path.relative_to(HERE)),
            "venue": "binance-usdm",
            "source": catalogue_url,
            "captured_on": day,
            "how": (
                f"one REST call, then subset to the first {keep_per_kind} symbols of each "
                f"(contractType, status) pair, each kept verbatim. rateLimits kept whole."
            ),
            "full_response_symbol_count": len(catalogue["symbols"]),
            "symbol_counts_by_contract_type_and_status": kind_counts,
            "response_headers_of_note": {
                name: value for name, value in headers.items() if name.startswith("x-mbx")
            },
        }
    )


VENUE_CAPTURES = {"binance-usdm": capture_binance_usdm}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("venue", choices=sorted(VENUE_CAPTURES), help="which venue to capture from")
    parser.add_argument(
        "--day",
        required=True,
        help="the UTC day to name the fixture after, YYYY-MM-DD -- passed in rather than "
        "read from the clock so a re-run names the file the operator meant",
    )
    arguments = parser.parse_args(argv)
    VENUE_CAPTURES[arguments.venue](arguments.day)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
