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
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from completion import (
    DECLARED,
    FAILING,
    IMPLEMENTED,
    LADDER,
    PartState,
    RUNG_MEANING,
    RUNNING,
    TESTED,
    UNMEASURED,
    block_completion,
    part_is_measured_complete,
)
from render_blueprint import FeatureRegistry, find_contract_violations, load_feature_registry

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
OUT = HERE / "part-monitor.html"

# Where implementation would live. docs/ and dashboard/ are excluded on purpose:
# a part is not implemented because it is described in a document, and the monitor
# must never mistake its own source for the bot's.
EXCLUDED_DIRS = {".git", "docs", "dashboard", "__pycache__", ".githooks"}
SOURCE_SUFFIXES = {".py", ".ts", ".js", ".rs", ".go", ".java", ".kt", ".rb", ".cpp", ".c", ".mjs"}


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


def is_test_file(path: Path) -> bool:
    """A test file, by directory or by filename convention -- never by substring.

    Substring matching was the rule until 2026-08-22, when the backtesting block
    landed: every file under parts/backtesting/ was classified as a test because
    "backtesting" contains "test", so all ten parts reported DECLARED with their
    own implementations listed as their tests. The same bug made skill_tester.py
    invisible to itself. What identifies a test here is a directory named tests or
    a file named test_x / x_test, and both are exact.
    """
    name = path.name.lower()
    if name.startswith("test_") or name.removesuffix(path.suffix).endswith("_test"):
        return True
    return any(part.lower() in {"test", "tests"} for part in path.parts)


def find_test_files(part_id: str, sources: list[Path]) -> list[Path]:
    """Every test file that names this part, by filename or by what it imports.

    Filename alone was the rule until 2026-08-22, when a block's eight parts were
    tested by one module covering all of them and every one of their dots stayed
    red. A test that imports a part and asserts on it is evidence about that part
    whatever the file is called -- and one module per block is how the rest of
    this build is going, so the probe reads the file rather than only its name.
    """
    variants = name_variants(part_id)
    named = [p for p in sources if is_test_file(p) and any(v in p.stem for v in variants)]
    importing = [
        path
        for path, text in _test_file_texts(sources)
        if path not in named and any(variant in text for variant in variants)
    ]
    return named + importing


# Every test file is read once per board build, not once per part: at 321 parts
# the naive form was 321 passes over every test file and made the build minutes
# long instead of seconds.
_TEST_TEXT_CACHE: dict[tuple[Path, ...], tuple[tuple[Path, str], ...]] = {}


def _test_file_texts(sources: list[Path]) -> tuple[tuple[Path, str], ...]:
    key = tuple(sources)
    cached = _TEST_TEXT_CACHE.get(key)
    if cached is not None:
        return cached
    texts = []
    for path in sources:
        if not is_test_file(path):
            continue
        try:
            texts.append((path, path.read_text()))
        except OSError:
            continue
    _TEST_TEXT_CACHE[key] = tuple(texts)
    return _TEST_TEXT_CACHE[key]


def find_implementation_file(part_id: str, sources: list[Path]) -> Path | None:
    """The one non-test source file whose name matches this part, if any.

    Shared by probe_part_rung (how far up the ladder) and the wiring check below
    (what the part actually built) -- one definition of "this part's file",
    rather than two that could quietly disagree about which file a part is.
    """
    variants = name_variants(part_id)
    hits = [p for p in sources if any(v in p.stem for v in variants)]
    impls = [p for p in hits if not is_test_file(p)]
    if not impls:
        return None
    # An exact stem wins over a substring. Without this, counterfactual-replayer
    # claimed exit_counterfactual_replayer.py -- the shorter id is a substring of
    # the longer one, and the board then reported a wiring mismatch that was really
    # two parts sharing one file. A part's file is named for the part, so an exact
    # match is the answer whenever one exists.
    exact = [p for p in impls if p.stem in variants]
    return exact[0] if exact else impls[0]


