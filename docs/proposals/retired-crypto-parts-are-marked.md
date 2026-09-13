# Retired crypto parts are marked in the blueprint

**Origin:** the operator, 2026-09-13 — "yes do A B and C", after the full suite
failed three parts in `test_every_launchable_part_starts`.

## What was measured

Two of the three failures were not faults. `symbol-catalogue-reader` and
`stream-budget-planner` exit before reporting with "captured_venues names no venue
this build has an adapter for" — the operator's live `captured_venues` is `[]`
because the crypto path was retired on 2026-09-02, and refusing is exactly what
those parts should do then. The retirement was recorded only as a comment in
`operate/run_live_spine.py`, so the test, which reads the blueprint, could not
tell a retired part from a broken one.

## What changes

Each part the spine's comment names, that is declared and **not** on the live
spine, carries `"retired": {"on", "why", "replaced_by"?}` in `docs/features.json`.
24 parts. The comment also names three that are running today and are not marked
— `venue-outage-rider`, `clock-skew-monitor`, `leverage-selector` — and two that
were never declared. `replaced_by` is given only where one part does that job for
the Indian market; `stream-budget-planner` has none, because the broker feed's
2,000-key budget is decided inside `broker-market-feed-reader` rather than by a
planner, and naming a wrong successor would be worse than naming none.

`test_every_launchable_part_starts` still starts every retired part. One that
reports health passes as before; one that exits before reporting is **skipped by
name with the part's own refusal** rather than failed — a retired part that stops
starting is a fact worth seeing, not a regression.

Retired, not deleted: every one stays declared with its contracts.

## Not changed

`run_live_spine.py --without-feed` was meant to hand the tape to
`operate/start_trade_capture.py`. That script captures crypto venues only, so
pointing the flag at the broker feed parts would stop Indian capture with nothing
writing in its place. Its help text now says what it does today: the three parts
it removes are retired and off the spine, so the flag only skips the
capture-script conflict check.
