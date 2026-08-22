"""What a part is handed when it starts: its bus, its switch, and its settings.

Every part gains one entry point, `start_part(context)`, identical in all 321 --
the sameness is what T-1 means. This is the argument. It carries the four things a
part cannot obtain for itself and must never invent:

    the control socket   its switch, which only the governor holds the other end of
    the bus             addresses derived from the blueprint, never peers by name
    the settings        every number with provenance, refused if it has none
    the declaration     what the blueprint says this part consumes and produces

A part reads its inputs through `context.bus.reader(data_type)` and publishes
through `context.bus.publisher_for(data_type)`. It cannot ask who is at the other
end, because nothing here will tell it (T-4).
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

from runtime.bus import HEALTH_TYPE, PartBus
from runtime.part_declaration import PartDeclaration
from runtime.part_process import PartHealth
from runtime.settings_reader import (
    SettingEntry,
    SettingsDocument,
    SettingsParseRefused,
    load_settings_document,
    settings_directory,
)
from runtime.wiring_plan import PartWiring, create_inbox_root, derive_wiring

RUNTIME_SCOPE = "runtime"

# The settings the substrate itself reads for every part. A part's own numbers are
# its own business; these are the ones the runtime needs to start it at all.
INBOX_RECEIVE_BUFFER_SETTING = "inbox_receive_buffer_bytes"
MAXIMUM_MESSAGE_SETTING = "maximum_message_bytes"
ABSENT_RECHECK_SETTING = "publisher_absent_recheck_interval"
HEALTH_INTERVAL_SETTING = "part_health_interval"
TICK_FLOOR_SETTING = "part_tick_floor"

# Which segment this build is running. Named in the runtime scope, and used to load
# that segment's own capital settings alongside it -- a segment's allocated balance
# and quote currency are facts about that segment's account, not about the runtime,
# and a part that had to name the file itself would carry the segment as a literal.
SEGMENT_SETTING = "segment_id"
SEGMENTS_DIRECTORY_NAME = "segments"


class SettingMissing(KeyError):
    """A number was asked for and the operator has not set it.

    Never defaulted. RL-061: a number is either estimated from data or a named
    setting carrying its provenance, and a fallback written into code is neither.
    """


@dataclass
class PartContext:
    """One part's whole world. Closing it releases the bus."""

    part_id: str
    declaration: PartDeclaration
    control_socket: object
    bus: PartBus
    settings: dict[str, SettingsDocument]
    health_interval_seconds: float
    tick_floor_seconds: float
    # Where this part may ask the launcher to switch another part, or None -- which
    # is what all but one part gets. T-2 is enforced by what the process was handed:
    # a part with no endpoint has no way to reach the switch, whatever its code says.
    switch_endpoint: str | None = None

    def setting(self, name: str, scope: str = RUNTIME_SCOPE) -> SettingEntry:
        document = self.settings.get(scope)
        if document is None:
            raise SettingMissing(
                f"'{self.part_id}' asked for setting '{name}' in scope '{scope}', and that scope "
                f"is not loaded. Scopes loaded: {sorted(self.settings)}."
            )
        try:
            return document.entries[name]
        except KeyError as missing:
            raise SettingMissing(
                f"'{self.part_id}' needs setting '{name}' in scope '{scope}' and it is not set in "
                f"{document.source_path}. There is no default: a number without provenance is the "
                f"thing RL-061 exists to refuse."
            ) from missing

    def number(self, name: str, scope: str = RUNTIME_SCOPE) -> float:
        value = self.setting(name, scope).value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingMissing(
                f"setting '{name}' in scope '{scope}' is {value!r}, which is not a number"
            )
        return value

    @property
    def input_descriptors(self) -> tuple[int, ...]:
        """What `run_part` waits on, so the part wakes on data and not only on time."""
        return self.bus.input_descriptors

    def emit_health(self, health: PartHealth) -> None:
        """Publish one health report, carrying what this part's inputs lost.

        The loss rides on health rather than having a channel of its own: health is
        the outward channel a part already has, and 223 parts declare that a skipped
        tick corrupts their answer -- a part that lost input and said nothing would
        be reporting an answer it cannot support.
        """
        self.bus.publish(
            HEALTH_TYPE,
            [
                PartHealth(
                    part_id=health.part_id,
                    state=health.state,
                    rate_ratio=health.rate_ratio,
                    staleness_seconds=health.staleness_seconds,
                    observed_at_ns=health.observed_at_ns,
                    refused_control_frame=health.refused_control_frame,
                    input_loss=tuple(sorted(self.bus.input_loss().items())),
                )
            ],
        )

    def close(self) -> None:
        self.bus.close()

    def __enter__(self) -> PartContext:
        return self

    def __exit__(self, *_exception) -> None:
        self.close()


