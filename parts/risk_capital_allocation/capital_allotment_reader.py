"""capital-allotment-reader: this segment's slice of the settings file (RL-051).

Everything downstream that decides how much money to use starts here, which makes
this the one part where a wrong number is not a bad trade but a wrong system.

So it refuses rather than defaults. There is no sensible fallback for "how much
money may this segment use": zero would silently stop trading in a way that looks
like a strategy with no signals, and any positive number would be capital the
operator never allocated. A settings file that cannot be read leaves the previous
allotment standing and says so, and a first read that fails yields nothing at all.

The bounds are checked for coherence here rather than downstream, because a
minimum above a maximum is not a number to clamp -- it is an instruction that
cannot be obeyed, and the part that would have to obey it is the one placing
orders.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.risk_types import CapitalAllotment, TradeCapitalBounds
from runtime.settings_reader import (
    SettingsParseRefused,
    load_settings_document,
    settings_directory,
)

PART_ID = "capital-allotment-reader"

PART_DECLARATION = PartDeclaration(
    part_id="capital-allotment-reader",
    consumes=(),
    produces=("capital-allotment", "leverage-ceiling", "part-health", "trade-capital-bounds"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

# The settings each segment file must carry. Named here so a missing one is
# reported by name rather than as a KeyError from somewhere downstream.
REQUIRED_SETTINGS = (
    "allocated_balance",
    "minimum_capital_per_trade",
    "maximum_capital_per_trade",
    "leverage_ceiling",
    "quote_currency",
)


class AllotmentUnreadable(RuntimeError):
    """The segment's capital settings could not be read or do not cohere."""


@dataclass
class ReaderStanding:
    reads: int = 0
    refusals: int = 0
    settings_path: str | None = None
    last_failure: str | None = None
    last_read_at_ns: int | None = None
    allotted: float | None = None


class CapitalAllotmentReader:
    """Reads one segment's capital settings, refusing anything incoherent."""

    def __init__(self, segment: str, settings_path=None, now_ns=time.time_ns) -> None:
        self._segment = segment
        self._path = settings_path or (settings_directory() / "segments" / f"{segment}.toml")
        self._now_ns = now_ns
        self._current: CapitalAllotment | None = None
        self.standing = ReaderStanding(settings_path=str(self._path))

    @property
    def current(self) -> CapitalAllotment | None:
        """The last allotment that read cleanly, or None if none ever has."""
        return self._current

    def read(self) -> CapitalAllotment:
        """Read the segment's capital settings, or raise and keep the last good one."""
        self.standing.reads += 1
        try:
            document = load_settings_document(self._path, self._segment)
        except SettingsParseRefused as refusal:
            return self._refuse(f"{type(refusal).__name__}: {refusal}")

        missing = [name for name in REQUIRED_SETTINGS if name not in document.entries]
        if missing:
            return self._refuse(
                f"{self._path} is missing {', '.join(missing)}; there is no sensible default for "
                f"how much money a segment may use"
            )

        try:
            bounds = TradeCapitalBounds(
                segment=self._segment,
                minimum_capital=float(document.read_value("minimum_capital_per_trade")),
                maximum_capital=float(document.read_value("maximum_capital_per_trade")),
                currency=str(document.read_value("quote_currency")),
            )
        except ValueError as incoherent:
            return self._refuse(f"{self._path}: {incoherent}")

        allotted = float(document.read_value("allocated_balance"))
        ceiling = float(document.read_value("leverage_ceiling"))

        if allotted < 0:
            return self._refuse(f"{self._path}: an allocation cannot be negative")
        if ceiling < 1.0:
            return self._refuse(
                f"{self._path}: a leverage ceiling of {ceiling} is below unlevered; 1.0 is the floor"
            )
        if bounds.maximum_capital > allotted and allotted > 0:
            return self._refuse(
                f"{self._path}: the maximum per trade ({bounds.maximum_capital}) exceeds the whole "
                f"allocation ({allotted}); one trade could commit more than the segment has"
            )

        self._current = CapitalAllotment(
            segment=self._segment,
            allotted=allotted,
            currency=bounds.currency,
            leverage_ceiling=ceiling,
            bounds=bounds,
            read_at_ns=self._now_ns(),
        )
        self.standing.allotted = allotted
        self.standing.last_failure = None
        self.standing.last_read_at_ns = self._current.read_at_ns
        return self._current

    def _refuse(self, reason: str) -> CapitalAllotment:
        """Keep serving the last good allotment, or refuse outright if there is none.

        Keeping the last one matters: an operator mid-edit must not be able to
        stop a running segment with a half-saved file, and stopping is what a
        zero allocation would do.
        """
        self.standing.refusals += 1
        self.standing.last_failure = reason
        if self._current is None:
            raise AllotmentUnreadable(
                f"{self._segment} has no usable capital settings and none have ever been read: {reason}"
            )
        return self._current


def describe_allotment(reader: CapitalAllotmentReader) -> dict:
    current = reader.current
    return {
        "part_id": PART_ID,
        "segment": reader._segment,
        "settings_path": reader.standing.settings_path,
        "reads": reader.standing.reads,
        "refusals": reader.standing.refusals,
        "last_failure": reader.standing.last_failure,
        "allotted": current.allotted if current else None,
        "currency": current.currency if current else None,
        "leverage_ceiling": current.leverage_ceiling if current else None,
        "minimum_capital_per_trade": current.bounds.minimum_capital if current else None,
        "maximum_capital_per_trade": current.bounds.maximum_capital if current else None,
    }


def run_capital_allotment_reader(
    reader: CapitalAllotmentReader, control_socket, publish_allotment,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        try:
            publish_allotment(reader.read())
        except AllotmentUnreadable:
            # Nothing is published. A segment that has never had readable capital
            # settings must not appear downstream with any allocation at all.
            pass

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_allotment(reader),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Consumes nothing: it reads the operator's capital settings for one segment and
    publishes what they say. Two types come out of one read because the allotment
    and the per-trade bounds are the same document read for two different questions
    -- how much this segment may use in total, and how much one trade may commit.

    A segment whose settings do not read cleanly publishes nothing at all. There is
    no default for how much money something may use, and a part downstream receiving
    a guess would size a real position against it.
    """
    publish_allotment_type = context.bus.publisher_for("capital-allotment")
    publish_bounds_type = context.bus.publisher_for("trade-capital-bounds")
    # Declared since the blueprint and published since 2026-08-23: the ceiling
    # is a field of the allotment, and the allotment goes out on this type too
    # so leverage-selector reads leverage_ceiling off what it is handed.
    publish_ceiling_type = context.bus.publisher_for("leverage-ceiling")

    def publish_allotment(allotment) -> None:
        publish_allotment_type([allotment])
        publish_bounds_type([allotment.bounds])
        publish_ceiling_type([allotment])

    return run_capital_allotment_reader(
        reader=CapitalAllotmentReader(segment=str(context.setting("segment_id").value)),
        control_socket=context.control_socket,
        publish_allotment=publish_allotment,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
