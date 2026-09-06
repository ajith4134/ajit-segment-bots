"""Every HTTP request this project makes to a broker, built one way.

There is one reason this module exists and it is a measured one.

**Upstox's API sits behind Cloudflare, and Cloudflare bans the stdlib's default
User-Agent.** A request carrying `Python-urllib/3.14` is answered with

    HTTP 403  Error 1010: browser_signature_banned
    "The site owner has blocked access based on your browser's signature."
    "**Do not retry.** Your user-agent has been banned by the site owner."

It is not an auth failure, it does not depend on the token, and it never
recovers on its own -- Cloudflare's own body says not to retry. Measured on the
live spine 2026-09-06, every REST caller in the project was failing every call:

    broker-margin-quoter          calls_made 1,016   calls_failed 1,016   quotes_read 0
    broker-account-funds-reader   reads         34   failures         34

`broker-margin-quoter` is the only producer of `broker-margin-requirement`, and
`leverage-selector` sizes the 5x intraday equity bot against it, so the whole
leverage path was reading nothing while every counter looked like a part doing
its job. Nothing reported a fault: `last_failure` holds the reason, and
`countable_standing` keeps numbers only, so the one field that explained a
thousand failures was dropped before it reached any board.

**A real User-Agent is the whole fix.** Verified against the live API the same
day, same token, same payload:

    no User-Agent    margin -> 403 (cf 1010)    funds -> 403 (cf 1010)
    this User-Agent  margin -> 200              funds -> 200

so no browser impersonation is needed, and none is done here. The string names
this client and where it comes from, which is what a User-Agent is for and what
every official broker SDK sends. `broker-history-reader` reached the same
Cloudflare block on 2026-09-02 and answered it with `curl_cffi`'s
`impersonate="chrome"` -- that worked, but it was applied to exactly one of the
four call sites and the knowledge never travelled to the other three. This is
where it lives now, so a new broker caller gets it by construction rather than
by remembering.

**The header goes on `assets.upstox.com` calls too**, even though that host does
not block today. Two conventions for one broker is how the next call site gets
written without it.
"""

from __future__ import annotations

import urllib.request

# Names the client and where it comes from -- never a browser's signature. A
# request that claimed to be Chrome would be a lie told to the broker whose API
# this is authorised against, and this project makes those calls with the
# account holder's own token under their own agreement.
BROKER_HTTP_USER_AGENT = "ajit-segment-bots/1.0 (+https://github.com/ajith4134/ajit-segment-bots)"


def build_broker_request(
    url: str,
    *,
    accept: str = "application/json",
    access_token: str | None = None,
    body: bytes | None = None,
    content_type: str | None = None,
    method: str | None = None,
) -> urllib.request.Request:
    """One broker HTTP request, carrying the headers every one of them needs.

    `access_token` is optional because not every broker endpoint is
    authenticated -- the instrument master is a public file on a CDN -- and an
    `Authorization: Bearer None` would be a header stating a credential that
    does not exist.
    """
    headers = {"Accept": accept, "User-Agent": BROKER_HTTP_USER_AGENT}
    if access_token is not None:
        headers["Authorization"] = f"Bearer {access_token}"
    if content_type is not None:
        headers["Content-Type"] = content_type
    return urllib.request.Request(url, data=body, headers=headers, method=method)


__all__ = ["BROKER_HTTP_USER_AGENT", "build_broker_request"]
