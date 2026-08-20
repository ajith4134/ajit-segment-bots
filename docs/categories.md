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

---

# Interview answers 2026-08-20

## The thinking blocks are peers, side by side

Intelligence, knowledge, prediction, learning loop, hypothesis and the AI brain
are **peers**. The AI brain is not a container and does not absorb the others.
Each is a block like any other, connected by data.

**Still needed:** a one-line job for intelligence, knowledge, prediction and
learning loop. Until the user gives it, they carry no flow rather than an
invented one, and they render in the undescribed box.

## LLM services — utility now, thinker later

Infrastructure first. It makes model calls and returns answers, holds no opinion
about trading, and decides nothing — so there is one place to swap models, cap
cost and cache, and it is a genuine spare part. A calling block names the data
type `llm-request`, never the LLM block itself, which is what keeps T-4 intact.

Its own reasoning role is recorded as a **planned extension**, not as a gap. The
board currently reports that nothing produces `llm-request` and nothing consumes
`llm-response` — correct, because no thinking block is described yet to call it.

## Closed-trade decoding reads from both

Portfolio state signals that a position closed; the ledger supplies that trade's
full history — every scan, intent, sizing and fill that led to it. Decoding sees
the reasoning, not only the result.

**This closes the last dead end.** `journal-entry` was produced by the ledger and
consumed by nothing; it now feeds decoding.

## The 19 edges are accepted provisionally

> "option 1 for now we will deside tem aain wen we lock te desi and start
> implemention pase"

`flow_origin` reads **`agreed-provisional`**, never `agreed`. They are settled
enough to build the rest of the blueprint against, and they are re-opened when
the design is locked and implementation starts. That re-opening is a scheduled
step, not a courtesy.

---

# C-20 — Skills (added 2026-08-20)

> "New foundation feature called skills it reads books , research papers ,
> community chats etc many more and convert them to skills"

Reads books, research papers, community chats and anything else, and converts
each into a **skill** the brains load on demand.

## Why this is not just another store

The memory tiers (C-07 knowledge) settled a hard constraint: **procedural memory
is never searched and always injected, so it is a fixed cost on every single scan
— which is why it must be capped and hold behaviour rather than diagnosis.**

That cap is the problem a skill library solves. A skill is **loaded only when a
question needs it**, so the library can grow without end while costing nothing
until it is used. The two are complementary rather than competing:

| | procedural playbook | skill library |
|---|---|---|
| when read | every scan, always | only when the trigger matches |
| size | **capped**, deliberately small | unbounded |
| holds | the few rules that must always apply | everything worth knowing |
| cost | fixed, paid every time | paid only on use |

## Structure, not a summary

The reference implementation is explicit about this and it is the part worth
copying: a skill is **frameworks, decision rules and anti-patterns with
per-chapter files** — never a summary. A summary is what makes an agent answer
confidently from nothing. Structure is what lets it answer from the real content.

The same argument as episodic memory embedding the *symptom* rather than the fix:
a skill is indexed by **what it is for**, not by its title.

## The five parts

| part | its one responsibility |
|---|---|
| Source ingester | pull a source into text whatever form it arrived in |
| Skill distiller | distil a source document into a skill with its trigger described |
| Skill index | hold every skill so one can be found by what it is for |
| **Skill loader** | **load only the section of a skill that the current question needs** |
| Skill scorer | score whether a loaded skill changed the outcome |

The scorer exists so a skill that never changes an outcome can be **retired**
rather than quietly accumulating. A skill nobody scores is an assertion.

## Reference

`virgiliojr94/book-to-skill` — MIT, **23,262 stars**, pushed 2026-08-19, verified
with `gh`. The screenshot said 16.3k; the repository has 23,262. It emits the open
**Agent Skills** `SKILL.md` format, which Claude Code, Copilot CLI and Amp all
read — so the output is not locked to one host.

---

# The three bots, the brain, and what came out of them (2026-08-20)

## Bull and bear

Each issues a **directional opinion** — a direction with conviction — on an entry
candidate. Never an order. Both learn from their own right and wrong calls, each
with its own features.

**One template in spot, two designs in futures and options.** In spot a short is
just an exit, so the same machine with the direction configured is enough and T-1
is satisfied cleanly. In futures and options the asymmetry is real — funding is
paid rather than received, liquidation is not symmetric, and a short can lose more
than it stakes — so the bear earns a separate design there.

