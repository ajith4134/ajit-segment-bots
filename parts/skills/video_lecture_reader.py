"""video-lecture-reader: the transcript and the frames, because slides carry content.

A lecture's audio is half of it. Slides, charts and screen recordings carry the
part that is hardest to say out loud -- a formula, a chart, a table of results --
and a reader that took only the transcript would systematically lose exactly the
material worth having.

So this reads both, and the discipline is about what each is good for:

- **The transcript is the argument.** It says why, and it is where the decision
  rules are phrased.
- **The frames are the evidence.** Formulas, tables and charts appear there and
  nowhere else, and a transcript saying "as you can see here" is a pointer to a
  frame with no text to point at.
- **A transcript with no frames is reported as partial**, never as complete. That
  combination is the specific failure mode of every transcript tool -- the audio
  works and the video extraction silently returns nothing -- and it looks
  identical to a lecture with no slides.
- **Frames are sampled, not exhaustive.** A frame per second of a 90-minute
  lecture is 5,400 near-identical images; sampling on change is what makes this
  affordable.

**Nothing is inferred from the title or the description.** They are written to be
clicked on, and a reader that used them would be reading marketing.

**Content is data.** A slide saying "run this command" is a slide.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "video-lecture-reader"

PART_DECLARATION = PartDeclaration(
    part_id="video-lecture-reader",
    consumes=("web-idea",),
    produces=("source-document", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

READ = "read"
PARTIAL_TRANSCRIPT_ONLY = "the-transcript-worked-and-the-frames-returned-nothing"
PARTIAL_FRAMES_ONLY = "frames-were-extracted-and-no-transcript-was-available"
NOTHING_RETRIEVED = "neither-a-transcript-nor-a-frame-could-be-obtained"
NO_READER = "no-reader-is-installed"


@dataclass(frozen=True)
class LectureReading:
    """What one lecture produced, from both halves of it."""

    reference: str
    state: str
    transcript: str | None
    frame_texts: tuple
    cue_count: int
    frame_count: int
    frames_sampled_from: int
    reason: str
    read_at_ns: int

    @property
    def is_a_document(self) -> bool:
        return self.state == READ

    @property
    def is_complete(self) -> bool:
        return self.cue_count > 0 and self.frame_count > 0

    @property
    def content(self) -> str:
        """Both halves together, with the frames marked as what they are."""
        parts = []
        if self.transcript:
            parts.append(f"[transcript]\n{self.transcript}")
        for index, text in enumerate(self.frame_texts):
            parts.append(f"[frame {index + 1}]\n{text}")
        return "\n\n".join(parts)


@dataclass
class ReaderStanding:
    lectures_read: int = 0
    complete: int = 0
    transcript_only: int = 0
    frames_only: int = 0
    nothing_retrieved: int = 0
    failures: int = 0
    cues_read: int = 0
    frames_kept: int = 0
    frames_sampled_from: int = 0


class VideoLectureReader:
    """Reads the transcript and the frames, and says loudly when only one arrived."""

    def __init__(
        self,
        maximum_frames: int,
        frame_change_threshold: float,
        now_ns=time.time_ns,
    ) -> None:
        if maximum_frames < 1:
            raise ValueError(
                "slides carry the part that is hardest to say out loud, so at least one frame "
                "has to be kept"
            )
        if not 0.0 < frame_change_threshold <= 1.0:
            raise ValueError(
                "a frame per second of a 90-minute lecture is 5,400 near-identical images; "
                "sampling on change is what makes this affordable"
            )
        self._maximum_frames = maximum_frames
        self._change_threshold = frame_change_threshold
        self._now_ns = now_ns
        self._reader = None
        self.standing = ReaderStanding()

    def install_reader(self, reader) -> None:
        """The real retrieval. `reader(reference)` returns `(cues, frame_texts)`."""
        self._reader = reader

    def sample_frames(self, frame_texts) -> tuple:
        """Keep frames that differ from the one before, up to the cap.

        Sampling on change rather than on a clock: consecutive frames of the same
        slide are one slide, and keeping all of them spends the budget on
        repetition.
        """
        kept = []
        previous = ""
        for text in frame_texts:
            if len(kept) >= self._maximum_frames:
                break
            if self._difference(previous, text) < self._change_threshold:
                continue
            kept.append(text)
            previous = text
        return tuple(kept)

    def read(self, reference: str) -> LectureReading:
        """One lecture, both halves, with the partial cases named."""
        self.standing.lectures_read += 1

        if self._reader is None:
            self.standing.failures += 1
            return self._reading(reference, NO_READER, None, (), 0, 0, 0,
                                 "no reader is installed")

        try:
            cues, frame_texts = self._reader(reference)
        except Exception:
            self.standing.failures += 1
            return self._reading(
                reference, NOTHING_RETRIEVED, None, (), 0, 0, 0,
                "the read failed and is not retried",
            )

        cues = list(cues or ())
        frame_texts = list(frame_texts or ())
        self.standing.cues_read += len(cues)
        self.standing.frames_sampled_from += len(frame_texts)

        sampled = self.sample_frames(frame_texts)
        self.standing.frames_kept += len(sampled)
        transcript = "\n".join(cues) if cues else None

        if not cues and not sampled:
            self.standing.nothing_retrieved += 1
            return self._reading(
                reference, NOTHING_RETRIEVED, None, (), 0, 0, len(frame_texts),
                "neither a transcript nor a frame could be obtained. Nothing is inferred from "
                "the title or the description: they are written to be clicked on",
            )

        if cues and not sampled:
            # The specific failure mode of every transcript tool: the audio works
            # and the video extraction silently returns nothing, which looks
            # identical to a lecture with no slides.
            self.standing.transcript_only += 1
            return self._reading(
                reference, PARTIAL_TRANSCRIPT_ONLY, transcript, (), len(cues), 0,
                len(frame_texts),
                f"{len(cues)} transcript cue(s) and no frames. That combination is the "
                f"specific failure of every transcript tool -- the audio works and the video "
                f"extraction returns nothing -- and it looks identical to a lecture with no "
                f"slides, so it is reported as partial rather than as complete",
            )

        if sampled and not cues:
            self.standing.frames_only += 1
            return self._reading(
                reference, PARTIAL_FRAMES_ONLY, None, sampled, 0, len(sampled),
                len(frame_texts),
                f"{len(sampled)} frame(s) and no transcript. The frames carry the evidence and "
                f"the transcript carries the argument, so this has the results without the "
                f"reasoning",
            )

        self.standing.complete += 1
        return self._reading(
            reference, READ, transcript, sampled, len(cues), len(sampled), len(frame_texts),
            f"{len(cues)} transcript cue(s) and {len(sampled)} frame(s) sampled from "
            f"{len(frame_texts)}. Both halves: the transcript is the argument and the frames "
            f"are the evidence -- formulas, tables and charts appear there and nowhere else, "
            f"and 'as you can see here' is a pointer to a frame with no text to point at",
        )

    def _difference(self, left: str, right: str) -> float:
        """How much two frames differ, on their words."""
        if not right:
            return 0.0
        if not left:
            return 1.0
        left_words = set(left.split())
        right_words = set(right.split())
        union = left_words | right_words
        if not union:
            return 0.0
        return len(union - (left_words & right_words)) / len(union)

    def _reading(
        self, reference, state, transcript, frames, cues, frame_count, sampled_from, reason
    ) -> LectureReading:
        return LectureReading(
            reference=reference,
            state=state,
            transcript=transcript,
            frame_texts=frames,
            cue_count=cues,
            frame_count=frame_count,
            frames_sampled_from=sampled_from,
            reason=reason,
            read_at_ns=self._now_ns(),
        )


def describe_lecture_reading(reader: VideoLectureReader) -> dict:
    return {
        "part_id": PART_ID,
        "reader_is_installed": reader._reader is not None,
        "lectures_read": reader.standing.lectures_read,
        "complete": reader.standing.complete,
        "transcript_only": reader.standing.transcript_only,
        "frames_only": reader.standing.frames_only,
        "nothing_retrieved": reader.standing.nothing_retrieved,
        "failures": reader.standing.failures,
        "cues_read": reader.standing.cues_read,
        "frames_kept": reader.standing.frames_kept,
        "frames_sampled_from": reader.standing.frames_sampled_from,
        "infers_from_title_or_description": False,
    }


def run_video_lecture_reader(
    reader: VideoLectureReader, control_socket, read_references, publish_documents,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_documents(tuple(reader.read(reference) for reference in read_references(reader)))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    A web idea whose source is a video is a lecture to read. No reader is
    installed on this box, so each is answered NO_READER by name and no
    document is published; `install_reader` is the one way one gets in.
    """
    from runtime.input_assembly import Batch

    ideas = Batch(read=context.bus.reader("web-idea"))
    publish_documents = context.bus.publisher_for("source-document")
    reader = VideoLectureReader(
        maximum_frames=int(context.number("video_maximum_frames")),
        frame_change_threshold=context.number("video_frame_change_threshold"),
    )
    video_hosts = ("youtube.com", "youtu.be", "vimeo.com")

    def read_references(_reader):
        return tuple(
            str(idea.source_url) for idea in ideas.payloads()
            if idea.source_kind == "video" or any(host in str(idea.source_url) for host in video_hosts)
        )

    def publish(readings) -> None:
        documents = tuple(reading for reading in readings if reading is not None and reading.is_a_document)
        if documents:
            publish_documents(documents)

    return run_video_lecture_reader(
        reader=reader,
        control_socket=context.control_socket,
        read_references=read_references,
        publish_documents=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
