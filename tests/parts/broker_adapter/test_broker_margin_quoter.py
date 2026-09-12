"""What the broker will lend, asked rather than assumed.

Bot 3's 5x had no source before this part. The failure it prevents is not a
wrong number, it is an invented one: `intraday_margin` appears in zero of the
102,789 rows of Upstox's real instrument master, so anything built on
`InstrumentListing.intraday_margin_percent` would have read None forever and
defaulted to something nobody measured.

The test that matters most is the one where the broker says nothing. A margin
endpoint that is down must not become 5x.
"""

import pytest

from parts.broker_adapter.broker_margin_quoter import (
    BUY, INTRADAY_PRODUCT, BrokerMarginQuoter, describe_quoting, leverage_from,
    total_margin_of,
)
from runtime.brokers.upstox import MarginQuote, MarginQuoteRequest, UpstoxAdapter


class _Entry:
    """A symbol-universe entry, as broker-symbol-universe-bridge publishes one."""

    def __init__(self, symbol, venue_instrument_id, lot_size=1):
        self.symbol = symbol
        self.venue_instrument_id = venue_instrument_id
        self.lot_size = lot_size


def a_quote(equity_margin=280.0, span=0.0, exposure=0.0, additional=0.0,
            net_buy_premium=0.0):
    return MarginQuote(
        span_margin=span, exposure_margin=exposure, equity_margin=equity_margin,
        net_buy_premium=net_buy_premium, additional_margin=additional,
    )


class _Clock:
    def __init__(self, at_ns=1_000_000_000):
        self.at_ns = at_ns

    def __call__(self):
        return self.at_ns

    def advance_seconds(self, seconds):
        self.at_ns += int(seconds * 1e9)


WAIT_AFTER_A_REFUSAL_SECONDS = 60.0


def _learn_one_priced_instrument(quoter):
    """One instrument the quoter can ask about: it needs a key and a price."""
    quoter.observe_universe_entry(_Entry("RELIANCE", "NSE_EQ|INE002A01018"))
    quoter.observe_price("NSE_EQ|INE002A01018", 1400.0)


def a_quoter(clock, per_call=20, requote_after=3600.0,
             wait_after_a_refusal=WAIT_AFTER_A_REFUSAL_SECONDS):
    return BrokerMarginQuoter(
        venue_id="upstox", instruments_per_call=per_call,
        requote_after_seconds=requote_after,
        wait_after_a_refusal_seconds=wait_after_a_refusal, now_ns=clock,
    )


# ---- the arithmetic ----------------------------------------------------------


def test_the_leverage_is_the_notional_over_what_the_broker_requires():
    """RELIANCE at 1,400 against a 280 intraday margin is 5x."""
    assert leverage_from(1400.0, 280.0) == pytest.approx(5.0)


def test_a_margin_of_zero_is_refused_rather_than_treated_as_infinite_leverage():
    """A number a position is sized against is refused when it cannot be
    computed, never clamped to something that looks reasonable (RL-061)."""
    assert leverage_from(1400.0, 0.0) is None
    assert leverage_from(0.0, 280.0) is None


def test_the_premium_of_a_bought_option_is_not_counted_as_margin():
    """net_buy_premium is the cash cost of buying an option, not margin lent
    against -- counting it would make a bought option look like it needed
    margin it does not."""
    quote = a_quote(equity_margin=100.0, net_buy_premium=9_999.0)
    assert total_margin_of(quote) == 100.0


def test_every_margin_component_the_broker_charges_is_counted():
    quote = a_quote(equity_margin=100.0, span=20.0, exposure=30.0, additional=50.0)
    assert total_margin_of(quote) == 200.0


# ---- what it asks about ------------------------------------------------------


def test_only_instruments_with_a_price_are_quoted():
    """The leverage is a ratio against notional, so an instrument whose price is
    unknown produces no ratio -- counted, not silently skipped."""
    quoter = a_quoter(_Clock())
    quoter.observe_universe_entry(_Entry("RELIANCE", "NSE_EQ|INE002A01018"))
    quoter.observe_universe_entry(_Entry("TCS", "NSE_EQ|INE467B01029"))
    quoter.observe_price("NSE_EQ|INE002A01018", 1400.0)

    assert quoter.instruments_due_a_quote() == ("NSE_EQ|INE002A01018",)
    assert quoter.quotes_with_no_price == 1


