"""venue-order-status-translator: one venue's status words into the system's closed set.

Every venue names order states differently and none of them mean quite the same
thing. Binance says `PARTIALLY_FILLED`, `EXPIRED`, `EXPIRED_IN_MATCH`; Bybit says
`PartiallyFilled`, `Deactivated`, `Untriggered`. A part downstream that switched
on those strings would carry a copy of every venue's vocabulary and would break
silently the day a venue added a word.

So this collapses them into a closed set, and **refuses an unknown status rather
than guessing**. Guessing is the dangerous option in exactly one direction: a
status wrongly read as `FILLED` invents a position that does not exist, and every
part downstream then trades against it. An unknown status is reported and the
order left in its previous state, which is recoverable.

It emits fills as well as statuses, because a fill is what a status change
actually means to the rest of the system, and it is idempotent on the venue's own
fill id -- venues re-send, and a doubled fill is a doubled position.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.trading_types import BUY, SELL, Fill

PART_ID = "venue-order-status-translator"

PART_DECLARATION = PartDeclaration(
    part_id="venue-order-status-translator",
    consumes=("raw-venue-order-status",),
    produces=("fill", "order-reject-reason", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

# The closed set. Nothing downstream may see any other order status.
PENDING = "pending"
OPEN = "open"
PARTIALLY_FILLED = "partially-filled"
FILLED = "filled"
CANCELLED = "cancelled"
REJECTED = "rejected"
EXPIRED = "expired"
UNKNOWN = "unknown"

ORDER_STATUSES = (PENDING, OPEN, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED, EXPIRED, UNKNOWN)

# A status from which no further change can come. An order in one of these is
# finished, and anything arriving for it afterwards is a duplicate or an error.
TERMINAL_STATUSES = (FILLED, CANCELLED, REJECTED, EXPIRED)

# Each venue's own words, from its documentation. Wire vocabulary, kept per venue
# because the same word can differ: Bybit's `Deactivated` is a conditional order
# that was stood down, which is an expiry rather than a cancellation.
VENUE_STATUS_WORDS = {
    "binance-usdm": {
        "NEW": OPEN,
        "PARTIALLY_FILLED": PARTIALLY_FILLED,
        "FILLED": FILLED,
        "CANCELED": CANCELLED,
        "REJECTED": REJECTED,
        "EXPIRED": EXPIRED,
        "EXPIRED_IN_MATCH": EXPIRED,
        "PENDING_CANCEL": OPEN,
    },
    "bybit-linear": {
        "New": OPEN,
        "Created": PENDING,
        "PartiallyFilled": PARTIALLY_FILLED,
        "PartiallyFilledCanceled": CANCELLED,
        "Filled": FILLED,
        "Cancelled": CANCELLED,
        "Rejected": REJECTED,
        "Untriggered": PENDING,
        "Triggered": OPEN,
        "Deactivated": EXPIRED,
    },
}


@dataclass(frozen=True)
class TranslatedStatus:
    """One venue status, in this system's vocabulary, with what it produced."""

    order_id: str
    venue_id: str
    symbol: str
    status: str
    venue_status: str
    is_terminal: bool
    fill: Fill | None
    reject_message: str | None
    reason: str
    translated_at_ns: int


@dataclass
class TranslatorStanding:
    translated: int = 0
    unknown_statuses: int = 0
    fills_emitted: int = 0
    duplicate_fills_ignored: int = 0
    terminal_after_terminal: int = 0
    by_status: dict = field(default_factory=dict)
    unknown_words: dict = field(default_factory=dict)


