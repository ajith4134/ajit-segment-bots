"""The transports that answer an `llm-request`, and where their credentials live.

Until 2026-09-07 nothing answered one. `llm-model-picker` reported
`models_declared` 0, `local-model-caller` reported `is_available` 0, and neither
the metered nor the subscription caller had ever made a call -- so
`skill-distiller` published 88 `llm-request`s that went nowhere, and the whole
knowledge chain behind them sat in a bootstrap cycle: a model is needed to make a
skill, a skill to write the prompt template, and the template to call the model.

Two transports, because they fail in completely different ways:

**The subscription** is Claude Code itself, driven with `claude -p`. That is a
first-party interface that runs on the operator's own Claude subscription, and it
is the only one here that needs no credential -- the CLI already holds the
operator's login. It is deliberately NOT a reverse-engineered claude.ai session:
scraping the web app or lifting its token to imitate the API is circumventing
what the subscription is sold as, and this project does not do that.

**The metered endpoints** are OpenAI-shaped chat completions. Kimi (Moonshot),
Groq, Cerebras, OpenRouter and DeepSeek all speak the same request and response
body, so one client covers every one of them and adding a provider is a row in a
settings table rather than a new module. Each needs an API key, and a provider
whose key is absent is reported absent rather than tried -- a caller that fires
at an endpoint with no credential spends a retry budget to learn what the store
already knew.

**No key is ever read from a settings file or an environment variable this
project writes.** They come from the encrypted store (`docs/secrets.md`), which
is decrypted to a pipe and never to disk, and a `PLACEHOLDER_` value is refused
by name rather than sent -- signing with one produces a confusing provider error
instead of an obvious local one.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

# What one call is allowed to take before it is abandoned. A model that has not
# answered in this long has not answered: the request behind it is about a market
# that has moved, and the caller's own retry budget is spent waiting.
DEFAULT_TIMEOUT_SECONDS = 120.0

# Refused by name rather than sent. `docs/secrets.md` rule 2.
PLACEHOLDER_PREFIX = "PLACEHOLDER_"


class NoCredential(RuntimeError):
    """No usable key is installed for this provider."""


@dataclass(frozen=True)
class OpenAiShapedProvider:
    """One chat-completions endpoint, and what it costs.

    `cost_per_input_token` and `cost_per_output_token` are zero for a free tier,
    which is a real price and not a missing one -- `metered-api-caller` prices a
    call before making it, and a provider with no price would be unpriceable
    rather than free.
    """

    provider_id: str
    base_url: str
    model_id: str
    secret_field: str
    cost_per_input_token: float = 0.0
    cost_per_output_token: float = 0.0
    typical_latency_seconds: float = 6.0


def claude_code_session_call(
    model: str = "haiku",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    system_prompt: str = "You answer only from the facts given. State what they show, briefly.",
    run=None,
):
    """`call(routed) -> (text, finish_reason, in_tokens, out_tokens, seconds)`.

    The shape `subscription-session-caller.install_session` asks for.

    **Measured on this box, 2026-09-07.** A bare `claude -p` from the project
    directory loaded 24,107 cache-creation tokens and picked Opus, costing $0.245
    for a one-word answer. Stripped -- `--model haiku`, an explicit
    `--system-prompt`, dynamic sections excluded, slash commands and MCP off --
    the same answer cost $0.041. The floor is about 20,000 cache-creation tokens
    because the operator's own `~/.claude/CLAUDE.md` loads whatever the working
    directory is, and cache creation is charged once and then read cheaply, so
    sustained use amortises and a burst does not.

    That figure is why this transport is for few, high-value calls and the
    metered providers are for bulk. `llm-request-router` owns that decision; this
    module only makes the call it is told to make.
    """
    def call(routed):
        started = time.monotonic()
        command = [
            "claude", "-p", str(routed.text),
            "--output-format", "json",
            "--model", model,
            "--system-prompt", system_prompt,
            "--exclude-dynamic-system-prompt-sections",
            "--disable-slash-commands",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        ]
        runner = run or subprocess.run
        try:
            finished = runner(
                command, capture_output=True, text=True, timeout=timeout_seconds,
                # An empty cwd so a project's own CLAUDE.md is not loaded on top
                # of the operator's. It cannot avoid the operator's own.
                cwd="/tmp",
            )
        except subprocess.TimeoutExpired:
            return ("", "timeout", 0, 0, time.monotonic() - started)
        except (OSError, ValueError):
            return ("", "error", 0, 0, time.monotonic() - started)
        if finished.returncode != 0:
            return ("", "error", 0, 0, time.monotonic() - started)
        try:
            answer = json.loads(finished.stdout)
        except ValueError:
            return ("", "error", 0, 0, time.monotonic() - started)
        usage = answer.get("usage") or {}
        return (
            str(answer.get("result") or ""),
            str(answer.get("stop_reason") or "end_turn"),
            int(usage.get("input_tokens") or 0)
            + int(usage.get("cache_creation_input_tokens") or 0)
            + int(usage.get("cache_read_input_tokens") or 0),
            int(usage.get("output_tokens") or 0),
            time.monotonic() - started,
        )

    return call


def openai_shaped_call(provider: OpenAiShapedProvider, api_key: str,
                       timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, read=None):
    """`call(routed) -> (text, finish_reason, in_tokens, out_tokens, seconds, cost)`.

    The shape `metered-api-caller.install_endpoint` asks for. One function for
    Kimi, Groq, Cerebras, OpenRouter and DeepSeek: all five answer the same
    `/chat/completions` body, so a new provider is a settings row.

    The cost is computed from the provider's own declared per-token prices and the
    tokens the provider itself reports, never estimated after the fact from the
    text -- a free tier reports zero and that is a measurement, not an absence.
    """
    if not api_key or api_key.startswith(PLACEHOLDER_PREFIX):
        raise NoCredential(
            f"{provider.provider_id} has no usable key: a placeholder is not a "
            f"credential (docs/secrets.md rule 2)"
        )

    def call(routed):
        started = time.monotonic()
        body = json.dumps({
            "model": provider.model_id,
            "messages": [{"role": "user", "content": str(routed.text)}],
        }).encode()
        request = urllib.request.Request(
            provider.base_url,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                answer = json.loads(response.read())
        except urllib.error.HTTPError as refusal:
            # 429 is the free tier's own answer and is not a fault of the request.
            # Reported by name so `metered-api-caller` counts it as rate limiting
            # rather than as a failure.
            reason = "rate-limited" if refusal.code == 429 else "error"
            return ("", reason, 0, 0, time.monotonic() - started, 0.0)
        except (urllib.error.URLError, OSError, ValueError):
            return ("", "error", 0, 0, time.monotonic() - started, 0.0)

        choices = answer.get("choices") or []
        text = ""
        finish = "error"
        if choices:
            text = str((choices[0].get("message") or {}).get("content") or "")
            finish = str(choices[0].get("finish_reason") or "stop")
        usage = answer.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        cost = (
            input_tokens * provider.cost_per_input_token
            + output_tokens * provider.cost_per_output_token
        )
        return (text, finish, input_tokens, output_tokens,
                time.monotonic() - started, cost)

    return call


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "NoCredential",
    "OpenAiShapedProvider",
    "claude_code_session_call",
    "openai_shaped_call",
]
