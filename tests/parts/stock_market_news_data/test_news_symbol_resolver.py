"""news-symbol-resolver against the real instrument master and real model output.

RL-063: real data, never invented fixtures. The alias table is every company and
index row of Upstox's own instrument master
(`tests/captured/upstox/2026-09-05-master-companies-and-indices.json`, 2,871
rows, the five fields the resolver reads). The names are exactly what `haiku`
wrote into `names_mentioned` for two real Upstox stories
(`tests/captured/upstox/2026-09-12-two-real-stories-read-by-haiku.json`).

The full master matters, not a slice picked for the names: ambiguity is the
failure an exact rule can have, and it only shows against every spelling the
broker actually lists -- nine NIFTY50 variants, four SENSEX ones.
"""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace

import pytest

from parts.stock_market_news_data.news_symbol_resolver import (
    NewsSymbolResolver,
    describe_resolving,
    normalised,
)

CAPTURED = pathlib.Path(__file__).resolve().parents[3] / "tests/captured/upstox"
MASTER = CAPTURED / "2026-09-05-master-companies-and-indices.json"
ANSWERS = CAPTURED / "2026-09-12-two-real-stories-read-by-haiku.json"


@pytest.fixture(scope="module")
def master_rows() -> list[dict]:
    return json.loads(MASTER.read_text())["rows"]


@pytest.fixture(scope="module")
def names_the_model_wrote() -> dict[str, tuple[str, ...]]:
    answers = json.loads(ANSWERS.read_text())["answers"]
    names = {}
    for answer in answers:
        text = answer["answer_text"].strip().removeprefix("```json").removesuffix("```")
        value = json.loads(text)
        names[value["story_key"]] = tuple(
            name.strip() for name in value["names_mentioned"].split(";") if name.strip()
        )
    return names


@pytest.fixture
def resolver(master_rows) -> NewsSymbolResolver:
    resolver = NewsSymbolResolver(now_ns=lambda: 1_000)
    for row in master_rows:
        resolver.observe_listing(SimpleNamespace(**row))
    return resolver


def an_item(story_key, names):
    return SimpleNamespace(story_key=story_key, names_mentioned=names)


def test_the_master_is_the_shape_this_suite_claims(master_rows, resolver):
    assert len(master_rows) == 2871
    assert resolver.instruments_known == 2871


def test_the_oil_story_resolves_every_name_the_master_lists(resolver, names_the_model_wrote):
    key = next(key for key in names_the_model_wrote if "article-200159" in key)
    tagging = resolver.tag(an_item(key, names_the_model_wrote[key]))

    assert tagging.underlying_symbols == ("RELIANCE", "INDIGO", "BPCL")
    assert tagging.instrument_keys == (
        "NSE_EQ|INE002A01018",
        "NSE_EQ|INE646L01027",
        "NSE_EQ|INE029A01011",
    )
    # The master writes HINDPETRO and HINDUSTAN PETROLEUM CORP. Nothing derivable
    # bridges "HPCL", and saying so is the answer.
    assert tagging.names_not_resolved == ("HPCL",)
    assert tagging.names_an_index is False


def test_the_market_wrap_resolves_names_that_need_the_company_name_not_the_symbol(
    resolver, names_the_model_wrote
):
    key = next(key for key in names_the_model_wrote if "article-200198" in key)
    tagging = resolver.tag(an_item(key, names_the_model_wrote[key]))

    assert tagging.underlying_symbols == (
        "SENSEX",
        "NIFTY",
        "RELIANCE",
        "LT",
        "AXISBANK",
        "ICICIBANK",
        "BAJFINANCE",
        "SBIN",
        "M&M",
    )
    assert tagging.names_not_resolved == ()
    assert tagging.names_an_index is True
    assert tagging.names_anything_tradable


def test_twelve_of_thirteen_real_mentions_resolve(resolver, names_the_model_wrote):
    for key, names in names_the_model_wrote.items():
        resolver.tag(an_item(key, names))
    standing = describe_resolving(resolver)
    assert standing["names_resolved"] == 12
    assert standing["names_not_resolved"] == 1
    assert standing["names_that_were_ambiguous"] == 0
    assert standing["items_naming_an_index"] == 1


