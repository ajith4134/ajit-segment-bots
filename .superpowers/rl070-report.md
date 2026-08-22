# RL-070 report — a dot per feature, and the wiring checked against the diagram

Branch: `phase-1-market-data-feed`. No commits made (not asked for). Working
tree changes only, listed below with real command output for every claim.

## What was built

### 1. `runtime/part_declaration.py` — how a built part states its real wiring

Added, alongside the existing `PartDeclaration` / `load_declaration_from_blueprint`:

- `PART_DECLARATION_ATTRIBUTE = "PART_DECLARATION"` — the one name a built
  part's module is required to expose its wiring under.
- `ModuleDeclaresNoWiring(Exception)` — raised whether the attribute is simply
  absent or present but the wrong type. Both are the same fact to the checker:
  this module cannot be compared against the blueprint, so it must not be read
  as agreeing with it.
- `read_declaration_from_module(module) -> PartDeclaration` — reads
  `module.PART_DECLARATION` and validates its shape.

**Design decision, and why:** I read `runtime/part_declaration.py` first, as
instructed. It already has exactly one shape for "what a part declares"
(`PartDeclaration`) and one function that reads it from the blueprint side
(`load_declaration_from_blueprint`). The natural, symmetric choice — and the
one the task suggested checking first — is that a built part's own module
exposes a module-level `PART_DECLARATION` in that same shape. I checked what
actually exists before committing to this: today there are zero built parts
(all 321 are `DECLARED`), so there was no existing convention to contradict or
confirm against. I chose this because:

- It reuses one dataclass on both sides of the comparison (blueprint and
  built), so there is no second shape that could silently drift from the
  first — the same argument the module's own docstring makes for
  `PartDeclaration` itself ("One shape, no privileged parts").
- It matches T-4 (a part knows nothing about the circuit) and T-1 (every
  feature is the same shape): the convention is "one attribute, one shape,"
  not "however this part's author wants to expose it."
- A part with a source file but no `PART_DECLARATION` is treated as
  unverifiable, not as passing — never green by inference.

### 2. `dashboard/build_part_monitor.py` — the wiring probe, and both dots

- `find_implementation_file()` extracted from `probe_part_rung()` so the rung
  probe and the wiring probe agree on "this part's file" from one definition.
- `import_part_module()` — imports a built part's own file the way this
  repo's own tests already import a standalone script (its directory on
  `sys.path`, then a plain import by file stem — the same pattern
  `tests/dashboard/test_build_status_board.py` uses).
- `WiringCheck` (dataclass) and `check_wiring_against_blueprint(registry,
  sources)` — RL-067's probe. For every part with a source file, it loads the
  blueprint's declaration and the module's own `PART_DECLARATION` and
  compares `consumes`/`produces`. A mismatch, or a built part that cannot
  state its own wiring at all, goes into `.mismatches` keyed by part id, with
  both sides named in the message. Parts with no source file are absent from
  the check entirely (nothing built, nothing to disagree with). When nothing
  is built, `checked_part_ids == ()` and `mismatches == {}` — the caller is
  required to print/render the distinction between "nothing to compare" and
  "compared, 0 disagreed," never collapse the two.
- `_measure_build_state()` folds the wiring result into each part's rung:
  a wiring mismatch (or an unreadable declaration) now paints the part
  `FAILING`, the same red rung the existing contract checker already uses —
  so RL-067 red and R-01 red are the same visible state, distinguished by
  their proof text.
- `part_is_measured_complete(state)` — the part dot: green only at `TESTED`
  or `RUNNING`. `FAILING` never reaches those rungs (the override happens
  before this is asked), so a wiring-broken part cannot render green.
- `block_completion(owned)` — the block dot: green only when every part in
  the block is green; an empty block is red, not vacuously green.
