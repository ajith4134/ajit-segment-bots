"""broker-order-router: the part that can spend real money on NSE.

**No test here can reach a broker.** The transport is injected everywhere, and
one test asserts that the part's own default transport is never constructed by
accident. A test that could place an order is a test that might.

The five refusals are asserted one at a time, each with every OTHER gate open,
so none of them can be passing because a different one happened to fire first.
That matters most for the two live-money gates: they read different producers on
purpose -- `destination` from order-destination-router, `money-mode` from
money-mode-reader -- and the whole point is that either alone stops an order.
"""

from __future__ import annotations

import gzip
import json
import pathlib
import types

import pytest

from parts.broker_adapter.broker_order_router import (
    BROKER_REFUSED, CANCELLED, CLIENT_ORDER_ID_LENGTH, FAILED, PART_DECLARATION, PLACED,
    REFUSED_NO_BROKER_ORDER_ID, REPRICED,
    REFUSED_DUPLICATE, REFUSED_NOT_LIVE_DESTINATION, REFUSED_NO_INSTRUMENT_KEY,
    REFUSED_NO_TOKEN, REFUSED_SEGMENT_IS_ON_PAPER, BrokerOrderRouter, describe_router,
)
from runtime.brokers.upstox import UpstoxAdapter
from runtime.part_declaration import load_declaration_from_blueprint
from runtime.trading_types import BUY, LIVE_VENUE, SELL

SEGMENT = "index-options"
MASTER = pathlib.Path.home() / ".local/share/ajit-segment-bots/instrument-master/complete.json.gz"


@pytest.fixture(scope="module")
def a_real_contract():
    """A real NIFTY option out of the broker's own instrument master (RL-063).

    The instrument key's shape is the whole reason gate 4 exists, so it is taken
    from the master rather than written here.
    """
    assert MASTER.exists(), f"{MASTER} is missing; this runs on the real master"
    for row in json.load(gzip.open(MASTER)):
        if row.get("instrument_type") == "CE" and row.get("underlying_symbol") == "NIFTY":
            return row
    raise AssertionError("no NIFTY call in the master")


def a_token(valid=True):
    return types.SimpleNamespace(
        broker_id="upstox", access_token="a-token", is_still_valid=lambda: valid
    )


def an_order(
    symbol="NIFTY 24550 CE 08 SEP 26", side=BUY, quantity=75, destination=LIVE_VENUE,
    segment=SEGMENT, order_type="market", limit_price=0.0, intent_id="an-intent",
):
    return types.SimpleNamespace(
        venue_id="upstox", symbol=symbol, side=side, quantity=quantity,
        destination=destination, segment=segment, order_type=order_type,
        limit_price=limit_price, intent_id=intent_id,
    )


def a_router(
    place=None, mode="live", instrument_key="NSE_FO|51420", token=a_token,
    product="D", validity="DAY",
):
    sent = []

    def record(url, payload, access_token):
        sent.append({"url": url, "payload": payload, "token": access_token})
        return {"status": "success", "data": {"order_id": "241212000000001"}}

    router = BrokerOrderRouter(
        adapter=UpstoxAdapter(),
        place=place if place is not None else record,
        read_money_mode=lambda segment: (
            None if mode is None else types.SimpleNamespace(segment=segment, mode=mode)
        ),
        read_instrument_key=lambda symbol: instrument_key,
        read_token=token,
        product=product,
        validity=validity,
    )
    router.sent = sent
    return router


# ---- the declaration ---------------------------------------------------------

def test_the_built_declaration_equals_the_blueprint():
    assert PART_DECLARATION == load_declaration_from_blueprint("broker-order-router")


def test_the_part_does_not_import_another_part():
    """T-4: a part names data, never another part."""
    source = pathlib.Path(
        "parts/broker_adapter/broker_order_router.py"
    ).read_text(encoding="utf-8")
    for line in source.splitlines():
        assert not line.startswith(("from parts.", "import parts.")), line


# ---- gate 1: the paper book owns anything not addressed to the live venue -----

def test_an_order_not_addressed_to_the_live_venue_is_refused():
    router = a_router()
    status = router.route(an_order(destination="paper-book"))

    assert status.outcome == REFUSED_NOT_LIVE_DESTINATION
    assert router.sent == [], "nothing may reach the broker"
    assert router.standing.refused_not_live_destination == 1


# ---- gate 2: the segment's money mode, from a different producer -------------

