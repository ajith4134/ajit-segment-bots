#!/usr/bin/env python3
"""Generate the ajit-segment-bots status board from probes that actually run.

Rule 8: every tile on the board traces to a probe executed by this script. A
check that did not run renders as NOT MEASURED, never as healthy. The page is
output, this generator is the deliverable — nothing on the board is written by
hand.

    python3 dashboard/build_status_board.py

Writes dashboard/status-board.html next to this file.
"""

from __future__ import annotations

import html
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from render_blueprint import (  # same directory as this script, so already importable
    REGISTRY_PATH,
    count_data_edges,
    count_health_edges,
    derive_category_edges,
    find_contract_violations,
    find_flow_gaps,
    load_feature_registry,
    render_blueprint_section,
)

PROJECT_HOME = Path(__file__).resolve().parent.parent
BOARD_PATH = PROJECT_HOME / "dashboard" / "status-board.html"
HOME = Path.home()

# Probe outcomes. UNMEASURED is the default for anything a probe could not
# establish — it is a state in its own right, visually distinct, never green.
OK = "OK"
NOT_BUILT = "NOT BUILT"
FAILING = "FAILING"
UNMEASURED = "NOT MEASURED"


@dataclass
class ProbeResult:
    """One measured fact and the evidence it came from."""

    label: str
    state: str
    value: str
    proof: str


def read_command_output(command: str) -> str | None:
    """Run a shell command, returning stripped stdout or None if it failed."""
    try:
        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def probe_project_home() -> ProbeResult:
    proof = f"test -d {PROJECT_HOME}"
    if not PROJECT_HOME.is_dir():
        return ProbeResult("Project home", FAILING, "missing", proof)
    entries = sorted(p.name for p in PROJECT_HOME.iterdir() if not p.name.startswith("."))
    return ProbeResult("Project home", OK, f"{len(entries)} entries", proof)


def probe_project_instructions() -> ProbeResult:
    instructions = PROJECT_HOME / "CLAUDE.md"
    proof = f"wc -l {instructions}"
    if not instructions.is_file():
        return ProbeResult("Project CLAUDE.md", NOT_BUILT, "absent", proof)
    line_count = len(instructions.read_text().splitlines())
    return ProbeResult("Project CLAUDE.md", OK, f"{line_count} lines", proof)


def probe_claude_startup_directory() -> ProbeResult:
    """Claude Code should start in this project home on every interactive shell."""
    aliases = HOME / ".bash_aliases"
    bashrc = HOME / ".bashrc"
    proof = "grep AJIT_SEGMENT_BOTS_HOME ~/.bash_aliases; grep bash_aliases ~/.bashrc"
    if not aliases.is_file():
        return ProbeResult("Claude startup directory", NOT_BUILT, "no ~/.bash_aliases", proof)
    defines_function = "AJIT_SEGMENT_BOTS_HOME" in aliases.read_text()
    is_sourced = bashrc.is_file() and "bash_aliases" in bashrc.read_text()
    if defines_function and is_sourced:
        return ProbeResult("Claude startup directory", OK, "function installed and sourced", proof)
    if defines_function:
        return ProbeResult("Claude startup directory", FAILING, "defined but not sourced", proof)
    return ProbeResult("Claude startup directory", NOT_BUILT, "function absent", proof)


def probe_git_repository() -> ProbeResult:
    """Rule 9 — work that exists only on this VM is one failed disk from gone."""
    proof = f"git -C {PROJECT_HOME} rev-parse --abbrev-ref HEAD"
    if shutil.which("git") is None:
        return ProbeResult("Git repository", UNMEASURED, "git not on PATH", proof)
    branch = read_command_output(f"git -C {PROJECT_HOME} rev-parse --abbrev-ref HEAD 2>/dev/null")
    if not branch:
        return ProbeResult("Git repository", NOT_BUILT, "not a repository", proof)
    remote = read_command_output(f"git -C {PROJECT_HOME} remote get-url origin 2>/dev/null")
    if not remote:
        return ProbeResult("Git repository", FAILING, f"{branch}, no remote", proof)
    return ProbeResult("Git repository", OK, f"{branch} to {remote}", proof)


def probe_goal_definition() -> ProbeResult:
    """The goal the user gave, recorded verbatim so the board tracks it, not memory."""
    goal = PROJECT_HOME / "docs" / "goal.md"
    proof = f"wc -l {goal}"
    if not goal.is_file():
        return ProbeResult("Goal", NOT_BUILT, "not given yet", proof)
    open_questions = sum(1 for line in goal.read_text().splitlines() if line.startswith("- "))
    return ProbeResult("Goal", OK, f"recorded, {open_questions} open questions", proof)


