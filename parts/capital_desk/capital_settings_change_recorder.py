"""capital-settings-change-recorder: journal every change to a capital setting.

RL-055 asks that the board show the current values *and when they last changed*.
This part is the only place that answer is allowed to come from -- never the
file's mtime, never `git log`, because the settings file is deliberately not in
git and a timestamp says nothing about which value moved.

The reason it matters beyond bookkeeping: the equity curve is unreadable without
it. A segment that stopped making money on Tuesday and a segment whose maximum
per trade was quartered on Tuesday look identical in the results, and only this
journal separates them.

So each change records **which setting, its old value, its new value, and when**,
one entry per setting rather than one per file save. A save that changed three
settings is three facts, and a later reader asking "when did leverage change"
must not have to diff two file snapshots to find out.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.journal import Journal, JournalEntry
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "capital-settings-change-recorder"

PART_DECLARATION = PartDeclaration(
    part_id="capital-settings-change-recorder",
    consumes=(
        "main-account-setting", "capital-allotment", "trade-capital-bounds", "leverage-ceiling"
    ),
    produces=("journal-entry", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

CAPITAL_SETTING_CHANGED = "capital-setting-changed"
CAPITAL_SETTING_FIRST_SEEN = "capital-setting-first-seen"


@dataclass(frozen=True)
class SettingChange:
    """One setting moving, with both sides of the move."""

    scope: str
    setting: str
    previous_value: float | str | None
    new_value: float | str
    changed_at_ns: int
    is_first_reading: bool

    @property
    def direction(self) -> str | None:
        """Which way a numeric setting moved, for a reader scanning the journal."""
        if self.is_first_reading or not isinstance(self.new_value, (int, float)):
            return None
        if not isinstance(self.previous_value, (int, float)):
            return None
        if self.new_value > self.previous_value:
            return "increased"
        if self.new_value < self.previous_value:
            return "decreased"
        return None


@dataclass
class RecorderStanding:
    observations: int = 0
    changes_recorded: int = 0
    first_readings: int = 0
    settings_tracked: int = 0
    by_setting: dict = field(default_factory=dict)
    last_change: str | None = None


class CapitalSettingsChangeRecorder:
    """Journals each capital setting the first time it is seen and every time it moves."""

    def __init__(self, journal: Journal, now_ns=time.time_ns) -> None:
        self._journal = journal
        self._now_ns = now_ns
        self._known: dict[tuple[str, str], float | str] = {}
        self.standing = RecorderStanding()

    def observe(self, scope: str, settings: dict) -> tuple[SettingChange, ...]:
        """Record whatever moved in one scope's settings since it was last seen.

        `scope` separates the main account from each segment, so two segments
        with a `leverage_ceiling` are two settings rather than one that appears
        to flap between them.
        """
        self.standing.observations += 1
        changes: list[SettingChange] = []

        for setting, value in sorted(settings.items()):
            key = (scope, setting)
            previous = self._known.get(key)
            first = key not in self._known

            if not first and previous == value:
                continue

            self._known[key] = value
            self.standing.settings_tracked = len(self._known)
            change = SettingChange(
                scope=scope,
                setting=setting,
                previous_value=previous,
                new_value=value,
                changed_at_ns=self._now_ns(),
                is_first_reading=first,
            )
            changes.append(change)
            self._record(change)

        return tuple(changes)

    def _record(self, change: SettingChange) -> JournalEntry:
        if change.is_first_reading:
            self.standing.first_readings += 1
            kind = CAPITAL_SETTING_FIRST_SEEN
        else:
            self.standing.changes_recorded += 1
            kind = CAPITAL_SETTING_CHANGED
            self.standing.last_change = (
                f"{change.scope}.{change.setting}: {change.previous_value} -> {change.new_value}"
            )
        key = f"{change.scope}.{change.setting}"
        self.standing.by_setting[key] = self.standing.by_setting.get(key, 0) + 1

        return self._journal.append(
            kind=kind,
            part_id=PART_ID,
            payload={
                "scope": change.scope,
                "setting": change.setting,
                "previous_value": change.previous_value,
                "new_value": change.new_value,
                "direction": change.direction,
                "changed_at_ns": change.changed_at_ns,
            },
        )

    def last_changed_at(self, scope: str, setting: str) -> int | None:
        """When one setting last moved, read from the journal rather than the file.

        The board's "when it last changed" answer. A file's mtime would say when
        it was saved, which is a different question and often a misleading one.
        """
        latest = None
        for entry in self._journal.entries:
            if entry.part_id != PART_ID:
                continue
            if entry.payload.get("scope") == scope and entry.payload.get("setting") == setting:
                latest = entry.payload.get("changed_at_ns", latest)
        return latest

    def history_of(self, scope: str, setting: str) -> tuple[dict, ...]:
        """Every recorded value of one setting, oldest first."""
        return tuple(
            entry.payload
            for entry in self._journal.entries
            if entry.part_id == PART_ID
            and entry.payload.get("scope") == scope
            and entry.payload.get("setting") == setting
        )


def describe_changes(recorder: CapitalSettingsChangeRecorder) -> dict:
    return {
        "part_id": PART_ID,
        "observations": recorder.standing.observations,
        "changes_recorded": recorder.standing.changes_recorded,
        "first_readings": recorder.standing.first_readings,
        "settings_tracked": recorder.standing.settings_tracked,
        "by_setting": dict(recorder.standing.by_setting),
        "last_change": recorder.standing.last_change,
    }


def run_capital_settings_change_recorder(
    recorder: CapitalSettingsChangeRecorder, control_socket, read_settings, publish_entries,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        changes = []
        for scope, settings in read_settings():
            changes.extend(recorder.observe(scope, settings))
        publish_entries(tuple(changes))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