## Profit tailgating, and why it became three parts

The user's description gave it four jobs: ride winners, follow smart money, keep
score of which features preceded profits and losses, and protect open profit so a
pullback locks in gain. All four are good ideas. Four jobs in one part is a **T-6**
breach, and two of them had specific reasons to move:

**Profit protection is not a bot.** It defends whatever is open regardless of which
bot opened it. Inside the tailgater, a bull-opened trade would lose its protection
the moment that bot was switched off — and R-02 says every part can be switched
off. It now lives in **risk and capital allocation**, which already owns stops.

**Keeping score is measurement, not trading.** It runs after trades close, not
during them. It now lives in **learning loop**.

| the idea | the part | where |
|---|---|---|
| ride winners · follow smart money | `profit-tailgater` | segment bot |
| lock in profit on a pullback | `profit-lock` | risk and capital allocation |
| score which features led to profit | `feature-reliability-scorer` | learning loop |

The tailgater keeps one responsibility: **take a position in something already
proven to be working.** It never originates cold — it rides a position already in
profit, or follows a high-profit trader that C-09 decoded. That second half wires
online research straight into the segment bot.

## The AI brain arbitrates

Three opinions in, **one trade intent out**. That is why the brain sits inside the
segment bot rather than beside it, and it is what finally describes C-18.

## Opposing positions: exploration, then discipline

> "at first it can open 2 positions to test which is correct or which is more
> profitable ... but i need option 3 as final"

**While a bot is in exploration**, bull and bear may both open on the same symbol.
It is a live test of which one is right on this kind of setup, and the paired
outcome is exactly what the scorekeeper learns from.

**Once a bot graduates**, opposing positions are allowed only when the regime
classifier reads *mixed* — a deliberate straddle when there is no trend to take a
side on.

**The gate is measured edge**, per bot, per segment. Bull and bear graduate
separately, on their own numbers, not on a trade count or a date.

**The cost, stated once:** two legs means two sets of fees and, in futures, funding
on both sides. In a chop both can lose. That is the price of exploration, and it is
why the phase ends on evidence rather than on a schedule.

---

# C-04 Intelligence — the one global thinker (2026-08-20)

The last block to be described, and the fork was real: by the time the other
nineteen were done, the obvious territory was taken. Prediction owns what happens
next, hypothesis owns turning lessons into instructions, knowledge owns memory,
learning loop owns scorekeeping, the AI brain owns arbitration. Retiring
intelligence was a live option. It named four things instead, none of which
anything else owns.

**Global scope.** It joins the resource governor and observability as one of three
blocks that are not instantiated per segment — and unlike those two, it is a
thinker. Everything else sees only its own market.

## The five parts

| part | its one responsibility |
|---|---|
| Cross-segment exposure watch | watch every segment at once to report total capital at risk |
| Decision quality critic | score whether a decision was sound separately from whether it won |
| Regime break detector | detect when the market has changed enough that what was learned no longer applies |
| Open web reader | read the open web for ideas nobody pointed it at |
| Idea generator | propose strategies nobody here has tried |

## What each one is actually for

**Cross-segment exposure closes a hole the user knowingly accepted.** Choosing to
split everything per segment carried a stated cost: nothing could see total
exposure, so three independent allocators could each stay within their own budget
while the account as a whole was over-committed. This watch is the fix, and it
fixes it without breaking the isolation — it *reports* the total to risk
allocation, which still decides for itself, per segment.

**Decision quality is not outcome.** Learning loop scores what happened. A trade
can win on luck and lose on a correct call, and a system that cannot tell those
apart learns the wrong lesson with perfect discipline. This score now also feeds
the graduation gate, so a bot cannot graduate on a lucky streak.

**Regime break is not regime classification.** The scanner's Hurst classifier
labels the *current* regime — trending, mean-reverting, mixed. This detector
watches for the regime itself breaking, which is the failure that kills systems
that were working right up until they weren't. Its alert reaches both hypothesis,
which can retire instructions, and the AI brain, which can stand down.

**The open web reader goes looking.** Three blocks now touch outside sources and
they are genuinely different jobs: C-09 decodes *named* traders, C-20's ingester
distils a source it was *handed*, and this one searches for ideas nobody pointed
it at.