def probe_architecture_blueprint() -> ProbeResult:
    """The blueprint is the current deliverable: the diagram computed from the registry."""
    proof = "render_mermaid_flowchart() over docs/features.json"
    registry = load_feature_registry()
    if not REGISTRY_PATH.is_file():
        return ProbeResult("Architecture blueprint", NOT_BUILT, "no registry to draw from", proof)
    if not registry.has_categories:
        return ProbeResult("Architecture blueprint", NOT_BUILT, "no foundation blocks yet", proof)
    if not registry.has_features:
        if not registry.has_declared_flow:
            return ProbeResult(
                "Architecture blueprint", NOT_BUILT,
                f"{len(registry.categories)} blocks stand, flow not drawn", proof,
            )
        edges = len([e for e in derive_category_edges(registry) if e[2] != "part-health"])
        return ProbeResult(
            "Architecture blueprint", NOT_BUILT,
            f"block flow proposed, {edges} edges, awaiting the user's verdict", proof,
        )
    return ProbeResult(
        "Architecture blueprint",
        OK,
        f"{len(registry.features)} parts, {count_data_edges(registry)} data edges, "
        f"{count_health_edges(registry)} health",
        proof,
    )


def probe_declared_categories() -> ProbeResult:
    """The foundation blocks. Every feature must belong to exactly one of them."""
    proof = "categories[] in docs/features.json"
    registry = load_feature_registry()
    if not registry.has_categories:
        return ProbeResult("Foundation categories", NOT_BUILT, "none declared", proof)
    from_user = sum(1 for c in registry.categories if c.get("origin") == "user")
    proposed = len(registry.categories) - from_user
    detail = f"{len(registry.categories)} declared, {from_user} from the user"
    if proposed:
        detail += f", {proposed} proposed"
    return ProbeResult("Foundation categories", OK, detail, proof)


def probe_contract_enforcement() -> ProbeResult:
    """The rules must hold without anyone being reminded, so the enforcement is measured too."""
    proof = "test -x .githooks/pre-commit; git config core.hooksPath"
    hook = PROJECT_HOME / ".githooks" / "pre-commit"
    checker = PROJECT_HOME / "dashboard" / "check_contracts.py"
    if not checker.is_file():
        return ProbeResult("Contract enforcement", NOT_BUILT, "no checker", proof)
    if not (hook.is_file() and os.access(hook, os.X_OK)):
        return ProbeResult("Contract enforcement", FAILING, "checker present, hook missing", proof)
    hooks_path = read_command_output(f"git -C {PROJECT_HOME} config core.hooksPath")
    if hooks_path != ".githooks":
        return ProbeResult("Contract enforcement", FAILING, "hook present, git not pointed at it", proof)
    return ProbeResult("Contract enforcement", OK, "pre-commit hook armed", proof)


def probe_defined_features() -> ProbeResult:
    """How many parts the user has described. Empty until they do — never invented."""
    proof = "features[] in docs/features.json"
    if not REGISTRY_PATH.is_file():
        return ProbeResult("Defined features", NOT_BUILT, "no registry file", proof)
    registry = load_feature_registry()
    if not registry.has_features:
        return ProbeResult("Defined features", NOT_BUILT, "0 — awaiting the user's list", proof)
    return ProbeResult(
        "Defined features",
        OK,
        f"{len(registry.features)} features, {len(registry.data_types)} data types",
        proof,
    )


def probe_flow_contract() -> ProbeResult:
    """The hard constraint: the flow must hold together however many parts are added."""
    proof = "find_contract_violations() in dashboard/render_blueprint.py"
    registry = load_feature_registry()
    if not registry.has_declared_flow:
        return ProbeResult("Flow contract", UNMEASURED, "no flow declared between blocks", proof)
    violations = find_contract_violations(registry)
    if violations:
        return ProbeResult("Flow contract", FAILING, f"{len(violations)} violation(s)", proof)
    if not registry.has_features:
        edges = len([e for e in derive_category_edges(registry) if e[2] != "part-health"])
        gaps = len(find_flow_gaps(registry))
        return ProbeResult(
            "Flow contract", NOT_BUILT, f"{edges} block edges proposed, {gaps} gaps, 0 features", proof
        )
    return ProbeResult(
        "Flow contract", OK,
        f"{count_data_edges(registry)} data edges, all resolved "
        f"({count_health_edges(registry)} health emissions, counted apart)",
        proof,
    )