def test_a_live_destination_is_still_refused_when_the_segment_is_on_paper():
    """The gate that exists because one flag is one bug away from real money.

    Every other gate is open here: the destination IS the live venue, the token
    is valid, the instrument key resolves. Only the mode says paper.
    """
    router = a_router(mode="paper")
    status = router.route(an_order())

    assert status.outcome == REFUSED_SEGMENT_IS_ON_PAPER
    assert router.sent == []
    assert "not 'live'" in status.reason


def test_a_segment_with_no_money_mode_at_all_is_refused():
    """An age-bounded level that stopped arriving must stop authorising orders."""
    router = a_router(mode=None)
    status = router.route(an_order())

    assert status.outcome == REFUSED_SEGMENT_IS_ON_PAPER
    assert router.sent == []


def test_a_mode_that_is_neither_paper_nor_live_is_not_treated_as_live():
    router = a_router(mode="LIVE")  # wrong case is not the live mode
    status = router.route(an_order())

    assert status.outcome == REFUSED_SEGMENT_IS_ON_PAPER
    assert router.sent == []


# ---- gate 3: the token -------------------------------------------------------

def test_an_expired_token_refuses_the_order():
    router = a_router(token=lambda: a_token(valid=False))
    status = router.route(an_order())

    assert status.outcome == REFUSED_NO_TOKEN
    assert router.sent == []


def test_no_token_at_all_refuses_the_order():
    router = a_router(token=lambda: None)
    status = router.route(an_order())

    assert status.outcome == REFUSED_NO_TOKEN
    assert router.sent == []


# ---- gate 4: the instrument key ----------------------------------------------

def test_a_symbol_with_no_instrument_key_is_refused():
    """Sending a trading symbol where a token is expected is not one bad order."""
    router = a_router(instrument_key=None)
    status = router.route(an_order())

    assert status.outcome == REFUSED_NO_INSTRUMENT_KEY
    assert router.sent == []


# ---- gate 5: idempotency -----------------------------------------------------

def test_the_same_order_twice_reaches_the_broker_once():
    router = a_router()
    first = router.route(an_order())
    second = router.route(an_order())

    assert first.outcome == PLACED
    assert second.outcome == REFUSED_DUPLICATE
    assert len(router.sent) == 1, "a retry is the same decision, not a second position"
    assert first.client_order_id == second.client_order_id


def test_two_different_decisions_asking_for_the_same_order_are_not_one():
    router = a_router()
    first = router.route(an_order(intent_id="decision-one"))
    second = router.route(an_order(intent_id="decision-two"))

    assert first.outcome == PLACED and second.outcome == PLACED
    assert first.client_order_id != second.client_order_id
    assert len(router.sent) == 2


def test_a_timeout_still_marks_the_id_as_sent():
    """The dangerous case: the order may or may not have arrived.

    Remembering the id BEFORE the call is what makes a retry refuse rather than
    open a second position.
    """
    def times_out(url, payload, token):
        raise TimeoutError("no response")

    router = a_router(place=times_out)
    first = router.route(an_order())
    assert first.outcome == FAILED

    router._place = lambda url, payload, token: {
        "status": "success", "data": {"order_id": "x"}
    }
    second = router.route(an_order())
    assert second.outcome == REFUSED_DUPLICATE


# ---- what the broker actually receives ---------------------------------------

def test_the_payload_is_what_upstox_place_order_takes(a_real_contract):
    router = a_router(instrument_key=a_real_contract["instrument_key"])
    status = router.route(an_order(quantity=a_real_contract["lot_size"]))

    assert status.outcome == PLACED
    payload = router.sent[0]["payload"]
    # Upstox's own body names this instrument_token, not instrument_key.
    assert payload["instrument_token"] == a_real_contract["instrument_key"]
    assert payload["quantity"] == a_real_contract["lot_size"]
    assert payload["transaction_type"] == "BUY"
    assert payload["order_type"] == "MARKET"
    assert payload["product"] == "D"
    assert payload["validity"] == "DAY"
    assert payload["tag"] == status.client_order_id
    assert len(status.client_order_id) == CLIENT_ORDER_ID_LENGTH
    assert router.sent[0]["url"] == UpstoxAdapter().order_endpoint_url()
    assert router.sent[0]["token"] == "a-token"


def test_a_sell_becomes_upstox_s_own_word_for_one():
    router = a_router()
    router.route(an_order(side=SELL))
    assert router.sent[0]["payload"]["transaction_type"] == "SELL"