def test_a_universe_entry_with_no_instrument_key_cannot_be_asked_about():
    """Upstox is quoted by instrument_key and nothing else. Counting this is
    what separates 'the broker refused' from 'we never asked'."""
    quoter = a_quoter(_Clock())
    quoter.observe_universe_entry(_Entry("RELIANCE", None))

    assert quoter.instruments_due_a_quote() == ()
    assert quoter.entries_with_no_instrument_key == 1


def test_no_more_than_one_call_s_worth_is_asked_at_once():
    quoter = a_quoter(_Clock(), per_call=2)
    for n in range(5):
        key = f"NSE_EQ|{n}"
        quoter.observe_universe_entry(_Entry(f"SYM{n}", key))
        quoter.observe_price(key, 100.0)

    assert len(quoter.instruments_due_a_quote()) == 2


def test_an_instrument_is_not_requoted_until_its_quote_has_aged():
    clock = _Clock()
    quoter = a_quoter(clock, requote_after=3600.0)
    quoter.observe_universe_entry(_Entry("RELIANCE", "NSE_EQ|INE002A01018"))
    quoter.observe_price("NSE_EQ|INE002A01018", 1400.0)

    quoter.requirements_from({"NSE_EQ|INE002A01018": a_quote()})
    assert quoter.instruments_due_a_quote() == ()

    clock.advance_seconds(3601.0)
    assert quoter.instruments_due_a_quote() == ("NSE_EQ|INE002A01018",)


def test_a_quoter_that_asks_about_nothing_is_refused():
    with pytest.raises(ValueError):
        a_quoter(_Clock(), per_call=0)


def test_a_requote_interval_of_zero_is_refused():
    with pytest.raises(ValueError) as refusal:
        a_quoter(_Clock(), requote_after=0.0)
    assert "rate limit" in str(refusal.value)


# ---- what it publishes -------------------------------------------------------


def test_the_requirement_carries_the_leverage_and_what_produced_it():
    clock = _Clock()
    quoter = a_quoter(clock)
    quoter.observe_universe_entry(_Entry("RELIANCE", "NSE_EQ|INE002A01018"))
    quoter.observe_price("NSE_EQ|INE002A01018", 1400.0)

    requirements = quoter.requirements_from({"NSE_EQ|INE002A01018": a_quote(280.0)})

    assert len(requirements) == 1
    requirement = requirements[0]
    assert requirement.symbol == "RELIANCE"
    assert requirement.instrument_key == "NSE_EQ|INE002A01018"
    assert requirement.notional_quoted == pytest.approx(1400.0)
    assert requirement.margin_required == pytest.approx(280.0)
    assert requirement.leverage_available == pytest.approx(5.0)
    assert requirement.is_leveraged is True
    assert requirement.quoted_at_ns == clock.at_ns


def test_a_zero_margin_answer_publishes_nothing_at_all():
    """position-sizer would size against whatever it was handed, so an answer
    this part cannot use is refused rather than published."""
    quoter = a_quoter(_Clock())
    quoter.observe_universe_entry(_Entry("RELIANCE", "NSE_EQ|INE002A01018"))
    quoter.observe_price("NSE_EQ|INE002A01018", 1400.0)

    assert quoter.requirements_from({"NSE_EQ|INE002A01018": a_quote(0.0)}) == ()
    assert quoter.quotes_refused_no_margin == 1


def test_the_notional_is_the_lot_the_instrument_actually_trades_in():
    quoter = a_quoter(_Clock())
    quoter.observe_universe_entry(_Entry("NIFTY", "NSE_FO|1", lot_size=75))
    quoter.observe_price("NSE_FO|1", 100.0)

    requirement = quoter.requirements_from({"NSE_FO|1": a_quote(1500.0)})[0]

    assert requirement.quantity_quoted == 75.0
    assert requirement.notional_quoted == pytest.approx(7500.0)
    assert requirement.leverage_available == pytest.approx(5.0)


