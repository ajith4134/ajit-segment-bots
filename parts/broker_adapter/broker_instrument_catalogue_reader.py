"""broker-instrument-catalogue-reader: every tradable contract, as the
broker's own daily instrument master lists it.

Static gzipped files refreshed once a day around 6 AM IST (spec section 4)
-- not a paginated REST catalogue the way the crypto build's
symbol-catalogue-reader followed. No cursor loop, no per-page request.
"""

from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.request

from runtime.brokers.broker_adapter import BrokerAdapter, InstrumentListing
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "broker-instrument-catalogue-reader"

PART_DECLARATION = PartDeclaration(
    part_id="broker-instrument-catalogue-reader",
    consumes=(),
    produces=("broker-instrument-listing", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)


def fetch_bytes(url: str, timeout_seconds: float = 30.0) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/gzip"})
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


def describe_standing(listings: tuple, last_failure: str | None) -> dict:
    return {
        "part_id": PART_ID,
        "listings_seen": len(listings),
        "last_failure": last_failure,
    }


def start_part(context) -> int:
    """One reader, one broker for now (Upstox) -- a settings-driven adapter
    registry follows the same pattern as venue_adapter's once a second
    broker is actually built, not invented ahead of that need.
    """
    from runtime.brokers.upstox import UpstoxAdapter

    adapter = UpstoxAdapter()
    publish_listings = context.bus.publisher_for("broker-instrument-listing")
    refresh_interval_seconds = context.number("broker_catalogue_refresh_interval")

    state = {"listings": (), "last_failure": None, "last_read_at": None}

    def read_if_due() -> None:
        import time

        now = time.monotonic()
        due = (
            state["last_read_at"] is None
            or now - state["last_read_at"] >= refresh_interval_seconds
        )
        if not due:
            return
        try:
            state["listings"] = fetch_and_parse_listings(adapter)
            state["last_failure"] = None
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as failure:
            state["last_failure"] = f"{type(failure).__name__}: {failure}"
            return
        state["last_read_at"] = now
        publish_listings(state["listings"])

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=read_if_due,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_standing(state["listings"], state["last_failure"]),
    )


__all__ = [
    "PART_DECLARATION",
    "PART_ID",
    "describe_standing",
    "fetch_and_parse_listings",
    "fetch_bytes",
    "start_part",
]
