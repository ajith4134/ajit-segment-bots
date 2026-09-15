"""The settings that belong to one segment rather than to the whole machine.

`~/.config/ajit-segment-bots/settings/runtime.toml` is machine scope: one value
for everything running here. `segments/<segment_id>.toml` is segment scope, and
`capital-allotment-reader` has read it that way since 2026-09-02 -- money mode,
allocated balance, the per-trade bounds.

**What a segment trades belongs in the same place, and did not.** The tracked
underlyings and the width of each chain lived in machine scope while
`segment_id` decided which capital file was read, so pointing this spine at
`stock-options` would have moved the money and left the universe on NIFTY,
BANKNIFTY and SENSEX. Nothing would have failed: the stock-options bot would
have run, taken its capital from the right file, and traded index options --
which is the shape of defect this project keeps finding, a wrong answer that
never reports itself.

Read once, when a part starts. The universe changes when the operator changes
it and the governor restarts parts far more often than that; a part that must
follow an edit within the same run reads its own file per tick the way
`capital-allotment-reader` does, and says so.
"""

from __future__ import annotations

import pathlib

from runtime.settings_reader import (
    SettingEntry, load_settings_document, settings_directory,
)


class SegmentSettingMissing(KeyError):
    """A segment asked for a setting its own file does not carry.

    Its own message names the file, because the fix is always an edit to that
    file and never a change here: a segment whose universe is not stated has no
    universe, and inheriting another segment's would be the silent-wrong-answer
    this module exists to prevent.
    """


def segment_settings_path(segment_id: str, root: pathlib.Path | None = None) -> pathlib.Path:
    """Where one segment's own settings live."""
    base = root if root is not None else settings_directory()
    return base / "segments" / f"{segment_id}.toml"


def read_segment_setting(
    segment_id: str, name: str, root: pathlib.Path | None = None,
) -> SettingEntry:
    """One setting from this segment's own file, or a refusal naming the file."""
    path = segment_settings_path(segment_id, root)
    document = load_settings_document(path, scope=f"segment:{segment_id}")
    try:
        return document.entries[name]
    except KeyError:
        raise SegmentSettingMissing(
            f"the {segment_id} segment has no setting '{name}' in {path}. Every segment "
            f"states its own universe: inheriting another segment's would trade the wrong "
            f"instruments while every part reported healthy."
        ) from None


def read_segment_symbols(
    segment_id: str, name: str, root: pathlib.Path | None = None,
) -> tuple[str, ...]:
    """A segment setting that is a list of trading symbols.

    Refuses an empty list rather than returning one: a part handed no symbols
    scans nothing, and every one of its skip counters reads zero -- which is
    indistinguishable from a part that is working and finding nothing. That
    exact reading cost a day on 2026-09-04, when `universal-symbol-sweeper` had
    run 3,114 sweeps over an empty universe.
    """
    entry = read_segment_setting(segment_id, name, root)
    value = entry.value
    if not isinstance(value, (list, tuple)):
        raise SegmentSettingMissing(
            f"the {segment_id} segment's '{name}' is {type(value).__name__}, not a list of "
            f"trading symbols, in {segment_settings_path(segment_id, root)}"
        )
    symbols = tuple(str(symbol) for symbol in value)
    if not symbols:
        raise SegmentSettingMissing(
            f"the {segment_id} segment's '{name}' is empty in "
            f"{segment_settings_path(segment_id, root)}. A segment with no underlyings "
            f"scans nothing while reporting healthy."
        )
    return symbols


def _root_of(context, root: pathlib.Path | None):
    """The settings directory a context's own files came from.

    An explicit root wins, then the context's own, then the operator's. A helper
    reading the operator's directory while every other number came from a copy is
    the defect this exists to close (2026-09-05).
    """
    if root is not None:
        return root
    return getattr(context, "settings_root", None)


def underlyings_this_segment_trades(context, root: pathlib.Path | None = None) -> tuple[str, ...]:
    """The underlyings whose option chains this spine's segment trades.

    Read from the segment's own file, and from machine scope only when the
    segment does not state them -- which is what every segment file looked like
    before 2026-09-05. The fallback is deliberate and narrow: it keeps a segment
    that has not been given a universe running exactly as it ran yesterday,
    rather than making a settings edit a condition of the spine starting on the
    Monday this had to be ready for.

    It is not a default to keep. A segment inheriting the machine's universe is
    the wrong-instrument failure this module exists to prevent, so the fallback
    is reported on the part's own standing wherever it is used.
    """
    segment_id = str(context.setting("segment_id").value)
    try:
        return read_segment_symbols(
            segment_id, "segment_underlying_trading_symbols", _root_of(context, root)
        )
    except (SegmentSettingMissing, OSError, ValueError):
        return tuple(
            str(symbol)
            for symbol in context.setting(
                "underlying_price_bridge_index_trading_symbols"
            ).value
        )


