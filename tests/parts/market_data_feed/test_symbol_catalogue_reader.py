"""Which symbols get captured, against both venues' real catalogues and tickers.

Every response replayed here is one the venue actually returned on 2026-08-22,
subset but not edited, with the rule that selected the entries recorded in
`tests/captured/capture-manifest.json`. The ticker fixtures deliberately keep the
highest-volume symbols, because the ordering is the thing under test and a
fixture of whichever symbols the venue listed first would exercise the merge
without exercising the selection.
"""

import pytest

from parts.market_data_feed.symbol_catalogue_reader import (
    MAXIMUM_CATALOGUE_PAGES,
    CatalogueIncomplete,
    CAPTURE_EVERY_SYMBOL,
    PART_DECLARATION,
    PART_ID,
    QUOTE_VOLUME_24H,
    SymbolCatalogueReader,
    SymbolSelectionRefused,
    describe_catalogue,
    select_capturable_symbols,
)
from runtime.part_declaration import load_declaration_from_blueprint
from runtime.venues.adapter_registry import load_venue_adapter

CATALOGUE_FIXTURES = {
    "binance-usdm": "2026-08-22-catalogue-subset.json",
    "bybit-linear": "2026-08-22-catalogue-subset.json",
}
TICKER_FIXTURE = "2026-08-22-ticker-24h-subset.json"
CAPTURED_SYMBOL_COUNT = 30
REQUEST_TIMEOUT = 30.0


def load_real_responses(venue_id, read_captured_json):
    return (
        read_captured_json(venue_id, CATALOGUE_FIXTURES[venue_id]),
        read_captured_json(venue_id, TICKER_FIXTURE),
    )


def build_reader(venue_id, read_captured_json, count=CAPTURED_SYMBOL_COUNT, metric=QUOTE_VOLUME_24H):
    adapter = load_venue_adapter(venue_id)
    catalogue, tickers = load_real_responses(venue_id, read_captured_json)
    responses = {adapter.catalogue_url(): catalogue, adapter.ticker_url(): tickers}

    def fetch(url, _timeout):
        return responses[url]

    return SymbolCatalogueReader(
        adapter=adapter,
        captured_symbol_count=count,
        selection_metric=metric,
        request_timeout_seconds=REQUEST_TIMEOUT,
        fetch=fetch,
    )


def test_the_built_wiring_equals_the_blueprint():
    assert PART_DECLARATION == load_declaration_from_blueprint(PART_ID)
    assert PART_DECLARATION.consumes == (), "this part reads the venue, not another part"


@pytest.mark.parametrize("venue_id", sorted(CATALOGUE_FIXTURES))
def test_a_read_selects_symbols_and_records_what_it_saw(venue_id, read_captured_json):
    reader = build_reader(venue_id, read_captured_json)
    assert reader.selection == (), "a reader that has not read yet selects nothing"

    selection = reader.read_catalogue()
    assert selection
    assert reader.standing.reads_completed == 1
    assert reader.standing.read_at_ns is not None
    assert reader.standing.last_failure is None
    assert reader.standing.capturable_seen <= reader.standing.listings_seen
    assert reader.standing.selected == len(selection)


@pytest.mark.parametrize("venue_id", sorted(CATALOGUE_FIXTURES))
def test_selection_is_ordered_by_real_quote_volume(venue_id, read_captured_json):
    """The ordering is the whole policy, and the wrong order is the wrong symbols."""
    reader = build_reader(venue_id, read_captured_json)
    selection = reader.read_catalogue()
    priced = [entry.quote_volume_24h for entry in selection if entry.quote_volume_24h is not None]
    assert priced, "no selected symbol carried a volume, so the ordering is untested"
    assert priced == sorted(priced, reverse=True)
    # An unpriced symbol sorts last rather than as a zero: unknown is not none.
    volumes = [entry.quote_volume_24h for entry in selection]
    first_unpriced = next((index for index, value in enumerate(volumes) if value is None), None)
    if first_unpriced is not None:
        assert all(value is None for value in volumes[first_unpriced:])


