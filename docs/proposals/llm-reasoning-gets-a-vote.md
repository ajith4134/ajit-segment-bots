# The LLM foundation grows the thinker it was always meant to grow

**Proposed 2026-08-29, after the operator asked to extend the LLM feature
toward reasoning, interviewed the same day.**

## Where this comes from

`llm-services`'s own blueprint entry already names this, unbuilt, as
`planned_extension`: *"Becomes a thinker in its own right once the rest is
running — reasoning about the market rather than only answering other blocks'
questions."* That condition looks met now: 27 of 27 blocks are on, the LLM
foundation's 24 infrastructure parts (routing, cache, budget, prompt registry,
retrieval, golden-case scoring, promotion gates) are live and load-bearing.

It traces further back than that. RL-010, RL-013 and RL-026 are three separate
sessions of the same instruction, restated because it kept not landing: *real*
intelligence — learning, reasoning, depth, tested mechanically rather than
asserted — not rule-based, not hardcoded thresholds. This proposal is the
first piece of infrastructure that reasons about the market as its whole job,
rather than answering one narrow question for a part that does.

## Interview answers (2026-08-29)

- **Authority: a real vote.** Not advisory-only — it can change what gets
  traded, weighed by `opinion-arbiter` the same as bull-bot, bear-bot and the
  tailgater.
- **Scope: all three.** Broad market thesis, a second opinion on a specific
  setup, and a meta view on which of the system's own bots/strategies are
  working.
- **Cadence: on a clock**, to start. Simplest, matches how every part in this
  system already ticks.

## What already exists, so this does not duplicate it

`opinion-arbiter` already consumes three of the shapes this proposal would
otherwise have to invent: `counter-argument` (from `devils-advocate`, which
already calls the LLM — the only one of the three that genuinely does),
`competence-map` (from `self-model-reporter`, pure statistics, no LLM despite
the reasoning-shaped name), and `conflict-ruling` (from
`opinion-conflict-resolver`, also pure statistics). None of the three casts a
vote — each is an advisory modifier the arbiter weighs other bots' opinions
against.

**None of the three existing parts is touched.** Every project rule this
system runs on says grow by adding a part, never by making one cleverer (T-1,
T-6). `devils-advocate` keeps arguing against the final intent as a last
check; `self-model-reporter` keeps computing decayed, coverage-bounded
competence the way a number should be computed, not guessed at by a model.

## The central open question, decided here rather than left open

A vote that fits `DirectionalOpinion` cleanly needs a `conviction: Estimate`
(a fitted statistical estimate — observations, prior, bounds) and usually a
`timing: EntryTiming` and an `exit_plan: ExitPlan` (a real stop and target, a
number a position is priced against). An LLM does not have a fitted
distribution behind its confidence, and should not be inventing a stop price
by reading a book — that is precisely the kind of number this project has
spent this session's fixes making sure never gets guessed at.

Two ways to give it a vote anyway:

- **(A) Lightweight vote** — the reasoner publishes `directional-opinion` with
  `timing=None, exit_plan=None`. It can confirm or contradict a symbol another
  bot already has a live setup on; it cannot single-handedly manufacture a
  trade with no exit plan behind it, because nothing downstream acts on a
  `directional-opinion` that calls to act with no exit plan attached.
- **(B) Full peer bot** — same shape as bull-bot/bear-bot/tailgater: its own
  setup filter, feature builder, entry timer and exit-plan proposer, with an
  LLM standing in for the trained conviction model. Fully self-contained,
  much larger scope — effectively a fourth segment bot.

**This proposal builds (A).** It answers what was asked — reasoning that
votes — without opening a fourth bot's worth of new parts before the first
one has run. (B) stays the documented larger path if (A) proves too limited
once it has run.

## The three new parts

All three in `ai-brain`, beside `opinion-arbiter`, `devils-advocate` and
`opinion-conflict-resolver` — co-located with what reads them, per T-4 (a part
names data, not other parts, but nothing stops parts that talk about the same
thing living in the same block). All three follow `devils-advocate`'s own
wiring shape: consume the triggering context, publish `llm-request`, consume
`validated-llm-output` back, correlated to the request that asked for it.

