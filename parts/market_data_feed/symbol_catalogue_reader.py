"""symbol-catalogue-reader: which symbols exist, and which of them we capture.

The list is read from the venue on an interval and is never written into code.
The reason is a measurement rather than a principle: Binance reported 169
tokenised-equity perpetuals on one call and 170 minutes later on the next, on
2026-08-21, while the count was being taken. A symbol list in a source file is
stale the moment it is written, and every symbol missing from it is history that
accrues only in real time and cannot be fetched afterwards.

Selection is a named policy, not a list (spec §4.1): a count from
`captured_symbol_count` and an ordering from `symbol_selection_metric`. Setting
the count to `0` captures every symbol the venue lists, which is the
full-universe path the user asked to keep open -- with no code change at all,
though §4.2's file-descriptor ceiling has to be dealt with before it is used.

Every contract type is kept. Binance lists ~170 tokenised equities that look
exactly like crypto perpetuals from outside -- `AAPLUSDT` is quoted in USDT and
marked active -- and the user's ruling on 2026-08-21 was to capture them and
filter at order time, because capture is irreversible and a filter is not. What
is excluded is only what the venue says is on its way out.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.venues.venue_adapter import SymbolListing, VenueAdapter

PART_ID = "symbol-catalogue-reader"

PART_DECLARATION = PartDeclaration(
    part_id="symbol-catalogue-reader",
    consumes=(),
    produces=("symbol-universe", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

# What `captured_symbol_count` means when it is this. Spelled out rather than
# compared to a bare zero at the point of use, because "every symbol the venue
# lists" is a policy and `0` is how the operator writes it.
CAPTURE_EVERY_SYMBOL = 0

# How many catalogue pages to follow before refusing to continue. Not a venue
# limit -- it is a bound on a loop whose exit condition is a cursor the venue
# controls, so a venue that returned a cursor forever would otherwise be an
# unbounded request loop against an IP-banning rate limit. At the largest page
# size either venue serves, this covers many times the whole live universe.
MAXIMUM_CATALOGUE_PAGES = 20


# The one ordering this reader knows how to apply. A settings file naming any
# other metric is refused rather than silently ordered by this one: capturing
# the wrong 30 symbols is irreversible, and it would look exactly like capturing
# the right ones.
QUOTE_VOLUME_24H = "quote-volume-24h"


class SymbolSelectionRefused(ValueError):
    """The selection policy asked for something this reader cannot honestly do."""


class CatalogueIncomplete(RuntimeError):
    """The venue had more pages of contracts than this reader was willing to follow.

    Raised rather than returning what arrived. A truncated universe is the worst
    kind of wrong here: the symbols it leaves out are captured by nobody, and
    nothing in the response says any are missing.
    """


@dataclass(frozen=True)
class CapturableSymbol:
    """One symbol chosen for capture, with the figure that chose it."""

    venue_id: str
    symbol: str
    contract_type: str
    quote_volume_24h: float | None
    price_increment: float | None


@dataclass
class CatalogueStanding:
    """What the last read actually saw. Every field is counted, none asserted."""

    venue_id: str
    reads_completed: int = 0
    catalogue_pages: int = 0
    listings_seen: int = 0
    capturable_seen: int = 0
    selected: int = 0
    without_volume: int = 0
    contract_types_seen: dict[str, int] = field(default_factory=dict)
    last_failure: str | None = None
    read_at_ns: int | None = None


def fetch_json(url: str, timeout_seconds: float) -> object:
    """One public GET, decoded. No key is held anywhere in phase 1."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def select_capturable_symbols(
    adapter: VenueAdapter,
    listings: tuple[SymbolListing, ...],
    quote_volumes: dict[str, float],
    captured_symbol_count: int,
    selection_metric: str,
    standing: CatalogueStanding | None = None,
) -> tuple[CapturableSymbol, ...]:
    """Apply the settings policy to one venue's catalogue. Pure, so it is testable.

    A symbol the venue lists but the ticker does not price is kept and counted
    rather than dropped: it sorts last, because an unknown volume is not a zero
    one, and the count of them is reported so a venue that stopped pricing half
    its symbols is visible rather than merely quiet.
    """
    if selection_metric != QUOTE_VOLUME_24H:
        raise SymbolSelectionRefused(
            f"symbol_selection_metric is {selection_metric!r}, and this reader can only order by "
            f"{QUOTE_VOLUME_24H!r}. Ordering by the wrong metric captures the wrong symbols, "
            f"which is not recoverable later -- so it refuses rather than falling back."
        )
    if captured_symbol_count < CAPTURE_EVERY_SYMBOL:
        raise SymbolSelectionRefused(
            f"captured_symbol_count is {captured_symbol_count}; it is a count of symbols, and "
            f"{CAPTURE_EVERY_SYMBOL} already means every symbol the venue lists"
        )

    capturable = [listing for listing in listings if adapter.is_symbol_capturable(listing)]
    chosen = [
        CapturableSymbol(
            venue_id=adapter.venue_id,
            symbol=listing.symbol,
            contract_type=listing.contract_type,
            quote_volume_24h=quote_volumes.get(listing.symbol),
            price_increment=listing.price_increment,
        )
        for listing in capturable
    ]
    # Descending volume, with unpriced symbols last and ties broken by name so
    # two runs over the same catalogue select the same symbols.
    chosen.sort(key=lambda entry: (-(entry.quote_volume_24h or 0.0), entry.symbol))
    if captured_symbol_count != CAPTURE_EVERY_SYMBOL:
        chosen = chosen[:captured_symbol_count]

    if standing is not None:
        standing.listings_seen = len(listings)
        standing.capturable_seen = len(capturable)
        standing.selected = len(chosen)
        standing.without_volume = sum(1 for entry in chosen if entry.quote_volume_24h is None)
        types: dict[str, int] = {}
        for listing in capturable:
            types[listing.contract_type] = types.get(listing.contract_type, 0) + 1
        standing.contract_types_seen = types
    return tuple(chosen)


