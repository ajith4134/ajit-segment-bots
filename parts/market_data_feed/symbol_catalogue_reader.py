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

import dataclasses
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from runtime.level_publishing import LevelPublisher, LevelPublisherByKey
from runtime.part_context import RUNTIME_SCOPE as RUNTIME_SCOPE_NAME
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.symbol_universe import CapturableSymbol
from runtime.venues.venue_adapter import ContractFunding, SymbolListing, VenueAdapter

PART_ID = "symbol-catalogue-reader"

PART_DECLARATION = PartDeclaration(
    part_id="symbol-catalogue-reader",
    consumes=("position",),
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


# The orderings this reader knows how to apply. A settings file naming any
# other metric is refused rather than silently ordered by one of these: capturing
# the wrong symbols is irreversible, and it would look exactly like capturing
# the right ones.
QUOTE_VOLUME_24H = "quote-volume-24h"
# Volume and volatility, blended by percentile rank within a volume-qualified
# pool -- never by raw magnitude, which would let volume (in the billions)
# swamp volatility (a fraction near 1). See _rank_by_volume_and_volatility.
VOLUME_AND_VOLATILITY_BLEND = "volume-and-volatility-blend"
KNOWN_SELECTION_METRICS = (QUOTE_VOLUME_24H, VOLUME_AND_VOLATILITY_BLEND)


class SymbolSelectionRefused(ValueError):
    """The selection policy asked for something this reader cannot honestly do."""


class CatalogueIncomplete(RuntimeError):
    """The venue had more pages of contracts than this reader was willing to follow.

    Raised rather than returning what arrived. A truncated universe is the worst
    kind of wrong here: the symbols it leaves out are captured by nobody, and
    nothing in the response says any are missing.
    """


@dataclass
class CatalogueStanding:
    """What the last read actually saw. Every field is counted, none asserted."""

    venue_id: str
    reads_completed: int = 0
    catalogue_pages: int = 0
    listings_seen: int = 0
    capturable_seen: int = 0
    selected: int = 0
    # Symbols in the universe only because the bot is holding them -- below the
    # volume cut and kept anyway. A number that is not zero is the feed doing the
    # thing this exists for, so it belongs on health rather than in a comment.
    kept_because_held: int = 0
    without_volume: int = 0
    without_volatility: int = 0
    # Both partial by design, not a fault: momentum is venue-asymmetric (Bybit
    # states it, Binance does not), and the acceleration scan only covers
    # whatever slice of the pool the rotation has reached so far.
    with_momentum: int = 0
    short_window_scanned: int = 0
    short_window_scan_failure: str | None = None
    # How many selected symbols the venue quoted no funding rate for, and how many
    # it quoted a rate for but no settlement interval. Counted rather than
    # asserted: a perpetual missing either cannot have its carry priced, and the
    # part that refuses it should not be the first place that becomes visible.
    without_funding_rate: int = 0
    without_funding_interval: int = 0
    contract_types_seen: dict[str, int] = field(default_factory=dict)
    last_failure: str | None = None
    # A funding read that failed while the catalogue read succeeded. Separate from
    # `last_failure` because the consequences are different and must not be
    # confused: a failed catalogue read stops symbols being captured, which is
    # irrecoverable, while a failed funding read only leaves carry unpriced for
    # one refresh -- so the first keeps the previous selection and the second
    # publishes the symbols anyway.
    funding_failure: str | None = None
    # What happened when the maintenance margin ladder was last read. The
    # unavailable-reason is the venue's own, and is safe to print: it names the
    # environment variables that were looked for, never any value found in them.
    margin_symbols_read: int = 0
    margin_requests_made: int = 0
    margin_failure: str | None = None
    margin_unavailable_reason: str | None = None
    read_at_ns: int | None = None


def fetch_json(url: str, timeout_seconds: float, headers: dict | None = None) -> object:
    """One GET, decoded.

    `headers` carries whatever the adapter says this venue's endpoint needs --
    an authentication header for a signed endpoint, nothing for a public one.
    The adapter builds them because it knows how its venue authenticates; this
    function only knows how to fetch, and neither has to learn the other's half.
    Nothing here logs a header: an authentication header is a credential.
    """
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", **(headers or {})}
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def _with_margin_tiers(
    symbol: CapturableSymbol, tiers: tuple, source: str | None
) -> CapturableSymbol:
    """The same symbol, carrying the maintenance margin ladder the venue published.

    Unchanged when the venue's schedule could not be read. An empty ladder is
    never filled in: a maintenance margin of zero puts a liquidation price at the
    entry, so a defaulted ladder does not make a liquidation map slightly wrong,
    it makes every cluster in it wrong in the same direction.
    """
    if not tiers:
        return symbol
    return dataclasses.replace(symbol, margin_tiers=tuple(tiers), margin_source=source)


def _with_funding(
    symbol: CapturableSymbol, funding: ContractFunding | None
) -> CapturableSymbol:
    """The same symbol, carrying what the venue said holding it costs.

    Unchanged when the venue quoted no rate for it. That is the state of a dated
    contract, which pays no funding at all, and of a symbol whose funding read
    failed -- and the two are told apart downstream by whether the read failed,
    which `CatalogueStanding.funding_failure` records, rather than by guessing
    here.
    """
    if funding is None:
        return symbol
    return dataclasses.replace(
        symbol,
        funding_rate_per_settlement=funding.rate_per_settlement,
        funding_settlements_per_day=funding.settlements_per_day,
        funding_source=funding.source,
        funding_rate_cap=funding.rate_cap,
        funding_rate_floor=funding.rate_floor,
        funding_interest_rate_per_interval=funding.interest_rate_per_interval,
    )


def _rank_pool_by_blended_percentiles(
    chosen: list[CapturableSymbol],
    liquidity_pool_size: int,
    weighted_signals: tuple[tuple, ...],
) -> list[CapturableSymbol]:
    """Blend volume with zero or more other signals by percentile rank, never by
    raw magnitude.

    Volume runs from thousands to billions of USDT; a 24-hour range fraction
    runs from zero to a few; a percent change can be negative. Adding any of
    these to volume as they stand would let volume decide the order by itself
    -- so each is first turned into where a symbol sits among its pool peers
    (0.0 = best, 1.0 = worst on that one signal), and only the percentiles are
    blended, in shares that sum to 1.0 (volume takes whatever the named signals
    do not spend).

    The liquidity floor comes first and is not negotiable under this metric:
    only the top `liquidity_pool_size` by volume are eligible at all, so a
    thin, hard-to-fill symbol cannot outrank a liquid one purely by having
    spiked. `weighted_signals` is `((key_function, weight), ...)` -- each
    `key_function` reads the value to rank a pool entry by (already signed or
    made absolute by the caller, e.g. momentum ranked by its magnitude rather
    than its direction). A symbol with no reading for a signal ranks as the
    pool's worst on that signal rather than being dropped, since its volume
    reading already qualified it as tradeable.
    """
    by_volume = sorted(chosen, key=lambda entry: (-(entry.quote_volume_24h or 0.0), entry.symbol))
    priced = [entry for entry in by_volume if entry.quote_volume_24h is not None]
    unpriced = [entry for entry in by_volume if entry.quote_volume_24h is None]
    pool = priced[:liquidity_pool_size]
    outside_pool = priced[liquidity_pool_size:] + unpriced

    pool_size = len(pool)
    if pool_size <= 1:
        return pool + outside_pool

    def percentile_rank(key) -> dict[str, float]:
        ordered = sorted(pool, key=lambda entry: (-(key(entry) or 0.0), entry.symbol))
        return {entry.symbol: i / (pool_size - 1) for i, entry in enumerate(ordered)}

    volume_weight = 1.0 - sum(weight for _, weight in weighted_signals)
    percentiles = [(percentile_rank(lambda entry: entry.quote_volume_24h), volume_weight)]
    percentiles.extend((percentile_rank(key), weight) for key, weight in weighted_signals)

    def blended_score(entry: CapturableSymbol) -> float:
        return sum(pct[entry.symbol] * weight for pct, weight in percentiles)

    ranked_pool = sorted(pool, key=lambda entry: (blended_score(entry), entry.symbol))
    return ranked_pool + outside_pool


def select_capturable_symbols(
    adapter: VenueAdapter,
    listings: tuple[SymbolListing, ...],
    quote_volumes: dict[str, float],
    captured_symbol_count: int,
    selection_metric: str,
    funding: Mapping[str, ContractFunding] | None = None,
    standing: CatalogueStanding | None = None,
    held_symbols=(),
    volatility: Mapping[str, float] | None = None,
    liquidity_pool_size: int | None = None,
    volatility_weight: float | None = None,
    momentum: Mapping[str, float] | None = None,
    momentum_weight: float = 0.0,
    short_window_acceleration: Mapping[str, float] | None = None,
    acceleration_weight: float = 0.0,
) -> tuple[CapturableSymbol, ...]:
    """Apply the settings policy to one venue's catalogue. Pure, so it is testable.

    A symbol the venue lists but the ticker does not price is kept and counted
    rather than dropped: it sorts last, because an unknown volume is not a zero
    one, and the count of them is reported so a venue that stopped pricing half
    its symbols is visible rather than merely quiet.

    `momentum` and `short_window_acceleration` are optional on top of volume and
    24-hour volatility. Both are asymmetric by venue (Binance states nothing
    shorter than 24h in bulk; the acceleration scan only covers whatever slice
    of the pool has been rotated through so far) -- when a mapping is entirely
    empty, its weight is silently redistributed to volume rather than spent on
    a signal that would rank every symbol identically anyway.
    """
    if selection_metric not in KNOWN_SELECTION_METRICS:
        raise SymbolSelectionRefused(
            f"symbol_selection_metric is {selection_metric!r}, and this reader can only order by "
            f"one of {KNOWN_SELECTION_METRICS!r}. Ordering by the wrong metric captures the wrong "
            f"symbols, which is not recoverable later -- so it refuses rather than falling back."
        )
    if captured_symbol_count < CAPTURE_EVERY_SYMBOL:
        raise SymbolSelectionRefused(
            f"captured_symbol_count is {captured_symbol_count}; it is a count of symbols, and "
            f"{CAPTURE_EVERY_SYMBOL} already means every symbol the venue lists"
        )
    if selection_metric == VOLUME_AND_VOLATILITY_BLEND:
        if not liquidity_pool_size or liquidity_pool_size < captured_symbol_count:
            raise SymbolSelectionRefused(
                f"symbol_selection_liquidity_pool_size is {liquidity_pool_size!r}; the blend "
                f"metric needs a pool of at least captured_symbol_count "
                f"({captured_symbol_count}) volume-qualified symbols to rank within"
            )
        if volatility_weight is None or not 0.0 <= volatility_weight <= 1.0:
            raise SymbolSelectionRefused(
                f"symbol_selection_volatility_weight is {volatility_weight!r}; it must sit in [0, 1]"
            )
        if not 0.0 <= momentum_weight <= 1.0:
            raise SymbolSelectionRefused(
                f"symbol_selection_momentum_weight is {momentum_weight!r}; it must sit in [0, 1]"
            )
        if not 0.0 <= acceleration_weight <= 1.0:
            raise SymbolSelectionRefused(
                f"symbol_selection_acceleration_weight is {acceleration_weight!r}; it must sit "
                f"in [0, 1]"
            )
        spent = volatility_weight + momentum_weight + acceleration_weight
        if spent > 1.0:
            raise SymbolSelectionRefused(
                f"volatility_weight + momentum_weight + acceleration_weight is {spent!r}, which "
                f"would leave a negative share for volume; the three must sum to at most 1.0"
            )

    capturable = [listing for listing in listings if adapter.is_symbol_capturable(listing)]
    held = frozenset(held_symbols or ())
    funding = funding or {}
    volatility = volatility or {}
    momentum = momentum or {}
    short_window_acceleration = short_window_acceleration or {}
    chosen = [
        _with_funding(
            CapturableSymbol(
                venue_id=adapter.venue_id,
                symbol=listing.symbol,
                contract_type=listing.contract_type,
                quote_volume_24h=quote_volumes.get(listing.symbol),
                price_increment=listing.price_increment,
                instrument_kind=listing.instrument_kind,
                volatility_24h=volatility.get(listing.symbol),
                momentum_1h=momentum.get(listing.symbol),
                short_window_acceleration=short_window_acceleration.get(listing.symbol),
            ),
            funding.get(listing.symbol),
        )
        for listing in capturable
    ]
    if selection_metric == VOLUME_AND_VOLATILITY_BLEND:
        weighted_signals = [(lambda entry: entry.volatility_24h, volatility_weight)]
        if momentum:
            weighted_signals.append((lambda entry: abs(entry.momentum_1h) if entry.momentum_1h is not None else None, momentum_weight))
        if short_window_acceleration:
            weighted_signals.append((lambda entry: entry.short_window_acceleration, acceleration_weight))
        chosen = _rank_pool_by_blended_percentiles(chosen, liquidity_pool_size, tuple(weighted_signals))
    else:
        # Descending volume, with unpriced symbols last and ties broken by name so
        # two runs over the same catalogue select the same symbols.
        chosen.sort(key=lambda entry: (-(entry.quote_volume_24h or 0.0), entry.symbol))
    if captured_symbol_count != CAPTURE_EVERY_SYMBOL:
        # The rank cut, then whatever is held put back. A symbol the bot is
        # holding is captured whatever its volume, because a position that cannot
        # be priced cannot be stopped out, cannot have its excursion measured, and
        # cannot be valued by risk -- and every one of those failures is silent.
        #
        # Measured 2026-08-26: the bot held STORJUSDT on both venues and its tape
        # stopped at 2026-08-25 18:40, with no file for today at all. The universe
        # is re-selected every 900 seconds, STORJUSDT's volume had slipped below
        # rank 50, and `feed-coverage-auditor` read complete throughout -- it
        # audits coverage of the universe, and the symbol had left it.
        #
        # Added after the cut rather than sorted ahead of it, so a held symbol
        # never displaces a higher-volume one: the universe becomes "the top N by
        # volume, plus what we are still holding". It leaves on the ordinary
        # rotation once the position is flat, which is not an exception to the
        # rotation but the rotation resuming.
        ranked = chosen[:captured_symbol_count]
        inside = {entry.symbol for entry in ranked}
        kept_for_a_position = [
            entry for entry in chosen[captured_symbol_count:]
            if entry.symbol in held and entry.symbol not in inside
        ]
        chosen = ranked + kept_for_a_position
        if standing is not None:
            standing.kept_because_held = len(kept_for_a_position)

    if standing is not None:
        standing.listings_seen = len(listings)
        standing.capturable_seen = len(capturable)
        standing.selected = len(chosen)
        standing.without_volume = sum(1 for entry in chosen if entry.quote_volume_24h is None)
        standing.without_volatility = sum(1 for entry in chosen if entry.volatility_24h is None)
        standing.with_momentum = sum(1 for entry in chosen if entry.momentum_1h is not None)
        standing.short_window_scanned = sum(
            1 for entry in chosen if entry.short_window_acceleration is not None
        )
        standing.without_funding_rate = sum(
            1 for entry in chosen if entry.funding_rate_per_settlement is None
        )
        standing.without_funding_interval = sum(
            1 for entry in chosen if entry.funding_settlements_per_day is None
        )
        types: dict[str, int] = {}
        for listing in capturable:
            types[listing.contract_type] = types.get(listing.contract_type, 0) + 1
        standing.contract_types_seen = types
    return tuple(chosen)


def _short_window_acceleration(closes: tuple[float, ...], recent_bars: int) -> float | None:
    """What fraction of the whole window's own range happened in its most recent slice.

    Both ranges are measured as high-low over the same closes, so a subset's
    range can never exceed the full window's -- the ratio is bounded in [0, 1]
    by construction. Close to 1 means most of what this window moved happened
    in just the last `recent_bars`; close to 0 means the move is over and this
    symbol has been flat since. None when there are not enough bars to measure
    both a baseline and a distinct recent slice, or the baseline never moved at
    all (a symbol with zero range has no "share of it" to speak of).
    """
    if recent_bars < 1 or len(closes) <= recent_bars:
        return None
    baseline_high, baseline_low = max(closes), min(closes)
    if baseline_high == baseline_low:
        return None
    recent = closes[-recent_bars:]
    recent_high, recent_low = max(recent), min(recent)
    return (recent_high - recent_low) / (baseline_high - baseline_low)


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
        liquidity_pool_size: int | None = None,
        volatility_weight: float | None = None,
        momentum_weight: float = 0.0,
        acceleration_weight: float = 0.0,
        short_window_scan_size: int = 0,
        short_window_kline_interval: str = "5m",
        short_window_kline_count: int = 12,
        short_window_recent_bars: int = 3,
    ) -> None:
        import time

        self._adapter = adapter
        self._captured_symbol_count = captured_symbol_count
        self._selection_metric = selection_metric
        self._request_timeout_seconds = request_timeout_seconds
        self._fetch = fetch
        self._monotonic = monotonic or time.monotonic
        self._time_ns = time.time_ns
        # Only meaningful under VOLUME_AND_VOLATILITY_BLEND; select_capturable_symbols
        # validates them itself when that metric is asked for (T-4: this class
        # threads settings through, it does not re-decide what they mean).
        self._liquidity_pool_size = liquidity_pool_size
        self._volatility_weight = volatility_weight
        self._momentum_weight = momentum_weight
        self._acceleration_weight = acceleration_weight
        self._short_window_scan_size = short_window_scan_size
        self._short_window_kline_interval = short_window_kline_interval
        self._short_window_kline_count = short_window_kline_count
        self._short_window_recent_bars = short_window_recent_bars
        # Held in memory alone, deliberately: unlike a checkpointed price series,
        # losing this on a restart costs at most one rotation's worth of scans
        # (up to liquidity_pool_size / short_window_scan_size refreshes) before
        # the picture rebuilds, which is a mild cost next to what a lost lot book
        # or a lost regime series costs -- so it is not checkpointed in this pass.
        self._short_window_scores: dict[str, float] = {}
        self._short_window_rotation_position = 0
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

    def _read_funding(self, listings, tickers) -> Mapping[str, ContractFunding]:
        """What the venue says each contract costs to hold, or nothing if it would not say.

        Its failure is caught separately from the catalogue's and does not stop
        the read, because the two cost different things when they go wrong. A
        catalogue read that failed would capture no symbols for that interval and
        those minutes are not recoverable; a funding read that failed leaves carry
        unpriced until the next refresh, and the part that prices carry refuses an
        unpriced instrument by name rather than assuming it is free.

        On a venue that publishes funding in what has already been fetched this
        makes no request at all -- `funding_request_urls` is empty there.
        """
        try:
            responses = [
                self._fetch(url, self._request_timeout_seconds)
                for url in self._adapter.funding_request_urls()
            ]
            facts = self._adapter.read_funding_facts(listings, tickers, responses)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError, KeyError) as failure:
            self.standing.funding_failure = f"{type(failure).__name__}: {failure}"
            return {}
        self.standing.funding_failure = None
        return facts

    def _scan_short_window_momentum(self, pool_symbols: Sequence[str]) -> None:
        """Refresh a rotating slice of the pool's acceleration reading.

        Neither venue bulk-serves candles shorter than 24h, so this is one REST
        call per symbol scanned -- bounded to `short_window_scan_size` and
        rotated through the pool rather than scanning it all at once, the same
        reasoning `cointegration-pair-finder` rotates through pairs for. A
        symbol not reached this cycle keeps whatever reading a previous cycle
        gave it; a symbol never reached at all stays None, which
        `select_capturable_symbols` treats as "not yet scanned", not as flat.

        A failed fetch stops the scan for this cycle rather than raising: the
        catalogue read that starts this must not fail over a single symbol's
        candle request timing out, the same reasoning `_read_margin_tiers` uses.
        """
        if self._short_window_scan_size <= 0 or not pool_symbols:
            return
        pool_symbols = list(pool_symbols)
        start = self._short_window_rotation_position % len(pool_symbols)
        slice_ = pool_symbols[start:start + self._short_window_scan_size]
        if len(slice_) < self._short_window_scan_size:
            slice_ += pool_symbols[: self._short_window_scan_size - len(slice_)]
        self._short_window_rotation_position = (start + len(slice_)) % len(pool_symbols)

        requests = self._adapter.short_window_kline_requests(
            slice_, self._short_window_kline_interval, self._short_window_kline_count
        )
        if not requests:
            return

        paired = []
        for request in requests:
            try:
                paired.append(
                    (request.describes, self._fetch(request.url, self._request_timeout_seconds))
                )
            except (urllib.error.URLError, OSError, TimeoutError, ValueError) as failure:
                self.standing.short_window_scan_failure = (
                    f"{type(failure).__name__} reading recent klines for "
                    f"{request.describes}: {failure}"
                )
                break
        if not paired:
            return

        closes_by_symbol = self._adapter.read_short_window_klines(paired)
        for symbol, closes in closes_by_symbol.items():
            score = _short_window_acceleration(closes, self._short_window_recent_bars)
            if score is not None:
                self._short_window_scores[symbol] = score
        self.standing.short_window_scan_failure = None

    @property
    def selection(self) -> tuple:
        """What this reader last selected, so a caller can see what is missing."""
        return self._selection

    @property
    def venue_id(self) -> str:
        """Which venue this reader reads, so a caller need not open its adapter."""
        return self._adapter.venue_id

    def _read_margin_tiers(
        self, selection: tuple[CapturableSymbol, ...]
    ) -> tuple[CapturableSymbol, ...]:
        """Attach each captured symbol's maintenance margin ladder, or say why not.

        **A failure here never costs the selection.** The ladder is one input to
        one downstream map; the selection is what the whole capture runs on, and
        dropping it because a margin endpoint timed out would stop the tape for a
        number nothing else needs. So a failure is recorded and the symbols are
        returned without the ladder, which downstream tells apart from a zero rate
        because an absent ladder is an empty tuple and never a zero.
        """
        self.standing.margin_unavailable_reason = self._adapter.margin_schedule_unavailable_reason()
        requests = self._adapter.margin_schedule_requests(
            [symbol.symbol for symbol in selection]
        )
        self.standing.margin_requests_made = len(requests)
        if not requests:
            self.standing.margin_symbols_read = 0
            return selection

        responses = []
        for request in requests:
            try:
                responses.append(
                    self._fetch(request.url, self._request_timeout_seconds, request.headers)
                )
            except (urllib.error.URLError, OSError, TimeoutError, ValueError) as failure:
                # Named by what it was asking about rather than by URL: a signed
                # URL carries a signature, and a signature in a log is a
                # credential in a log.
                self.standing.margin_failure = (
                    f"{type(failure).__name__} reading the margin schedule for "
                    f"{request.describes}: {failure}"
                )
                break

        if not responses:
            self.standing.margin_symbols_read = 0
            return selection

        schedule = self._adapter.read_margin_tiers(responses)
        source = f"{self._adapter.venue_id} margin schedule, {len(responses)} response(s)"
        attached = tuple(
            _with_margin_tiers(symbol, schedule.get(symbol.symbol, ()), source)
            for symbol in selection
        )
        self.standing.margin_symbols_read = sum(1 for s in attached if s.margin_tiers)
        if self.standing.margin_symbols_read:
            self.standing.margin_failure = None
        return attached

    def read_catalogue(self, held_symbols=()) -> tuple[CapturableSymbol, ...]:
        """Fetch both responses, apply the policy, and keep what came back.

        On a failed fetch the previous selection stands and the failure is
        recorded. Dropping to an empty selection because one request timed out
        would stop the capture of every symbol over a transient error -- and
        those minutes are not recoverable.

        `held_symbols` are kept whatever their volume rank. They are passed in
        rather than looked up, because what the bot holds is not a fact about a
        venue catalogue and this reader has no business knowing where it comes
        from (T-4).
        """
        try:
            listings = self._read_every_catalogue_page()
            tickers = self._fetch(self._adapter.ticker_url(), self._request_timeout_seconds)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as failure:
            self.standing.last_failure = f"{type(failure).__name__}: {failure}"
            return self._selection

        volumes = dict(self._adapter.read_quote_volumes(tickers))
        volatility = dict(self._adapter.read_volatility_facts(tickers))
        momentum = dict(self._adapter.read_momentum_facts(tickers))
        funding = self._read_funding(listings, tickers)

        if self._selection_metric == VOLUME_AND_VOLATILITY_BLEND and self._acceleration_weight > 0:
            # The same volume-qualified pool select_capturable_symbols itself
            # will rank -- computed again here, cheaply (a sort of symbol names),
            # because the scan has to run before the selection it feeds.
            capturable_symbols = [
                listing.symbol for listing in listings if self._adapter.is_symbol_capturable(listing)
            ]
            pool_symbols = sorted(
                (symbol for symbol in capturable_symbols if volumes.get(symbol) is not None),
                key=lambda symbol: -volumes[symbol],
            )[: self._liquidity_pool_size or 0]
            self._scan_short_window_momentum(pool_symbols)

        selection = select_capturable_symbols(
            adapter=self._adapter,
            listings=listings,
            quote_volumes=volumes,
            captured_symbol_count=self._captured_symbol_count,
            selection_metric=self._selection_metric,
            funding=funding,
            standing=self.standing,
            held_symbols=held_symbols,
            volatility=volatility,
            liquidity_pool_size=self._liquidity_pool_size,
            volatility_weight=self._volatility_weight,
            momentum=momentum,
            momentum_weight=self._momentum_weight,
            short_window_acceleration=dict(self._short_window_scores),
            acceleration_weight=self._acceleration_weight,
        )
        # The ladder is read for the symbols actually being captured, after the
        # selection has chosen them. Asked before, this would be one request per
        # symbol the venue lists -- ~840 on Bybit against the 50 anything watches.
        self._selection = self._read_margin_tiers(selection)
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
    publish_universe=None,
    restatement_interval_seconds: float = 30.0,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
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
    # The universe is a level, and a level nobody restates is an event. It was
    # published only on a read, so a consumer that started between two reads had
    # an empty universe for up to the whole refresh interval -- fifteen minutes.
    # Measured on the live spine at 14:41 on 2026-08-26, four minutes after a
    # restart: this reader had completed both venues' catalogues and selected 104
    # symbols, instrument-selector had received **zero** symbol-universe messages
    # and registered no listings, and every one of the 794 intents the arbiter
    # formed was refused for having no instrument. The venue's list is re-read on
    # the REST interval; what it says is restated on this one.
    restated = LevelPublisher(
        publish=publish_universe or (lambda items: None),
        refresh_interval_seconds=restatement_interval_seconds,
    )

    def read_if_due() -> None:
        now = time.monotonic()
        if last_read_at[0] is None or now - last_read_at[0] >= refresh_interval_seconds:
            reader.read_catalogue()
            last_read_at[0] = now
        if publish_universe is not None and reader.selection:
            # In full rather than as a diff, and on a cadence rather than on a
            # change: a consumer that joined after the last read has no way to ask
            # for what it missed, and nothing else can tell it that its empty
            # universe is missing rather than empty.
            restated.publish_level(reader.selection)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=read_if_due,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_catalogue(reader),
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
        "kept_because_held": reader.standing.kept_because_held,
        "selected_without_volume": reader.standing.without_volume,
        "selected_without_volatility": reader.standing.without_volatility,
        "selected_with_momentum": reader.standing.with_momentum,
        "selected_short_window_scanned": reader.standing.short_window_scanned,
        "short_window_scan_failure": reader.standing.short_window_scan_failure,
        "selected_without_funding_rate": reader.standing.without_funding_rate,
        "selected_without_funding_interval": reader.standing.without_funding_interval,
        "funding_failure": reader.standing.funding_failure,
        # Rule 8: a liquidation map built without a margin schedule is not a
        # smaller map, it is a wrong one -- so how many symbols actually got a
        # ladder, and the reason when none did, are on the board rather than
        # inferable only from an empty map downstream.
        "margin_symbols_read": reader.standing.margin_symbols_read,
        "margin_requests_made": reader.standing.margin_requests_made,
        "margin_failure": reader.standing.margin_failure,
        "margin_unavailable_reason": reader.standing.margin_unavailable_reason,
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
    "restate_each_venues_universe",
    "run_symbol_catalogue_reader",
    "select_capturable_symbols",
]


