"""news-symbol-resolver: resolve the instruments a news item names.

The blueprint says one thing about this part and it is the whole design: the
alias table is **built off `broker-instrument-listing`, never hardcoded**. A
handwritten map of "Larsen & Toubro" to `LT` is a file that is wrong the week NSE
lists something new and that nobody notices is wrong, because a name that fails
to resolve looks exactly like an item about nothing.

So the table is the broker's own master, re-derived from every restatement. Two
fields carry it, and both are needed:

    trading_symbol   RELIANCE          LT                    HINDPETRO
    name             RELIANCE          LARSEN & TOUBRO LTD.  HINDUSTAN PETROLEUM
                     INDUSTRIES LTD                          CORP

`name` was not on `InstrumentListing` until 2026-09-12 — the adapter read it from
Upstox's master and dropped it — and without it this part could not exist: no
amount of string work turns "Larsen & Toubro" into `LT`, and that is the second
most-mentioned name in the real capture.

## One rule: the normalised forms are equal, or the name does not resolve

A mention is normalised — upper case, punctuation to spaces, digits split from
letters, the company-form words (`LTD`, `CORP`, `THE`, `AND`) dropped — and must
match a normalised symbol or a normalised name **exactly**. That is the whole
rule, and it does the work: measured on real `haiku` output over two real stories
against the real 2,871-instrument master, 12 of 13 mentions resolved, including
every one that needed the name rather than the symbol.

    Larsen & Toubro       -> LT            State Bank of India -> SBIN
    Mahindra & Mahindra   -> M&M           Bajaj Finance       -> BAJFINANCE
    Reliance Industries   -> RELIANCE      Axis Bank           -> AXISBANK

**A looser rule was written first and measured worse.** "Every word of the
mention appears in the name" was meant to catch a shortening, and on real data it
caught nothing at all — the company-form words being dropped already handles
`HINDUSTAN PETROLEUM CORP` — while making `NIFTY50` match nine index variants
(`NIFTY50 VALUE 20`, `NIFTY50 SHARIAH`, ...) and so resolve to none of them. One
shared word is one shared word whether the rule says "prefix" or "subset". It was
deleted rather than tuned.

The one that does not resolve is the honest answer: **`HPCL`** appears nowhere
in the master, which writes `HINDPETRO` and `HINDUSTAN PETROLEUM CORP`. Nothing
derivable can bridge it, and the alias that would is the hardcoded table the
blueprint refuses. It lands in `names_not_resolved`. `SENSEX` and `NIFTY50` both
resolve, the second because splitting digits from letters is normalisation rather
than an alias.

`names_not_resolved` is the number that says whether this part is working: an
empty tagging with no explanation is indistinguishable from an item about nothing.

**Ambiguity is refused, not broken.** A mention matching more than one instrument
resolves to none of them and is counted: guessing between two companies in a
trading system is worse than saying the name was unclear.

## What it does not do

It does not read the item. The names come from `news-text-structurer`'s
`names_mentioned`, as the article wrote them; this part only decides which
instrument each one is. And it does not tag segments — an index and a share are
different markets, so `names_an_index` travels on the tagging and
`news-segment-classifier` routes on it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from runtime.news_types import NewsSymbolTagging
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "news-symbol-resolver"

PART_DECLARATION = PartDeclaration(
    part_id="news-symbol-resolver",
    consumes=("broker-instrument-listing", "structured-news-item"),
    produces=("news-symbol-tagging", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

# What news can be about, in the broker's own instrument_type words. The same
# two `broker-news-reader` asks about: there is no story about "NIFTY 24550 CE",
# only about NIFTY.
INSTRUMENT_TYPES_NEWS_CAN_NAME = ("EQ", "INDEX")
INDEX = "INDEX"

# Words that say what kind of company something is, not which company. Dropped
# from both sides so "RELIANCE INDUSTRIES" reaches "RELIANCE INDUSTRIES LTD".
# Deliberately only company-form words: dropping anything that carries identity
# is how two different companies start matching each other.
COMPANY_FORM_WORDS = frozenset(
    "LTD LIMITED CORP CORPORATION CO COMPANY INC PLC THE AND".split()
)

NOT_A_LETTER_OR_DIGIT = re.compile(r"[^A-Z0-9]+")
# A digit run against a letter run, so `NIFTY50` and `Nifty 50` reach the same
# form. Measured on real model output 2026-09-12: without this, `NIFTY50` --
# which is how an article writes the index -- matched none of the master's
# spellings of it, because the master writes `Nifty 50`.
LETTER_DIGIT_BOUNDARY = re.compile(r"(?<=[A-Z])(?=\d)|(?<=\d)(?=[A-Z])")


def normalised(name: str) -> str:
    """One name in the form both sides of the match are put into.

    Upper case, punctuation to spaces, digits split from letters, company-form
    words dropped, single spaces. `Larsen & Toubro` and `LARSEN & TOUBRO LTD.`
    both become `LARSEN TOUBRO`; `NIFTY50` and `Nifty 50` both become
    `NIFTY 50`.
    """
    upper = NOT_A_LETTER_OR_DIGIT.sub(" ", (name or "").upper())
    spaced = LETTER_DIGIT_BOUNDARY.sub(" ", upper)
    words = [word for word in spaced.split() if word and word not in COMPANY_FORM_WORDS]
    return " ".join(words)


@dataclass
class ResolverStanding:
    listings_read: int = 0
    instruments_known: int = 0
    items_read: int = 0
    taggings_published: int = 0
    names_resolved: int = 0
    names_not_resolved: int = 0
    # A mention that matched more than one instrument. Refused rather than
    # guessed: picking one of two companies is worse than saying it was unclear.
    names_that_were_ambiguous: int = 0
    items_naming_an_index: int = 0
    items_naming_nothing_tradable: int = 0
    resolved_exactly: int = 0
    # Instruments taken from the source's own statement of what a story is
    # about, rather than from reading the text, and keys the instrument master
    # has no listing for.
    resolved_from_the_source: int = 0
    source_keys_not_in_the_master: int = 0


@dataclass
class Instrument:
    """One thing a news item could be about, from the broker's own master."""

    instrument_key: str
    trading_symbol: str
    name: str
    instrument_type: str

    @property
    def is_an_index(self) -> bool:
        return self.instrument_type == INDEX


