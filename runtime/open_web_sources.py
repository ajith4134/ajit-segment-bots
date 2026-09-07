"""The real network calls the three research readers were built to be given.

Until 2026-09-07 none of them had one. `open-web-reader`, `book-and-paper-fetcher`
and `arxiv-feed-reader` each counted 380,660 failed fetches out of 380,660 on a
single live session, because `self._fetcher is None` -- and the whole chain behind
them was inert as a result: no `research-finding` and no `skill`, so
`prompt-template-author` received nothing, so `prompt-registry` held no version,
so `prompt-renderer` refused **210,951** real `llm-request`s with
`refused_no_active_version`. Twelve parts, about 2.5 million messages in and zero
out, from one missing function.

**Every source here is keyless on purpose.** A research reader that needs a
secret nobody installed is the same failure one layer along -- it would read as
configured and fetch nothing. arXiv, Crossref and GitHub all answer unauthenticated
from this box (verified 2026-09-07: HTTP 200 to each), and DuckDuckGo is reached
through `ddgs`. GitHub's unauthenticated search allows 10 requests a minute and its
REST API 60 an hour; both readers already own rate limits of their own, and this
module adds none -- the part decides how often to call, which is where that
decision belongs (T-4).

**A failure is returned, never raised into the tick.** Each reader's own docstring
says a failed fetch is a fact it counts; a fetcher that raised would take the part
off the air instead, which is what happened to `arxiv-feed-reader` on 2026-08-25.
So every call here is wrapped and answers with nothing rather than an exception.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree

# Named because every request this module makes should be attributable to this
# project by whoever is serving it. arXiv and Crossref both ask for it in their
# published etiquette, and Crossref gives a faster pool to requests that carry a
# contact. A generic client string is what gets a shared IP blocked for someone
# else's behaviour.
USER_AGENT = "ajit-segment-bots/1.0 (research reader; +https://github.com/ajith4134)"

ARXIV_ENDPOINT = "https://export.arxiv.org/api/query"
CROSSREF_ENDPOINT = "https://api.crossref.org/works"
GITHUB_README = "https://api.github.com/repos/{owner}/{repo}/readme"

ATOM = "{http://www.w3.org/2005/Atom}"
# arXiv states the version in the identifier's own tail: ".../2401.01234v3".
ARXIV_VERSION = re.compile(r"v(\d+)$")
TAG = re.compile(r"<[^>]+>")
WHITESPACE = re.compile(r"\s+")


def _read(url: str, timeout: float, accept: str = "*/*") -> bytes | None:
    """One GET, or None. Never raises: see the module docstring."""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": accept}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None


def readable_text(html: bytes | str, limit: int) -> str:
    """The visible words of a page, with script and style thrown away first.

    Deliberately not a parser. What the readers do with this is count words and
    look for condition lines, and a tag-stripper is enough for that; a real HTML
    parse would be a second dependency for no gain in what is actually read.
    """
    text = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
    for block in ("script", "style", "noscript"):
        text = re.sub(rf"<{block}\b.*?</{block}>", " ", text, flags=re.S | re.I)
    text = TAG.sub(" ", text)
    text = (
        text.replace("&nbsp;", " ").replace("&amp;", "&")
        .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    )
    return WHITESPACE.sub(" ", text).strip()[:limit]


def github_repository_in(url: str) -> tuple[str, str] | None:
    """(owner, repo) for a github.com URL, or None.

    `github-strategy-miner` reads condition lines out of a web idea's content and
    only looks at ideas whose source is a repository, so a GitHub result whose
    content is a search snippet gives it nothing to mine. The README is what
    actually carries the strategy.
    """
    parts = urllib.parse.urlparse(url)
    if parts.netloc.lower() not in ("github.com", "www.github.com"):
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) < 2:
        return None
    return segments[0], segments[1]


def web_search_fetcher(search=None, timeout: float = 20.0, content_limit: int = 20_000):
    """`open-web-reader`'s fetcher: `query -> (title, content, source_url, kind)`.

    The search gives the links; the content is the page itself, fetched and
    stripped. A snippet would be cheaper and would defeat the reader's purpose --
    `github-strategy-miner` looks for lines naming a condition, and no search
    snippet carries one. A GitHub result is answered with its README through
    GitHub's own API rather than by scraping the rendered page.

    `search` is injected so a test can run without the network; the default is
    DuckDuckGo through `ddgs`.
    """
    def default_search(query: str, maximum: int):
        from ddgs import DDGS

        return list(DDGS().text(query, max_results=maximum))

    finder = search or default_search

    def fetch(query: str, maximum: int = 5):
        try:
            results = finder(query, maximum)
        except Exception:
            return ()
        out = []
        for result in results or ():
            url = str(result.get("href") or result.get("url") or "")
            title = str(result.get("title") or "").strip()
            if not url or not title:
                continue
            repository = github_repository_in(url)
            if repository is not None:
                owner, repo = repository
                raw = _read(
                    GITHUB_README.format(owner=owner, repo=repo), timeout,
                    accept="application/vnd.github.raw+json",
                )
                content = readable_text(raw, content_limit) if raw else ""
                kind = "repository"
            else:
                page = _read(url, timeout, accept="text/html")
                content = readable_text(page, content_limit) if page else ""
                kind = "web-page"
            if not content:
                # The snippet is what the search engine already told us, and it is
                # a real reading even when the page itself refused us. Empty is
                # reported as empty rather than dropped, so a page that says
                # nothing is distinguishable from one that was never reached.
                content = str(result.get("body") or "").strip()
            out.append((title, content, url, kind))
        return tuple(out)

    return fetch


def arxiv_search(timeout: float = 20.0, maximum: int = 8):
    """`arxiv-feed-reader`'s search: `query -> rows`.

    Each row is what that reader reads off it: `identifier`, `version`, `title`,
    `abstract`, `is_withdrawn` and `source_reference`. The version is parsed from
    arXiv's own identifier tail rather than invented, because the reader uses it
    to decide whether a paper it already holds has been revised.
    """
    def search(query: str):
        url = f"{ARXIV_ENDPOINT}?" + urllib.parse.urlencode(
            # **Relevance, not recency.** Sorted by submittedDate this returned
            # "Two extremely irradiated volatile-rich sub-Neptunes" for a query of
            # "options implied volatility" -- arXiv's `all:` matched "volatile"
            # and the newest paper in all of arXiv won. A reader answering a skill
            # gap with the newest astronomy preprint is decoration of exactly the
            # kind this project's third goal is about.
            {"search_query": f"all:{query}", "max_results": maximum,
             "sortBy": "relevance", "sortOrder": "descending"}
        )
        body = _read(url, timeout, accept="application/atom+xml")
        if not body:
            return ()
        try:
            feed = ElementTree.fromstring(body)
        except ElementTree.ParseError:
            return ()
        rows = []
        for entry in feed.findall(f"{ATOM}entry"):
            identifier = (entry.findtext(f"{ATOM}id") or "").strip()
            if not identifier:
                continue
            title = WHITESPACE.sub(" ", (entry.findtext(f"{ATOM}title") or "").strip())
            abstract = WHITESPACE.sub(" ", (entry.findtext(f"{ATOM}summary") or "").strip())
            match = ARXIV_VERSION.search(identifier)
            comment = entry.findtext("{http://arxiv.org/schemas/atom}comment") or ""
            rows.append({
                "identifier": identifier.rsplit("/", 1)[-1].split("v")[0],
                "version": int(match.group(1)) if match else 1,
                "title": title,
                # The same text under both names on purpose: `fetch_for` builds
                # its SourceDocument from `content` while `_best_match` scores
                # word overlap against `abstract`. Carrying one and not the other
                # crash-looped the reader on the first gap it was ever given
                # (KeyError: 'content', 2026-09-07).
                "abstract": abstract,
                "content": abstract,
                # arXiv has no withdrawal flag; a withdrawn paper says so in its
                # comment, which is the only signal the site itself gives.
                "is_withdrawn": "withdrawn" in comment.lower(),
                "source_reference": identifier,
            })
        return tuple(rows)

    return search


def paper_fetcher(timeout: float = 20.0):
    """`book-and-paper-fetcher`'s fetcher.

    `query -> (title, content, source_reference, kind, host)`. Crossref first,
    because it indexes the published literature including books; arXiv second,
    for the preprints Crossref does not carry. Content is the abstract where one
    is published and `None` where it is not -- that reader treats content of None
    with a reference as a paywall, which is exactly what a Crossref record with no
    abstract is: the work exists, its text is not free.
    """
    arxiv = arxiv_search(timeout=timeout, maximum=1)

    def fetch(query: str):
        url = f"{CROSSREF_ENDPOINT}?" + urllib.parse.urlencode(
            {"query": query, "rows": 1, "select": "title,abstract,DOI,type,container-title"}
        )
        body = _read(url, timeout, accept="application/json")
        if body:
            try:
                items = json.loads(body)["message"]["items"]
            except (ValueError, KeyError, TypeError):
                items = []
            if items:
                item = items[0]
                titles = item.get("title") or []
                abstract = item.get("abstract")
                return (
                    WHITESPACE.sub(" ", titles[0]).strip() if titles else query,
                    readable_text(abstract, 20_000) if abstract else None,
                    f"https://doi.org/{item.get('DOI')}" if item.get("DOI") else query,
                    str(item.get("type") or "journal-article"),
                    "api.crossref.org",
                )
        rows = arxiv(query)
        if rows:
            row = rows[0]
            return (row["title"], row["abstract"], row["source_reference"],
                    "preprint", "export.arxiv.org")
        raise LookupError(f"no source answered for {query!r}")

    return fetch


__all__ = [
    "arxiv_search",
    "github_repository_in",
    "paper_fetcher",
    "readable_text",
    "web_search_fetcher",
]