**The idea generator is the generative counterpart to hypothesis.** Hypothesis is
reactive by design — it turns what happened into instructions. Nothing else
proposes something untried.

## Every block is now described

Twenty foundation blocks, none awaiting a description. What remains open is
correction, not absence.

---

# C-10 LLM services — seven parts, subscription first (2026-08-20)

## What the user asked for

> "for llm feature i needd a featuretat uses my claude pro or maxsubscription in
> to a api llm claude so it emitaes or workes same as te llms or claude api and
> all te resonin"

The model backend for every thinking block is the user's **Claude Pro/Max
subscription**, driven the way an API would be — full reasoning, model choice,
tool use — so the bot's thinking costs subscription allowance instead of
per-token API money.

## The mechanism, measured before it was designed around

The Claude Agent SDK (`claude-agent-sdk`, the Claude Code harness as a library)
authenticates from the subscription login on this server. No `ANTHROPIC_API_KEY`
is involved. Verified here on 2026-08-20 with a real headless call:

    claude -p "Reply with exactly: PONG" --model claude-haiku-4-5 --output-format json
    -> {"result":"PONG","is_error":false,"duration_ms":2433, ...}

Three measured facts the design has to answer to, not three worries:

1. **The allowance is shared.** The same 5-hour and weekly limits serve the
   user's own interactive sessions. A bot running 24/7 (C-11's world) can starve
   its owner out of their own account.
2. **Every call carries harness overhead.** That PONG billed 17,772
   cache-creation tokens of system prompt before it answered four letters. A lean
   configuration strips most of it -- the amount is to be measured, never assumed.
3. **Latency floors at roughly two seconds.** Correct for reasoning, wrong for
   anything sitting on a price tick.

## What happens when the allowance runs out

Given by the user, 2026-08-20:

> "back fall to cloud llm api keys and for te claude sccout coose model wic
> isfast and cost less"

So: **fall back to a metered cloud API key rather than queue or go dark**, and on
the subscription account prefer a model that is fast and cheap in allowance. No
key value enters this repository -- `docs/secrets.md` holds the rule that the repo
carries the inventory while the machine carries the values.

## Routing is done with data types, never with names

The obvious design -- a router that turns the paid caller on when the
subscription runs dry -- is exactly the thing T-2 forbids, and it would give the
clean data flow a second, invisible graph running underneath it.

Instead the router **emits a differently typed request**. It consumes
`llm-request` and produces either `subscription-llm-request` or
`paid-llm-request`; each caller consumes only its own type. Fallback becomes an
edge in the diagram rather than a hidden switch, no part names another part
(T-4), and swapping either caller changes nothing anywhere else.

## The seven parts

| Part | Its one job |
|---|---|
| **LLM request router** | route each request to the cheapest route that still has allowance |
| **Subscription session caller** | answer a subscription-routed request through the Claude subscription session |
| **Metered API caller** | answer a paid-routed request through a cloud model API key |
| **Subscription quota watch** | report how much allowance is left before the next reset |
| **Paid spend ledger** | record what the metered route spends against its ceiling |
| **LLM model picker** | name which model answers a given class of request |
| **LLM response cache** | return the stored answer for a repeated request |

The last three are why this is one block instead of a call scattered through
every thinker: the user's own summary asked for one place to **swap models, cap
cost, and cache**, and each of those is a part rather than a setting.

## The open question the user left open on purpose

> "after entire dot is completed we need to experement on all modes so keep it
> asopen qution"

**Which model each class of request should use is not decided.** For now: fast and
cheap. Once the whole bot is built, every model is to be experimented on. This is
recorded as open, not defaulted -- and because the choice lives in one part, that
experiment is a swap of the model picker, never an edit to a caller. T-6 is what
makes the later experiment cheap.

## What is still not built

Nothing here is code. These are blueprint rows, and the board reports them as
designed rather than as working. The stack for this block is still undecided --
the user declined to settle it, so the subscription caller names the mechanism
without naming a language.

---

# C-21 — Autonomous operation (2026-08-20)

## What the user asked for

> "i need you to addnew foundation feature call autonous feature"

The twenty-first foundation block, and the first one added after the flow closed.
Asked what the block actually does, the user picked **all four** jobs offered,
then said:

> "andmore i will explann next"

