# The arbiter does not weigh maturity — so it should not declare it

2026-09-06. Closes the one question
`dashboard/blueprint_edits/apply_2026-09-06_declared_inputs_that_were_never_read.py`
deliberately left open: *"opinion-arbiter's bot-maturity is deliberately NOT
touched here … That one is the operator's call."* The operator's answer, given
2026-09-06, is to drop the declaration.

## What is true today

`opinion-arbiter` declares twelve `consumes` types and binds a reader for
**eleven**. `bot-maturity` appears exactly once in the file — inside the
`PART_DECLARATION` tuple — and nowhere else. So maturity has never affected a
single arbitration, and dropping the declaration changes **no** behaviour.

R-01 computes every edge from consumes/produces, so what the declaration does
today is draw a wire on every diagram that no message can travel, and put a
`NOT CARRYING` line on the audit board against a producer that is not at fault.

## Why dropping is right rather than binding

- **The part's own docstring says what moves conviction**: agreement, bot weight
  in this regime, the forecast bias, the counter-argument, the strategy review —
  and coverage and competence as refusals. Maturity is not among them, and it is
  not a near-miss omission: the list is argued item by item.
- **Maturity is a gate, not a weight.** The four parts that really consume it —
  `live-switch-guard`, `autonomy-boundary`, `opinion-conflict-resolver`,
  `exploration-pair-opener` — use it to decide what the system is *allowed* to
  do, not how convinced it is. That is the right home for "how proven is this
  bot".
- **Binding it would be dead on arrival.** `edge-graduation-gate`, its only
  producer, has never published. The wire would still read `NOT CARRYING`, and
  the arbiter would gain a code path nothing exercises, one day before the first
  live session.

## What is not being decided

Whether an arbiter *should* eventually weigh how proven a bot is remains open
and is a real question. It is deliberately not answered a day before the first
live run, and it is not answered by leaving an unread declaration in place —
that records the intent nowhere anyone reads and costs a false wire on every
diagram in the meantime.

## Effect

- `docs/features.json`: `opinion-arbiter.consumes` loses `bot-maturity`.
- `parts/ai_brain/opinion_arbiter.py`: the same, in the `PART_DECLARATION`.
- `dashboard/check_declared_inputs.py` reports clean, which is what allows it
  into the pre-commit hook beside the other three checkers.
