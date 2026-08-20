#!/usr/bin/env python3
"""Render a spec in docs/superpowers/specs/ as a page the user can read in a browser.

The user cannot open files on this server (docs/goal.md, point 6), so any spec
meant to be read has to become a browser link. This turns one markdown spec into
one self-contained HTML page.

The page is generated, never hand-written, and it stamps where it came from: the
source file, its git commit, and the time it was built. A spec page that cannot
name its own provenance is exactly the kind of asserted state Rule 8 forbids.

Usage:
    .venv/bin/python dashboard/render_spec.py docs/superpowers/specs/<name>.md
"""

from __future__ import annotations

import html
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from markdown_it import MarkdownIt

REPO_ROOT = Path(__file__).resolve().parent.parent

# Words that carry a verdict in these specs. They are rendered as badges so a
# reader scanning the page sees the verdict before reading the sentence around
# it. The mapping is explicit rather than heuristic: a keyword only becomes a
# badge when the author wrote it in capitals, which is a deliberate act.
VERDICT_CLASSES = {
    "UNVERIFIED": "caution",
    "NEVER": "withdrawn",
    "MEASURED": "measured",
    "DECLARED": "neutral",
    "IMPLEMENTED": "neutral",
    "TESTED": "measured",
    "RUNNING": "measured",
    "NO DEADLOCK": "measured",
}


def read_git_commit() -> str:
    """Return the short commit the spec is being rendered from, or a plain note."""
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        commit = out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "not in a git working tree"

    dirty = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True, text=True,
    ).stdout.strip()
    return f"{commit} (uncommitted changes present)" if dirty else commit


def extract_title(markdown_text: str) -> str:
    """Take the page name from the spec's first heading."""
    for line in markdown_text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "Specification"


def extract_standfirst(markdown_text: str) -> str:
    """Take the one-line summary from the spec's 'What it decides' entry."""
    match = re.search(r"\*\*What it decides:\*\*\s*(.+?)(?:\n\n|\n\*\*)", markdown_text, re.S)
    if not match:
        return ""
    return " ".join(match.group(1).split())


def collect_sections(markdown_text: str) -> list[tuple[str, str]]:
    """Return (anchor, heading text) for every numbered top-level section."""
    sections: list[tuple[str, str]] = []
    for line in markdown_text.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            sections.append((slugify(heading), heading))
    return sections


