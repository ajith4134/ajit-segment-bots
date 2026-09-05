"""broker-margin-quoter: the margin the broker requires against each instrument.

Bot 3 trades cash equity intraday on leverage, and until this part existed
nothing in the system could find out how much leverage it is allowed. The
segment file states a ceiling of 5.0 because the operator asked for it; the real
limit is the broker's, set per stock from SEBI VAR+ELM margins that change
daily, and it is always the smaller of the two that binds.

**It cannot be read from the instrument master.** Measured 2026-09-05:
`intraday_margin` appears in **zero of the 102,789 rows** of Upstox's real
instrument file, so `InstrumentListing.intraday_margin_percent` is always
`None` -- a field the adapter parses that the source never carries. Anything
built on it would have read `None` forever and defaulted to something invented.

So the leverage is asked for, from the one place that knows:
`https://api.upstox.com/v2/charges/margin`, which prices a specific candidate
order.

**A quote per instrument, not per order.** Margin is a property of the
instrument and the day, not of the particular order -- Upstox's requirement
scales with quantity, so the ratio does not. Quoting the universe on an interval
therefore answers the same question as quoting each order, without putting a
network call inside the trade path and without spending the rate limit on every
intent. The leverage is the ratio:

    leverage available = notional quoted / margin the broker requires

**One share, and the ratio, rather than a guess at size.** The quote asks for
the smallest quantity the instrument trades in, because what is wanted is the
multiple and not the rupee figure -- a quote sized to an imagined order would
have to guess the order first, which is the circularity that made this look
harder than it is: position-sizer needs the leverage to choose a size, so the
leverage must not need a size to be known.

**Absence is never a leverage of one, and never a leverage of five.** An
instrument the broker has not answered for produces no requirement at all, and
`leverage-selector` treats a missing requirement as a reason to stay unlevered
rather than as permission to use the ceiling. A margin endpoint that is down
must not silently become 5x.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from runtime.part_declaration import PartDeclaration

PART_ID = "broker-margin-quoter"

PART_DECLARATION = PartDeclaration(
    part_id="broker-margin-quoter",
    consumes=("symbol-universe", "broker-market-data", "broker-token-standing"),
    produces=("broker-margin-requirement", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="delays",
)

# Upstox's own product code for an intraday (margin) equity order. The whole
# point of this quote is the intraday requirement: the delivery product carries
# no leverage at all, so quoting it would answer a question nobody asked.
INTRADAY_PRODUCT = "I"
BUY = "BUY"


@dataclass(frozen=True)
class MarginRequirement:
    """What the broker requires against one instrument, and the leverage it implies.

    `leverage_available` is the ratio the sizer may use, never the ceiling the
    operator set -- the two are compared by `leverage-selector`, and the smaller
    binds. `quoted_at_ns` is when the broker answered, so a consumer can bound
    how old a permission it is acting on.
    """

    venue_id: str
    symbol: str
    instrument_key: str
    quantity_quoted: float
    notional_quoted: float
    margin_required: float
    leverage_available: float
    quoted_at_ns: int

    @property
    def is_leveraged(self) -> bool:
        return self.leverage_available > 1.0


def total_margin_of(quote) -> float:
    """Every component the broker charges, summed.

    `net_buy_premium` is deliberately excluded: it is the cash cost of buying an
    option, not margin lent against, and adding it would make a bought option
    look like it needed margin it does not.
    """
    return (
        float(quote.span_margin)
        + float(quote.exposure_margin)
        + float(quote.equity_margin)
        + float(quote.additional_margin)
    )


def leverage_from(notional: float, margin_required: float) -> float | None:
    """The multiple the broker's own requirement implies, or None.

    None where it cannot be computed -- a margin of zero would divide to
    infinity, and this is a number a position is sized against (RL-061), so it
    is refused rather than clamped to something that looks reasonable.
    """
    if notional <= 0 or margin_required <= 0:
        return None
    return notional / margin_required


class BrokerMarginQuoter:
    """Asks the broker what it lends against each instrument in the universe."""

    def __init__(
        self,
        venue_id: str,
        instruments_per_call: int,
        requote_after_seconds: float,
        now_ns=time.time_ns,
    ) -> None:
        if instruments_per_call < 1:
            raise ValueError(
                "a quoter that asks about no instrument publishes nothing while looking "
                f"like a working quoter; got {instruments_per_call!r}"
            )
        if requote_after_seconds <= 0:
            raise ValueError(
                "a requote interval of zero asks the broker on every tick, which spends "
                f"the rate limit the trade path needs; got {requote_after_seconds!r}"
            )
        self._venue_id = venue_id
        self._per_call = instruments_per_call
        self._requote_after_ns = int(requote_after_seconds * 1_000_000_000)
        self._now_ns = now_ns
        # instrument_key -> the symbol every other part names it by, and its lot.
        self._symbol_by_key: dict[str, str] = {}
        self._lot_by_key: dict[str, float] = {}
        self._price_by_key: dict[str, float] = {}
        self._quoted_at_ns: dict[str, int] = {}
        self.universe_entries_seen = 0
        self.entries_with_no_instrument_key = 0
        self.prices_seen = 0
        self.calls_made = 0
        self.calls_failed = 0
        self.quotes_read = 0
        self.quotes_with_no_price = 0
        self.quotes_refused_no_margin = 0
        self.last_failure: str | None = None

    # ---- what it is told ---------------------------------------------------

    def observe_universe_entry(self, entry) -> None:
        """One symbol this segment may trade."""
        self.universe_entries_seen += 1
        key = getattr(entry, "venue_instrument_id", None)
        if not key:
            # Upstox is quoted by instrument_key and nothing else. A universe
            # entry without one cannot be asked about, and counting it is what
            # separates "the broker refused" from "we never asked".
            self.entries_with_no_instrument_key += 1
            return
        self._symbol_by_key[key] = entry.symbol
        lot = getattr(entry, "lot_size", None)
        self._lot_by_key[key] = float(lot) if lot else 1.0

    def observe_price(self, instrument_key: str, price: float) -> None:
        if price > 0:
            self.prices_seen += 1
            self._price_by_key[instrument_key] = price

    # ---- what it asks ------------------------------------------------------

    def instruments_due_a_quote(self, at_ns: int | None = None) -> tuple[str, ...]:
        """The instruments worth asking about now, capped at one call's worth.

        Only instruments with a price: the leverage is a ratio against notional,
        and a quote whose notional is unknown produces no ratio. Counted rather
        than skipped silently.
        """
        at = self._now_ns() if at_ns is None else at_ns
        due = []
        without_price = 0
        for key in sorted(self._symbol_by_key):
            if key not in self._price_by_key:
                without_price += 1
                continue
            quoted_at = self._quoted_at_ns.get(key)
            if quoted_at is not None and at - quoted_at < self._requote_after_ns:
                continue
            due.append(key)
        self.quotes_with_no_price = without_price
        return tuple(due[: self._per_call])

    def requirements_from(self, quotes, at_ns: int | None = None) -> tuple:
        """The broker's answers, as requirements this system can size against."""
        at = self._now_ns() if at_ns is None else at_ns
        requirements = []
        for instrument_key, quote in quotes.items():
            self._quoted_at_ns[instrument_key] = at
            self.quotes_read += 1
            quantity = self._lot_by_key.get(instrument_key, 1.0)
            price = self._price_by_key.get(instrument_key)
            if price is None:
                continue
            notional = price * quantity
            margin = total_margin_of(quote)
            leverage = leverage_from(notional, margin)
            if leverage is None:
                # A zero margin is not infinite leverage, it is an answer this
                # part could not use. Refused rather than published, because
                # position-sizer would size against whatever it was handed.
                self.quotes_refused_no_margin += 1
                continue
            requirements.append(
                MarginRequirement(
                    venue_id=self._venue_id,
                    symbol=self._symbol_by_key[instrument_key],
                    instrument_key=instrument_key,
                    quantity_quoted=quantity,
                    notional_quoted=notional,
                    margin_required=margin,
                    leverage_available=leverage,
                    quoted_at_ns=at,
                )
            )
        return tuple(requirements)


