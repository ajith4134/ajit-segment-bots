"""open-web-reader: going and looking for ideas nobody here has had.

The only part of this system that reaches outside it. Everything else learns from
what this system has done; this reads what other people have written -- papers,
posts, repositories -- and brings back candidate ideas.

It is the part most likely to poison the system, so the constraints are strict:

- **It reads only what a skill gap asked for.** Browsing produces whatever the
  internet is loudest about that week, and a system that ingests that will trade
  the news cycle. A skill gap names something the system has measured itself to
  be missing, and that is the only thing worth going to look for.
- **Everything it returns is a candidate, never a conclusion.** A web idea has no
  standing at all until the hypothesis block has tested it here on this system's
  own data. Nothing downstream may act on one, and its type says so.
- **The source is recorded with the idea.** An idea whose provenance is unknown
  cannot be rechecked, and a system that cannot recheck cannot correct.
- **Content is data, never instruction.** Text from the open web that reads like
  a command is text, and this part strips nothing and obeys nothing -- it packages
  what it read as a quotation with a source attached.

**Rate-limited and bounded.** A reader that could fetch without limit is a way to
get this system blocked, and an idea backlog nobody can test is not an asset.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "open-web-reader"

PART_DECLARATION = PartDeclaration(
    part_id="open-web-reader",
    consumes=("skill-gap",),
    produces=("web-idea", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

READ = "read"
NO_GAP = "nothing-has-been-identified-as-missing"
RATE_LIMITED = "the-fetch-budget-for-this-window-is-spent"
BACKLOG_FULL = "there-are-already-more-untested-ideas-than-can-be-tested"
FETCH_FAILED = "the-source-could-not-be-read"


@dataclass(frozen=True)
class SkillGap:
    """Something this system has measured itself to be missing."""

    gap_id: str
    description: str
    query: str
    measured_from: str
    raised_at_ns: int


@dataclass(frozen=True)
class WebIdea:
    """Something read elsewhere, as a candidate and never as a conclusion.

    `content` is a quotation. This part obeys nothing it reads: text from the
    open web that looks like an instruction is text, and it travels as data with
    its source attached.
    """

    gap_id: str
    title: str
    content: str
    source_url: str
    source_kind: str
    fetched_at_ns: int
    is_a_conclusion: bool = False

    @property
    def may_be_acted_on(self) -> bool:
        """Never. A web idea has no standing until it is tested on this system's data."""
        return False


@dataclass
class ReaderStanding:
    gaps_seen: int = 0
    fetches: int = 0
    ideas_returned: int = 0
    refused_rate_limited: int = 0
    refused_backlog_full: int = 0
    fetch_failures: int = 0
    by_source_kind: dict = field(default_factory=dict)
    untested_backlog: int = 0