```
market-regime, verified-snapshot ──► market-thesis-reasoner ──llm-request──► (LLM foundation) ──validated-llm-output──► market-thesis-reasoner ──directional-opinion──► opinion-arbiter

directional-opinion, verified-snapshot ──► setup-second-opinion-reasoner ──llm-request──► (LLM foundation) ──validated-llm-output──► setup-second-opinion-reasoner ──directional-opinion──► opinion-arbiter

competence-map, bot-scorecard, closed-trade ──► strategy-review-reasoner ──llm-request──► (LLM foundation) ──validated-llm-output──► strategy-review-reasoner ──strategy-review──► opinion-arbiter
```

("(LLM foundation)" is the existing pipeline none of this touches:
`retrieval-querier → retrieval-index → context-assembler → prompt-renderer →
llm-request-router → structured-output-enforcer`.)

| Part | Consumes | Produces | Purpose string | Casts a vote? |
|---|---|---|---|---|
| `market-thesis-reasoner` | `market-regime`, `verified-snapshot`, `validated-llm-output` | `directional-opinion`, `llm-request`, `part-health` | "what is this market doing right now, across symbols, and why" | Yes — `timing=None, exit_plan=None` |
| `setup-second-opinion-reasoner` | `directional-opinion`, `verified-snapshot`, `validated-llm-output` | `directional-opinion`, `llm-request`, `part-health` | "does the case for this specific setup actually hold up" | Yes — `timing=None, exit_plan=None` |
| `strategy-review-reasoner` | `competence-map`, `bot-scorecard`, `closed-trade`, `validated-llm-output` | `strategy-review` (new type), `llm-request`, `part-health` | "which of this system's own bots or detectors is working, and why" | No — feeds the arbiter a qualitative read, not a per-trade call, because it is not making a symbol-specific claim |

`strategy-review` is the one new data type this needs: a per-bot narrative
judgment (`bot`, `assessment`, `confidence`, `reason`, `formed_at_ns`) that
`opinion-arbiter` consumes alongside `bot-weight`, `competence-map` and
`conflict-ruling` — it does not write to `bot-weight` itself, which stays
`bot-weight-sampler`'s alone: one type, one producer, the same rule that
governs everything else this session's fixes protected.

## What market-thesis-reasoner and setup-second-opinion-reasoner actually ask for

Both need a **bounded, named symbol set** per tick, not "reason about
everything" — an unbounded LLM fan-out is an unbounded token bill, and
`part-token-budgeter`/`decision-cost-accountant` already exist specifically to
put a ceiling on that.

- `market-thesis-reasoner`: a settings-named list of bellwether symbols
  (starting point: whatever `captured_symbol_count` ranks top by volume per
  venue, capped small — e.g. 3 per venue), reasoned about once per some
  multiple of the LLM foundation's own refresh cadence, not every tick.
- `setup-second-opinion-reasoner`: only symbols with a live `directional-opinion`
  already calling to act this tick — it reviews real candidates, it does not
  go looking for its own.

Both numbers (bellwether count, review cadence) are operator settings with
provenance, not literals in the code, the same discipline every setting this
session added already followed.

## Cost and safety, before this goes near real capital

- Every LLM call already flows through `llm-request-router`
  (subscription → paid → local, quota/backpressure-aware) and
  `part-token-budgeter`. Nothing here bypasses that.
- `structured-output-enforcer` already refuses a malformed answer and re-asks
  once — a reasoner that gets back nothing usable stands down for that tick
  rather than guessing.
- This stays paper-only, same as everything else in this system, until
  RL-005's promotion gate says otherwise. A vote with no capital behind it is
  the cheapest place to find out whether an LLM's directional call is worth
  anything at all.

## What this does not do

It does not touch `devils-advocate`, `self-model-reporter`,
`opinion-conflict-resolver`, or `opinion-arbiter`'s existing weighing logic
beyond adding three more inputs it already knows how to consume a sibling of.
It does not raise `captured_symbol_count` or change symbol selection. It does
not build the full peer-bot path (B) — that stays a documented option, not a
commitment.

## Build order, if approved

1. `strategy-review` data type + `strategy-review-reasoner` — lowest risk,
   advisory only, no vote, proves the LLM-reasons-about-the-system-itself
   shape works before anything gets a vote.
2. `setup-second-opinion-reasoner` — a vote, but reviewing candidates other
   bots already found, bounded by construction.
3. `market-thesis-reasoner` — the broadest scope, built last once the other
   two have run and their token cost and answer quality are measured rather
   than guessed at.

## Checked

Not yet — this is the proposal. `python3 dashboard/check_contracts.py` and a
blueprint edit script follow only once this is reviewed, the same order every
other design decision this session went through.
