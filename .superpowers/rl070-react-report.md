# RL-070 dots on the React board — report

Branch: `phase-1-market-data-feed`, starting at `bdbcc71`.
Commit produced: `f535c36` — "RL-070's dots on the React board, and one shared rule behind all three"

## What changed, file by file

### `dashboard/completion.py` (new)
The single definition of RL-070's dot rule, extracted verbatim (docstrings and
all) from `dashboard/build_part_monitor.py`:

- The rung vocabulary: `DECLARED`, `IMPLEMENTED`, `TESTED`, `RUNNING`, `FAILING`,
  `UNMEASURED`, `LADDER`, `RUNG_MEANING`.
- `PartState` — the dataclass a part's measured state is carried in.
- `part_is_measured_complete(state) -> bool` — green only at `TESTED` or `RUNNING`.
- `block_completion(owned) -> (bool, str)` — green only when every owned part is
  green; never green by inference for an empty block.

Kept the rung vocabulary alongside the predicate rather than splitting them,
because `part_is_measured_complete` is stated directly in terms of `TESTED`/
`RUNNING` — separating them would let one drift out of step with the other.

### `dashboard/build_part_monitor.py`
No longer *owns* the predicate — it now does
`from completion import (DECLARED, FAILING, IMPLEMENTED, LADDER, PartState,
RUNG_MEANING, RUNNING, TESTED, UNMEASURED, block_completion,
part_is_measured_complete)` at the top and the local definitions of all of
those are deleted. Every other function in the file (rendering, the ladder
probes, the RL-067 wiring check) is untouched, and since Python re-exports
imported names as module attributes, `build_part_monitor.part_is_measured_complete`
and `build_part_monitor.block_completion` still resolve exactly as before —
the existing test suite (`tests/dashboard/test_build_part_monitor.py`) needed
no changes.

### `dashboard/build_status_board.py`
`collect_block_completion_results()`'s import split in two: `category_lookup`
and `measure_parts` still come from `build_part_monitor`, but `block_completion`
(and the `TESTED`/`RUNNING` aliases it uses) now come directly from
`completion`, rather than indirectly through `build_part_monitor`'s re-export.
Docstring updated to name `completion.py` as the source of truth shared by all
three consumers.

### `dashboard/part_health_api.py`
`build_board_payload()` now imports `part_is_measured_complete` and
`block_completion` from `completion` (not from `build_part_monitor`, and not
reimplemented). Added to the payload:

- **Per part:** `is_complete` (bool) and `dot_proof` (str) — `dot_proof` mirrors
  `state.proof` for a part, since the rung *is* the completeness evidence there.
- **Per block:** `is_complete` and `dot_proof`, computed by calling
  `block_completion(owned_states)` on the same `PartState` objects `measure_parts()`
  already produced — no re-measurement, no second definition of "complete".

### `dashboard/web/src/theme.js`
Added `COMPLETION_DOT` / `completionDot(isComplete)`: colour (`#57D9A3` green /
`#D45B54` red, matching the existing rung palette) plus a **second channel** —
`glyph: '●'` for complete, `glyph: '○'` for unfinished, and a `label` used for
`title`/`aria-label`.

### `dashboard/web/src/CompletionDot.jsx` (new)
One small component rendering the glyph, used by both `PartCell` and
`BlockPanel` so the two never draw the dot differently. `title`/`aria-label`
carry the short verdict word ("complete"/"unfinished"); the actual proof text
travels through the board's existing mechanisms (see below), not a second one.

### `dashboard/web/src/PartCell.jsx`
- Header: dot placed next to the part name inside a new `.cell-top` row (mirrors
  the layout the HTML part monitor already uses for the same purpose).
- The existing click-to-expand detail panel's `.detail-proof` line now shows the
  dot plus its verdict word plus `dot_proof`, folded into the same reveal the
  cell already had — no new mechanism was added to reach it.

### `dashboard/web/src/BlockPanel.jsx`
Dot placed next to the block title (`.panel-title-name`), and a
`.panel-dot-proof` line placed under the header, always visible — the same
"just show it inline" treatment the panel already gives `scope` and `summary`.
No click was added to the panel; nothing else on it is hidden behind one either.

### `dashboard/web/src/styles.css`
`.completion-dot`, `.cell-top`, `.panel-title-name`, `.panel-dot-proof` added,
in the existing IBM Plex Mono / dark-panel idiom already used for `cell-rung`,
`legend-row`, etc.

### `tests/dashboard/test_part_health_api.py` (new)
No prior test file existed for `part_health_api.py`. Added 6 tests, matching
the fixture shape `test_build_part_monitor.py` already established (a real
scratch source+test file for `kline-window-builder`, written under the project
root and removed in a `finally`):