def probe_running_processes() -> ProbeResult:
    """Long-running processes this project owns, excluding this generator itself.

    A naive `pgrep -f <project home>` matches the generator and the shell that
    launched it, which would report phantom services on an empty project. Those
    are excluded by PID so the count means what the tile says it means.
    """
    proof = "scan /proc/*/cmdline for the project home, excluding this generator"
    own_pids = {os.getpid(), os.getppid()}
    matches = []
    for process_dir in Path("/proc").iterdir():
        if not process_dir.name.isdigit() or int(process_dir.name) in own_pids:
            continue
        try:
            cmdline = (process_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")
        except OSError:
            continue
        if str(PROJECT_HOME) in cmdline and "build_status_board.py" not in cmdline:
            matches.append(cmdline.strip())
    if not matches:
        return ProbeResult("Running processes", NOT_BUILT, "none — nothing to run yet", proof)
    return ProbeResult("Running processes", OK, f"{len(matches)} running", proof)


def probe_server_load() -> ProbeResult:
    proof = "cat /proc/loadavg; nproc"
    loadavg = Path("/proc/loadavg")
    if not loadavg.is_file():
        return ProbeResult("Server load", UNMEASURED, "no /proc/loadavg", proof)
    one_minute = float(loadavg.read_text().split()[0])
    cores = read_command_output("nproc") or "?"
    state = OK if cores != "?" and one_minute < float(cores) else FAILING
    return ProbeResult("Server load", state, f"{one_minute:.2f} over {cores} cores", proof)


def probe_server_memory() -> ProbeResult:
    proof = "grep -E 'MemTotal|MemAvailable' /proc/meminfo"
    meminfo = Path("/proc/meminfo")
    if not meminfo.is_file():
        return ProbeResult("Server memory", UNMEASURED, "no /proc/meminfo", proof)
    fields = {}
    for line in meminfo.read_text().splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            fields[key] = int(rest.split()[0]) / 1048576
    if len(fields) < 2:
        return ProbeResult("Server memory", UNMEASURED, "fields absent", proof)
    available, total = fields["MemAvailable"], fields["MemTotal"]
    state = OK if available / total > 0.15 else FAILING
    return ProbeResult("Server memory", state, f"{available:.1f} of {total:.1f} GiB free", proof)


def probe_server_disk() -> ProbeResult:
    proof = f"df -h {PROJECT_HOME}"
    usage = shutil.disk_usage(PROJECT_HOME)
    free_gib = usage.free / 1073741824
    used_percent = 100 * usage.used / usage.total
    state = OK if used_percent < 90 else FAILING
    return ProbeResult("Server disk", state, f"{free_gib:.0f} GiB free, {used_percent:.0f}% used", proof)


PROBES = (
    probe_goal_definition,
    probe_architecture_blueprint,
    probe_project_home,
    probe_project_instructions,
    probe_claude_startup_directory,
    probe_git_repository,
    probe_declared_categories,
    probe_contract_enforcement,
    probe_defined_features,
    probe_flow_contract,
    probe_running_processes,
    probe_server_load,
    probe_server_memory,
    probe_server_disk,
)


def collect_block_completion_results() -> list[ProbeResult]:
    """RL-070: one tile per foundation block (category), green only when every
    part inside it is measured complete.

    Reuses dashboard/build_part_monitor.py's own probes rather than
    re-measuring: measure_parts() already folds RL-067's wiring check and the
    contract checker into each part's rung. block_completion() itself comes
    from dashboard/completion.py -- the single definition RL-070 requires,
    also imported by build_part_monitor.py and part_health_api.py -- so this
    board, the part monitor, and the React board can never silently disagree
    about what "complete" means.

    Guarded on import the same way collect_substrate_results guards runtime/:
    a board build must still succeed, with a tile that says so, if
    dashboard/build_part_monitor.py or dashboard/completion.py cannot be
    imported.
    """
    dashboard_directory = str(Path(__file__).resolve().parent)
    if dashboard_directory not in sys.path:
        sys.path.insert(0, dashboard_directory)
    try:
        from build_part_monitor import category_lookup, measure_parts
        from completion import RUNNING as PART_RUNNING, TESTED as PART_TESTED, block_completion
    except ImportError as failure:
        return [
            ProbeResult(
                "Foundation feature completeness",
                UNMEASURED,
                "build_part_monitor not importable",
                f"import build_part_monitor: {failure}",
            )
        ]

    states = measure_parts()
    categories = category_lookup()
    results = []
    for category_id, category in categories.items():
        owned = [state for state in states if state.category == category_id]
        is_complete, proof = block_completion(owned)
        results.append(
            ProbeResult(
                label=category.get("name", category_id),
                state=OK if is_complete else FAILING,
                value=f"{len([s for s in owned if s.rung in (PART_TESTED, PART_RUNNING)])} "
                f"of {len(owned)} parts TESTED",
                proof=proof,
            )
        )
    return results


def collect_substrate_results() -> list[ProbeResult]:
    """The substrate's own probes (RL-069, Task 14), adapted into this board's tiles.

    The substrate is off-diagram on purpose: docs/features.json describes the 321
    declared parts, and runtime/ is the silicon underneath them, not one of them.
    It still gets measured. SubstrateProbeResult and this board's own ProbeResult
    share their field names by design (Task 14's interface), so adapting one into
    the other is a construction, never a translation.

    Guarded on import: a board build must still succeed before runtime/ exists (or
    while it is broken), and Rule 8 requires that failure be a tile that says so
    rather than a tile that is silently missing.
    """
    repository_root = str(PROJECT_HOME)
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)
    try:
        from runtime.probes.substrate_probes import run_all_substrate_probes
    except ImportError:
        return [
            ProbeResult(
                "Part runtime substrate",
                UNMEASURED,
                "runtime package not importable",
                "import runtime.probes.substrate_probes",
            )
        ]
    return [
        ProbeResult(label=result.label, state=result.state, value=result.value, proof=result.proof)
        for result in run_all_substrate_probes()
    ]


