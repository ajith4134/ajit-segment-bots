# The symbol walk, resumed: 30 → 50 (RL-009)

The first attempt at this walk (2026-08-23, 30 → 100) was rolled back the same
morning: a trade was decided at an ENAUSDT price fifty-six minutes stale. This
step was taken only after the two causes were removed — the sampled price frame
(consumers stopped scaling with print volume) and per-symbol gap patience
(`price_gap_patience_multiple`, closing the promise written into
`price_series_maximum_gap_seconds`).

`measure_walk_step.py` takes one snapshot of everything the walk is judged by.
Run it before a step and after the system settles; every number is read from
the heartbeat table, the tape, and `/sys/fs/cgroup` — never asserted.

## The step of 2026-08-24, 12:59 UTC

Before: `walk-20260824T125012Z-30sym.json`. After (25 minutes settled):
`walk-20260824T132558Z-50sym.json`.

| | 30 symbols | 50 symbols |
|---|---|---|
| prints/s, both venues | 471 | 602 (+28%) |
| symbols writing to the tape | 30 + 30 | 50 + 51 |
| worst part staleness | 1.15 s | 1.16 s |
| symbols in the price frame | 60 | 101 |
| largest frame | 30 symbols | 51 of the 2,000 cap |
| frame splits | 0 | 0 |
| hog reports | 0 | 0 |
| market-data / frame input loss | none | none |
| per-part memory, total | 1.35 GB | 1.18 GB |

**The decision half did not move.** Worst staleness stayed at the stream
reader's own ~1s tick — the fifty-six-minute failure shape did not reappear,
which is what the sampler was built to guarantee: 37 consumers read one frame
per venue per 250 ms whatever the print volume does. Within the settle window
the bot opened paper positions on symbols the step admitted (MUUSDT, KORUUSDT,
SKHYNIXUSDT, VELVETUSDT), so the walk is delivering the coverage RL-009 asked
for, not just surviving it.

## What the step surfaced anyway

Walking is measuring, and the after-snapshot caught one flood that predates the
step: `capital-settings-validator`, woken by every republished allotment, bound
and ceiling, judged 4.5×/s, and `trade-capital-bounds-gate` — busy with its own
order stream — dropped 19,435 redundant verdicts over the prior run. A verdict
is a level; it is now paced at the health interval like every other level
publisher. The same session's restart also exposed that the first scoped
shutdown reported every part's exit as 255 — systemd's control-group kill took
the forkserver out from under the supervisor mid-stop — fixed with
`KillMode=mixed` (verified: the next stop reported 56 clean zeros), plus a
startup sweep for scopes a dead run leaves behind, and a planner rule that a
reservation only reconciles a part the planner has itself seen running.

## The next step

50 → 100, the step that failed on 2026-08-23 — taken the same way: snapshot,
step, settle, snapshot, compare. What decides it is decision staleness holding
at ~1 s and input loss staying at none; what would roll it back is either one
moving the way it moved that morning.
