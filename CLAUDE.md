# ajit-segment-bots

`/home/anushadudekula71/ajit-segment-bots` is the home directory for this project.
Everything built for it lives inside this directory. Claude Code starts here on
this server by default (see *Startup* below).

## Every part is a transistor — read this before designing anything

**`docs/transistor-rule.md` decides how every feature in this project is built.**
Read it before proposing, designing, or writing any part. It is not a guideline
and no feature is exempt.

The six rules, in short — the full statement and the reasoning are in that file:

| | |
|---|---|
| **T-1** | every feature is the same shape — one part template, no privileged parts |
| **T-2** | control path separate from data path — only the resource governor switches parts, never a feature |
| **T-3** | off means genuinely off — an off part releases its CPU and RAM |
| **T-4** | a part knows nothing about the circuit — it names data, never other parts |
| **T-5** | states are explicit and countable, from `state_vocabulary` |
| **T-6** | grow by adding parts, never by making a part cleverer |

Alongside them, **R-01** (`docs/contracts.md`): edges are computed from
consumes/produces, so a part swaps out cleanly and none can wire itself in.

Checked by `python3 dashboard/check_contracts.py`, and the git pre-commit hook
refuses a commit that breaks any of them. If a design seems to need an exception,
that is the signal to stop and ask — never to grant one.

## This project is not the trading-system project

It is unrelated to `~/trading-system`, `~/capture`, `~/research`, the AJIT MASTER
PLAN, and the `RL-0xx` rulings. Those govern that project, not this one.

Do not import its design decisions, architecture, naming, venv, or code here
unless explicitly asked. If something from there is genuinely wanted, the user
says so — it is never assumed.

The global rules in `~/.claude/CLAUDE.md` still apply in full: verification
before claiming completion (Rule 0), model selection for subagents (Rule 1),
install rather than skip (Rule 3), the enforcement hooks (Rule 4), research
tooling (Rule 5), interview-spec-plan before creative work (Rule 6), names that
state what the thing does (Rule 7), displays that show measured state (Rule 8),
and everything lives in GitHub (Rule 9).

## Goal

**NOT YET DEFINED.** The user has not given the goal for this project. Interview
for it before designing or building anything — do not infer one from the
directory name.

Nothing below this line has been decided: stack, language, architecture,
dependencies, repository. Each is an open question, not a deferred default.

## Startup

`~/.bash_aliases` defines a `claude` shell function that runs Claude Code from
this directory regardless of where the shell was. To run it somewhere else for
one invocation:

    CLAUDE_KEEP_CWD=1 claude

The function lives in `~/.bash_aliases` rather than `~/.bashrc` because the
`protect-files.sh` PreToolUse hook guards shell profiles, and `~/.bashrc`
already sources that file. It is a shell function rather than a wrapper script
in `~/.local/bin` because Claude Code auto-updates rewrite the symlink there,
which would silently revert the behaviour.

## Status board

    python3 dashboard/build_status_board.py

Regenerates `dashboard/status-board.html` from probes that actually run. Every
tile traces to one. Nothing on it is hand-written, and anything unprobed renders
as `NOT MEASURED` — never as healthy. Re-publish that file to the same Artifact
URL to update the shared link.

## Part monitor

    python3 dashboard/build_part_monitor.py

Regenerates `dashboard/part-monitor.html`: every part in the blueprint as a cell,
coloured by how far it is actually built. Four rungs, each a probe that runs —
`DECLARED` (in the blueprint, contract intact, no code), `IMPLEMENTED` (a source
file named for the part exists), `TESTED` (a test file names it), `RUNNING` (it
reports a heartbeat). `FAILING` is wired to the contract checker, so a part it
names goes red.

Today every part is `DECLARED`, which is the correct board for a system that is
entirely unbuilt. Cells climb as code lands — nothing is ever inferred upward,
and a failing part never counts as progress.

## Part board (the live one)

    python3 dashboard/part_health_api.py          # serves /api/board + the built frontend
    cd dashboard/web && npm install && npm run build
    python3 dashboard/build_board_snapshot.py     # freezes it into dashboard/board.html
    dashboard/web/verify_board_renders.sh         # proves the page actually draws

A React board over the same probes as the part monitor: 22 blocks, 78 parts, each
cell clickable for the proof that produced its rung. Two builds from one source —
`dist/` polls the API and says `LIVE · polling`, `board.html` inlines one measured
payload and says `SNAPSHOT · frozen`. The mode travels inside the payload, so a
frozen page can never pass itself off as live.

The snapshot exists because only port 22 listens on this server: the live board
cannot reach the user, so the page is published instead.

Never trust a green build for this. `verify_board_renders.sh` mounts the page in
Chromium and fails on any console error. Chromium needs libraries and fonts this
server does not have; both are installed in `~/.local/pwdeps` and the script
refuses to run without them. Missing fonts do not error — they paint every glyph
invisible while every DOM assertion still passes, which is exactly the failure a
screenshot catches and an assertion does not.

## Wiring explorer

    python3 dashboard/build_wiring_explorer.py
    dashboard/web/verify_wiring_renders.sh      # proves the page draws, clicks all three views

Regenerates `dashboard/wiring-explorer.html`: every connection between parts, as
the contract checker sees it. The block-level mermaid planes on the status board
stay the right picture of the *story*; at 25 blocks and 1 363 part-to-part wires
a node-link drawing is a hairball, so this page is the tool for *checking*:

- **Matrix** — 25 × 25 blocks, a cell counts the data types flowing row → column.
  Click a cell for the exact part pairs. Peer blocks (bull, bear, tailgater)
  must stay empty against each other — a filled cell there paints red (R-03).
- **Part focus** — one part in the centre, what feeds it left, what it feeds
  right, wires labelled by data type. Click any part to recentre.
- **Data type** — who writes it, who reads it.

The checker's verdict and the measurement time are stamped in the header.
