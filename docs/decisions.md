# Decisions — ajit-segment-bots

The user's own rulings, recorded when given. Not paraphrased from memory, not
re-derived, not overridden by a later design that finds them inconvenient.
Declining a decision is the user's to do; forgetting one is not.

---

## D-001 — Clean slate now, harvest the old project later

**Given:** 2026-08-20

> "We will start with a clean slate and after we complete and I am satisfied then
> we will compare them to get the missing best parts from the old project"

**What it means for the work now:** the `~/trading-system` design corpus —
`ARCHITECTURE.md`, the segment and brain specs, the failure-mode research, the
87 KB master plan, the 45 `RL-xxx` rulings — is **not** an input to this
blueprint. Nothing is read from it, carried over from it, or justified by it.
This blueprint is drawn from this project's own conversation and its own
research.

**The scheduled second step, which must not be dropped:** once the blueprint is
complete *and the user says they are satisfied with it*, the old project is then
compared against it, to pull across the best parts this design is missing. That
comparison is a real deliverable, not a nicety — it is the reason a clean slate
is affordable.

**Trigger:** the user declaring satisfaction with the blueprint. Not blueprint
completion alone, and never a decision taken without them.

---

## D-002 — The blueprint is the deliverable, and it is shown, not filed

**Given:** 2026-08-20, in the goal (see `goal.md`)

The diagram comes before the bot, the way an architect perfects a skyscraper's
drawings before anything is poured. The user cannot open files on this server, so
every artefact meant to be seen is published as a browser link — standing, not
per-request. The status board and the blueprint both live at links, and both stay
true to what was discussed and to what is actually on the server.

---

## D-003 — Personal project, not distributed: licence is not a constraint

**Given:** 2026-08-20

> "te project w arew buildin is personal project it will not be oted for any one
> so licenes is not a problem"

**Correct, and it is the reason it is correct that matters:** GPL and LGPL
obligations attach to **distribution**. Running, modifying and depending on GPL
code privately, for yourself, triggers nothing. A private GitHub repository is
not distribution either — it is a backup of your own work.

So Freqtrade, Backtrader, Lumibot (GPL-3.0) and NautilusTrader (LGPL-3.0) are
available on the same footing as CCXT and Hummingbot. Dependencies are chosen on
**merit alone** from here.

**The one line worth keeping:** if this ever becomes something distributed — sold,
published, or shipped to another person — that decision comes back. It does not
come back for hosting it as a service, because none of these is AGPL. Noted so
the answer exists later without re-deriving it, not as a caveat on today.

**What this changed:** Freqtrade moved from "careful, copyleft" to the second
most useful item in the set, and NautilusTrader from a licence-flagged reference
to a live one. Backtrader stays deprioritised — for being two years stale, which
was always a quality objection rather than a legal one.

---

## D-004 — Links become skills, not summaries

**Given:** 2026-08-20, alongside C-20

> "like this example I am give what I expect you do when I give you any links"

**Standing instruction.** When the user sends a link — a paper, a book, a repo, a
post, a chat log — the deliverable is a **skill**, not a summary and not a
paragraph of notes.

That means, every time:

1. **Read the source at its own level.** Media, not captions. The
   `instagram-content` skill's evidence is unambiguous: caption-only assessment
   produces false conclusions, and it has produced them here already.
2. **Verify every falsifiable claim** — repos, stars, licences, versions — with
   `gh` or `curl`, never with a summariser. Star counts in posts are routinely
   stale, and two have been wrong in this project already.
3. **Distil into structure**: frameworks, decision rules, anti-patterns, with the
   trigger that says when it applies. Never a summary — a summary is what makes an
   agent answer confidently from nothing.
4. **Say where it lands**, which foundation block it serves, and say plainly when
   the answer is *nowhere*.
5. **Publish it as a link**, since the user cannot open files on this server.

This is the same discipline C-20 asks of the bot. The rule is that the project and
the person building it work the same way.

---

## D-005 — The subscription is the model backend; a paid key is the fallback

**Given:** 2026-08-20

> "for llm feature i needd a featuretat uses my claude pro or maxsubscription in
> to a api llm claude so it emitaes or workes same as te llms or claude api and
> all te resonin"

and, when asked what should happen once that allowance is spent:

> "back fall to cloud llm api keys and for te claude sccout coose model wic
> isfast and cost less after entire dot is completed we need to experement on all
> modes so keep it asopen qution"

**Three rulings in one answer, and they are separate:**