def collect_trading_results() -> list[ProbeResult]:
    """Whether this system is trading, and what is missing before it could be.

    The question an operator actually asks. It goes above the capture tiles on
    purpose: capture is the thing that must never stop, trading is the thing that
    must never start by accident.
    """
    repository_root = str(PROJECT_HOME)
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)
    try:
        from runtime.probes.trading_probes import run_all_trading_probes
    except ImportError:
        return [
            ProbeResult(
                "Trading",
                UNMEASURED,
                "trading probes not importable",
                "import runtime.probes.trading_probes",
            )
        ]
    return [
        ProbeResult(label=result.label, state=result.state, value=result.value, proof=result.proof)
        for result in run_all_trading_probes()
    ]


def collect_capture_results() -> list[ProbeResult]:
    """What the tape has actually captured (Rule 8).

    The capture is the one thing in this project that cannot be caught up on
    later, and it was the one thing this board could not see: 321 blueprint tiles
    and nothing about whether bytes were landing. A capture that quietly stopped
    looked exactly like one that was running.

    Guarded on import for the same reason the substrate probes are: a board build
    must succeed before the capture exists or while it is broken, and Rule 8 wants
    that failure to be a tile saying so rather than a tile silently missing.
    """
    repository_root = str(PROJECT_HOME)
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)
    try:
        from runtime.probes.capture_probes import run_all_capture_probes
    except ImportError:
        return [
            ProbeResult(
                "Market data capture",
                UNMEASURED,
                "capture probes not importable",
                "import runtime.probes.capture_probes",
            )
        ]
    return [
        ProbeResult(label=result.label, state=result.state, value=result.value, proof=result.proof)
        for result in run_all_capture_probes()
    ]


def run_all_probes() -> list[ProbeResult]:
    results = []
    for probe in PROBES:
        try:
            results.append(probe())
        except Exception as error:  # a probe that crashes is unmeasured, not healthy
            results.append(
                ProbeResult(probe.__name__, UNMEASURED, f"probe raised {type(error).__name__}", probe.__name__)
            )
    results.extend(collect_trading_results())
    results.extend(collect_capture_results())
    results.extend(collect_substrate_results())
    return results


STATE_CLASS = {OK: "ok", NOT_BUILT: "unbuilt", FAILING: "fail", UNMEASURED: "unmeasured"}

# Diagrams are pan-and-zoom viewports rather than a fixed picture, so a large
# blueprint can be read close up instead of only seen entire and tiny. Kept as
# raw constants rather than inline template text: the template runs through
# .format(), and doubling every brace in a block of JavaScript is how a page
# stops working for reasons nobody can see.

ZOOM_STYLE = """  .diagram {
    position: relative;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 3px;
    height: clamp(340px, 56vh, 640px);
    overflow: hidden;
    box-shadow: var(--shadow);
    touch-action: none;
    cursor: grab;
  }
  .diagram.dragging { cursor: grabbing; }
  .diagram .mermaid { margin: 0; }
  .diagram svg { max-width: none !important; display: block; }
  .zoom-controls {
    position: absolute;
    top: .55rem;
    right: .55rem;
    display: flex;
    gap: .3rem;
    z-index: 3;
  }
  .zoom-controls button {
    font: 500 .78rem/1 "IBM Plex Mono", ui-monospace, monospace;
    padding: .42rem .58rem;
    min-width: 2.1rem;
    border: 1px solid var(--line-strong);
    background: var(--surface);
    color: var(--ink);
    border-radius: 2px;
    cursor: pointer;
  }
  .zoom-controls button:hover { border-color: var(--accent); color: var(--accent); }
  .zoom-controls button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .zoom-hint {
    position: absolute;
    left: .75rem;
    bottom: .55rem;
    font: .68rem/1.4 "IBM Plex Mono", ui-monospace, monospace;
    color: var(--muted);
    pointer-events: none;
  }"""

