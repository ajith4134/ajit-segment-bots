"""Read the operator's settings files, and refuse a number with no provenance.

RL-055: the operator edits these over SSH, and the board shows the current values
and when they last changed. RL-061: a number is either estimated at runtime or a
named entry here carrying its provenance, and settings act as hard bounds on what
estimation may produce.

Nothing in this module writes. capital-settings-change-recorder declares
produces: [journal-entry, part-health], so under R-01 no part can write settings
back, and stdlib tomllib is read-only -- which makes that structural rather than
a convention someone has to remember.

A bad edit fails closed: the candidate is parsed into a fresh document and swapped
in only if it parsed, so the part keeps serving the last value that did. Under T-3
a part never turns itself off in reaction to its input; that is the governor's call.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import time
import tomllib
from dataclasses import dataclass

# The three keys every entry carries. 'note' is the operator's own provenance --
# who changed it and why -- recorded at the point the number enters the system.
REQUIRED_ENTRY_KEYS = ("value", "unit", "note")

# A list is admitted because some settings are a set rather than a number -- which
# venues are captured, for one. It stays a TOML array of scalars: a setting whose
# value needed a table would be a schema hiding inside a value, and the board could
# not render it or say what changed.
SettingValue = float | int | str | bool | list[str]


class SettingsParseRefused(ValueError):
    """A settings file did not parse, or an entry was missing part of its shape."""


@dataclass(frozen=True)
class SettingEntry:
    """One number the operator set, with its unit and why they set it."""

    name: str
    value: SettingValue
    unit: str
    note: str


@dataclass(frozen=True)
class SettingsDocument:
    """Everything one settings file said, and the evidence of where it came from."""

    scope: str
    entries: dict[str, SettingEntry]
    source_path: pathlib.Path
    content_digest: str
    parsed_at_ns: int

    def read_entry(self, name: str) -> SettingEntry:
        """Return the whole entry, raising if nobody declared it."""
        try:
            return self.entries[name]
        except KeyError:
            raise KeyError(
                f"no setting named '{name}' in {self.source_path}. RL-061 admits no "
                f"default: a number that was never declared has no provenance to show."
            ) from None

    def read_value(self, name: str) -> SettingValue:
        """Return just the number. Every timeout and threshold in the runtime comes from here."""
        return self.read_entry(name).value


@dataclass(frozen=True)
class SettingsRejection:
    """A candidate edit that did not parse, and therefore did not take effect."""

    source_path: pathlib.Path
    reason: str
    rejected_at_ns: int


def settings_directory() -> pathlib.Path:
    """Where the operator's settings live: ~/.config/ajit-segment-bots/settings.

    Outside the repository on purpose (section 15.3): the repo carries the schema
    and a commented template, the machine carries the values.
    """
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = pathlib.Path(config_home) if config_home else pathlib.Path.home() / ".config"
    return root / "ajit-segment-bots" / "settings"


def _digest_of(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def load_settings_document(path: pathlib.Path, scope: str) -> SettingsDocument:
    """Parse one settings file, refusing any entry that cannot say where it came from."""
    path = pathlib.Path(path)
    try:
        body = path.read_bytes()
    except OSError as failure:
        raise SettingsParseRefused(f"{path} could not be read: {failure}") from failure
    try:
        parsed = tomllib.loads(body.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as failure:
        raise SettingsParseRefused(f"{path} is not valid TOML: {failure}") from failure

    entries: dict[str, SettingEntry] = {}
    for name, table in parsed.items():
        if not isinstance(table, dict):
            raise SettingsParseRefused(
                f"{path}: '{name}' is a bare value. Every setting is a table carrying "
                f"value, unit and note, so the board can show what it means and who set it."
            )
        missing = [key for key in REQUIRED_ENTRY_KEYS if key not in table]
        if missing:
            raise SettingsParseRefused(
                f"{path}: setting '{name}' is missing {', '.join(missing)}. RL-061 requires "
                f"every number to carry its unit and its provenance."
            )
        entries[name] = SettingEntry(
            name=name, value=table["value"], unit=str(table["unit"]), note=str(table["note"])
        )

    return SettingsDocument(
        scope=scope,
        entries=entries,
        source_path=path,
        content_digest=_digest_of(body),
        parsed_at_ns=time.time_ns(),
    )


class LastKnownGoodSettings:
    """Serve the last settings that parsed, and swap only when a candidate parses.

    validate-then-swap. A syntax error in a file the operator is editing must not
    take a part down, and must not silently take effect either.
    """

    def __init__(self, path: pathlib.Path, scope: str) -> None:
        self._path = pathlib.Path(path)
        self._scope = scope
        self._current = load_settings_document(self._path, scope)

    @property
    def current(self) -> SettingsDocument:
        return self._current

    @property
    def path(self) -> pathlib.Path:
        return self._path

    def offer_candidate(self) -> SettingsRejection | None:
        """Re-read the file. Swap on success; on failure keep serving and report why.

        Returns None when the candidate was accepted or was byte-identical to what
        is already loaded -- a no-op save is not a change, which is why this compares
        the parsed digest rather than trusting mtime.
        """
        try:
            candidate = load_settings_document(self._path, self._scope)
        except SettingsParseRefused as refusal:
            return SettingsRejection(
                source_path=self._path, reason=str(refusal), rejected_at_ns=time.time_ns()
            )
        if candidate.content_digest == self._current.content_digest:
            return None
        self._current = candidate
        return None
