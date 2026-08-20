# Foundation categories — ajit-segment-bots

Given by the user on 2026-08-20. These are the main blocks: the foundation of an
advanced multi-storey skyscraper. Every future idea or feature belongs to one of
these categories. A feature that fits none of them means a category is missing,
and a missing category is raised with the user rather than invented.

## The user's words

> I will tell you the features and the main blocks or the consider them the
> foundation to a advance d multi store sky scraper so in future any idea or
> features should be in the categories I mentioned or if you think of a new
> category other than I mentioned then tell me and add it the categories are
> papper trading with live data in that trading option or switch to live real
> money or papper money, universal opportunity scanner this is constantly
> monitoring all the symbols in the entire segment it picks the time when a
> symbol should enter trading it don't know there is papper or live money, and 3
> bots bull , bear and profit tail gating those 3 bots live in side the segment
> bot we will discuss detail when implementing these features, intelligence
> category, learning loop category, hypothesis features, knowledge feature,
> prediction features, online research on internet on decoding the famous or high
> profit crypto trades portfolio to copy their strategy or step up into us, for
> now we start with this and open to others in the future

## The nine categories as given

### C-01 — Paper trading on live data
Trading runs on live market data. Inside it sits the switch: real money or paper
money. Paper is not a replay of old data — it is the live market, with simulated
money.

### C-02 — Universal opportunity scanner
Constantly monitors **all** symbols in the entire segment, not a shortlist. Its
job is to pick the moment a symbol should enter trading.

**Stated constraint:** it does not know whether the money is paper or live. That
is an architectural boundary the user drew, not an implementation detail — the
scanner's output must be identical either way, and the paper/live switch sits
downstream of it.

### C-03 — Segment bot, holding three bots
Three bots live inside the segment bot: **bull**, **bear**, and **profit
tailgating**. Their internals are deliberately not specified yet — the user said
these are discussed in detail at implementation time.

### C-04 — Intelligence

### C-05 — Learning loop

### C-06 — Hypothesis

### C-07 — Knowledge

### C-08 — Prediction

### C-09 — Online research
Research on the internet, decoding the portfolios of famous or high-profit crypto
traders — to copy their strategy, or to step it up beyond theirs.

## What is deliberately not recorded here

The user said "for now we start with this and open to others in the future", and
that the three bots are detailed at implementation time. So:

- **No internals.** What each category contains is not written down until the
  user describes it.
- **No data flow between categories.** Which category feeds which is not yet
  declared. The diagram shows the blocks and says so, rather than drawing an
  inferred flow that would then be mistaken for a decision.

The flow contract is the next thing to establish, and it is the thing the whole
blueprint is judged on.

---

# Added 2026-08-20, after the nine

## Two more from the user

### C-10 — LLM services
> "llm s it isalo important catoery"

Large language models as a first-class part of the system. Contents not yet
described.

### C-11 — Hardware resource governor
> "ai aent wic scanes te ard ware and elps all te processes oe all te featues to
> worke fulley wit out que wit allocatin te codes and ram so and swappin tem fast
> so id any ideal feater is hoging te hardware tis intelleent ai turn off tat and
> turn on te featue tat needs it"

An AI agent that scans the hardware and keeps every other part working **without
queueing**: allocating CPU and RAM, swapping parts fast, turning off a part that
is hogging the machine and turning on the part that needs it.

This category is the reason **R-02** exists (see `contracts.md`). The governor
cannot turn off a part that has no off switch, so the switch became a rule
binding every part rather than a feature of this one.

**Open, to settle when this category is detailed:** how the governor learns a
part's resource appetite — declared by the part, or measured at runtime.

## Six proposed by Claude, approved by the user

Raised because each was structurally absent, not because it seemed nice to have.
All six were approved on 2026-08-20 and carry `origin: proposed` in the registry
so the provenance stays visible.

| id | why it was missing |
|---|---|
| `market-data-feed` | C-02 monitors every symbol in the segment; that data has to arrive, and prediction and paper-fill pricing drink from the same tap |
| `risk-capital-allocation` | the three bots decide direction; nothing decided size, leverage, exposure caps, drawdown limits or the kill switch |
| `ledger` | learning loop, hypothesis and prediction need something to learn from and be scored against |
| `portfolio-state` | the scanner needs to know what is already open before calling a new entry |
| `observability` | forces every part to emit its state rather than have it inferred |
| `execution-venue-adapter` | partial fills, rejects, retries, rate limits. The user chose it standing alone rather than nested inside C-01 |

**Total: 17 foundation categories.** Every future feature belongs to exactly one.
A feature fitting none means a category is missing, and that is raised with the
user rather than invented.

---

# Correction 2026-08-20 — the scanner lives inside the segment bot

> "Universal opportunity scanner should be inside the segment bot"

**C-02 is now nested inside C-03.** The segment bot is a container, and inside it
live four things the user has named:

- the universal opportunity scanner
- the bull bot
- the bear bot
- the profit-tailgating bot

The scanner's own contract is unchanged — it still consumes market data and
current positions, still produces an entry candidate, and still knows nothing
about whether the money is paper or live. What changed is where it sits: the
entry candidate now moves *within* the segment bot rather than into it, and the
segment bot as a whole is what the rest of the system sees.

In the registry this is `parent: segment-bot` on the scanner and
`contains: [opportunity-scanner]` on the segment bot. The diagram renders it as a
box, so containment is visible rather than implied.

---

# Added 2026-08-20 — what an opportunity is, and the loop that closes

## What "opportunity" means (C-02 clarified)

> "what is the opportunities mean in the universe opportunity scanner they may be
> instructions or feeds that can be derivatives from hypotheses feature ... I need
> you to think opportunity can be any thing like if a symbols has sudden increase
> in pride then there can be a minor decrease following after so the we can put or
> short or according to the bot segment"

**The scanner holds no rules of its own.** What counts as an opportunity arrives
as an **instruction** from the hypothesis feature. This is the difference between
a scanner that can only ever find what someone coded into it, and one whose
search widens as the system learns.

An opportunity is a condition plus what it implies. The user's example: a symbol
jumps sharply, a minor pullback often follows, so the bot takes the other side —
**a put in options, a short in futures**, and whatever the equivalent is in spot.
The condition is shared; the response belongs to the segment.

## C-18 — AI brain

> "Now I need a ai brain new feature"

Named, not described. No flow declared, no placement decided.

## C-19 — Closed trade decoding

> "a new feature closed trades decoding feature that create instructions from
> profit trades and loss trades and this goes to the hypothesis feature that can
> think how to turn the loss trades informtion to open profit trade"

Decodes finished trades into instructions and hands them to hypothesis. **Losers
are not discarded** — their information is precisely what hypothesis works on to
produce an opening profit trade.

## The loop this closes

The blueprint previously had a dead end: the ledger produced journal entries that
nothing consumed. These two additions close the circuit.

    portfolio state --closed trade--> closed trade decoding
                    --decoded trade instruction--> hypothesis
                    --opportunity instruction--> scanner
                    --entry candidate--> bull / bear / profit tailgating
                    --trade intent--> risk --> paper/live --> venue
                    --fill--> portfolio state

A trade closes, is decoded, becomes a hypothesis, becomes an instruction the
scanner did not have before, and changes what the system looks for next. That is
the learning loop expressed as data flow rather than as a promise.

## Still open

- `journal-entry` from the ledger is still consumed by nothing. Closed trades are
  proposed as coming from portfolio state, which knows when a position closes —
  but the ledger is what holds everything that led to it. Which one decoding
  reads from is not decided.
