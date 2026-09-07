"""The research sources, against the real services they call.

RL-063: a fixture is what makes a hollow part look healthy, and these three
readers spent a full live session counting 380,660 failed fetches each while
every contract checker was green. So the tests that matter here go to the real
endpoints. They are marked `network` so a run without egress can deselect them,
and the shape-only tests beside them need no network at all.

    .venv/bin/python -m pytest tests/runtime/test_open_web_sources.py
    .venv/bin/python -m pytest tests/runtime/test_open_web_sources.py -m "not network"
"""

from __future__ import annotations

import pytest

from runtime.open_web_sources import (
    arxiv_search, github_repository_in, paper_fetcher, readable_text,
    web_search_fetcher,
)


def test_readable_text_drops_script_and_style_before_tags():
    """A page's scripts are not its words. Stripping tags without removing
    script bodies first leaves minified JavaScript in what a reader counts as
    the content of a page."""
    html = "<html><style>.a{color:red}</style><script>var x=1;</script><p>Real words here</p></html>"
    assert readable_text(html, 500) == "Real words here"


def test_readable_text_is_bounded():
    assert len(readable_text("<p>" + "word " * 10_000 + "</p>", 100)) <= 100


def test_a_github_url_is_resolved_to_its_repository():
    assert github_repository_in("https://github.com/kernc/backtesting.py") == ("kernc", "backtesting.py")
    assert github_repository_in("https://github.com/kernc/backtesting.py/blob/main/a.py") == ("kernc", "backtesting.py")
    # Not a repository: the site itself, and a user page with no repo.
    assert github_repository_in("https://github.com/kernc") is None
    assert github_repository_in("https://example.com/kernc/repo") is None


def test_a_search_that_raises_answers_with_nothing_rather_than_propagating():
    """Every reader here counts a failed fetch as a fact. A fetcher that raised
    would take the part off the air instead -- which is what happened to
    arxiv-feed-reader on 2026-08-25, when the first skill-gap to arrive killed
    it."""
    def explode(query, maximum):
        raise RuntimeError("the network is gone")

    assert web_search_fetcher(search=explode)("anything") == ()


def test_a_result_with_no_url_or_no_title_is_skipped():
    def search(query, maximum):
        return [{"href": "", "title": "no url"}, {"href": "https://x.test", "title": ""}]

    assert web_search_fetcher(search=search)("anything") == ()


@pytest.mark.network
def test_arxiv_answers_an_options_query_with_options_papers():
    """Sorted by relevance, not by date. Sorted by date this returned "Two
    extremely irradiated volatile-rich sub-Neptunes" for this exact query --
    arXiv's `all:` matched "volatile" and the newest paper in all of arXiv won."""
    rows = arxiv_search()("options implied volatility")

    assert rows, "arXiv returned nothing"
    for row in rows:
        assert row["identifier"] and row["source_reference"]
        assert isinstance(row["version"], int) and row["version"] >= 1
        assert isinstance(row["is_withdrawn"], bool)
    words = " ".join(f"{r['title']} {r['abstract']}" for r in rows).lower()
    assert "option" in words or "volatility" in words


@pytest.mark.network
def test_crossref_answers_with_a_real_work():
    title, content, reference, kind, host = paper_fetcher()("volatility risk premium index options")

    assert title and reference
    assert host in ("api.crossref.org", "export.arxiv.org")
    # content is None for a work whose abstract is not published -- a paywall,
    # which this part reads as a different fact from finding nothing.
    assert content is None or len(content) > 0
    assert kind


@pytest.mark.network
def test_a_github_result_is_answered_with_its_readme_not_a_snippet():
    """`github-strategy-miner` mines condition lines out of this content, and no
    search snippet carries one."""
    results = web_search_fetcher()("github backtesting.py trading strategy python", maximum=4)

    assert results, "the search returned nothing"
    repositories = [row for row in results if row[3] == "repository"]
    assert repositories, "no repository among the results"
    title, content, url, kind = repositories[0]
    # A README is substantially longer than a search snippet, which is the whole
    # reason this path exists.
    assert len(content) > 500, f"only {len(content)} chars, that is a snippet"
    assert "github.com" in url


@pytest.mark.network
def test_a_crossref_record_with_no_abstract_falls_through_to_arxiv():
    """A record with no text is a worse answer than a preprint with text.

    Measured on the live spine 2026-09-07: ten of ten Crossref answers carried no
    abstract, so `book-and-paper-fetcher` counted `paywalled` 10 of 10 and
    returned zero documents -- the fetcher was installed, every counter said it
    was working, and nothing came out. A paywall is the honest state only when
    nothing free has the words.
    """
    fetch = paper_fetcher()
    answers = [
        fetch("volatility risk premium index options"),
        fetch("mean reversion pairs trading cointegration"),
        fetch("order flow imbalance market microstructure"),
    ]

    with_text = [a for a in answers if a[1]]
    assert len(with_text) >= 2, "at least two of three queries should find real text"
    for title, content, reference, kind, host in with_text:
        assert host in ("api.crossref.org", "export.arxiv.org")
        assert len(content) > 100, f"{len(content)} chars is not an abstract"
        assert title and reference
