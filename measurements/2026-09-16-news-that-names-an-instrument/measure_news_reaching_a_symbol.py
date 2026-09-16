"""How many captured news stories can be tied to a tradable instrument?

`news-symbol-resolver` matched only `names_mentioned` -- the company names a
model read out of the text. Nothing on this box calls a model
(`knowledge-embedder` has none installed, so the whole prompt chain refuses), so
that field arrives empty and 162 of 162 structured items tagged nothing
tradable. Meanwhile the broker's news API is asked one instrument at a time and
answers with that instrument's stories, so every raw item already carries the key
it was returned under.

This replays the real captured news tape through the real instrument master and
the real resolver, and answers both halves: what the names alone resolve, and
what the source's own key resolves.

Run: .venv/bin/python measurements/2026-09-16-news-that-names-an-instrument/measure_news_reaching_a_symbol.py
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from parts.stock_market_news_data.news_symbol_resolver import NewsSymbolResolver  # noqa: E402
from runtime.brokers.instrument_master import fetch_and_parse_listings  # noqa: E402
from runtime.brokers.upstox import UpstoxAdapter  # noqa: E402
from runtime.tape import read_payload, read_tape_index  # noqa: E402

NEWS = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/news/upstox-news-api"


def captured_stories(days: int = 3):
    """Every captured news item, newest days first."""
    for index_path in sorted(NEWS.glob("*.news.index"))[-days:]:
        blob_path = index_path.parent / index_path.name.replace(".index", ".blob")
        for record in read_tape_index(index_path):
            try:
                yield json.loads(read_payload(blob_path, record))
            except Exception:
                continue


def main() -> int:
    resolver = NewsSymbolResolver(now_ns=lambda: 0)
    for listing in fetch_and_parse_listings(UpstoxAdapter()):
        resolver.observe_listing(listing)
    print(f"instruments the resolver knows: {resolver.instruments_known:,}\n")

    stories = list(captured_stories())
    by_symbol = collections.Counter()
    with_a_key = tagged = 0
    for story in stories:
        key = story.get("returned_under_instrument_key")
        source_keys = (str(key),) if key else ()
        if source_keys:
            with_a_key += 1
        # `names_mentioned` is what the reading half would have produced. On this
        # box it is always empty, which is the point being measured.
        tagging = resolver.tag(SimpleNamespace(
            story_key=story.get("story_key", ""), names_mentioned=(),
            source_instrument_keys=source_keys,
        ))
        if tagging.underlying_symbols:
            tagged += 1
            for symbol in tagging.underlying_symbols:
                by_symbol[symbol] += 1

    print(f"captured stories read            {len(stories):,}")
    print(f"carrying the source's own key    {with_a_key:,} ({with_a_key / max(1, len(stories)):.1%})")
    print(f"tagged with a tradable symbol    {tagged:,} ({tagged / max(1, len(stories)):.1%})")
    print(f"  by reading the text            0  (no model is called on this box)")
    print(f"distinct symbols covered         {len(by_symbol)}")
    print(f"  most covered: " + ", ".join(
        f"{symbol} {count}" for symbol, count in by_symbol.most_common(6)
    ))
    print(f"source keys the master does not list: "
          f"{resolver.standing.source_keys_not_in_the_master:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
