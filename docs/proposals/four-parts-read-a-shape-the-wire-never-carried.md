# Four parts read a shape the wire never carried

**Proposed 2026-08-29. Revised twice the same day: round 1 of adversarial
review found 3 of 4 original fixes rested on false premises about the code;
round 2 found the round-1 rewrite itself computed a wrong quantity in one fix,
left another still crashing, and mis-keyed a wire type that carries no such
key. Both rounds verified against live source and the journal, not against
each other's claims.**

`lesson-extractor` has restarted 708+ times in six hours and appears in 42,301
journal lines over 14 days. It has never written a single instruction since
the part existed. Six hours of journal, same morning, all five defects live:

    836  ValueError: 'sequence:...' is not a change anything downstream can apply
     73  AttributeError: 'StopAudit' object has no attribute 'venue_id'
     36  TypeError: BullEntryTimer.observe_entry_quality() missing 2 required arguments
     36  TypeError: BearEntryTimer.observe_entry_quality() missing 2 required arguments
     36  AttributeError: 'ExitCounterfactual' object has no attribute 'venue_id'
     16  AttributeError: 'StopAudit' object has no attribute 'symbol'

## What was found

T-4 forbids a part from importing another part's module, so a part consuming
a wire it doesn't produce declares its own local class of the same name,
matching the wire's shape by hand. Five sites got the hand-matching wrong:

- **`lesson-extractor`** reads `stop-audit` and does `audit.symbol`,
  `audit.venue_id`. The real `StopAudit` (`runtime/trade_decoding_types.py`,
  published by `stop-placement-auditor`) carries `trade_id`, `stop_price`,
  `distance`, `typical_movement`, `distance_in_typical_movements`, `was_hit`,
  `would_have_recovered`, `verdict`, `is_measurable`, `reason`,
  `audited_at_ns` -- no `symbol`, no `venue_id`. Both lines are live: which one
  fires depends on which wire has payloads on a given tick (`read_evidence`
  builds the whole job list, audits included, before `tick()` calls
  `observe_evidence`), so the whitelist `ValueError` and the `AttributeError`
  are two faces of one tick, not two sequenced failures.

- **`bull-entry-timer` / `bear-entry-timer`** read `entry-quality` and call
  `timer.observe_entry_quality(entry)` -- the whole payload, positionally,
  into a method whose signature is `(detector, extension_at_entry,
  given_away)`. The real `EntryQuality` carries `trade_id`, prices, and a
  percentile against the reachable-price window; it has never carried a
  detector name, because it is built from `ClosedTrade`, which doesn't carry
  one either.

- **`tail-trailing-exit-planner`** reads `exit-counterfactual` and does
  `counterfactual.venue_id`, `.trail_fraction`, `.trail_was_too_tight`. The
  real `ExitCounterfactual` compares the trade against **named alternative
  exit rails** -- `fixed-target`, `fixed-stop`, `trailing-stop`, `time-exit`
  -- and carries `rule_name`, `exit_price`, `realised_pnl`, `difference`,
  `is_hindsight`. It has never carried a trail **width**.

- **`bull-exit-plan-proposer` / `bear-exit-plan-proposer`** also read
  `stop-audit` and do `audit.venue_id`, `.symbol`, `.worst_reversal_excursion`
  -- the third field doesn't exist on the real type at all. Confirmed live in
  the same six-hour window (73 hits), not merely predicted.

- **`exit-counterfactual-replayer`** (the producer, not a hand-matching
  consumer) has an independent bug that starves its own output: `start_part`
  keys an **open** position's tape entry with the literal string
  `f"{venue}:{symbol}:pending"`, but `replay()` looks the tape up by
  `closed_trade_id(trade)` -- the real id, computable only once the trade
  closes. The two keys never match. `_tape` is `dict[str, list]` keyed by
  whatever `observe_tape(trade_id, ...)` is called with, and it is only ever
  called with the placeholder while a position is open, so `_tape.get(closed_
  trade_id(trade))` at replay time is always empty. `exit-counterfactual` has
  almost certainly returned `NO_TAPE` for essentially every replay since the
  part was written. This fixes exactly one of its three consumers --
  `holding-horizon-profiler` and `exit-timing-learner` are dark for unrelated,
  independent reasons (see the "Explicitly out of scope" section) and are not
  claimed as fixed here.

