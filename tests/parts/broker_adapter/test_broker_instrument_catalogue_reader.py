import gzip
import json

from parts.broker_adapter.broker_instrument_catalogue_reader import fetch_and_parse_listings
from runtime.brokers.upstox import UpstoxAdapter


def test_fetch_and_parse_listings_reads_a_gzipped_json_array():
    # Same real sample row as Task 2's test, gzipped the way Upstox actually
    # serves these files -- the shape under test here is the gzip+JSON
    # handling, not the row parsing (already covered in Task 2).
    rows = [{
        "segment": "NSE_EQ", "name": "JOCIL LIMITED", "exchange": "NSE",
        "isin": "INE839G01010", "instrument_type": "EQ",
        "instrument_key": "NSE_EQ|INE839G01010", "lot_size": 1,
        "freeze_quantity": 100000.0, "exchange_token": "16927",
        "tick_size": 5.0, "trading_symbol": "JOCIL",
    }]
    gzipped = gzip.compress(json.dumps(rows).encode("utf-8"))

    fetched_urls = []
    def fake_fetch(url: str) -> bytes:
        fetched_urls.append(url)
        return gzipped

    adapter = UpstoxAdapter()
    listings = fetch_and_parse_listings(adapter, fetch=fake_fetch)

    assert len(listings) == 2  # one per URL -- NSE.json.gz and BSE.json.gz, same fixture for both
    assert listings[0].instrument_key == "NSE_EQ|INE839G01010"
    assert fetched_urls == list(adapter.instrument_listing_urls())


# ---- the conveyor ------------------------------------------------------------
#
# Until 2026-09-04 the master was published only when it was re-fetched, once an
# hour, in one burst of 102,940 listings. Both halves were broken. A consumer
# that started a second after a fetch waited the full hour -- and the governor
# restarts parts far more often than that, which is why
# `expiry-day-zero-to-hero-detector` was measured having never held a single
# listing. And the burst did not arrive anyway: a listing pickles to 400 bytes
# against a 212,992-byte socket buffer that holds 532, so `broker-market-feed-
# reader` held 1,067 of 102,940 and none of the three index underlyings the
# segment trades.

class _Bus:
    def __init__(self):
        self.published = []

    def publisher_for(self, _data_type):
        def publish(items):
            self.published.extend(items)
        return publish


class _Context:
    """Only what `start_part` reads, so the test drives the real code path."""

    def __init__(self, numbers, bus):
        self._numbers = numbers
        self.bus = bus
        self.control_socket = None
        self.health_interval_seconds = 1.0
        self.input_descriptors = ()
        self.tick_floor_seconds = 0.0
        self.emit_health = lambda *a, **k: None
        self.declaration = None

    def number(self, name):
        return self._numbers[name]


def _a_catalogue(count):
    adapter = UpstoxAdapter.__new__(UpstoxAdapter)
    rows = json.loads(
        (
            __import__("pathlib").Path(__file__).resolve().parents[3]
            / "tests/captured/upstox/2026-09-04-nse-instrument-master-nifty-slice.json"
        ).read_text()
    )
    real = tuple(UpstoxAdapter.read_instrument_listings(adapter, rows))
    import dataclasses

    out = []
    copy = 0
    while len(out) < count:
        for listing in real:
            out.append(
                dataclasses.replace(listing, instrument_key=f"{listing.instrument_key}#{copy}")
            )
            if len(out) >= count:
                break
        copy += 1
    return tuple(out)


def _conveyor(monkeypatch, catalogue, cycle_seconds=1800.0):
    """The real `start_part` wiring, with the fetch and the clock supplied.

    The patches must outlive this call: `read_if_due` looks
    `fetch_and_parse_listings` up as a module global when the tick runs, not when
    `start_part` builds the closure, so restoring it here would send the first
    tick to Upstox's real endpoint -- which is what the first version of this
    helper did, and the catalogue it came back with was the live 102,940.
    """
    import parts.broker_adapter.broker_instrument_catalogue_reader as module

    bus = _Bus()
    captured = {}

    def fake_run_part(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(module, "run_part", fake_run_part)
    monkeypatch.setattr(module, "fetch_and_parse_listings", lambda _adapter: catalogue)
    module.start_part(
        _Context(
            {
                "broker_catalogue_refresh_interval": 3600.0,
                "broker_catalogue_restatement_cycle_seconds": cycle_seconds,
            },
            bus,
        )
    )
    return bus, captured["do_one_tick"], captured["read_standing"]


def test_the_whole_master_is_restated_within_one_cycle(monkeypatch):
    """A consumer that started after the fetch must still receive every listing."""
    catalogue = _a_catalogue(2_000)
    bus, tick, _standing = _conveyor(monkeypatch, catalogue, cycle_seconds=100.0)

    clock = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])

    tick()  # fetches, and starts the conveyor's clock
    for _ in range(200):
        clock[0] += 1.0
        tick()

    keys = {listing.instrument_key for listing in bus.published}
    assert keys == {listing.instrument_key for listing in catalogue}


def test_no_slice_can_overrun_a_consumers_buffer(monkeypatch):
    """532 listings fill the default socket buffer; a slice must stay well under it.

    The burst this replaces was 102,940 at once, and what arrived was 1,067.
    """
    catalogue = _a_catalogue(102_940)
    bus, tick, _standing = _conveyor(monkeypatch, catalogue, cycle_seconds=1800.0)

    clock = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    tick()

    largest = 0
    for _ in range(60):
        before = len(bus.published)
        clock[0] += 1.0
        tick()
        largest = max(largest, len(bus.published) - before)

    assert largest <= 532, f"a slice of {largest} would overflow a consumer's buffer"
    assert largest > 0, "the conveyor published nothing at all"


def test_a_long_pause_does_not_become_the_burst_this_replaces(monkeypatch):
    """A part starved of CPU for a while must not then flood every consumer."""
    catalogue = _a_catalogue(2_000)
    bus, tick, _standing = _conveyor(monkeypatch, catalogue, cycle_seconds=100.0)

    clock = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    tick()

    clock[0] += 10_000.0  # the machine was busy for nearly three hours
    tick()

    assert len(bus.published) <= len(catalogue)


def test_the_standing_shows_the_conveyor_turning(monkeypatch):
    """A conveyor that has stopped must be visible, not merely quiet (Rule 8)."""
    catalogue = _a_catalogue(2_000)
    bus, tick, standing = _conveyor(monkeypatch, catalogue, cycle_seconds=100.0)

    clock = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    tick()
    for _ in range(10):
        clock[0] += 1.0
        tick()

    reported = standing()
    assert reported["listings_seen"] == 2_000
    assert reported["listings_restated"] == len(bus.published) > 0
    assert reported["listings_per_second"] == 20.0
