# watch-condition-compiler joins the verdict to the instruction it is about

**Proposed by Claude, 2026-08-25, during the payload-shape sweep.**

## What was found

`watch-condition-compiler` turns a proven instruction into something the scanner
can watch for. It needs the instruction's *body* -- measurement, comparison,
threshold, direction, expectation, horizon -- and read it as:

    body = getattr(instruction, "body", None)
    if body is None:
        compiler.standing.proven_without_a_body += 1
        continue

`ProvenInstruction` is a verdict: `instruction_id`, `runs`, `folds_passed`,
`folds_total`, `net_return`, `trials_before_it`, `survived_refutation`. It has
never carried a body, so **every proven instruction has been counted and skipped**
and no watch condition has ever been compiled from one.

## The change

    watch-condition-compiler  consumes += opportunity-instruction

`OpportunityInstruction` is the body, and it already carries every field this part
reads. `instruction-promotion-gate` proves an instruction by id, and five other
parts already join to the instruction the same way.

## Why the join goes here rather than into the verdict

The alternative is to give `instruction-promotion-gate` the instruction and have
it copy the body into its verdict. That makes the gate consume something it does
not judge, and it puts two copies of an instruction's body on the bus -- which is
the shape that lets them disagree. A verdict naming what it is about, and the one
part that needs the body joining to it, keeps a single writer for each fact.
