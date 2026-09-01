from parts.broker_adapter.broker_account_funds_reader import (
    FRESH, NO_TOKEN, STALE, UNREADABLE, BrokerAccountFundsReader,
)


def test_reads_equity_and_commodity_segments_from_upstox_own_sample():
    # Source: upstox.com/developer/api-documentation/get-user-fund-margin,
    # response sample, fetched 2026-09-01.
    response = {
        "status": "success",
        "data": {
            "equity": {
                "used_margin": 0.8, "payin_amount": 200.0, "span_margin": 0.0,
                "adhoc_margin": 0.0, "notional_cash": 0.0,
                "available_margin": 15507.46, "exposure_margin": 0.0,
            },
            "commodity": {
                "used_margin": 0, "payin_amount": 0, "span_margin": 0,
                "adhoc_margin": 0, "notional_cash": 0,
                "available_margin": 0, "exposure_margin": 0,
            },
        },
    }
    reader = BrokerAccountFundsReader(
        broker_id="upstox", fetch=lambda: response, freshness_seconds=30.0,
    )
    readings = reader.read()
    assert len(readings) == 2
    equity = next(r for r in readings if r.segment == "equity")
    assert equity.state == FRESH
    assert equity.available_margin == 15507.46
    assert equity.span_margin == 0.0


def test_no_token_is_reported_rather_than_fetched():
    calls = []
    reader = BrokerAccountFundsReader(
        broker_id="upstox",
        fetch=lambda: calls.append(1) or {},
        freshness_seconds=30.0,
        has_valid_token=lambda: False,
    )
    readings = reader.read()
    assert len(readings) == 1
    assert readings[0].state == NO_TOKEN
    assert calls == []  # never even tried


def test_a_failed_fetch_serves_the_last_known_reading_marked_by_age():
    response = {
        "status": "success",
        "data": {"equity": {
            "used_margin": 1.0, "payin_amount": 0.0, "span_margin": 0.0,
            "adhoc_margin": 0.0, "notional_cash": 0.0,
            "available_margin": 500.0, "exposure_margin": 0.0,
        }},
    }
    calls = [0]
    def flaky_fetch():
        calls[0] += 1
        if calls[0] == 1:
            return response
        raise RuntimeError("network down")

    clock = [100.0]
    reader = BrokerAccountFundsReader(
        broker_id="upstox", fetch=flaky_fetch, freshness_seconds=30.0,
        monotonic=lambda: clock[0],
    )
    first = reader.read()
    assert first[0].state == FRESH

    clock[0] += 5.0  # well within freshness
    second = reader.read()
    assert second[0].state == FRESH
    assert second[0].available_margin == 500.0
    assert "network down" in second[0].reason


def test_a_reading_past_its_freshness_bound_is_reported_stale():
    response = {
        "status": "success",
        "data": {"equity": {
            "used_margin": 1.0, "payin_amount": 0.0, "span_margin": 0.0,
            "adhoc_margin": 0.0, "notional_cash": 0.0,
            "available_margin": 500.0, "exposure_margin": 0.0,
        }},
    }
    calls = [0]
    def flaky_fetch():
        calls[0] += 1
        if calls[0] == 1:
            return response
        raise RuntimeError("network down")

    clock = [100.0]
    reader = BrokerAccountFundsReader(
        broker_id="upstox", fetch=flaky_fetch, freshness_seconds=30.0,
        monotonic=lambda: clock[0],
    )
    reader.read()
    clock[0] += 31.0  # past the freshness bound
    stale = reader.read()
    assert stale[0].state == STALE


def test_never_fetched_and_then_failing_is_unreadable_not_stale():
    reader = BrokerAccountFundsReader(
        broker_id="upstox", fetch=lambda: (_ for _ in ()).throw(RuntimeError("down")),
        freshness_seconds=30.0,
    )
    readings = reader.read()
    assert readings[0].state == UNREADABLE
