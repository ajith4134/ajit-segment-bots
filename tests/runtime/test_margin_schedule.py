"""The maintenance margin ladder, and the key that signs the request for one.

The Bybit response replayed here is the one the venue actually returned on
2026-08-26 (RL-063), kept verbatim. It is BTCUSDT because its ladder is the
longest and spans the widest range of notional, which is what exercises the
reading that matters: this venue states a tier by its **ceiling**, so a tier's
floor is the ceiling of the one below it.

No key is present on this box and none is created here. The signing tests use a
throwaway value passed in through an explicit environment mapping, never the
process environment, so nothing in this file can pick up a real credential and
nothing it prints could contain one.
"""

import pytest

from runtime.symbol_universe import MarginTier
from runtime.venues.adapter_registry import load_venue_adapter
from runtime.venues.venue_adapter import EVERY_SYMBOL
from runtime.venues.venue_signing import (
    NO_KEY_CONFIGURED,
    ONLY_A_KEY,
    ONLY_A_SECRET,
    RequestSigner,
    environment_variable_names,
    signer_for,
)

BYBIT = "bybit-linear"
BINANCE = "binance-usdm"
RISK_LIMIT_FIXTURE = "2026-08-26-risk-limit-btcusdt.json"


@pytest.fixture
def bybit_risk_limit(read_captured_json):
    return read_captured_json(BYBIT, RISK_LIMIT_FIXTURE)


# -- the ladder ---------------------------------------------------------------

def test_bybit_states_a_tier_by_its_ceiling_so_the_first_tier_starts_at_zero(bybit_risk_limit):
    """Reading riskLimitValue as a floor mis-prices every position under it.

    Which is every position this bot takes, so the error would be total rather
    than marginal, and in the same direction every time.
    """
    tiers = load_venue_adapter(BYBIT).read_margin_tiers([bybit_risk_limit])["BTCUSDT"]

    assert tiers[0].notional_floor == 0.0
    # Each tier begins where the venue's stated ceiling for the previous one ends.
    ceilings = [float(row["riskLimitValue"]) for row in bybit_risk_limit["result"]["list"]]
    ceilings.sort()
    assert [tier.notional_floor for tier in tiers[1:]] == ceilings[:-1]


def test_the_ladder_rises_with_notional(bybit_risk_limit):
    """A bigger position is charged a higher maintenance margin; that is the point."""
    tiers = load_venue_adapter(BYBIT).read_margin_tiers([bybit_risk_limit])["BTCUSDT"]

    assert len(tiers) == 35
    floors = [tier.notional_floor for tier in tiers]
    rates = [tier.maintenance_margin_rate for tier in tiers]
    assert floors == sorted(floors)
    assert rates == sorted(rates)
    assert all(isinstance(tier, MarginTier) for tier in tiers)
    # Real figures from the real response, not a shape assertion.
    assert rates[0] == pytest.approx(0.0033)
    assert tiers[0].maximum_leverage == pytest.approx(150.0)


def test_a_response_with_nothing_readable_yields_no_ladder():
    """Absent, never a zero rate -- a zero puts every liquidation at the entry."""
    adapter = load_venue_adapter(BYBIT)
    assert adapter.read_margin_tiers([]) == {}
    assert adapter.read_margin_tiers([{"result": {"list": []}}]) == {}
    assert adapter.read_margin_tiers(["not a mapping"]) == {}


def test_bybit_asks_once_per_captured_symbol_and_needs_no_key():
    """~840 listed against 50 captured: the whole-venue form is 56 paged calls."""
    adapter = load_venue_adapter(BYBIT)
    requests = adapter.margin_schedule_requests(["BTCUSDT", "ETHUSDT"])

    assert len(requests) == 2
    assert [request.describes for request in requests] == ["BTCUSDT", "ETHUSDT"]
    assert all(request.headers is None for request in requests)
    assert all("symbol=" in request.url for request in requests)
    assert adapter.margin_schedule_unavailable_reason() is None


def test_binance_states_a_tier_by_its_floor_directly():
    """The two venues state a tier from opposite ends, and swapping them is silent."""
    response = [{
        "symbol": "BTCUSDT",
        "brackets": [
            {"bracket": 1, "initialLeverage": 125, "notionalCap": 50000,
             "notionalFloor": 0, "maintMarginRatio": 0.004, "cum": 0.0},
            {"bracket": 2, "initialLeverage": 100, "notionalCap": 600000,
             "notionalFloor": 50000, "maintMarginRatio": 0.005, "cum": 50.0},
        ],
    }]
    tiers = load_venue_adapter(BINANCE).read_margin_tiers([response])["BTCUSDT"]

    assert [tier.notional_floor for tier in tiers] == [0.0, 50000.0]
    assert [tier.maintenance_margin_rate for tier in tiers] == [0.004, 0.005]
    assert tiers[0].maximum_leverage == 125.0


