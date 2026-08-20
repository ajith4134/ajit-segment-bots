#!/usr/bin/env python3
"""Render the part monitor: every part in the blueprint, coloured by how far it is built.

    python3 dashboard/build_part_monitor.py

The user asked for a board like a CPU monitor, showing every feature working. Rule 8
in ~/.claude/CLAUDE.md decides what that is allowed to look like: a display shows
MEASURED state, never asserted state. Nothing in this project is implemented yet, so
a board showing parts "working" would be fiction with good intentions -- and a green
board is more convincing than no board, which makes a wrong one worse than none.

So this is a BUILD monitor rather than a runtime monitor. Each part sits on a
four-rung ladder and every rung is a probe that actually runs:

    DECLARED     the part exists in the blueprint with its contract intact
    IMPLEMENTED  a source file named for the part exists on disk
    TESTED       a test file naming the part exists on disk
    RUNNING      the part reports a live heartbeat

Today every part sits at DECLARED, and the board says so in the dark. As code lands,
cells climb. The generator is the deliverable; the page is its output, and nothing on
it is hand-written.
"""

from __future__ import annotations

import html
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from render_blueprint import find_contract_violations, load_feature_registry

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
OUT = HERE / "part-monitor.html"

# Where implementation would live. docs/ and dashboard/ are excluded on purpose:
# a part is not implemented because it is described in a document, and the monitor
# must never mistake its own source for the bot's.
EXCLUDED_DIRS = {".git", "docs", "dashboard", "__pycache__", ".githooks"}
SOURCE_SUFFIXES = {".py", ".ts", ".js", ".rs", ".go", ".java", ".kt", ".rb", ".cpp", ".c", ".mjs"}

# Rungs, lowest first. The order is the ladder.
DECLARED = "DECLARED"
IMPLEMENTED = "IMPLEMENTED"
TESTED = "TESTED"
RUNNING = "RUNNING"
FAILING = "FAILING"
UNMEASURED = "NOT MEASURED"

LADDER = (DECLARED, IMPLEMENTED, TESTED, RUNNING)

RUNG_MEANING = {
    DECLARED: "in the blueprint, contract intact, no code",
    IMPLEMENTED: "a source file named for this part exists",
    TESTED: "a test file naming this part exists",
    RUNNING: "the part reports a live heartbeat",
    FAILING: "a probe ran and the part is broken",
    UNMEASURED: "no probe could run for this part",
}


@dataclass
class PartState:
    part_id: str
    name: str
    role: str
    category: str
    rung: str
    proof: str


def find_source_files() -> list[Path]:
    """Every candidate implementation file in the project, excluding docs and this board."""
    found = []
    for path in PROJECT.rglob("*"):
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        if any(part in EXCLUDED_DIRS for part in path.relative_to(PROJECT).parts):
            continue
        found.append(path)
    return found


def name_variants(part_id: str) -> set[str]:
    """A part named risk-gate could land as risk-gate, risk_gate, riskGate or RiskGate."""
    words = part_id.split("-")
    camel = words[0] + "".join(w.capitalize() for w in words[1:])
    return {part_id, "_".join(words), camel, camel[:1].upper() + camel[1:]}


def probe_part_rung(part_id: str, sources: list[Path]) -> tuple[str, str]:
    """How far up the ladder this part actually is, and the evidence for saying so.

    Never guesses upward. A part with no file is DECLARED, which is a real state and
    not a gap -- the blueprint phase is where every part is meant to be right now.
    """
    variants = name_variants(part_id)
    hits = [p for p in sources if any(v in p.stem for v in variants)]
    if not hits:
        return DECLARED, "no source file on disk matches this part's name"

    tests = [p for p in hits if "test" in p.name.lower() or "test" in str(p.parent).lower()]
    impls = [p for p in hits if p not in tests]

    if not impls:
        return DECLARED, f"only test files found ({tests[0].relative_to(PROJECT)}), no implementation"
    where = impls[0].relative_to(PROJECT)
    if tests:
        return TESTED, f"{where} plus test {tests[0].relative_to(PROJECT)}"
    return IMPLEMENTED, f"{where}, no test file naming this part"


def parts_in_violation(registry) -> dict[str, str]:
    """Parts the contract checker names, keyed by part id.

    This is what makes FAILING reachable. Without it the board could only ever climb,
    and a board with no way to render red has never been tested against a real failure.
    Violations are reported by a part's NAME, so the lookup is built from names back to
    ids rather than by matching on strings that happen to look similar.
    """
    by_name = {f.get("name", f["id"]): f["id"] for f in registry.features}
    broken: dict[str, str] = {}
    for violation in find_contract_violations(registry):
        for name, part_id in by_name.items():
            if violation.startswith(f"{violation.split()[0]} {name}:"):
                broken.setdefault(part_id, violation)
    return broken