ZOOM_SCRIPT = """<script>
(function () {
  var MIN = 0.15, MAX = 10;

  function button(label, title) {
    var b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.title = title;
    b.setAttribute('aria-label', title);
    return b;
  }

  function attach(box) {
    if (box.dataset.zoomReady === '1') return;
    var svg = box.querySelector('svg');
    if (!svg) return;
    box.dataset.zoomReady = '1';

    var viewBox = svg.viewBox && svg.viewBox.baseVal;
    var rect = svg.getBoundingClientRect();
    var nw = (viewBox && viewBox.width) || rect.width || 900;
    var nh = (viewBox && viewBox.height) || rect.height || 500;

    svg.style.transformOrigin = '0 0';
    svg.style.position = 'absolute';
    svg.style.left = '0';
    svg.style.top = '0';
    svg.style.width = nw + 'px';
    svg.style.height = nh + 'px';

    var k = 1, tx = 0, ty = 0;

    function apply() {
      svg.style.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(' + k + ')';
    }

    function fit() {
      var r = box.getBoundingClientRect();
      var scale = Math.min((r.width - 32) / nw, (r.height - 32) / nh);
      if (!isFinite(scale) || scale <= 0) scale = 1;
      k = scale;
      tx = (r.width - nw * k) / 2;
      ty = (r.height - nh * k) / 2;
      apply();
    }

    function zoomAt(cx, cy, factor) {
      var next = Math.min(MAX, Math.max(MIN, k * factor));
      tx = cx - (cx - tx) * (next / k);
      ty = cy - (cy - ty) * (next / k);
      k = next;
      apply();
    }

    box.addEventListener('wheel', function (e) {
      e.preventDefault();
      var r = box.getBoundingClientRect();
      zoomAt(e.clientX - r.left, e.clientY - r.top, e.deltaY < 0 ? 1.14 : 1 / 1.14);
    }, { passive: false });

    var dragging = false, lastX = 0, lastY = 0;
    box.addEventListener('pointerdown', function (e) {
      if (e.target.closest && e.target.closest('.zoom-controls')) return;
      dragging = true; lastX = e.clientX; lastY = e.clientY;
      box.classList.add('dragging');
      try { box.setPointerCapture(e.pointerId); } catch (err) {}
    });
    box.addEventListener('pointermove', function (e) {
      if (!dragging) return;
      tx += e.clientX - lastX; ty += e.clientY - lastY;
      lastX = e.clientX; lastY = e.clientY;
      apply();
    });
    function endDrag(e) {
      if (!dragging) return;
      dragging = false;
      box.classList.remove('dragging');
      try { box.releasePointerCapture(e.pointerId); } catch (err) {}
    }
    box.addEventListener('pointerup', endDrag);
    box.addEventListener('pointercancel', endDrag);
    box.addEventListener('dblclick', fit);

    var controls = document.createElement('div');
    controls.className = 'zoom-controls';
    var out = button('\u2212', 'Zoom out');
    var into = button('+', 'Zoom in');
    var whole = button('Fit', 'Fit the whole diagram');
    out.onclick = function () { var r = box.getBoundingClientRect(); zoomAt(r.width / 2, r.height / 2, 0.8); };
    into.onclick = function () { var r = box.getBoundingClientRect(); zoomAt(r.width / 2, r.height / 2, 1.25); };
    whole.onclick = fit;
    controls.appendChild(out);
    controls.appendChild(into);
    controls.appendChild(whole);
    box.appendChild(controls);

    var hint = document.createElement('div');
    hint.className = 'zoom-hint';
    hint.textContent = 'scroll to zoom \u00b7 drag to pan \u00b7 double-click to fit';
    box.appendChild(hint);

    fit();
    window.addEventListener('resize', fit);
  }

  function scan() {
    var boxes = document.querySelectorAll('.diagram');
    for (var i = 0; i < boxes.length; i++) attach(boxes[i]);
  }

  // Mermaid renders after this script runs, so watch for the SVG appearing and
  // also poll briefly - an observer alone misses a diagram that is already there.
  scan();
  if (window.MutationObserver) {
    new MutationObserver(scan).observe(document.body, { childList: true, subtree: true });
  }
  var tries = 0;
  var timer = setInterval(function () {
    scan();
    if (++tries > 60) clearInterval(timer);
  }, 250);
})();
</script>"""


