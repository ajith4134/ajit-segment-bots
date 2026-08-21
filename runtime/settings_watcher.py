"""Notice that the operator edited a settings file, and be right about it.

RL-055 wants the board to show current values and when they last changed, and
Rule 8 wants that timestamp to come from a probe rather than an assertion. So:

  trigger   watchdog's inotify backend on the DIRECTORY, not the files -- an editor
            that saves by write-temp-then-rename replaces the inode, and a watch on
            the file is left pointing at one nobody will write again. A moved event
            carries the path it came FROM in src_path and the path it landed AS in
            dest_path, so both are inspected: a save that lands ON a settings file
            is a change (the common case -- src_path is a stray temp name, dest_path
            is the settings file), and a rename that takes a settings file's name
            AWAY is a change too (src_path is the settings file, its entries read
            as gone).
  decide    re-parse and diff the parsed structure. mtime is not enough: vim
            rewrites the file on :wq even with nothing changed.
  overflow  inotify(7) documents IN_Q_OVERFLOW for a dropped event, but measured
            directly against this project's pinned watchdog==6.0.0: its own C shim
            discards that marker (wd == -1) before it becomes an observable event,
            so nothing built on the library can react to the kernel's own signal --
            silent by construction, which is worse than no guarantee at all. So
            correctness does not rest on catching it. Instead a periodic full
            re-read runs on its own schedule (recheck_interval_seconds, a named
            setting -- RL-061, this module reads none itself), independent of
            whatever any event announced. A document already in sync produces no
            diff, so any diff that periodic pass finds is proof, not a guess, that
            no event resolved it first -- that is exactly when on_overflow fires,
            alongside on_change reporting the real diff. A dropped or coalesced
            event then costs at most one interval of latency, never correctness.

Nothing here writes a settings file. It reports; capital-settings-change-recorder
turns the report into a journal_entry, which is what the board queries.
"""

from __future__ import annotations

import pathlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from watchdog.events import FileSystemEventHandler
from watchdog.observers.inotify import InotifyObserver

from runtime.settings_reader import (
    LastKnownGoodSettings,
    SettingsDocument,
    SettingsRejection,
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


def _document_for_a_vanished_file(previous: SettingsDocument) -> SettingsDocument:
    """Stand in for a settings file that is no longer there: every entry it
    carried reads as gone, the same way an entry that never existed does.
    """
    return SettingsDocument(
        scope=previous.scope, entries={}, source_path=previous.source_path,
        content_digest="", parsed_at_ns=time.time_ns(),
    )


class _SettingsFileHandler(FileSystemEventHandler):
    """Decide whether a raw filesystem event is worth a recheck.

    Carries no overflow callback: watchdog's inotify backend has no path to
    surface IN_Q_OVERFLOW to a handler (see the module docstring), so a
    parameter here that could never fire would be a placeholder dressed as a
    guarantee -- RL-058 forbids exactly that. The periodic backstop on
    SettingsDirectoryWatch is what actually stands behind Rule 8, not this class.
    """

    def __init__(self, recheck: Callable[[], None]) -> None:
        self._recheck = recheck

    def on_any_event(self, event) -> None:
        if getattr(event, "is_directory", False):
            return
        paths = (str(event.src_path), str(getattr(event, "dest_path", "") or ""))
        if any(path.endswith(SETTINGS_SUFFIX) for path in paths if path):
            self._recheck()


class SettingsDirectoryWatch:
    """Watch a settings directory and report real changes, rejections and overflows."""

    def __init__(
        self,
        directory: pathlib.Path,
        on_change: Callable[[list[SettingsChange]], None],
        on_rejection: Callable[[SettingsRejection], None],
        on_overflow: Callable[[], None],
        recheck_interval_seconds: float,
    ) -> None:
        self._directory = pathlib.Path(directory)
        self._on_change = on_change
        self._on_rejection = on_rejection
        self._on_overflow = on_overflow
        self._recheck_interval_seconds = recheck_interval_seconds
        self._known: dict[pathlib.Path, LastKnownGoodSettings] = {}
        # Guards self._known against the observer thread and the periodic thread
        # both wanting to recheck at once. Reentrant so a callback invoked from
        # inside a recheck may itself call recheck_now() without deadlocking.
        self._lock = threading.RLock()
        for path in sorted(self._directory.glob(f"*{SETTINGS_SUFFIX}")):
            try:
                self._known[path] = LastKnownGoodSettings(path, scope=path.stem)
            except Exception as refusal:  # a file already broken when we arrived
                self._on_rejection(
                    SettingsRejection(path, str(refusal), time.time_ns())
                )
        self._observer = InotifyObserver()
        self._observer.schedule(
            _SettingsFileHandler(self.recheck_now), str(self._directory), recursive=True,
        )
        self._stop_event = threading.Event()
        self._periodic_thread = threading.Thread(
            target=self._run_periodic_recheck,
            name="settings-directory-watch-periodic-recheck",
            daemon=True,
        )

    def start(self) -> None:
        self._observer.start()
        self._periodic_thread.start()

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()
        self._stop_event.set()
        self._periodic_thread.join()

    def recheck_now(self) -> None:
        """Re-read every settings file now, because an event or a caller already
        has reason to look. Does not itself signal overflow: a recheck that was
        asked for finding a real diff is the system working, not evidence that
        anything was missed.
        """
        self._recheck(is_periodic=False)

    def _run_periodic_recheck(self) -> None:
        """The backstop's own clock: fire every recheck_interval_seconds until
        stopped, regardless of whether any event fired in between.
        """
        while not self._stop_event.wait(self._recheck_interval_seconds):
            self._recheck(is_periodic=True)

    def _recheck(self, *, is_periodic: bool) -> None:
        """Re-read every settings file and report what actually moved.

        `is_periodic` marks a pass the backstop's own clock ran, rather than one
        an event or a caller asked for. A document already in sync produces no
        diff, so any diff a periodic pass finds is proof no event resolved it
        first -- see _emit_changes_between.
        """
        with self._lock:
            current_paths = set(self._directory.glob(f"*{SETTINGS_SUFFIX}"))
            for path in sorted(current_paths):
                settings = self._known.get(path)
                if settings is None:
                    try:
                        self._known[path] = LastKnownGoodSettings(path, scope=path.stem)
                    except Exception as refusal:
                        self._on_rejection(
                            SettingsRejection(path, str(refusal), time.time_ns())
                        )
                    continue
                previous = settings.current
                rejection = settings.offer_candidate()
                if rejection is not None:
                    self._on_rejection(rejection)
                    continue
                self._emit_changes_between(previous, settings.current, is_periodic)

            # A path _known still remembers but the directory no longer lists is
            # a settings file that vanished -- renamed away, or deleted outright.
            # Its entries read as gone, exactly like an entry that never existed.
            for path in sorted(set(self._known) - current_paths):
                settings = self._known.pop(path)
                previous = settings.current
                self._emit_changes_between(
                    previous, _document_for_a_vanished_file(previous), is_periodic
                )

    def _emit_changes_between(
        self, previous: SettingsDocument, candidate: SettingsDocument, is_periodic: bool
    ) -> None:
        """Diff two parses and call the callbacks a real difference earns.

        on_overflow fires only alongside a periodic-pass diff: that is the one
        situation proving the event stream did not already report this change.
        """
        changes = compare_settings_documents(previous, candidate)
        if not changes:
            return
        if is_periodic:
            self._on_overflow()
        self._on_change(changes)
