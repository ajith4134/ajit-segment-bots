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


def underlyings_this_segment_trades(context) -> tuple[str, ...]:
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
        return read_segment_symbols(segment_id, "segment_underlying_trading_symbols")
    except (SegmentSettingMissing, OSError, ValueError):
        return tuple(
            str(symbol)
            for symbol in context.setting(
                "underlying_price_bridge_index_trading_symbols"
            ).value
        )


def option_contracts_per_underlying(context) -> int:
    """How wide a chain this segment publishes per underlying, same fallback."""
    segment_id = str(context.setting("segment_id").value)
    try:
        return int(
            read_segment_setting(
                segment_id, "segment_option_contracts_per_underlying"
            ).value
        )
    except (SegmentSettingMissing, OSError, ValueError):
        return int(context.number("symbol_universe_option_contracts_per_underlying"))


__all__ = [
    "SegmentSettingMissing",
    "option_contracts_per_underlying",
    "read_segment_setting",
    "read_segment_symbols",
    "segment_settings_path",
    "underlyings_this_segment_trades",
]
