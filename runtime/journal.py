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
    """Appends entries and hands back what was written. In memory plus a sink.

    The sink is injected rather than opened here: a recorder writing to a file, a
    test writing to a list, and a later phase writing to durable storage are the
    same journal with different sinks, and none of them changes the chain.
    """

    def __init__(self, append_line=None, now_ns=time.time_ns) -> None:
        self._append_line = append_line
        self._now_ns = now_ns
        self._entries: list[JournalEntry] = []
        self._last_digest = GENESIS_DIGEST

    @property
    def entries(self) -> tuple[JournalEntry, ...]:
        return tuple(self._entries)

    @property
    def last_digest(self) -> str:
        return self._last_digest

    def append(self, kind: str, part_id: str, payload: dict) -> JournalEntry:
        sequence = len(self._entries) + 1
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
        self._entries.append(entry)
        self._last_digest = digest
        if self._append_line is not None:
            self._append_line(entry.as_line())
        return entry