def slugify(heading: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
    return slug or "section"


def add_heading_anchors(body_html: str) -> str:
    """Give every h2 an id so the contents list can link to it."""

    def anchor(match: re.Match[str]) -> str:
        inner = match.group(1)
        text = re.sub(r"<[^>]+>", "", inner)
        return f'<h2 id="{slugify(text)}">{inner}</h2>'

    return re.sub(r"<h2>(.*?)</h2>", anchor, body_html, flags=re.S)


def badge_verdicts(body_html: str) -> str:
    """Render capitalised verdict words as badges, longest match first.

    Only words the author marked up — bold or code — become badges. Prose that
    happens to shout is left alone, so a badge always reflects a deliberate act
    rather than a keyword the renderer went looking for.
    """
    for word in sorted(VERDICT_CLASSES, key=len, reverse=True):
        css_class = VERDICT_CLASSES[word]
        badge = f'<span class="verdict verdict-{css_class}">{word}</span>'
        for marked_up in (f"<strong>{word}</strong>", f"<code>{word}</code>"):
            body_html = body_html.replace(marked_up, badge)
    return body_html


def wrap_tables(body_html: str) -> str:
    """Let a wide table scroll inside itself rather than the page."""
    return body_html.replace("<table>", '<div class="table-scroll"><table>').replace(
        "</table>", "</table></div>"
    )


def render_spec_to_html(spec_path: Path) -> str:
    markdown_text = spec_path.read_text(encoding="utf-8")

    title = extract_title(markdown_text)
    standfirst = extract_standfirst(markdown_text)
    sections = collect_sections(markdown_text)
    commit = read_git_commit()
    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    parser = MarkdownIt("commonmark", {"html": False, "linkify": True}).enable("table")
    # The standfirst is markdown too - emphasis and code spans in it must render,
    # not appear as asterisks and backticks.
    standfirst_html = parser.renderInline(standfirst)
    # The first heading becomes the page header, so it is not repeated in the body.
    body_source = re.sub(r"^#\s+.*?\n", "", markdown_text, count=1)
    body_html = parser.render(body_source)
    body_html = add_heading_anchors(body_html)
    body_html = badge_verdicts(body_html)
    body_html = wrap_tables(body_html)

    # Headings are markdown too, so a heading containing a code span must render
    # as one in the contents list rather than showing its backticks.
    contents = "\n".join(
        f'      <li><a href="#{anchor}">{parser.renderInline(text)}</a></li>'
        for anchor, text in sections
    )

    return PAGE_TEMPLATE.format(
        title=html.escape(title),
        standfirst=standfirst_html,
        contents=contents,
        body=body_html,
        source=html.escape(str(spec_path.relative_to(REPO_ROOT))),
        commit=html.escape(commit),
        built_at=built_at,
        section_count=len(sections),
    )


PAGE_TEMPLATE = """<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap">
<style>
  :root {{
    --ground:      #eef1f1;
    --surface:     #fbfcfc;
    --surface-sunk:#e6ebeb;
    --ink:         #101819;
    --ink-soft:    #55646a;
    --ink-faint:   #7d8b90;
    --rule:        #cfd8d8;
    --rule-strong: #aebaba;
    --accent:      #0e5a5e;
    --accent-soft: #d3e5e5;
    --measured:    #17663a;
    --measured-bg: #d8ecdf;
    --caution:     #8a5300;
    --caution-bg:  #f4e6cd;
    --withdrawn:   #a32a2a;
    --withdrawn-bg:#f3dcdc;
    --neutral-bg:  #dde4e4;

    --step--1: 0.815rem;
    --step-0:  1.0rem;
    --step-1:  1.21rem;
    --step-2:  1.55rem;
    --step-3:  2.05rem;
    --step-4:  2.9rem;
  }}

  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --ground:      #0c1112;
      --surface:     #131a1b;
      --surface-sunk:#0a0f10;
      --ink:         #e3eaea;
      --ink-soft:    #9dadb2;
      --ink-faint:   #74858a;
      --rule:        #263234;
      --rule-strong: #3a4a4d;
      --accent:      #58bcc1;
      --accent-soft: #17393b;
      --measured:    #5cc98d;
      --measured-bg: #14311f;
      --caution:     #e2a950;
      --caution-bg:  #35280d;
      --withdrawn:   #ec8b8b;
      --withdrawn-bg:#3a1c1c;
      --neutral-bg:  #1e2829;
    }}
  }}

  :root[data-theme="dark"] {{
    --ground:      #0c1112;
    --surface:     #131a1b;
    --surface-sunk:#0a0f10;
    --ink:         #e3eaea;
    --ink-soft:    #9dadb2;
    --ink-faint:   #74858a;
    --rule:        #263234;
    --rule-strong: #3a4a4d;
    --accent:      #58bcc1;
    --accent-soft: #17393b;
    --measured:    #5cc98d;
    --measured-bg: #14311f;
    --caution:     #e2a950;
    --caution-bg:  #35280d;
    --withdrawn:   #ec8b8b;
    --withdrawn-bg:#3a1c1c;
    --neutral-bg:  #1e2829;
  }}

  *, *::before, *::after {{ box-sizing: border-box; }}

  body {{
    margin: 0;
    background: var(--ground);
    color: var(--ink);
    font-family: "Source Serif 4", Charter, Georgia, serif;
    font-size: var(--step-0);
    line-height: 1.62;
    -webkit-font-smoothing: antialiased;
  }}

  .sheet {{
    max-width: 61rem;
    margin: 0 auto;
    padding: clamp(1.25rem, 3vw, 3rem) clamp(1.1rem, 4vw, 3rem) 6rem;
    display: flex;
    flex-direction: column;
    gap: 2.75rem;
  }}

  /* ---- datasheet header ------------------------------------------------ */

  .masthead {{
    display: flex;
    flex-direction: column;
    gap: 1.15rem;
    padding-bottom: 1.75rem;
    border-bottom: 2px solid var(--ink);
  }}

  .eyebrow {{
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: var(--step--1);
    font-weight: 500;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--accent);
    margin: 0;
  }}

  h1 {{
    font-family: Archivo, "Helvetica Neue", sans-serif;
    font-weight: 700;
    font-size: var(--step-4);
    line-height: 1.05;
    letter-spacing: -0.022em;
    text-wrap: balance;
    margin: 0;
  }}

  .standfirst {{
    margin: 0;
    max-width: 46rem;
    font-size: var(--step-1);
    line-height: 1.5;
    color: var(--ink-soft);
  }}

  .stamp {{
    display: flex;
    flex-wrap: wrap;
    gap: 0 2rem;
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: var(--step--1);
    color: var(--ink-faint);
  }}
  .stamp b {{ font-weight: 500; color: var(--ink-soft); }}

  /* ---- contents -------------------------------------------------------- */

  .contents {{
    background: var(--surface);
    border: 1px solid var(--rule);
    padding: 1.4rem 1.6rem;
  }}
  .contents h2 {{
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: var(--step--1);
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--ink-faint);
    margin: 0 0 0.9rem;
    padding: 0;
    border: 0;
  }}
  .contents ol {{
    list-style: none;
    margin: 0;
    padding: 0;
    columns: 2;
    column-gap: 2.5rem;
  }}
  .contents li {{ break-inside: avoid; margin-bottom: 0.3rem; }}
  .contents a {{
    font-family: Archivo, sans-serif;
    font-size: var(--step--1);
    color: var(--ink-soft);
    text-decoration: none;
    border-bottom: 1px solid transparent;
  }}
  .contents code {{
    background: none;
    border: 0;
    padding: 0;
    font-size: 0.92em;
    color: inherit;
  }}
  .contents a:hover, .contents a:focus-visible {{
    color: var(--accent);
    border-bottom-color: var(--accent);
  }}
  @media (max-width: 46rem) {{ .contents ol {{ columns: 1; }} }}

  /* ---- body ------------------------------------------------------------ */

  .prose {{ display: flex; flex-direction: column; gap: 1.15rem; }}

  .prose > * {{ margin: 0; max-width: 42rem; }}
  .prose > .table-scroll, .prose > hr, .prose > pre {{ max-width: none; }}

  h2 {{
    font-family: Archivo, sans-serif;
    font-size: var(--step-3);
    font-weight: 700;
    line-height: 1.15;
    letter-spacing: -0.018em;
    text-wrap: balance;
    margin: 2.4rem 0 0;
    padding-top: 1.1rem;
    border-top: 1px solid var(--rule-strong);
    scroll-margin-top: 1.5rem;
  }}

  h3 {{
    font-family: Archivo, sans-serif;
    font-size: var(--step-1);
    font-weight: 600;
    letter-spacing: -0.008em;
    text-wrap: balance;
    margin: 1.5rem 0 0;
    color: var(--ink);
  }}

  p {{ margin: 0; }}
  strong {{ font-weight: 600; }}

  a {{ color: var(--accent); text-decoration-thickness: 1px; text-underline-offset: 2px; }}

  /* Scoped to .prose: a global ul/ol flex rule would override the contents
     list's multi-column layout, which is a cascade collision, not a style. */
  .prose ul, .prose ol {{
    margin: 0;
    padding-left: 1.35rem;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }}
  .prose li {{ padding-left: 0.15rem; }}
  .prose li::marker {{ color: var(--ink-faint); }}

  code {{
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: 0.88em;
    background: var(--surface-sunk);
    border: 1px solid var(--rule);
    padding: 0.08em 0.34em;
    border-radius: 2px;
  }}

  pre {{
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    background: var(--surface-sunk);
    border: 1px solid var(--rule);
    border-left: 3px solid var(--accent);
    padding: 0.95rem 1.1rem;
    overflow-x: auto;
    font-size: var(--step--1);
    line-height: 1.55;
    max-width: none;
  }}
  pre code {{ background: none; border: 0; padding: 0; font-size: inherit; }}

  /* A code span inside a heading must not inherit the heading's size, and one
     that wraps inside a table cell must not paint over the row border. */
  h1 code, h2 code, h3 code {{
    font-size: 0.62em;
    font-weight: 500;
    vertical-align: 0.08em;
  }}
  code {{
    overflow-wrap: anywhere;
    box-decoration-break: clone;
    -webkit-box-decoration-break: clone;
  }}

  blockquote {{
    margin: 0.35rem 0;
    padding: 0.15rem 0 0.15rem 1.15rem;
    border-left: 3px solid var(--accent);
    color: var(--ink-soft);
    font-size: var(--step-1);
    line-height: 1.5;
  }}
  blockquote p + p {{ margin-top: 0.6rem; }}

  hr {{
    border: 0;
    border-top: 1px solid var(--rule);
    margin: 1.4rem 0 0.4rem;
  }}

  /* ---- tables as datasheet parameter blocks ---------------------------- */

  .table-scroll {{
    overflow-x: auto;
    border: 1px solid var(--rule);
    background: var(--surface);
  }}

  table {{
    border-collapse: collapse;
    width: 100%;
    font-family: Archivo, sans-serif;
    font-size: var(--step--1);
    font-variant-numeric: tabular-nums;
    line-height: 1.45;
  }}

  thead th {{
    text-align: left;
    font-weight: 600;
    font-size: 0.76rem;
    letter-spacing: 0.09em;
    text-transform: uppercase;
    color: var(--ink-faint);
    background: var(--surface-sunk);
    border-bottom: 1px solid var(--rule-strong);
    padding: 0.6rem 0.85rem;
    white-space: nowrap;
  }}

  tbody td {{
    padding: 0.62rem 0.85rem;
    line-height: 1.75;
    border-bottom: 1px solid var(--rule);
    vertical-align: top;
  }}
  tbody tr:last-child td {{ border-bottom: 0; }}
  tbody tr:nth-child(even) td {{ background: color-mix(in srgb, var(--surface-sunk) 45%, transparent); }}
  tbody td:first-child {{ font-weight: 600; color: var(--ink); }}
  table code {{ font-size: 0.84em; }}

  /* ---- verdict badges -------------------------------------------------- */

  .verdict {{
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: 0.74rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    padding: 0.12em 0.45em;
    border-radius: 2px;
    white-space: nowrap;
  }}
  .verdict-measured  {{ color: var(--measured);  background: var(--measured-bg); }}
  .verdict-caution   {{ color: var(--caution);   background: var(--caution-bg); }}
  .verdict-withdrawn {{ color: var(--withdrawn); background: var(--withdrawn-bg); }}
  .verdict-neutral   {{ color: var(--ink-soft);  background: var(--neutral-bg); }}

  :focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}

  @media (prefers-reduced-motion: reduce) {{
    *, *::before, *::after {{ animation: none !important; transition: none !important; }}
  }}
</style>

<main class="sheet">
  <header class="masthead">
    <p class="eyebrow">ajit-segment-bots · implementation spec</p>
    <h1>{title}</h1>
    <p class="standfirst">{standfirst}</p>
    <div class="stamp">
      <span><b>source</b> {source}</span>
      <span><b>commit</b> {commit}</span>
      <span><b>rendered</b> {built_at}</span>
      <span><b>sections</b> {section_count}</span>
    </div>
  </header>

  <nav class="contents" aria-label="Contents">
    <h2>Contents</h2>
    <ol>
{contents}
    </ol>
  </nav>

  <article class="prose">
{body}
  </article>
</main>
"""


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    spec_path = Path(sys.argv[1]).resolve()
    if not spec_path.is_file():
        print(f"no such spec: {spec_path}", file=sys.stderr)
        return 1

    out_path = spec_path.with_suffix(".html")
    out_path.write_text(render_spec_to_html(spec_path), encoding="utf-8")

    print(f"rendered {spec_path.name} -> {out_path}")
    print(f"  {out_path.stat().st_size / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