1. baseline: every part and every block `is_complete is False`, counts are `0`.
2. every part's and every block's `dot_proof` is non-empty (Rule 8).
3. `build_board_payload`'s `is_complete` is asserted equal to a fresh call to
   `completion.part_is_measured_complete` on the same states — proves the API
   reuses the predicate rather than recomputing an equivalent one.
4. building+testing `kline-window-builder` turns its part dot green while its
   block (`prediction`, 16 parts) stays red.
5. removing the scratch files turns the part dot red again and the count back
   to 0.
6. `build_board_payload("live")["mode"] == "live"`,
   `build_board_payload("snapshot")["mode"] == "snapshot"`.

## Where the shared rule lives now

`dashboard/completion.py` — imported by `dashboard/build_part_monitor.py`,
`dashboard/build_status_board.py`, and `dashboard/part_health_api.py`. Nobody
reimplements `part_is_measured_complete` or `block_completion`; verified by
test 3 above, which asserts the API's per-part verdict against a fresh
independent call into `completion.py`.

## Second-channel choice, and why

**Filled disc (●) for complete, hollow ring (○) for unfinished**, in addition
to colour (green/red matching the existing rung palette). Chosen because:

- It reads correctly with colour removed entirely — filled vs. hollow is a
  shape distinction, not a hue distinction, so it survives greyscale and
  print. I confirmed this by eye in the rendered screenshots (below); the
  ring/disc distinction is visible independent of the colour.
- It costs nothing in layout — same glyph size as the surrounding
  IBM Plex Mono rung labels, no new icon asset, no extra DOM structure beyond
  one `<span>`.
- It matches the vocabulary already established by the HTML board's own
  RL-070 work (`dashboard/build_part_monitor.py`'s `render_dot`, which is
  circular via CSS `border-radius: 50%` + fill/no-fill) — the React board's
  dot is the same *concept*, filled-vs-hollow, expressed as a text glyph
  instead of a CSS circle because the rest of `PartCell`/`BlockPanel` renders
  everything else as plain elements/text rather than drawn shapes.

## Verification — commands run and real output

**1. Nothing built yet — every dot red, counts 0/321, 0/27:**

```
$ python3 -c "
from part_health_api import build_board_payload
p = build_board_payload('live')
print('green parts', sum(1 for x in p['parts'] if x['is_complete']))
print('green blocks', sum(1 for x in p['blocks'] if x['is_complete']))
"
green parts 0
green blocks 0
```//run from dashboard/
Also via the live HTTP API (server started, curled, killed):
```
mode live
green parts 0
green blocks 0
```
And the two HTML boards, run directly:
```
$ python3 build_part_monitor.py | tail -3
dots (RL-070): 0/321 parts green, 0/27 blocks green
wrote .../dashboard/part-monitor.html

$ python3 build_status_board.py | tail -1
block dots (RL-070): 0 of 27 blocks green
```
Both HTML boards report the exact same `0/321`, `0/27` as before the refactor
— extraction did not change behaviour.

**2. Green reachable and reversible, on the React board itself (not just the
API):**

Wrote real scratch source+test files for the real blueprint part
`kline-window-builder` (block `prediction`, 16 parts) under
`rl070_scratch_react_dot/` at the project root — outside every excluded
directory `find_source_files()` skips. Rebuilt the frozen snapshot
(`python3 dashboard/build_board_snapshot.py`), then drove a real Chromium via
Playwright to click the "Prediction" tab and read the DOM:

```
{
  "found": true,
  "partDotClass": "completion-dot complete",
  "partDotGlyph": "●",
  "partRung": "TESTED",
  "blockName": "Prediction",
  "blockDotClass": "completion-dot unfinished",
  "blockDotGlyph": "○"
}
```
Screenshot confirmed by eye: `K-line window builder` cell has a green filled
dot and a green left border, rung `TESTED`; the `Prediction` block header has
a red/orange hollow ring and the badge reads `PARTIAL` (pre-existing
`summarise_block_state`, unrelated to the RL-070 dot but consistent with it —
one part built out of 16 is neither `DECLARED` nor `RUNNING`).

Then removed `rl070_scratch_react_dot/`, rebuilt the snapshot, and reconfirmed
`0/321`, `0/27` (shown above) — green is reversible, not a one-way ratchet.

The same reachability/reversibility is also pinned down as an automated
regression in `tests/dashboard/test_part_health_api.py` (tests 4 and 5 above),
so it does not depend only on the manual run recorded here.

**3. `dashboard/web/verify_board_renders.sh`, actually looked at:**