def measure_parts() -> list[PartState]:
    registry = load_feature_registry()
    sources = find_source_files()
    broken = parts_in_violation(registry)
    states = []
    for feature in registry.features:
        rung, proof = probe_part_rung(feature["id"], sources)
        if feature["id"] in broken:
            rung, proof = FAILING, broken[feature["id"]]
        states.append(
            PartState(
                part_id=feature["id"],
                name=feature.get("name", feature["id"]),
                role=feature.get("role", ""),
                category=feature.get("category", ""),
                rung=rung,
                proof=proof,
            )
        )
    return states


def read_last_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%h %cI %s"],
            cwd=PROJECT, capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else "git unavailable"
    except Exception:
        return "git unavailable"


def category_lookup() -> dict[str, dict]:
    registry = load_feature_registry()
    return {c["id"]: c for c in registry.categories}


def esc(text: str) -> str:
    return html.escape(str(text), quote=True)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def render_cell(state: PartState) -> str:
    return (
        f'<div class="cell rung-{slug(state.rung)}" '
        f'title="{esc(state.name)} — {esc(state.rung)} — {esc(state.proof)}">'
        f'<span class="cell-name">{esc(state.name)}</span>'
        f'<span class="cell-rung">{esc(state.rung)}</span>'
        f"</div>"
    )


def render_block(category: dict, states: list[PartState]) -> str:
    total = len(states)
    built = len([s for s in states if s.rung in (IMPLEMENTED, TESTED, RUNNING)])
    pct = round(100 * built / total) if total else 0
    cells = "".join(render_cell(s) for s in states)
    return f"""
    <section class="block">
      <header class="block-head">
        <h3>{esc(category.get("name", category["id"]))}</h3>
        <div class="block-meter" role="img"
             aria-label="{built} of {total} parts past declared">
          <div class="meter-track"><div class="meter-fill" style="width:{pct}%"></div></div>
          <span class="meter-num">{built}/{total}</span>
        </div>
      </header>
      <div class="cells">{cells}</div>
    </section>"""