So this block is recorded as **deliberately unfinished**. Four jobs are in; more
are coming from the user, and none will be invented in the meantime.

## The four jobs

1. **Runs itself, no human.** Starts, restarts, survives a reboot or a venue
   outage, keeps going with nobody watching.
2. **Builds its own new parts.** Finds a gap in the circuit, writes the part that
   fills it.
3. **Decides without asking.** Acts inside a boundary it holds itself, rather than
   waiting on a human.
4. **Heals its own breakage.** Notices a part that has failed, plans its
   replacement.

**Scope: global** — the user's choice. One instance for the whole bot, like
Intelligence (C-04), not one per segment. Autonomy about the system staying alive
cannot be delegated to three parts that each see a third of it.

## The problem this block creates, and how it is answered

Every one of those four jobs wants to reach for the same forbidden move: **turn a
part on or off**. Restart it. Swap it. Stand it down. That is precisely T-2 — the
gate is a third terminal, and only the resource governor drives it.

So no part in this block ever switches anything. Each one **states a need as
data** and the governor acts:

| The need | The data it becomes | Who acts |
|---|---|---|
| something that should be running is not | `restart-request` | resource governor |
| a faulted part needs replacing | `replacement-plan` | resource governor |
| a new part is fit to enter the circuit | `admitted-part` | resource governor |
| the whole bot should stop trading | `trading-halt` | risk allocation, which owns the kill switch |

This is not a workaround for the rule. It is the rule working: the transistor
document already says *"if part A needs part B running, that is the governor's
problem, expressed through the control plane."* Autonomy expressed as data stays
visible in the diagram. Autonomy expressed as a switch would be the second,
invisible graph the whole architecture exists to prevent.

## The nine parts

| Part | Its one job | Job |
|---|---|---|
| **Unattended run warden** | keep the whole system running with nobody watching | 1 |
| **Venue outage rider** | carry the bot through an exchange outage without human help | 1 |
| **Capability gap finder** | find what the circuit cannot yet do | 2 |
| **Part author** | write a new part that fills a named gap | 2 |
| **Part admission gate** | admit a proposed part only after it passes every contract | 2 |
| **Autonomy boundary** | name which decisions the bot may take without a human | 3 |
| **Trading halt decider** | decide when the whole bot stops trading | 3 |
| **Failing part detector** | spot a part that has stopped behaving | 4 |
| **Part replacement planner** | choose the replacement for a faulted part | 4 |

## Two brakes, and why they are parts rather than good intentions

**The part admission gate.** A bot that writes its own parts can write its own
defects. Nothing self-authored enters the circuit except through this gate, which
runs the same contract check the pre-commit hook runs — applied to the machine's
own output. Job two without this part is self-corruption with good intentions.

**The autonomy boundary.** "Decides without asking" is a boundary, not the absence
of one. Held in a single part, it can be read, changed and audited. Spread across
nine parts as an assumption, it could only ever be discovered *after* it was
crossed. It widens with earned maturity rather than by default.

Said plainly: jobs 2 and 3 together — a system that writes its own parts and acts
without approval, eventually on live capital — are the highest blast radius
anything in this project has. That is the user's call and it is recorded as made.
These two parts are what keep it a decision rather than a drift.

## What is deliberately not here

- **Nothing is built.** Blueprint rows. The board reports them as designed.
- **The block is open.** More jobs are coming from the user.
- **No part names another part.** Not even the replacement planner, which chooses
  from admitted parts as data, and hands the swap to the governor.

---

# C-21 grows from two read codebases (2026-08-20)

## What the user sent

> "this is the link u need you to read find the project read its code and inspire and add
> the features to the autonomous and parts to it"

Three Instagram reels. All three were read at media level -- Whisper transcript plus sampled
frames -- never from the caption, because caption-only reading has produced false conclusions
in this project before. Then every falsifiable claim in them was checked against the source.

| Reel | Claim | Checked |
|---|---|---|
| `100xengineers` | An AI that dies if it does not earn, clones itself if it does. Project "Automaton", infrastructure "Conway" | **True.** `Conway-Research/automaton` -- 5,776 stars, 1,271 forks, TypeScript, MIT. Cloned and read. |
| `sorhan.hq` | A programming language that makes LLMs code faster, foldable, open source | **True.** `WeaveMindAI/weft` -- 1,868 stars, Rust, POC, open source. Docs read. |
| `vince.quant` | NASA physicist's paper: order-flow entropy predicts magnitude, not direction | **True and accurately reported.** arXiv 2512.15720, Mainak Singha, NASA Goddard. 2.89x magnitude ratio, 45.0% directional accuracy, +1,126 bps, 36 days. |

