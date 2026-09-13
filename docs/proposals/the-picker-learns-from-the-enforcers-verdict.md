# The model picker learns from the enforcer's verdict

**Origin:** the operator, 2026-09-13 — "look into why the model picker refuses 474
requests", then "yes do A B and C".

## What was measured

Live spine, 2026-09-13, about four minutes after start: `llm-model-picker` saw
526 requests, made 52 choices and refused 474 as
`no-model-has-shown-it-can-do-this-purpose-well-enough`. The picker run alone with
the live settings — one model declared, nothing measured, `llm_exploration_share`
0.1 — reproduces 52 / 474 exactly.

## What changes

**A. An unmeasured purpose is explored, not refused.** Refusal exists to stop a
downgrade: answering with a model measured below the bar when nothing better is
known. With no model measured for a purpose there is nothing to downgrade from,
and refusing nine requests in ten discards them without protecting anything. The
cheapest unmeasured model is chosen and marked as measuring. Refusal stays for a
purpose where a model has been measured and fell below the bar. Spend is still
bounded by `llm_subscription_calls_per_hour` in the router.

**B. Quality is learned from whether the answer was usable.** The picker learned
from `llm-call-record.succeeded`, which is "the call returned". Its own docstring
says quality is "did the answer pass the enforcer". A new data type,
`llm-answer-verdict`, is published by `structured-output-enforcer` for every
response it judges — model, purpose, validated or not — and consumed by the
picker. A call that failed outright is still read from `llm-call-record` as a bad
outcome; a call that returned waits for its verdict, so no answer is counted twice.

**B needs the enforcer to judge against the real facts.** Its `start_part` checked
every response against `{}` — its own docstring says "none, until ..." — with
`require_a_citation` on, so every real answer on the spine would have been
rejected, and B would have taught the picker that every model is bad at every
purpose. The enforcer now consumes `rendered-llm-request`, which already carries
the facts the prompt was rendered with, keyed by `rendered_id` exactly as
`LlmResponse` names it. A response whose rendered request has not arrived is held,
as a response with an unknown version already is.

**C.** The picker's minimum observations read `decoding_minimum_trades`, a count
of closed trades. It gets its own setting, `llm_picker_minimum_verdicts`.

## Blueprint

- `structured-output-enforcer` consumes `rendered-llm-request`; produces `llm-answer-verdict`
- `llm-model-picker` consumes `llm-answer-verdict`
- new data type `llm-answer-verdict`

Applied by `dashboard/blueprint_edits/apply_2026-09-13_the_picker_learns_from_the_enforcers_verdict.py`.
