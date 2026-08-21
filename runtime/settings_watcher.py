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
            as gone). A file appearing for the first time after the watch has
            already taken its catalog is symmetric with one disappearing: its
            entries read as a change from nothing, the same way an added TOML table
            in an existing file already does.
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
            But the periodic clock and an event's own delivery race on the same
            lock, and the clock can win: if it finds a diff for a path an event was
            already mid-delivery on, that is not evidence the stream lied, only
            that the backstop got there first. Every path an event names is marked
            "in flight" the instant the raw event is seen -- before either thread
            contends for the lock -- and a periodic pass finding a diff on a marked
            path reports the change without calling on_overflow. Only the event's
            own recheck clears the mark, once it has actually looked at that path.
  reliably  a callback can raise -- a transient store write failing is exactly the
            kind of thing capital-settings-change-recorder will do -- and no
            exception from on_change, on_rejection or on_overflow is allowed to
            escape into the observer's dispatch thread or the backstop's loop:
            either would silently end the corresponding line of defence, for good.
            A diff is not considered delivered, and the watch's bookkeeping does
            not advance, until its callback returns without raising -- so a broken
            consumer causes the identical diff to be recomputed and retried on the
            next pass, on either thread, rather than being dropped. A rejection is
            different: it is a state, not an outstanding piece of work, so once
            delivered once it is not repeated for the same broken content -- keyed
            on that content's own digest, so a file edited into a *different*
            broken state is reported again, and a file returned to a state already
            reported is not.