class SymbolCatalogueReader:
    """Re-reads one venue's catalogue on an interval and holds the current selection."""

    def __init__(
        self,
        adapter: VenueAdapter,
        captured_symbol_count: int,
        selection_metric: str,
        request_timeout_seconds: float,
        fetch=fetch_json,
        monotonic=None,
    ) -> None:
        import time

        self._adapter = adapter
        self._captured_symbol_count = captured_symbol_count
        self._selection_metric = selection_metric
        self._request_timeout_seconds = request_timeout_seconds
        self._fetch = fetch
        self._monotonic = monotonic or time.monotonic
        self._time_ns = time.time_ns
        self.standing = CatalogueStanding(venue_id=adapter.venue_id)
        self._selection: tuple[CapturableSymbol, ...] = ()

    @property
    def selection(self) -> tuple[CapturableSymbol, ...]:
        """The symbols currently chosen for capture, empty until a read succeeds.

        Empty is a real state and is reported as one: a reader that had never
        completed a read would otherwise be indistinguishable from a venue that
        lists nothing (Rule 8).
        """
        return self._selection

    def _read_every_catalogue_page(self) -> tuple[SymbolListing, ...]:
        """Follow the venue's own cursor until the catalogue is whole.

        Bybit serves 500 instruments by default against 837 live symbols, and a
        page without a cursor looks exactly like a complete catalogue. Binance
        paginates nothing and answers None on the first page, so this loop runs
        once there -- the shape is the same for both, which is what keeps the
        difference out of this part.
        """
        listings: list[SymbolListing] = []
        cursor: str | None = None
        for page in range(MAXIMUM_CATALOGUE_PAGES):
            response = self._fetch(
                self._adapter.catalogue_url(cursor), self._request_timeout_seconds
            )
            listings.extend(self._adapter.read_symbol_listings(response))
            cursor = self._adapter.read_catalogue_cursor(response)
            if cursor is None:
                self.standing.catalogue_pages = page + 1
                return tuple(listings)
        raise CatalogueIncomplete(
            f"{self._adapter.venue_id} still offered a page after {MAXIMUM_CATALOGUE_PAGES}, "
            f"having returned {len(listings)} contracts. Capturing a prefix of a universe is "
            f"worse than failing to read it, because nothing downstream can tell the difference."
        )

    def read_catalogue(self) -> tuple[CapturableSymbol, ...]:
        """Fetch both responses, apply the policy, and keep what came back.

        On a failed fetch the previous selection stands and the failure is
        recorded. Dropping to an empty selection because one request timed out
        would stop the capture of every symbol over a transient error -- and
        those minutes are not recoverable.
        """
        try:
            listings = self._read_every_catalogue_page()
            tickers = self._fetch(self._adapter.ticker_url(), self._request_timeout_seconds)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as failure:
            self.standing.last_failure = f"{type(failure).__name__}: {failure}"
            return self._selection

        volumes = dict(self._adapter.read_quote_volumes(tickers))
        self._selection = select_capturable_symbols(
            adapter=self._adapter,
            listings=listings,
            quote_volumes=volumes,
            captured_symbol_count=self._captured_symbol_count,
            selection_metric=self._selection_metric,
            standing=self.standing,
        )
        self.standing.reads_completed += 1
        self.standing.read_at_ns = self._time_ns()
        self.standing.last_failure = None
        return self._selection


