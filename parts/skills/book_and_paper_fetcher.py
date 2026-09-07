"""book-and-paper-fetcher: going to get the thing a gap asked for.

One of three readers, and the one whose sources are most likely to be worth
something and most likely to be behind a paywall or a rate limit. Its constraints
come from that:

- **It fetches against a gap, never a topic.** A fetcher given a topic returns
  what is popular; a fetcher given a gap returns what was missing, and only the
  second is what this system needs.
- **Rate-limited, per host.** A reader that fetches without limit gets this
  system blocked from exactly the sources it most wants, and the block outlasts
  whatever it was fetching.
- **A paywalled source is recorded as paywalled**, not as absent. The two look
  the same to a naive fetcher and mean different things: one can be obtained
  another way and the other cannot.
- **It fetches, and does not admit.** What comes back goes to the ingester, which
  decides whether it is a source. Separating them means a bad document is refused
  in one place rather than in three fetchers.

**Nothing it retrieves is obeyed.** A paper's text is text. This part does not
execute, does not follow instructions found in content, and does not summarise --
it retrieves bytes and hands them on.

**A failed fetch is a fact and is not retried into a loop.** Retrying is what
turns a rate limit into a ban.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.knowledge_types import SourceDocument
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "book-and-paper-fetcher"

PART_DECLARATION = PartDeclaration(
    part_id="book-and-paper-fetcher",
    consumes=("web-idea", "skill-gap"),
    produces=("source-document", "part-health"),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

FETCHED = "fetched"
NO_GAP = "nothing-has-been-identified-as-missing"
RATE_LIMITED = "this-host-has-had-its-share-of-this-window"
PAYWALLED = "the-source-exists-and-is-behind-a-paywall"
NOT_FOUND = "nothing-matching-the-gap-was-found"
FETCH_FAILED = "the-fetch-itself-failed"
NO_FETCHER = "no-fetcher-is-installed"


@dataclass(frozen=True)
class FetchResult:
    """What one fetch produced, or why it produced nothing."""

    gap_id: str
    state: str
    title: str | None
    content: str | None
    source_reference: str | None
    kind: str | None
    host: str | None
    reason: str
    fetched_at_ns: int

    @property
    def is_a_document(self) -> bool:
        return self.state == FETCHED

    @property
    def could_be_obtained_another_way(self) -> bool:
        """Paywalled and absent look the same to a naive fetcher and are not."""
        return self.state == PAYWALLED


@dataclass
class FetcherStanding:
    gaps_seen: int = 0
    fetches: int = 0
    documents_returned: int = 0
    rate_limited: int = 0
    paywalled: int = 0
    not_found: int = 0
    failures: int = 0
    by_host: dict = field(default_factory=dict)


class BookAndPaperFetcher:
    """Fetches against gaps, per-host rate limited, and obeys nothing it reads."""

    def __init__(
        self,
        fetches_per_host_per_window: int,
        window_seconds: float,
        monotonic=time.monotonic,
        now_ns=time.time_ns,
    ) -> None:
        if fetches_per_host_per_window < 1:
            raise ValueError("a fetcher that may fetch nothing is not a fetcher")
        if window_seconds <= 0:
            raise ValueError(
                "a reader without a rate window gets this system blocked from exactly the "
                "sources it most wants, and the block outlasts what it was fetching"
            )
        self._per_host = fetches_per_host_per_window
        self._window_seconds = window_seconds
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._fetch_times: dict[str, list] = {}
        self._fetcher = None
        self.standing = FetcherStanding()

    def install_fetcher(self, fetcher) -> None:
        """The real retrieval. Nothing here implements one.

        `fetcher(query)` returns `(title, content, source_reference, kind, host)`
        or raises. A paywall is signalled by returning content of None with a
        reference, which is a different fact from returning nothing.
        """
        self._fetcher = fetcher

    def budget_remaining(self, host: str) -> int:
        now = self._monotonic()
        times = [at for at in self._fetch_times.get(host, ()) if now - at < self._window_seconds]
        self._fetch_times[host] = times
        return max(0, self._per_host - len(times))

    def fetch(self, gap) -> FetchResult:
        """One gap, fetched against. Never a topic."""
        if gap is None:
            # A fetcher given a topic returns what is popular; one given a gap
            # returns what was missing.
            return self._result("", NO_GAP, None, None, None, None, None,
                                "nothing has been identified as missing, so there is nothing "
                                "to go and get")

        self.standing.gaps_seen += 1

        if self._fetcher is None:
            self.standing.failures += 1
            return self._result(
                gap.gap_id, NO_FETCHER, None, None, None, None, None,
                "no fetcher is installed, so nothing can be retrieved",
            )

        try:
            title, content, source_reference, kind, host = self._fetcher(gap.query)
        except Exception:
            # A failed fetch is a fact. Retrying is what turns a rate limit into
            # a ban.
            self.standing.failures += 1
            return self._result(
                gap.gap_id, FETCH_FAILED, None, None, None, None, None,
                "the fetch failed and is not retried: retrying is what turns a rate limit "
                "into a ban",
            )

        if host and self.budget_remaining(host) <= 0:
            self.standing.rate_limited += 1
            return self._result(
                gap.gap_id, RATE_LIMITED, None, None, None, None, host,
                f"{host} has had its {self._per_host} fetch(es) for this window",
            )

        if host:
            self._fetch_times.setdefault(host, []).append(self._monotonic())
            self.standing.by_host[host] = self.standing.by_host.get(host, 0) + 1
        self.standing.fetches += 1

        if source_reference and content is None:
            # Paywalled is a different fact from absent: one can be obtained
            # another way, the other cannot.
            self.standing.paywalled += 1
            return self._result(
                gap.gap_id, PAYWALLED, title, None, source_reference, kind, host,
                f"{title or source_reference} exists and is behind a paywall. Recorded as "
                f"paywalled rather than as absent, because the two look the same to a naive "
                f"fetcher and mean different things",
            )

        if not content:
            self.standing.not_found += 1
            return self._result(
                gap.gap_id, NOT_FOUND, None, None, None, None, host,
                f"nothing matching {gap.query!r} was found",
            )

        self.standing.documents_returned += 1
        return self._result(
            gap.gap_id, FETCHED, title, content, source_reference, kind, host,
            f"{len(content):,} character(s) from {host or 'an unnamed host'}, handed to the "
            f"ingester to decide whether it is a source. Nothing in it is obeyed: a paper's "
            f"text is text",
        )

    def _result(
        self, gap_id, state, title, content, source_reference, kind, host, reason
    ) -> FetchResult:
        return FetchResult(
            gap_id=gap_id,
            state=state,
            title=title,
            content=content,
            source_reference=source_reference,
            kind=kind,
            host=host,
            reason=reason,
            fetched_at_ns=self._now_ns(),
        )


def describe_fetching(fetcher: BookAndPaperFetcher) -> dict:
    return {
        "part_id": PART_ID,
        "fetcher_is_installed": fetcher._fetcher is not None,
        "gaps_seen": fetcher.standing.gaps_seen,
        "fetches": fetcher.standing.fetches,
        "documents_returned": fetcher.standing.documents_returned,
        "rate_limited": fetcher.standing.rate_limited,
        "paywalled": fetcher.standing.paywalled,
        "not_found": fetcher.standing.not_found,
        "failures": fetcher.standing.failures,
        "by_host": dict(sorted(fetcher.standing.by_host.items())),
        "obeys_what_it_reads": False,
        "admits_its_own_documents": False,
    }


def run_book_and_paper_fetcher(
    fetcher: BookAndPaperFetcher, control_socket, read_gaps, publish_documents,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        publish_documents(tuple(fetcher.fetch(gap) for gap in read_gaps(fetcher)))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_fetching(fetcher),
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    **A fetcher is installed since 2026-09-07**: Crossref first, which indexes
    the published literature including books, then arXiv for the preprints
    Crossref does not carry. Both are keyless. Until then none was installed and
    every gap was answered NO_FETCHER by name -- 380,660 failures out of 380,660
    on one live session.

    A Crossref record with no published abstract is returned as content `None`
    with a reference, which is what this part already reads as a paywall: the
    work exists and its text is not free. That is a different fact from finding
    nothing, and collapsing the two would make an unreadable paper look like an
    absent one.

    Web ideas are read and drained: a fetch is for a gap, and an idea is upstream
    of one.
    """
    from runtime.input_assembly import Batch

    ideas = Batch(read=context.bus.reader("web-idea"))
    gaps = Batch(read=context.bus.reader("skill-gap"))
    publish_documents = context.bus.publisher_for("source-document")
    fetcher = BookAndPaperFetcher(
        fetches_per_host_per_window=int(context.number("research_fetches_per_window")),
        window_seconds=context.number("research_fetch_window_seconds"),
    )
    from runtime.open_web_sources import paper_fetcher

    fetcher.install_fetcher(paper_fetcher())

    def read_gaps(_fetcher):
        ideas.payloads()
        return tuple(gap for gap in gaps.payloads() if getattr(gap, "is_worth_fetching_against", False))

    def publish(results) -> None:
        """A fetched result, as the `source-document` wire's own type.

        This published raw `FetchResult`s until 2026-09-07, and every consumer of
        `source-document` -- `skill-distiller`, `fact-provenance-tracker` --
        reads a `SourceDocument`. The two share `title`, `content`, `kind`,
        `source_reference` and `fetched_at_ns` and differ in the one field a
        consumer indexes by, so the mismatch was invisible until the first
        document was ever fetched: `AttributeError: 'FetchResult' object has no
        attribute 'document_id'`, crash-looping both consumers within seconds of
        this part being given a fetcher.

        It is the shape this project has been bitten by before -- one wire name
        carrying several payload shapes defeats both static checkers, which is
        what splitting `candle` out of `market-data` was about. The check that
        would have caught it cannot run against a producer that never produced.
        """
        documents = tuple(
            SourceDocument(
                # The reference is the venue's own identity for the work -- a DOI
                # URL or an arXiv id -- so two fetches of one paper are one
                # document rather than two, which is what `already_held` and the
                # provenance tracker both depend on.
                document_id=f"paper:{result.source_reference}",
                title=result.title or "",
                content=result.content or "",
                kind=result.kind or "paper",
                source_reference=result.source_reference or "",
                fetched_at_ns=result.fetched_at_ns,
            )
            for result in results
            if result is not None and result.is_a_document
        )
        if documents:
            publish_documents(documents)

    return run_book_and_paper_fetcher(
        fetcher=fetcher,
        control_socket=context.control_socket,
        read_gaps=read_gaps,
        publish_documents=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