Nothing here writes a settings file. It reports; capital-settings-change-recorder
turns the report into a journal_entry, which is what the board queries.
"""

from __future__ import annotations

import hashlib
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


def _empty_document_sharing_identity_with(previous: SettingsDocument) -> SettingsDocument:
    """A document with no entries, standing in for "nothing here yet" or "nothing
    here any more" -- used both for a file that vanished (every entry it carried
    reads as gone) and for one just discovered (every entry it carries reads as
    new), by diffing against this rather than against previous's own entries.
    """
    return SettingsDocument(
        scope=previous.scope, entries={}, source_path=previous.source_path,
        content_digest="", parsed_at_ns=time.time_ns(),
    )


class _SettingsFileHandler(FileSystemEventHandler):
    """Decide whether a raw filesystem event is worth a recheck, and mark which
    settings path it concerns before anything contends for the watch's lock.

    Carries no overflow callback: watchdog's inotify backend has no path to
    surface IN_Q_OVERFLOW to a handler (see the module docstring), so a
    parameter here that could never fire would be a placeholder dressed as a
    guarantee -- RL-058 forbids exactly that. The periodic backstop on
    SettingsDirectoryWatch is what actually stands behind Rule 8, not this class.
    """

    def __init__(
        self, mark_in_flight: Callable[[pathlib.Path], None], recheck: Callable[[], None]
    ) -> None:
        self._mark_in_flight = mark_in_flight
        self._recheck = recheck

    def on_any_event(self, event) -> None:
        if getattr(event, "is_directory", False):
            return
        raw_paths = (str(event.src_path), str(getattr(event, "dest_path", "") or ""))
        matched = [
            pathlib.Path(raw) for raw in raw_paths if raw and raw.endswith(SETTINGS_SUFFIX)
        ]
        for path in matched:
            self._mark_in_flight(path)
        if matched:
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
        # The last document each path's callbacks were actually, successfully
        # handed -- distinct from LastKnownGoodSettings.current, which advances
        # on every successful parse regardless of whether delivery succeeded.
        # This is what makes an unconsumed change retried rather than lost.
        self._last_delivered: dict[pathlib.Path, SettingsDocument] = {}
        # The content digest of the last rejection actually reported for a path,
        # so a standing broken file is reported once, not once per recheck.
        self._last_reported_rejection_digest: dict[pathlib.Path, str] = {}

        # Guards self._known / self._last_delivered / the rejection digests
        # against the observer thread and the periodic thread both wanting to
        # recheck at once. Reentrant so a callback invoked from inside a recheck
        # may itself call recheck_now() without deadlocking.
        self._lock = threading.RLock()
        # A separate, cheap lock for the in-flight set: the handler must be able
        # to mark a path before it ever contends for self._lock, which a recheck
        # may hold for the duration of a slow callback.
        self._in_flight_lock = threading.Lock()
        self._in_flight: set[pathlib.Path] = set()

        for path in sorted(self._directory.glob(f"*{SETTINGS_SUFFIX}")):
            try:
                settings = LastKnownGoodSettings(path, scope=path.stem)
            except Exception as refusal:  # a file already broken when we arrived
                self._on_rejection(SettingsRejection(path, str(refusal), time.time_ns()))
                continue
            self._known[path] = settings
            # Content present when the watch first catalogued the directory is
            # not a change -- there is nothing to compare it against yet -- so it
            # is recorded as already delivered, unlike a file that appears later.
            self._last_delivered[path] = settings.current

        self._observer = InotifyObserver()
        self._observer.schedule(
            _SettingsFileHandler(self._mark_in_flight, self.recheck_now),
            str(self._directory), recursive=True,
        )
        self._stop_event = threading.Event()
        self._periodic_thread = threading.Thread(
            target=self._run_periodic_recheck,
            name="settings-directory-watch-periodic-recheck",
            daemon=True,
        )
        self._observer_started = False
        self._periodic_thread_started = False
        self._stopped = False

    def start(self) -> None:
        self._periodic_thread.start()
        self._periodic_thread_started = True
        try:
            self._observer.start()
        except Exception:
            # The periodic thread came up; the observer didn't. Reclaim what did
            # start before the caller's exception propagates, so a `finally:
            # watch.stop()` -- the idiom this whole suite uses -- does not itself
            # raise and mask the real failure.
            self.stop()
            raise
        self._observer_started = True

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        if self._observer_started:
            self._observer.stop()
            self._observer.join()
        if self._periodic_thread_started:
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

    def _mark_in_flight(self, path: pathlib.Path) -> None:
        with self._in_flight_lock:
            self._in_flight.add(path)

    def _is_in_flight(self, path: pathlib.Path) -> bool:
        with self._in_flight_lock:
            return path in self._in_flight

    def _clear_in_flight(self, path: pathlib.Path) -> None:
        with self._in_flight_lock:
            self._in_flight.discard(path)

    def _recheck(self, *, is_periodic: bool) -> None:
        """Re-read every settings file and report what actually moved."""
        with self._lock:
            current_paths = set(self._directory.glob(f"*{SETTINGS_SUFFIX}"))
            for path in sorted(current_paths):
                self._recheck_present_path(path, is_periodic=is_periodic)
            # A path _known still remembers but the directory no longer lists is
            # a settings file that vanished -- renamed away, or deleted outright.
            for path in sorted(set(self._known) - current_paths):
                self._recheck_vanished_path(path, is_periodic=is_periodic)

    def _recheck_present_path(self, path: pathlib.Path, *, is_periodic: bool) -> None:
        settings = self._known.get(path)
        if settings is None:
            self._discover_new_path(path, is_periodic=is_periodic)
            return
        rejection = settings.offer_candidate()
        if rejection is not None:
            self._report_rejection_if_new(path, rejection)
            if not is_periodic:
                self._clear_in_flight(path)
            return
        self._last_reported_rejection_digest.pop(path, None)
        previous = self._last_delivered.get(path, settings.current)
        self._deliver_diff(path, previous, settings.current, is_periodic=is_periodic)

    def _discover_new_path(self, path: pathlib.Path, *, is_periodic: bool) -> None:
        """A settings file the watch's catalog has never seen before. Unlike the
        files found at construction time, this one appeared while the watch was
        already running -- symmetric with a vanished file, its entries read as a
        change from nothing rather than being adopted silently.
        """
        try:
            settings = LastKnownGoodSettings(path, scope=path.stem)
        except Exception as refusal:
            self._report_rejection_if_new(
                path, SettingsRejection(path, str(refusal), time.time_ns())
            )
            if not is_periodic:
                self._clear_in_flight(path)
            return
        self._known[path] = settings
        nothing_yet = _empty_document_sharing_identity_with(settings.current)
        self._deliver_diff(path, nothing_yet, settings.current, is_periodic=is_periodic)

    def _recheck_vanished_path(self, path: pathlib.Path, *, is_periodic: bool) -> None:
        settings = self._known[path]
        previous = self._last_delivered.get(path, settings.current)
        nothing_left = _empty_document_sharing_identity_with(previous)
        delivered = self._deliver_diff(path, previous, nothing_left, is_periodic=is_periodic)
        if delivered:
            self._known.pop(path, None)
            self._last_delivered.pop(path, None)
            self._last_reported_rejection_digest.pop(path, None)

    def _deliver_diff(
        self,
        path: pathlib.Path,
        previous: SettingsDocument,
        candidate: SettingsDocument,
        *,
        is_periodic: bool,
    ) -> bool:
        """Diff two parses and hand the result to the callbacks it earns.

        Bookkeeping (self._last_delivered) only advances once every callback the
        diff triggers has returned without raising -- a broken consumer leaves
        the identical diff in place to be recomputed and retried on the next
        pass, on whichever thread runs it, rather than dropped. on_overflow
        fires only on a periodic pass finding a diff on a path with no event
        outstanding: a marked-in-flight path means the backstop simply won the
        race with an event's own delivery, not that the stream lied.

        Only a non-periodic pass ever clears an in-flight mark -- it is the
        pass an event's own dispatch actually triggered, so reaching this path
        is what resolves that specific outstanding event, whether or not this
        particular call finds anything left to report.
        """
        changes = compare_settings_documents(previous, candidate)
        if not changes:
            if not is_periodic:
                self._clear_in_flight(path)
            return True

        signal_overflow = is_periodic and not self._is_in_flight(path)
        delivered = True
        if signal_overflow:
            delivered = self._safely_call(self._on_overflow) and delivered
        delivered = self._safely_call(self._on_change, changes) and delivered

        if not is_periodic:
            self._clear_in_flight(path)
        if delivered:
            self._last_delivered[path] = candidate
        return delivered

    def _report_rejection_if_new(self, path: pathlib.Path, rejection: SettingsRejection) -> None:
        """Report a rejection once per distinct broken state, not once per look.

        Keyed on the offending file's own content digest: the same broken bytes
        sitting there is not reported again, but a fresh edit into a different
        broken state is. Only advances the recorded digest once on_rejection
        actually succeeds, so a callback failure leaves the rejection retried
        exactly like an unconsumed change would be.
        """
        digest = self._digest_for_rejection_dedup(path, rejection)
        if self._last_reported_rejection_digest.get(path) == digest:
            return
        if self._safely_call(self._on_rejection, rejection):
            self._last_reported_rejection_digest[path] = digest

    @staticmethod
    def _digest_for_rejection_dedup(path: pathlib.Path, rejection: SettingsRejection) -> str:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            # The file vanished between the failed parse and this read -- fall
            # back to the reason text rather than lose the dedup key entirely.
            return rejection.reason

    @staticmethod
    def _safely_call(callback: Callable[..., None], *args) -> bool:
        """Call a callback without letting it end the thread that called it.

        A callback that raises is the consumer failing, not the watch. Returns
        whether it succeeded, so the caller can decide what stays outstanding.
        """
        try:
            callback(*args)
        except Exception:
            return False
        return True
