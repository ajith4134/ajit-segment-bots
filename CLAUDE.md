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

Given by the user; `docs/goal.md` is the source of truth and records what was
actually said. Rulings RL-046..057 (in `~/trading-system/docs/rulings.json`,
injected at session start) refine it.

## Where the project stands — 2026-08-20, RL-057

**Design phase closed. Implementation starts.** The blueprint is
`docs/features.json`: 321 parts in 27 blocks, every contract holding
(`python3 dashboard/check_contracts.py`). Build against it; do not redesign it.

- **Build order (RL-050): futures segment bot first, fully.** Spot and options
  stay skeleton — blocks declared, placeholders, no working code — until futures
  is done.
- **Feature by feature (RL-017).** One part at a time. A part climbs
  `DECLARED → IMPLEMENTED → TESTED → RUNNING` on the part monitor only by the
  probes that measure it — never inferred upward.
- **A design change is a blueprint edit first** (`dashboard/blueprint_edits/`,
  idempotent script + proposal in `docs/proposals/`), checked, committed; code
  follows the registry, never the other way round.
- **Nothing in the runtime spec is open any more** (D-011, 2026-08-20). Stack is
  **standard CPython 3.14.4** — not the free-threaded build, because five packages
  including `ta-lib` ship no `cp314t` wheel and this box has no compiler. Structured
  state is **SQLite** (stdlib, WAL). Settings live at
  **`~/.config/ajit-segment-bots/settings/`** as TOML, one file per scope, closing
  RL-055. Durable numeric state is **file-backed `numpy.memmap`**. Every dependency
  is pinned with a written reason. The numbers behind each are in
  `measurements/2026-08-20-part-runtime/`, and `.venv` is built on 3.14.4.

## How code gets written here — RL-058, and the runtime it produced

**`docs/superpowers/specs/2026-08-20-part-runtime-design.md` is the implementation
spec.** Read it before writing any part. It decides what a part physically is, where
its on/off switch sits, how scarcity is answered, and where state lives.

The standard, given by the user on 2026-08-20 (RL-058): this is built the way a
professional team builds, not the way a personal project is built. No shortcut code.
No hardcoded values. No placeholders. Real learning in the code. No upper limit on
lines per file. Intent is established by interview, never by assumption (RL-016).

The decisions that followed from that interview:

| | |
|---|---|
| **RL-059** | at most 5 subagents at once, research agents included |
| **RL-060** | parts that judge carry a learned component; pure transport stays deterministic |
| **RL-061** | every number is estimated from data or a named setting with provenance — no numeric literals in decision code |
| **RL-062** | the no-placeholder rule binds futures; spot and options stay honestly empty |
| **RL-063** | tests run on real captured crypto data, never invented fixtures |
| **RL-064** | Python core; Rust for hot paths later and only on measurement |
| **RL-065** | proven libraries for solved problems, own code for the edge, every dependency pinned with a reason |
| **RL-066** | the transistor is hardware governance: a part is a process, the governor owns the switch, scarcity is never answered by a queue |
| **RL-067** | what is built matches the diagrams — a part's real consumes and produces equal what the blueprint declares |
| **RL-068** | build order: substrate, then market-data-feed, then the governor spine, then the futures vertical |
| **RL-069** | the runtime substrate is off-diagram — its own status-board tile, not a cell on the part monitor |
| **RL-071** | the bot trades on live prices as they arrive, exactly as it would with real money; a tape replay is never what a trading decision or a live run's learning is made from |

**Build order matters because no dependency order exists.** 299 of the 321 parts sit
in one feedback cycle, and the transitive inputs of a paper fill are 306 parts. So no
part waits for its upstreams: each is built and tested against recorded real data.
The architecture forces the tape, which is why `market-data-feed` is built first —
history accrues only in real time and cannot be recovered later.

The research behind the runtime, with citations and its own UNVERIFIED sections, is
in `~/research/segment-bots-runtime/` (`ajith4134/trading-system-research`).

## The tape is recording — since 2026-08-22