The third one does **not** belong to C-21 and no part was invented for it -- see the end of
this section.

## The seven new parts

| Part | Its one job | Taken from |
|---|---|---|
| **Survival tier monitor** | grade how much runway the bot has left before it must conserve | `survival/monitor.ts` |
| **Conservation planner** | name what the bot sheds at each level of scarcity | `survival/low-compute.ts` |
| **Autonomy policy engine** | rule on each act the bot proposes to take by itself | `agent/policy-engine.ts` |
| **Self-modification journal** | record every change the bot makes to itself | `self-mod/audit-log.ts` |
| **No-progress detector** | spot the bot repeating itself without making progress | `agent/loop-detector.ts` |
| **Upstream improvement watch** | notice when a better version of the bot's own code exists | `self-mod/upstream.ts` |
| **Folded circuit view** | compress the whole circuit into a view small enough to reason over | Weft groups |

## The five ideas that actually changed the design

**Scarcity is graded, never binary.** The automaton runs
`high -> normal -> low_compute -> critical -> dead`, and each rung *changes behaviour* rather
than stopping: cheaper model, slower heartbeat, non-essential work shed. C-10 was designed
with a single cliff -- subscription allowance spent, fall to a paid key. That is now one rung
of a ladder. **And the tier is data, not a part state**: T-5 keeps states `off`/`on`, so the
tier is produced as `survival-tier` and any part can read it without knowing who computed it.

**A threshold crossed for a second is not an event.** The automaton reaches `dead` only after
sixty *continuous* minutes at zero, explicitly so funding has time to arrive. A 24/7 bot with
no grace period kills itself on a blip.

**Authority comes from the trigger, not the actor.** In `policy-engine.ts`, an act the
**heartbeat** started gets the *lowest* authority level -- below one the creator asked for --
and an unknown origin defaults to the same floor. This is the single most transferable idea
for a bot that runs unattended: what it decides to do on its own must clear a higher bar than
what you asked for.

**"Busy" is not "progressing".** The loop detector blocks three identical calls, warns then
enforces on a repeated call *pattern*, and keeps an explicit list of idle-only tools --
checking a balance is not progress. This is the unattended failure that looks healthiest:
every part green, the machine busy, nothing advancing.

**Integrity is a hash, never a file permission.** The constitution is propagated to children
with its SHA-256 alongside, and the code says why: *"Uses SHA-256 hash verification instead of
superficial chmod 444."* The chmod is kept, labelled defense-in-depth, not the mechanism.

## Weft reached this project's architecture from the other direction

Weft is a language; this is a blueprint. They converged:

- nodes declare typed inputs and outputs, never each other's names -- **R-01**
- the graph is *derived* from the code, never drawn by hand -- `render_blueprint.py`
- the compiler refuses a graph that breaks a node's declared wiring rules -- the pre-commit hook
- one source of truth, two views: dense for the machine, visual for the human -- the board

And its group rule, verbatim: *"Child nodes inside a group can only talk to each other and to
`self`. They cannot reference nodes outside the group, and nothing outside the group can
reference a child by name."* That is **T-4**, written by someone who had never seen this
project.

Its foldability answers a problem already flagged here: 66 parts and a `part-health` fan-out
nobody can read. A part that must *write* a new part cannot first read the whole circuit --
so the circuit has to fold, or self-building is capped by context rather than by judgement.

## What was deliberately rejected

The automaton's headline features are specific to a **sovereign agent that sells services to
strangers**, and are wrong here:

- **Crypto wallet, USDC, x402 payments, ERC-8004 identity.** Payment rails for an agent with
  no human. This bot has an owner and a funded account.
- **Replication into child agents with their own wallets.** A different product, not a feature
  of this one.
- **"Dies if it does not earn."** For a trading system this is strictly worse than standing
  down. Losing capital allocation is the right pressure; deleting itself is not -- and the
  graduation gate already applies that pressure correctly.