class NewsSymbolResolver:
    """Which instruments a news item names, against the broker's own listings."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._by_instrument_key: dict[str, Instrument] = {}
        # normalised form -> the instrument keys carrying it. A set, because two
        # instruments can normalise to one form and that is ambiguity to refuse
        # rather than a collision to resolve arbitrarily.
        self._keys_by_form: dict[str, set[str]] = {}
        self.standing = ResolverStanding()

    def observe_listing(self, listing) -> None:
        """One instrument listing. The alias table is these, and only these."""
        self.standing.listings_read += 1
        if getattr(listing, "instrument_type", None) not in INSTRUMENT_TYPES_NEWS_CAN_NAME:
            return
        key = str(getattr(listing, "instrument_key", "") or "")
        if not key or key in self._by_instrument_key:
            return
        instrument = Instrument(
            instrument_key=key,
            trading_symbol=str(getattr(listing, "trading_symbol", "") or ""),
            name=str(getattr(listing, "name", "") or ""),
            instrument_type=str(getattr(listing, "instrument_type", "") or ""),
        )
        self._by_instrument_key[key] = instrument
        self.standing.instruments_known = len(self._by_instrument_key)
        for spelling in (instrument.trading_symbol, instrument.name):
            form = normalised(spelling)
            if not form:
                continue
            self._keys_by_form.setdefault(form, set()).add(key)

    def resolve(self, mention: str) -> tuple[Instrument | None, str]:
        """The instrument a mention names, and how it was matched."""
        form = normalised(mention)
        if not form:
            return None, "empty"

        keys = self._keys_by_form.get(form)
        if not keys:
            return None, "unmatched"
        if len(keys) > 1:
            return None, "ambiguous"
        return self._by_instrument_key[next(iter(keys))], "exactly"

    def tag(self, item) -> NewsSymbolTagging:
        """Every instrument one structured item is about.

        Two sources, and they are not the same kind of statement. The source's
        own `source_instrument_keys` is the broker answering "stories about this
        instrument" -- it is what the story was returned under, not a reading of
        the text. `names_mentioned` is what a reader found in the words, which is
        richer (one story names several companies) and less certain.

        The source's keys are taken first and unconditionally. Before
        2026-09-16 only the names were read, and on this box nothing calls a
        model, so `names_mentioned` arrives empty and 162 of 162 stories tagged
        nothing tradable -- while every one of 10,568 captured items carried a
        key from the broker. A resolver that reads only the uncertain half
        answers nothing whenever the uncertain half is missing.
        """
        self.standing.items_read += 1
        symbols: list[str] = []
        keys: list[str] = []
        unresolved: list[str] = []
        names_an_index = False

        for stated_key in getattr(item, "source_instrument_keys", ()) or ():
            instrument = self._by_instrument_key.get(str(stated_key))
            if instrument is None:
                # A key the master has no listing for. Counted rather than
                # dropped: it means the two halves of the broker disagree about
                # what exists, which is worth knowing and is not this part's to
                # resolve.
                self.standing.source_keys_not_in_the_master += 1
                continue
            self.standing.resolved_from_the_source += 1
            if instrument.trading_symbol not in symbols:
                symbols.append(instrument.trading_symbol)
                keys.append(instrument.instrument_key)
            names_an_index = names_an_index or instrument.is_an_index

        for mention in getattr(item, "names_mentioned", ()) or ():
            instrument, how = self.resolve(str(mention))
            if instrument is None:
                unresolved.append(str(mention))
                self.standing.names_not_resolved += 1
                if how == "ambiguous":
                    self.standing.names_that_were_ambiguous += 1
                continue
            self.standing.names_resolved += 1
            self.standing.resolved_exactly += 1
            if instrument.trading_symbol not in symbols:
                symbols.append(instrument.trading_symbol)
                keys.append(instrument.instrument_key)
            names_an_index = names_an_index or instrument.is_an_index

        if names_an_index:
            self.standing.items_naming_an_index += 1
        if not symbols:
            self.standing.items_naming_nothing_tradable += 1
        self.standing.taggings_published += 1
        return NewsSymbolTagging(
            story_key=str(getattr(item, "story_key", "") or ""),
            underlying_symbols=tuple(symbols),
            instrument_keys=tuple(keys),
            names_not_resolved=tuple(unresolved),
            names_an_index=names_an_index,
            resolved_at_ns=self._now_ns(),
        )

    @property
    def instruments_known(self) -> int:
        return len(self._by_instrument_key)


def describe_resolving(resolver: NewsSymbolResolver) -> dict:
    standing = resolver.standing
    return {
        "part_id": PART_ID,
        "listings_read": standing.listings_read,
        "instruments_known": resolver.instruments_known,
        "items_read": standing.items_read,
        "taggings_published": standing.taggings_published,
        "names_resolved": standing.names_resolved,
        "names_not_resolved": standing.names_not_resolved,
        "names_that_were_ambiguous": standing.names_that_were_ambiguous,
        "resolved_exactly": standing.resolved_exactly,
        "items_naming_an_index": standing.items_naming_an_index,
        "items_naming_nothing_tradable": standing.items_naming_nothing_tradable,
        "resolved_from_the_source": standing.resolved_from_the_source,
        "source_keys_not_in_the_master": standing.source_keys_not_in_the_master,
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    listings = Batch(read=context.bus.reader("broker-instrument-listing"))
    items = Batch(read=context.bus.reader("structured-news-item"))
    publish_taggings = context.bus.publisher_for("news-symbol-tagging")
    resolver = NewsSymbolResolver()

    def tick() -> None:
        for listing in listings.payloads():
            resolver.observe_listing(listing)
        taggings = [resolver.tag(item) for item in items.payloads()]
        if taggings:
            publish_taggings(tuple(taggings))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_resolving(resolver),
    )


__all__ = [
    "COMPANY_FORM_WORDS",
    "INSTRUMENT_TYPES_NEWS_CAN_NAME",
    "Instrument",
    "NewsSymbolResolver",
    "PART_DECLARATION",
    "PART_ID",
    "ResolverStanding",
    "describe_resolving",
    "normalised",
    "start_part",
]
