"""exchange-announcement-reader: the venue telling this system its model is wrong.

Every other source in this block is somebody's opinion about the market.
Announcements are not. When Binance says a symbol delists on Thursday, changes a
leverage tier, or moves a funding interval, that is the venue changing the rules
this system trades under -- and continuing to trade the old rules is not a bad
prediction, it is operating on a stale contract.

That makes freshness a correctness property here rather than a nice-to-have, which
is why this part's blueprint entry says a skipped tick corrupts. Missing a
sentiment read costs a sentiment read. Missing a delisting notice means holding a
position into a forced settlement.

Four things this part gets right that a naive feed reader does not:

- **Effective time is not publication time.** A notice published today about
  Thursday is not actionable today and is critical on Thursday. Both stamps are
  carried, and "already in effect" is a distinct state from "coming".
- **A notice names symbols, and the naming is messy.** Venues write "BTCUSDT
  perpetual", "BTC/USDT", and "BTCUSDT" for the same instrument -- or, for a
  broker, "NIFTY 24500 CE" and "NIFTY24500CE". Matching is done against the
  declared universe rather than by substring, because a substring match on
  "NIFTY" hits every option in the chain.
- **Kind matters more than text.** A delisting and a fee promotion arrive through
  the same feed. Only some kinds change how a symbol trades, and that set is
  explicit rather than inferred from wording.
- **An unrecognised kind is surfaced, not dropped.** A venue inventing a new
  category is exactly when a reader that silently ignores unknowns fails, so
  unknown kinds are counted and passed through as unclassified.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.external_research_types import VenueAnnouncement
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "exchange-announcement-reader"

PART_DECLARATION = PartDeclaration(
    part_id="exchange-announcement-reader",
    consumes=("broker-instrument-listing",),
    produces=("venue-announcement", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

DELISTING = "delisting"
LISTING = "listing"
LEVERAGE_CHANGE = "leverage-change"
FUNDING_CHANGE = "funding-change"
SETTLEMENT_CHANGE = "settlement-change"
TRADING_HALT = "trading-halt"
TICK_SIZE_CHANGE = "tick-size-change"
MAINTENANCE = "maintenance"
PROMOTION = "promotion"
UNCLASSIFIED = "unclassified"

ANNOUNCEMENT_KINDS = (
    DELISTING, LISTING, LEVERAGE_CHANGE, FUNDING_CHANGE, SETTLEMENT_CHANGE,
    TRADING_HALT, TICK_SIZE_CHANGE, MAINTENANCE, PROMOTION, UNCLASSIFIED,
)

# The subset that changes the contract this system trades under. Everything else
# is information; these are instructions.
CHANGES_THE_RULES = (
    DELISTING, LEVERAGE_CHANGE, FUNDING_CHANGE, SETTLEMENT_CHANGE, TRADING_HALT,
    TICK_SIZE_CHANGE,
)

RECORDED = "recorded"
ALREADY_SEEN = "already-recorded"
NO_SYMBOL_MATCHED = "it-names-no-symbol-in-the-universe"
ALREADY_IN_EFFECT = "the-effective-time-has-already-passed"
READ_FAILED = "read-failed"


@dataclass(frozen=True)
class AnnouncementRead:
    announcement_id: str
    state: str
    announcement: VenueAnnouncement | None
    seconds_until_effective: float | None
    changes_the_rules: bool
    reason: str
    read_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state in (RECORDED, ALREADY_IN_EFFECT) and self.announcement is not None

    @property
    def needs_acting_on_now(self) -> bool:
        """A rule change already in effect is the case a queue must not delay."""
        return self.changes_the_rules and (
            self.state == ALREADY_IN_EFFECT
            or (self.seconds_until_effective is not None
                and self.seconds_until_effective <= 0)
        )


@dataclass
class AnnouncementStanding:
    rows_seen: int = 0
    recorded: int = 0
    duplicates: int = 0
    matched_no_symbol: int = 0
    rule_changes: int = 0
    already_in_effect: int = 0
    unclassified_kinds: int = 0
    unknown_kind_names: tuple = ()
    failures: int = 0


class ExchangeAnnouncementReader:
    """Reads venue notices, matches them to the declared universe, keeps both stamps."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._universe: dict[str, set] = {}
        self._aliases: dict[str, str] = {}
        self._seen: set = set()
        self.standing = AnnouncementStanding()

    def observe_universe(self, venue_id: str, symbols) -> None:
        """Matching is against the declared universe: a substring match on BTC
        hits forty symbols, and a notice about one of them is not about all."""
        self._universe[venue_id] = set(symbols)

    def observe_alias(self, written_as: str, symbol: str) -> None:
        """Venues write BTC/USDT, BTCUSDT perpetual and BTCUSDT for one instrument."""
        self._aliases[self._normalise(written_as)] = symbol

    def symbols_named_in(self, venue_id: str, text: str) -> tuple:
        universe = self._universe.get(venue_id, set())
        normalised = self._normalise(text)
        found = set()
        for written_as, symbol in self._aliases.items():
            if written_as and written_as in normalised and symbol in universe:
                found.add(symbol)
        for symbol in universe:
            if self._normalise(symbol) in normalised:
                found.add(symbol)
        return tuple(sorted(found))

    def read(self, row) -> AnnouncementRead:
        self.standing.rows_seen += 1
        announcement_id = str(row["announcement_id"])

        if announcement_id in self._seen:
            self.standing.duplicates += 1
            return self._read(
                announcement_id, ALREADY_SEEN, None, None, False,
                "already recorded. A feed replaying a notice is one notice",
            )

        kind = row.get("kind", UNCLASSIFIED)
        if kind not in ANNOUNCEMENT_KINDS:
            self.standing.unclassified_kinds += 1
            self.standing.unknown_kind_names = tuple(
                sorted(set(self.standing.unknown_kind_names) | {str(kind)})
            )
            kind = UNCLASSIFIED

        venue_id = row["venue_id"]
        symbols = row.get("symbols")
        if symbols is None:
            symbols = self.symbols_named_in(venue_id, row.get("headline", ""))
        symbols = tuple(sorted(symbols))

        if not symbols:
            self.standing.matched_no_symbol += 1
            self._seen.add(announcement_id)
            return self._read(
                announcement_id, NO_SYMBOL_MATCHED, None, None, False,
                "it names no symbol in the declared universe. That is recorded rather "
                "than treated as an error: venues announce about instruments this system "
                "does not trade",
            )

        now = self._now_ns()
        announcement = VenueAnnouncement(
            announcement_id=announcement_id,
            venue_id=venue_id,
            symbols=symbols,
            kind=kind,
            headline=row.get("headline", ""),
            effective_at_ns=row.get("effective_at_ns"),
            published_at_ns=int(row["published_at_ns"]),
            observed_at_ns=now,
            source_reference=row.get("source_reference", f"notice:{announcement_id}"),
        )
        self._seen.add(announcement_id)
        self.standing.recorded += 1

        changes = announcement.changes_how_a_symbol_trades
        if changes:
            self.standing.rule_changes += 1

        until = None
        if announcement.effective_at_ns is not None:
            until = (announcement.effective_at_ns - now) / 1e9

        if until is not None and until <= 0:
            self.standing.already_in_effect += 1
            return self._read(
                announcement_id, ALREADY_IN_EFFECT, announcement, until, changes,
                f"{kind} on {', '.join(symbols)}, already in effect "
                f"{abs(until) / 3600.0:.1f}h ago"
                + (
                    ". The rules this system trades under have already changed, so acting "
                    "on the old ones is operating on a stale contract"
                    if changes
                    else ". Informational"
                ),
            )

        return self._read(
            announcement_id, RECORDED, announcement, until, changes,
            f"{kind} on {', '.join(symbols)}"
            + (
                f", effective in {until / 3600.0:.1f}h"
                if until is not None
                else ", with no effective time given"
            )
            + (". This changes how the symbol trades" if changes else ". Informational"),
        )

    @staticmethod
    def _normalise(text: str) -> str:
        return "".join(character for character in text.upper() if character.isalnum())

    def _read(
        self, announcement_id, state, announcement, until, changes, reason,
    ) -> AnnouncementRead:
        return AnnouncementRead(
            announcement_id=announcement_id, state=state, announcement=announcement,
            seconds_until_effective=until, changes_the_rules=changes, reason=reason,
            read_at_ns=self._now_ns(),
        )