def load_scope(scope: str, directory: pathlib.Path | None = None) -> SettingsDocument:
    root = directory if directory is not None else settings_directory()
    return load_settings_document(root / f"{scope}.toml", scope)


def load_segment_scope(segment: str, directory: pathlib.Path | None = None) -> SettingsDocument:
    """One segment's capital settings, from settings/segments/<segment>.toml."""
    root = directory if directory is not None else settings_directory()
    return load_settings_document(root / SEGMENTS_DIRECTORY_NAME / f"{segment}.toml", segment)


def open_part_context(
    part_id: str,
    control_socket,
    wiring: PartWiring | None = None,
    runtime_directory: pathlib.Path | None = None,
    settings_directory_path: pathlib.Path | None = None,
    extra_scopes: tuple[str, ...] = (),
    switch_endpoint: str | None = None,
) -> PartContext:
    """Build everything one part needs, from the blueprint and the operator's settings.

    The wiring may be supplied -- the launcher derives it once for all parts rather
    than 321 times -- and is otherwise derived here from the blueprint. Either way
    it comes from `docs/features.json`: there is no third source, and no part may
    hand in a plan of its own.
    """
    if wiring is None:
        wiring = derive_wiring(runtime_directory=runtime_directory)[part_id]
    create_inbox_root(runtime_directory)

    documents = {RUNTIME_SCOPE: load_scope(RUNTIME_SCOPE, settings_directory_path)}
    segment_entry = documents[RUNTIME_SCOPE].entries.get(SEGMENT_SETTING)
    if segment_entry is not None:
        # Loaded for every part rather than only for the ones that ask, because the
        # alternative is each part naming its own settings file -- and a part that
        # names a file is a part carrying a path it could get wrong. A segment with
        # no settings file yet is not an error here: the part that needs the numbers
        # refuses when it asks for one, which is where the refusal means something.
        try:
            documents[str(segment_entry.value)] = load_segment_scope(
                str(segment_entry.value), settings_directory_path
            )
        except SettingsParseRefused:
            pass
    for scope in extra_scopes:
        documents[scope] = load_scope(scope, settings_directory_path)

    runtime_settings = documents[RUNTIME_SCOPE]

    def required(name: str) -> float:
        try:
            entry = runtime_settings.entries[name]
        except KeyError as missing:
            raise SettingMissing(
                f"the runtime scope has no '{name}', and the bus cannot be built without it. "
                f"Install it from settings/runtime.example.toml -- {runtime_settings.source_path} "
                f"is the file the operator edits."
            ) from missing
        return entry.value

    bus = PartBus(
        wiring=wiring,
        inbox_receive_buffer_bytes=int(required(INBOX_RECEIVE_BUFFER_SETTING)),
        maximum_message_bytes=int(required(MAXIMUM_MESSAGE_SETTING)),
        absent_recheck_interval_seconds=float(required(ABSENT_RECHECK_SETTING)),
    )
    return PartContext(
        part_id=part_id,
        declaration=wiring.declaration,
        control_socket=control_socket,
        bus=bus,
        settings=documents,
        health_interval_seconds=float(required(HEALTH_INTERVAL_SETTING)),
        tick_floor_seconds=float(required(TICK_FLOOR_SETTING)),
        switch_endpoint=switch_endpoint,
    )
