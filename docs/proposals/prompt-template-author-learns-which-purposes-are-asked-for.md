# prompt-template-author consumes llm-request

Proposed by Claude, 2026-09-07, after wiring the LLM transports and finding the
chain still dead one link short.

## The defect

`llm-request-router` holds **12,154 requests and routes none.** It consumes
`rendered-llm-request`, and `prompt-renderer` refuses every one it sees —
`refused_no_active_version` 132 of 132 — because `prompt-registry` has never
registered a version, because `prompt-template-author` has never written a
template.

That much reads like the bootstrap cycle already recorded in
`docs/part-purpose-audit.md`: a model is needed to make a skill, a skill to write
the template, the template to call the model. It is worse than that, and the
cycle hides it.

**The author and the requesters do not share a vocabulary of purposes.**

`prompt-template-author` writes a template for a purpose once evidence names it,
and it takes the purpose from the evidence's own words:

    purposes_with_evidence[finding.topic] += 1
    purposes_with_evidence[skill.title]   += 1

A finding's topic and a skill's title are free text out of research — "volatility
risk premium", "order flow imbalance".

Every request that will ever be made names one of **eleven fixed purposes**,
declared as a constant by the part that makes it:

| part | purpose |
|---|---|
| `skill-distiller` | `distil-a-source-into-structure` |
| `intent-explainer` | `explain-why-this-trade-exists` |
| `devils-advocate` | `argue-against-this-trade` |
| `premortem-writer` | `describe-how-this-trade-fails` |
| `market-thesis-reasoner` | `review-the-thesis-for-a-bellwether-symbol` |
| `setup-second-opinion-reasoner` | `review-the-case-for-a-setup` |
| `strategy-review-reasoner` | `review-a-bots-own-record` |
| `brain-self-reflector` | `judge-the-reasoning-behind-a-closed-trade` |
| `trade-narrative-writer` | `describe-a-closed-trade` |
| `decision-quality-critic` | `judge-the-quality-of-a-decision-separately-from-its-outcome` |
| `idea-generator` | `propose-a-testable-idea-this-system-has-not-tried` |

These two sets can never intersect. A research topic is not one of those eleven
strings and never will be. **So even if the cycle were broken — a finding
arrives, a template is written, a version is promoted — the template would be for
a purpose no part ever asks for, and `prompt-renderer` would go on refusing every
real request for want of an active version.** Breaking the cycle does not fix
this; it produces a template nobody uses and a chain that still carries nothing.

Seventeen parts produce `llm-request`. The author consumes none of them.

## What changes

`prompt-template-author` consumes `llm-request`.

That is the whole edit. The purposes that need templates are exactly the purposes
being asked for, and they arrive stamped on every request. The author already has
everything else it needs: it holds the evidence, it already declares the
`verified-facts` context, and its output schema is already the one every purpose
here shares.

## Why this and not the alternatives

**Not a hardcoded list of the eleven.** A twelfth part would be added and nothing
would tell anyone its requests could never be answered — which is exactly the
failure being fixed, one layer along. The list has to be learned from what is
asked, or it goes stale silently.

**Not keying templates off evidence and hoping.** That is the current behaviour
and it is what produced 12,154 unroutable requests.

**Not having each part ship its own template.** T-6: grow by adding parts, not by
making a part cleverer — but also T-4, a part names data and never another part.
Eleven parts each carrying prompt text is eleven copies of a decision that
`prompt-registry` exists to own, and prompt *quality* is learned centrally from
`prompt-score`; a template inside the part that uses it can never be A/B'd
against a challenger.

## What this does not fix

The cycle is still there for the *content*: with no model, the author writes an
instruction from evidence rather than from a drafted one, and with no evidence at
all it has nothing to say about a purpose. The point of this edit is that once a
single template exists for a purpose that is actually requested, the chain can
carry — and the subscription transport wired the same day can answer it.

Whether the author should write a minimal template for a requested purpose it has
no evidence for is a second question, deliberately not settled here: a prompt
grounded in nothing is the decoration this project's third goal exists to find.

## Contract

R-01 recomputes block edges from part consumes/produces. `llm-request` is already
produced inside `llm-foundation` (`prompt-template-author` itself, and
`prompt-evaluator` and `structured-output-enforcer`), so this adds no new
block-level edge and no peer-block crossing. `check_contracts.py` must pass
unchanged.

---

## What happened when it was applied — 2026-09-07

Applied, and the chain moved two links for the first time in the project's life:

    prompt-template-author   templates_written  0 -> 2
    prompt-registry          templates_seen 0 -> 2, versions_registered 0 -> 2

Both are counted as `first_templates_written_before_any_evidence`, which needed a
second decision the proposal above deliberately left open.

### The first template for a purpose is exempt from the evidence bar

`prompt_minimum_evidence` is 3 and the author held 0, so even with the right
purposes it wrote nothing. The bar's own stated reason is about **rewriting** —
*"rewriting from opinion is how a prompt gets worse in a way nothing detects"* —
which is exactly right for a replacement and cannot apply to a template that does
not exist. There is nothing to make worse, and waiting does not help: on this
system the evidence *is* skills, and a skill can only be distilled by a model
that cannot be called until a template exists.

A first template is not ungrounded. It declares `required_context_kinds`, and the
facts it reasons over arrive with each request — the grounding is per call, not
prior research. **Every other guard still applies**: it must declare an output
shape, name the context it needs, not ask the model to recall a number, and not
ask it to decide something this system decides. Only the evidence count is
waived, and it is counted separately so a board can tell a template derived from
measured findings from one written to get the chain moving.

### What still does not carry, and what it needs

`prompt-renderer` refuses all 136 requests it sees: a version is *registered* but
not *active*, and only `prompt-promotion-gate` makes one active. The gate has a
first-version path (`first_versions_promoted`) that promotes without an
incumbent — but it still needs a `prompt-score` to promote, and a score comes
from `prompt-evaluator` running the template against **golden cases**.

`golden-case-keeper` builds those from `closed-trade`, which needs no model at
all. It has received none since the spine last restarted because the market is
shut; 60 trades closed earlier the same day.

So the remaining bootstrap is not a missing wire — it is a trading session:

    closed-trade -> golden-case -> prompt-score -> prompt-promotion
                 -> an active version -> a rendered request -> a routed call

That is testable at the next open and was not invented here. Whether
`prompt-evaluator` can produce its first score without an active version to run
the prompt through is the next thing to measure, and it is deliberately left
unmeasured rather than guessed at with the market closed.