def describe_quoting(quoter: BrokerMarginQuoter) -> dict:
    """What was asked, what answered, and what could not be asked at all.

    `calls_made` at zero with `universe_entries_seen` climbing is the signature
    of a universe nobody can be quoted for -- a different fault from a broker
    that refuses, and the two must not read the same (Rule 8).
    """
    return {
        "part_id": PART_ID,
        "universe_entries_seen": quoter.universe_entries_seen,
        "entries_with_no_instrument_key": quoter.entries_with_no_instrument_key,
        "prices_seen": quoter.prices_seen,
        "instruments_quotable": len(quoter._symbol_by_key),
        "calls_made": quoter.calls_made,
        "calls_failed": quoter.calls_failed,
        "quotes_read": quoter.quotes_read,
        "quotes_with_no_price": quoter.quotes_with_no_price,
        "quotes_refused_no_margin": quoter.quotes_refused_no_margin,
        "last_failure": quoter.last_failure,
    }


def fetch_margin_quotes(url: str, payload: dict, access_token: str, timeout_seconds: float):
    """One call to the broker's margin endpoint."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read())


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.brokers.upstox import MarginQuoteRequest, UpstoxAdapter
    from runtime.input_assembly import Batch, LatestByKey

    adapter = UpstoxAdapter()
    universe = Batch(read=context.bus.reader("symbol-universe"))
    prices = Batch(read=context.bus.reader("broker-market-data"))
    tokens = LatestByKey(
        read=context.bus.reader("broker-token-standing"),
        key_of=lambda standing: standing.broker_id,
        maximum_age_seconds=context.number("broker_token_standing_maximum_age"),
    )
    publish_requirements = context.bus.publisher_for("broker-margin-requirement")

    quoter = BrokerMarginQuoter(
        venue_id=adapter.broker_id,
        instruments_per_call=int(context.number("margin_quote_instruments_per_call")),
        requote_after_seconds=context.number("margin_requote_interval_seconds"),
    )
    timeout_seconds = context.number("broker_connection_open_timeout")

    def tick() -> None:
        for entry in universe.payloads():
            quoter.observe_universe_entry(entry)
        for update in prices.payloads():
            quoter.observe_price(update.instrument_key, update.last_traded_price)
        tokens.take_in_what_arrived()

        due = quoter.instruments_due_a_quote()
        if not due:
            return
        token = tokens.mapping().get(adapter.broker_id)
        if token is None or not token.is_still_valid():
            # Not a failure: a normal state before the scheduler has spoken or
            # while a refresh is in flight. Reported on the standing, never
            # raised, and never a reason to publish a leverage nobody quoted.
            return

        payload = adapter.build_margin_quote_request_payload(
            [
                MarginQuoteRequest(
                    instrument_key=key,
                    quantity=int(quoter._lot_by_key.get(key, 1.0)),
                    transaction_type=BUY,
                    product=INTRADAY_PRODUCT,
                )
                for key in due
            ]
        )
        quoter.calls_made += 1
        try:
            response = fetch_margin_quotes(
                adapter.margin_endpoint_url(), payload,
                token.access_token, timeout_seconds,
            )
            quoter.last_failure = None
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as failure:
            quoter.calls_failed += 1
            quoter.last_failure = f"{type(failure).__name__}: {failure}"
            return

        quotes = adapter.read_margin_quotes(response, due)
        requirements = quoter.requirements_from(quotes)
        if requirements:
            publish_requirements(requirements)

    from runtime.part_process import run_part

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_quoting(quoter),
    )


__all__ = [
    "BUY",
    "INTRADAY_PRODUCT",
    "BrokerMarginQuoter",
    "MarginRequirement",
    "PART_DECLARATION",
    "PART_ID",
    "describe_quoting",
    "fetch_margin_quotes",
    "leverage_from",
    "start_part",
    "total_margin_of",
]
