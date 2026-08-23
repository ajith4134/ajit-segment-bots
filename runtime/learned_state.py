"""Learned state that survives the off switch.

**Why this is substrate and not a part (RL-069).** A part is a process the governor
may SIGKILL at any moment (T-3), and one that keeps what it learned only in that
process learns nothing across a day. The place a learned component's coefficients
live is the same kind of question as where the tape lives or where settings live:
below the diagram, available to every part, wired to none.

The failure this closes was measured, not imagined. `bull-conviction-model` built
`OnlineLogisticModel` fresh in `start_part` on every fork. Its `is_fitted` needs
`bull_minimum_training_observations` labelled outcomes **of each class**; every
restart of the spine put that count back to zero, so the bot could accumulate
labels for an hour, be restarted, and be exactly as untrained as it was the first
time. On the run of 2026-08-22 17:37--18:42 it noticed 184 412 setups and formed
no opinion at all.

It also closes the second half of that: **nothing wrote the training count
anywhere**, so the trade board could only say `NOT MEASURED` about how far the bot
was from its first decision (Rule 8). A checkpoint is a file with a number in it,
and a board that reads the file is reporting a measurement.

## What a checkpoint is

One JSON document per (part, component), replaced atomically. JSON rather than a
pickle because a model's coefficients are the one thing about this system a person
must be able to read and argue with, and rather than `numpy.memmap` because the
shape is a sparse dict of named features that grows as features appear, not a
fixed-width array.

Small by construction: the bull model carries one coefficient and four moment
figures per feature name. Written every
`learned_state_checkpoint_interval` training observations rather than every tick,
because the cost of a crash is that many observations and the cost of writing on
every tick is an fsync per market frame.

## What is refused, and why refusing is the honest answer

A checkpoint is restored only when the settings that give its numbers their meaning
are unchanged. `feature_half_life_observations` is the decay baked into every
stored moment; `minimum_feature_observations` decides which of them may be used at
all. Restoring coefficients standardised under one half-life into a model running
another is not a small error -- it is a model whose inputs are scaled by a rule
that no longer applies, reporting itself as trained.

So a mismatch starts the component cold and **says so**, in the standing and on the
board. Learning lost to a deliberate settings change is a cost the operator chose;
learning silently reinterpreted is a model nobody can trust afterwards.

A document from a different `SCHEMA_VERSION` is refused for the same reason.
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


class LearnedStateStore:
    """Where a learned component's numbers wait for the next process.

    One directory, one file per component, named `{part_id}.{component}.json`.
    Flat rather than nested per part: the set of learned components is small, the
    names are already unique, and a flat directory is one `ls` for an operator
    asking what this system has learned.
    """

    def __init__(self, root: pathlib.Path, now_ns=time.time_ns) -> None:
        self._root = require_durable_directory(pathlib.Path(root).expanduser())
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