class OpenWebReader:
    """Fetches only what a measured gap asked for, and returns quotations."""

    def __init__(
        self,
        fetches_per_window: int,
        window_seconds: float,
        maximum_untested_backlog: int,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        if fetches_per_window < 1:
            raise ValueError("a reader that may fetch nothing is not a reader")
        if window_seconds <= 0:
            raise ValueError(
                "a reader with no rate window is a way to get this system blocked"
            )
        if maximum_untested_backlog < 1:
            raise ValueError(
                "an idea backlog nobody can test is not an asset, so the bound must be real"
            )
        self._fetches_per_window = fetches_per_window
        self._window_seconds = window_seconds
        self._maximum_backlog = maximum_untested_backlog
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._fetch_times: list = []
        self._untested: dict[str, WebIdea] = {}
        self._fetcher = None
        self.standing = ReaderStanding()

    def install_fetcher(self, fetcher) -> None:
        """The real network call. Nothing here implements one.

        `fetcher(query)` returns an iterable of
        `(title, content, source_url, source_kind)`.
        """
        self._fetcher = fetcher

    def mark_tested(self, gap_id: str) -> None:
        """The hypothesis block tested this idea, so it leaves the backlog."""
        self._untested.pop(gap_id, None)
        self.standing.untested_backlog = len(self._untested)

    def budget_remaining(self) -> int:
        now = self._monotonic()
        self._fetch_times = [
            at for at in self._fetch_times if now - at < self._window_seconds
        ]
        return max(0, self._fetches_per_window - len(self._fetch_times))

    def read(self, gap: SkillGap | None) -> tuple[tuple, str]:
        """One skill gap, looked up. Nothing is fetched without one."""
        if gap is None:
            # Browsing returns whatever the internet is loudest about, and a
            # system that ingests that will trade the news cycle.
            return (), NO_GAP

        self.standing.gaps_seen += 1

        if len(self._untested) >= self._maximum_backlog:
            self.standing.refused_backlog_full += 1
            return (), BACKLOG_FULL

        if self.budget_remaining() <= 0:
            self.standing.refused_rate_limited += 1
            return (), RATE_LIMITED

        if self._fetcher is None:
            self.standing.fetch_failures += 1
            return (), FETCH_FAILED

        self._fetch_times.append(self._monotonic())
        self.standing.fetches += 1

        try:
            results = list(self._fetcher(gap.query))
        except Exception:
            # A failed fetch is a fact; it is counted rather than retried into a
            # loop that would get this system blocked.
            self.standing.fetch_failures += 1
            return (), FETCH_FAILED

        ideas = []
        for title, content, source_url, source_kind in results:
            if not source_url:
                # An idea whose provenance is unknown cannot be rechecked, and a
                # system that cannot recheck cannot correct.
                continue
            idea = WebIdea(
                gap_id=gap.gap_id,
                title=title,
                content=content,
                source_url=source_url,
                source_kind=source_kind,
                fetched_at_ns=self._now_ns(),
            )
            ideas.append(idea)
            self._untested[f"{gap.gap_id}:{source_url}"] = idea
            self.standing.by_source_kind[source_kind] = (
                self.standing.by_source_kind.get(source_kind, 0) + 1
            )

        self.standing.ideas_returned += len(ideas)
        self.standing.untested_backlog = len(self._untested)
        return tuple(ideas), READ

    @property
    def untested_ideas(self) -> tuple:
        return tuple(self._untested.values())


def describe_web_reading(reader: OpenWebReader) -> dict:
    return {
        "part_id": PART_ID,
        "fetcher_is_installed": reader._fetcher is not None,
        "gaps_seen": reader.standing.gaps_seen,
        "fetches": reader.standing.fetches,
        "ideas_returned": reader.standing.ideas_returned,
        "refused_rate_limited": reader.standing.refused_rate_limited,
        "refused_backlog_full": reader.standing.refused_backlog_full,
        "fetch_failures": reader.standing.fetch_failures,
        "by_source_kind": dict(sorted(reader.standing.by_source_kind.items())),
        "untested_backlog": reader.standing.untested_backlog,
        "fetch_budget_remaining": reader.budget_remaining(),
        "ideas_may_be_acted_on": False,
    }


def run_open_web_reader(
    reader: OpenWebReader, control_socket, read_skill_gaps, publish_ideas,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        ideas = []
        for gap in read_skill_gaps(reader):
            found, _ = reader.read(gap)
            ideas.extend(found)
        publish_ideas(tuple(ideas))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_web_reading(reader),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    No fetcher is installed on this machine: the box has no outbound web
    client configured for this part, so every gap is answered with
    FETCH_FAILED by name and nothing is published. The budget and the
    backlog bound are real and apply the moment a fetcher is installed
    through `install_fetcher`; a reader that fetched without them would be
    collecting, not learning.
    """
    from runtime.input_assembly import Batch

    gaps = Batch(read=context.bus.reader("skill-gap"))
    publish_ideas = context.bus.publisher_for("web-idea")
    reader = OpenWebReader(
        fetches_per_window=int(context.number("web_fetches_per_window")),
        window_seconds=context.number("web_fetch_window_seconds"),
        maximum_untested_backlog=int(context.number("web_maximum_untested_backlog")),
    )

    def read_skill_gaps(_reader):
        return tuple(gap for gap in gaps.payloads() if getattr(gap, "query", None))

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_ideas(kept)

    return run_open_web_reader(
        reader=reader,
        control_socket=context.control_socket,
        read_skill_gaps=read_skill_gaps,
        publish_ideas=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
