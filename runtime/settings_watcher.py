"""Notice that the operator edited a settings file, and be right about it.

RL-055 wants the board to show current values and when they last changed, and
Rule 8 wants that timestamp to come from a probe rather than an assertion. So:

  trigger   watchdog's inotify backend on the DIRECTORY, not the files -- an editor
            that saves by write-temp-then-rename replaces the inode, and a watch on
            the file is left pointing at one nobody will write again.
  decide    re-parse and diff the parsed structure. mtime is not enough: vim
            rewrites the file on :wq even with nothing changed.
  overflow  inotify(7) says the queue can overflow and silently drop events. An
            absent event stream is its own state, so it forces a full re-read
            rather than reading as quiet.

Nothing here writes a settings file. It reports; capital-settings-change-recorder
turns the report into a journal_entry, which is what the board queries.
"""

from __future__ import annotations

import pathlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from watchdog.events import FileSystemEventHandler
from watchdog.observers.inotify import InotifyObserver

from runtime.settings_reader import (
    LastKnownGoodSettings,
    SettingsDocument,
    SettingsRejection,
    load_settings_document,
)

SETTINGS_SUFFIX = ".toml"
# An entry that did not exist before reads as a change from nothing, rather than
# being silently skipped -- a new bound appearing is exactly as notable as one moving.
ABSENT_VALUE = ""


@dataclass(frozen=True)
class SettingsChange:
    """One field the operator moved, and what it moved between."""

    field: str
    old_value: str
    new_value: str
    observed_at_ns: int


def compare_settings_documents(
    previous: SettingsDocument, candidate: SettingsDocument
) -> list[SettingsChange]:
    """What actually changed between two parses. Empty for a no-op save."""
    observed_at_ns = time.time_ns()
    changes: list[SettingsChange] = []
    for name in sorted(set(previous.entries) | set(candidate.entries)):
        before = previous.entries.get(name)
        after = candidate.entries.get(name)
        old_value = ABSENT_VALUE if before is None else str(before.value)
        new_value = ABSENT_VALUE if after is None else str(after.value)
        if old_value != new_value:
            changes.append(
                SettingsChange(
                    field=name, old_value=old_value, new_value=new_value,
                    observed_at_ns=observed_at_ns,
                )
            )
    return changes


class _SettingsFileHandler(FileSystemEventHandler):
    def __init__(self, recheck: Callable[[], None], on_overflow: Callable[[], None]) -> None:
        self._recheck = recheck
        self._on_overflow = on_overflow

    def on_any_event(self, event) -> None:
        if getattr(event, "is_directory", False):
            return
        if str(event.src_path).endswith(SETTINGS_SUFFIX):
            self._recheck()


class SettingsDirectoryWatch:
    """Watch a settings directory and report real changes, rejections and overflows."""

    def __init__(
        self,
        directory: pathlib.Path,
        on_change: Callable[[list[SettingsChange]], None],
        on_rejection: Callable[[SettingsRejection], None],
        on_overflow: Callable[[], None],
    ) -> None:
        self._directory = pathlib.Path(directory)
        self._on_change = on_change
        self._on_rejection = on_rejection
        self._on_overflow = on_overflow
        self._known: dict[pathlib.Path, LastKnownGoodSettings] = {}
        for path in sorted(self._directory.glob(f"*{SETTINGS_SUFFIX}")):
            try:
                self._known[path] = LastKnownGoodSettings(path, scope=path.stem)
            except Exception as refusal:  # a file already broken when we arrived
                self._on_rejection(
                    SettingsRejection(path, str(refusal), time.time_ns())
                )
        self._observer = InotifyObserver()
        self._observer.schedule(
            _SettingsFileHandler(self.recheck_now, self._on_overflow),
            str(self._directory), recursive=True,
        )

    def start(self) -> None:
        self._observer.start()

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()

    def recheck_now(self) -> None:
        """Re-read every settings file and report what actually moved.

        Called on an inotify event and, deliberately, on overflow: the answer to a
        stream that admits it dropped something is to re-establish ground truth.
        """
        for path in sorted(self._directory.glob(f"*{SETTINGS_SUFFIX}")):
            settings = self._known.get(path)
            if settings is None:
                try:
                    self._known[path] = LastKnownGoodSettings(path, scope=path.stem)
                except Exception as refusal:
                    self._on_rejection(SettingsRejection(path, str(refusal), time.time_ns()))
                continue
            previous = settings.current
            rejection = settings.offer_candidate()
            if rejection is not None:
                self._on_rejection(rejection)
                continue
            changes = compare_settings_documents(previous, settings.current)
            if changes:
                self._on_change(changes)
