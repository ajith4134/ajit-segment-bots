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
import time
from dataclasses import dataclass

GENESIS_DIGEST = "0" * 64


@dataclass(frozen=True)
class JournalTail:
    """The last entry a journal file already holds: where the next one follows on."""

    sequence: int
    digest: str


def read_journal_tail(path) -> "JournalTail | None":
    """The sequence and digest of the last entry in a journal file, or None if empty.

    Read by line rather than by parsing the whole file: this runs at the start of
    every recorder, and what it needs is one number and one digest from the end.
    A line that will not parse returns None rather than a guess -- continuing a
    chain from an entry that could not be read would put a digest in the record
    that nothing can check.
    """
    if not path.exists():
        return None
    last = None
    for line in path.read_text().splitlines():
        if line.strip():
            last = line
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