Copying those would have been the easy way to look inspired. The transferable half is the
survival machinery, and that is what was taken.

## The entropy paper is real, and it is not C-21

`arXiv:2512.15720` belongs to **C-08 prediction** or **C-02 opportunity scanner**: order-flow
entropy over a 15-state Markov chain in a rolling 120-second window, forecasting *magnitude*
while direction stays at chance by mathematical necessity -- entropy is invariant under
swapping buy and sell labels.

It is not added as a part, for two reasons. It is not autonomy, and the user's instruction was
to add to C-21. And the paper's own limits are severe: 36 days, one instrument, VIX 14-22
throughout, and **38.5% of all profit came from a single day**. Recorded here so the finding
exists; a part for it is the user's call, not an assumption.

---

# C-08 gains order-flow entropy (2026-08-20)

## What the user asked for

> "add the entropy paper as a part to prediction"

**`arXiv:2512.15720` — "Hidden Order in Trades Predicts the Size of Price Moves",**
Mainak Singha, Astrophysics Science Division, NASA Goddard Space Flight Center.
Verified at source; the reel that carried it reported its numbers accurately.

## The three parts

| Part | Its one job |
|---|---|
| **Order flow state encoder** | label each second of trade flow as one of fifteen states |
| **Flow entropy meter** | measure how structured the recent order flow is |
| **Entropy magnitude forecaster** | forecast how far price moves next from how structured the flow is |

The mechanism, in the paper's own terms: each second is labelled by the sign of the price
change `{-1,0,+1}` crossed with the volume quintile `{1..5}` — fifteen states. A 15x15
transition matrix is estimated over a rolling 120-second window, its stationary distribution
taken by eigendecomposition, and entropy computed as the stationary-weighted average of row
entropies, normalised by `log 15`. Low entropy means structure: informed traders leaving a
footprint.

## Why three parts rather than one feature on the existing branch

The volatility branch already here — `volatility-feature-builder` → `realised-vol-regressor` —
is built on **candles**. Entropy is built on the **tick sequence**, which a candlestick window
cannot see at any resolution. Folding it into the feature builder would have been making a part
cleverer so it could also do a second job, which is exactly what T-6 forbids. So it enters as
its own chain, and its output joins the same data type the other estimators produce.

That makes it the **third independent estimator of the same quantity**, beside the realised-vol
regressor and the implied surface. Three estimators of one number is not duplication here — it
is what "replaceable spare part" means. If entropy is better, it wins on measured accuracy and
the others stay switched off.

## The one thing this part must never do

**It cannot carry direction, and that is a theorem, not a weak result.**

Entropy is invariant under swapping the "buy" and "sell" labels: an informed buyer and an
informed seller produce the same entropy signature. So the measure detects *that* a large move
is coming without revealing *which way*. The paper measured 45.0% directional accuracy —
statistically indistinguishable from chance — and predicted exactly that in advance from the
symmetry.

The design consequence is hard: **this part produces `volatility-forecast` and never
`directional-opinion`.** Direction stays where the user put it — bull, bear and profit
tailgating (RL-023). A part that quietly used a magnitude signal to pick a side would be
trading on 45% accuracy while believing it had an edge, and nothing downstream would be able
to tell.

Worth keeping alongside that: in the paper's own trading rule, profit attribution was **87.8%
from timing, 12.2% from payoff structure, 0.0% from direction.** The value is knowing *when*,
paired with tight stops.

## Unproven here, and the board should say so

The paper's limits are severe, and its author states them plainly:

- **36 trading days, one instrument** — SPY, an equity ETF. This project trades intraday crypto.
- **VIX 14-22 throughout.** Behaviour in a high-volatility regime is unknown.
- **38.5% of all profit came from a single day** (October 29). Concentration, not a distribution.
- Execution assumed immediate fills at fixed cost.

So it enters as a part like any other: switched off until it earns its way on. The **ablation
harness** already exists to measure what breaks when a part is switched off, and that is what
decides whether this one stays — not the paper, and not the fact that the mechanism is elegant.

## The equations, saved (added 2026-08-20 after the user asked)

The section above described the mechanism in prose and saved **not one formula** — no
state definition, no entropy expression, no threshold, no cost model. A part built from
that prose would have been guessed at.