1. **The Claude Pro/Max subscription is the default model backend.** Thinking
   costs allowance, not per-token API money. The mechanism is the Claude Agent
   SDK, and it was proven on this server before the design leaned on it -- a real
   headless call, 2.4s, answer intact.
2. **When the allowance is spent, fall back to a metered cloud API key.** Not
   queue, not go dark. The bot keeps thinking and the cost becomes visible
   instead of the bot becoming silent. The `paid spend ledger` part exists
   because that turns a fixed subscription into a variable bill.
3. **On the subscription account, prefer a fast, cheap model** -- for now.

**The open question, left open deliberately:** which model serves which class of
request is *not* settled. After the whole bot is built, every model is to be
experimented on. Recorded here so it is not quietly defaulted later by whoever
writes the first caller.

**Also decided, by declining to decide:** the stack for this block stays open.
The user chose blueprint rows only and "do not settle stack yet", so C-10 names
the mechanism without naming a language, and no code was written.

**What this changed:** C-10 went from a described block with no parts to seven
parts. Fallback is expressed as a *typed request* rather than a switch, because a
router that turned a caller on would be the exact T-2 breach the transistor rule
exists to prevent.

---

## D-006 — The bot is autonomous: it runs, builds, decides, heals

**Given:** 2026-08-20

> "i need you to addnew foundation feature call autonous feature"

Asked what the block does, the user chose **all four** jobs — runs itself with no
human, builds its own new parts, decides without asking, heals its own breakage —
scoped **global**, one instance for the whole bot. And:

> "andmore i will explann next"

**Recorded as unfinished on purpose.** Four jobs are in. More are the user's to
give, and nothing is invented to fill the space while waiting.

**What the decision forces on the architecture:** all four jobs want to switch
parts on and off, which is the one move T-2 forbids to everything except the
resource governor. The answer is not an exception. Every need becomes data —
`restart-request`, `replacement-plan`, `admitted-part`, `trading-halt` — and the
governor or risk allocation acts on it. Autonomy stays visible in the diagram
instead of running underneath it.

**The blast radius, stated because it is real:** jobs two and three together are a
system that writes its own parts *and* acts without approval, eventually on live
capital. That is the user's call, taken deliberately, and it is recorded as taken.
Two parts exist to keep it a decision rather than a drift: the **part admission
gate**, which lets nothing self-authored into the circuit until it passes the same
contract check the pre-commit hook runs, and the **autonomy boundary**, which
holds what the bot may do unasked in one readable place instead of as an
assumption spread across nine parts.

**What this changed:** 20 foundation blocks became 21, and two existing blocks
gained an inbound edge — the resource governor now consumes restart requests,
admitted parts and replacement plans; risk allocation now consumes a trading
halt. Those are the only two parts of the system allowed to act on what this
block decides.

---

## D-007 — Two codebases read, seven parts taken, the wallet left behind

**Given:** 2026-08-20

> "this is the link u need you to read find the project read its code and inspire and add the
> features to the autonomous and parts to it"

**What was done, in the order D-004 requires:** all three reels read at media level (Whisper
transcript plus frames, never the caption), every falsifiable claim checked at the source, the
named project cloned and its code read, and the result distilled into a skill rather than a
summary -- `sovereign-agent-patterns`.

**Verified, not repeated:** `Conway-Research/automaton` (5,776 stars, 1,271 forks, TypeScript,
MIT) and `WeaveMindAI/weft` (1,868 stars, Rust) both exist and are what the reels said. The
entropy paper is `arXiv:2512.15720`, Mainak Singha, NASA Goddard, and the reel reported its
numbers accurately -- which is worth recording, because in this project's history most reels
did not.

**Seven parts added to C-21**, each traced to a file that was read: survival tier monitor,
conservation planner, autonomy policy engine, self-modification journal, no-progress detector,
upstream improvement watch, folded circuit view.

**Three things deliberately rejected**, because inspiration is not imitation: the crypto
wallet and x402 payment rails, replication into child agents, and "dies if it does not earn".
The first two solve a problem this bot does not have -- it has an owner and a funded account.
The third is strictly worse than standing down for a system trading real capital, and the
graduation gate already applies the right pressure.

**What changed elsewhere:** C-10's fallback was designed as a single cliff -- allowance spent,
switch to a paid key. It is now one rung of a graded ladder, because the automaton's tiers make
the case that scarcity handled binary is scarcity handled badly.

**Not added:** the entropy paper. It is real and it is interesting, but it is prediction, not
autonomy, and its own limits are severe -- 36 days, one instrument, 38.5% of profit from a
single day. Recorded in categories.md so it is not lost. Whether it becomes a part is the
user's call.

