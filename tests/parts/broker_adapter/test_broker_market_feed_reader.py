from parts.broker_adapter.broker_market_feed_reader import plan_subscriptions
from runtime.brokers.broker_adapter import InstrumentListing, SubscriptionMode
from runtime.brokers.upstox import UpstoxAdapter


def _listing(key: str) -> InstrumentListing:
    return InstrumentListing(
        instrument_key=key, exchange="NSE", segment="NSE_EQ", instrument_type="EQ",
        trading_symbol=key, lot_size=1, tick_size=0.05, freeze_quantity=None,
        expiry_ms=None, strike_price=None, underlying_key=None,
        intraday_margin_percent=None, intraday_leverage=None,
    )


def test_plan_subscriptions_stops_at_the_full_mode_individual_limit():
    adapter = UpstoxAdapter()
    listings = tuple(_listing(f"NSE_EQ|{i}") for i in range(2500))  # over the 2000 individual cap
    plan = plan_subscriptions(adapter, listings, mode=SubscriptionMode.FULL)
    assert len(plan) == 2000


def test_plan_subscriptions_covers_every_listing_when_under_the_cap():
    adapter = UpstoxAdapter()
    listings = tuple(_listing(f"NSE_EQ|{i}") for i in range(180))
    plan = plan_subscriptions(adapter, listings, mode=SubscriptionMode.FULL)
    assert len(plan) == 180