def probe_part_rung(part_id: str, sources: list[Path]) -> tuple[str, str]:
    """How far up the ladder this part actually is, and the evidence for saying so.

    Never guesses upward. A part with no file is DECLARED, which is a real state and
    not a gap -- the blueprint phase is where every part is meant to be right now.
    """
    impl = find_implementation_file(part_id, sources)
    tests = find_test_files(part_id, sources)
    if impl is None and not tests:
        return DECLARED, "no source file on disk matches this part's name"
    if impl is None:
        return DECLARED, f"only test files found ({tests[0].relative_to(PROJECT)}), no implementation"
    where = impl.relative_to(PROJECT)
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


def _part_declaration_module():
    """runtime.part_declaration, imported defensively.

    RL-070's wiring check must say so rather than take the whole board generation
    down if the runtime package is not importable -- the same guard
    build_status_board.py's collect_substrate_results already applies to the same
    package, for the same reason.
    """
    project_root = str(PROJECT)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    from runtime import part_declaration

    return part_declaration


@dataclass
class WiringCheck:
    """What the built-vs-blueprint wiring probe found (RL-067, RL-070).

    checked_part_ids -- every part that has a source file, and so was actually
    compared. mismatches -- part_id -> proof naming both sides, for a part whose
    own module disagrees with the blueprint or cannot state its wiring at all
    (never green by inference: a part that cannot be checked is not the same as
    a part that agrees). unavailable -- set only when the whole check could not
    run, so that failure is never silently indistinguishable from "0 mismatches".
    """

    checked_part_ids: tuple[str, ...]
    mismatches: dict[str, str]
    unavailable: str | None = None


def check_wiring_against_blueprint(registry: FeatureRegistry, sources: list[Path]) -> WiringCheck:
    """RL-067: does a built part's real consumes/produces match docs/features.json?

    Checked only for parts with a source file -- a part with no code cannot
    disagree with the blueprint, it is simply DECLARED, which is not a defect.
    A built part states its real wiring as a module-level PART_DECLARATION,
    read WITHOUT importing or executing the part's file --
    runtime.part_declaration.read_declaration_from_source parses it with `ast`
    instead. Generating a dashboard must never run part code: once real parts
    exist, importing one to read its wiring would run whatever that part's
    import does (a socket, a thread, a forkserver) on every board build, and a
    part that hangs or crashes on import would take the board generator down
    with it -- exactly when a broken part most needs to be seen. The static
    reader returns the same PartDeclaration shape the blueprint side already
    uses (load_declaration_from_blueprint), so the two sides are still compared
    as one type.

    Today no part has a source file, so checked_part_ids is empty and
    mismatches is empty too -- that is "nothing to compare", not "everything
    agrees", and the caller is expected to print the distinction rather than
    read an empty dict as a clean bill of health.
    """
    try:
        part_declaration = _part_declaration_module()
    except ImportError as failure:
        return WiringCheck((), {}, unavailable=f"runtime.part_declaration not importable: {failure}")

    checked: list[str] = []
    mismatches: dict[str, str] = {}
    for feature in registry.features:
        part_id = feature["id"]
        source_path = find_implementation_file(part_id, sources)
        if source_path is None:
            continue
        checked.append(part_id)
        where = source_path.relative_to(PROJECT)
        try:
            declared = part_declaration.load_declaration_from_blueprint(part_id)
            built = part_declaration.read_declaration_from_source(source_path)
        except Exception as failure:  # a part that cannot be read is unverifiable, not healthy
            mismatches[part_id] = (
                f"{part_id}: {where} has no verifiable wiring "
                f"({type(failure).__name__}: {failure})"
            )
            continue
        if built.consumes != declared.consumes or built.produces != declared.produces:
            mismatches[part_id] = (
                f"{part_id}: blueprint declares consumes={list(declared.consumes)} "
                f"produces={list(declared.produces)}; {where} declares "
                f"consumes={list(built.consumes)} produces={list(built.produces)}"
            )
    return WiringCheck(tuple(checked), mismatches)


def read_heartbeat_table_path() -> Path:
    """Where the operator's settings say heartbeat-collector writes its table."""
    from runtime.settings_reader import load_settings_document, settings_directory

    document = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    return Path(str(document.read_value("heartbeat_table_path"))).expanduser()