# -- the key that signs for it ------------------------------------------------

def test_with_no_key_binance_makes_no_request_at_all():
    """An unsigned attempt spends a rate limit to be told what is known here.

    Measured 2026-08-26: unsigned, the endpoint answers
    {"code":-2014,"msg":"API-key format invalid."} -- and that rate limit is
    shared with the endpoints that keep the tape running.
    """
    adapter = load_venue_adapter(BINANCE)
    key_name, secret_name = environment_variable_names(BINANCE)

    signer, reason = signer_for(BINANCE, environment={})
    assert signer is None
    assert reason.startswith(NO_KEY_CONFIGURED)
    # The reason names the variables looked for, so an operator can act on it.
    assert key_name in reason and secret_name in reason
    assert adapter.margin_schedule_requests(["BTCUSDT"]) == ()


def test_half_a_key_is_refused_and_says_which_half():
    key_name, secret_name = environment_variable_names(BINANCE)

    signer, reason = signer_for(BINANCE, environment={key_name: "a-key"})
    assert signer is None and reason.startswith(ONLY_A_SECRET)

    signer, reason = signer_for(BINANCE, environment={secret_name: "a-secret"})
    assert signer is None and reason.startswith(ONLY_A_KEY)


def test_a_blank_value_counts_as_absent():
    """An exported-but-empty variable is the shape an unset one takes in a unit file."""
    key_name, secret_name = environment_variable_names(BINANCE)
    signer, reason = signer_for(BINANCE, environment={key_name: "   ", secret_name: ""})
    assert signer is None
    assert reason.startswith(NO_KEY_CONFIGURED)


def test_the_signature_is_taken_over_the_query_exactly_as_sent():
    """Signing a differently-ordered string than the one transmitted is the classic failure.

    It surfaces as an authentication error that reads like a bad key, so it is
    worth pinning against a known-answer HMAC rather than a self-consistency check.
    """
    import hashlib
    import hmac

    signer = RequestSigner(venue_id=BINANCE, api_key="a-key", api_secret="a-secret")
    query = signer.signed_query({"timestamp": 1700000000000, "recvWindow": 5000})

    sent, _, signature = query.rpartition("&signature=")
    assert sent == "timestamp=1700000000000&recvWindow=5000"
    assert signature == hmac.new(
        b"a-secret", sent.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def test_a_signer_never_prints_what_it_signs_with():
    """This object is held by adapters that appear in tracebacks.

    A secret in a traceback is a secret in the journal, and the journal is what
    everything debugging this system reads.
    """
    signer = RequestSigner(venue_id=BINANCE, api_key="the-key", api_secret="the-secret")
    printed = f"{signer!r} {signer}"

    assert "the-key" not in printed
    assert "the-secret" not in printed
    assert "redacted" in printed


def test_a_configured_key_produces_one_signed_request_for_the_whole_venue(monkeypatch):
    """One call returns every symbol's ladder, so `symbols` is ignored on purpose."""
    key_name, secret_name = environment_variable_names(BINANCE)
    monkeypatch.setenv(key_name, "a-throwaway-key")
    monkeypatch.setenv(secret_name, "a-throwaway-secret")

    adapter = load_venue_adapter(BINANCE)
    requests = adapter.margin_schedule_requests(["BTCUSDT", "ETHUSDT", "SOLUSDT"])

    assert len(requests) == 1
    assert requests[0].describes == EVERY_SYMBOL
    assert "signature=" in requests[0].url
    assert requests[0].headers == {"X-MBX-APIKEY": "a-throwaway-key"}
    # The secret signs; it is never transmitted.
    assert "a-throwaway-secret" not in requests[0].url
    assert adapter.margin_schedule_unavailable_reason() is None


def test_neither_adapter_fails_to_construct_over_this():
    """The 2026-08-25 lesson: an abstract method implemented for one venue only
    stopped the other from being constructed at all, and every part that loads an
    adapter crash-looped invisibly for hours because the running spine held the
    pre-change code in memory.
    """
    for venue_id in (BYBIT, BINANCE):
        adapter = load_venue_adapter(venue_id)
        assert isinstance(adapter.margin_schedule_requests([]), tuple)
        assert isinstance(adapter.read_margin_tiers([]), dict)