def restate_each_venues_universe(readers, restated) -> int:
    """Say again what each venue currently lists, and answer how many spoke.

    The venue's list is re-read on the REST interval; this is what is said in
    between. `symbol-universe` is a level -- these are the symbols this system
    captures, now -- and a level published only when it is re-read is an event to
    everyone who was not listening at that moment.

    A reader with nothing selected says nothing rather than saying an empty
    universe: never read and lists nothing are different facts, and only one of
    them means a consumer should stop looking for instruments (Rule 8).
    """
    spoke = 0
    for reader in readers:
        if reader.selection and restated.publish_level(reader.venue_id, reader.selection):
            spoke += 1
    return spoke


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    One part, every captured venue. The blueprint declares a single
    symbol-catalogue-reader, and which venues it reads is a settings question
    (spec §3.1) -- so this runs one catalogue reader per adapter named in
    `captured_venues` and publishes the union. A part per venue would have been a
    part id per venue, which is a blueprint edit, not an implementation choice.

    This part consumes nothing: it is one of the twelve that start the flow rather
    than continue it, so it is woken by its own clock and passes no input
    descriptors.
    """
    import time as clock

    from runtime.venues.adapter_registry import load_captured_venue_adapters

    settings = context.settings[RUNTIME_SCOPE_NAME]
    adapters = load_captured_venue_adapters(settings)
    if not adapters:
        raise RuntimeError(
            "captured_venues names no venue this build has an adapter for, so there is no "
            "catalogue to read. The setting is the operator's; the adapters are the code's, "
            "and a mismatch between them is a fact rather than something to work around."
        )

    from runtime.input_assembly import LatestByKey

    publish_universe = context.bus.publisher_for("symbol-universe")
    # What the bot holds, per venue and symbol. `fill-reconciler` republishes every
    # held position on every tick, so this is a current picture rather than a log
    # of what was once opened -- and it carries `is_flat`, so a position that has
    # closed stops being held here without anything having to expire it.
    positions = LatestByKey(
        read=context.bus.reader("position"),
        key_of=lambda position: (position.venue_id, position.symbol),
    )
    readers = [
        SymbolCatalogueReader(
            adapter=adapter,
            captured_symbol_count=settings.entries["captured_symbol_count"].value,
            selection_metric=settings.entries["symbol_selection_metric"].value,
            request_timeout_seconds=context.number("catalogue_request_timeout"),
            # Only meaningful under VOLUME_AND_VOLATILITY_BLEND; read unconditionally
            # since select_capturable_symbols validates them itself when that
            # metric is what symbol_selection_metric actually names.
            liquidity_pool_size=int(context.number("symbol_selection_liquidity_pool_size")),
            volatility_weight=context.number("symbol_selection_volatility_weight"),
            momentum_weight=context.number("symbol_selection_momentum_weight"),
            acceleration_weight=context.number("symbol_selection_acceleration_weight"),
            short_window_scan_size=int(context.number("symbol_selection_short_window_scan_size")),
            short_window_kline_interval=settings.entries[
                "symbol_selection_short_window_kline_interval"
            ].value,
            short_window_kline_count=int(
                context.number("symbol_selection_short_window_kline_count")
            ),
            short_window_recent_bars=int(
                context.number("symbol_selection_short_window_recent_bars")
            ),
        )
        for adapter in adapters
    ]
    refresh_interval_seconds = context.number("symbol_catalogue_refresh_interval")
    # The venue's list is re-read on the REST interval; what it currently says is
    # restated on this one. The universe is a level -- these are the symbols we
    # capture, now -- and it was published only on a read, which makes it an event
    # for anybody who was not listening at that moment.
    #
    # Measured on the live spine at 14:41 on 2026-08-26, four minutes after a
    # restart: this part had read both venues and selected 104 symbols;
    # instrument-selector had received **zero** symbol-universe messages, held no
    # listings, and refused all 794 intents the arbiter had formed for having no
    # instrument to express them with. Nothing was faulty and nothing said
    # anything: the one message that carried the universe went out before its
    # reader was listening, and the next was fifteen minutes away.
    restated = LevelPublisherByKey(
        publish=publish_universe,
        refresh_interval_seconds=context.number("symbol_universe_restatement_interval"),
    )
    last_read_at = [None]
    # What was handed to each reader at its last read, so a held symbol that could
    # not be kept is not asked for again on the next tick.
    asked_for: dict[str, frozenset] = {}
    # Held symbols this venue refused to capture, per venue, for health.
    uncapturable: dict[str, list] = {}

    def held_by_venue(held: dict, venue_id: str) -> frozenset:
        return frozenset(
            symbol
            for (its_venue, symbol), position in held.items()
            if its_venue == venue_id and not position.is_flat
        )

    def read_if_due() -> None:
        # Drained every tick, not only when a catalogue read is due: the position
        # stream is how this part learns what is held, and reading it once every
        # 900 seconds would mean acting on a picture that old.
        held = positions.mapping()
        now = clock.monotonic()
        due = last_read_at[0] is None or now - last_read_at[0] >= refresh_interval_seconds
        # A held symbol missing from what is currently selected is read for now,
        # rather than at the next refresh. Two moments need it and both leave a
        # position unpriced for up to the whole interval: the first tick, where
        # the catalogue is read before fill-reconciler has republished the
        # restored book; and a symbol whose volume slips out of the cut while it
        # is held. Fifteen minutes without a price is fifteen minutes in which a
        # resting stop cannot trigger.
        #
        # Only for a symbol not already asked for. Some held symbols cannot be
        # captured at all -- measured 2026-08-26, binance-usdm still *lists*
        # STORJUSDT while `is_symbol_capturable` refuses it, so asking again can
        # never change the answer. Without this the condition never clears and the
        # part re-reads the catalogue every tick: 39 reads in five minutes, each
        # of them two or more requests to the venue, which is how a fix for a
        # silent gap becomes a rate-limit ban.
        wanted = {
            reader.venue_id: held_by_venue(held, reader.venue_id) for reader in readers
        }
        newly_missing = any(
            wanted[reader.venue_id]
            - {entry.symbol for entry in reader.selection}
            - asked_for.get(reader.venue_id, frozenset())
            for reader in readers
        )
        if not (due or newly_missing):
            # Nothing new to read, which is not the same as nothing to say. Each
            # venue's current selection is restated on its own cadence, so a
            # consumer that started a moment ago waits that long rather than the
            # rest of the refresh interval.
            restate_each_venues_universe(readers, restated)
            return
        last_read_at[0] = now
        for reader in readers:
            held_here = wanted[reader.venue_id]
            asked_for[reader.venue_id] = held_here
            selection = reader.read_catalogue(held_here)
            # Through the same publisher the restatement uses, so a read and a
            # restatement cannot disagree about when this venue last spoke. A read
            # that changed nothing is not republished here; it is already out
            # there and being restated on its cadence.
            restated.publish_level(reader.venue_id, selection)
            # A symbol the bot holds that this venue will not let us capture. Not
            # a fault in this part and not something asking again can fix -- but a
            # position on it can never be priced from this venue's stream, so it
            # is named rather than left to look like an ordinary rotation.
            uncapturable[reader.venue_id] = sorted(
                held_here - {entry.symbol for entry in reader.selection}
            )

    def describe_all_catalogues() -> dict:
        # One recorder per venue in one process; a heartbeat standing keeps
        # top-level numbers, so each venue's facts travel under a suffixed key.
        merged: dict = {"part_id": PART_ID, "venues": len(readers)}
        for venue, symbols in uncapturable.items():
            # A held position on a symbol the venue will not stream is a position
            # that cannot be priced, stopped out, or measured from that venue.
            merged[f"held_but_uncapturable.{venue}"] = float(len(symbols))
        for reader in readers:
            one = describe_catalogue(reader)
            venue = one["venue_id"] or "unread"
            for field in (
                "reads_completed", "listings_seen", "capturable_seen", "selected",
                "kept_because_held",
            ):
                merged[f"{field}.{venue}"] = one[field]
        return merged

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=read_if_due,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=describe_all_catalogues,
    )
