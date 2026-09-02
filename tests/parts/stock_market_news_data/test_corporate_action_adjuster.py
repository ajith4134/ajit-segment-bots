"""Subject strings are NSE's own wording. "Dividend - Rs 3.50 Per Share" is
verbatim from the live 2026-09-02 response; the bonus and split wordings follow
the same published pattern on that endpoint."""

import datetime

from runtime.market_conditions import CorporateActionReport
from parts.stock_market_news_data.corporate_action_adjuster import CorporateActionAdjuster

EX_DATE = datetime.date(2026, 9, 2)
OBSERVED_AT_NS = 1_756_800_000_000_000_000


def _report(subject, symbol="RELIANCE"):
    return CorporateActionReport(
        symbol=symbol, series="EQ", isin="INE002A01018", subject=subject,
        ex_date=EX_DATE, record_date=EX_DATE, face_value=10.0,
        observed_at_ns=OBSERVED_AT_NS,
    )


def test_a_one_for_one_bonus_halves_the_price_and_doubles_the_quantity():
    """The defect this whole part exists to stop: unadjusted, this is a -50%
    candle. kline-window-builder feeds it to Kronos, every detector fires, and
    cost-basis-tracker prices a position against a basis that is now wrong."""
    action = CorporateActionAdjuster().action_for(_report("Bonus 1:1"))
    assert action.kind == "bonus"
    assert action.price_factor == 0.5
    assert action.quantity_factor == 2.0
    assert action.ex_date == EX_DATE


def test_a_one_for_two_bonus_is_a_third_off_not_a_half():
    """1:2 means one new share for every two held: three shares where two were."""
    action = CorporateActionAdjuster().action_for(_report("Bonus 1:2"))
    assert action.price_factor == 2 / 3
    assert action.quantity_factor == 3 / 2


def test_a_face_value_split_divides_by_the_ratio_of_the_two_face_values():
    action = CorporateActionAdjuster().action_for(
        _report("Face Value Split From Rs 10/- To Rs 2/-")
    )
    assert action.kind == "split"
    assert action.price_factor == 0.2
    assert action.quantity_factor == 5.0


def test_a_dividend_is_carried_but_never_rescales_a_price_series():
    """A dividend does drop the price on ex-date, by an absolute amount rather
    than a ratio. Carrying it with a factor of 1.0 says 'known, not a series
    adjustment' -- silently dropping it would lose a real ex-date event."""
    action = CorporateActionAdjuster().action_for(_report("Dividend - Rs 3.50 Per Share"))
    assert action.kind == "dividend"
    assert action.price_factor == 1.0
    assert action.quantity_factor == 1.0


def test_an_action_whose_wording_states_no_ratio_is_refused_not_guessed():
    """NSE publishes wordings this parser has never seen. Returning None means
    'not understood' and leaves the series unadjusted, which is recoverable.
    A guessed ratio silently rewrites a price history, which is not."""
    assert CorporateActionAdjuster().action_for(_report("Scheme of Arrangement")) is None


def test_a_bonus_with_a_zero_denominator_is_refused_rather_than_dividing_by_zero():
    assert CorporateActionAdjuster().action_for(_report("Bonus 1:0")) is None


def test_a_split_to_a_zero_face_value_is_refused():
    assert CorporateActionAdjuster().action_for(
        _report("Face Value Split From Rs 10/- To Rs 0/-")
    ) is None


def test_the_wording_is_carried_so_a_wrong_factor_can_be_traced_to_its_source():
    action = CorporateActionAdjuster().action_for(_report("Bonus 1:1"))
    assert action.stated_from == "Bonus 1:1"


def test_what_was_understood_and_what_was_refused_are_both_counted():
    """The refused count is this parser's real coverage gap, and the input to
    the next iteration. A refusal nobody counts is a wording never added."""
    adjuster = CorporateActionAdjuster()
    adjuster.action_for(_report("Bonus 1:1"))
    adjuster.action_for(_report("Scheme of Arrangement"))
    assert adjuster.understood == 1
    assert adjuster.refused == 1
    assert "Scheme of Arrangement" in adjuster.wordings_refused


def test_a_split_wording_that_also_says_bonus_is_read_as_the_bonus_it_states():
    """NSE has published combined actions. Bonus is matched first deliberately:
    it is the one whose quantity factor a position must have, and reading a
    combined wording as a split alone would leave the share count wrong."""
    action = CorporateActionAdjuster().action_for(
        _report("Bonus 1:1 / Face Value Split From Rs 10/- To Rs 5/-")
    )
    assert action.kind == "bonus"
