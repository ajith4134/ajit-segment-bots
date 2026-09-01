"""Contract-level tests for BrokerAdapter -- fact validation and the
conformance tuples every concrete adapter (Task 2's UpstoxAdapter, and
whichever of the other five brokers docs/goal.md names is built next) is
checked against.
"""

import pytest

from runtime.brokers.broker_adapter import BrokerFact, BrokerFactWithoutSource


def test_broker_fact_refuses_construction_without_a_source():
    with pytest.raises(BrokerFactWithoutSource):
        BrokerFact(name="connections_per_user", value=2, unit="count", source="")


def test_broker_fact_accepts_a_real_source():
    fact = BrokerFact(
        name="connections_per_user", value=2, unit="count",
        source="upstox v3 market-data-feed docs, fetched 2026-09-01",
    )
    assert fact.value == 2


def test_broker_adapter_conformance_tuples_name_real_methods():
    from runtime.brokers.broker_adapter import (
        BrokerAdapter,
        QUESTIONS_ANSWERED_WITHOUT_BROKER_DATA,
        QUESTIONS_ANSWERED_FROM_BROKER_DATA,
    )

    every_question = QUESTIONS_ANSWERED_WITHOUT_BROKER_DATA + QUESTIONS_ANSWERED_FROM_BROKER_DATA
    for name in every_question:
        assert hasattr(BrokerAdapter, name), f"{name} is not a method on BrokerAdapter"
    # No overlap: a question needs live data or it doesn't, never both lists.
    assert not set(QUESTIONS_ANSWERED_WITHOUT_BROKER_DATA) & set(QUESTIONS_ANSWERED_FROM_BROKER_DATA)