PAGE_TEMPLATE = """<title>Segment Bots Status Board</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
  :root {{
    --ground: #eceff4;
    --surface: #ffffff;
    --ink: #161a21;
    --muted: #5c6675;
    --line: #d3d9e2;
    --line-strong: #b6bfcd;
    --accent: #0c7c74;
    --warn: #9c6100;
    --fail: #a32b22;
    --shadow: 0 1px 2px rgba(22, 26, 33, .06), 0 8px 24px -16px rgba(22, 26, 33, .28);
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --ground: #0f1319;
      --surface: #171c24;
      --ink: #e4e8ee;
      --muted: #8a93a2;
      --line: #262d38;
      --line-strong: #3a4453;
      --accent: #35c9bc;
      --warn: #e0a22b;
      --fail: #f0776b;
      --shadow: 0 1px 2px rgba(0, 0, 0, .4), 0 8px 24px -16px rgba(0, 0, 0, .8);
    }}
  }}
  :root[data-theme="dark"] {{
    --ground: #0f1319;
    --surface: #171c24;
    --ink: #e4e8ee;
    --muted: #8a93a2;
    --line: #262d38;
    --line-strong: #3a4453;
    --accent: #35c9bc;
    --warn: #e0a22b;
    --fail: #f0776b;
    --shadow: 0 1px 2px rgba(0, 0, 0, .4), 0 8px 24px -16px rgba(0, 0, 0, .8);
  }}

  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--ground);
    color: var(--ink);
    font-family: "IBM Plex Sans", system-ui, -apple-system, sans-serif;
    line-height: 1.55;
    -webkit-font-smoothing: antialiased;
  }}
  .page {{
    max-width: 1180px;
    margin: 0 auto;
    padding: clamp(1.5rem, 4vw, 3.5rem) clamp(1rem, 4vw, 2rem) 5rem;
    display: flex;
    flex-direction: column;
    gap: 2.5rem;
  }}

  .masthead {{ display: flex; flex-direction: column; gap: .9rem; }}
  .eyebrow {{
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: .72rem;
    letter-spacing: .14em;
    text-transform: uppercase;
    color: var(--muted);
  }}
  h1 {{
    font-family: Archivo, system-ui, sans-serif;
    font-weight: 700;
    font-size: clamp(1.9rem, 5vw, 2.9rem);
    letter-spacing: -.025em;
    line-height: 1.05;
    margin: 0;
    text-wrap: balance;
  }}
  .verdict {{
    font-size: 1.02rem;
    color: var(--muted);
    max-width: 62ch;
    margin: 0;
  }}
  .stamp {{
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: .5rem 1.25rem;
    padding-top: .9rem;
    border-top: 1px solid var(--line);
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: .82rem;
    color: var(--muted);
  }}
  .stamp strong {{ color: var(--ink); font-weight: 500; font-variant-numeric: tabular-nums; }}

  .tally {{ display: flex; flex-wrap: wrap; gap: .55rem; }}
  .tally span {{
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: .74rem;
    letter-spacing: .07em;
    text-transform: uppercase;
    padding: .3rem .65rem;
    border: 1px solid var(--line-strong);
    border-radius: 2px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }}
  .tally span.has-ok {{ border-color: var(--accent); color: var(--accent); }}
  .tally span.has-fail {{ border-color: var(--fail); color: var(--fail); }}

  section {{ display: flex; flex-direction: column; gap: 1rem; }}
  h2 {{
    font-family: Archivo, system-ui, sans-serif;
    font-weight: 600;
    font-size: .82rem;
    letter-spacing: .12em;
    text-transform: uppercase;
    color: var(--muted);
    margin: 0;
  }}

  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(255px, 1fr));
    gap: .85rem;
  }}
  .tile {{
    background: var(--surface);
    border: 1px solid var(--line);
    border-left: 3px solid var(--line-strong);
    border-radius: 3px;
    padding: .95rem 1.05rem 1rem;
    display: flex;
    flex-direction: column;
    gap: .45rem;
    box-shadow: var(--shadow);
  }}
  .tile.ok {{ border-left-color: var(--accent); }}
  .tile.fail {{ border-left-color: var(--fail); }}
  .tile.unbuilt {{ background: none; border-style: dashed; box-shadow: none; }}
  .tile.unmeasured {{ background: none; border-style: dashed; box-shadow: none; }}

  .tile-label {{ font-weight: 600; font-size: .96rem; display: flex; align-items: center; gap: .5rem; }}
  .dot {{
    display: inline-block; width: .6rem; height: .6rem; border-radius: 50%;
    flex: none; border: 1px solid var(--line-strong);
  }}
  .dot-green {{ background: var(--accent); border-color: var(--accent); }}
  .dot-red {{ background: var(--fail); border-color: var(--fail); }}
  .tile-state {{
    align-self: flex-start;
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: .68rem;
    letter-spacing: .1em;
    padding: .18rem .45rem;
    border-radius: 2px;
    border: 1px solid currentColor;
    color: var(--muted);
  }}
  .ok .tile-state {{ color: var(--accent); }}
  .fail .tile-state {{ color: var(--fail); }}
  .unbuilt .tile-state {{ color: var(--warn); }}
  .tile-value {{ font-size: .9rem; color: var(--ink); font-variant-numeric: tabular-nums; }}
  .unbuilt .tile-value, .unmeasured .tile-value {{ color: var(--muted); }}
  .tile-proof {{
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: .7rem;
    color: var(--muted);
    overflow-x: auto;
    white-space: pre;
    padding-top: .3rem;
    border-top: 1px dashed var(--line);
  }}

  .empty-frame {{
    border: 1px dashed var(--line-strong);
    border-radius: 3px;
    padding: clamp(2rem, 6vw, 3.5rem) 1.5rem;
    text-align: center;
    display: flex;
    flex-direction: column;
    gap: .6rem;
    align-items: center;
  }}
  .empty-frame p {{ margin: 0; max-width: 52ch; color: var(--muted); font-size: .94rem; }}
  .empty-frame .headline {{
    font-family: Archivo, system-ui, sans-serif;
    font-weight: 600;
    font-size: 1.05rem;
    color: var(--ink);
    letter-spacing: -.01em;
  }}

  .diagram-meta {{
    display: flex;
    flex-wrap: wrap;
    gap: .5rem 1.4rem;
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: .76rem;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }}
{zoom_style}
  .violations {{
    border: 1px solid var(--fail);
    border-left: 3px solid var(--fail);
    border-radius: 3px;
    padding: .85rem 1.05rem;
  }}
  .violations-head {{
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: .72rem;
    letter-spacing: .1em;
    text-transform: uppercase;
    color: var(--fail);
    margin-bottom: .4rem;
  }}
  .violations ul {{ margin: 0; padding-left: 1.1rem; font-size: .88rem; }}

  .gaps {{
    border: 1px dashed var(--warn);
    border-left: 3px solid var(--warn);
    border-radius: 3px;
    padding: .85rem 1.05rem;
  }}
  .gaps-head {{
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: .72rem;
    letter-spacing: .1em;
    text-transform: uppercase;
    color: var(--warn);
    margin-bottom: .4rem;
  }}
  .gaps ul {{ margin: 0; padding-left: 1.1rem; font-size: .88rem; color: var(--muted); }}
  .plane-head {{
    font-family: Archivo, system-ui, sans-serif;
    font-weight: 600;
    font-size: .95rem;
    margin: .4rem 0 0;
    letter-spacing: -.01em;
  }}

  .category-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(255px, 1fr));
    gap: .85rem;
  }}
  .category {{
    background: var(--surface);
    border: 1px solid var(--line);
    border-top: 3px solid var(--accent);
    border-radius: 3px;
    padding: .95rem 1.05rem 1.05rem;
    display: flex;
    flex-direction: column;
    gap: .35rem;
    box-shadow: var(--shadow);
  }}
  .category.proposed {{ border-top-color: var(--warn); border-style: dashed; box-shadow: none; }}
  .category-name {{ font-weight: 600; font-size: .96rem; }}
  .category-origin {{
    align-self: flex-start;
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: .64rem;
    letter-spacing: .12em;
    text-transform: uppercase;
    color: var(--accent);
  }}
  .category.proposed .category-origin {{ color: var(--warn); }}
  .category p {{ margin: 0; font-size: .86rem; color: var(--muted); }}
  .category-scope {{
    align-self: flex-start;
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: .62rem;
    letter-spacing: .08em;
    text-transform: uppercase;
    color: var(--muted);
    border: 1px solid var(--line);
    border-radius: 2px;
    padding: .12rem .35rem;
  }}

  .table-wrap {{ overflow-x: auto; }}
  .feature-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: .88rem;
    min-width: 640px;
  }}
  .feature-table th {{
    text-align: left;
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: .68rem;
    letter-spacing: .1em;
    text-transform: uppercase;
    color: var(--muted);
    font-weight: 500;
    padding: .5rem .7rem;
    border-bottom: 1px solid var(--line-strong);
  }}
  .feature-table td {{
    padding: .6rem .7rem;
    border-bottom: 1px solid var(--line);
    vertical-align: top;
    color: var(--muted);
  }}
  .feature-table td:first-child {{ color: var(--ink); white-space: nowrap; }}
  .feature-table .mono {{
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: .78rem;
  }}
  .origin-tag {{
    display: inline-block;
    margin-left: .5rem;
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: .6rem;
    letter-spacing: .08em;
    text-transform: uppercase;
    color: var(--accent);
    border: 1px solid currentColor;
    border-radius: 2px;
    padding: .1rem .3rem;
    vertical-align: middle;
  }}

  footer {{
    border-top: 1px solid var(--line);
    padding-top: 1.1rem;
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: .74rem;
    color: var(--muted);
    line-height: 1.8;
    overflow-x: auto;
  }}
</style>

<div class="page">
  <header class="masthead">
    <div class="eyebrow">ajit-segment-bots</div>
    <h1>Segment Bots Status Board</h1>
    <p class="verdict">{verdict}</p>
    <div class="tally">{tally}</div>
    <div class="stamp">
      <span>measured <strong>{measured_at}</strong></span>
      <span>probes run <strong>{probe_count}</strong></span>
      <span>host <strong>{project_home}</strong></span>
    </div>
  </header>

  <section>
    <h2>Probed state</h2>
    <div class="grid">{tiles}</div>
  </section>

  <section>
    <h2>Architecture blueprint</h2>
    {blueprint_region}
  </section>

  <section>
    <h2>Foundation feature completeness — a dot per block (RL-070)</h2>
    <p>Green only when every part in that block has climbed to TESTED (a source file and a
    test naming it) and neither the contract checker nor the built-vs-blueprint wiring check
    (RL-067) named it. Red covers both genuinely unfinished and built-but-unprobed on purpose
    — the distinction lives in the proof under each tile, not in the colour. Per-part dots are
    on the part monitor; this is the block-level rollup. Measured by
    dashboard/build_part_monitor.py's block_completion(), reused here rather than re-computed.</p>
    <div class="grid">{block_tiles}</div>
  </section>

  <section>
    <h2>Part runtime substrate — off-diagram (RL-069)</h2>
    <p>The blueprint above describes the circuit; this is the silicon underneath it.
    Not a part, never a cell on the part monitor — measured here instead, by the
    probes in runtime/probes/substrate_probes.py.</p>
    <div class="grid">{substrate_tiles}</div>
  </section>

  <footer>
    Generated by dashboard/build_status_board.py — every tile above is the result of a probe that ran.<br>
    A check that did not run reads NOT MEASURED. Nothing here is asserted by hand.<br>
    Regenerate: python3 dashboard/build_status_board.py
  </footer>
</div>
{zoom_script}
"""