**`docs/research/order-flow-entropy.md` now holds the complete specification**, read two
ways and cross-checked: the paper's own text, and the frames of the `vince.quant` reel,
which carry boards the transcript never mentions.

What was recovered from the reel frames specifically:

    H_t = −Σ_ij π_i P_ij log P_ij

and the numerical symmetry demonstration — swap B and S in a transition matrix and the
entropy is unchanged at **0.847** either way, which is the whole argument for why
direction is unrecoverable rather than merely difficult.

And the piece the earlier write-up missed entirely, **the trading rule**:

    Entry:      entropy < 5th percentile
    Filter:     volume > 95th percentile
    Direction:  5-min trailing momentum
    Exit:       5 bps stop-loss / 300s

That rule is recorded but **deliberately not built into C-08**. Prediction says how far
price moves. When to enter is the scanner's job, stops are risk allocation's, and
direction belongs to bull, bear and profit tailgating. Importing the paper's momentum
heuristic into a prediction part would smuggle a direction call into a block with no
business making one — and by the paper's own attribution that heuristic earned **0.0%**.

The research file also carries five things that must be decided before any of it is
implemented on crypto: the cost model (SPY's 1.57 bps round-trip does not survive the
move), whether the second-resolution clock survives under 1m-30m bars, per-symbol volume
quintiles, the fact that every threshold is *trained* rather than constant, and the
5-20 bps trailing filter that actually controls how often the part fires.

---

# The six posts' equations, written out (2026-08-20)

## What the user asked for

> "add tem ... i am talkiin about eqution fron first six intaram pot andreels"

The six Instagram posts read earlier carried real formulas. Most of them had
already become **parts** — the volatility features, the memory tiers, the Hurst
classifier, the Drake decomposition. What had **not** happened is that the
equations themselves were never written out: they sat in shorthand inside the
assessment note, and not one part pointed at them.

That is the same defect already corrected once for order-flow entropy. A part
built from shorthand is a part that was guessed at.

**`docs/research/equations.md` now holds every one**, each with the part that owns
it named beside it, and provenance marked on every line — `[post]` for what the
slides actually taught, `[standard]` for textbook maths the post named without
writing out, so the two can never be confused.

## Seven parts now point at their own maths

| Part | Gets |
|---|---|
| Volatility feature builder | all ten feature formulas |
| Realised-vol regressor | the ten-term regression |
| Volatility gap detector | the `E[RV₁₀]` against `IV₃₀` comparison |
| Mean reversion detector | z-score, thresholds, four failure conditions |
| Regime classifier | Hurst's three bands |
| Expectancy decomposer | the Drake decomposition, factor by factor |
| Forecast scorer | **its scoring protocol**, which it did not have |

## Two things the equations exposed

**Statistical arbitrage had equations but no part.** `Spread = A − βB`,
Ornstein-Uhlenbeck, ADF/KPSS, Engle-Granger, Johansen — all sitting in the corpus
with nowhere to live. Two parts now carry them:

- **Cointegration pair finder** — find pairs of symbols whose spread has held
  together → `cointegrated-pair`
- **Spread reversion detector** — flag a cointegrated spread that has stretched far
  enough to snap back → `entry-candidate`

The half-life is the part that decides whether this is usable at all:
`half-life = ln(2)/κ` turns the reversion speed into a holding period, and a pair
with a three-day half-life is real and useless to an intraday bot on 1m–30m bars
(RL-043).

**The forecast scorer had no method.** It scored forecasts with no stated protocol.
The neural-network sheet supplies one: walk-forward validation, time-based splits
never random, no look-ahead bias, and out-of-sample evaluation **always net of
transaction costs**. Recorded as its method rather than as a new part.

## Two constraints that must not be papered over

**Three of the ten volatility features need an options surface.** ATM implied vol,
term slope and put skew cannot be computed from candles. With spot and futures the
current focus (RL-039), **those two bots can compute seven of ten** — and the
regression they run is a different regression from the options bot's. The part has
to declare which features it actually had, never silently zero the missing ones.

**The annualisation constant is wrong for crypto.** Every realised-vol formula in
that post annualises by `√252`, the equity trading year. Crypto trades 365 days
(RL-018, RL-020). The constant has to be restated before any of those numbers mean
anything — and it is exactly the kind of detail that survives unnoticed when a
formula is copied as prose instead of written down.

