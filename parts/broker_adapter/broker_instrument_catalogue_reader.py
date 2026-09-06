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
from runtime.brokers.broker_http_request import build_broker_request
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.restatement_conveyor import RestatementConveyor

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


def describe_standing(
    listings: tuple, last_failure: str | None, restatement: dict | None = None
) -> dict:
    """What the reader holds, and how far round the restatement cycle it is.

    `listings_restated` climbing is the evidence that a consumer which started
    after the last fetch will get the catalogue at all: until 2026-09-04 the
    master was spoken only when it was re-fetched, once an hour, and seventeen
    parts consume it while the governor restarts them far more often than that.
    `cycle_position` says where in the master the next slice comes from, so a
    conveyor that has stopped turning is visible rather than merely quiet.
    """
    restatement = restatement or {}
    return {
        "part_id": PART_ID,
        "listings_seen": len(listings),
        "last_failure": last_failure,
        "listings_restated": restatement.get("restated", 0),
        "cycle_position": restatement.get("position", 0),
        "cycles_completed": restatement.get("cycles", 0),
        "listings_per_second": restatement.get("rate", 0.0),
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

    conveyor = RestatementConveyor(
        context.number("broker_catalogue_restatement_cycle_seconds")
    )

    state = {"listings": (), "last_failure": None, "last_read_at": None}

    def read_if_due(now: float) -> None:
        due = (
            state["last_read_at"] is None
            or now - state["last_read_at"] >= refresh_interval_seconds
        )
        if not due:
            return
        try:
            fetched = fetch_and_parse_listings(adapter)
            state["last_failure"] = None
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as failure:
            state["last_failure"] = f"{type(failure).__name__}: {failure}"
            return
        state["last_read_at"] = now
        # A re-fetch replaces the master, so a listing dropped from the
        # catalogue stops being restated at once and a new one is reached within
        # a cycle. The conveyor keeps its position across the swap rather than
        # restarting -- see RestatementConveyor.hold for why restarting is the
        # trap, not the safeguard.
        state["listings"] = fetched
        conveyor.hold(fetched)

    def restate_a_slice(now: float) -> None:
        """Say the next part of the master, at a rate a consumer can drain.

        **The whole master, forever, evenly.** Publishing only at the fetch left a
        consumer that started a second later waiting the full hour, and the
        governor restarts parts far more often than that: measured 2026-09-04,
        `expiry-day-zero-to-hero-detector` had never held a single listing.

        **Paced, because one burst does not arrive.** A listing pickles to 400
        bytes and the default socket buffer is 212,992, so it holds 532 of them;
        publishing 102,940 at once overflows it 193 times over, which is why
        `broker-market-feed-reader` was measured holding 1,067 of them and none of
        the three index underlyings the segment trades. The rate is the master
        divided by the cycle -- 57 a second at 1,800 s -- a tenth of what one
        buffer holds, so no consumer can be overrun however slowly it drains.

        Time-based rather than a fixed slice per tick: this part is woken by its
        clock and the interval between ticks is not guaranteed, so a slice sized
        per tick would speed up or slow down with the machine's load. What is
        owed is computed from elapsed time, and capped at one cycle so a long
        pause does not become the burst this exists to prevent.

        The rate itself lives in `runtime/restatement_conveyor.py`, shared with
        `subscribed-instrument-listing-filter`, which restates the subscribed
        subset of this same master to the parts that can only use that.
        """
        slice_ = conveyor.due_slice(now)
        if slice_:
            publish_listings(slice_)

    def tick() -> None:
        import time

        now = time.monotonic()
        read_if_due(now)
        restate_a_slice(now)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_standing(
            state["listings"], state["last_failure"], conveyor.standing()
        ),
    )


__all__ = [
    "PART_DECLARATION",
    "PART_ID",
    "describe_standing",
    "fetch_and_parse_listings",
    "fetch_bytes",
    "start_part",
]
