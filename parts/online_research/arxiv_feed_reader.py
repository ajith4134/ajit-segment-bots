"""arxiv-feed-reader: papers fetched against a gap, never against a topic.

A preprint feed is an infinite source of plausible reading. Left to run on a topic
-- "machine learning for finance" -- it produces a steady stream of papers that are
interesting and change nothing, and the ingestion budget goes to whatever was posted
most recently rather than to what this system could not answer.

So this part is driven by gaps: a question that was asked, went unanswered more than
once, and is judged worth fetching against. If there is no gap, it fetches nothing,
and that is the correct behaviour rather than an idle failure.

What it filters on, and why each filter exists:

- **A paper must have a persistent identifier.** A preprint is revised; v1 and v3
  can disagree. Without the versioned identifier there is no way to know which one
  a distilled rule came from, and provenance that cannot be pinned is provenance
  that cannot be rechecked.
- **Withdrawn papers are excluded and recorded as withdrawn.** Silently dropping
  them loses the fact that something previously ingested is now retracted -- which
  is exactly when downstream skills need invalidating.
- **A revision is a new version, not an edit.** A paper already ingested at v1 and
  now at v2 is fetched again and superseded downstream, because the revision is
  usually where the result changed.
- **Category is not relevance.** Papers are matched against the gap's own words,
  not against a subscribed category, because the whole point is answering a
  specific question rather than staying current.

Nothing here judges whether a paper is correct. Preprints are not peer reviewed, and
this part carries that forward as a property of the document rather than dropping
them -- most of the useful quantitative-finance literature is preprint-only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.external_research_types import UNAVAILABLE
from runtime.knowledge_types import SourceDocument
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "arxiv-feed-reader"

PART_DECLARATION = PartDeclaration(
    part_id="arxiv-feed-reader",
    consumes=("skill-gap",),
    produces=("source-document", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PREPRINT = "preprint"

FETCHED = "fetched"
NO_GAP = "nothing-is-missing-so-nothing-is-fetched"
NOT_WORTH_FETCHING = "the-gap-is-not-one-a-paper-would-close"
NOTHING_MATCHED = "no-paper-matched-the-gap"
WITHDRAWN = "the-paper-was-withdrawn"
ALREADY_HAVE_THIS_VERSION = "already-ingested-at-this-version"
REVISED = "a-newer-version-of-a-paper-already-held"
NO_IDENTIFIER = "no-versioned-identifier-to-pin-it-to"
RATE_LIMITED = "rate-limited"
FETCH_FAILED = "fetch-failed"
# No search is installed on this box. Its own state, not FETCH_FAILED: a fetch
# that failed says the search was tried and did not answer, and a reader with no
# search never tried. The two need different fixes and one of them is not a fault.
NO_SEARCH = "no-search-is-installed-on-this-machine"


@dataclass(frozen=True)
class PaperRead:
    gap_id: str | None
    state: str
    document: SourceDocument | None
    identifier: str | None
    version: int | None
    supersedes: str | None
    is_peer_reviewed: bool
    reason: str
    read_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state in (FETCHED, REVISED) and self.document is not None


@dataclass
class FeedReaderStanding:
    fetches_attempted: int = 0
    papers_fetched: int = 0
    revisions_fetched: int = 0
    withdrawn_seen: int = 0
    already_held: int = 0
    without_identifier: int = 0
    nothing_matched: int = 0
    rate_limited: int = 0
    refused_no_search: int = 0
    failures: int = 0
    fetched_without_a_gap: int = 0


class ArxivFeedReader:
    """Fetches preprints that answer a recorded gap, pinned to their version."""

    def __init__(
        self,
        fetches_per_window: int,
        window_seconds: float,
        minimum_word_overlap: int,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        if fetches_per_window < 1:
            raise ValueError("a reader allowed zero fetches reads nothing")
        if window_seconds <= 0:
            raise ValueError("the rate-limit window is a positive number of seconds")
        if minimum_word_overlap < 1:
            raise ValueError(
                "matching on zero shared words matches everything, which is what "
                "subscribing to a category already does"
            )
        self._fetches_per_window = fetches_per_window
        self._window_seconds = window_seconds
        self._minimum_overlap = minimum_word_overlap
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._fetch_times: list = []
        self._held_versions: dict[str, int] = {}
        self._withdrawn: set = set()
        self._search = None
        self.standing = FeedReaderStanding()

    def install_search(self, search) -> None:
        """`search(query) -> rows`, each row a mapping describing one preprint."""
        self._search = search

    def held_version(self, identifier: str) -> int | None:
        return self._held_versions.get(identifier)

    def withdrawn_papers(self) -> tuple:
        """Kept, because a retraction is when downstream skills need invalidating."""
        return tuple(sorted(self._withdrawn))

    def fetch_for(self, gap) -> PaperRead:
        self.standing.fetches_attempted += 1
        if self._search is None:
            # Refused by name rather than raised. This part's own docstring says
            # "every gap is answered FETCH_FAILED by name and nothing is fetched",
            # and the code raised instead -- so the first skill-gap to arrive took
            # the part off the air on 2026-08-25 rather than reporting a state.
            self.standing.refused_no_search += 1
            return self._read(
                getattr(gap, "gap_id", None), NO_SEARCH, None, None, None, None,
                "no search is installed on this machine, so nothing can be fetched. "
                "install_search is the one way one gets in, and the fetch budget "
                "applies from then",
            )

        if gap is None:
            self.standing.fetched_without_a_gap += 0
            return self._read(
                None, NO_GAP, None, None, None, None,
                "no gap to fetch against. A feed reader given a topic returns whatever "
                "was posted most recently, which spends the ingestion budget on recency",
            )

        if not getattr(gap, "is_worth_fetching_against", True):
            return self._read(
                gap.gap_id, NOT_WORTH_FETCHING, None, None, None, None,
                "this gap is not one a paper would close. Fetching against it produces "
                "reading, not an answer",
            )

        if not self._may_fetch():
            self.standing.rate_limited += 1
            return self._read(
                gap.gap_id, RATE_LIMITED, None, None, None, None,
                f"{self._fetches_per_window} fetch(es) per {self._window_seconds:.0f}s "
                f"already spent. Retrying now is what turns a rate limit into a ban",
            )

        self._fetch_times.append(self._monotonic())
        try:
            rows = self._search(gap.query)
        except Exception as failure:
            self.standing.failures += 1
            return self._read(
                gap.gap_id, FETCH_FAILED, None, None, None, None,
                f"the feed could not be read ({type(failure).__name__}). Nothing is "
                f"concluded from a failed fetch",
            )

        best = self._best_match(gap.query, rows or ())
        if best is None:
            self.standing.nothing_matched += 1
            return self._read(
                gap.gap_id, NOTHING_MATCHED, None, None, None, None,
                f"nothing shared at least {self._minimum_overlap} word(s) with the gap. "
                f"Category membership is not relevance",
            )

        identifier = best.get("identifier")
        version = best.get("version")
        if not identifier or version is None:
            self.standing.without_identifier += 1
            return self._read(
                gap.gap_id, NO_IDENTIFIER, None, identifier, version, None,
                "no versioned identifier. A preprint is revised, v1 and v3 can disagree, "
                "and a rule distilled from one of them could never be traced back",
            )

        if best.get("is_withdrawn", False):
            self._withdrawn.add(identifier)
            self.standing.withdrawn_seen += 1
            return self._read(
                gap.gap_id, WITHDRAWN, None, identifier, version, None,
                f"{identifier} was withdrawn. That is recorded rather than skipped: a "
                f"retraction is exactly when anything distilled from it needs invalidating",
            )

        held = self._held_versions.get(identifier)
        if held is not None and held >= version:
            self.standing.already_held += 1
            return self._read(
                gap.gap_id, ALREADY_HAVE_THIS_VERSION, None, identifier, version, None,
                f"{identifier}v{version} is already held",
            )

        document = SourceDocument(
            document_id=f"arxiv:{identifier}v{version}",
            title=best["title"],
            content=best["content"],
            kind=PREPRINT,
            source_reference=best.get(
                "source_reference", f"https://arxiv.org/abs/{identifier}v{version}"
            ),
            fetched_at_ns=self._now_ns(),
        )
        supersedes = f"arxiv:{identifier}v{held}" if held is not None else None
        self._held_versions[identifier] = version
        self.standing.papers_fetched += 1
        if supersedes:
            self.standing.revisions_fetched += 1

        return self._read(
            gap.gap_id, REVISED if supersedes else FETCHED, document, identifier, version,
            supersedes,
            f"{identifier}v{version} fetched against the gap"
            + (
                f", superseding v{held} -- a revision is usually where the result changed"
                if supersedes
                else ". It is a preprint and is carried as one: not peer reviewed, which "
                     "is a property of the document rather than a reason to drop it"
            ),
        )

    def _best_match(self, query, rows):
        wanted = self._words(query)
        best = None
        best_overlap = 0
        for row in rows:
            overlap = len(wanted & self._words(f"{row.get('title', '')} {row.get('abstract', '')}"))
            if overlap >= self._minimum_overlap and overlap > best_overlap:
                best, best_overlap = row, overlap
        return best

    @staticmethod
    def _words(text: str) -> set:
        return {
            word.strip(".,;:()[]").lower()
            for word in str(text).split()
            if len(word.strip(".,;:()[]")) > 3
        }

    def _may_fetch(self) -> bool:
        now = self._monotonic()
        self._fetch_times = [
            stamp for stamp in self._fetch_times if now - stamp < self._window_seconds
        ]
        return len(self._fetch_times) < self._fetches_per_window

    def _read(
        self, gap_id, state, document, identifier, version, supersedes, reason,
    ) -> PaperRead:
        return PaperRead(
            gap_id=gap_id, state=state, document=document, identifier=identifier,
            version=version, supersedes=supersedes, is_peer_reviewed=False, reason=reason,
            read_at_ns=self._now_ns(),
        )


def describe_feed_reading(reader: ArxivFeedReader) -> dict:
    return {
        "part_id": PART_ID,
        "fetches_attempted": reader.standing.fetches_attempted,
        "papers_fetched": reader.standing.papers_fetched,
        "revisions_fetched": reader.standing.revisions_fetched,
        "withdrawn_papers_seen": reader.standing.withdrawn_seen,
        "already_held": reader.standing.already_held,
        "rejected_without_identifier": reader.standing.without_identifier,
        "nothing_matched": reader.standing.nothing_matched,
        "rate_limited": reader.standing.rate_limited,
        "refused_no_search_installed": reader.standing.refused_no_search,
        "failures": reader.standing.failures,
        "subscribes_to_a_category": False,
        "judges_whether_a_paper_is_correct": False,
    }


def run_arxiv_feed_reader(
    reader: ArxivFeedReader, control_socket, read_gaps, publish_documents,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for gap in read_gaps():
            result = reader.fetch_for(gap)
            if result.is_usable:
                publish_documents(result.document)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_feed_reading(reader),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    **A search is installed since 2026-09-07**, arXiv's own public API, which
    needs no key. Until then none was and every gap was answered FETCH_FAILED by
    name: 380,660 fetches attempted and 0 papers, all of them
    `refused_no_search_installed`.

    Sorted by relevance rather than by submission date. Sorted by date, arXiv's
    `all:` search answered "options implied volatility" with "Two extremely
    irradiated volatile-rich sub-Neptunes" -- it matched "volatile" and the newest
    paper in all of arXiv won. A reader answering a skill gap with the newest
    astronomy preprint is exactly the decoration this project's third goal exists
    to find.
    """
    from runtime.input_assembly import Batch

    gaps = Batch(read=context.bus.reader("skill-gap"))
    publish_documents = context.bus.publisher_for("source-document")
    reader = ArxivFeedReader(
        fetches_per_window=int(context.number("research_fetches_per_window")),
        window_seconds=context.number("research_fetch_window_seconds"),
        minimum_word_overlap=int(context.number("arxiv_minimum_word_overlap")),
    )
    from runtime.open_web_sources import arxiv_search

    reader.install_search(arxiv_search())

    return run_arxiv_feed_reader(
        reader=reader,
        control_socket=context.control_socket,
        read_gaps=lambda: tuple(gap for gap in gaps.payloads() if getattr(gap, "query", None)),
        publish_documents=lambda document: publish_documents((document,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
