"""The header that turned every Upstox REST call in this project from 403 to 200.

Measured against the live API on 2026-09-06, same token and same payload:
without a User-Agent both the margin and the funds endpoint answered
`403 Error 1010: browser_signature_banned`; with this one both answered 200.
On the live spine at that moment broker-margin-quoter read calls_made 1,016 /
calls_failed 1,016 and broker-account-funds-reader read reads 34 / failures 34.
"""

from runtime.brokers.broker_http_request import (
    BROKER_HTTP_USER_AGENT, build_broker_request,
)


def test_every_request_carries_a_user_agent():
    """The whole reason this module exists: Cloudflare bans urllib's default."""
    request = build_broker_request("https://api.upstox.com/v2/user/get-funds-and-margin")
    assert request.get_header("User-agent") == BROKER_HTTP_USER_AGENT


def test_the_user_agent_names_this_client_and_never_impersonates_a_browser():
    """A request claiming to be Chrome would be a lie told to the broker whose
    API this is authorised against, with the account holder's own token."""
    assert "ajit-segment-bots" in BROKER_HTTP_USER_AGENT
    lowered = BROKER_HTTP_USER_AGENT.lower()
    for browser_signature in ("mozilla", "chrome", "safari", "applewebkit", "gecko"):
        assert browser_signature not in lowered


def test_a_token_becomes_a_bearer_header():
    request = build_broker_request("https://api.upstox.com/v2/charges/margin", access_token="abc")
    assert request.get_header("Authorization") == "Bearer abc"


def test_an_unauthenticated_call_states_no_credential_at_all():
    """The instrument master is a public file on a CDN. `Bearer None` would be
    a header stating a credential that does not exist."""
    request = build_broker_request(
        "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz",
        accept="application/gzip",
    )
    assert request.get_header("Authorization") is None
    assert request.get_header("Accept") == "application/gzip"
    # The header still travels on the CDN host: two conventions for one broker
    # is how the next call site gets written without it.
    assert request.get_header("User-agent") == BROKER_HTTP_USER_AGENT


def test_a_post_carries_its_body_method_and_content_type():
    request = build_broker_request(
        "https://api.upstox.com/v2/charges/margin",
        access_token="abc",
        body=b'{"instruments": []}',
        content_type="application/json",
        method="POST",
    )
    assert request.get_method() == "POST"
    assert request.data == b'{"instruments": []}'
    assert request.get_header("Content-type") == "application/json"


def test_a_get_states_no_content_type_it_has_no_body_for():
    request = build_broker_request("https://api.upstox.com/v2/user/get-funds-and-margin")
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.get_header("Content-type") is None