def run_symbol_catalogue_reader(
    adapter: VenueAdapter,
    control_socket,
    captured_symbol_count: int,
    selection_metric: str,
    refresh_interval_seconds: float,
    request_timeout_seconds: float,
    health_interval_seconds: float,
    emit_health,
) -> int:
    """Run this part until the governor turns it off, re-reading on its interval.

    The tick fires at the health interval, which is far shorter than the refresh
    interval, so the reader checks whether a re-read is due rather than sleeping
    through it. A part that slept for its whole interval would be a part the
    governor could not switch for that long, which is T-2 lost.
    """
    import time

    reader = SymbolCatalogueReader(
        adapter=adapter,
        captured_symbol_count=captured_symbol_count,
        selection_metric=selection_metric,
        request_timeout_seconds=request_timeout_seconds,
    )
    last_read_at = [None]

    def read_if_due() -> None:
        now = time.monotonic()
        if last_read_at[0] is None or now - last_read_at[0] >= refresh_interval_seconds:
            reader.read_catalogue()
            last_read_at[0] = now

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=read_if_due,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )


def describe_catalogue(reader: SymbolCatalogueReader) -> dict:
    """Everything measured about the last read. Nothing here asserts health."""
    return {
        "part_id": PART_ID,
        "venue_id": reader.standing.venue_id,
        "reads_completed": reader.standing.reads_completed,
        "catalogue_pages": reader.standing.catalogue_pages,
        "read_at_ns": reader.standing.read_at_ns,
        "listings_seen": reader.standing.listings_seen,
        "capturable_seen": reader.standing.capturable_seen,
        "selected": reader.standing.selected,
        "selected_without_volume": reader.standing.without_volume,
        "contract_types_seen": dict(reader.standing.contract_types_seen),
        "last_failure": reader.standing.last_failure,
        "symbols": [entry.symbol for entry in reader.selection],
    }


__all__ = [
    "CAPTURE_EVERY_SYMBOL",
    "CatalogueIncomplete",
    "MAXIMUM_CATALOGUE_PAGES",
    "CapturableSymbol",
    "CatalogueStanding",
    "PART_DECLARATION",
    "PART_ID",
    "QUOTE_VOLUME_24H",
    "SymbolCatalogueReader",
    "SymbolSelectionRefused",
    "describe_catalogue",
    "fetch_json",
    "run_symbol_catalogue_reader",
    "select_capturable_symbols",
]
