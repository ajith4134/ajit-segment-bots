"""market-event-reader: turning venue announcements into things a part can act on.

Venues announce delistings, contract changes, maintenance windows, leverage-tier
revisions and margin-asset changes. Each is a fact that invalidates something a
model learned, and each arrives as prose in whatever shape the venue chose.

This part reads announcements and produces **events with a type, a time and the
symbols they touch** -- because "there was an announcement" is not actionable and
"BTCUSDT's maximum leverage falls to 20x at 08:00 UTC on the 24th" is.

**It classifies, and refuses to guess.** An announcement it cannot classify is
published as unclassified with its text, not dropped and not forced into the
nearest category. A delisting misfiled as maintenance would let a position ride
into a symbol that stops existing, and the silent failure is worse than the loud
one -- so unclassified events are surfaced for a person.

**An announcement without an effective time is not a scheduled event.** The
system needs to know when to act; an event with no time is a notice, and it is
published as one so nothing waits for a deadline that was never stated.

**Nothing here trades.** The reader produces facts; whether a delisting means
close now or close at the deadline is a decision belonging to parts that know
about the position.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "market-event-reader"

PART_DECLARATION = PartDeclaration(
    part_id="market-event-reader",
    consumes=("venue-announcement",),
    produces=("market-event", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

DELISTING = "delisting"
NEW_LISTING = "new-listing"
LEVERAGE_CHANGE = "leverage-or-margin-tier-change"
FUNDING_CHANGE = "funding-interval-or-cap-change"
MAINTENANCE = "scheduled-maintenance"
SETTLEMENT = "contract-settlement"
UNCLASSIFIED = "unclassified"

# What each type is matched on. Kept as data rather than as branches so a venue
# that renames its announcements is a change to this table -- and so the patterns
# can be read by whoever has to check them against a real announcement feed.
CLASSIFIERS = (
    (DELISTING, (r"\bdelist", r"\bremov(e|al) of", r"\bwill be removed", r"\bterminat")),
    (NEW_LISTING, (r"\blist(ing|s)?\b", r"\blaunch", r"\bwill be added")),
    (LEVERAGE_CHANGE, (r"\bleverage", r"\bmargin tier", r"\bmaintenance margin")),
    (FUNDING_CHANGE, (r"\bfunding (rate|interval|cap)", r"\bfunding fee")),
    (SETTLEMENT, (r"\bsettl(e|ement)", r"\bexpir")),
    (MAINTENANCE, (r"\bmaintenance", r"\bupgrade", r"\bsuspend", r"\bhalt")),
)

# A last resort for a venue that publishes free text and resolves nothing.
# Symbols look like BTCUSDT, 1000PEPEUSDT, ETH-PERP. Deliberately narrow: a
# looser pattern pulls ordinary words out of prose and attaches an event to a
# symbol nobody mentioned -- and that is exactly why it is not extended to NSE
# names. "RELIANCE", "TRENT" and "LT" are also ordinary words, and a pattern
# loose enough to catch them would attach a delisting notice to any capitalised
# word in a sentence.
#
# The real answer is that a symbol should not be re-derived here at all:
# `exchange-announcement-reader` already resolves the symbols an announcement
# names **against the declared universe**, which is the only reliable way to do
# it, and publishes them on the announcement. This part ignored that and ran the
# pattern below over a text blob instead -- so on the Indian market it matched
# nothing and every market event was published touching NO symbols (2026-09-12).
SYMBOL_PATTERN = re.compile(r"\b[0-9]{0,4}[A-Z]{2,10}(?:USDT|USDC|BUSD|PERP|-PERP)\b")


@dataclass(frozen=True)
class VenueAnnouncement:
    """One announcement, as the venue published it."""

    venue_id: str
    title: str
    body: str
    published_at_ns: int
    url: str | None = None
    effective_at_ns: int | None = None
    # The symbols the announcement reader already resolved against the declared
    # universe. Empty when the source resolved none, which is a different fact
    # from "this announcement names none" -- `symbols_in` falls back to the
    # pattern there and says which route it took.
    symbols: tuple = ()


@dataclass(frozen=True)
class MarketEvent:
    """One classified fact, with when it takes effect and what it touches."""

    venue_id: str
    event_type: str
    symbols: tuple
    effective_at_ns: int | None
    published_at_ns: int
    is_scheduled: bool
    title: str
    source_url: str | None
    reason: str
    read_at_ns: int

    @property
    def is_classified(self) -> bool:
        return self.event_type != UNCLASSIFIED

    @property
    def needs_a_person(self) -> bool:
        """Unclassified events are surfaced, because a silent miss is the worse failure."""
        return not self.is_classified

    def seconds_until_effective(self, now_ns: int) -> float | None:
        if self.effective_at_ns is None:
            return None
        return (self.effective_at_ns - now_ns) / 1e9


@dataclass
class ReaderStanding:
    announcements_read: int = 0
    events_published: int = 0
    unclassified: int = 0
    unscheduled: int = 0
    symbols_extracted: int = 0
    # Which route the symbols came by (2026-09-12). Kept apart because they are
    # different levels of trust: the source matched against the declared
    # universe, the pattern guessed from text. A board reading only the total
    # cannot tell an announcement whose symbols were resolved from one whose
    # symbols were scraped -- and on this market the pattern resolves nothing at
    # all, which is how every event came to be published touching no symbols.
    symbols_from_the_source: int = 0
    symbols_from_the_pattern: int = 0
    by_type: dict = field(default_factory=dict)
    by_venue: dict = field(default_factory=dict)


class MarketEventReader:
    """Classifies announcements into typed events, and refuses to guess."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._compiled = tuple(
            (event_type, tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns))
            for event_type, patterns in CLASSIFIERS
        )
        self._seen: set[tuple] = set()
        self.standing = ReaderStanding()

    def classify(self, announcement: VenueAnnouncement) -> str:
        """The first type whose patterns match. Unclassified rather than nearest."""
        text = f"{announcement.title}\n{announcement.body}"
        for event_type, patterns in self._compiled:
            if any(pattern.search(text) for pattern in patterns):
                return event_type
        return UNCLASSIFIED

    def symbols_in(self, announcement: VenueAnnouncement) -> tuple:
        """The symbols this announcement names.

        Resolved upstream wherever possible. `exchange-announcement-reader`
        matches an announcement's text against the declared universe, which
        handles "NIFTY 24500 CE" and "NIFTY24500CE" being the same instrument
        and a substring match being wrong -- work this part cannot repeat,
        because it holds no universe.
        """
        if announcement.symbols:
            self.standing.symbols_from_the_source += 1
            return tuple(sorted(set(announcement.symbols)))
        text = f"{announcement.title}\n{announcement.body}"
        found = tuple(sorted(set(SYMBOL_PATTERN.findall(text))))
        if found:
            self.standing.symbols_from_the_pattern += 1
        return found

    def read(self, announcement: VenueAnnouncement) -> MarketEvent | None:
        """One announcement. A repeat of one already read produces nothing."""
        self.standing.announcements_read += 1
        fingerprint = (announcement.venue_id, announcement.title, announcement.published_at_ns)
        if fingerprint in self._seen:
            return None
        self._seen.add(fingerprint)

        event_type = self.classify(announcement)
        symbols = self.symbols_in(announcement)
        self.standing.symbols_extracted += len(symbols)

        if event_type == UNCLASSIFIED:
            self.standing.unclassified += 1
        if announcement.effective_at_ns is None:
            self.standing.unscheduled += 1

        self.standing.events_published += 1
        self.standing.by_type[event_type] = self.standing.by_type.get(event_type, 0) + 1
        self.standing.by_venue[announcement.venue_id] = (
            self.standing.by_venue.get(announcement.venue_id, 0) + 1
        )

        return MarketEvent(
            venue_id=announcement.venue_id,
            event_type=event_type,
            symbols=symbols,
            effective_at_ns=announcement.effective_at_ns,
            published_at_ns=announcement.published_at_ns,
            is_scheduled=announcement.effective_at_ns is not None,
            title=announcement.title,
            source_url=announcement.url,
            reason=(
                f"{announcement.venue_id}: {event_type}"
                + (f" affecting {', '.join(symbols)}" if symbols else " affecting no named symbol")
                + (
                    ", scheduled"
                    if announcement.effective_at_ns is not None
                    else ", with no stated effective time -- a notice rather than a deadline, so "
                    "nothing should wait for one"
                )
                + (
                    ". This could not be classified and is surfaced for a person rather than "
                    "filed under the nearest category: a delisting misfiled as maintenance "
                    "would let a position ride into a symbol that stops existing"
                    if event_type == UNCLASSIFIED
                    else ""
                )
            ),
            read_at_ns=self._now_ns(),
        )

    def read_all(self, announcements) -> tuple:
        events = []
        for announcement in announcements:
            event = self.read(announcement)
            if event is not None:
                events.append(event)
        return tuple(events)