Phase 1 started capture on the day it became possible. Both venues, the 30
highest-volume symbols on each, trades to disk at
`~/.local/share/ajit-segment-bots/tape/{venue}/{symbol}/{day}.{index,blob}`.

**Since 2026-08-23 the tape is written by the live spine**, not by the capture
script: `operate/run_live_spine.py` starts 47 parts (the feed, the bull bot,
the trading half, and the governor observing without `gate-actuator`), and
`venue-trade-stream-reader` writes the tape. Check it is alive with
`systemctl --user status ajit-spine` and the *Parts alive* tile; restart with
`systemctl --user restart ajit-spine` (SIGTERM flushes the tape and journals).
The spine refuses to start while `start_trade_capture.py` runs, and vice
versa — two writers on one tape make a duplicate indistinguishable from a real
second print.

**Do not stop it without a reason, and never leave it stopped.** History accrues
only in real time: every other part can be built against a tape that exists, and
an hour not captured is gone permanently.

    operate/README.md          how to start, stop, and see what it has captured
    operate/ajit-spine.service       the systemd user unit the spine runs under
    operate/keep_capture_running.sh  the capture fallback's shell supervisor

**Since 2026-08-24 the spine survives crashes, logouts and reboots**: it runs as
the systemd user unit `ajit-spine` (Restart=always, lingering already enabled),
installed after the 04:35 reboot that morning cost eleven hours of live
learning — the capture units came back at boot and the hand-started spine did
not. The `ajit-capture@` units stay installed but *disabled* as the fallback;
enable one side only, never both.

`operate/` is not parts. It stands in for `stream-budget-planner` and the
governor until those exist, and should be deleted when they do.

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

## Rebuilding every board

    dashboard/rebuild_all_boards.sh

One command, all five boards, in dependency order. It exists because they went
stale and nobody noticed: the part monitor was current and the other three were
two days old, showing a project with nothing built while five parts were built
and a million records were on the tape. **A stale board is worse than no board —
it is convincing.** The script prints the five Artifact URLs to re-publish to;
publishing is not scripted, because those URLs live outside this repository and a
script that pretended to publish would be the same failure one layer along.

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

Where it stands on 2026-08-23: 324 parts `TESTED`, 47 of them `RUNNING` on the
live spine. `TESTED` means a source file and a test file exist, nothing more;
**277 parts carry no `start_part` and cannot be launched at all.** `RUNNING` is
read from the table `heartbeat-collector` writes at `heartbeat_table_path` —
the same file the trade board's *Parts alive* tile reads — and a table older
than `heartbeat_silent_after_seconds` proves nothing about any part. Cells
climb as code lands and as parts run — nothing is ever inferred upward, and a
failing part never counts as progress.

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

## Trade board

    python3 dashboard/build_trade_board.py

Regenerates `dashboard/trade-board.html`: every trade the system has recorded,
and the probes that say whether it can make another one. Each tile reads a file
on this machine — the journal the settings name, the live spine's supervisor log
joined to `/proc`, the tape's last write, the segment's money mode, and the
conviction model's own checkpoint.

Two tables. **Open positions** carries the capital in USDT that went into each
one (2026-08-23), the entry, the price now with its age, the stop, the peak and
the worst it went through. **Closed trades** carries entry, exit, capital in, how
long it was held, peak, worst, fees and net. An exit fill reduces a position
rather than adding to it, so a position sold back reads as closed.

The states it must be able to reach, because they are the true ones:
`NOTHING YET` when no trade has opened on a live run and when nothing has closed
yet, `NOT BUILT` for tamper evidence (the journal starts a new chain every time a
recorder starts), and `NOT MEASURED` when the bull bot has never checkpointed —
which is a different fact from a checkpoint saying zero, and neither is healthy.

**Learning progress is measured, since 2026-08-23.** The number on the board is
read from `bull-conviction-model`'s own checkpoint under `learned_state_root`,
which is the same file the model restores from — not a second count kept for the
board, which would be free to disagree with the one the bot acts on.

**A journal entry recorded before the trading half was first started live is
marked as a test's.** The integration test runs the same fourteen parts, and the
boundary is read from the supervisor log rather than guessed.