def render_tile(result: ProbeResult) -> str:
    return (
        f'<article class="tile {STATE_CLASS[result.state]}">'
        f'<div class="tile-label">{html.escape(result.label)}</div>'
        f'<div class="tile-state">{html.escape(result.state)}</div>'
        f'<div class="tile-value">{html.escape(result.value)}</div>'
        f'<div class="tile-proof">{html.escape(result.proof)}</div>'
        f"</article>"
    )


def render_dot_tile(result: ProbeResult) -> str:
    """RL-070's two-colour dot, on the same tile shape the rest of the board
    uses. OK carries the green dot, everything else (never inferred green)
    carries red -- the proof underneath is what tells red apart from red.
    """
    is_complete = result.state == OK
    dot = f'<span class="dot dot-{"green" if is_complete else "red"}"></span>'
    return (
        f'<article class="tile {STATE_CLASS[result.state]}">'
        f'<div class="tile-label">{dot}{html.escape(result.label)}</div>'
        f'<div class="tile-value">{html.escape(result.value)}</div>'
        f'<div class="tile-proof">{html.escape(result.proof)}</div>'
        f"</article>"
    )


def render_tally(results: list[ProbeResult]) -> str:
    order = (OK, NOT_BUILT, FAILING, UNMEASURED)
    chips = []
    for state in order:
        count = sum(1 for r in results if r.state == state)
        modifier = ""
        if state == OK and count:
            modifier = " has-ok"
        if state == FAILING and count:
            modifier = " has-fail"
        chips.append(f'<span class="{modifier.strip()}">{count} {html.escape(state.lower())}</span>')
    return "".join(chips)


