"""paper-currency-converter: the main account's currency into each segment's quote (RL-029).

The main account is held in one currency and segments trade in whatever their
symbols quote in. Adding results across them without converting produces a number
that is not wrong by a little -- it is a sum of different units.

RL-029 requires the rate used to be **journalled**, and that is the part that
matters. A conversion done at an unrecorded rate cannot be re-derived later, so a
historical result becomes unreproducible: recomputing it at today's rate gives a
different answer and nothing says which was right.

**A rate is never invented.** An unknown pair converts nothing and says so, and a
rate older than its freshness bound is refused rather than used quietly -- a stale
rate on a volatile pair silently misstates every figure derived from it, and
looks exactly like a fresh one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.journal import Journal
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "paper-currency-converter"

PART_DECLARATION = PartDeclaration(
    part_id="paper-currency-converter",
    consumes=("main-account-setting", "market-data"),
    produces=("paper-currency-rate", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

CONVERTED = "converted"
SAME_CURRENCY = "same-currency"
NO_RATE = "no-rate-for-this-pair"
STALE_RATE = "rate-too-old"

PAPER_CURRENCY_RATE = "paper-currency-rate"


@dataclass(frozen=True)
class CurrencyConversion:
    """One conversion, and the rate it used -- both journalled together."""

    from_currency: str
    to_currency: str
    amount: float
    converted_amount: float | None
    rate: float | None
    rate_age_seconds: float | None
    outcome: str
    reason: str
    converted_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.outcome in (CONVERTED, SAME_CURRENCY) and self.converted_amount is not None


@dataclass
class _Rate:
    rate: float
    at_monotonic: float
    source: str


@dataclass
class ConverterStanding:
    conversions: int = 0
    same_currency: int = 0
    refused_no_rate: int = 0
    refused_stale: int = 0
    rates_journalled: int = 0
    pairs_known: int = 0
    oldest_rate_used_seconds: float = 0.0


class PaperCurrencyConverter:
    """Converts between the account's currency and a segment's, journalling each rate."""

    def __init__(
        self,
        journal: Journal,
        maximum_rate_age_seconds: float,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        if maximum_rate_age_seconds <= 0:
            raise ValueError("a rate must be allowed some age, or nothing can ever be converted")
        self._journal = journal
        self._maximum_age = maximum_rate_age_seconds
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._rates: dict[tuple[str, str], _Rate] = {}
        self.standing = ConverterStanding()

    def observe_rate(self, from_currency: str, to_currency: str, rate: float, source: str) -> None:
        """A live rate for one pair, and where it came from.

        The inverse is stored too, because a system that knows USDT per USDC and
        refuses to answer USDC per USDT would be refusing arithmetic it can do.
        """
        if rate <= 0:
            return
        now = self._monotonic()
        self._rates[(from_currency, to_currency)] = _Rate(rate, now, source)
        self._rates[(to_currency, from_currency)] = _Rate(1.0 / rate, now, f"inverse of {source}")
        self.standing.pairs_known = len(self._rates)

    def convert(self, amount: float, from_currency: str, to_currency: str) -> CurrencyConversion:
        """Convert an amount, journalling the rate that was used (RL-029)."""
        self.standing.conversions += 1

        if from_currency == to_currency:
            self.standing.same_currency += 1
            return self._conversion(
                from_currency, to_currency, amount, amount, 1.0, 0.0, SAME_CURRENCY,
                "the same currency; nothing to convert",
            )

        held = self._rates.get((from_currency, to_currency))
        if held is None:
            self.standing.refused_no_rate += 1
            return self._conversion(
                from_currency, to_currency, amount, None, None, None, NO_RATE,
                f"no rate has been observed for {from_currency}/{to_currency}; a converted "
                f"figure at an invented rate is a number nobody can check",
            )

        age = self._monotonic() - held.at_monotonic
        if age > self._maximum_age:
            self.standing.refused_stale += 1
            return self._conversion(
                from_currency, to_currency, amount, None, held.rate, age, STALE_RATE,
                f"the {from_currency}/{to_currency} rate is {age:.0f}s old, past the "
                f"{self._maximum_age:.0f}s bound; a stale rate looks exactly like a fresh one",
            )

        converted = amount * held.rate
        self.standing.oldest_rate_used_seconds = max(self.standing.oldest_rate_used_seconds, age)
        conversion = self._conversion(
            from_currency, to_currency, amount, converted, held.rate, age, CONVERTED,
            f"{amount:,.4f} {from_currency} at {held.rate:.6f} from {held.source}",
        )
        self._journal_rate(conversion, held.source)
        return conversion

    def _journal_rate(self, conversion: CurrencyConversion, source: str) -> None:
        """RL-029: the rate used is recorded, so the figure can be re-derived."""
        self.standing.rates_journalled += 1
        self._journal.append(
            kind=PAPER_CURRENCY_RATE,
            part_id=PART_ID,
            payload={
                "from_currency": conversion.from_currency,
                "to_currency": conversion.to_currency,
                "rate": conversion.rate,
                "rate_source": source,
                "rate_age_seconds": conversion.rate_age_seconds,
                "amount": conversion.amount,
                "converted_amount": conversion.converted_amount,
                "converted_at_ns": conversion.converted_at_ns,
            },
        )

    def _conversion(
        self, from_currency, to_currency, amount, converted, rate, age, outcome, reason
    ) -> CurrencyConversion:
        return CurrencyConversion(
            from_currency=from_currency, to_currency=to_currency, amount=amount,
            converted_amount=converted, rate=rate, rate_age_seconds=age,
            outcome=outcome, reason=reason, converted_at_ns=self._now_ns(),
        )

    def rate_for(self, from_currency: str, to_currency: str) -> float | None:
        held = self._rates.get((from_currency, to_currency))
        return held.rate if held else None


def describe_conversion(converter: PaperCurrencyConverter) -> dict:
    return {
        "part_id": PART_ID,
        "conversions": converter.standing.conversions,
        "same_currency": converter.standing.same_currency,
        "refused_no_rate": converter.standing.refused_no_rate,
        "refused_stale_rate": converter.standing.refused_stale,
        "rates_journalled": converter.standing.rates_journalled,
        "pairs_known": converter.standing.pairs_known,
        "oldest_rate_used_seconds": converter.standing.oldest_rate_used_seconds,
    }


def run_paper_currency_converter(
    converter: PaperCurrencyConverter, control_socket, read_rates_and_requests, publish_conversions,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        rates, requests = read_rates_and_requests()
        for rate in rates:
            converter.observe_rate(**rate)
        publish_conversions(tuple(converter.convert(**request) for request in requests))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