- Rendering: `render_dot()` for the coloured dot with its proof in `title`,
  wired into `render_cell()` (per-part) and `render_block()` (per-block, with
  the proof also printed as visible text under the header, not only in a
  hover title — Rule 8's "visible in the artefact"). `render_wiring_check_note()`
  renders the RL-067 probe's own honest summary, including the
  nothing-built-yet case, into the page.
- CLI (`__main__`) now prints the wiring-check summary and the dot counts
  alongside the existing ladder counts.

### 3. `dashboard/build_status_board.py` — the block-level rollup

Added `collect_block_completion_results()` (guarded against
`build_part_monitor` being unimportable, the same guard
`collect_substrate_results()` already uses for `runtime/`), `render_dot_tile()`,
and a new "Foundation feature completeness — a dot per block (RL-070)" section
between "Architecture blueprint" and the substrate section.

**Design decision — why the block rollup goes here, and why it is kept out of
the top verdict/tally:** the status board is the project-wide overview; the
part monitor is the per-part detail. Per-part dots (321 of them) belong only
on the part monitor — duplicating all 321 on the status board would just be
noise at the wrong altitude. The 27 block dots are exactly the board's
existing altitude (it already summarises at block level in "Architecture
blueprint"), so they go here, reusing `build_part_monitor`'s own
`measure_parts()` / `block_completion()` rather than re-measuring — the two
boards can never silently disagree about what "complete" means because there
is one function computing it.

I deliberately did **not** fold the block-completion results into
`run_all_probes()`'s flat list that feeds `compose_verdict()` and
`render_tally()`. Those treat `FAILING` as "something is broken" (a hook
misconfigured, a real contract violation). A block being "not yet measured
complete" at the blueprint stage is the expected, correct state — folding it
in would make the top-line verdict read "27 probe(s) failing," which is a
false alarm exactly of the kind Rule 8 exists to prevent. The block-completion
tiles get their own section, their own dot vocabulary (green/red only,
`OK`/`FAILING` reused as the underlying two states but rendered as a dot, not
a state chip), and are counted separately in `write_board()`'s printed output.

### 4. Tests

- `tests/runtime/test_part_declaration.py` — 3 new tests for
  `read_declaration_from_module` / `ModuleDeclaresNoWiring` (present, absent,
  wrong type).
- `tests/dashboard/test_build_part_monitor.py` (new file) — 8 tests. Because
  `find_source_files()` scans the real project tree, not an isolated
  `tmp_path`, the fixture `scratch_part_files` writes real files under a
  scratch directory at the project root (outside `tests/`, so pytest never
  collects it, and outside `build_part_monitor.EXCLUDED_DIRS`) and removes
  them — and clears `sys.modules`/`sys.path` — in a `finally` block. Covers:
  a part with no source file is `DECLARED`/red; nothing built means the
  wiring check reports "nothing to compare" (`checked_part_ids == ()`,
  `mismatches == {}`); a real source+test file pair turns the dot green
  *and reversibly* turns it back red on removal (one test does both
  directions); a wiring mismatch paints the part red and names both the
  blueprint's and the module's `consumes`/`produces`; a built part exposing
  no `PART_DECLARATION` is red, not green; block completion is green only
  when every part is green; an empty block is red.

## Verification run, with real output

### 1. Regenerate the part monitor — every dot should be red, every block red

```
$ python3 dashboard/build_part_monitor.py
DECLARED      321
IMPLEMENTED     0
TESTED          0
RUNNING         0
FAILING         0
NOT MEASURED    0

wiring vs blueprint (RL-067): 0 parts have a source file — nothing to compare yet

dots (RL-070): 0/321 parts green, 0/27 blocks green

wrote /home/anushadudekula71/ajit-segment-bots/dashboard/part-monitor.html
```

Confirmed in the rendered HTML too (screenshot taken, see check 3): the
summary tiles read `0/321 parts green`, `0/27 blocks green`; the dots legend
reads `GREEN 0` / `RED 321`; every block header carries a red dot; every part
cell carries a red dot; the wiring note reads "0 parts have a source file
yet ... not zero mismatches, nothing to check." Nothing renders green.

```
$ python3 dashboard/build_status_board.py   (tail)
block dots (RL-070): 0 of 27 blocks green
  RED   Paper trading on live data: 0 of 10 parts are TESTED; not yet: ...
  ... (all 27 blocks, all RED, each naming its unfinished parts) ...
wrote /home/anushadudekula71/ajit-segment-bots/dashboard/status-board.html
```

### 2. Prove a green dot is reachable, and reversible

Manual, then codified as a test. Using the real blueprint part
`kline-window-builder` (blueprint declares `consumes=["market-data"]`,
`produces=["kline-window","part-health"]`), I wrote a real source file and a
real test file under a scratch directory at the project root, re-measured,
then deleted them:

```
$ python3 dashboard/build_part_monitor.py   (after adding the scratch files)
TESTED          1
...
wiring vs blueprint (RL-067): checked 1 built part(s), 0 mismatch(ed)
dots (RL-070): 1/321 parts green, 0/27 blocks green
```

Grep of the rendered HTML confirmed the cell:
`<div class="cell rung-tested" title="K-line window builder — TESTED — ...">
<span class="cell-top"><span class="dot dot-green" title="complete: ...">`.

I then changed the scratch module's `PART_DECLARATION` to
`consumes=("wrong-data",)` and re-measured directly against
`_measure_build_state()`:

```
wiring.mismatches == {
  'kline-window-builder': "kline-window-builder: blueprint declares
  consumes=['market-data'] produces=['kline-window', 'part-health'];
  tmp_scratch_rl070/kline_window_builder.py declares
  consumes=['wrong-data'] produces=['kline-window', 'part-health']"
}
state.rung == FAILING
part_is_measured_complete(state) == False
```

Both sides named, dot stays red. I then removed the scratch directory
entirely and re-ran the monitor — back to `DECLARED 321`, `0/321 parts
green`, confirming the colour is reversible, not a one-way artefact. This
whole sequence is now a standing test
(`tests/dashboard/test_build_part_monitor.py::test_a_source_file_and_a_matching_test_file_turn_the_dot_green`
and `::test_removing_the_scratch_files_turns_the_dot_red_again`), not just a
manual observation.

```
$ .venv/bin/python -m pytest tests/dashboard/test_build_part_monitor.py tests/runtime/test_part_declaration.py -q
..................
18 passed in 6.14s
```

### 3. Rendering — Chromium + fonts from `~/.local/pwdeps`

`dashboard/web/verify_status_board_renders.sh` (covers `status-board.html`,
which I changed):

```
$ dashboard/web/verify_status_board_renders.sh "file://.../status-board.html" ...
{
  "tileCount": 47,
  "substrateHeading": "Part runtime substrate — off-diagram (RL-069)",
  "substrateTileCount": 6,
  ... all 6 substrate chips OK/NOT MEASURED as expected, proofs present ...
  "font": "Archivo, system-ui, sans-serif",
  "errors": []
}
screenshot: .../status-board-render.png
```

`47` tiles = 14 base probes + 27 new block-completion tiles + 6 substrate
tiles — confirms the new section rendered and did not silently disappear.
Zero console errors. I additionally screenshotted the new "Foundation feature
completeness" section directly (full page, not just the substrate crop the
script itself takes) and looked at it: fonts render (not the invisible-glyph
failure mode), every one of the 27 blocks shows a red dot, red left border,
and its proof text ("0 of N parts are TESTED; not yet: ...") visible under
the tile, not only in a hover title.

I also screenshotted `dashboard/part-monitor.html` directly (no dedicated
verify script names it, so I reused the same Chromium+fonts harness): 0
console errors, summary stats read `0/321 parts green`, `0/27 blocks green`,
the dots legend and wiring-check note both render as text, and per-part
cells show red dots (checked "Instrument selector" specifically).

As a backward-compatibility check (I did not change `dashboard/board.html`'s
generator, but it depends on `measure_parts()`, which I changed the internals
of): rebuilt it and ran `dashboard/web/verify_board_renders.sh` —
`"consoleErrors": []`, `"failedRequests": []`, `"failing": 0`, all 321 cells
present. No regression.

### 4. Full test suite and contract check

```
$ .venv/bin/python -m pytest tests -q
160 passed in 19.11s

$ .venv/bin/python -W error::ResourceWarning -m pytest tests -q
160 passed in 19.07s

$ python3 dashboard/check_contracts.py
321 features, 27 categories: all contracts hold
```

160 = the baseline 149 plus 11 new tests (8 in `tests/dashboard/`, 3 in
`tests/runtime/test_part_declaration.py`). Clean under
`-W error::ResourceWarning`.

```
$ git status --porcelain
 M dashboard/build_part_monitor.py
 M dashboard/build_status_board.py
 M runtime/part_declaration.py
 M tests/runtime/test_part_declaration.py
?? tests/dashboard/test_build_part_monitor.py
```

No scratch directories left behind (`ls | grep -i rl070` /
`grep -i scratch` both empty). The three generated HTML files
(`board.html`, `part-monitor.html`, `status-board.html`) are all
`.gitignore`d, confirmed with `git check-ignore -v`, and were not committed.

## Concerns

- **`FAILING` is now shared by three different reasons** on the part monitor:
  R-01 blueprint self-consistency violations (`parts_in_violation`), RL-067
  built-vs-blueprint wiring mismatches, and (once a real part exists) an
  unreadable `PART_DECLARATION`. All three render identically red with a
  proof string — I kept them merged into one rung rather than adding a fifth
  rung, because the task asked for the wiring probe to "paint the part red,"
  which the existing `FAILING` rung already does, and RL-070 doesn't ask for
  a new rung. If that turns out to be too coarse once real mismatches start
  happening, the proof text is what disambiguates them today; a dedicated
  state would be a small follow-up.
- **`find_source_files()` (and therefore the wiring check) scans the whole
  project tree** except `{.git, docs, dashboard, __pycache__, .githooks}` —
  notably not `.venv` or `tests/`. This is pre-existing behaviour I did not
  change, but it means a stray file anywhere in the repo whose name happens
  to match a part's `name_variants()` could in principle be picked up as that
  part's implementation. Out of scope for RL-070, flagging because the wiring
  check now *executes* whatever file gets matched (importing it), which is a
  slightly higher-stakes version of the existing risk.
- **I did not touch the React board** (`dashboard/web/src/*`, `board.html`'s
  generator, `part_health_api.py`). RL-070's dots are not on that board. The
  task's "where the work goes" section named only
  `build_part_monitor.py`/`build_status_board.py`, so I read this as
  in-scope only for the two HTML-generating scripts; the React board reads
  `measure_parts()` output (rung/proof) exactly as before, which is why it
  still passes `verify_board_renders.sh` unchanged.

## Files touched

- `/home/anushadudekula71/ajit-segment-bots/runtime/part_declaration.py`
- `/home/anushadudekula71/ajit-segment-bots/dashboard/build_part_monitor.py`
- `/home/anushadudekula71/ajit-segment-bots/dashboard/build_status_board.py`
- `/home/anushadudekula71/ajit-segment-bots/tests/runtime/test_part_declaration.py`
- `/home/anushadudekula71/ajit-segment-bots/tests/dashboard/test_build_part_monitor.py` (new)

---

## Addendum — the coordinator's correction: stop importing part code

The coordinator flagged, correctly, that `import_part_module()` +
`read_declaration_from_module()` made **generating a dashboard execute part
code**: `importlib.import_module(source_path.stem)` after putting the file's
own directory on `sys.path`. Once real parts exist that would run their
import-time side effects (sockets, threads, a forkserver) on every board
build, let a hanging or crashing import take the board generator down with
it, and execute a stray file that merely collided in name with a part's
`name_variants()` — all real, and the first one alone was disqualifying: a
board that reports on the system must not operate it. `sys.path` also grew a
directory per part and never shrank. All correct; this addendum is the fix.

### What changed

**`runtime/part_declaration.py`**

- Removed `read_declaration_from_module()` entirely — nothing else in the
  codebase used it (confirmed by `grep -rn "read_declaration_from_module"`
  before deleting: only this module and its now-removed tests referenced
  it), so per the coordinator's instruction it was deleted rather than left
  as a second, unused way to do the same thing.
- Added `read_declaration_from_source(source_path: pathlib.Path) ->
  PartDeclaration`, which never imports or executes the file. It:
  1. Reads the file's text and `ast.parse`s it — a `SyntaxError` becomes
     `ModuleDeclaresNoWiring("... does not parse: ...")`.
  2. Walks the module body for a top-level `PART_DECLARATION = ...`
     assignment — absent becomes `ModuleDeclaresNoWiring("... has no
     PART_DECLARATION assignment ...")`.
  3. Requires the assigned value to be a call whose callee resolves (by bare
     name only — `PartDeclaration(...)` or `x.PartDeclaration(...)`, never by
     resolving an import) to `PartDeclaration` — anything else becomes
     `ModuleDeclaresNoWiring("... is not a PartDeclaration(...) call")`.
  4. Requires every argument to be a keyword (no positional args, no
     `**kwargs`) and every keyword's value to pass `ast.literal_eval` — a
     value that does not (an enum-attribute access, a call, a name
     reference, an f-string) becomes `ModuleDeclaresNoWiring("...'s '<field>'
     argument is not a literal (...) -- a built part's declaration must be
     written as literal values, never computed or resolved at runtime")`.
  5. Builds the `PartDeclaration`, converting the three enum fields from
     their literal string values via `ResourceClass(...)` /
     `RateRisk(...)` / `SkippedTickEffect(...)` — an unknown string raises
     `ValueError`, wrapped the same way.
- **New rule, stated as one, not implied**: a built part's `PART_DECLARATION`
  must be a literal `PartDeclaration(...)` call, every field a keyword,
  every value a literal. Concretely this means the three enum-typed fields
  are written as their raw string value (`resource_class="bandwidth-bound"`),
  **not** `ResourceClass.BANDWIDTH_BOUND` — an enum-attribute access is a
  name resolved at runtime, which `ast.literal_eval` correctly refuses, and
  refusing it is the point: a declaration that cannot be read without running
  the program is a declaration this checker cannot trust.

**`dashboard/build_part_monitor.py`**

- Removed `import_part_module()` and the `import importlib` it needed.
- `check_wiring_against_blueprint()` now calls
  `part_declaration.read_declaration_from_source(source_path)` directly —
  no module object, no import, no `sys.path` mutation beyond the one,
  idempotent, repository-root insertion `_part_declaration_module()` already
  did to import `runtime.part_declaration` itself (our own trusted code, not
  a part's). That single insertion is the only `sys.path` write left in the
  whole wiring path — confirmed by `grep -n "sys.path" dashboard/build_part_monitor.py`.
- Updated the wiring-check docstring and the on-page note
  (`render_wiring_check_note`) to say the read is static — "parsed with ast,
  never imported" — so the artefact itself states the guarantee, not just
  the code.

**`tests/runtime/test_part_declaration.py`**

- Replaced the 3 `read_declaration_from_module` tests (which used
  `types.SimpleNamespace` stand-ins) with 9 tests against
  `read_declaration_from_source`, using real files under `tmp_path`:
  a valid literal declaration is read correctly, and — to prove no import
  happens — the file also contains `import
  this_module_does_not_exist_anywhere`, which would raise `ImportError` if
  the file were ever executed; it never is. Then: no assignment, not a
  `PartDeclaration(...)` call, positional arguments, a file that does not
  parse, and — **parametrized, four cases** — a non-literal argument:
  an enum-attribute access (`ResourceClass.BANDWIDTH_BOUND`), a call
  expression (`tuple([...])`), a bare name reference inside a tuple, and an
  f-string. Every one asserts the raised message contains "literal".

**`tests/dashboard/test_build_part_monitor.py`**

- `_write_declaration()` (used by the green/reversible/mismatch tests)
  rewritten to emit the literal convention (`resource_class="bandwidth-bound"`
  etc.) instead of enum-attribute access.
- The `scratch_part_files` fixture and the module docstring updated: no
  `sys.modules`/`sys.path` cleanup remains, because nothing is imported
  anymore — there is nothing to clean up there.
- Added `test_a_built_part_with_a_non_literal_declaration_is_red_not_green`:
  a scratch part whose `resource_class` is written as
  `ResourceClass.BANDWIDTH_BOUND` (the natural, wrong-for-this-purpose thing
  a future author would write) is asserted `FAILING`, not green, with
  `"literal"` in its proof.

### Re-verification, with real output

Full suite:

```
$ .venv/bin/python -m pytest tests -q
167 passed in 19.92s

$ .venv/bin/python -W error::ResourceWarning -m pytest tests -q
167 passed in 19.90s
```

167 = the earlier 160 plus 7 net new (9 new source-based tests replacing 3
module-based ones in `test_part_declaration.py`, +1 new dashboard-level
non-literal test).

```
$ python3 dashboard/check_contracts.py
321 features, 27 categories: all contracts hold
```

Both boards regenerated, honest-empty state unchanged:

```
$ python3 dashboard/build_part_monitor.py
DECLARED      321
...
wiring vs blueprint (RL-067): 0 parts have a source file — nothing to compare yet
dots (RL-070): 0/321 parts green, 0/27 blocks green

$ python3 dashboard/build_status_board.py   (tail)
block dots (RL-070): 0 of 27 blocks green
  RED   Paper trading on live data: ...
  ... (all 27 RED) ...
```

`dashboard/web/verify_status_board_renders.sh` re-run against the
regenerated page: `"errors": []`, substrate section still intact (6 tiles,
all valid states). 27 `RED` block lines confirmed via `grep -c "RED "` on the
CLI output (29 matches including the two mentions in the summary line and
header — the 27 per-block lines are the substance).

**Green-is-reachable, redone with the static reader, proving no import
occurs:** wrote a real scratch part
(`rl070_manual_check/kline_window_builder.py`) for the real blueprint part
`kline-window-builder`, deliberately containing `import
this_module_definitely_does_not_exist_anywhere_12345` as its first line —
this is the load-bearing part of the check: if the board generator imported
this file, that line would raise `ModuleNotFoundError` and the part would
report unverifiable, not matched. It did not:

```
TESTED          1
wiring vs blueprint (RL-067): checked 1 built part(s), 0 mismatch(ed)
dots (RL-070): 1/321 parts green, 0/27 blocks green
```

Grepped the rendered HTML: `<div class="cell rung-tested" ...><span
class="dot dot-green" title="complete: rl070_manual_check/kline_window_builder.py
plus test ...">` — green, with its proof.

**Deliberately mismatched**, changing `consumes` to `("wrong-data",)`:

```
mismatches: {'kline-window-builder': "kline-window-builder: blueprint
declares consumes=['market-data'] produces=['kline-window', 'part-health'];
rl070_manual_check/kline_window_builder.py declares
consumes=['wrong-data'] produces=['kline-window', 'part-health']"}
rung: FAILING
green? False
```

Both sides named, dot red.

**Deliberately non-literal**, setting
`resource_class=ResourceClass.BANDWIDTH_BOUND` (importing `ResourceClass`
from `runtime.part_declaration` but writing it as an enum-attribute access
rather than the literal string):

```
mismatches: {'kline-window-builder': "kline-window-builder:
rl070_manual_check/kline_window_builder.py has no verifiable wiring
(ModuleDeclaresNoWiring: .../kline_window_builder.py: PART_DECLARATION's
'resource_class' argument is not a literal (malformed node or string on
line 7: Attribute(value=Name(id='ResourceClass', ctx=Load()),
attr='BANDWIDTH_BOUND', ctx=Load())) -- a built part's declaration must be
written as literal values, never computed or resolved at runtime)"}
rung: FAILING
green? False
```

Red, naming exactly which argument and why.

Cleaned up (`rm -rf rl070_manual_check`), re-ran the monitor: back to
`DECLARED 321`, `0/321 parts green`. `git status --porcelain` shows no
scratch directories left behind; only the same five source files as before
this addendum plus this report.

### `sys.path` growth — confirmed gone

```
$ grep -n "sys.path" dashboard/build_part_monitor.py
158:    if project_root not in sys.path:
159:        sys.path.insert(0, project_root)
```

One guarded insertion, for the repository root, to import
`runtime.part_declaration` (this project's own trusted module) — not one
insertion per part. No per-part directory is ever added to `sys.path`
anymore, and nothing is ever removed from `sys.modules` because nothing is
ever put there.

### Concerns, updated

The two `FAILING`-conflation and `find_source_files()`-scope concerns from
the original report stand unchanged. The `sys.path` growth concern is
resolved by this addendum and removed from the list. One new, minor note:
`read_declaration_from_source`'s "not a literal" message includes Python's
own `ast.literal_eval` exception text (e.g. `"malformed node or string on
line 7: Attribute(...)"`), which is accurate but implementation-flavoured —
readable by whoever wrote this code, less so by the user glancing at a red
tile. The leading half of the message ("... argument is not a literal ...
must be written as literal values, never computed or resolved at runtime")
is the part meant to carry the fix; the `ast` internals are appended for
whoever needs to debug further, not trimmed, since Rule 8 asks for the real
evidence, not a paraphrase of it.