---

## D-008 — Order-flow entropy joins prediction, as magnitude only

**Given:** 2026-08-20

> "add the entropy paper as a part to prediction"

`arXiv:2512.15720`, Mainak Singha, NASA Goddard. Three parts in C-08: an order flow state
encoder, a flow entropy meter, an entropy magnitude forecaster.

**Why it is a new chain rather than a new feature on the existing volatility branch:** that
branch reads candles; entropy reads the tick sequence, which a candlestick window cannot see.
Adding it to the feature builder would have made one part do a second job — T-6. It joins as
its own chain and produces the same `volatility-forecast` the other estimators produce, which
makes it the third independent estimator of one quantity and a genuine spare part.

**The constraint that came with it, and it is not negotiable:** entropy is invariant under
swapping buy and sell labels, so it cannot carry direction. 45.0% directional accuracy is a
theorem, not a poor result. This part produces `volatility-forecast` and **never**
`directional-opinion`. Direction remains with bull, bear and profit tailgating (RL-023). The
failure this prevents is specific: a part quietly picking a side from a magnitude signal would
be trading at chance while appearing to have an edge.

**Recorded as unproven, deliberately:** 36 days, one equity ETF, VIX 14-22 throughout, and
38.5% of the paper's profit from one single day. It enters switched off and the ablation
harness decides whether it earns its place. The paper is a reason to build the part, not
evidence that it works on intraday crypto.

---

## D-009 — The six posts' equations are written out, not paraphrased

**Given:** 2026-08-20

> "add tem ... i am talkiin about eqution fron first six intaram pot andreels"

The formulas from the six Instagram posts existed only as shorthand inside the
assessment note, and no part pointed at them. `docs/research/equations.md` now holds
each one written out, with the owning part named and provenance marked line by line
— `[post]` for what the slides taught, `[standard]` for textbook maths the post
named but never wrote, so the two are never confused.

**Seven existing parts** now point at their own maths, including `forecast-scorer`,
which had been scoring forecasts with no stated protocol at all. Its method is now
the neural-network sheet's: walk-forward, time-based splits, no look-ahead,
out-of-sample always net of transaction costs.

**Two parts added**, because the statistical-arbitrage equations had no home:
`cointegration-pair-finder` and `spread-reversion-detector`. The half-life,
`ln(2)/κ`, is what decides whether a pair is usable by an intraday bot at all.

**Two constraints surfaced by writing the maths down, both of which prose had
hidden:**

1. Three of the ten volatility features need an options surface. With spot and
   futures the focus (RL-039), those bots compute **seven of ten**, and their
   regression is a different regression. The part declares what it had; it never
   silently zeroes what it could not compute.
2. Every realised-vol formula annualises by `√252`, the equity trading year. Crypto
   trades 365 days. The constant is wrong here and must be restated before any of
   those numbers mean anything.

Neither would have been caught by copying the formulas as sentences. That is the
argument for this file existing.

---

## D-010 — Backtesting becomes C-22, and it gates the scanner

**Given:** 2026-08-20, choosing it from the open items

The gap had been raised twice and left for the user: five of the ten repositories
they were shown are backtesting frameworks, and the blueprint had no block for it.
C-01 runs on live data, which is *forward* testing — replaying history is a different
thing, and without it every hypothesis has to be proven in forward time at real cost.

**Seven parts:** historical bar store, walk-forward splitter, execution cost model,
instruction replayer, look-ahead auditor, backtest scorer, instruction promotion gate.

**Decided rather than asked, and stated so it can be overturned:** backtesting is a
**gate**, not a report. The scanner now consumes `proven-instruction` instead of
`opportunity-instruction`, so an untested idea cannot reach live scanning because
somebody forgot to check. It is the same shape as the edge graduation gate that
already governs bots. If backtesting should only advise, pointing the scanner back at
`opportunity-instruction` reverses it in one line.

**Also decided by following precedent rather than by ruling:** scope is per-segment,
after RL-019. Recorded with `scope_origin: proposed`, not `user`, so it is never read
back as a decision the user made for this block.

**The reason four of the seven parts are safeguards:** a backtest cannot fail loudly.
It returns a number either way, and a result from a leaking replay is
indistinguishable from a sound one. Nothing currently keeps the past (RL-024 put C-01
on live data), random splits leak the future, costs inherited from an equity paper do
not survive a crypto spread, and a pooled number hides that one day carried 38.5% of
the profit. Each of those is a part rather than a good intention.
