"""State that survives the off switch, whatever kind of state it is.

**Why this is substrate and not a part (RL-069).** A part is a process the governor
may SIGKILL at any moment (T-3), and one that keeps its state only in that process
keeps nothing across a day. Where that state lives is the same kind of question as
where the tape lives or where settings live: below the diagram, available to every
part, wired to none.

This module is the mechanism -- one JSON document per (part, component), replaced
atomically, restored only when the settings that give its numbers meaning are
unchanged and the schema still matches. It has no opinion about what the numbers
are. `runtime/learned_state.py` is this mechanism used for a learned component's
coefficients, and carries the measured story of why that was needed.

It was named for learning because learning was the first thing that needed it. It
is not the only thing: a lot book is not learned and still has to survive a
restart, because a position whose lots are forgotten can never be closed and the
round trip can never be scored. Two names for one mechanism would be worse than
one name that says what it does (Rule 7).

## What a checkpoint is

One JSON document per (part, component), replaced atomically. JSON rather than a
pickle because the stored numbers are the one thing about this system a person
must be able to read and argue with.

## What is refused, and why refusing is the honest answer

A checkpoint is restored only when the settings that give its numbers their meaning
are unchanged, and only at a schema version this build can read. A mismatch starts
the component cold and **says so**, in the standing and on the board.

State lost to a deliberate settings change is a cost the operator chose; state
silently reinterpreted is state nobody can trust afterwards.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import time
from dataclasses import dataclass

from runtime.storage_facts import require_durable_directory

# Bumped when the shape of a stored document changes in a way that makes an older
# one unreadable. A reader that guessed at an older shape would restore numbers
# into fields that no longer mean what they meant.
SCHEMA_VERSION = 1

# Why a component started cold. These are states, not error strings: the board
# renders each one differently and `NO_CHECKPOINT` on a first run is not a fault.
STARTED_COLD_NO_CHECKPOINT = "no-checkpoint-has-been-written-yet"
STARTED_COLD_SETTINGS_CHANGED = "the-settings-that-give-the-stored-numbers-meaning-changed"
STARTED_COLD_SCHEMA_CHANGED = "the-checkpoint-was-written-by-an-incompatible-version"
STARTED_COLD_UNREADABLE = "the-checkpoint-could-not-be-read"
RESTORED = "restored-from-checkpoint"


class CheckpointRefused(RuntimeError):
    """A checkpoint exists and may not be used. Never raised past a part's start."""


@dataclass(frozen=True)
class Restoration:
    """What happened when a component asked for its previous state.

    `state` is None whenever the component must start cold, and `verdict` says
    which of the reasons above applies. A caller that reads only `state` gets
    correct behaviour; a caller that reports `verdict` gets a board that can
    distinguish a first run from a discarded checkpoint.
    """

    state: dict | None
    verdict: str
    saved_at_ns: int | None
    detail: str

    @property
    def was_restored(self) -> bool:
        return self.state is not None