def option_contracts_per_underlying(context, root: pathlib.Path | None = None) -> int:
    """How wide a chain this segment publishes per underlying, same fallback."""
    segment_id = str(context.setting("segment_id").value)
    try:
        return int(
            read_segment_setting(
                segment_id,
                "segment_option_contracts_per_underlying",
                _root_of(context, root),
            ).value
        )
    except (SegmentSettingMissing, OSError, ValueError):
        return int(context.number("symbol_universe_option_contracts_per_underlying"))



# Machine scope names which segments this spine actually trades. `segment_id` is
# the one whose settings stand in wherever a value has not been keyed by segment
# yet; `built_segments` is the list, and a spine running one segment states a
# one-item list rather than a different shape (2026-09-05,
# docs/proposals/three-segments-on-one-spine.md).
BUILT_SEGMENTS_SETTING = "built_segments"
SEGMENT_INSTRUMENT_TYPES_SETTING = "segment_instrument_types"


class SegmentsOverlap(ValueError):
    """Two built segments both claim the same instrument.

    Never resolved by picking one. Which segment an instrument belongs to
    decides whose money buys it, whose exposure it counts against and whose
    money mode governs it, so a tie is a settings mistake with a wrong answer
    behind it rather than a choice this code may make.
    """


def built_segments(context) -> tuple[str, ...]:
    """Every segment this spine trades, in the order the operator listed them.

    Falls back to the single `segment_id` when machine scope does not name the
    list, which is what runtime.toml looked like before 2026-09-05. The fallback
    keeps a one-segment spine starting without a settings edit; it is not a
    default to keep, and a part relying on it says so on its own standing.
    """
    try:
        listed = context.setting(BUILT_SEGMENTS_SETTING).value
    except KeyError:
        # SettingMissing is a KeyError. Machine scope not naming the list is the
        # pre-2026-09-05 shape, and is the only absence this narrows to: a
        # malformed value is not caught here, because a list the operator wrote
        # and this code could not read must refuse rather than quietly become one
        # segment.
        listed = None
    if isinstance(listed, (list, tuple)) and listed:
        seen: dict[str, None] = {}
        for segment in listed:
            seen.setdefault(str(segment), None)
        return tuple(seen)
    return (str(context.setting("segment_id").value),)


def underlyings_every_built_segment_trades(
    context, root: pathlib.Path | None = None,
) -> tuple[str, ...]:
    """The union of what every built segment trades, each underlying once.

    The feed subscribes once per underlying however many segments want it:
    RELIANCE is a stock-options underlying and a cash-equity-intraday one, and
    subscribing twice would spend the broker's per-connection instrument budget
    on a duplicate rather than on a symbol nothing is watching.
    """
    union: dict[str, None] = {}
    for segment in built_segments(context):
        try:
            symbols = read_segment_symbols(
                segment, "segment_underlying_trading_symbols", _root_of(context, root)
            )
        except (SegmentSettingMissing, OSError, ValueError):
            continue
        for symbol in symbols:
            union.setdefault(symbol, None)
    if union:
        return tuple(union)
    return underlyings_this_segment_trades(context, root)


def instrument_types_this_segment_trades(
    segment_id: str, root: pathlib.Path | None = None,
) -> tuple[str, ...]:
    """Which instrument types belong to one segment, from its own file.

    Two segments can share an underlying and never share an instrument: RELIANCE
    is stock-options as an option and cash-equity-intraday as spot. The pair is
    what identifies a segment, so neither half may be inferred from the other.
    """
    entry = read_segment_setting(segment_id, SEGMENT_INSTRUMENT_TYPES_SETTING, root)
    value = entry.value
    if not isinstance(value, (list, tuple)) or not value:
        raise SegmentSettingMissing(
            f"the {segment_id} segment's '{SEGMENT_INSTRUMENT_TYPES_SETTING}' is not a "
            f"non-empty list in {segment_settings_path(segment_id, root)}. A segment that "
            f"states no instrument type claims nothing, and every instrument would read as "
            f"belonging to a segment that is not built."
        )
    return tuple(str(instrument_type) for instrument_type in value)


