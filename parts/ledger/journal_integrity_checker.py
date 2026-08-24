"""journal-integrity-checker: a break in the journal's sequence or hash chain.

The journal is the system's only account of what it did. This is the part that
says whether that account can be believed.

Two independent breaks, deliberately not merged:

- **A sequence gap** is a missing entry. Something was written and is no longer
  there, or was never written at all.
- **A chain break** is an altered entry. Every entry's digest covers the previous
  digest, so changing any entry changes every digest after it, and the first
  place recomputation disagrees is where the tampering starts.

A journal can have either without the other. A deleted entry leaves a sequence
gap with an intact chain on both sides of it; an edited entry leaves a perfect
sequence and a chain that stops matching. Reporting one number would hide
whichever failure happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from runtime.journal import GENESIS_DIGEST, JournalEntry, compute_digest
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "journal-integrity-checker"

PART_DECLARATION = PartDeclaration(
    part_id="journal-integrity-checker",
    consumes=("journal-entry", "replay-mismatch"),
    produces=("journal-gap", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

SEQUENCE_GAP = "sequence-gap"
CHAIN_BREAK = "chain-break"
DIGEST_MISMATCH = "digest-mismatch"
REPLAY_MISMATCH = "replay-mismatch"


@dataclass(frozen=True)
class JournalGap:
    """One break, naming exactly where the account stops being trustworthy."""

    reason: str
    at_sequence: int
    expected: str
    observed: str
    detail: str


@dataclass
class IntegrityStanding:
    checks: int = 0
    entries_checked: int = 0
    sequence_gaps: int = 0
    chain_breaks: int = 0
    digest_mismatches: int = 0
    replay_mismatches: int = 0
    last_verified_sequence: int = 0


class JournalIntegrityChecker:
    """Recomputes the chain from the beginning and reports the first disagreement.

    From the beginning on purpose. Checking only new entries would trust
    everything already checked, and an attacker -- or a bug -- that edits history
    edits the part nobody looks at again.
    """

    def __init__(self) -> None:
        self.standing = IntegrityStanding()

    def check(self, entries) -> tuple[JournalGap, ...]:
        """Every break in this journal, in the order they occur."""
        gaps, _digest, _sequence = self.check_continuing(entries, GENESIS_DIGEST, 1)
        return gaps

    def check_continuing(
        self, entries, previous_digest: str, expected_sequence: int
    ) -> tuple[tuple[JournalGap, ...], str, int]:
        """Check entries that continue a chain already checked up to (digest, sequence).

        Returns the gaps and where the chain now stands, so a long-running
        checker keeps one digest and one number per recorder rather than every
        entry it has ever seen: a trade-lifecycle journal grows by tens of
        thousands of entries an hour, and a checker that re-read all of them on
        every wake would be the part whose tick is slower than its feed.
        """
        self.standing.checks += 1
        gaps: list[JournalGap] = []

        for entry in entries:
            self.standing.entries_checked += 1

            if entry.sequence != expected_sequence:
                self.standing.sequence_gaps += 1
                gaps.append(
                    JournalGap(
                        reason=SEQUENCE_GAP,
                        at_sequence=entry.sequence,
                        expected=str(expected_sequence),
                        observed=str(entry.sequence),
                        detail=(
                            f"{entry.sequence - expected_sequence} entr(ies) missing before "
                            f"sequence {entry.sequence}"
                        ),
                    )
                )
                expected_sequence = entry.sequence

            if entry.previous_digest != previous_digest:
                self.standing.chain_breaks += 1
                gaps.append(
                    JournalGap(
                        reason=CHAIN_BREAK,
                        at_sequence=entry.sequence,
                        expected=previous_digest,
                        observed=entry.previous_digest,
                        detail="this entry does not follow the one before it",
                    )
                )

            recomputed = compute_digest(
                entry.sequence, entry.kind, entry.part_id, entry.payload, entry.previous_digest
            )
            if recomputed != entry.digest:
                self.standing.digest_mismatches += 1
                gaps.append(
                    JournalGap(
                        reason=DIGEST_MISMATCH,
                        at_sequence=entry.sequence,
                        expected=recomputed,
                        observed=entry.digest,
                        detail="this entry's contents do not match its own digest",
                    )
                )

            previous_digest = entry.digest
            expected_sequence = entry.sequence + 1

        if not gaps:
            self.standing.last_verified_sequence = expected_sequence - 1
        return tuple(gaps), previous_digest, expected_sequence

    def observe_replay_mismatch(self, subject: str, expected: str, observed: str) -> JournalGap:
        """A replay that did not reproduce what the journal says happened.

        Not a break in the journal itself: the chain can be perfect while the
        journal describes a world the code no longer produces. That is still a
        reason to distrust the account, so it is reported here rather than
        somewhere it would be read as a code problem alone.
        """
        self.standing.replay_mismatches += 1
        return JournalGap(
            reason=REPLAY_MISMATCH,
            at_sequence=self.standing.last_verified_sequence,
            expected=expected,
            observed=observed,
            detail=f"replaying {subject} did not reproduce the journalled outcome",
        )

    def is_trustworthy(self, entries) -> bool:
        """Whether the whole journal verifies. Any break at all means no."""
        return not self.check(entries)


def describe_integrity(checker: JournalIntegrityChecker) -> dict:
    return {
        "part_id": PART_ID,
        "checks": checker.standing.checks,
        "entries_checked": checker.standing.entries_checked,
        "sequence_gaps": checker.standing.sequence_gaps,
        "chain_breaks": checker.standing.chain_breaks,
        "digest_mismatches": checker.standing.digest_mismatches,
        "replay_mismatches": checker.standing.replay_mismatches,
        "last_verified_sequence": checker.standing.last_verified_sequence,
    }


def run_journal_integrity_checker(
    checker: JournalIntegrityChecker, control_socket, read_journal, publish_gaps,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_gaps(checker.check(read_journal()))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_integrity(checker),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    Every recorder's entries arrive on one type; each recorder is its own
    chain, so the checker keeps where each chain stands -- the last digest and
    the next sequence -- and checks only what arrived since. A replay
    mismatch from the verifier is reported as a gap in trust rather than in
    the chain.
    """
    from runtime.input_assembly import Batch

    entries = Batch(read=context.bus.reader("journal-entry"))
    mismatches = Batch(read=context.bus.reader("replay-mismatch"))
    publish_gaps = context.bus.publisher_for("journal-gap")
    checker = JournalIntegrityChecker()
    chains: dict[str, tuple[str, int]] = {}

    def tick() -> None:
        by_recorder: dict[str, list] = {}
        for entry in entries.payloads():
            by_recorder.setdefault(entry.part_id, []).append(entry)
        found = []
        for recorder, batch in by_recorder.items():
            batch.sort(key=lambda entry: entry.sequence)
            digest, sequence = chains.get(recorder, (GENESIS_DIGEST, batch[0].sequence))
            gaps, digest, sequence = checker.check_continuing(batch, digest, sequence)
            chains[recorder] = (digest, sequence)
            found.extend(gaps)
        for mismatch in mismatches.payloads():
            found.append(
                checker.observe_replay_mismatch(
                    mismatch.trade_id, str(mismatch.journal_value), str(mismatch.venue_value)
                )
            )
        if found:
            publish_gaps(tuple(found))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_integrity(checker),
    )