def probe_live_heartbeats(
    table_path: Path | None = None,
    now_ns: int | None = None,
    silent_after_seconds: float | None = None,
) -> tuple[dict[str, str], str]:
    """Which parts are alive right now, and the evidence.

    Reads the table heartbeat-collector writes -- the same file, not a second
    count kept for the board. Returns {part_id: proof} for every part whose
    latest report is REPORTING in a table that is itself fresh, and a sentence
    saying what was read. A table older than the collector's own silence
    threshold proves nothing about any part: the collector that wrote it has
    stopped, and its last word must not be shown as current (Rule 8).
    """
    project_root = str(PROJECT)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    from parts.observability.heartbeat_collector import REPORTING, read_heartbeat_table_file

    if table_path is None:
        try:
            table_path = read_heartbeat_table_path()
        except Exception as refusal:  # the settings file is the operator's; say why
            return {}, f"no heartbeat table: settings refused ({refusal})"
    if silent_after_seconds is None:
        try:
            from runtime.settings_reader import load_settings_document, settings_directory

            silent_after_seconds = float(
                load_settings_document(settings_directory() / "runtime.toml", "runtime")
                .read_value("heartbeat_silent_after_seconds")
            )
        except Exception:
            silent_after_seconds = None
    document = read_heartbeat_table_file(table_path)
    if document is None:
        return {}, f"no heartbeat table at {table_path}: heartbeat-collector has not written one"
    if now_ns is None:
        import time

        now_ns = time.time_ns()
    table_age = (now_ns - int(document.get("collected_at_ns", 0))) / 1e9
    if silent_after_seconds is not None and table_age >= silent_after_seconds:
        return {}, (
            f"heartbeat table at {table_path} is {table_age:.0f}s old, past the "
            f"{silent_after_seconds:.0f}s silence threshold: the collector itself has stopped"
        )
    alive = {}
    for beat in document.get("heartbeats", ()):
        if beat.get("state") == REPORTING:
            alive[beat["part_id"]] = (
                f"{table_path.name}: reported {beat.get('age_seconds', 0.0):.1f}s before a table "
                f"written {table_age:.0f}s ago (rate {beat.get('rate_ratio')}, "
                f"staleness {beat.get('staleness_seconds')}s, input loss {beat.get('input_loss') or 'none'})"
            )
    return alive, (
        f"heartbeat table at {table_path}, written {table_age:.0f}s ago: "
        f"{document.get('reporting', 0)} reporting, {document.get('late', 0)} late, "
        f"{document.get('silent', 0)} silent"
    )


def _measure_build_state() -> tuple[list[PartState], WiringCheck]:
    registry = load_feature_registry()
    sources = find_source_files()
    broken = parts_in_violation(registry)
    wiring = check_wiring_against_blueprint(registry, sources)
    alive, _heartbeat_note = probe_live_heartbeats()
    states = []
    for feature in registry.features:
        rung, proof = probe_part_rung(feature["id"], sources)
        # RUNNING sits above TESTED on the ladder and is reached only from it: a
        # part that reports a heartbeat but has no test is a part running
        # unproven, and the proof says so rather than the rung climbing.
        if rung == TESTED and feature["id"] in alive:
            rung, proof = RUNNING, f"{proof}; {alive[feature['id']]}"
        elif feature["id"] in alive:
            proof = f"{proof}; reporting a heartbeat but not TESTED, so it does not climb"
        if feature["id"] in wiring.mismatches:
            rung, proof = FAILING, wiring.mismatches[feature["id"]]
        elif feature["id"] in broken:
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
    return states, wiring


def measure_parts() -> list[PartState]:
    """Every part's state. Kept as its own entry point -- dashboard/part_health_api.py
    imports this directly and does not need the wiring detail alongside it, since
    a wiring mismatch already surfaces as that part's FAILING rung and proof.
    """
    states, _wiring = _measure_build_state()
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


def render_dot(is_complete: bool, proof: str) -> str:
    """RL-070's summary layer: one dot, green only when measured complete, with
    its proof carried in the title so the colour is never asserted without it
    (Rule 8) -- hover reads the same fact the colour is claiming.
    """
    label = "complete" if is_complete else "unfinished"
    return (
        f'<span class="dot dot-{"green" if is_complete else "red"}" '
        f'title="{label}: {esc(proof)}" aria-label="{label}"></span>'
    )


