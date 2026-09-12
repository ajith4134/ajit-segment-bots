"""broker-news-reader: read whatever news the broker's own API carries.

**The head of the news feature, and until 2026-09-12 the feature had no head.**
29 parts are declared in `stock-market-news-data`; five ran; and *nothing
produced `raw-news-item` at all*. Every part below a source — the deduplicator,
the structurer, the symbol resolver, the sentiment model, the impact forecaster
— was starved at the top of the chain, so building any of them first would have
achieved nothing measurable. This is the source.

Its own design document says it is "declared but built last, after each broker's
news endpoint is verified against primary docs", and that is what happened:

    GET https://api.upstox.com/v2/news
        ?category=instrument_keys
        &instrument_keys=<comma separated, MAXIMUM 30>
        &page_number=<1-100>  &page_size=<1-100>
    Authorization: Bearer <access token>

verified against upstox.com/developer/api-documentation/get-news on 2026-09-12,
and exercised against the real endpoint the same day: 17 items came back for
RELIANCE and HDFCBANK, under the fields `heading`, `summary`, `thumbnail`,
`article_link` and `published_time`.

## Three facts about that API that shape this part

**Thirty keys per request.** The two option segments track 220 underlyings, so
asking about all of them is eight requests, not one. This part asks about a
bounded slice per tick and rotates through the rest, rather than issuing eight
calls in one tick — a part that blocks its own loop on I/O cannot be switched
off (T-2), which is exactly what `book-and-paper-fetcher` was found doing on the
same day.

**`published_time` is epoch MILLISECONDS.** Everything else in this project
reasons in nanoseconds. Mixing the two is the timestamp trap `clock-skew-monitor`
exists to catch, so the conversion happens once, in `runtime/news_types.py`, and
an item with no stamp gets None rather than zero — zero is 1970, which would
make an undated item look like the oldest news in the system.

**The response is keyed by instrument key, not a flat list.** An item can appear
under more than one key, and the same story reaches this part more than once.
Deduplication is `news-item-deduplicator`'s job and is deliberately not done
here: this part reports what the source said, including that it said it twice.

## What this part does not do

It does not read, summarise, classify or resolve anything. The title and body
are the source's own words, handed on unparsed — `news-text-structurer` is the
one LLM read in this feature, and doing it in the fetch path would put a model
between the broker and the tape.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from runtime.brokers.broker_http_request import build_broker_request
from runtime.news_types import RawNewsItem, published_at_ns_from_milliseconds
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "broker-news-reader"

PART_DECLARATION = PartDeclaration(
    part_id="broker-news-reader",
    consumes=("broker-instrument-listing", "broker-token-standing"),
    produces=("raw-news-item", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

# Upstox's own cap, from its own documentation. Named rather than inlined
# because exceeding it is not a slow request, it is a refused one.
MAXIMUM_INSTRUMENT_KEYS_PER_REQUEST = 30

NEWS_ENDPOINT = "https://api.upstox.com/v2/news"
BY_INSTRUMENT_KEYS = "instrument_keys"

# The source id every item from this reader carries. One string, because a
# `raw-news-item` has to say where it came from and "the broker" is the honest
# answer -- Upstox aggregates several outlets and does not say which.
SOURCE_ID = "upstox-news-api"

# What news can be ABOUT. A company or an index has news; an option contract
# does not -- there is no such thing as a story about "NIFTY 24550 CE 08 SEP
# 26", only a story about NIFTY. The broker's instrument master is 118,388 rows
# of which 95,496 are CE, PE or futures contracts, so a reader that asked about
# every listing would spend 97% of its requests on instruments no story can
# name, and would take hours to come back round to the ones that matter.
#
# Upstox's own instrument_type words, read off the real master 2026-09-12:
# 2,655 EQ shares and 216 INDEX entries, 2,871 together.
INSTRUMENT_TYPES_NEWS_CAN_BE_ABOUT = ("EQ", "INDEX")


@dataclass
class ReaderStanding:
    requests_made: int = 0
    items_read: int = 0
    items_with_no_published_time: int = 0
    keys_known: int = 0
    keys_asked_about: int = 0
    # Listings skipped because no story can be about them -- option and futures
    # contracts. 95,496 of the master's 118,388 rows on 2026-09-12.
    listings_that_news_cannot_be_about: int = 0
    # Ticks that did nothing because the pace had not elapsed. A large number
    # here is the part working, not stalling: news does not change every tick
    # and the endpoint is rate limited.
    ticks_inside_the_pace: int = 0
    failures: int = 0
    last_failure: str | None = None
    refused_no_token: int = 0
    # True while no instrument listing has arrived, so there is nothing to ask
    # about. A different fact from "asked and got nothing", and the two would
    # otherwise both read as zero items (Rule 8).
    waiting_for_the_first_instrument_listing: bool = True


class BrokerNewsReader:
    """Asks the broker for news about the instruments it has been told exist."""

    def __init__(
        self,
        fetch,
        keys_per_request: int = MAXIMUM_INSTRUMENT_KEYS_PER_REQUEST,
        seconds_between_requests: float = 10.0,
        now_ns=time.time_ns,
    ) -> None:
        if not 1 <= keys_per_request <= MAXIMUM_INSTRUMENT_KEYS_PER_REQUEST:
            raise ValueError(
                f"the broker accepts at most {MAXIMUM_INSTRUMENT_KEYS_PER_REQUEST} "
                f"instrument keys per request and at least one; {keys_per_request} is "
                f"not a request it would answer"
            )
        if seconds_between_requests < 0:
            raise ValueError("a negative pace is not a pace")
        self._fetch = fetch
        self._keys_per_request = keys_per_request
        self._seconds_between_requests = seconds_between_requests
        self._last_request_at_ns: int | None = None
        self._now_ns = now_ns
        # Insertion-ordered, so the rotation below is stable across ticks and
        # every instrument is eventually asked about rather than the first
        # thirty being asked about forever.
        self._keys: dict[str, None] = {}
        self._next_key_index = 0
        self.standing = ReaderStanding()

    def observe_listing(self, listing) -> None:
        """One instrument listing. Its key is something news can be asked about."""
        key = getattr(listing, "instrument_key", None)
        if not key:
            return
        if getattr(listing, "instrument_type", None) not in INSTRUMENT_TYPES_NEWS_CAN_BE_ABOUT:
            # A contract, not a thing news is about. Counted so the standing
            # shows the filter working rather than the reader looking idle.
            self.standing.listings_that_news_cannot_be_about += 1
            return
        if key not in self._keys:
            self._keys[key] = None
            self.standing.keys_known = len(self._keys)
            self.standing.waiting_for_the_first_instrument_listing = False

    def keys_for_this_tick(self) -> tuple[str, ...]:
        """The next slice of instruments to ask about, rotating through them all.

        Rotating rather than slicing from the front: at 220 underlyings and 30
        keys a request, asking about the first thirty every tick would mean the
        other 190 never had their news read, and nothing would report it --
        `items_read` would climb happily.
        """
        keys = list(self._keys)
        if not keys:
            return ()
        start = self._next_key_index % len(keys)
        slice_ = keys[start:start + self._keys_per_request]
        if len(slice_) < self._keys_per_request:
            # Wrap, so a rotation that runs off the end continues from the
            # beginning rather than returning a short request forever.
            slice_ += keys[: self._keys_per_request - len(slice_)]
        self._next_key_index = (start + len(slice_)) % len(keys)
        return tuple(dict.fromkeys(slice_))

    def read(self, access_token: str | None) -> tuple[RawNewsItem, ...]:
        """One request's worth of news, as the broker delivered it."""
        if not access_token:
            self.standing.refused_no_token += 1
            return ()

        # **Paced.** Measured on the live spine 2026-09-12 before this existed:
        # 371 requests in 110 seconds, 3.37 a second, which is about 291,000
        # calls a day at a news endpoint that is rate limited. Nothing failed
        # yet, and that is exactly the shape of a problem that arrives as a ban
        # rather than as an error -- the same reasoning
        # `api-key-pool-rotator`'s own note gives for resting a key.
        #
        # News does not change between ticks. A tick inside the pace does
        # nothing and says so, rather than looking idle.
        now = self._now_ns()
        if self._last_request_at_ns is not None:
            elapsed = (now - self._last_request_at_ns) / 1e9
            if elapsed < self._seconds_between_requests:
                self.standing.ticks_inside_the_pace += 1
                return ()

        keys = self.keys_for_this_tick()
        if not keys:
            return ()

        self.standing.keys_asked_about += len(keys)
        self._last_request_at_ns = now
        try:
            response = self._fetch(request_url_for(keys), access_token)
        except Exception as failure:  # noqa: BLE001 - one fact: the read failed
            self.standing.failures += 1
            self.standing.last_failure = f"{type(failure).__name__}: {failure}"
            return ()

        self.standing.requests_made += 1
        return self.items_in(response)

    def items_in(self, response) -> tuple[RawNewsItem, ...]:
        """Every item in one response, keyed by the instrument it arrived under.

        The response is a mapping of instrument key to a list of items, not a
        flat list, and one story reaches this part under several keys. That
        repetition is preserved: `news-item-deduplicator` collapses it, and
        collapsing here would hide that the source said it twice.
        """
        observed_at_ns = self._now_ns()
        data = (response or {}).get("data") or {}
        items: list[RawNewsItem] = []
        for instrument_key, rows in data.items():
            for row in rows or ():
                published_at_ns = published_at_ns_from_milliseconds(
                    row.get("published_time")
                )
                if published_at_ns is None:
                    # Recorded, not dropped: an item with no stamp is still news
                    # and `news-latency-meter` should see that it arrived
                    # undated rather than never see it at all.
                    self.standing.items_with_no_published_time += 1
                    published_at_ns = observed_at_ns
                items.append(RawNewsItem(
                    source_id=SOURCE_ID,
                    url=str(row.get("article_link") or ""),
                    title=str(row.get("heading") or ""),
                    # The summary is the body this source gives. Upstox returns
                    # no full text, and calling a summary the body is honest
                    # where inventing one would not be -- news-text-structurer
                    # reads whatever is here.
                    body=str(row.get("summary") or ""),
                    published_at_ns=published_at_ns,
                    observed_at_ns=observed_at_ns,
                    returned_under_instrument_key=str(instrument_key),
                ))
        self.standing.items_read += len(items)
        return tuple(items)