def test_a_mention_two_instruments_share_is_refused_rather_than_guessed(
    master_rows, resolver
):
    """Found, not constructed: any normalised form the real master gives twice."""
    forms: dict[str, set[str]] = {}
    for row in master_rows:
        for spelling in (row["trading_symbol"], row["name"]):
            if normalised(spelling or ""):
                forms.setdefault(normalised(spelling), set()).add(row["instrument_key"])
    shared = sorted(form for form, keys in forms.items() if len(keys) > 1)
    assert shared, "the real master has no shared spelling, so ambiguity is untested"

    instrument, how = resolver.resolve(shared[0])
    assert instrument is None
    assert how == "ambiguous"
    tagging = resolver.tag(an_item("k", (shared[0],)))
    assert tagging.names_not_resolved == (shared[0],)
    assert resolver.standing.names_that_were_ambiguous == 1


def test_an_item_naming_nothing_says_so(resolver):
    tagging = resolver.tag(an_item("k", ()))
    assert tagging.underlying_symbols == ()
    assert not tagging.names_anything_tradable
    assert resolver.standing.items_naming_nothing_tradable == 1


def test_a_listing_that_is_not_a_company_or_index_is_not_in_the_table():
    resolver = NewsSymbolResolver(now_ns=lambda: 1_000)
    resolver.observe_listing(
        SimpleNamespace(
            instrument_key="NSE_FO|12345",
            trading_symbol="NIFTY 24550 CE",
            name="NIFTY",
            instrument_type="CE",
        )
    )
    assert resolver.instruments_known == 0
    assert resolver.resolve("NIFTY") == (None, "unmatched")


def an_item_from_the_source(story_key, source_keys, names=()):
    return SimpleNamespace(
        story_key=story_key, names_mentioned=names, source_instrument_keys=source_keys,
    )


def test_a_story_the_source_returned_under_an_instrument_is_tagged_with_it(resolver):
    """The broker's news API is asked one instrument at a time and answers with
    that instrument's stories, so the key it returned a story under is the source
    saying what the story is about -- not a reading of the text.

    Measured on the captured news tape 2026-09-16: 10,568 items, **100%** of them
    carrying such a key across 179 instruments. And 162 of 162 structured items
    had tagged nothing tradable, because nothing on this box calls a model, so
    `names_mentioned` arrived empty and the resolver read only names.
    """
    tagging = resolver.tag(
        an_item_from_the_source("story-1", ("NSE_EQ|INE002A01018",))
    )
    assert tagging.underlying_symbols == ("RELIANCE",)
    assert tagging.instrument_keys == ("NSE_EQ|INE002A01018",)


def test_the_source_and_the_text_are_added_together_without_repeating_an_instrument(
    resolver, names_the_model_wrote,
):
    """One story names several companies; the source names the one it was filed
    under. Both are wanted, and the one they agree on is not two mentions.
    """
    key = next(key for key in names_the_model_wrote if "article-200159" in key)
    tagging = resolver.tag(
        an_item_from_the_source(
            key, ("NSE_EQ|INE002A01018",), names_the_model_wrote[key],
        )
    )
    # RELIANCE comes from the source and is also named in the text; INDIGO and
    # BPCL only from the text.
    assert tagging.underlying_symbols == ("RELIANCE", "INDIGO", "BPCL")
    assert len(set(tagging.instrument_keys)) == len(tagging.instrument_keys)
    assert resolver.standing.resolved_from_the_source == 1


def test_a_source_key_the_master_does_not_list_is_counted_not_guessed(resolver):
    """The two halves of the broker disagreeing about what exists is worth
    knowing, and is not this part's to resolve."""
    tagging = resolver.tag(
        an_item_from_the_source("story-2", ("NSE_EQ|NOT-A-REAL-ISIN",))
    )
    assert tagging.underlying_symbols == ()
    assert resolver.standing.source_keys_not_in_the_master == 1
    assert resolver.standing.items_naming_nothing_tradable == 1


def test_an_item_with_no_source_keys_still_resolves_by_name(resolver, names_the_model_wrote):
    """The old path is unchanged: a source that names no instrument leaves the
    text as the only evidence, which is what every non-broker feed will be."""
    key = next(key for key in names_the_model_wrote if "article-200159" in key)
    tagging = resolver.tag(an_item(key, names_the_model_wrote[key]))
    assert tagging.underlying_symbols == ("RELIANCE", "INDIGO", "BPCL")
    assert resolver.standing.resolved_from_the_source == 0
