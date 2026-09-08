"""Fetch and parse a broker's own static instrument master file.

Shared infrastructure (T-4), not a part: two parts need the same static
gzipped-JSON file -- broker-instrument-catalogue-reader, which is the file's
paced publisher on the bus, and broker-symbol-universe-bridge, which reads it
a second time on its own start rather than waiting on that pacing for a held
position's own listing (docs/proposals/broker-symbol-universe-bridge.md,
2026-09-08 addendum). A part importing another part's module by name is
exactly what T-4 refuses; a part importing shared code from `runtime` is what
every other broker-adapter call in this file already does.
"""

from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.request

from runtime.brokers.broker_adapter import BrokerAdapter, InstrumentListing
from runtime.brokers.broker_http_request import build_broker_request


def fetch_bytes(url: str, timeout_seconds: float = 30.0) -> bytes:
    request = build_broker_request(url, accept="application/gzip")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return response.read()


def fetch_and_parse_listings(
    adapter: BrokerAdapter, fetch=fetch_bytes
) -> tuple[InstrumentListing, ...]:
    """Every listing from every URL the adapter names, gunzipped and parsed.

    One call per URL, never a cursor loop -- these are whole files, not
    paginated responses (spec section 4)."""
    listings: list[InstrumentListing] = []
    for url in adapter.instrument_listing_urls():
        raw = fetch(url)
        rows = json.loads(gzip.decompress(raw).decode("utf-8"))
        listings.extend(adapter.read_instrument_listings(rows))
    return tuple(listings)


__all__ = ["fetch_and_parse_listings", "fetch_bytes"]