def render_cell(state: PartState) -> str:
    is_complete = part_is_measured_complete(state)
    return (
        f'<div class="cell rung-{slug(state.rung)}" '
        f'title="{esc(state.name)} — {esc(state.rung)} — {esc(state.proof)}">'
        f'<span class="cell-top">{render_dot(is_complete, state.proof)}'
        f'<span class="cell-name">{esc(state.name)}</span></span>'
        f'<span class="cell-rung">{esc(state.rung)}</span>'
        f"</div>"
    )


def render_block(category: dict, states: list[PartState]) -> str:
    total = len(states)
    built = len([s for s in states if s.rung in (IMPLEMENTED, TESTED, RUNNING)])
    pct = round(100 * built / total) if total else 0
    is_complete, dot_proof = block_completion(states)
    cells = "".join(render_cell(s) for s in states)
    return f"""
    <section class="block">
      <header class="block-head">
        <div class="block-title">
          {render_dot(is_complete, dot_proof)}
          <h3>{esc(category.get("name", category["id"]))}</h3>
        </div>
        <p class="block-dot-proof">{esc(dot_proof)}</p>
        <div class="block-meter" role="img"
             aria-label="{built} of {total} parts past declared">
          <div class="meter-track"><div class="meter-fill" style="width:{pct}%"></div></div>
          <span class="meter-num">{built}/{total}</span>
        </div>
      </header>
      <div class="cells">{cells}</div>
    </section>"""


def render_wiring_check_note(wiring: WiringCheck) -> str:
    """RL-067: the artefact carries this probe's own proof, not just the code that
    ran it (Rule 8) -- including the honest-empty case, where the note must say
    there was nothing built to compare rather than let a reader infer agreement
    from an empty mismatch list.
    """
    if wiring.unavailable:
        detail = f"unavailable — {esc(wiring.unavailable)}"
    elif not wiring.checked_part_ids:
        detail = (
            "0 parts have a source file yet, so there is nothing built to compare "
            "against the blueprint — not zero mismatches, nothing to check"
        )
    else:
        detail = (
            f"{len(wiring.checked_part_ids)} built part(s) compared against "
            f"docs/features.json, {len(wiring.mismatches)} mismatch(ed)"
        )
    return (
        '<div class="wiring-check">'
        "<p class=\"note\"><b>Wiring vs blueprint (RL-067):</b> "
        f"{detail}. Every part with a source file has its consumes/produces read statically "
        "from its own PART_DECLARATION (parsed with ast, never imported) and compared against "
        "docs/features.json — check_wiring_against_blueprint() in this file.</p>"
        "</div>"
    )


