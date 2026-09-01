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