def segment_that_trades(
    instrument_type: str,
    underlying: str,
    context,
    root: pathlib.Path | None = None,
    derived_membership: Mapping[str, object] | None = None,
) -> str | None:
    """Which built segment an instrument belongs to, or None if none does.

    None is the honest answer for an instrument in a segment this spine does not
    trade -- the caller reports it as such rather than choosing the nearest
    segment, which is the wrong-instrument failure this module exists around.

    A segment whose `segment_universe_selection` is not `"stated"` --
    cash-equity-intraday's `"every-nse-share-without-a-derivative"`,
    2026-09-05 -- does not claim from `segment_underlying_trading_symbols` at
    all: that list is a 14-name legacy fallback, disconnected from the
    2,444-share derived universe `broker-symbol-universe-bridge` actually
    publishes. Its claim is asked of `derived_membership` instead --
    `{segment_id: frozenset(symbols)}`, the day's `cash-equity-shortlist` for a
    replay, or the live one for a caller with bus access. `derived_membership`
    is None for a caller with none, and then a derived-selection segment claims
    nothing -- the same honest "not built" answer an unresolvable segment
    always got, never a silent fall-back to the stale static list.
    """
    claimants = []
    root = _root_of(context, root)
    for segment in built_segments(context):
        try:
            types = instrument_types_this_segment_trades(segment, root)
        except (SegmentSettingMissing, OSError, ValueError):
            continue
        try:
            selection = str(
                read_segment_setting(segment, UNIVERSE_SELECTION_SETTING, root).value
            )
        except (SegmentSettingMissing, OSError, ValueError):
            selection = UNIVERSE_IS_STATED
        if selection != UNIVERSE_IS_STATED:
            if (
                instrument_type in types and derived_membership is not None
                and underlying in derived_membership.get(segment, frozenset())
            ):
                claimants.append(segment)
            continue
        try:
            symbols = read_segment_symbols(
                segment, "segment_underlying_trading_symbols", root
            )
        except (SegmentSettingMissing, OSError, ValueError):
            continue
        if instrument_type in types and underlying in symbols:
            claimants.append(segment)
    if not claimants:
        return None
    if len(claimants) > 1:
        raise SegmentsOverlap(
            f"{instrument_type} on {underlying} is claimed by {', '.join(claimants)}. "
            f"Both segments' files name that instrument type and that underlying, so whose "
            f"capital buys it is undecidable -- narrow one file's "
            f"'{SEGMENT_INSTRUMENT_TYPES_SETTING}' or its underlyings."
        )
    return claimants[0]


def option_chain_width_by_underlying(
    context, root: pathlib.Path | None = None,
) -> dict[str, int]:
    """How many option contracts to publish per underlying, per built segment.

    Three segments want three different chains: 50 contracts on an index, 20 on a
    single stock, and none at all on a cash-equity underlying, which is tracked
    for its own price and whose options no segment here trades. One width for all
    of them either truncates the index chain or fills the broker's instrument
    budget with stock strikes nothing acts on.

    Zero is a real answer, not a missing one. An underlying only ever claimed by a
    segment that does not trade options has no chain on this spine, and the map
    says so rather than leaving the width to a default.
    """
    from runtime.trading_types import OPTION

    widths: dict[str, int] = {}
    root = _root_of(context, root)
    for segment in built_segments(context):
        try:
            symbols = read_segment_symbols(
                segment, "segment_underlying_trading_symbols", root
            )
            types = instrument_types_this_segment_trades(segment, root)
        except (SegmentSettingMissing, OSError, ValueError):
            continue
        trades_options = OPTION in types
        width = 0
        if trades_options:
            width = int(
                read_segment_setting(
                    segment, "segment_option_contracts_per_underlying", root
                ).value
            )
        for symbol in symbols:
            # The widest chain any segment wants of that underlying. Two segments
            # claiming one underlying's options is refused elsewhere; this is the
            # ordinary case of an underlying tracked by an options segment and a
            # cash one, where the options segment's width is the one that matters.
            widths[symbol] = max(widths.get(symbol, 0), width)
    return widths


def segments_trading_underlying(
    underlying: str, context, root: pathlib.Path | None = None,
) -> tuple[str, ...]:
    """Every built segment that trades this underlying, in the operator's order.

    More than one is the ordinary case, not an error: RELIANCE is a stock-options
    underlying and a cash-equity-intraday one, and a part reasoning about an
    underlying before an instrument has been chosen -- `leverage-selector` reads
    `trade-intent`, which names an asset and not a contract -- has to answer for
    each of them. `segment_that_trades` is the narrower question, asked once the
    instrument kind is known.
    """
    root = _root_of(context, root)
    return tuple(
        segment
        for segment in built_segments(context)
        if underlying in _underlyings_or_nothing(segment, root)
    )