Ran at baseline (0/321 green) and again in the green-dot state above. Both
times: `rootChildren: 1`, `consoleErrors: []`, `failedRequests: []`,
`cells: 321`, `panels: 27`, `mode: "SNAPSHOT · frozen"`, and
`proofOnClick: "○ unfinished — no source file on disk matches this part's
name"` — the click-to-expand path now includes the dot glyph and verdict
word, proving the dot's proof is reachable through the existing mechanism.

Screenshots were written to the scratchpad
(`/tmp/claude-1001/.../scratchpad/board-render-final.png` and
`green-dot-check.png`) and viewed directly: text is legible (fonts from
`~/.local/pwdeps` / `~/.local/share/fonts` are present and loaded — no
invisible-glyph failure), the layout is intact, hollow rings appear on every
cell/panel at baseline, and the one filled disc appears exactly where the
scratch part was placed and nowhere else.

**4. API and snapshot builds both declare mode correctly:**

```
$ python3 -c "
from part_health_api import build_board_payload
print(build_board_payload('live')['mode'])
print(build_board_payload('snapshot')['mode'])
"
live
snapshot
```
Live HTTP server (`python3 part_health_api.py --port 8788`), curled:
```
mode live
sample part keys [..., 'dot_proof', 'is_complete', ...]
sample block keys [..., 'dot_proof', 'is_complete', ...]
```

**5. Full suite, clean under `-W error::ResourceWarning`:**

```
$ .venv/bin/python -m pytest tests -q -W error::ResourceWarning
........................................................................ [ 41%]
........................................................................ [ 83%]
.............................                                            [100%]
173 passed in 28.60s
```
167 pre-existing + 6 new (`tests/dashboard/test_part_health_api.py`) = 173.
Also ran `tests/dashboard/` alone (11 passed, pre-existing two files
unaffected) and the new file alone (6 passed) before the full run, to isolate
any failure if one had occurred.

**6. Contract checker / pre-commit hook:**

```
$ python3 dashboard/check_contracts.py
321 features, 27 categories: all contracts hold
```
Committed with the hook active (`.githooks/pre-commit` runs
`check_contracts.py`); the commit went through without `--no-verify`.

**7. Frontend build itself:**

```
$ cd dashboard/web && npm run build
✓ 32 modules transformed.
✓ built in 763ms
```
No `npm install` was run — `node_modules` was already present as stated.

## Concerns

- **`PARTIAL` block badge vs. the red dot, side by side.** When a block has
  some but not all parts built, `summarise_block_state` (unrelated,
  pre-existing) shows an amber `PARTIAL` text badge next to a red/hollow RL-070
  dot on the same panel. Both are individually correct and neither claims the
  other's meaning, but a reader unfamiliar with the distinction could plausibly
  read "PARTIAL" as "partially green." I did not touch `summarise_block_state`
  or its badge — out of scope for this task and not requested — but flagging
  it since it is the one place the two vocabularies (four-way block `state`
  and two-way RL-070 `is_complete`) sit next to each other on the same panel.
- **`dot_proof` duplicates `proof` for parts.** By design (the rung *is* the
  completeness evidence at the part level), so `part["dot_proof"] ==
  part["proof"]` always holds today. I kept them as separate payload fields
  for symmetry with the block level (where `dot_proof` is a genuinely
  different fact from any single part's `proof`), rather than collapsing them
  — flagging this as a judgement call rather than something forced by the spec.
- I did not modify the already-shipped HTML dot rendering (`render_dot` in
  `build_part_monitor.py`) to add a second channel there — the task scoped
  this work to the React board, and the instruction to keep HTML behaviour
  identical (`0/321`, `0/27`) argued for leaving its rendering untouched. Its
  existing `title` attribute does carry the proof text on hover, but has no
  shape/glyph distinction; if that board is meant to meet the same
  accessibility bar, it is not done here.

## Files touched (all paths under `/home/anushadudekula71/ajit-segment-bots`)

- `dashboard/completion.py` (new)
- `dashboard/build_part_monitor.py`
- `dashboard/build_status_board.py`
- `dashboard/part_health_api.py`
- `dashboard/web/src/CompletionDot.jsx` (new)
- `dashboard/web/src/PartCell.jsx`
- `dashboard/web/src/BlockPanel.jsx`
- `dashboard/web/src/theme.js`
- `dashboard/web/src/styles.css`
- `tests/dashboard/test_part_health_api.py` (new)

Generated artefacts touched only to verify, never committed: `dashboard/board.html`,
`dashboard/part-monitor.html`, `dashboard/status-board.html`,
`dashboard/web/dist/`, `dashboard/web/dist-single/` — all git-ignored, confirmed
with `git check-ignore -v` and `git status --short` showing none of them staged.
