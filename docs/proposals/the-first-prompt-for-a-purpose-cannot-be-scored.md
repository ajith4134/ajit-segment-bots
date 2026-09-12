# A purpose's first prompt version cannot be scored, so nothing can promote it

**2026-09-12. One new part: `seed-prompt-promoter`.** Measured on the live spine,
not reasoned about.

## The deadlock, in the parts' own counters

Sixteen parts publish `llm-request`. Not one LLM call has ever been made. The
chain is not broken anywhere — every part is doing exactly what it was built to
do, and the cycle closes on itself:

    prompt-template-author    2 templates written, first_templates_written_before_any_evidence 2
    prompt-registry           2 versions registered, purposes_with_an_active_version 0, promotions 0
    prompt-renderer           906 requests seen, 906 refused_no_active_version, 0 rendered
    llm-request-router        0 requests_seen   (nothing is ever rendered to route)
    subscription-session-caller / metered-api-caller / local-model-caller   0 calls
    structured-output-enforcer 0 responses_seen, 0 validated-llm-output ever published
    golden-case-keeper        0 cases_offered   (a case needs a validated output)
    prompt-evaluator          0 cases_run       (scoring needs cases and outputs)
    prompt-promotion-gate     0 decisions       (it consumes prompt-score, and none exists)

Read as a ring: **an active version needs a promotion, a promotion needs a score,
a score needs golden cases and validated output, and validated output needs an
active version.** Every arrow is correct on its own. The ring has no entrance.

`prompt-renderer`'s refusal is right — "rendering against an inactive version
would run a prompt nobody promoted". `prompt-promotion-gate`'s refusal is right,
and it is the whole reason that part exists:

> A version with no incumbent is promoted only if it clears an absolute bar.
> Being the first is not the same as being good enough, and "it is all we have"
> is how an unusable prompt becomes production.

The absolute bar is measured by running golden cases through the model. For a
purpose's **first** version there is nothing to measure it with. The bar is not
too strict; it is unreachable by construction.

## Why the gate is not the place to fix it

The obvious change — let the gate promote an unscored first version — would
delete the one property that makes the gate worth having, and it would do it
silently: `describe_promotion_gate` currently reports
`"promotes_without_a_score": False` as a standing claim, and that claim would
become a lie on a tile nobody re-reads.

The same objection applies to feeding the gate a manufactured score. A fabricated
number that clears a bar is worse than an honest absence of one, and every part
downstream (`prompt-drift-monitor` watches scores) would treat it as a
measurement.

## The change: one part, and it can only ever act once per purpose

`seed-prompt-promoter` (T-6: grow by adding parts, never by making a part
cleverer).

| | |
|---|---|
| consumes | `prompt-version` |
| produces | `prompt-promotion`, `part-health` |
| resource class | compute-bound |
| rate risk | changes-the-answer |
| skipped tick effect | delays |

It promotes the **first** version of a purpose, and refuses everything else:

- **Only a purpose with no active version, ever.** It watches `prompt-version`
  and the moment any version for a purpose reports `is_active`, that purpose is
  closed to it permanently — including a version promoted by the gate on real
  evidence.
- **Only once per purpose.** After it has seeded a purpose it will not seed it
  again, even if the seeded version is later retired. A second seed would be a
  rollback taken without evidence, which is the oscillation the gate exists to
  prevent.
- **Never a replacement.** It has no comparison to make and does not pretend to
  one: the promotion it publishes carries `replaces=None` and a reason that says
  in words that the version was never scored.
- **It is countable and visible.** `purposes_seeded` and the list of purpose
  names are in its standing, so "how much of this system is running on an
  unscored prompt" is a number on a board rather than a thing to remember. Rule 8.

Everything after the first version goes through `prompt-promotion-gate` on
evidence, unchanged. The seed is what makes that evidence obtainable: once one
prompt runs, outputs exist, so golden cases can exist, so scores can exist, so
the gate can do its job. The seed is the entrance to the ring, and it is one
door, once per purpose, with its name written down.

## What this does not fix, stated rather than discovered later

`golden-case-keeper` consumes `validated-llm-output` **and** `closed-trade`, and
refuses a case whose outcome is not known. For a purpose whose answer is not
settled by a trade — structuring a news item, for instance — it is not yet clear
what supplies the outcome. So seeding will produce real LLM calls and real
validated output; whether the *scoring* loop then closes for every purpose is a
separate question this proposal does not answer, and the evaluator's
`cases_run` staying at zero for such a purpose is where it will show.

## Cost, because it is the part of this that spends

Unblocking the renderer turns refused requests into real calls.
`prompt-renderer` saw 906 requests in about three hours of spine uptime — call it
five a minute — and the subscription transport is measured at about $0.041 per
stripped `haiku` call (`runtime/llm_providers.claude_code_session_call`,
2026-09-07). Unthrottled that is on the order of $12 an hour of the operator's
own allowance.

The throttles that decide this are `llm-request-router`'s (quota, spend,
backpressure, per-part budget), `part-token-budgeter`'s `llm-part-budget`, and
`paid-spend-ledger`'s ceiling. **They are what must be read and confirmed before
the first seed goes live, not after** — the seed makes the calls possible, the
governors decide how many, and that is an operator decision rather than a
consequence of landing a part.
