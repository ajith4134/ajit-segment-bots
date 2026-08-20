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