class DurableStateStore:
    """Where a learned component's numbers wait for the next process.

    One directory, one file per component, named `{part_id}.{component}.json`.
    Flat rather than nested per part: the set of learned components is small, the
    names are already unique, and a flat directory is one `ls` for an operator
    asking what this system has learned.
    """

    def __init__(self, root: pathlib.Path, now_ns=time.time_ns) -> None:
        # Created, not merely required. `require_durable_directory` answers "would
        # what I write here survive" and does not make the directory, so until
        # 2026-08-25 every store depended on someone having made it by hand. That
        # held only because `learned/` happened to exist: the first store pointed
        # at a new directory crash-looped its part on a FileNotFoundError from
        # inside `tempfile`, which names the temp file rather than the directory
        # and reads like a race. Durability is checked first, so a directory that
        # would not survive is still refused rather than quietly created on tmpfs.
        self._root = require_durable_directory(pathlib.Path(root).expanduser())
        self._root.mkdir(parents=True, exist_ok=True)
        self._now_ns = now_ns

    @property
    def root(self) -> pathlib.Path:
        return self._root

    def path_for(self, part_id: str, component: str) -> pathlib.Path:
        return self._root / f"{part_id}.{component}.json"

    def save(self, part_id: str, component: str, state: dict, settings: dict) -> pathlib.Path:
        """Write one checkpoint, atomically, with the settings it was learned under.

        Temp file in the same directory, fsync, then `os.replace`, then fsync of
        the directory itself. Without the directory sync the rename can be lost
        while the file's contents are safe, which leaves the old checkpoint in
        place -- a silently stale model is the failure this whole module exists
        to prevent.
        """
        document = {
            "schema_version": SCHEMA_VERSION,
            "part_id": part_id,
            "component": component,
            "saved_at_ns": self._now_ns(),
            "settings": dict(settings),
            "state": state,
        }
        destination = self.path_for(part_id, component)
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self._root,
            prefix=f".{destination.name}.", suffix=".partial", delete=False,
        )
        try:
            with handle:
                json.dump(document, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, destination)
        except BaseException:
            pathlib.Path(handle.name).unlink(missing_ok=True)
            raise
        directory = os.open(self._root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return destination

    def restore(self, part_id: str, component: str, settings: dict) -> Restoration:
        """The stored state, or the reason this component must start cold."""
        path = self.path_for(part_id, component)
        if not path.exists():
            return Restoration(
                state=None,
                verdict=STARTED_COLD_NO_CHECKPOINT,
                saved_at_ns=None,
                detail=f"nothing has been written to {path}",
            )
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as failure:
            return Restoration(
                state=None,
                verdict=STARTED_COLD_UNREADABLE,
                saved_at_ns=None,
                detail=f"{path} could not be read: {failure}",
            )

        stored_version = document.get("schema_version")
        if stored_version != SCHEMA_VERSION:
            return Restoration(
                state=None,
                verdict=STARTED_COLD_SCHEMA_CHANGED,
                saved_at_ns=document.get("saved_at_ns"),
                detail=(
                    f"{path} was written at schema version {stored_version!r} and this build "
                    f"reads version {SCHEMA_VERSION}"
                ),
            )

        changed = compare_settings(document.get("settings") or {}, settings)
        if changed:
            return Restoration(
                state=None,
                verdict=STARTED_COLD_SETTINGS_CHANGED,
                saved_at_ns=document.get("saved_at_ns"),
                detail=(
                    "the stored numbers were learned under different settings: "
                    + "; ".join(changed)
                ),
            )

        return Restoration(
            state=document.get("state"),
            verdict=RESTORED,
            saved_at_ns=document.get("saved_at_ns"),
            detail=f"restored from {path}",
        )


def compare_settings(stored: dict, current: dict) -> list[str]:
    """Which meaning-bearing settings differ, said in full rather than counted.

    Compared as text after a float round-trip so that 5000 and 5000.0 -- the same
    number arriving from TOML and from a JSON document -- are not reported as a
    change that discards a model.
    """
    differences = []
    for name in sorted(set(stored) | set(current)):
        before = _comparable(stored.get(name))
        after = _comparable(current.get(name))
        if before != after:
            differences.append(f"{name} was {stored.get(name)!r} and is now {current.get(name)!r}")
    return differences


def _comparable(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return value


class CheckpointSchedule:
    """Decides when enough has been learned to be worth an fsync.

    Counts training observations rather than seconds: what a crash costs is
    observations, and a model that trained twice in an hour has nothing to lose by
    waiting while one training a thousand times a minute has a great deal.
    """

    def __init__(self, every_observations: int) -> None:
        if every_observations < 1:
            raise ValueError(
                "a checkpoint interval below one observation would fsync on every training step"
            )
        self._every = int(every_observations)
        self._written_at: int | None = None

    def is_due(self, observations: int) -> bool:
        """True immediately on a component that has never written one.

        The first checkpoint is written before anything has been learned, and
        that is the point: a document saying "0 outcomes, as of this time" is what
        lets a board report how far the bot is from its first decision. With no
        file at all, an untrained bot and a bot nobody started look identical
        (Rule 8).
        """
        if self._written_at is None:
            return True
        return observations - self._written_at >= self._every

    def record_written(self, observations: int) -> None:
        self._written_at = int(observations)

    @property
    def observations_at_last_write(self) -> int | None:
        return self._written_at


def restore_and_arm_checkpoint(store, schedule, part_id, component, holder, settings):
    """Bring a component's state back, and return the function that writes it down.

    `holder` is anything with `read_checkpoint_state()`, `restore_from_checkpoint()`
    and a `standing` -- named by shape rather than by part, so this stays substrate
    and never becomes a list of the parts that use it (T-4).

    A checkpoint that cannot be read starts the component cold and says so in the
    standing. Refusing to start would be worse: a part that will not run because it
    cannot remember has turned a recoverable gap into an outage.

    The first write happens here, before any observation. A file saying "nothing
    held, as of this time" is what lets a board tell a part that has never run from
    one that came back holding nothing (Rule 8).
    """
    restoration = store.restore(part_id, component, settings)
    if restoration.was_restored:
        holder.standing.restored_symbols = holder.restore_from_checkpoint(restoration.state)
    holder.standing.checkpoint_verdict = restoration.verdict

    def write_checkpoint(observations: int) -> None:
        if not schedule.is_due(observations):
            return
        store.save(part_id, component, holder.read_checkpoint_state(), settings)
        schedule.record_written(observations)

    write_checkpoint(0)
    return write_checkpoint