def request_url_for(instrument_keys) -> str:
    """The broker's own news URL for these instruments.

    The keys carry a pipe (`NSE_EQ|INE002A01018`) and MUST be percent-encoded or
    the query is not the query that was asked for -- the same trap the
    historical-candle URL already carries a note about.
    """
    keys = tuple(instrument_keys)
    if not keys:
        raise ValueError("a news request with no instrument keys asks about nothing")
    if len(keys) > MAXIMUM_INSTRUMENT_KEYS_PER_REQUEST:
        raise ValueError(
            f"{len(keys)} instrument keys, and the broker accepts "
            f"{MAXIMUM_INSTRUMENT_KEYS_PER_REQUEST}; a request above the cap is refused "
            f"rather than truncated, so truncating here would hide which instruments "
            f"were never asked about"
        )
    query = urllib.parse.urlencode({
        "category": BY_INSTRUMENT_KEYS,
        "instrument_keys": ",".join(keys),
    })
    return f"{NEWS_ENDPOINT}?{query}"


def fetch_news(url: str, access_token: str, timeout_seconds: float) -> dict:
    """One authenticated GET to the broker's news endpoint."""
    request = build_broker_request(url, access_token=access_token)
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read())


def describe_reading(reader: BrokerNewsReader) -> dict:
    return {
        "part_id": PART_ID,
        "requests_made": reader.standing.requests_made,
        "items_read": reader.standing.items_read,
        "items_with_no_published_time": reader.standing.items_with_no_published_time,
        "keys_known": reader.standing.keys_known,
        "keys_asked_about": reader.standing.keys_asked_about,
        "listings_that_news_cannot_be_about": (
            reader.standing.listings_that_news_cannot_be_about
        ),
        "ticks_inside_the_pace": reader.standing.ticks_inside_the_pace,
        "failures": reader.standing.failures,
        "last_failure": reader.standing.last_failure,
        "refused_no_token": reader.standing.refused_no_token,
        "waiting_for_the_first_instrument_listing": (
            reader.standing.waiting_for_the_first_instrument_listing
        ),
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.brokers.upstox import UpstoxAdapter
    from runtime.input_assembly import Batch, LatestByKey

    adapter = UpstoxAdapter()
    listings = Batch(read=context.bus.reader("broker-instrument-listing"))
    tokens = LatestByKey(
        read=context.bus.reader("broker-token-standing"),
        key_of=lambda standing: standing.broker_id,
        maximum_age_seconds=context.number("broker_token_standing_maximum_age"),
    )
    publish_items = context.bus.publisher_for("raw-news-item")
    timeout_seconds = context.number("broker_news_timeout_seconds")

    reader = BrokerNewsReader(
        fetch=lambda url, token: fetch_news(url, token, timeout_seconds),
        keys_per_request=int(context.number("broker_news_instruments_per_request")),
        seconds_between_requests=context.number("broker_news_seconds_between_requests"),
    )

    def tick() -> None:
        for listing in listings.payloads():
            reader.observe_listing(listing)
        tokens.take_in_what_arrived()
        token = tokens.mapping().get(adapter.broker_id)
        access_token = (
            token.access_token if token is not None and token.is_still_valid() else None
        )
        items = reader.read(access_token)
        if items:
            publish_items(items)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_reading(reader),
    )


__all__ = [
    "BY_INSTRUMENT_KEYS",
    "BrokerNewsReader",
    "INSTRUMENT_TYPES_NEWS_CAN_BE_ABOUT",
    "MAXIMUM_INSTRUMENT_KEYS_PER_REQUEST",
    "NEWS_ENDPOINT",
    "PART_DECLARATION",
    "PART_ID",
    "ReaderStanding",
    "SOURCE_ID",
    "describe_reading",
    "fetch_news",
    "request_url_for",
    "start_part",
]