## The live board — a real URL, since 2026-08-25

    systemctl --user status ajit-board          # API + frontend, loopback only
    systemctl --user status ajit-board-tunnel   # the public URL
    operate/read_board_url.sh                   # what that URL is right now

The part board served live instead of published, over a cloudflared quick
tunnel that dials *out* — no inbound port, port 22 stays the only thing
listening. **The URL changes on every restart**, so it is read from the tunnel's
journal rather than remembered.

**Four views.** *What it is doing* — every part's live standing counters with a
**measured** rate. *Trading* — open positions from the checkpoint
`position-close-detector` restores from, marked against the tape per held symbol,
and closed trades from a bounded tail of the position journal (net, not gross: a
board showing gross calls a fee-eaten loser a winner). *Server load* — CPU,
memory, load and disk from `/proc` and `statvfs`, plus what each running part
costs, joined from the spine's supervisor log to `/proc/<pid>`. *How far built* —
the rung ladder.

Two decisions on that last one worth not undoing: busy **excludes iowait**,
because counting it reports 100% CPU on a machine asleep waiting for a disk; and
memory is total minus **MemAvailable**, not MemFree, because free memory on a busy
Linux box is near zero by design.

`build_trade_board.py` still streams both journals end to end and still takes ten
minutes. That is right for a page built once and wrong for a view meant to be
refreshed, which is why `/api/trades` reads two small things instead — and says
in the panel that its rows are the recent ones, never all of them.

The live rate is the same idea throughout:
taken as a delta between two heartbeat tables the serving process actually
observed. Three-valued on purpose — `WORKING` is a counter that moved, `IDLE` is
every counter holding still (a finding, not a fault), `NOT MEASURED` is no rate
taken yet, because calling that idle would assert a measurement nobody made.

`/api/board` is the expensive shape (327 rungs, measured off the filesystem) and
is polled slowly; `/api/activity` is one small file and is polled fast. They fail
independently: when activity dies the shape stays on screen and only the live
column goes dark.

Never trust a green `npm run build`. `dashboard/web/verify_live_board_renders.sh`
mounts the page in Chromium, waits for a *second* poll so a rate exists, opens a
block, opens a part, and fails on any console error. It is separate from
`verify_board_renders.sh`, which waits for `networkidle` — that never fires on a
page that polls forever.

## Position state survives a restart — since 2026-08-25

`position-close-detector` and `cost-basis-tracker` checkpoint their lot books to
`position_state_root` on every fill, and restore on start.

Before this they held them in memory alone. The spine had started 46 times, 855
positions had been opened and 115 round trips closed: **every start forgot every
open position**, and a position whose lots are forgotten can never reach flat,
never emits a `closed-trade`, and can never be scored. 86% of everything ever
opened was unaccounted for and nothing reported it as a fault.

Quantities are stored as strings, and `runtime/trading_types.exact_quantity` is
the door they come through. `Decimal(str(x))`, never `Decimal(x)` — the latter
carries the float's error in. `LotBook.is_flat` is `== 0` exactly, so there is no
tolerance to tune and no numeric literal to justify under RL-061.

`cointegration-pair-finder` keeps its price series and its verdicts too, under
`pair_state_checkpoint_interval` observations rather than every fill — a lot book
changes 40 times an hour and losing one costs a round trip, while a price series
changes hundreds of times a second and losing a second of it costs nothing.
Measured across a restart: 100 series and 214 verdicts back, 263 cointegrated
pairs where a cold start is at zero.

Each window carries its own last observation time, so the gap across a restart is
**measured** rather than assumed continuous — without it the first price after an
outage sits beside the last one before it and reads as an instant move, which is
exactly the shape a detector fires on. A window restored under a different length
or gap bound is refused rather than reinterpreted.

**Still memory-only:** `spread-reversion-detector`'s per-pair spread history, and
the bull feature builder's own windows. After a restart the arbiter stands aside
until conviction is back, which is what actually delays the first trade — the
scanner is no longer the bottleneck it was.

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