# ---- the request the broker actually receives --------------------------------


def test_the_quote_asks_for_the_intraday_product_and_not_delivery():
    """The delivery product carries no leverage at all, so quoting it would
    answer a question nobody asked."""
    adapter = UpstoxAdapter()
    payload = adapter.build_margin_quote_request_payload(
        [MarginQuoteRequest(
            instrument_key="NSE_EQ|INE002A01018", quantity=1,
            transaction_type=BUY, product=INTRADAY_PRODUCT,
        )]
    )

    assert payload["instruments"][0]["product"] == "I"
    assert payload["instruments"][0]["transaction_type"] == "BUY"


def test_a_real_upstox_margin_response_is_read_back_as_a_leverage():
    """The response shape Upstox documents: margins in an ordered array whose
    order matches the request's, never keyed by instrument."""
    adapter = UpstoxAdapter()
    response = {"data": {"margins": [
        {"span_margin": 0.0, "exposure_margin": 0.0, "equity_margin": 280.0,
         "net_buy_premium": 0.0, "additional_margin": 0.0},
    ]}}

    quotes = adapter.read_margin_quotes(response, ["NSE_EQ|INE002A01018"])

    quoter = a_quoter(_Clock())
    quoter.observe_universe_entry(_Entry("RELIANCE", "NSE_EQ|INE002A01018"))
    quoter.observe_price("NSE_EQ|INE002A01018", 1400.0)
    requirement = quoter.requirements_from(quotes)[0]

    assert requirement.leverage_available == pytest.approx(5.0)


# ---- what it reports ---------------------------------------------------------


def test_a_universe_nobody_can_be_quoted_for_reads_differently_from_a_refusing_broker():
    """calls_made 0 with universe_entries_seen climbing is its own fault, and
    must not look like a broker that answered badly (Rule 8)."""
    quoter = a_quoter(_Clock())
    quoter.observe_universe_entry(_Entry("RELIANCE", None))

    reported = describe_quoting(quoter)

    assert reported["universe_entries_seen"] == 1
    assert reported["entries_with_no_instrument_key"] == 1
    assert reported["instruments_quotable"] == 0
    assert reported["calls_made"] == 0
    assert reported["calls_failed"] == 0


def test_a_refused_call_is_not_retried_until_the_wait_has_passed():
    """18,943 failed calls out of 18,946, in one live session.

    A failed call marks nothing as quoted, so without a backoff every instrument
    it asked about is due again on the very next tick. Measured on the live spine
    2026-09-08: every failure was HTTP 429 `UDAPI10005 Too Many Request Sent`
    (reproduced by hand against the real endpoint), `quotes_read` reached 60 for
    a whole session, and `unattended-run-warden` escalated the part four times as
    `taking-longer-every-tick`. The retry was the cause of the refusal.
    """
    clock = _Clock()
    subject = a_quoter(clock)
    _learn_one_priced_instrument(subject)
    assert subject.instruments_due_a_quote(), "nothing was due, so nothing is being tested"

    subject.note_a_refusal(was_rate_limited=True)

    assert subject.instruments_due_a_quote() == ()
    assert subject.calls_refused_for_rate == 1
    assert subject.ticks_waiting_out_a_refusal == 1


def test_the_quoter_asks_again_once_the_wait_has_passed():
    """A backoff that never ends is a part that has stopped, which is worse."""
    clock = _Clock()
    subject = a_quoter(clock)
    _learn_one_priced_instrument(subject)
    subject.note_a_refusal(was_rate_limited=True)
    assert subject.instruments_due_a_quote() == ()

    clock.advance_seconds(WAIT_AFTER_A_REFUSAL_SECONDS + 1.0)

    assert subject.instruments_due_a_quote() != ()


def test_a_quoter_with_no_wait_after_a_refusal_is_refused_at_construction():
    with pytest.raises(ValueError):
        BrokerMarginQuoter(
            venue_id="upstox", instruments_per_call=20, requote_after_seconds=3600.0,
            wait_after_a_refusal_seconds=0.0,
        )
