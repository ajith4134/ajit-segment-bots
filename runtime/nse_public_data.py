"""One warmed session for NSE's own public endpoints.

Two facts, both verified against the live site 2026-09-02:

1. `urllib.request` is refused with a Cloudflare 403 -- the same wall
   `broker-market-feed-reader` hit and answered the same way, with
   `curl_cffi` impersonating a real browser's TLS fingerprint.
2. An `/api/` call from a session that has not yet fetched the homepage is
   refused. The homepage sets the cookie every later call is checked against,
   so it is fetched once per session and never again.

A non-200 raises. This matters more here than in most readers: an error page
parsed as a ban list is an *empty* ban list, and an empty ban list reads as
"nothing is banned today" -- the failure that looks exactly like the good case.
"""

from __future__ import annotations

NSE_HOME = "https://www.nseindia.com"
NSE_API_HOST = "https://www.nseindia.com"
NSE_ARCHIVE_HOST = "https://nsearchives.nseindia.com"
BROWSER_TO_IMPERSONATE = "chrome"


def open_browser_session():
    """A real curl_cffi session. Separated so tests never touch the network."""
    from curl_cffi import requests

    return requests.Session(impersonate=BROWSER_TO_IMPERSONATE)


class NsePublicData:
    """Fetches NSE's public files through one session, warmed once."""

    def __init__(self, session, timeout_seconds: float) -> None:
        self._session = session
        self._timeout_seconds = timeout_seconds
        self._is_warm = False
        self._requests_made = 0
        self._requests_refused = 0

    def _warm(self) -> None:
        if self._is_warm:
            return
        self._session.get(NSE_HOME, timeout=self._timeout_seconds)
        self._is_warm = True

    def _fetch(self, url: str):
        self._warm()
        self._requests_made += 1
        response = self._session.get(url, timeout=self._timeout_seconds)
        if response.status_code != 200:
            self._requests_refused += 1
            raise RuntimeError(
                f"NSE answered {response.status_code} for {url}. Refusing rather than "
                f"parsing the body: an error page read as a ban list is an empty ban "
                f"list, which reads as 'nothing is banned today'."
            )
        return response

    def read_text(self, url: str) -> str:
        return self._fetch(url).text

    def read_json(self, url: str):
        return self._fetch(url).json()

    def standing(self) -> dict:
        return {
            "is_warm": self._is_warm,
            "requests_made": self._requests_made,
            "requests_refused": self._requests_refused,
        }


__all__ = [
    "BROWSER_TO_IMPERSONATE",
    "NSE_API_HOST",
    "NSE_ARCHIVE_HOST",
    "NSE_HOME",
    "NsePublicData",
    "open_browser_session",
]
