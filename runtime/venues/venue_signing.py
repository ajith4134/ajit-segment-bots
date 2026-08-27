"""How a request to a venue's private endpoint is signed, and where the key comes from.

**This file holds no secret and no default.** It reads a key and a secret from
the environment when the operator has put them there, and returns nothing when
they have not. Nothing here writes a credential anywhere, logs one, or puts one
in a reason string -- a secret that reaches a log has left the machine as far as
anyone reading that log later is concerned.

The convention is the one `api-key-pool-rotator` already states for this project:
the mechanism is complete and the key set is honestly empty (RL-062). An absent
key makes a venue's private requests an empty tuple with a named reason, never a
request that fails at the venue and never a placeholder that looks like a key.
Measured 2026-08-26: `GET /fapi/v1/leverageBracket` unsigned returns
`{"code":-2014,"msg":"API-key format invalid."}`, so an unsigned attempt is a
request spent against a rate limit to learn what is already known here.

To supply one, the operator exports two variables before the spine starts:

    AJIT_BINANCE_USDM_API_KEY=...
    AJIT_BINANCE_USDM_API_SECRET=...

The venue id is upper-cased with `-` replaced by `_`. Both must be present: a key
without its secret cannot sign, and treating that as usable would produce
requests the venue rejects with a message naming the key.

**A read-only key is enough for everything here.** Nothing in this file places an
order, and the only private endpoint any adapter names is a leverage-bracket
read. A key with trading permission would grant this process authority it has no
code to use.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import urllib.parse
from dataclasses import dataclass

KEY_VARIABLE = "AJIT_{venue}_API_KEY"
SECRET_VARIABLE = "AJIT_{venue}_API_SECRET"

NO_KEY_CONFIGURED = "no-api-key-is-configured-for-this-venue"
ONLY_A_KEY = "an-api-key-is-configured-without-its-secret"
ONLY_A_SECRET = "an-api-secret-is-configured-without-its-key"


@dataclass(frozen=True)
class RequestSigner:
    """Signs one venue's private requests. Never prints what it signs with.

    `__repr__` is overridden because a dataclass prints its fields and this object
    is held by adapters that appear in tracebacks. A secret in a traceback is a
    secret in the journal, and the journal is read by everything that debugs this
    system.
    """

    venue_id: str
    api_key: str
    api_secret: str

    def __repr__(self) -> str:
        return f"RequestSigner(venue_id={self.venue_id!r}, api_key=<redacted>, api_secret=<redacted>)"

    def signed_query(self, parameters: dict) -> str:
        """The query string with an HMAC-SHA256 signature appended.

        The signature is computed over the encoded query exactly as it will be
        sent, and appended last. Signing a differently-ordered or
        differently-encoded string from the one transmitted is the standard way
        this goes wrong, and it fails as an authentication error that reads like
        a bad key rather than like a bad signature.
        """
        query = urllib.parse.urlencode(parameters)
        signature = hmac.new(
            self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"{query}&signature={signature}"

    @property
    def authentication_headers(self) -> dict:
        return {"X-MBX-APIKEY": self.api_key}


def environment_variable_names(venue_id: str) -> tuple[str, str]:
    """What the operator exports to supply this venue's key."""
    name = venue_id.upper().replace("-", "_")
    return KEY_VARIABLE.format(venue=name), SECRET_VARIABLE.format(venue=name)


def signer_for(venue_id: str, environment=None) -> tuple[RequestSigner | None, str]:
    """This venue's signer if both halves are present, and why not if either is missing.

    Returns `(signer, reason)`. The reason is always safe to print: it names the
    variables that were looked for and says which was absent, never any part of a
    value that was found.
    """
    source = os.environ if environment is None else environment
    key_name, secret_name = environment_variable_names(venue_id)
    key = (source.get(key_name) or "").strip()
    secret = (source.get(secret_name) or "").strip()

    if key and secret:
        return RequestSigner(venue_id=venue_id, api_key=key, api_secret=secret), "configured"
    if key:
        return None, f"{ONLY_A_SECRET}: {secret_name} is not set"
    if secret:
        return None, f"{ONLY_A_KEY}: {key_name} is not set"
    return None, f"{NO_KEY_CONFIGURED}: neither {key_name} nor {secret_name} is set"


__all__ = [
    "NO_KEY_CONFIGURED",
    "ONLY_A_KEY",
    "ONLY_A_SECRET",
    "RequestSigner",
    "environment_variable_names",
    "signer_for",
]