def find_state(results: list[ProbeResult], label: str) -> str:
    for result in results:
        if result.label == label:
            return result.state
    return UNMEASURED


def compose_verdict(results: list[ProbeResult]) -> str:
    """Derive the headline from the probes. Never assert a stage the board cannot see."""
    failing = sum(1 for r in results if r.state == FAILING)
    goal_state = find_state(results, "Goal")
    blueprint_state = find_state(results, "Architecture blueprint")

    if goal_state != OK:
        stage = "No goal has been given yet, so there is nothing to design against."
    elif blueprint_state != OK:
        stage = (
            "The goal is recorded and the blueprint is the current deliverable. It has not "
            "been drawn yet — the feature catalogue and the data-flow contract are still "
            "open questions the user has to answer, not defaults to be picked."
        )
    else:
        stage = "The blueprint exists. Every tile below is measured against what is on the server."

    if failing:
        return f"{failing} probe(s) failing. {stage}"
    return f"{stage} Anything unmeasured reads as its own state below, never as healthy."


def render_board(results: list[ProbeResult], block_results: list[ProbeResult]) -> str:
    # run_all_probes() appends the substrate's results after every PROBES-derived
    # one (collect_substrate_results, above), always in that order -- so this is
    # the split point between the two tile groups, not a guess about where they
    # start. It gives the substrate its own heading without SubstrateProbeResult
    # ever needing to say "I am off-diagram" about itself.
    #
    # block_results (RL-070) is kept out of results/compose_verdict/render_tally
    # on purpose: its FAILING is "not yet measured complete", which is the
    # correct, expected reading for a blueprint that is not built yet -- folding
    # it into the top verdict's failure count would read as broken probes on a
    # board that has none, exactly the false alarm Rule 8 exists to prevent.
    base_results = results[: len(PROBES)]
    substrate_results = results[len(PROBES) :]
    return PAGE_TEMPLATE.format(
        verdict=html.escape(compose_verdict(results)),
        blueprint_region=render_blueprint_section(load_feature_registry()),
        tally=render_tally(results),
        measured_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        probe_count=len(results),
        project_home=html.escape(str(PROJECT_HOME)),
        zoom_style=ZOOM_STYLE,
        zoom_script=ZOOM_SCRIPT,
        tiles="".join(render_tile(r) for r in base_results),
        block_tiles="".join(render_dot_tile(r) for r in block_results),
        substrate_tiles="".join(render_tile(r) for r in substrate_results),
    )


def write_board() -> Path:
    results = run_all_probes()
    block_results = collect_block_completion_results()
    BOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    BOARD_PATH.write_text(render_board(results, block_results))
    for result in results:
        print(f"{result.state:<13} {result.label:<26} {result.value}")
    n_green = sum(1 for r in block_results if r.state == OK)
    print(f"\nblock dots (RL-070): {n_green} of {len(block_results)} blocks green")
    for result in block_results:
        print(f"  {'GREEN' if result.state == OK else 'RED  '} {result.label}: {result.proof}")
    print(f"\nwrote {BOARD_PATH}")
    return BOARD_PATH


if __name__ == "__main__":
    write_board()