@pytest.mark.parametrize("venue_id", sorted(CATALOGUE_FIXTURES))
def test_only_capturable_symbols_are_selected(venue_id, read_captured_json):
    """A contract on its way to delisting has no history left to accrue."""
    adapter = load_venue_adapter(venue_id)
    catalogue, tickers = load_real_responses(venue_id, read_captured_json)
    listings = adapter.read_symbol_listings(catalogue)
    volumes = dict(adapter.read_quote_volumes(tickers))

    selection = select_capturable_symbols(
        adapter=adapter,
        listings=listings,
        quote_volumes=volumes,
        captured_symbol_count=CAPTURE_EVERY_SYMBOL,
        selection_metric=QUOTE_VOLUME_24H,
    )
    selected = {entry.symbol for entry in selection}
    for listing in listings:
        assert (listing.symbol in selected) is adapter.is_symbol_capturable(listing)


def test_tokenised_equities_are_captured_and_marked(read_captured_json):
    """The user's ruling: capture them, decide tradeability when there is something to trade."""
    adapter = load_venue_adapter("binance-usdm")
    catalogue, tickers = load_real_responses("binance-usdm", read_captured_json)
    selection = select_capturable_symbols(
        adapter=adapter,
        listings=adapter.read_symbol_listings(catalogue),
        quote_volumes=dict(adapter.read_quote_volumes(tickers)),
        captured_symbol_count=CAPTURE_EVERY_SYMBOL,
        selection_metric=QUOTE_VOLUME_24H,
    )
    types = {entry.contract_type for entry in selection}
    assert "TRADIFI_PERPETUAL" in types
    assert "PERPETUAL" in types
    # And the type travels with the symbol, so a later phase can separate them
    # without re-reading the venue.
    equities = [entry for entry in selection if entry.contract_type == "TRADIFI_PERPETUAL"]
    assert all(entry.symbol.endswith("USDT") for entry in equities)


@pytest.mark.parametrize("venue_id", sorted(CATALOGUE_FIXTURES))
def test_a_count_of_zero_captures_every_listed_symbol(venue_id, read_captured_json):
    """The full-universe path the user asked to keep open, with no code change."""
    limited = build_reader(venue_id, read_captured_json, count=2).read_catalogue()
    everything = build_reader(venue_id, read_captured_json, count=CAPTURE_EVERY_SYMBOL).read_catalogue()
    assert len(limited) == 2
    assert len(everything) > len(limited)
    assert [entry.symbol for entry in limited] == [entry.symbol for entry in everything[:2]]


def test_a_metric_this_reader_cannot_apply_is_refused(read_captured_json):
    """Silently falling back would capture the wrong symbols and look identical."""
    reader = build_reader("binance-usdm", read_captured_json, metric="whatever-sorts-first")
    with pytest.raises(SymbolSelectionRefused) as refusal:
        reader.read_catalogue()
    assert "whatever-sorts-first" in str(refusal.value)


def test_a_negative_count_is_refused(read_captured_json):
    reader = build_reader("binance-usdm", read_captured_json, count=-1)
    with pytest.raises(SymbolSelectionRefused):
        reader.read_catalogue()


def test_a_failed_fetch_keeps_the_last_selection_and_says_so(read_captured_json):
    """A transient error must not stop the capture of every symbol."""
    reader = build_reader("binance-usdm", read_captured_json)
    good = reader.read_catalogue()
    assert good

    def refuse(_url, _timeout):
        raise TimeoutError("the venue did not answer")

    reader._fetch = refuse
    after = reader.read_catalogue()
    assert after == good, "a failed refresh dropped the symbols it had"
    assert reader.standing.reads_completed == 1
    assert "TimeoutError" in reader.standing.last_failure


