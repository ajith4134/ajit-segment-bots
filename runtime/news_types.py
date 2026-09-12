"""What a news item is, before and after this system has understood it.

The `stock-market-news-data` feature is 29 parts and, until 2026-09-12, five of
them ran and **nothing produced `raw-news-item` at all** — so the whole chain
below the sources was starved at its head. Building any downstream part before a
source would have achieved nothing, which is why the source came first and why
this type exists now rather than earlier.

`raw-news-item` is the blueprint's own description, made real:

> One item exactly as its source delivered it: source id, url, title, body,
> published_at_ns as the source stamped it, observed_at_ns as this system first
> saw it. Both times, always -- replaying on publish time alone hands a backtest
> information the live bot did not have.

That last sentence is the whole reason two timestamps are mandatory rather than
convenient, and it is enforced here rather than left to each reader.
"""

from __future__ import annotations

from dataclasses import dataclass

# Epoch milliseconds to nanoseconds. Upstox stamps `published_time` in
# milliseconds; this project reasons in nanoseconds everywhere else, and mixing
# the two is the timestamp trap `clock-skew-monitor` exists to catch.
NANOSECONDS_PER_MILLISECOND = 1_000_000


@dataclass(frozen=True)
class RawNewsItem:
    """One news item exactly as its source delivered it.

    **Both timestamps, always.** `published_at_ns` is when the source says the
    item was published; `observed_at_ns` is when this system first saw it. A
    replay driven on publish time alone hands a backtest information the live
    bot did not have — the item is dated 09:15 and did not reach this machine
    until 09:47, and a strategy that traded it at 09:15 never existed.

    Nothing here is interpreted. The title and body are the source's own words,
    unparsed and unsummarised: `news-text-structurer` is the part that reads
    them, and doing it here would put an LLM in the fetch path.
    """

    source_id: str
    url: str
    title: str
    body: str
    published_at_ns: int
    observed_at_ns: int
    # The instrument key the source returned this item under, where it returned
    # one. Carried rather than resolved: `news-symbol-resolver` resolves the
    # instruments an item NAMES, against the broker's own listings, and that is
    # a different question from which key it arrived under. A broker returning
    # an item under RELIANCE does not make the item about RELIANCE only.
    returned_under_instrument_key: str | None = None

    @property
    def latency_ns(self) -> int:
        """How long this item took to reach this system.

        Negative is possible and is not corrected: a source whose clock runs
        ahead of this machine's is a fact `news-latency-meter` should see, and
        clamping it to zero would hide exactly the skew that matters.
        """
        return self.observed_at_ns - self.published_at_ns


def published_at_ns_from_milliseconds(published_time_ms) -> int | None:
    """A source's millisecond stamp as nanoseconds, or None if it said nothing.

    None rather than zero, and the difference matters: zero is 1970 and would
    make every undated item look like the oldest news in the system, which is
    precisely backwards for a part whose job is to find what is new.
    """
    if published_time_ms in (None, ""):
        return None
    try:
        return int(published_time_ms) * NANOSECONDS_PER_MILLISECOND
    except (TypeError, ValueError):
        return None


__all__ = [
    "NANOSECONDS_PER_MILLISECOND",
    "RawNewsItem",
    "published_at_ns_from_milliseconds",
]