def describe_announcement_reading(reader: ExchangeAnnouncementReader) -> dict:
    return {
        "part_id": PART_ID,
        "rows_seen": reader.standing.rows_seen,
        "recorded": reader.standing.recorded,
        "duplicates": reader.standing.duplicates,
        "matched_no_symbol": reader.standing.matched_no_symbol,
        "rule_changes": reader.standing.rule_changes,
        "already_in_effect_when_read": reader.standing.already_in_effect,
        "unclassified_kinds": reader.standing.unclassified_kinds,
        "unknown_kind_names": list(reader.standing.unknown_kind_names),
        "kinds_that_change_the_rules": list(CHANGES_THE_RULES),
        "matches_symbols_by_substring": False,
    }


def run_exchange_announcement_reader(
    reader: ExchangeAnnouncementReader, control_socket, read_rows, publish_announcements,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for row in read_rows():
            result = reader.read(row)
            if result.is_usable:
                publish_announcements(result.announcement)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_announcement_reading(reader),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The universe is observed (2026-09-01: broker-instrument-listing's own
    trading_symbol, not crypto's symbol-universe) so an announcement's
    symbols can be matched. No announcement feed is connected on this box
    -- Upstox's own notice pages are on no input this part declares, same
    as no venue's ever was -- so no row arrives and nothing is published;
    the reader reports health and waits.
    """
    from runtime.input_assembly import Batch
    from runtime.brokers.upstox import UPSTOX_BROKER_ID

    listings = Batch(read=context.bus.reader("broker-instrument-listing"))
    publish_announcements = context.bus.publisher_for("venue-announcement")
    reader = ExchangeAnnouncementReader()

    def read_rows():
        symbols = [listing.trading_symbol for listing in listings.payloads()]
        if symbols:
            reader.observe_universe(UPSTOX_BROKER_ID, tuple(symbols))
        return ()

    return run_exchange_announcement_reader(
        reader=reader,
        control_socket=context.control_socket,
        read_rows=read_rows,
        publish_announcements=lambda announcement: publish_announcements((announcement,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
