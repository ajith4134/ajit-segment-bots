"""The LLM transports: what they return, and what they refuse to send.

The subscription transport is exercised against the real `claude -p` under the
`network` marker, because that is the only way to know it still answers -- the
CLI's flags and output shape are not this project's to freeze. Everything else
runs with an injected runner and needs nothing.
"""

from __future__ import annotations

import json
import subprocess
import types

import pytest

from runtime.llm_providers import (
    NoCredential, OpenAiShapedProvider, claude_code_session_call, openai_shaped_call,
)


class Routed:
    """A routed request, as a caller hands one to a transport -- by shape."""

    def __init__(self, text="what do these facts show?", model_id="haiku", routed_id="r1"):
        self.text = text
        self.model_id = model_id
        self.routed_id = routed_id


A_PROVIDER = OpenAiShapedProvider(
    provider_id="kimi", base_url="https://api.moonshot.ai/v1/chat/completions",
    model_id="kimi-k2-0905-preview", secret_field="moonshot_api.api_key",
    cost_per_input_token=0.0000006, cost_per_output_token=0.0000025,
)


def test_a_placeholder_key_is_refused_by_name_and_never_sent():
    """`docs/secrets.md` rule 2: a placeholder is not a credential. Sending one
    produces a confusing provider error instead of an obvious local one."""
    with pytest.raises(NoCredential):
        openai_shaped_call(A_PROVIDER, "PLACEHOLDER_replace_with_the_real_key")
    with pytest.raises(NoCredential):
        openai_shaped_call(A_PROVIDER, "")


def test_the_cost_is_the_providers_own_reported_tokens_not_an_estimate():
    """A price computed from the text this system sent would disagree with the
    bill. The provider counts the tokens; this multiplies its own declared price
    by that count."""
    body = json.dumps({
        "choices": [{"message": {"content": "an answer"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1_000, "completion_tokens": 200},
    }).encode()

    class Response:
        def read(self): return body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import runtime.llm_providers as module
    original = module.urllib.request.urlopen
    module.urllib.request.urlopen = lambda *a, **k: Response()
    try:
        text, finish, tokens_in, tokens_out, seconds, cost = openai_shaped_call(
            A_PROVIDER, "sk-real-looking-key"
        )(Routed())
    finally:
        module.urllib.request.urlopen = original

    assert text == "an answer" and finish == "stop"
    assert (tokens_in, tokens_out) == (1_000, 200)
    assert cost == pytest.approx(1_000 * 0.0000006 + 200 * 0.0000025)


def test_a_free_tier_reports_zero_cost_which_is_a_price_not_an_absence():
    """`metered-api-caller` prices a call before making it and refuses what it
    cannot price, so a free tier has to declare zero rather than nothing."""
    free = OpenAiShapedProvider(
        provider_id="groq", base_url="https://api.groq.com/openai/v1/chat/completions",
        model_id="llama-3.3-70b-versatile", secret_field="groq_api.api_key",
    )
    assert free.cost_per_input_token == 0.0
    assert free.cost_per_output_token == 0.0


def test_a_subscription_call_that_times_out_answers_rather_than_raises():
    """Every caller here counts a failed call and retries it. A transport that
    raised would take the part off the air instead."""
    def explode(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    text, finish, tokens_in, tokens_out, seconds = claude_code_session_call(run=explode)(Routed())

    assert text == "" and finish == "timeout"
    assert (tokens_in, tokens_out) == (0, 0)


def test_a_subscription_call_reads_the_tokens_the_cli_reports():
    """Cache creation and cache reads are input tokens the operator's allowance
    pays for, and they dominate: about 20,000 of them are the floor on this box
    whatever is asked, against 2 tokens of actual prompt."""
    payload = json.dumps({
        "result": "ready",
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 2, "cache_creation_input_tokens": 20_176,
            "cache_read_input_tokens": 0, "output_tokens": 38,
        },
    })
    run = lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=payload, stderr="")

    text, finish, tokens_in, tokens_out, seconds = claude_code_session_call(run=run)(Routed())

    assert text == "ready" and finish == "end_turn"
    assert tokens_in == 2 + 20_176
    assert tokens_out == 38


@pytest.mark.network
def test_the_real_claude_code_transport_still_answers():
    """The CLI's flags and output shape are not this project's to freeze, so the
    only honest check is the real one."""
    text, finish, tokens_in, tokens_out, seconds = claude_code_session_call()(
        Routed(text="Reply with exactly the word: ready")
    )

    assert "ready" in text.lower(), f"got {text!r}"
    assert finish == "end_turn"
    assert tokens_in > 0 and tokens_out > 0
