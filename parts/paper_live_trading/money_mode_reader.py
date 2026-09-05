"""money-mode-reader: paper or live, for this segment.

The single most consequential setting in the system, and the one whose default
must never be convenient. Everything that spends money asks this part first, and
a wrong answer in one direction is a paper simulation nobody notices, while a
wrong answer in the other is real money moved by a system nobody authorised.

So: **anything other than an explicit, exact `live` is paper.** A missing file,
an unreadable file, a typo, a value of `true`, `yes`, `LIVE ` with a trailing
space -- all paper. There is no reading of an ambiguous setting that justifies
spending real money, and the operator who meant live will notice immediately and
fix it, which is the cheap failure.

Going live is also **not reversible by accident**: the transition from paper to
live is recorded, so an operator can see when a segment started spending real
money without having to remember.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.segment_settings import built_segments
from runtime.part_process import run_part
from runtime.settings_reader import (
    SettingsParseRefused,
    load_settings_document,
    settings_directory,
)

PART_ID = "money-mode-reader"

PART_DECLARATION = PartDeclaration(
    part_id="money-mode-reader",
    consumes=(),
    produces=("money-mode", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PAPER = "paper"
LIVE = "live"

# The exact value that means real money. Compared exactly, with no normalisation
# beyond the setting's own string: accepting "Live", "LIVE" or "live " would mean
# a typo could start spending, and the operator who wrote one of those is one
# keystroke from the correct value anyway.
LIVE_SETTING_VALUE = "live"

MONEY_MODE_SETTING = "money_mode"


@dataclass(frozen=True)
class MoneyMode:
    """Whether this segment spends real money, and how that was decided."""

    segment: str
    mode: str
    is_explicit: bool
    reason: str
    read_at_ns: int
    went_live_at_ns: int | None

    @property
    def is_live(self) -> bool:
        return self.mode == LIVE


@dataclass
class ReaderStanding:
    reads: int = 0
    live_reads: int = 0
    paper_reads: int = 0
    fell_back_to_paper: int = 0
    transitions_to_live: int = 0
    transitions_to_paper: int = 0
    settings_path: str | None = None
    last_failure: str | None = None
    went_live_at_ns: int | None = None


class MoneyModeReader:
    """Reads one segment's money mode, defaulting to paper in every ambiguous case."""

    def __init__(self, segment: str, settings_path=None, now_ns=time.time_ns) -> None:
        self._segment = segment
        self._path = settings_path or (settings_directory() / "segments" / f"{segment}.toml")
        self._now_ns = now_ns
        self._current: MoneyMode | None = None
        self.standing = ReaderStanding(settings_path=str(self._path))

    @property
    def current(self) -> MoneyMode | None:
        return self._current

    def read(self) -> MoneyMode:
        """Read the setting. Never raises: an unreadable setting is paper."""
        self.standing.reads += 1
        try:
            document = load_settings_document(self._path, self._segment)
        except (SettingsParseRefused, OSError) as failure:
            return self._paper(
                f"{self._path} could not be read ({type(failure).__name__}); paper is the only "
                f"safe reading of a setting that is not there"
            )

        if MONEY_MODE_SETTING not in document.entries:
            return self._paper(
                f"{self._path} declares no {MONEY_MODE_SETTING}; real money is never a default"
            )

        value = document.read_value(MONEY_MODE_SETTING)
        if value == LIVE_SETTING_VALUE:
            return self._live(f"{self._path} says {MONEY_MODE_SETTING} = {LIVE_SETTING_VALUE!r}")

        if isinstance(value, str) and value.strip().lower() == LIVE_SETTING_VALUE:
            # Close but not exact. Named specifically, because this is the case
            # an operator will be confused by, and silence would leave them
            # believing the segment is live when it is not.
            return self._paper(
                f"{self._path} says {MONEY_MODE_SETTING} = {value!r}, which is not exactly "
                f"{LIVE_SETTING_VALUE!r}; a typo must not be able to start spending real money"
            )

        return self._paper(f"{self._path} says {MONEY_MODE_SETTING} = {value!r}")

    def _live(self, reason: str) -> MoneyMode:
        was_live = self._current is not None and self._current.is_live
        if not was_live:
            self.standing.transitions_to_live += 1
            self.standing.went_live_at_ns = self._now_ns()
        self.standing.live_reads += 1
        self.standing.last_failure = None
        self._current = MoneyMode(
            segment=self._segment, mode=LIVE, is_explicit=True, reason=reason,
            read_at_ns=self._now_ns(), went_live_at_ns=self.standing.went_live_at_ns,
        )
        return self._current

    def _paper(self, reason: str) -> MoneyMode:
        was_live = self._current is not None and self._current.is_live
        if was_live:
            self.standing.transitions_to_paper += 1
            self.standing.went_live_at_ns = None
        self.standing.paper_reads += 1
        explicit = "says money_mode" in reason and "could not be read" not in reason
        if not explicit:
            self.standing.fell_back_to_paper += 1
            self.standing.last_failure = reason
        self._current = MoneyMode(
            segment=self._segment, mode=PAPER, is_explicit=explicit, reason=reason,
            read_at_ns=self._now_ns(), went_live_at_ns=None,
        )
        return self._current


