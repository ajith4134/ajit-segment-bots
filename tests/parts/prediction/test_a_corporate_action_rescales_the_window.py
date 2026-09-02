"""A 1:1 bonus halves the price. Unadjusted, that is a -50% candle.

kline-window-builder feeds its window to Kronos and to both conviction models,
and a synthetic -50% bar is the largest move any of them has ever seen: every
detector fires on an event that did not happen. The corporate action wire is
what makes the series continuous across an ex-date.
"""

import datetime

from runtime.forecast_types import Candle
from runtime.market_conditions import CorporateAction
from parts.prediction.kline_window_builder import KlineWindowBuilder

VENUE = "upstox"
SYMBOL = "RELIANCE"
EX_DATE = datetime.date(2026, 9, 2)
OBSERVED_AT_NS = 1_756_800_000_000_000_000

MINUTE_NS = 60 * 1_000_000_000
# 2026-09-01 09:15 IST and 2026-09-02 09:15 IST as epoch nanoseconds: one bar
# the day before the ex-date and one on it. Computed from the dates rather than
# typed -- the first draft of this file carried 2025 timestamps against a 2026
# ex-date, which would have put every bar on the same side of the boundary and
# made the test pass for the wrong reason.
BEFORE_EX_NS = 1_788_234_300_000_000_000
ON_EX_NS = 1_788_320_700_000_000_000


def _bonus(price_factor=0.5, quantity_factor=2.0, kind="bonus", stated="Bonus 1:1"):
    return CorporateAction(
        symbol=SYMBOL, kind=kind, price_factor=price_factor,
        quantity_factor=quantity_factor, ex_date=EX_DATE, stated_from=stated,
        observed_at_ns=OBSERVED_AT_NS,
    )


def _candle(open_time_ns, close):
    return Candle(
        open_time_ns=open_time_ns, open=close, high=close, low=close, close=close,
        volume=1000.0, quote_volume=close * 1000.0, trades=10, is_closed=True,
    )


def _builder_around_the_ex_date():
    builder = KlineWindowBuilder(interval="1m", maximum_window=10)
    builder.observe_candle(VENUE, SYMBOL, _candle(BEFORE_EX_NS, 1000.0))
    builder.observe_candle(VENUE, SYMBOL, _candle(BEFORE_EX_NS + MINUTE_NS, 1000.0))
    builder.observe_candle(VENUE, SYMBOL, _candle(ON_EX_NS, 500.0))
    return builder


def _closes(builder):
    return [candle.close for candle in builder.candles_for(VENUE, SYMBOL)]


def test_a_bonus_rescales_the_candles_before_its_ex_date():
    builder = _builder_around_the_ex_date()
    builder.observe_corporate_action(VENUE, _bonus())
    assert _closes(builder)[:2] == [500.0, 500.0]


def test_candles_on_and_after_the_ex_date_are_left_alone():
    builder = _builder_around_the_ex_date()
    builder.observe_corporate_action(VENUE, _bonus())
    assert _closes(builder)[-1] == 500.0


def test_every_price_field_is_rescaled_not_only_the_close():
    """A window whose closes were adjusted and whose highs were not has a bar
    with a high below its own close."""
    builder = _builder_around_the_ex_date()
    builder.observe_corporate_action(VENUE, _bonus())
    first = builder.candles_for(VENUE, SYMBOL)[0]
    assert (first.open, first.high, first.low, first.close) == (500.0, 500.0, 500.0, 500.0)


def test_volume_is_rescaled_by_the_quantity_factor_not_the_price_factor():
    """A bonus doubles the shares. Leaving volume alone makes the pre-ex bars
    look half as traded as they were, which is what a volume feature reads."""
    builder = _builder_around_the_ex_date()
    builder.observe_corporate_action(VENUE, _bonus())
    assert builder.candles_for(VENUE, SYMBOL)[0].volume == 2000.0


def test_quote_volume_is_left_alone_because_rupees_traded_did_not_change():
    """price x quantity is invariant across a bonus: the same money changed
    hands. Rescaling it by either factor would invent turnover."""
    builder = _builder_around_the_ex_date()
    builder.observe_corporate_action(VENUE, _bonus())
    assert builder.candles_for(VENUE, SYMBOL)[0].quote_volume == 1_000_000.0


def test_a_dividend_does_not_rescale_anything():
    builder = _builder_around_the_ex_date()
    before = _closes(builder)
    builder.observe_corporate_action(
        VENUE, _bonus(price_factor=1.0, quantity_factor=1.0, kind="dividend",
                      stated="Dividend - Rs 3.50 Per Share"),
    )
    assert _closes(builder) == before


def test_the_same_action_applied_twice_rescales_once():
    """Reports are republished on every poll. Applying a 1:1 bonus twice
    quarters the history -- worse than not applying it, because it looks
    adjusted."""
    builder = _builder_around_the_ex_date()
    builder.observe_corporate_action(VENUE, _bonus())
    once = _closes(builder)
    builder.observe_corporate_action(VENUE, _bonus())
    assert _closes(builder) == once


def test_an_action_for_another_symbol_leaves_this_one_alone():
    builder = _builder_around_the_ex_date()
    other = CorporateAction(
        symbol="NTPC", kind="bonus", price_factor=0.5, quantity_factor=2.0,
        ex_date=EX_DATE, stated_from="Bonus 1:1", observed_at_ns=OBSERVED_AT_NS,
    )
    builder.observe_corporate_action(VENUE, other)
    assert _closes(builder)[0] == 1000.0


def test_a_candle_arriving_after_the_adjustment_is_not_rescaled_again():
    """The action is applied to what is held when it arrives. A bar that
    arrives afterwards is already post-ex and must be left as the venue sent
    it."""
    builder = _builder_around_the_ex_date()
    builder.observe_corporate_action(VENUE, _bonus())
    builder.observe_candle(VENUE, SYMBOL, _candle(ON_EX_NS + MINUTE_NS, 505.0))
    assert _closes(builder)[-1] == 505.0


def test_how_many_actions_were_applied_is_counted():
    builder = _builder_around_the_ex_date()
    builder.observe_corporate_action(VENUE, _bonus())
    assert builder.standing.corporate_actions_applied == 1
