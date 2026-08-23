"""community-chat-reader: the noisiest source, read with that assumed.

Community chat is where a market's participants say what they are actually doing,
and it is overwhelmingly noise, promotion and people talking their own book. It is
worth reading and it is the source most likely to poison a system that reads it
naively.

So this reader is built around the assumption that most of what it sees is
worthless:

- **It consumes nothing.** The only part in the system with an empty consumes
  list -- it is a source of raw material, not a responder to demand, and that is
  what the blueprint declares. It is rate-limited hard for the same reason.
- **A claim repeated by many accounts is not corroboration.** It is the shape of
  both a real event and a coordinated one, and this reader records the count
  without treating it as evidence.
- **Nothing is admitted on its own.** A chat message becomes a source document
  only when it is substantive enough to distil from, and the distiller then
  requires every rule to trace to it. A one-line opinion produces nothing, which
  is correct.
- **Content is data, never instruction.** This is the source most likely to
  contain text designed to be obeyed, and nothing here obeys anything.

**Accounts are not scored or trusted.** Reputation in a chat is exactly what a
motivated participant builds, and a reader that weighted by it would be weighting
by effort spent on appearing credible.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "community-chat-reader"

PART_DECLARATION = PartDeclaration(
    part_id="community-chat-reader",
    consumes=(),
    produces=("source-document", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

READ = "read"
TOO_THIN = "too-short-to-distil-anything-from"
RATE_LIMITED = "the-read-budget-for-this-window-is-spent"
NO_READER = "no-reader-is-installed"
READ_FAILED = "the-read-itself-failed"


@dataclass(frozen=True)
class ChatMessage:
    """One message, as it arrived."""

    channel: str
    account: str
    text: str
    posted_at_ns: int
    message_reference: str


@dataclass(frozen=True)
class ChatReading:
    """What one read produced, with how many accounts said the same thing."""

    channel: str
    state: str
    content: str | None
    source_reference: str | None
    messages_read: int
    messages_kept: int
    accounts_saying_the_same: int
    reason: str
    read_at_ns: int

    @property
    def is_a_document(self) -> bool:
        return self.state == READ

    @property
    def repetition_is_corroboration(self) -> bool:
        """Never. It is the shape of both a real event and a coordinated one."""
        return False


@dataclass
class ReaderStanding:
    reads: int = 0
    messages_read: int = 0
    messages_kept: int = 0
    documents_returned: int = 0
    too_thin: int = 0
    rate_limited: int = 0
    failures: int = 0
    largest_repetition_seen: int = 0
    by_channel: dict = field(default_factory=dict)


class CommunityChatReader:
    """Reads chat assuming most of it is worthless, and scores no account."""

    def __init__(
        self,
        minimum_message_characters: int,
        reads_per_window: int,
        window_seconds: float,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        if minimum_message_characters < 1:
            raise ValueError(
                "a one-line opinion produces nothing to distil, and admitting it anyway is "
                "how the noisiest source becomes the loudest"
            )
        if reads_per_window < 1 or window_seconds <= 0:
            raise ValueError(
                "this is the source most likely to get an account blocked, so its rate has "
                "to be bounded"
            )
        self._minimum_characters = minimum_message_characters
        self._reads_per_window = reads_per_window
        self._window_seconds = window_seconds
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._read_times: list = []
        self._reader = None
        self.standing = ReaderStanding()

    def install_reader(self, reader) -> None:
        """The real connection. `reader(channel)` returns ChatMessage objects."""
        self._reader = reader

    def budget_remaining(self) -> int:
        now = self._monotonic()
        self._read_times = [at for at in self._read_times if now - at < self._window_seconds]
        return max(0, self._reads_per_window - len(self._read_times))

    def accounts_saying_the_same(self, messages) -> int:
        """How many distinct accounts said substantially the same thing.

        Counted and reported, never treated as evidence: repetition is the shape
        of both a real event and a coordinated one.
        """
        if not messages:
            return 0
        fingerprints: dict[str, set] = {}
        for message in messages:
            words = sorted(word.lower() for word in message.text.split() if len(word) > 5)
            fingerprint = " ".join(words[:8])
            if not fingerprint:
                continue
            fingerprints.setdefault(fingerprint, set()).add(message.account)
        return max((len(accounts) for accounts in fingerprints.values()), default=0)

    def read(self, channel: str) -> ChatReading:
        """One channel, read once, with the substantive messages kept."""
        self.standing.reads += 1

        if self._reader is None:
            self.standing.failures += 1
            return self._reading(channel, NO_READER, None, None, 0, 0, 0,
                                 "no reader is installed")

        if self.budget_remaining() <= 0:
            self.standing.rate_limited += 1
            return self._reading(
                channel, RATE_LIMITED, None, None, 0, 0, 0,
                f"{self._reads_per_window} read(s) already made this window",
            )

        self._read_times.append(self._monotonic())

        try:
            messages = list(self._reader(channel))
        except Exception:
            self.standing.failures += 1
            return self._reading(channel, READ_FAILED, None, None, 0, 0, 0,
                                 "the read failed and is not retried")

        self.standing.messages_read += len(messages)
        self.standing.by_channel[channel] = self.standing.by_channel.get(channel, 0) + 1

        # A message becomes material only when it is substantive enough to
        # distil from. A one-line opinion produces nothing, which is correct.
        kept = [
            message for message in messages
            if len(message.text) >= self._minimum_characters
        ]
        self.standing.messages_kept += len(kept)

        repetition = self.accounts_saying_the_same(messages)
        self.standing.largest_repetition_seen = max(
            self.standing.largest_repetition_seen, repetition
        )

        if not kept:
            self.standing.too_thin += 1
            return self._reading(
                channel, TOO_THIN, None, None, len(messages), 0, repetition,
                f"{len(messages)} message(s) read and none is longer than "
                f"{self._minimum_characters} character(s). This source is overwhelmingly "
                f"noise, and producing nothing from a channel of one-liners is correct",
            )

        self.standing.documents_returned += 1
        return self._reading(
            channel, READ,
            # As it arrived. Nothing here obeys anything, and this is the source
            # most likely to contain text designed to be obeyed.
            "\n\n".join(f"[{message.account}] {message.text}" for message in kept),
            f"chat:{channel}:{kept[0].message_reference}",
            len(messages), len(kept), repetition,
            f"{len(kept)} substantive message(s) of {len(messages)} read"
            + (
                f"; {repetition} account(s) said substantially the same thing, which is "
                f"recorded and is not corroboration -- it is the shape of both a real event "
                f"and a coordinated one"
                if repetition > 1
                else ""
            )
            + ". No account is scored or trusted: reputation in a chat is exactly what a "
            "motivated participant builds",
        )

    def _reading(
        self, channel, state, content, source_reference, read, kept, repetition, reason
    ) -> ChatReading:
        return ChatReading(
            channel=channel,
            state=state,
            content=content,
            source_reference=source_reference,
            messages_read=read,
            messages_kept=kept,
            accounts_saying_the_same=repetition,
            reason=reason,
            read_at_ns=self._now_ns(),
        )


def describe_chat_reading(reader: CommunityChatReader) -> dict:
    return {
        "part_id": PART_ID,
        "reader_is_installed": reader._reader is not None,
        "reads": reader.standing.reads,
        "messages_read": reader.standing.messages_read,
        "messages_kept": reader.standing.messages_kept,
        "documents_returned": reader.standing.documents_returned,
        "channels_with_nothing_substantive": reader.standing.too_thin,
        "rate_limited": reader.standing.rate_limited,
        "failures": reader.standing.failures,
        "largest_repetition_seen": reader.standing.largest_repetition_seen,
        "by_channel": dict(sorted(reader.standing.by_channel.items())),
        "scores_accounts": False,
        "treats_repetition_as_corroboration": False,
    }


def run_community_chat_reader(
    reader: CommunityChatReader, control_socket, read_channels, publish_documents,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_documents(tuple(reader.read(channel) for channel in read_channels()))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