`loss-cause-classifier` and `stop-target-placer`, the two other consumers of
`stop-audit`, are unaffected: the classifier reads only real fields
(`.verdict`, `.is_measurable`, `.distance_in_typical_movements`,
`.would_have_recovered`), and the placer drains the wire without reading any
field.

A note on what "not yet observed crashing" would have been worth, had this
proposal still needed to say it anywhere: `failing-part-detector`'s
`observe_crash()` is defined but never called anywhere in the repository, so
`CRASHED` -- its one `FATAL`, non-silent state -- can never fire, and a
crashing part is scored `SUSPICIOUSLY_PERFECT` instead. That is a distinct
root cause in a different file, tracked as its own proposal, and is why every
claim in this document is backed by a direct journal grep rather than by the
board's own reported health.

## What has to change

Four fixes. `exit-counterfactual-replayer`'s tape bug and `tail-trailing-exit-
planner`'s crash share one file and land together in #3.

### 1. `lesson-extractor` -- identity, the whitelist, and the republish storm both unmask

Add `venue_id` and `symbol` to the real `StopAudit`. `stop-placement-auditor`
already holds the source `ClosedTrade`, which carries both, at its one
construction site (`_audit`).

This lands in the same commit as replacing `EXPRESSIBLE_CHANGES` (nine
hyphenated verb phrases nothing has ever consumed) with validation against the
four real colon-prefixed families `read_evidence()` actually builds --
confirmed already load-bearing elsewhere: `cross-segment-lesson-bridge`
already maps exactly `{"stop", "pnl-from", "sequence", "detector"}` to
mechanisms, and `loss-inverter` already parses `instruction.change` expecting
this shape (`detector_of()` splits on `":"`). The four families, each checked
against its own finite set:

  - `stop:*` against the `StopAudit` verdict constants (`INSIDE_THE_NOISE`,
    `TOO_WIDE`, `WELL_PLACED_AND_HIT`, `WELL_PLACED_AND_NOT_HIT`,
    `NEVER_APPROACHED`, `NO_STOP`, `NOT_MEASURABLE`).
  - `pnl-from:*` against `PNL_COMPONENTS`, which already exists in
    `runtime/trade_decoding_types.py`.
  - `sequence:*` against the `SequencePattern` kind constants (`STREAKS`,
    `SIZE_DRIFT`, `SESSION_DECAY`, `OUTCOME_CONDITIONING`).
  - `detector:*` open, matching how a detector name is treated everywhere
    else in this codebase.

All four constant sets move to `runtime/trade_decoding_types.py`, alongside
the existing `PAIR_VERDICTS`, never imported from the producing part --
verified no part imports another today (`grep -rn "from parts\." parts/` is
empty) and `check_contracts.py`'s T-4 check is blueprint-only, so this would
not otherwise be caught.

Two corrections found on the second review round, both binding:

  - **`sequence:*` cannot be keyed by symbol.** `SequencePattern` (`runtime/
    trade_decoding_types.py`) carries `pattern_id, description, kind,
    trades_examined, occurrences, effect, is_significant, reason,
    found_at_ns` -- no `symbol`, and it cannot have one: `sequence-pattern-
    miner` holds one flat, process-wide trade list with no per-symbol
    dimension. Its four kinds are genuinely global findings. Evidence for
    `sequence:*` is keyed on `kind` alone, matching the data's real shape,
    not `(kind, symbol)` as first drafted.
  - **The whitelist fix unmasks an unbounded republish, and it is not
    confined to `sequence:*`.** `sequence-pattern-miner` mints a new
    `pattern_id` every tick and republishes all four kinds unconditionally,
    every tick, with no change-detection -- exactly the "a level is not an
    event" failure this project already paid 89,747 msg/s for on
    2026-08-26. Separately, `lesson-extractor`'s own `tick()` re-extracts and
    republishes **every** accumulated evidence key on **every** tick, forever,
    once it first clears the bar -- `extract()` mints a fresh
    `instruction_id` and `written_at_ns` unconditionally on every call, and
    nothing compares against what was last published. This is true of all
    four families, not only `sequence:*`; `sequence:*` is simply the family
    that would fire first, and most, because its evidence keys collapse
    fastest. Two independent fixes, both required:
      - `sequence-pattern-miner` wraps its publish in a `LevelPublisherByKey`
        (`runtime/level_publishing.py`) keyed on `kind`, comparing content
        with `pattern_id` and `found_at_ns` excluded (the existing
        `without_observation_time` convention) -- republish only when a
        kind's underlying finding actually changes.
      - `lesson-extractor`'s `tick()` only calls `publish_instructions` when
        the newly extracted instruction's content differs from
        `self._written.get(key)` (excluding `instruction_id`,
        `written_at_ns`), the same comparison shape.
  - `OPPOSITES` is rebuilt over the four families
    (`stop:INSIDE_THE_NOISE`/`stop:TOO_WIDE` are real opposites) so
    `refused_contradicted` is reachable again rather than permanently zero
    (Rule 8).
  - `loss-inverter`'s `detector_of()` strips *any* prefix; guard it to unwrap
    only `detector:*`, so a verdict, component or pattern kind never
    masquerades as a detector name in `LossCause.detector`.
  - `describe_lesson_extraction`'s `expressible_changes` key is rebuilt from
    the four families rather than left exporting the deleted nine-item tuple.

### 2. `bull-entry-timer` / `bear-entry-timer` -- given_away, matched by identity and time, not by a single overwritten slot

Confirmed decision: build this properly via `trade-episode`
(`venue_id`/`symbol`/`detector`/`action`/`opened_at_ns`/`conditions`, already
carrying `entry_percentile` from `trade-episode-encoder` -- no changes needed
there or in `entry-quality-scorer`), rather than a stop-the-crash patch, even
though nothing currently reads the value this ultimately feeds
(`EntryTiming.quality`).

The first draft's mechanism was wrong: a single slot per `(venue_id, symbol)`,
overwritten on every `ENTER_NOW`, would pair `given_away` from whichever trade
happens to close with `extension_at_entry` from an unrelated, likely
never-traded decision -- decisions arrive continuously, episodes arrive only
once a trade closes, minutes to hours later, and the slot is overwritten
hundreds of times in between. Corrected mechanism:

  - Drop `entry-quality` from `consumes`; add `trade-episode`.
  - On `ENTER_NOW`, append `(extension_at_entry, decided_at_ns)` to a bounded
    deque keyed on `(venue_id, symbol, detector)` -- not just
    `(venue_id, symbol)`, so two detectors active on the same symbol don't
    share a queue. Bounded by a named setting (length and max age both --
    RL-061), evicting the oldest entry past either bound so an entry whose
    trade was never taken does not wait forever (T-3).
  - When an episode arrives, filter on `episode.action` matching the bot's
    own side (`long` for bull, `short` for bear) -- `trade-episode` is not
    side-specific, and without this filter the bear timer would learn from
    bull trades on the same symbol and vice versa. Look up the deque for
    `(episode.venue_id, episode.symbol, episode.detector)` and pop the oldest
    entry whose `decided_at_ns <= episode.opened_at_ns`.
  - `entry_percentile` cannot be `None` on this wire (`entry-quality-scorer`
    only publishes when `is_usable`, and the usable branch always sets a
    float), so `given_away = 1.0 - entry_percentile` needs no null guard --
    it is only floored at zero as a defensive bound, not because the
    unguarded value is ever negative.
  - Call `observe_entry_quality(episode.detector, extension_at_entry,
    given_away)` and drop the matched entry.
  - This is an approximate temporal join (FIFO within a bounded window, not
    an exact trade-id match, since the timer's decision carries no trade
    identity it could hand forward) and is documented as such rather than
    presented as exact.

### 3. `exit-counterfactual-replayer` + `tail-trailing-exit-planner`

Five changes, one commit, because the crash and the tape bug are in the same
producer and the trail-advancing fix depends on the same read loop:

  - **Fix the tape key.** Key `_tape` by `(venue_id, symbol)` instead of
    `trade_id`. `replay()` already has `closed_trade.venue_id`/`.symbol` in
    scope. The only real "position closed" signal this part has is
    processing a `closed-trade` payload for that symbol -- there is no
    observable "position opened" event on its current wires -- so
    `read_jobs()` releases `(venue_id, symbol)`'s tape immediately after
    emitting that trade's replay jobs, right where `open_symbols.pop(key,
    None)` already runs today. The `plans` dict of `LatestByKey` readers
    gains a `maximum_age_seconds` bound (RL-061), since without one it
    accumulates tape for every symbol that has ever had an exit plan, not
    only symbols with a currently open position (T-3).
  - **Give `ExitCounterfactual` a trail-width field.** Adding
    `venue_id`/`symbol` alone does not stop the crash: `.trail_fraction` and
    `.trail_was_too_tight` are on the tail planner's *local* mirror class,
    not on the real payload, and neither exists there after an identity-only
    fix. Add a third field -- the replayed rule's width parameter -- to the
    real `ExitCounterfactual`.
  - **Feed the already-implemented, never-used `TRAILING_STOP` alternative.**
    `start_part` only ever emits `FIXED_STOP`/`FIXED_TARGET` jobs from
    `bull-exit-plan`/`bear-exit-plan`/`tail-exit-plan`. Add a job from
    `tail-exit-plan`'s `risk_fraction`, converted to the **absolute price
    distance** `replay()`'s `rule["distance"]` actually expects
    (`risk_fraction * entry_price` -- `risk_fraction` is a fraction of price,
    `rule["distance"]` is not, and feeding the fraction directly would fire
    the trail on the first print of every trade).
  - **Rewrite `tail-trailing-exit-planner`'s consumption.** Its local mirror
    class and `observe_exit_counterfactual` body are rewritten against the
    corrected real fields, filtered to `rule_name == TRAILING_STOP` -- without
    that filter it would also feed fixed-target/fixed-stop counterfactuals
    into a trail-width estimator.
  - **Make the trail actually advance.** Confirmed decision: add `position`
    as a new `consumes` edge (precedented -- `tail-winner-selector` already
    consumes it). `advance_trail`/`forget_position` already exist and are
    already correct (ratchets with `max`/`min`, never loosens, releases on
    close) -- they are simply never called from `start_part`, only from
    tests. The bug is in `plan()`, which overwrites `_standing_trails[key]`
    unconditionally on every candidate, clobbering any prior advancement.
    Fixed shape: `plan()` seeds `_standing_trails`/`_entry_prices` only the
    first time a key appears (`if key not in self._standing_trails`) and
    otherwise reports the already-advanced value; on every price print for a
    symbol with an open, non-flat position, `read_candidates_and_market`
    calls `advance_trail(venue_id, symbol, position.direction, price)`; on
    `position.is_flat`, it calls `forget_position(venue_id, symbol)`.

### 4. `bull-exit-plan-proposer` / `bear-exit-plan-proposer` -- the right quantity, available immediately

The second review round found the first fix computed the **wrong** quantity:
`(max(after) - entry_price) / entry_price` measures a *favorable* recovery
after a stop hit, but the consumer (`widened_to = audit.worst_reversal_
excursion * self._stop_multiple`, compared against and substituted for
`adverse_excursion` -- how far a winner goes *against* itself) needs an
*adverse* magnitude. A trade that stopped out and then rallied 8% would have
set an 8%-below-entry stop on the next trade under the first version.

The corrected quantity is already available with no deferral at all:
`worst_price` is a parameter `audit()` already receives, so
`(entry_price - worst_price) / entry_price` for a long (mirrored for a short)
is exactly "how far against the trade it went", computed at audit time, same
units as `adverse_excursion`. This also means the ordering bug that makes
`would_have_recovered` structurally always `None` in production (`audit()`
runs in the same tick a trade closes, before any post-exit price can arrive)
does not need fixing to unblock this proposal -- deferred, see below.

`self._audits[(venue, symbol)] = audit` is last-write-wins per symbol, so a
per-trade magnitude published under a name suggesting an aggregate
(`worst_reversal_excursion`) would be a name that lies (Rule 7): it tracks
whatever trade last closed on that symbol, not the worst ever seen. Renamed
to reflect exactly that -- the most recent trade's adverse excursion, not a
running maximum -- consistent with how every other field on this audit is
already latest-write-wins.

Add `venue_id`/`symbol` (as in #1) and this field to the real `StopAudit`,
constructed together at the one `_audit` call site. Both proposers' local
`StopAudit` mirror classes -- which still carry invented fields
(`stops_hit`/`stops_hit_then_reversed`/`reversal_fraction`) the real payload
will never carry -- are deleted as part of this fix; each proposer reads the
real fields plus the new one directly, so no local mirror is needed once #1's
identity fields and this section's magnitude both exist on the wire.

## Explicitly out of scope, tracked separately

  - **`observe_crash()` is never called; `CRASHED` is unreachable.** Different
    file (`failing-part-detector`), different root cause, own proposal.
  - **`would_have_recovered` is structurally always `None` in production**
    (`stop-placement-auditor` audits in the same tick a trade closes).
    Real and documented (`loss-cause-classifier` reads it), but not blocking
    any of the four fixes above -- #4 needs only `worst_price`, already a
    parameter. Also: even once deferred, `observe_price_after_exit` is fed by
    a `.pop()` that records exactly one post-exit price per trade today, so a
    real fix needs a bounded window, not just a reordering -- sized enough to
    make its own follow-up.
  - **`holding-horizon-profiler` and `exit-timing-learner` stay dark after
    fix #3.** `holding-horizon-profiler` parses `rule_name.rsplit(":", 1)[-1]`
    as a horizon in seconds, but every rule name this replayer emits is
    `"...-plan:stop"` / `"...-plan:target-N"` / `"...-plan:trail"` -- never
    numeric -- so every counterfactual is discarded regardless of the tape
    fix. `exit-timing-learner` checks `not counterfactual.is_hindsight`, and
    `is_hindsight=True` is hardcoded at every construction site, so that
    branch can never fire. Neither is caused by, or fixed by, anything in
    this proposal; naming them here so "why fix the tape key" isn't
    overstated as unstarving three consumers when it unstarves one.

## Verification

The existing test suite does not prove any of this: every test found during
triage calls the worker method directly with the *correct* signature (e.g.
`observe_entry_quality("momentum-burst-detector", extension_at_entry=0.5,
given_away=0.001)`), never through `start_part`'s reader wiring, which is
where all five defects live. A green suite proves the class is right and says
nothing about the wire. After each fix lands: restart the spine and read the
journal for the specific part (`journalctl --user --since "-10min" | grep -B
30 Error`), not only run pytest. Then `dashboard/check_contracts.py` (R-01),
and a manual read confirming `check_payload_reads.py`/`check_part_calls.py`
still pass -- both have known blind spots for this exact defect class (payload
-reads unions every dataclass across an entire imported module rather than
the one actually produced; part-calls only follows `self.`-bound calls,
missing the `model.method(...)`/`timer.method(...)` shape several of these
bugs used). Closing those two blind spots is the separately-requested,
already-deferred "checkers after" work.

## Blueprint edits required (RL-067)

Via `dashboard/blueprint_edits/`, idempotent, before code:

  - `bull-entry-timer` / `bear-entry-timer`: drop `entry-quality`, add
    `trade-episode`.
  - `tail-trailing-exit-planner`: add `position`.
  - No edge change for `exit-counterfactual-replayer`, the exit-plan-
    proposers, or `lesson-extractor` -- all three keep their current wires,
    fixed rather than rewired.
  - Confirmed no orphaned producer: `entry-quality` keeps `trade-episode-
    encoder`; `exit-counterfactual` keeps `holding-horizon-profiler` and
    `exit-timing-learner`; `stop-audit` keeps `loss-cause-classifier` and
    `stop-target-placer`.

New settings required (RL-061, every one needs an entry in
`settings/runtime.example.toml` -- `test_every_launchable_part_starts`
asserts every setting a part names actually exists there):

  - Entry-timer pending-match deque: a max length and a max age.
  - No new setting for #4 (uses `worst_price`, already a parameter) or for
    the tape-key fix in #3 (uses the existing per-plan `LatestByKey`, just
    bounding it).

## Restart posture

The entry-timer's pending deque (#2) and any settings/threshold state
introduced above are process-memory only, with no checkpoint, matching the
existing precedent for this class of bot-side timing state (`sample-weight`,
`reward-multiplier` and the bull feature builder's own windows are documented
elsewhere as "still memory-only" for the same reason: losing an in-flight
timing decision on restart costs one unmatched observation, not a position or
a ledger entry). Stated here rather than left implicit.

## Why one proposal and not several

All five defects were found in one investigation and are the same root cause
-- a hand-matched local class drifting from the wire it mirrors -- except the
tape-key bug, which is independent but lives in the same file already being
touched for #3. Two genuinely separate root causes (the `CRASHED`-unreachable
monitoring gap, and `would_have_recovered`'s ordering bug) are named and
deliberately left for their own proposals rather than folded in, so this one
stays bounded to defects that are either already crashing or a confirmed,
verified landmine in the same class.