## What deliberately carries no equation

The memory-tier post is structural, not mathematical, and is already the three
`knowledge` parts. The ten-repositories post carries no maths and lives in
`upstream_dependencies`. And five of the six equations in the "six equations" post
— Schrödinger, Riemann, Euler, Einstein, Navier-Stokes — carry nothing usable
here. Two have real but *indirect* links to finance that **the post never makes**;
claiming them from it would be dressing up a guess.

Every performance figure in those six posts remains decoration. None appears in the
equations file.

---

# C-22 — Backtesting (2026-08-20)

## Why this block exists

It was raised as a gap, twice, and left for the user to call:

> **Five of the ten repositories are backtesting frameworks, and this blueprint has
> no backtesting block.** `paper-live-trading` (C-01) runs on *live* data — that is
> forward testing. Replaying history is a different thing, and it is how an
> opportunity instruction would be tested before the scanner is ever told to watch
> for it. Without it, every hypothesis has to be proven in forward time at real cost.

The user chose to add it.

**The distinction that makes it a separate block:** C-01 proves an instruction in
*real time at real cost*. C-22 proves it in *past time at no cost*. Both are needed,
and neither substitutes for the other — forward testing is the only honest test, and
backtesting is the only cheap one.

## The seven parts

| Part | Its one job |
|---|---|
| **Historical bar store** | keep the recorded history a replay reads from |
| **Walk-forward splitter** | split history into training windows that never overlap the test window |
| **Execution cost model** | charge each simulated fill what it would really have cost |
| **Instruction replayer** | replay one opportunity instruction over recorded history |
| **Look-ahead auditor** | refuse a replay that used information it could not have had |
| **Backtest scorer** | score what a replay earned after costs |
| **Instruction promotion gate** | let an instruction reach the scanner only after it has survived replay |

## It is a gate, not a report

**The scanner now consumes `proven-instruction`, not `opportunity-instruction`.**

Hypothesis writes an instruction; it goes to backtesting; only what survives reaches
the scanner. An untested idea cannot reach live scanning because somebody forgot to
check — the same shape as the edge graduation gate that already governs bots: earn
the promotion, never assume it.

This is a design call, and a reversible one. If backtesting should only *advise*,
point the scanner back at `opportunity-instruction` and the gate becomes a report.

## Four parts exist because a backtest cannot fail loudly

This is the thing that makes backtesting dangerous rather than merely useful: **it
returns a number either way.** A result from a leaking replay looks exactly like a
result from a sound one. So the safeguards are parts, not habits:

**Nothing keeps the past yet.** C-01 runs on live data by the user's own decision
(RL-024), so the *historical bar store* is what makes a replay possible at all,
rather than an assumption that history is lying around somewhere.

**Random splits leak the future.** Both research sources insist on the same protocol
independently — the neural-network sheet gives walk-forward validation with
time-based splits and never random ones; `arXiv:2512.15720` runs 10 days training
against 5 days testing across five non-overlapping folds with thresholds frozen after
training. A random split on a time series produces a lie with a Sharpe ratio attached.

**Costs are where backtests lie most often.** The entropy paper's rule cleared
1,126 bps against a **1.57 bps** round-trip cost calibrated to SPY's 0.7 bps spread.
On a crypto venue with a wider spread the same rule may clear nothing. Costs are a
per-venue measurement here, never a constant inherited from an equity paper.

**Pooled numbers hide the answer.** The scorer reports per fold and reports how
concentrated the profit was across days, because the same paper looks like +1,126 bps
pooled while **one single day carried 38.5% of it**. Pooling is what turns one lucky
afternoon into an apparent edge.

## Prior art, and a candidate dependency

Five of the ten repositories already recorded in `upstream_dependencies` are
backtesting frameworks. **VectorBT** is vectorised for sweeping thousands of parameter
sets rather than one run; **NautilusTrader** is an event-driven core built explicitly
for correctness under load; **Freqtrade** has the whole shape solved including paper
trading before real money. The instruction replayer is a candidate for a dependency
rather than a rewrite, and that decision belongs to the implementation phase.

## Scope

**Per-segment**, following RL-019 — each segment is its own bot with its own
architecture. Marked `scope_origin: proposed`, not `user`: it follows the pattern the
user set rather than a scoping decision they made for this block specifically.
