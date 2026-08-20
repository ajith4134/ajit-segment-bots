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