def describe_market_events(reader: MarketEventReader) -> dict:
    return {
        "part_id": PART_ID,
        "announcements_read": reader.standing.announcements_read,
        "events_published": reader.standing.events_published,
        "unclassified_needing_a_person": reader.standing.unclassified,
        "events_with_no_effective_time": reader.standing.unscheduled,
        "symbols_extracted": reader.standing.symbols_extracted,
        "symbols_from_the_source": reader.standing.symbols_from_the_source,
        "symbols_from_the_pattern": reader.standing.symbols_from_the_pattern,
        "by_type": dict(sorted(reader.standing.by_type.items())),
        "by_venue": dict(sorted(reader.standing.by_venue.items())),
        "event_types": [event_type for event_type, _ in CLASSIFIERS] + [UNCLASSIFIED],
    }


def run_market_event_reader(
    reader: MarketEventReader, control_socket, read_announcements, publish_events,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_events(reader.read_all(read_announcements()))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_market_events(reader),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The announcement reader publishes what it read with the announcement
    inside; this part classifies that announcement's headline and kind.
    """
    from runtime.input_assembly import Batch

    reads = Batch(read=context.bus.reader("venue-announcement"))
    publish_events = context.bus.publisher_for("market-event")
    reader = MarketEventReader()

    def read_announcements():
        announcements = []
        for read in reads.payloads():
            announcement = getattr(read, "announcement", read)
            if announcement is None:
                continue
            announcements.append(
                VenueAnnouncement(
                    venue_id=str(announcement.venue_id),
                    title=str(getattr(announcement, "headline", getattr(announcement, "title", ""))),
                    # The kind alone. The symbols used to be appended here so a
                    # regex could find them again, which was a round trip through
                    # text that lost every NSE name (2026-09-12); they are carried
                    # as themselves below.
                    body=str(getattr(announcement, "kind", getattr(announcement, "body", ""))),
                    symbols=tuple(str(s) for s in getattr(announcement, "symbols", ()) or ()),
                    published_at_ns=int(announcement.published_at_ns),
                    url=getattr(announcement, "source_reference", getattr(announcement, "url", None)),
                    effective_at_ns=announcement.effective_at_ns,
                )
            )
        return tuple(announcements)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_events(kept)

    return run_market_event_reader(
        reader=reader,
        control_socket=context.control_socket,
        read_announcements=read_announcements,
        publish_events=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
