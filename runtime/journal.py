"""The journal: an append-only, hash-chained record of what happened.

Substrate, not a part. Five recorder parts write to it and one checker reads it,
and none of them may import another, so the shape lives here.

Hash-chained because the journal is the only account of what the system did. A
record that can be edited without trace is not evidence, and every entry carries
the digest of the one before it -- so a removed or altered entry breaks the chain
at that point and everything after it.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass

GENESIS_DIGEST = "0" * 64


@dataclass(frozen=True)
class JournalTail:
    """The last entry a journal file already holds: where the next one follows on."""

    sequence: int
    digest: str


# How much of the file's end to pull in per step when walking backwards to the
# last line. Entries measured about 500 bytes on 2026-08-24, so one block holds
# a hundred of them; the loop grows the window for the rare longer line.
TAIL_READ_BLOCK_BYTES = 65536


def read_journal_tail(path) -> "JournalTail | None":
    """The sequence and digest of the last entry in a journal file, or None if empty.

    Read from the end, backwards in blocks, never the whole file: this runs at
    the start of every recorder, and what it needs is one number and one digest
    from the last line. It used to read the entire file into one string --
    against its own docstring -- which at the 3.6 GB the lifecycle journal had
    reached (2026-08-24) stalled the part's start for minutes and briefly held
    the whole file in memory. A line that will not parse returns None rather
    than a guess -- continuing a chain from an entry that could not be read
    would put a digest in the record that nothing can check.
    """
    if not path.exists():
        return None
    with open(path, "rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        window = b""
        position = size
        last = None
        while position > 0:
            take = min(TAIL_READ_BLOCK_BYTES, position)
            position -= take
            handle.seek(position)
            window = handle.read(take) + window
            lines = [line for line in window.split(b"\n") if line.strip()]
            if not lines:
                continue
            # With the window not yet at the file's start, the earliest line in
            # it may be a fragment; the last line is whole once at least one
            # other line bounds it, or the window spans the whole file.
            if position == 0 or len(lines) >= 2:
                last = lines[-1]
                break
    if last is None:
        return None
    try:
        entry = json.loads(last)
        return JournalTail(sequence=int(entry["sequence"]), digest=str(entry["digest"]))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class JournalEntry:
    """One immutable thing that happened, and its place in the chain."""

    sequence: int
    kind: str
    part_id: str
    payload: dict
    previous_digest: str
    digest: str
    recorded_at_ns: int

    def as_line(self) -> str:
        return json.dumps(
            {
                "sequence": self.sequence,
                "kind": self.kind,
                "part_id": self.part_id,
                "payload": self.payload,
                "previous_digest": self.previous_digest,
                "digest": self.digest,
                "recorded_at_ns": self.recorded_at_ns,
            },
            sort_keys=True,
        )


def compute_digest(sequence: int, kind: str, part_id: str, payload: dict, previous_digest: str) -> str:
    """The digest of one entry, over everything that identifies it.

    Sorted keys so the same content always hashes the same way: a digest that
    depended on dictionary order would break the chain on a Python upgrade rather
    than on tampering.
    """
    body = json.dumps(
        {
            "sequence": sequence,
            "kind": kind,
            "part_id": part_id,
            "payload": payload,
            "previous_digest": previous_digest,
        },
        sort_keys=True,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class Journal:
    """Appends entries and hands back what was written. The record is the sink.

    The sink is injected rather than opened here: a recorder writing to a file, a
    test writing to a list, and a later phase writing to durable storage are the
    same journal with different sinks, and none of them changes the chain.

    A journal with a sink keeps nothing in memory but the chain state -- the last
    digest and the count. It has to: `position-recorder` retaining what it had
    already written to disk held 4.4 GiB of anonymous memory over two live days
    (measured 2026-08-24, 8.59 million entries), which is the file duplicated in
    RAM for nobody. Only a journal with no sink retains its entries, because
    memory is then the only record there is -- which is what a test wants and a
    recorder must never be.
    """

    def __init__(
        self,
        append_line=None,
        now_ns=time.time_ns,
        continues_from: "JournalTail | None" = None,
    ) -> None:
        self._append_line = append_line
        self._now_ns = now_ns
        self._entries: list[JournalEntry] | None = None if append_line is not None else []
        self._appended = 0
        # Where this journal picks up. Without it a restarted recorder appends to
        # the same file starting again from the genesis digest, which leaves the
        # file holding one chain per process: an edit inside a run is detected,
        # and a whole run removed from the file leaves nothing that says it was
        # ever there. A ledger that forgets what it wrote before it restarted is
        # a pile of ledgers.
        self._last_digest = continues_from.digest if continues_from else GENESIS_DIGEST
        self._entries_before = continues_from.sequence if continues_from else 0

    @property
    def entries(self) -> tuple[JournalEntry, ...]:
        """What this journal appended. Entries it continues from are not re-read.

        Only a journal without a sink can answer: one with a sink deliberately
        keeps nothing, and its record is the file. Asking it here is a defect in
        the caller, and a loud refusal beats an empty tuple that reads as a
        journal nothing was written to.
        """
        if self._entries is None:
            raise RuntimeError(
                "this journal writes to a sink and retains nothing in memory; its record is "
                "what the sink holds. Read the sink, or keep what append() returned."
            )
        return tuple(self._entries)

    @property
    def last_digest(self) -> str:
        return self._last_digest

    def append(self, kind: str, part_id: str, payload: dict) -> JournalEntry:
        sequence = self._entries_before + self._appended + 1
        digest = compute_digest(sequence, kind, part_id, payload, self._last_digest)
        entry = JournalEntry(
            sequence=sequence,
            kind=kind,
            part_id=part_id,
            payload=payload,
            previous_digest=self._last_digest,
            digest=digest,
            recorded_at_ns=self._now_ns(),
        )
        self._appended += 1
        if self._entries is not None:
            self._entries.append(entry)
        self._last_digest = digest
        if self._append_line is not None:
            self._append_line(entry.as_line())
        return entry


def journal_path_for(base_path, part_id: str):
    """Where one recorder's own journal lives, beside the base the settings name.

    **One writer per chain.** Every entry carries the digest of the one before it,
    so a file two processes append to interleaved has no chain at all: each entry
    points at whatever the *other* recorder happened to write last, and every
    verification fails. That is what happened on 2026-08-23 once
    `position-recorder` was wired to the same `journal_path` as
    `trade-lifecycle-recorder` -- the board's record and tamper tiles both went red,
    correctly.

    A file each, named for the part, so every chain is whole and verifiable on its
    own. What is lost is a single file to read; what is kept is the only property
    the chain was for.
    """
    import pathlib as _pathlib

    base = _pathlib.Path(base_path)
    return base.with_name(f"{base.stem}.{part_id}{base.suffix}")

# How large one journal segment may grow before the next entry starts a new file.
# A size rather than a time, because what makes a segment unwieldy is its bytes:
# `read_journal_tail` walks backwards in blocks and does not care, but everything
# that reads a journal forwards does, and on 2026-09-04 the five journals held
# 38 GB between them -- 15.4 GB in one file -- written on a day with no trade
# placed. The number is a setting, never a literal in a part (RL-061); this is the
# name it is read under.
JOURNAL_SEGMENT_BYTES_SETTING = "journal_segment_maximum_bytes"


def segment_paths_for(live_path) -> tuple:
    """Every segment of one recorder's chain, oldest first, ending at the live file.

    Rolled segments are `<stem>.<sequence-it-ended-at><suffix>`, so they sort by
    the number rather than by name, and the live file is always last. A reader
    verifying the chain must walk them in this order: the first entry of a segment
    carries the digest of the last entry of the one before it, which is what makes
    rotation a place the file changes rather than a place the chain does.
    """
    import pathlib as _pathlib
    import re as _re

    live = _pathlib.Path(live_path)
    pattern = _re.compile(rf"^{_re.escape(live.stem)}\.(\d+){_re.escape(live.suffix)}$")
    rolled = []
    for candidate in live.parent.glob(f"{live.stem}.*{live.suffix}"):
        match = pattern.match(candidate.name)
        if match:
            rolled.append((int(match.group(1)), candidate))
    return tuple(path for _, path in sorted(rolled)) + ((live,) if live.exists() else ())


class RollingJournalSink:
    """Appends to one file until it is large enough, then starts the next.

    **Nothing is deleted and the chain is not broken.** A full segment is renamed
    to carry the sequence it ended at and a new live file is started; the next
    entry still carries the digest of the previous one, because the chain lives in
    the entries and not in the file. Read `segment_paths_for` in order and it
    verifies exactly as one file would.

    Retention by rotation rather than by deletion is the whole point. The journal
    is the only account of what the system did, so a policy that throws the oldest
    part of it away destroys evidence to save disk; a policy that splits it leaves
    every entry in place and lets an operator archive, compress or move a closed
    segment with a tool that is not this one. Nothing here removes a file.

    Written 2026-09-04, when the five journals held 38 GB between them -- 15.4 GB
    of it in a single `learning-recorder` file, 75% `decision-rationale` -- on a
    day when no trade had been placed.
    """

    def __init__(self, live_path, maximum_bytes: int, continues_from_sequence: int = 0) -> None:
        if maximum_bytes <= 0:
            raise ValueError(
                "a journal segment must be allowed a positive number of bytes; got "
                f"{maximum_bytes!r}. Zero or negative would roll on every entry, which "
                "is one file per record rather than a rotation policy."
            )
        self._live_path = live_path
        self._maximum_bytes = int(maximum_bytes)
        self._rolls = 0
        # The sequence of the last entry written, so a closed segment is named for
        # where it ends. Seeded from the tail the recorder already read, because a
        # sink that started counting at zero after a restart would name its second
        # segment with a number the first one already used.
        self._sequence = int(continues_from_sequence)

    @property
    def rolls(self) -> int:
        """How many segments this sink has closed. On health, so a rotation that
        never happens and one that happens constantly are different numbers."""
        return self._rolls

    @property
    def live_bytes(self) -> int:
        return self._live_path.stat().st_size if self._live_path.exists() else 0

    def roll_if_full(self, sequence: int | None = None) -> bool:
        """Close the live segment if it is at its bound. Returns whether it rolled.

        Called before an append rather than after, so a segment never exceeds the
        bound by the size of the entry that noticed -- and so the sequence naming
        the closed file is the last one actually in it.
        """
        if self.live_bytes < self._maximum_bytes:
            return False
        ended_at = self._sequence if sequence is None else sequence
        closed = self._live_path.with_name(
            f"{self._live_path.stem}.{ended_at}{self._live_path.suffix}"
        )
        if closed.exists():
            # Two rolls at one sequence cannot happen with one writer per chain,
            # and silently overwriting a closed segment would destroy the evidence
            # this class exists to keep. Refuse rather than clobber.
            raise FileExistsError(
                f"{closed} already exists, so rolling would overwrite a closed segment. "
                f"One writer per chain is the assumption; check what else is writing "
                f"{self._live_path}."
            )
        self._live_path.rename(closed)
        self._rolls += 1
        return True

    def append_line(self, line: str) -> None:
        """Write one entry, rolling first if the live segment is already at its bound.

        This is the whole interface a recorder needs: it is a drop-in for the
        `append_line` closure all five of them used to define, so rotation is a
        property of the substrate rather than something each recorder remembers to
        do. Rolling before the write keeps a segment from exceeding the bound by
        the size of the entry that noticed it.
        """
        self.roll_if_full()
        self._sequence += 1
        # Opened per append and flushed: a recorder is killed the same way every
        # part is, and a buffered ledger loses exactly the entries that were about
        # to matter.
        with open(self._live_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