def render_page() -> str:
    states = measure_parts()
    categories = category_lookup()
    stamped = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    commit = read_last_commit()

    counts = {rung: len([s for s in states if s.rung == rung]) for rung in LADDER}
    n_failing = len([s for s in states if s.rung == FAILING])
    failing_clause = (
        f", and {n_failing} {'is' if n_failing == 1 else 'are'} FAILING a contract check"
        if n_failing else ", which is the correct reading"
    )
    total = len(states)
    past_declared = len([s for s in states if s.rung in (IMPLEMENTED, TESTED, RUNNING)])
    overall = round(100 * past_declared / total) if total else 0

    blocks = []
    for cid, category in categories.items():
        owned = [s for s in states if s.category == cid]
        if owned:
            blocks.append(render_block(category, owned))
    blocks_html = "".join(blocks)

    empty_blocks = [c.get("name", cid) for cid, c in categories.items()
                    if not any(s.category == cid for s in states)]
    empty_note = ""
    if empty_blocks:
        empty_note = (
            '<p class="note">Blocks with no parts declared yet: '
            + ", ".join(esc(n) for n in sorted(empty_blocks)) + ".</p>"
        )

    ladder_rows = "".join(
        f'<div class="legend-row"><span class="swatch rung-{slug(r)}"></span>'
        f'<b>{esc(r)}</b><span>{esc(RUNG_MEANING[r])}</span>'
        f'<span class="legend-count">{counts.get(r, 0)}</span></div>'
        for r in LADDER
    )
    extra_counts = {
        FAILING: len([s for s in states if s.rung == FAILING]),
        UNMEASURED: len([s for s in states if s.rung == UNMEASURED]),
    }
    unreached = "".join(
        f'<div class="legend-row{"" if extra_counts[r] else " unreached"}">'
        f'<span class="swatch rung-{slug(r)}"></span>'
        f'<b>{esc(r)}</b><span>{esc(RUNG_MEANING[r])}</span>'
        f'<span class="legend-count">{extra_counts[r]}</span></div>'
        for r in (FAILING, UNMEASURED)
    )

    return f"""<title>Part Monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
  :root {{
    --ground:#0B0D10; --panel:#12151A; --sunk:#080A0D;
    --ink:#DDE3EA; --muted:#8A94A3; --faint:#5D6673; --rule:#1F242C;
    --declared:#2B323C; --implemented:#3D7FA8; --tested:#2E9E74;
    --running:#59D3A0; --failing:#C4544E; --unmeasured:#6B5A2E;
    --accent:#59D3A0;
  }}
  @media (prefers-color-scheme: light) {{
    :root:not([data-theme="dark"]) {{
      --ground:#EDEFF2; --panel:#FFFFFF; --sunk:#E1E5EA;
      --ink:#12161C; --muted:#4E5866; --faint:#7A8494; --rule:#D3D9E0;
      --declared:#C3CAD3; --implemented:#2E6D93; --tested:#1E7F5C;
      --running:#188A62; --failing:#A83A35; --unmeasured:#8A7326;
      --accent:#188A62;
    }}
  }}
  :root[data-theme="light"] {{
    --ground:#EDEFF2; --panel:#FFFFFF; --sunk:#E1E5EA;
    --ink:#12161C; --muted:#4E5866; --faint:#7A8494; --rule:#D3D9E0;
    --declared:#C3CAD3; --implemented:#2E6D93; --tested:#1E7F5C;
    --running:#188A62; --failing:#A83A35; --unmeasured:#8A7326;
    --accent:#188A62;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    background:var(--ground); color:var(--ink);
    font-family:"IBM Plex Sans",ui-sans-serif,system-ui,sans-serif;
    margin:0; padding:clamp(1.2rem,4vw,2.6rem) clamp(0.9rem,4vw,2rem) 5rem;
    line-height:1.55; -webkit-font-smoothing:antialiased;
  }}
  .wrap {{ max-width:1180px; margin:0 auto; display:flex; flex-direction:column; gap:2rem; }}
  .eyebrow {{
    font-family:"IBM Plex Mono",monospace; font-size:0.76rem; letter-spacing:0.14em;
    text-transform:uppercase; color:var(--accent); margin:0;
  }}
  h1 {{ font-size:clamp(1.8rem,5vw,2.6rem); line-height:1.1; margin:0; font-weight:600;
       letter-spacing:-0.02em; text-wrap:balance; }}
  .lede {{ margin:0; color:var(--muted); max-width:62ch; }}

  .banner {{
    background:var(--panel); border:1px solid var(--rule);
    border-left:3px solid var(--declared);
    padding:1rem 1.15rem; display:flex; flex-direction:column; gap:0.5rem;
  }}
  .banner b {{ color:var(--ink); }}

  .summary {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr)); gap:0.7rem; }}
  .stat {{ background:var(--panel); border:1px solid var(--rule); padding:0.85rem 0.95rem;
          display:flex; flex-direction:column; gap:0.15rem; }}
  .stat .val {{ font-family:"IBM Plex Mono",monospace; font-size:1.65rem; font-weight:600;
               font-variant-numeric:tabular-nums; line-height:1.1; }}
  .stat .lab {{ font-family:"IBM Plex Mono",monospace; font-size:0.68rem; letter-spacing:0.1em;
               text-transform:uppercase; color:var(--faint); }}

  .overall {{ display:flex; flex-direction:column; gap:0.45rem; }}
  .overall-track {{ height:0.75rem; background:var(--sunk); border:1px solid var(--rule); }}
  .overall-fill {{ height:100%; background:var(--running); }}

  .legend {{ background:var(--panel); border:1px solid var(--rule); padding:1rem 1.15rem;
            display:flex; flex-direction:column; gap:0.45rem; }}
  .legend-row {{ display:grid; grid-template-columns:1rem 8.5rem 1fr 3rem; align-items:center;
                gap:0.7rem; font-size:0.86rem; }}
  .legend-row b {{ font-family:"IBM Plex Mono",monospace; font-size:0.76rem; letter-spacing:0.06em; }}
  .legend-row span:nth-child(3) {{ color:var(--muted); }}
  .legend-count {{ font-family:"IBM Plex Mono",monospace; text-align:right;
                  font-variant-numeric:tabular-nums; color:var(--ink); }}
  .legend-row.unreached {{ opacity:0.62; }}
  .swatch {{ width:1rem; height:1rem; border:1px solid var(--rule); }}

  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(19rem,1fr)); gap:0.9rem; }}
  .block {{ background:var(--panel); border:1px solid var(--rule); padding:0.85rem 0.9rem;
           display:flex; flex-direction:column; gap:0.7rem; }}
  .block-head {{ display:flex; flex-direction:column; gap:0.4rem; }}
  .block-head h3 {{ margin:0; font-size:0.98rem; font-weight:600; letter-spacing:-0.005em; }}
  .block-meter {{ display:flex; align-items:center; gap:0.6rem; }}
  .meter-track {{ flex:1; height:0.4rem; background:var(--sunk); border:1px solid var(--rule); }}
  .meter-fill {{ height:100%; background:var(--running); }}
  .meter-num {{ font-family:"IBM Plex Mono",monospace; font-size:0.72rem;
               color:var(--faint); font-variant-numeric:tabular-nums; }}

  .cells {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(8.2rem,1fr)); gap:3px; }}
  .cell {{ border:1px solid var(--rule); border-left-width:3px; background:var(--sunk);
          padding:0.4rem 0.5rem; display:flex; flex-direction:column; gap:0.1rem; min-height:3.1rem; }}
  .cell-name {{ font-size:0.72rem; line-height:1.25; }}
  .cell-rung {{ font-family:"IBM Plex Mono",monospace; font-size:0.6rem; letter-spacing:0.07em;
               color:var(--faint); }}

  .rung-declared    {{ border-left-color:var(--declared); }}
  .rung-implemented {{ border-left-color:var(--implemented); }}
  .rung-tested      {{ border-left-color:var(--tested); }}
  .rung-running     {{ border-left-color:var(--running); background:color-mix(in srgb,var(--running) 12%,var(--sunk)); }}
  .rung-failing     {{ border-left-color:var(--failing); background:color-mix(in srgb,var(--failing) 14%,var(--sunk)); }}
  .rung-not-measured{{ border-left-color:var(--unmeasured); }}
  .swatch.rung-declared {{ background:var(--declared); }}
  .swatch.rung-implemented {{ background:var(--implemented); }}
  .swatch.rung-tested {{ background:var(--tested); }}
  .swatch.rung-running {{ background:var(--running); }}
  .swatch.rung-failing {{ background:var(--failing); }}
  .swatch.rung-not-measured {{ background:var(--unmeasured); }}

  .note {{ color:var(--faint); font-size:0.85rem; margin:0; max-width:62ch; }}
  footer {{ border-top:1px solid var(--rule); padding-top:1rem; color:var(--faint);
           font-size:0.8rem; display:flex; flex-direction:column; gap:0.3rem;
           font-family:"IBM Plex Mono",monospace; }}
</style>

<div class="wrap">
  <header style="display:flex;flex-direction:column;gap:0.7rem">
    <p class="eyebrow">Measured {esc(stamped)}</p>
    <h1>Part monitor</h1>
    <p class="lede">Every part in the blueprint, on a four-rung ladder from declared to
      running. Each rung is a probe that ran against this server. Nothing here is
      hand-written and nothing is inferred upward.</p>
  </header>

  <div class="banner">
    <div><b>{counts[DECLARED]} of {total} parts are at DECLARED{failing_clause}.</b>
      This project is a blueprint: {len(categories)} blocks and {total} parts described, with
      no implementation written. That reading is correct, and a board showing parts
      "working" would be fiction.</div>
    <div>The cells climb as code lands. Re-run
      <code>python3 dashboard/build_part_monitor.py</code> to remeasure.</div>
  </div>

  <div class="summary">
    <div class="stat"><span class="val">{total}</span><span class="lab">parts</span></div>
    <div class="stat"><span class="val">{len(categories)}</span><span class="lab">blocks</span></div>
    <div class="stat"><span class="val">{past_declared}</span><span class="lab">past declared</span></div>
    <div class="stat"><span class="val">{overall}%</span><span class="lab">built</span></div>
  </div>

  <div class="overall">
    <div class="overall-track"><div class="overall-fill" style="width:{overall}%"></div></div>
    <p class="note">Overall build progress, measured: {past_declared} of {total} parts have a
      source file on disk.</p>
  </div>

  <div class="legend">
    {ladder_rows}
    {unreached}
    <p class="note">FAILING is wired to the contract checker: any part it names renders red
      here. It was tested by breaking a real part and watching this board go red, then
      restoring it — a board with no way to show red has never been tested against a
      failure.</p>
  </div>

  <div class="grid">{blocks_html}</div>
  {empty_note}

  <footer>
    <div>generated {esc(stamped)} by dashboard/build_part_monitor.py</div>
    <div>blueprint source: docs/features.json · last commit: {esc(commit)}</div>
    <div>a snapshot, not a live feed — the stamp above is when the probes ran</div>
  </footer>
</div>
"""


def write_part_monitor() -> Path:
    OUT.write_text(render_page())
    return OUT


if __name__ == "__main__":
    states = measure_parts()
    for rung in (*LADDER, FAILING, UNMEASURED):
        n = len([s for s in states if s.rung == rung])
        print(f"{rung:<13} {n:>3}")
    for s in states:
        if s.rung == FAILING:
            print(f"  RED  {s.part_id}: {s.proof}")
    path = write_part_monitor()
    print(f"\nwrote {path}")