def describe_money_mode(reader: MoneyModeReader) -> dict:
    current = reader.current
    return {
        "part_id": PART_ID,
        "segment": reader._segment,
        "settings_path": reader.standing.settings_path,
        "mode": current.mode if current else None,
        "is_explicit": current.is_explicit if current else None,
        "reads": reader.standing.reads,
        "live_reads": reader.standing.live_reads,
        "paper_reads": reader.standing.paper_reads,
        "fell_back_to_paper": reader.standing.fell_back_to_paper,
        "transitions_to_live": reader.standing.transitions_to_live,
        "transitions_to_paper": reader.standing.transitions_to_paper,
        "went_live_at_ns": reader.standing.went_live_at_ns,
        "last_failure": reader.standing.last_failure,
    }


class SegmentMoneyModes:
    """One money-mode reader per segment this spine trades.

    Whether money is real is a fact about a segment's own settings file, and three
    segment bots run on one spine since 2026-09-05. `MoneyMode` already named the
    segment it belongs to; what was missing was a producer that read more than one
    file, so a spine trading three segments published one mode and every part
    downstream applied it to all three -- including, in the worst direction, a
    segment the operator had left on paper.
    """

    def __init__(self, segments: tuple[str, ...], now_ns=time.time_ns) -> None:
        if not segments:
            raise ValueError(
                "a money-mode reader with no segment publishes nothing, and every part "
                "that sends an order treats an absent mode as a reason to send none"
            )
        self.readers = tuple(
            MoneyModeReader(segment=segment, now_ns=now_ns) for segment in segments
        )

    def read(self) -> tuple[MoneyMode, ...]:
        return tuple(reader.read() for reader in self.readers)


def describe_segment_money_modes(readers: SegmentMoneyModes) -> dict:
    return {
        "part_id": PART_ID,
        "segments": [reader._segment for reader in readers.readers],
        "live_segments": [
            reader._segment for reader in readers.readers
            if (mode := reader.current) is not None and mode.is_live
        ],
        "by_segment": {
            reader._segment: describe_money_mode(reader) for reader in readers.readers
        },
    }


def run_money_mode_reader(
    reader: SegmentMoneyModes, control_socket, publish_mode,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=lambda: publish_mode(reader.read()),
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_segment_money_modes(reader),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The part that decides whether money is real. It consumes nothing and reads one
    setting, and the reader refuses anything that is not exactly 'paper' or 'live':
    real money is never a default and never a typo. Everything downstream treats an
    unreadable mode as a reason to send no order at all.
    """
    publish_mode = context.bus.publisher_for("money-mode")

    return run_money_mode_reader(
        reader=SegmentMoneyModes(segments=built_segments(context)),
        control_socket=context.control_socket,
        publish_mode=lambda modes: publish_mode(list(modes)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