def render_page() -> str:
    states, wiring = _measure_build_state()
    _alive, heartbeat_note = probe_live_heartbeats()
    categories = category_lookup()
    stamped = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    commit = read_last_commit()

    green_parts = len([s for s in states if part_is_measured_complete(s)])
    green_blocks = sum(
        1 for cid in categories if block_completion([s for s in states if s.category == cid])[0]
    )

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
  .cell-top {{ display:flex; align-items:flex-start; gap:0.32rem; }}
  .cell-name {{ font-size:0.72rem; line-height:1.25; }}
  .cell-rung {{ font-family:"IBM Plex Mono",monospace; font-size:0.6rem; letter-spacing:0.07em;
               color:var(--faint); }}

  .dot {{ display:inline-block; width:0.6rem; height:0.6rem; border-radius:50%;
         flex:none; margin-top:0.15rem; border:1px solid var(--rule); }}
  .dot-green {{ background:var(--running); border-color:var(--running); }}
  .dot-red   {{ background:var(--failing); border-color:var(--failing); }}
  .dots-legend {{ background:var(--panel); border:1px solid var(--rule); padding:1rem 1.15rem;
                 display:flex; flex-direction:column; gap:0.45rem; }}
  .block-title {{ display:flex; align-items:center; gap:0.45rem; }}
  .block-dot-proof {{ margin:0; font-size:0.68rem; color:var(--faint); line-height:1.35; }}
  .wiring-check {{ background:var(--panel); border:1px solid var(--rule);
                   border-left:3px solid var(--implemented); padding:0.85rem 1.05rem; }}

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
    <div><b>{counts[DECLARED]} of {total} parts are at DECLARED, {counts[IMPLEMENTED]} IMPLEMENTED,
      {counts[TESTED]} TESTED, {counts[RUNNING]} RUNNING{failing_clause}.</b>
      {len(categories)} blocks and {total} parts in the blueprint. TESTED means a source file
      and a test file naming the part exist, nothing more; RUNNING means the part reported a
      heartbeat in the last few seconds. Neither says the part does its job on live data.</div>
    <div>Liveness: {esc(heartbeat_note)}.</div>
    <div>The cells climb as code lands and as parts run. Re-run
      <code>python3 dashboard/build_part_monitor.py</code> to remeasure.</div>
  </div>

  <div class="summary">
    <div class="stat"><span class="val">{total}</span><span class="lab">parts</span></div>
    <div class="stat"><span class="val">{len(categories)}</span><span class="lab">blocks</span></div>
    <div class="stat"><span class="val">{past_declared}</span><span class="lab">past declared</span></div>
    <div class="stat"><span class="val">{overall}%</span><span class="lab">built</span></div>
    <div class="stat"><span class="val">{green_parts}/{total}</span><span class="lab">parts green</span></div>
    <div class="stat"><span class="val">{green_blocks}/{len(categories)}</span><span class="lab">blocks green</span></div>
  </div>

  <div class="overall">
    <div class="overall-track"><div class="overall-fill" style="width:{overall}%"></div></div>
    <p class="note">Overall build progress, measured: {past_declared} of {total} parts have a
      source file on disk.</p>
  </div>

  <div class="legend">
    {ladder_rows}
    {unreached}
    <p class="note">FAILING is wired to the contract checker and the wiring check below: any
      part either names renders red here. It was tested by breaking a real part and watching
      this board go red, then restoring it — a board with no way to show red has never been
      tested against a failure.</p>
  </div>

  <div class="dots-legend">
    <div class="legend-row"><span class="dot dot-green"></span><b>GREEN</b>
      <span>measured complete — TESTED (source file and a test naming it), nothing the
        contract or wiring checks found wrong</span>
      <span class="legend-count">{green_parts}</span></div>
    <div class="legend-row"><span class="dot dot-red"></span><b>RED</b>
      <span>not proven complete — covers both genuinely unfinished and built-but-unprobed;
        which one it is stays in the proof beside the dot, not the colour</span>
      <span class="legend-count">{total - green_parts}</span></div>
    <p class="note">A block's dot (RL-070) is green only when every part in it is green — one
      red part keeps its block red. RUNNING is a separate rung and is not what the dot reports:
      a fully built, currently stopped part still reads green.</p>
  </div>

  {render_wiring_check_note(wiring)}

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
    states, wiring = _measure_build_state()
    for rung in (*LADDER, FAILING, UNMEASURED):
        n = len([s for s in states if s.rung == rung])
        print(f"{rung:<13} {n:>3}")
    for s in states:
        if s.rung == FAILING:
            print(f"  RED  {s.part_id}: {s.proof}")

    if wiring.unavailable:
        print(f"\nwiring vs blueprint (RL-067): unavailable — {wiring.unavailable}")
    elif not wiring.checked_part_ids:
        print("\nwiring vs blueprint (RL-067): 0 parts have a source file — nothing to compare yet")
    else:
        print(
            f"\nwiring vs blueprint (RL-067): checked {len(wiring.checked_part_ids)} built "
            f"part(s), {len(wiring.mismatches)} mismatch(ed)"
        )

    green_parts = len([s for s in states if part_is_measured_complete(s)])
    categories = category_lookup()
    green_blocks = sum(
        1 for cid in categories if block_completion([s for s in states if s.category == cid])[0]
    )
    print(
        f"\ndots (RL-070): {green_parts}/{len(states)} parts green, "
        f"{green_blocks}/{len(categories)} blocks green"
    )

    path = write_part_monitor()
    print(f"\nwrote {path}")