def _underlyings_or_nothing(segment: str, root: pathlib.Path | None) -> tuple[str, ...]:
    try:
        return read_segment_symbols(
            segment, "segment_underlying_trading_symbols", root
        )
    except (SegmentSettingMissing, OSError, ValueError):
        return ()


# How a segment's universe is decided. Two values, and a segment that names
# neither is read as stating its own symbols -- which is what every segment file
# looked like before 2026-09-05.
UNIVERSE_SELECTION_SETTING = "segment_universe_selection"
UNIVERSE_IS_STATED = "stated"
UNIVERSE_IS_EVERY_SHARE_WITHOUT_A_DERIVATIVE = "every-nse-share-without-a-derivative"
# Every ordinary NSE share that some derivative IS written on -- the exact
# complement of the rule above, and the stock-options segment's universe from
# 2026-09-12, when the operator asked for "option index and option stocks full
# universe". Stated as a rule for the same reason that one is: 210 trading
# symbols typed into a settings file is fiction the day NSE revises the F&O
# list, and nobody can audit it. Measured on the real master that day: exactly
# 210 shares carry option contracts, against the 14 the segment had stated.
UNIVERSE_IS_EVERY_STOCK_WITH_AN_OPTION = "every-nse-stock-with-an-option"
# Every NSE or BSE index that some option is written on -- the index-options
# segment's universe from 2026-09-12, on the operator's standing instruction
# that nothing is typed by hand and everything is detected every time. Measured
# on the real master that day: of 216 index listings, exactly 10 carry CE/PE
# contracts. MCX excludes itself rather than being blacklisted: MCXBULLDEX
# carries options and sits in MCX_INDEX, which docs/goal.md #6 defers.
UNIVERSE_IS_EVERY_INDEX_WITH_AN_OPTION = "every-nse-index-with-an-option"

# Every selection value that means "derive this segment's underlyings from the
# broker's own master". Named as a set so a new rule is added in one place, and
# so `chain_width_for_derived_underlyings` cannot fall behind the list it is
# supposed to cover -- which is exactly how a width would silently become zero
# for a segment whose rule nobody remembered to add.
DERIVED_OPTION_UNIVERSE_SELECTIONS = (
    UNIVERSE_IS_EVERY_INDEX_WITH_AN_OPTION,
    UNIVERSE_IS_EVERY_STOCK_WITH_AN_OPTION,
)


# The exchange segments an option's underlying key names, in the broker's own
# vocabulary. MCX is absent on purpose and excludes itself (docs/goal.md #6).
OPTION_UNDERLYING_INDEX_SEGMENTS = ("NSE_INDEX", "BSE_INDEX")
OPTION_UNDERLYING_STOCK_SEGMENTS = ("NSE_EQ",)


class OptionUnderlyingsByRule:
    """Which underlyings carry an option, split by the derived rule that claims them.

    Read off every option listing the broker states: its `underlying_symbol` names
    the underlying, and its `underlying_key`'s exchange segment says what kind of
    thing that is -- an index or a share. Nothing is named by hand, so an index NSE
    starts writing options on is claimed the day its first contract is listed.

    Exists because two segments derive their universes from 2026-09-12, and the
    selector resolved both against one set: every stock option in the retired cash
    segment's shortlist was bought with index-options' capital (2026-09-15).
    """

    def __init__(self) -> None:
        self.indices: set[str] = set()
        self.stocks: set[str] = set()

    def observe_listing(self, listing) -> None:
        underlying_key = getattr(listing, "underlying_key", None)
        underlying_symbol = getattr(listing, "underlying_symbol", None)
        if not underlying_key or not underlying_symbol:
            return
        exchange_segment = str(underlying_key).split("|", 1)[0]
        if exchange_segment in OPTION_UNDERLYING_INDEX_SEGMENTS:
            self.indices.add(underlying_symbol)
        elif exchange_segment in OPTION_UNDERLYING_STOCK_SEGMENTS:
            self.stocks.add(underlying_symbol)

    def underlyings_for(self, selection: str) -> frozenset:
        if selection == UNIVERSE_IS_EVERY_INDEX_WITH_AN_OPTION:
            return frozenset(self.indices)
        if selection == UNIVERSE_IS_EVERY_STOCK_WITH_AN_OPTION:
            return frozenset(self.stocks)
        return frozenset()