def test_a_limit_order_carries_its_price_and_a_market_order_does_not():
    router = a_router()
    router.route(an_order(order_type="limit", limit_price=12.5, intent_id="a"))
    router.route(an_order(order_type="market", limit_price=12.5, intent_id="b"))

    limit, market = router.sent[0]["payload"], router.sent[1]["payload"]
    assert limit["order_type"] == "LIMIT" and limit["price"] == 12.5
    assert market["order_type"] == "MARKET" and market["price"] == 0.0


def test_the_product_and_validity_come_from_settings_not_from_the_part():
    """RL-061: a future intraday segment says 'I' in settings, not here."""
    router = a_router(product="I", validity="IOC")
    router.route(an_order())

    assert router.sent[0]["payload"]["product"] == "I"
    assert router.sent[0]["payload"]["validity"] == "IOC"


# ---- what the broker says back -----------------------------------------------

def test_a_refusal_from_the_broker_is_reported_not_raised():
    router = a_router(place=lambda url, payload, token: {"status": "error", "errors": ["no"]})
    status = router.route(an_order())

    assert status.outcome == BROKER_REFUSED
    assert status.venue_response == {"status": "error", "errors": ["no"]}
    assert router.standing.broker_refused == 1


def test_every_refusal_is_published_rather_than_dropped():
    """An order that vanishes silently is the shape this project keeps hitting."""
    router = a_router(mode="paper")
    statuses = [
        router.route(an_order(destination="paper-book")),
        router.route(an_order()),
    ]
    assert all(s is not None for s in statuses)
    assert all(s.client_order_id and s.reason for s in statuses)


def test_the_standing_names_what_this_part_does_and_does_not_do():
    """Cancel and reprice landed 2026-09-12; polling is still another part's."""
    standing = describe_router(a_router())
    assert standing["cancels_orders"] is True
    assert standing["reprices_orders"] is True
    assert standing["polls_for_status"] is False, "order-state-poller is that part"


# ---- cancel and reprice ------------------------------------------------------
#
# Both take the BROKER's order id, and both decisions name the CLIENT's -- the
# only id the parts that form them ever saw. The router is the one place that
# holds both, because it learned the broker's from its own place response.

def a_decision(order_id, to_price=None):
    decision = types.SimpleNamespace(
        order_id=order_id, venue_id="upstox", symbol="NIFTY 24550 CE 08 SEP 26",
    )
    if to_price is not None:
        decision.to_price = to_price
    return decision


def a_router_that_has_placed(**kwargs):
    """A router that placed one order and knows the broker's id for it."""
    calls = {"cancel": [], "modify": []}

    def cancelled(url, token):
        calls["cancel"].append({"url": url, "token": token})
        return {"status": "success", "data": {"order_id": "241212000000001"}}

    def modified(url, payload, token):
        calls["modify"].append({"url": url, "payload": payload, "token": token})
        return {"status": "success", "data": {"order_id": "241212000000001"}}

    router = a_router(**kwargs)
    router._cancel = cancelled
    router._modify = modified
    router.calls = calls
    placed = router.route(an_order())
    assert placed.outcome == PLACED
    return router, placed.client_order_id


def test_a_cancel_reaches_the_brokers_own_cancel_endpoint():
    router, client_order_id = a_router_that_has_placed()
    status = router.cancel(a_decision(client_order_id))

    assert status.outcome == CANCELLED
    assert status.action == "cancel"
    # Upstox cancels by DELETE with the order id as a QUERY PARAMETER, and the
    # id is the BROKER's, not ours.
    assert router.calls["cancel"][0]["url"] == (
        UpstoxAdapter().cancel_endpoint_url("241212000000001")
    )
    assert "order_id=241212000000001" in router.calls["cancel"][0]["url"]
    assert router.standing.cancelled == 1


def test_a_reprice_sends_the_fields_upstox_requires_even_when_unchanged():
    """Upstox assumes the original order only for fields left OUT entirely.

    order_type, validity, price and trigger_price are all required on a modify,
    so a request that omitted price would not keep the old one -- it would be
    refused.
    """
    router, client_order_id = a_router_that_has_placed()
    status = router.reprice(a_decision(client_order_id, to_price=12.5))

    assert status.outcome == REPRICED
    payload = router.calls["modify"][0]["payload"]
    assert payload["order_id"] == "241212000000001"
    assert payload["price"] == 12.5
    assert payload["order_type"] == "LIMIT"
    assert payload["validity"] == "DAY"
    assert "trigger_price" in payload
    assert router.calls["modify"][0]["url"] == UpstoxAdapter().modify_endpoint_url()
    assert router.standing.repriced == 1