class VenueOrderStatusTranslator:
    """Collapses a venue's status vocabulary into the closed set, refusing surprises."""

    def __init__(self, status_words: dict | None = None, now_ns=time.time_ns) -> None:
        self._words = {
            venue: dict(words) for venue, words in (status_words or VENUE_STATUS_WORDS).items()
        }
        self._now_ns = now_ns
        self._seen_fill_ids: set[str] = set()
        self._final_status: dict[str, str] = {}
        self.standing = TranslatorStanding()

    def learn_status_word(self, venue_id: str, venue_word: str, status: str) -> None:
        """Teach this translator one venue's word for one of our statuses."""
        if status not in ORDER_STATUSES:
            raise ValueError(f"{status!r} is not one of this system's order statuses")
        self._words.setdefault(venue_id, {})[venue_word] = status

    def translate(
        self,
        order_id: str,
        venue_id: str,
        symbol: str,
        venue_status: str,
        filled_quantity: float = 0.0,
        fill_price: float = 0.0,
        fill_id: str | None = None,
        side: str = BUY,
        fee: float = 0.0,
        filled_at_ns: int | None = None,
        venue_message: str = "",
    ) -> TranslatedStatus:
        status = self._words.get(venue_id, {}).get(venue_status)
        self.standing.translated += 1

        if status is None:
            self.standing.unknown_statuses += 1
            key = f"{venue_id}:{venue_status}"
            self.standing.unknown_words[key] = self.standing.unknown_words.get(key, 0) + 1
            return TranslatedStatus(
                order_id=order_id, venue_id=venue_id, symbol=symbol, status=UNKNOWN,
                venue_status=venue_status, is_terminal=False, fill=None,
                reject_message=venue_message or None,
                reason=(
                    f"{venue_id} sent {venue_status!r}, which this translator has no reading for; "
                    f"the order keeps its previous state rather than being guessed at"
                ),
                translated_at_ns=self._now_ns(),
            )

        self.standing.by_status[status] = self.standing.by_status.get(status, 0) + 1
        previous = self._final_status.get(order_id)
        if previous in TERMINAL_STATUSES:
            self.standing.terminal_after_terminal += 1
        if status in TERMINAL_STATUSES:
            self._final_status[order_id] = status

        fill = None
        if filled_quantity > 0 and fill_id is not None:
            if fill_id in self._seen_fill_ids:
                self.standing.duplicate_fills_ignored += 1
            else:
                self._seen_fill_ids.add(fill_id)
                self.standing.fills_emitted += 1
                fill = Fill(
                    fill_id=fill_id,
                    venue_id=venue_id,
                    symbol=symbol,
                    side=side,
                    price=fill_price,
                    quantity=filled_quantity,
                    fee=fee,
                    filled_at_ns=filled_at_ns if filled_at_ns is not None else self._now_ns(),
                    order_id=order_id,
                    is_paper=False,
                )

        return TranslatedStatus(
            order_id=order_id,
            venue_id=venue_id,
            symbol=symbol,
            status=status,
            venue_status=venue_status,
            is_terminal=status in TERMINAL_STATUSES,
            fill=fill,
            reject_message=venue_message if status == REJECTED else None,
            reason=f"{venue_id}'s {venue_status!r} is this system's {status!r}",
            translated_at_ns=self._now_ns(),
        )

    def status_of(self, order_id: str) -> str | None:
        """The terminal status this order reached, if it reached one."""
        return self._final_status.get(order_id)


def describe_translation(translator: VenueOrderStatusTranslator) -> dict:
    return {
        "part_id": PART_ID,
        "translated": translator.standing.translated,
        "unknown_statuses": translator.standing.unknown_statuses,
        "unknown_words": dict(translator.standing.unknown_words),
        "fills_emitted": translator.standing.fills_emitted,
        "duplicate_fills_ignored": translator.standing.duplicate_fills_ignored,
        "updates_after_terminal": translator.standing.terminal_after_terminal,
        "by_status": dict(translator.standing.by_status),
    }


def run_venue_order_status_translator(
    translator: VenueOrderStatusTranslator, control_socket, read_raw_statuses, publish_translations,
    health_interval_seconds: float, emit_health,
) -> int:
    def tick() -> None:
        publish_translations(
            tuple(translator.translate(**status) for status in read_raw_statuses())
        )

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
    )
