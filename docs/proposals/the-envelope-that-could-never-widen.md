# The envelope that could never widen, and the halt that followed from it

Proposed by Claude, 2026-08-26, after the bot formed 31 convictions, arbitrated
219 symbols, produced 33 order intents and placed none of them.

## What was measured

    opinion-arbiter        intents_formed 33, symbols_arbitrated 219
    position-sizer         refused_no_risk_allowed 33
    halt-enforcer          is_halted 1, zero_limits_issued 444,545
    autonomy-boundary      issues 49,908, widenings 0, narrowings 0
    trading-halt-decider   halts 2, resumptions 1

`position-sizer` refuses any intent whose risk limit fraction is zero.
`halt-enforcer` issues a zero limit for every symbol while a halt stands.
`trading-halt-decider` halts whenever the autonomy envelope does not permit
trading. The envelope has never permitted trading, and `widenings 0` says it
never will.

## Why it never will

`autonomy-boundary` starts at `observe-only` and widens one level at a time,
where each level demands **all** of its conditions sustained. Two of those
conditions cannot be met by a system that has not traded:

- `COMPETENCE_FELL` — competence is the share of judged `(bot, regime)` records
  that are mature. With no closed trades there are no maturity records, so
  `self._competence is None`, which the check treats as below every bar.
- `A_MODIFICATION_BROKE_SOMETHING` — clean modification records required, and a
  system that has changed nothing has none.

Trading needs `act-within-limits`. Reaching `act-within-limits` needs competence.
Competence needs closed trades. Closed trades need trading. The loop has no entry
point, and nothing in the system reports it as a fault: every part is behaving
exactly as designed, and the design has no way to start.

## What this proposes

**On paper, the envelope's floor is `act-within-limits`.**

The envelope answers "what may this system do on its own". Its asymmetry —
narrowing needs one reason, widening needs all of them — is about *risk taken
without a human*, and on paper there is no risk to take: no capital moves, and
RL-005 makes paper-with-no-restrictions the method by which anything is learned
at all. Acting on paper is not a privilege the system earns by acting; it is how
it earns everything else.

So `autonomy-boundary` reads `money-mode`, and:

- **Paper**: the issued level is never below `act-within-limits`. The level the
  evidence supports is still tracked and still has to be earned; the floor only
  raises what is *issued*.
- **Live**: unchanged. The floor is `observe-only` and every level is earned.
- **`may_change_settings` and `may_admit_parts` follow the earned level, never
  the floor.** A system that has not demonstrated anything may trade on paper;
  it may not rewrite its own settings or admit new parts on the strength of a
  money mode.
- **A human override still narrows through the floor.** Nothing in this system
  may conclude that a person did not mean it, and that includes this.

This is the same distinction `trading-halt-decider` already draws for the
survival tier: an unmeasured tier is the most restrictive one *about spending
money with a provider*, and reading it as a reason to halt paper trading would be
"RL-005 inverted" (that part's own words, 2026-08-25). The envelope was making
exactly that mistake about competence.

## What it does not do

- It does not touch `live-switch-guard`. Live money still requires enough closed
  trades, positive after costs, drawdown inside tolerance and enough elapsed
  time — all four.
- It does not widen to `modify-itself`. Nothing admits a part or changes a
  setting because the money is fake.
- It does not remove any narrowing reason. A fault, an override or unreachable
  exposure narrows on paper exactly as before, down to the floor and, for an
  override, through it.

## The blueprint edit

`autonomy-boundary` gains `money-mode` in `consumes`.
`dashboard/blueprint_edits/apply_2026-08-26_the_paper_floor.py`, idempotent,
checked by `dashboard/check_contracts.py`.