def test_a_symbol_the_ticker_does_not_price_is_kept_and_counted(read_captured_json):
    """Unknown volume is not zero volume, and the count of them is reported."""
    adapter = load_venue_adapter("binance-usdm")
    catalogue, _ = load_real_responses("binance-usdm", read_captured_json)
    listings = adapter.read_symbol_listings(catalogue)
    from parts.market_data_feed.symbol_catalogue_reader import CatalogueStanding

    standing = CatalogueStanding(venue_id=adapter.venue_id)
    selection = select_capturable_symbols(
        adapter=adapter,
        listings=listings,
        quote_volumes={},
        captured_symbol_count=CAPTURE_EVERY_SYMBOL,
        selection_metric=QUOTE_VOLUME_24H,
        standing=standing,
    )
    assert selection, "every symbol was dropped for want of a volume"
    assert standing.without_volume == len(selection)
    assert all(entry.quote_volume_24h is None for entry in selection)


def test_the_description_reports_only_what_was_counted(read_captured_json):
    reader = build_reader("bybit-linear", read_captured_json)
    reader.read_catalogue()
    description = describe_catalogue(reader)
    assert description["part_id"] == PART_ID
    assert description["venue_id"] == "bybit-linear"
    assert description["selected"] == len(description["symbols"])
    assert description["contract_types_seen"]
    assert description["last_failure"] is None


def test_a_paginated_catalogue_is_followed_to_the_end(read_captured_json):
    """Bybit serves 500 of 837 by default, and a prefix looks like a whole universe.

    Replayed as two pages of the real catalogue, so what is under test is that
    the reader follows the venue's own cursor rather than trusting the first
    response it gets.
    """
    adapter = load_venue_adapter("bybit-linear")
    catalogue, tickers = load_real_responses("bybit-linear", read_captured_json)
    entries = catalogue["result"]["list"]
    assert len(entries) > 2, "the fixture is too small to split into pages"
    half = len(entries) // 2

    def page(entries_kept, cursor):
        body = {"result": dict(catalogue["result"])}
        body["result"]["list"] = entries_kept
        body["result"]["nextPageCursor"] = cursor
        return body

    pages = {
        adapter.catalogue_url(): page(entries[:half], "page-two"),
        adapter.catalogue_url("page-two"): page(entries[half:], ""),
        adapter.ticker_url(): tickers,
    }

    reader = SymbolCatalogueReader(
        adapter=adapter,
        captured_symbol_count=CAPTURE_EVERY_SYMBOL,
        selection_metric=QUOTE_VOLUME_24H,
        request_timeout_seconds=REQUEST_TIMEOUT,
        fetch=lambda url, _timeout: pages[url],
    )
    selection = reader.read_catalogue()
    assert reader.standing.catalogue_pages == 2
    assert reader.standing.listings_seen == len(entries)
    assert len(selection) > half - 1


def test_a_catalogue_that_never_ends_is_refused_rather_than_truncated(read_captured_json):
    """Capturing a prefix is worse than failing: nothing downstream can tell."""
    adapter = load_venue_adapter("bybit-linear")
    catalogue, tickers = load_real_responses("bybit-linear", read_captured_json)
    endless = {"result": dict(catalogue["result"])}
    endless["result"]["nextPageCursor"] = "always-another"

    reader = SymbolCatalogueReader(
        adapter=adapter,
        captured_symbol_count=CAPTURE_EVERY_SYMBOL,
        selection_metric=QUOTE_VOLUME_24H,
        request_timeout_seconds=REQUEST_TIMEOUT,
        fetch=lambda url, _timeout: tickers if url == adapter.ticker_url() else endless,
    )
    with pytest.raises(CatalogueIncomplete) as refusal:
        reader.read_catalogue()
    assert str(MAXIMUM_CATALOGUE_PAGES) in str(refusal.value)


def test_the_venue_that_paginates_nothing_reads_one_page(read_captured_json):
    reader = build_reader("binance-usdm", read_captured_json)
    reader.read_catalogue()
    assert reader.standing.catalogue_pages == 1
    assert load_venue_adapter("binance-usdm").read_catalogue_cursor({"symbols": []}) is None
