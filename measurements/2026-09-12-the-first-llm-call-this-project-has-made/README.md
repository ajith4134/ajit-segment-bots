# The first LLM call this project has ever made — 2026-09-12

Sixteen parts publish `llm-request`. Before today **not one call had ever been
made**, and nothing was broken: the promotion ring had no entrance, no part could
get its first budget, and the router looked a budget up by the wrong key.

## The transport, exercised directly

`runtime/llm_providers.claude_code_session_call` — the operator's own Claude Code
subscription, no API key — called with one fact and asked for it back:

    prompt          "Facts: NIFTY closed at 24550.30 on 2026-09-11.
                     Reply with the closing level only."
    finish_reason   end_turn
    input tokens    20,341
    output tokens   128
    seconds         4.99
    answer          "NIFTY close 2026-09-11: 24550.30"

The 20,341 input tokens are the cache-creation floor that transport's own
docstring predicted in September ("about 20,000 because the operator's own
CLAUDE.md loads whatever the working directory is"), measured again here and
matching. The answer contains the fact and nothing else, which is what
`structured-output-enforcer` and `claim_verification` are built to check.

**This was a direct call to the transport, not a call through the spine.** The
spine's own path still stops one step earlier: `prompt-renderer` refuses for
missing context, because `context-assembler` needs a verified snapshot and
`ground-truth-snapshot-builder` correctly refuses to build one while the market
is shut — 122 `refused_stale` on a Saturday evening. A snapshot, and so the first
call *through the parts*, needs a replay or Monday's open.

## What the chain reads now, measured on the live spine the same day

| part | before | after |
|---|---|---|
| `prompt-registry` promotions | 0 | 2 |
| `prompt-registry` purposes with an active version | 0 | 2 |
| `prompt-renderer` refused for no active version | 906 of 906 | 2 of 41 |
| `part-token-budgeter` budgets issued | **0 ever** | 1,774 and climbing |
| `part-token-budgeter` windows rolled | 855 in hours (per tick) | 0 |
| `llm-request-router` refused for no asking part | n/a | 0 |

## The four defects behind it

1. **Nothing could promote a purpose's first prompt version.** The ring: active
   version ← promotion ← score ← golden cases + validated output ← active
   version. Fixed by `seed-prompt-promoter`, one door, once per purpose, with the
   seeded purposes named in its standing. `prompt-promotion-gate` untouched.
2. **A part could not get its first budget.** `part-token-budgeter` learned a
   part existed only from an `llm-call-record`, and a record needs a call, and a
   call needs the budget. `llm_budget_starting_share` — a setting that exists for
   exactly the unmeasured part — had never been applied to anybody. Fixed by
   consuming `llm-request`, which now carries `asked_by`.
3. **The router looked the budget up by `str(rendered.context_id)`** — a context
   id, never a part id — so every lookup missed and
   `refused_part_out_of_budget` was the only outcome it could reach. Invisible
   because nothing had ever been rendered.
4. **The budget window rolled on every tick**, clearing `calls_used`,
   `tokens_used` and `money_used`, so no part could ever be found out of budget:
   a guard that cannot fire, the same shape as a board that cannot render red.

## Spend, and what bounds it

Read off the live settings before the switch was flipped:
`llm_subscription_calls_per_hour` 20, `llm_quota_calls_allowed` 200 per
`llm_quota_window_seconds` 18,000, `llm_subscription_model` haiku,
`llm_spend_ceiling` $1.00 per day with **no paid provider key installed at all**.
At the measured $0.041 per stripped haiku call that is about $0.82 an hour at the
cap, against the operator's own subscription rather than a card.
`prompt_seeding_is_allowed = 0` closes the ring again.

## The chain end to end, and the three defects only a real answer could show

Added later the same day. The live spine still cannot do this — the market is
shut and `ground-truth-snapshot-builder` correctly refuses a stale snapshot — so
the chain was driven through the real parts in
`tests/integration/test_a_market_fact_becomes_a_validated_llm_output.py` on real
captured prints:

    tests/captured/upstox/2026-09-08-reliance-book-and-trade-at-one-moment.json
    bid 1298.1   ask 1298.2   last 1298.2   -- the book's top level and the trade
    print that landed in the SAME NANOSECOND, off this project's own tape

One moment matters: the snapshot builder refuses two facts that contradict each
other, so a bid from one second beside an ask from another is how a test would
fake its way past that check.

Eight parts, in order: `ground-truth-snapshot-builder` → `part-token-budgeter` →
`context-assembler` → `prompt-registry` + `seed-prompt-promoter` →
`prompt-renderer` → `llm-model-picker` → `llm-request-router` →
`subscription-session-caller` (**the real call**) → `structured-output-enforcer`.

It failed three times before it passed, and every failure was a real defect
between two parts of the same block that no test could have found without an
answer from a model:

**1. `prompt-template-author` wrote a schema the enforcer crashes on.** It wrote
`{"venue_id": "str", ...}`; `structured-output-enforcer` does `rule.get("type")`,
so every template it has ever written raises `AttributeError: 'str' object has no
attribute 'get'` the moment a real answer arrives. Two of those templates are
promoted and active on the live spine right now. The enforcer would have
crash-looped on this system's first ever reply.

**2. The prompt never stated the shape the enforcer demands.** The rendered text
carried the instruction, the facts and the retrieved material, and nothing about
the output schema — while the enforcer refuses anything that is not that schema.
Every answer this system could ever have received would have been refused as "not
the declared structure", and a model cannot be blamed for not guessing a schema
nobody showed it.

**3. The prompt never said what the question was about.** The schema requires
`venue_id` and `symbol` in the answer. The first real answer was:

    {"symbol": null, "text": "bid=1298.1, ask=1298.2, last-trade=1298.2", "venue_id": null}

— correct, and null on both, because nothing in the prompt named them. The
request had carried both all along.

A fourth thing was fixed rather than found: the answer above arrived inside a
` ```json ` fence and the enforcer refused it as not-JSON. Every model fences
JSON when asked for JSON, so the fence is read now and **counted**
(`answers_arriving_in_a_code_fence`) — tolerating it silently would be the
opposite mistake, and that counter is what says how often the prompt's own "no
code fence" is ignored.

After all four: the chain passes, one real call, and the validated output carries
no unsupported claims — every number in it is one of the six measured facts it
was given.