def _segments_whose_selection_is_in(
    wanted, context, root: pathlib.Path | None = None,
) -> tuple[str, ...]:
    """Built segments whose universe rule is one of `wanted`, operator's order."""
    root = _root_of(context, root)
    taking = []
    for segment in built_segments(context):
        try:
            selection = str(
                read_segment_setting(segment, UNIVERSE_SELECTION_SETTING, root).value
            )
        except (SegmentSettingMissing, OSError, ValueError):
            continue
        if selection in wanted:
            taking.append(segment)
    return tuple(taking)


def segments_taking_every_index_with_an_option(
    context, root: pathlib.Path | None = None,
) -> tuple[str, ...]:
    """Which built segments want every index an option is written on."""
    return _segments_whose_selection_is_in(
        (UNIVERSE_IS_EVERY_INDEX_WITH_AN_OPTION,), context, root
    )


def segments_taking_every_stock_with_an_option(
    context, root: pathlib.Path | None = None,
) -> tuple[str, ...]:
    """Which built segments want every F&O stock underlying, in the operator's order.

    Asked of the spine rather than of one segment for the same reason
    `any_segment_takes_shares_without_a_derivative` is: the universe bridge
    publishes one universe for all of them, the feed subscribes each instrument
    once however many segments want it, and which segment may trade a contract
    is decided later, by (instrument kind, underlying), where it belongs.
    """
    return _segments_whose_selection_is_in(
        (UNIVERSE_IS_EVERY_STOCK_WITH_AN_OPTION,), context, root
    )


def chain_width_for_derived_underlyings(
    context, root: pathlib.Path | None = None,
) -> int:
    """The chain width to give an underlying no settings file names by hand.

    A derived universe has no per-symbol width map, because nobody typed the
    symbols. The width is the widest any segment taking a derived universe
    states for itself -- its own `segment_option_contracts_per_underlying`,
    which is a number the operator set with the feed's instrument budget in
    view. Zero when no segment takes one, which is what a spine trading only
    stated universes looks like.
    """
    root = _root_of(context, root)
    widest = 0
    for segment in _segments_whose_selection_is_in(
        DERIVED_OPTION_UNIVERSE_SELECTIONS, context, root
    ):
        try:
            widest = max(widest, int(read_segment_setting(
                segment, "segment_option_contracts_per_underlying", root
            ).value))
        except (SegmentSettingMissing, OSError, ValueError):
            continue
    return widest


def any_segment_takes_shares_without_a_derivative(
    context, root: pathlib.Path | None = None,
) -> bool:
    """Whether some segment on this spine wants the derived cash-equity universe.

    Asked of the spine rather than of one segment because the universe bridge
    publishes one universe for all of them: the feed subscribes each instrument
    once however many segments want it, and which segment may trade a share is
    decided later, by the pair (instrument kind, underlying), where it belongs.
    """
    root = _root_of(context, root)
    for segment in built_segments(context):
        try:
            selection = str(
                read_segment_setting(segment, UNIVERSE_SELECTION_SETTING, root).value
            )
        except (SegmentSettingMissing, OSError, ValueError):
            continue
        if selection == UNIVERSE_IS_EVERY_SHARE_WITHOUT_A_DERIVATIVE:
            return True
    return False

__all__ = [
    "BUILT_SEGMENTS_SETTING",
    "SEGMENT_INSTRUMENT_TYPES_SETTING",
    "SegmentSettingMissing",
    "SegmentsOverlap",
    "UNIVERSE_IS_EVERY_SHARE_WITHOUT_A_DERIVATIVE",
    "OptionUnderlyingsByRule",
    "UNIVERSE_IS_EVERY_INDEX_WITH_AN_OPTION",
    "UNIVERSE_IS_EVERY_STOCK_WITH_AN_OPTION",
    "DERIVED_OPTION_UNIVERSE_SELECTIONS",
    "segments_taking_every_index_with_an_option",
    "chain_width_for_derived_underlyings",
    "segments_taking_every_stock_with_an_option",
    "UNIVERSE_IS_STATED",
    "UNIVERSE_SELECTION_SETTING",
    "any_segment_takes_shares_without_a_derivative",
    "built_segments",
    "instrument_types_this_segment_trades",
    "option_chain_width_by_underlying",
    "segment_that_trades",
    "segments_trading_underlying",
    "underlyings_every_built_segment_trades",
    "option_contracts_per_underlying",
    "read_segment_setting",
    "read_segment_symbols",
    "segment_settings_path",
    "underlyings_this_segment_trades",
]