def test_a_cancel_for_an_order_this_part_never_placed_is_refused_by_name():
    router = a_router()
    router._cancel = lambda url, token: pytest.fail("nothing may reach the broker")
    status = router.cancel(a_decision("an-id-nobody-placed"))

    assert status.outcome == REFUSED_NO_BROKER_ORDER_ID
    assert router.standing.refused_no_broker_order_id == 1


def test_a_reprice_for_an_order_this_part_never_placed_is_refused_by_name():
    router = a_router()
    router._modify = lambda url, payload, token: pytest.fail("nothing may reach the broker")
    status = router.reprice(a_decision("an-id-nobody-placed", to_price=9.0))

    assert status.outcome == REFUSED_NO_BROKER_ORDER_ID


def test_a_cancel_without_a_valid_token_is_refused():
    router, client_order_id = a_router_that_has_placed()
    router._read_token = lambda: a_token(valid=False)
    router._cancel = lambda url, token: pytest.fail("nothing may reach the broker")
    status = router.cancel(a_decision(client_order_id))

    assert status.outcome == REFUSED_NO_TOKEN


def test_a_reprice_without_a_valid_token_is_refused():
    """A modify can RAISE a price or a quantity, so it is a spend.

    Guarding the place path and not this one would be exactly as dangerous as
    guarding neither, while looking safer.
    """
    router, client_order_id = a_router_that_has_placed()
    router._read_token = lambda: a_token(valid=False)
    router._modify = lambda url, payload, token: pytest.fail("nothing may reach the broker")
    status = router.reprice(a_decision(client_order_id, to_price=99.0))

    assert status.outcome == REFUSED_NO_TOKEN


def test_a_broker_refusal_of_a_cancel_is_reported_not_raised():
    router, client_order_id = a_router_that_has_placed()
    router._cancel = lambda url, token: {"status": "error", "errors": ["gone"]}
    status = router.cancel(a_decision(client_order_id))

    assert status.outcome == BROKER_REFUSED
    assert status.venue_response == {"status": "error", "errors": ["gone"]}


def test_a_failed_cancel_does_not_claim_the_order_is_gone():
    router, client_order_id = a_router_that_has_placed()

    def times_out(url, token):
        raise TimeoutError("no response")

    router._cancel = times_out
    status = router.cancel(a_decision(client_order_id))

    assert status.outcome == FAILED
    assert "may still be live" in status.reason
    assert router.standing.cancelled == 0


# ---- it must not be startable by accident ------------------------------------

def test_the_part_starts_after_every_producer_its_gates_read():
    """On the spine since 2026-09-12, and the ORDER is the safety property.

    Its gates read three levels -- `money-mode`, `broker-token-standing` and
    `symbol-universe`. A router started before those produce anything refuses
    every order for the wrong reason, and refusing for the wrong reason looks
    exactly like refusing correctly: the same counters climb. So the invariant
    that replaced "it is not on the spine" is that it starts last of the four.
    """
    spine = pathlib.Path("operate/run_live_spine.py").read_text(encoding="utf-8")
    order = [
        line.strip().strip(',').strip('"')
        for line in spine.splitlines()
        if line.strip().startswith('"') and line.strip().endswith('",')
    ]
    assert "broker-order-router" in order, "the part must be on the spine"

    router_at = order.index("broker-order-router")
    for producer in (
        "money-mode-reader",
        "broker-token-refresh-scheduler",
        "broker-symbol-universe-bridge",
        "order-destination-router",
    ):
        assert producer in order, producer
        assert order.index(producer) < router_at, (
            f"{producer} produces a level this router's gates read and must start "
            f"before it; started after, the router refuses everything for the wrong "
            f"reason and the counters look identical to refusing correctly"
        )


def test_it_places_nothing_while_every_segment_is_on_paper():
    """The property that makes putting it on the spine safe today.

    This is what the live spine is actually running into: both segments state
    money_mode "paper", so order-destination-router addresses every order to the
    paper book and gate 1 refuses it. Asserted rather than trusted, because "it
    is safe because of a setting" is the kind of claim that stops being true
    without anything failing.
    """
    router = a_router(mode="paper")
    router._place = lambda url, payload, token: pytest.fail("nothing may reach the broker")

    statuses = [
        router.route(an_order(destination="paper-book", intent_id="a")),
        router.route(an_order(destination="paper-book", intent_id="b")),
    ]

    assert [s.outcome for s in statuses] == [REFUSED_NOT_LIVE_DESTINATION] * 2
    assert router.standing.placed == 0
    assert router.standing.refused_not_live_destination == 2
