from parts.broker_adapter.broker_price_level_sampler import (
    BrokerPriceFrame, BrokerPriceLevel, BrokerPriceLevelSampler,
)
from runtime.brokers.broker_adapter import LtpUpdate

# The operator's own ceiling, restated here so what the test exercises is visible.
MAXIMUM_MESSAGE_BYTES = 131_072


def _ltp(instrument_key, price, ltt_ms=1_740_000_000_000):
    return LtpUpdate(
        instrument_key=instrument_key, last_traded_price=price,
        last_traded_quantity=10.0, last_traded_time_ms=ltt_ms,
        close_price=None, broker_time_ns=ltt_ms * 1_000_000,
    )


def test_the_first_frame_publishes_immediately_but_the_next_waits_for_cadence():
    sampler = BrokerPriceLevelSampler(
        broker_id="upstox", cadence_seconds=1.0, maximum_symbols_per_frame=1000,
        maximum_frame_bytes=MAXIMUM_MESSAGE_BYTES,
    )
    sampler.observe_ltp(_ltp("NSE_EQ|A", 100.0))
    first = sampler.frames_due(now_ns=0)
    assert len(first) == 1  # nothing published yet, so the cadence has nothing to gate

    sampler.observe_ltp(_ltp("NSE_EQ|A", 101.0))
    assert sampler.frames_due(now_ns=500_000_000) == ()  # 0.5s later, before the 1s cadence


def test_a_frame_carries_every_instrument_s_latest_price():
    sampler = BrokerPriceLevelSampler(
        broker_id="upstox", cadence_seconds=1.0, maximum_symbols_per_frame=1000,
        maximum_frame_bytes=MAXIMUM_MESSAGE_BYTES,
    )
    sampler.observe_ltp(_ltp("NSE_EQ|A", 100.0))
    sampler.observe_ltp(_ltp("NSE_EQ|B", 200.0))
    sampler.observe_ltp(_ltp("NSE_EQ|A", 101.0))  # replaces the first reading
    frames = sampler.frames_due(now_ns=2_000_000_000)
    assert len(frames) == 1
    frame = frames[0]
    assert frame.broker_id == "upstox"
    prices = {level.instrument_key: level.price for level in frame.levels}
    assert prices == {"NSE_EQ|A": 101.0, "NSE_EQ|B": 200.0}


def test_a_level_carries_the_broker_s_own_print_time_not_publication_time():
    sampler = BrokerPriceLevelSampler(
        broker_id="upstox", cadence_seconds=1.0, maximum_symbols_per_frame=1000,
        maximum_frame_bytes=MAXIMUM_MESSAGE_BYTES,
    )
    sampler.observe_ltp(_ltp("NSE_EQ|A", 100.0, ltt_ms=1_740_000_000_000))
    frame = sampler.frames_due(now_ns=5_000_000_000)[0]
    level = frame.levels[0]
    assert level.observed_at_ns == 1_740_000_000_000 * 1_000_000
    assert frame.published_at_ns == 5_000_000_000


def test_a_frame_larger_than_the_maximum_is_split():
    sampler = BrokerPriceLevelSampler(
        broker_id="upstox", cadence_seconds=1.0, maximum_symbols_per_frame=2,
        maximum_frame_bytes=MAXIMUM_MESSAGE_BYTES,
    )
    for i in range(5):
        sampler.observe_ltp(_ltp(f"NSE_EQ|{i}", float(i + 1)))
    frames = sampler.frames_due(now_ns=2_000_000_000)
    assert len(frames) == 3  # 2 + 2 + 1
    assert frames[0].of_parts == 3
    assert frames[0].was_split
    assert sampler.standing.frames_split == 1
    total_levels = sum(len(f.levels) for f in frames)
    assert total_levels == 5


def test_nothing_observed_publishes_no_frame():
    sampler = BrokerPriceLevelSampler(
        broker_id="upstox", cadence_seconds=1.0, maximum_symbols_per_frame=1000,
        maximum_frame_bytes=MAXIMUM_MESSAGE_BYTES,
    )
    assert sampler.frames_due(now_ns=5_000_000_000) == ()


def test_zero_or_negative_price_is_not_observed():
    sampler = BrokerPriceLevelSampler(
        broker_id="upstox", cadence_seconds=1.0, maximum_symbols_per_frame=1000,
        maximum_frame_bytes=MAXIMUM_MESSAGE_BYTES,
    )
    sampler.observe_ltp(_ltp("NSE_EQ|A", 0.0))
    sampler.observe_ltp(_ltp("NSE_EQ|B", -5.0))
    assert sampler.frames_due(now_ns=2_000_000_000) == ()


def test_cadence_must_be_positive():
    import pytest

    with pytest.raises(ValueError):
        BrokerPriceLevelSampler(
            broker_id="upstox", cadence_seconds=0.0, maximum_symbols_per_frame=10,
            maximum_frame_bytes=MAXIMUM_MESSAGE_BYTES,
        )
