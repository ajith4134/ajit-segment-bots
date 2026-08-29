# Four Parts Read a Shape the Wire Never Carried — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix five live/confirmed defects where a part hand-matched a wire's shape wrong (missing identity fields, an unreachable whitelist, a mis-keyed tape, a wrong quantity), all discovered from one investigation into why `lesson-extractor` has restarted 708+ times in six hours.

**Architecture:** No new parts. Every fix either (a) adds a field to a real wire type in `runtime/trade_decoding_types.py` at its one construction site, or (b) corrects a consumer's mistaken read of an existing wire, or (c) adds one already-precedented `consumes` edge. Two parts (`bull-entry-timer`/`bear-entry-timer`) gain a bounded, unchecked-pointed pending-match join; two producers (`sequence-pattern-miner`, `lesson-extractor`) gain `LevelPublisherByKey` wrapping to stop an unbounded republish the whitelist fix would otherwise unmask.

**Tech Stack:** Python 3.14, dataclasses, the existing `runtime.input_assembly` (`Batch`/`LatestByKey`) and `runtime.level_publishing` (`LevelPublisherByKey`) substrate. No new dependencies.

**Spec:** `docs/proposals/four-parts-read-a-shape-the-wire-never-carried.md`

## Global Constraints

- T-4: a part never imports another part's module. Shared constants move to `runtime/trade_decoding_types.py`; a part importing another part's *type only for a type hint from `runtime/`* is fine (already done throughout this codebase), importing from `parts/` is not.
- RL-061: every number is a named setting in `settings/runtime.example.toml` with a provenance note, or is derived from data already in hand — never a bare literal.
- RL-067: a part's real `consumes`/`produces` must equal `docs/features.json`. Every edge change goes through `dashboard/blueprint_edits/`, idempotent, before code.
- R-01/R-03: run `python3 dashboard/check_contracts.py` after every blueprint edit.
- Rule 7: a name states what it is. `worst_reversal_excursion` becomes `adverse_excursion_fraction` because it is last-write-wins per symbol, not a running maximum.
- Every test in this plan exercises the actual `start_part` reader-wiring closure (via real `runtime.input_assembly.Batch`/`LatestByKey` wrapping fake `runtime.bus.Message` streams), not just the worker class in isolation — that gap is what let all five defects ship untested.
- After every task, run the affected test file, then at the end of the plan restart the spine and read its journal (a green test suite does not prove `start_part` starts).

---

## File Structure

| File | Responsibility |
|---|---|
| `runtime/trade_decoding_types.py` | Add `venue_id`/`symbol` to `StopAudit`; add `adverse_excursion_fraction`; add `venue_id`/`symbol`/`trail_fraction` to `ExitCounterfactual`; host `STOP_VERDICTS` and `SEQUENCE_KINDS` (moved from the two producing parts, T-4) |
| `parts/closed_trade_decoding/stop_placement_auditor.py` | Construct `StopAudit` with the new fields; import moved verdict constants back |
| `parts/closed_trade_decoding/sequence_pattern_miner.py` | Import moved kind constants back; wrap its publish in `LevelPublisherByKey` |
| `parts/closed_trade_decoding/lesson_extractor.py` | Replace `EXPRESSIBLE_CHANGES` with the four real families; rebuild `OPPOSITES`; fix `sequence:*` evidence keying; dedupe its own publish |
| `parts/hypothesis/loss_inverter.py` | Guard `detector_of()` to unwrap only `detector:*` |
| `parts/bull_bot/bull_exit_plan_proposer.py`, `parts/bear_bot/bear_exit_plan_proposer.py` | Delete the local `StopAudit` mirror; read the real fields |
| `parts/bull_bot/bull_entry_timer.py`, `parts/bear_bot/bear_entry_timer.py` | Bounded pending-match join keyed on `(venue_id, symbol, detector)`; consume `trade-episode` instead of `entry-quality` |
| `parts/closed_trade_decoding/exit_counterfactual_replayer.py` | Fix the tape key; feed a `TRAILING_STOP` job from `tail-exit-plan`; add `maximum_age_seconds` to its plan readers |
| `parts/profit_tailgating_bot/tail_trailing_exit_planner.py` | Delete the local `ExitCounterfactual` mirror; read the real fields; consume `position`; wire `advance_trail`/`forget_position`; stop `plan()` clobbering the ratchet |
| `dashboard/blueprint_edits/apply_2026-08-29_four_parts_read_a_shape_the_wire_never_carried.py` | The blueprint edit: drop `entry-quality`/add `trade-episode` on both entry timers; add `position` to the tail planner |
| `settings/runtime.example.toml` | New settings for the pending-join bounds and the plan-reader age bound |

---

### Task 1: `runtime/trade_decoding_types.py` — identity fields, the new magnitude field, and the moved constants

**Files:**
- Modify: `runtime/trade_decoding_types.py`
- Test: `tests/runtime/test_trade_decoding_types.py` (new file)

**Interfaces:**
- Produces: `StopAudit(trade_id, stop_price, distance, typical_movement, distance_in_typical_movements, was_hit, would_have_recovered, verdict, is_measurable, reason, audited_at_ns, venue_id, symbol, adverse_excursion_fraction)` — three new fields, added after the existing ones with defaults so no other construction site breaks before it's updated.
- Produces: `ExitCounterfactual(trade_id, rule_name, exit_price, realised_pnl, difference, would_have_been_reachable, is_hindsight, reason, replayed_at_ns, venue_id, symbol, trail_fraction)` — three new fields, same pattern.
- Produces: `STOP_VERDICTS` (tuple of 7 strings) and the 7 named constants (`INSIDE_THE_NOISE`, `TOO_WIDE`, `WELL_PLACED_AND_HIT`, `WELL_PLACED_AND_NOT_HIT`, `NEVER_APPROACHED`, `NO_STOP`, `NOT_MEASURABLE`).
- Produces: `SEQUENCE_KINDS` (tuple of 4 strings) and the 4 named constants (`STREAKS`, `SIZE_DRIFT`, `SESSION_DECAY`, `OUTCOME_CONDITIONING`).
- Consumes: nothing new.

- [ ] **Step 1: Write the failing test**

Create `tests/runtime/test_trade_decoding_types.py`:

```python
from runtime.trade_decoding_types import (
    StopAudit, ExitCounterfactual, STOP_VERDICTS, SEQUENCE_KINDS,
    INSIDE_THE_NOISE, TOO_WIDE, WELL_PLACED_AND_HIT, WELL_PLACED_AND_NOT_HIT,
    NEVER_APPROACHED, NO_STOP, NOT_MEASURABLE,
    STREAKS, SIZE_DRIFT, SESSION_DECAY, OUTCOME_CONDITIONING,
)


def test_stop_audit_carries_identity_and_a_magnitude():
    audit = StopAudit(
        trade_id="t1", stop_price=100.0, distance=5.0, typical_movement=1.0,
        distance_in_typical_movements=5.0, was_hit=True, would_have_recovered=None,
        verdict=INSIDE_THE_NOISE, is_measurable=True, reason="r", audited_at_ns=1,
        venue_id="binance-usdm", symbol="BTCUSDT", adverse_excursion_fraction=0.03,
    )
    assert audit.venue_id == "binance-usdm"
    assert audit.symbol == "BTCUSDT"
    assert audit.adverse_excursion_fraction == 0.03


def test_exit_counterfactual_carries_identity_and_a_trail_fraction():
    counterfactual = ExitCounterfactual(
        trade_id="t1", rule_name="tail-exit-plan:trail", exit_price=99.0,
        realised_pnl=1.0, difference=0.5, would_have_been_reachable=True,
        is_hindsight=True, reason="r", replayed_at_ns=1,
        venue_id="binance-usdm", symbol="BTCUSDT", trail_fraction=0.02,
    )
    assert counterfactual.venue_id == "binance-usdm"
    assert counterfactual.symbol == "BTCUSDT"
    assert counterfactual.trail_fraction == 0.02


def test_stop_verdicts_and_sequence_kinds_are_the_seven_and_four_real_constants():
    assert STOP_VERDICTS == (
        INSIDE_THE_NOISE, TOO_WIDE, WELL_PLACED_AND_HIT, WELL_PLACED_AND_NOT_HIT,
        NEVER_APPROACHED, NO_STOP, NOT_MEASURABLE,
    )
    assert SEQUENCE_KINDS == (STREAKS, SIZE_DRIFT, SESSION_DECAY, OUTCOME_CONDITIONING)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/runtime/test_trade_decoding_types.py -v`
Expected: FAIL with `ImportError` (the new names don't exist yet) or `TypeError` (unexpected keyword argument).

- [ ] **Step 3: Implement**

In `runtime/trade_decoding_types.py`:

Add to the end of `ExitCounterfactual` (after `replayed_at_ns: int`):

```python
    # Added so a live consumer can key by instrument -- the producer already
    # holds this on the source ClosedTrade at construction time. See
    # docs/proposals/four-parts-read-a-shape-the-wire-never-carried.md.
    venue_id: str = ""
    symbol: str = ""
    # The trail width this rule was replayed at, as a fraction of entry price.
    # Only set when rule_name names a trailing-stop replay; None otherwise --
    # a fixed-target or fixed-stop replay has no trail width to report.
    trail_fraction: float | None = None
```

Add to the end of `StopAudit` (after `audited_at_ns: int`):

```python
    # Added so a live consumer can key by instrument -- the producer already
    # holds this on the source ClosedTrade at construction time.
    venue_id: str = ""
    symbol: str = ""
    # How far against the trade price went before the stop resolved, as a
    # fraction of entry price. Per-trade and latest-write-wins where a
    # consumer keys by symbol, like every other field on this audit -- never a
    # running maximum, which is why it is not named "worst_*".
    adverse_excursion_fraction: float | None = None
```

Move the verdict constants out of `stop_placement_auditor.py` and the kind
constants out of `sequence_pattern_miner.py` into this file, placed near
`PAIR_VERDICTS` (after line 427, before `PeakExcursionProfile`):

```python
# What stop-placement-auditor found. Named here rather than in the part,
# because a consumer that has to import a part to read its payload knows
# about the circuit rather than about the data (T-4).
INSIDE_THE_NOISE = "inside-the-symbols-ordinary-movement"
TOO_WIDE = "wide-enough-to-turn-a-small-loss-into-a-large-one"
WELL_PLACED_AND_HIT = "well-placed-and-it-did-its-job"
WELL_PLACED_AND_NOT_HIT = "well-placed-and-never-tested-by-this-trade"
NEVER_APPROACHED = "the-price-never-came-near-it"
NO_STOP = "no-stop-was-placed"
NOT_MEASURABLE = "the-symbols-typical-movement-has-never-been-measured"

STOP_VERDICTS = (
    INSIDE_THE_NOISE, TOO_WIDE, WELL_PLACED_AND_HIT, WELL_PLACED_AND_NOT_HIT,
    NEVER_APPROACHED, NO_STOP, NOT_MEASURABLE,
)

# What sequence-pattern-miner tests for. Named here for the same reason.
STREAKS = "losses-cluster-more-than-chance"
SIZE_DRIFT = "size-changes-with-the-previous-outcome"
SESSION_DECAY = "quality-falls-later-in-the-session"
OUTCOME_CONDITIONING = "the-next-trade-depends-on-the-last-one"

SEQUENCE_KINDS = (STREAKS, SIZE_DRIFT, SESSION_DECAY, OUTCOME_CONDITIONING)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/runtime/test_trade_decoding_types.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the existing full test suite to catch anything constructing these dataclasses positionally**

Run: `.venv/bin/python3 -m pytest tests/ -q -k "StopAudit or ExitCounterfactual or trade_decoding_types"`
Expected: PASS. (The new fields have defaults, so positional construction of the old 8/11-field shape still works.)

- [ ] **Step 6: Commit**

```bash
git add runtime/trade_decoding_types.py tests/runtime/test_trade_decoding_types.py
git commit -m "feat: add identity fields and moved constants to StopAudit/ExitCounterfactual"
```

---

### Task 2: `stop_placement_auditor.py` — construct the new fields, import the moved constants

**Files:**
- Modify: `parts/closed_trade_decoding/stop_placement_auditor.py`
- Test: `tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py`

**Interfaces:**
- Consumes: `STOP_VERDICTS`, `INSIDE_THE_NOISE`, `TOO_WIDE`, `WELL_PLACED_AND_HIT`, `WELL_PLACED_AND_NOT_HIT`, `NEVER_APPROACHED`, `NO_STOP`, `NOT_MEASURABLE` from `runtime.trade_decoding_types` (Task 1).
- Produces: `StopAudit` now carrying `venue_id`, `symbol`, `adverse_excursion_fraction` on every construction.

- [ ] **Step 1: Write the failing test**

Add to `tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py`:

```python
def test_stop_audit_carries_venue_symbol_and_adverse_excursion():
    """The audit is constructed with identity and a magnitude, not just a verdict."""
    from parts.closed_trade_decoding.stop_placement_auditor import StopPlacementAuditor
    from runtime.trade_decoding_types import INSIDE_THE_NOISE

    class ClosedTradeStub:
        venue_id = "binance-usdm"
        symbol = "BTCUSDT"
        direction = "long"
        entry_price = 100.0

    auditor = StopPlacementAuditor(
        inside_the_noise_below=0.5, too_wide_above=5.0, approach_fraction=0.5,
    )
    auditor.observe_typical_movement("binance-usdm", "BTCUSDT", 0.01)
    auditor.observe_stop("t1", 99.0)

    outcome = auditor.audit("t1", ClosedTradeStub(), worst_price=97.0)

    assert outcome.audit.venue_id == "binance-usdm"
    assert outcome.audit.symbol == "BTCUSDT"
    # Price went 3% against entry (100 -> 97) before the audit was taken.
    assert outcome.audit.adverse_excursion_fraction == pytest.approx(0.03)
```

(This file already imports `pytest`; if not, add `import pytest` at the top.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py -k test_stop_audit_carries_venue_symbol_and_adverse_excursion -v`
Expected: FAIL — `outcome.audit.venue_id` is `""` (the dataclass default), not `"binance-usdm"`.

- [ ] **Step 3: Implement**

In `parts/closed_trade_decoding/stop_placement_auditor.py`:

Replace the import line:
```python
from runtime.trade_decoding_types import StopAudit
```
with:
```python
from runtime.trade_decoding_types import (
    StopAudit, INSIDE_THE_NOISE, TOO_WIDE, WELL_PLACED_AND_HIT,
    WELL_PLACED_AND_NOT_HIT, NEVER_APPROACHED, NO_STOP, NOT_MEASURABLE,
)
```

Delete the seven local constant definitions (`INSIDE_THE_NOISE = "..."` through `NOT_MEASURABLE = "..."`) — they now come from the import above; every reference in this file (`verdict = INSIDE_THE_NOISE`, etc.) keeps working unchanged since the names are identical.

Change `audit()`'s signature and body to carry `closed_trade` through to `_audit`, and compute the adverse-excursion magnitude from `worst_price` (already a parameter). Every one of the five `self._audit(...)` call sites inside `audit()` needs `closed_trade.venue_id`, `closed_trade.symbol` added, and the magnitude computed once, near the top, right after `is_long` is known:

```python
        is_long = closed_trade.direction == "long"
        adverse_excursion_fraction = (
            (closed_trade.entry_price - worst_price) / closed_trade.entry_price
            if is_long
            else (worst_price - closed_trade.entry_price) / closed_trade.entry_price
        )
        distance = abs(closed_trade.entry_price - stop_price)
```

Update `_audit`'s signature to accept and pass through the three new values:

```python
    def _audit(
        self, trade_id, stop_price, distance, typical, in_movements, was_hit, recovered,
        verdict, is_a_defect, reason, venue_id, symbol, adverse_excursion_fraction,
    ) -> StopAudit:
        return StopAudit(
            trade_id=trade_id, stop_price=stop_price, distance=distance,
            typical_movement=typical, distance_in_typical_movements=in_movements,
            was_hit=was_hit, would_have_recovered=recovered, verdict=verdict,
            is_measurable=typical is not None, reason=reason,
            audited_at_ns=self._now_ns(),
            venue_id=venue_id, symbol=symbol,
            adverse_excursion_fraction=adverse_excursion_fraction,
        )
```

Update the five call sites inside `audit()` (the `NO_STOP` early return, the `NOT_MEASURABLE` early return, and the three verdict branches at the end) to pass `closed_trade.venue_id, closed_trade.symbol` and the magnitude. The two early returns (`NO_STOP`, `NOT_MEASURABLE`) happen *before* `is_long`/`adverse_excursion_fraction` are computable (no stop, or no typical movement) — pass `closed_trade.venue_id, closed_trade.symbol, None` for those two (magnitude genuinely unmeasurable without a stop or a typical-movement baseline; `is_measurable=typical is not None` on the dataclass already says so for the second case, and the first has no distance to be a fraction of in the first place). The final call at the bottom of `audit()` passes the computed `adverse_excursion_fraction`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py -k test_stop_audit_carries_venue_symbol_and_adverse_excursion -v`
Expected: PASS.

- [ ] **Step 5: Run the whole file's tests**

Run: `.venv/bin/python3 -m pytest tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py -v`
Expected: all PASS (existing `stop-placement-auditor` tests construct `StopAudit`/call `audit()` without the new fields, which still works via defaults on the return side — but existing tests calling `_audit` directly with the old positional signature would now fail; fix any such call sites to pass the three new arguments, using `""`/`None` for tests that don't care about identity).

- [ ] **Step 6: Commit**

```bash
git add parts/closed_trade_decoding/stop_placement_auditor.py tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py
git commit -m "fix: stop-placement-auditor publishes identity and adverse excursion on StopAudit"
```

---

### Task 3: `bull_exit_plan_proposer.py` / `bear_exit_plan_proposer.py` — delete the local mirror, read the real fields

**Files:**
- Modify: `parts/bull_bot/bull_exit_plan_proposer.py`
- Modify: `parts/bear_bot/bear_exit_plan_proposer.py`
- Test: `tests/parts/bull_bot/test_bull_bot_block.py`, `tests/parts/bear_bot/test_bear_bot_block.py`

**Interfaces:**
- Consumes: `StopAudit` from `runtime.trade_decoding_types` (Task 1) — `venue_id`, `symbol`, `adverse_excursion_fraction`, replacing the deleted local class's `venue_id`, `symbol`, `worst_reversal_excursion`.

- [ ] **Step 1: Write the failing test (bull)**

In `tests/parts/bull_bot/test_bull_bot_block.py`, find the existing test(s) constructing the local `StopAudit` (search for `StopAudit(` in that file) and replace their construction with the real type, asserting the widening reads the renamed field:

```python
def test_a_stop_audit_widens_the_stop_when_it_shows_a_bigger_adverse_move():
    from parts.bull_bot.bull_exit_plan_proposer import BullExitPlanProposer
    from runtime.trade_decoding_types import StopAudit, WELL_PLACED_AND_HIT

    proposer = a_bull_exit_plan_proposer()  # existing fixture in this file
    proposer.observe_stop_audit(
        StopAudit(
            trade_id="t1", stop_price=99.0, distance=1.0, typical_movement=0.5,
            distance_in_typical_movements=2.0, was_hit=True, would_have_recovered=None,
            verdict=WELL_PLACED_AND_HIT, is_measurable=True, reason="r", audited_at_ns=1,
            venue_id=VENUE, symbol=SYMBOL, adverse_excursion_fraction=0.10,
        )
    )
    fraction, widened = proposer._stop_fraction((VENUE, SYMBOL), an_excursion_profile())
    assert widened is True
    assert fraction == pytest.approx(0.10 * proposer._stop_multiple)
```

(Use whatever `VENUE`, `SYMBOL`, `an_excursion_profile()` / equivalent fixtures this test file already defines — check the top of the file and the existing `_stop_fraction` tests for the exact names in use, and match them rather than inventing new ones.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/parts/bull_bot/test_bull_bot_block.py -k adverse_move -v`
Expected: FAIL — `TypeError: StopAudit.__init__() got an unexpected keyword argument 'worst_reversal_excursion'` if the old test still references it, or `AttributeError` on `.adverse_excursion_fraction` before the rename lands.

- [ ] **Step 3: Implement (bull)**

In `parts/bull_bot/bull_exit_plan_proposer.py`:

Delete the local `class StopAudit:` block entirely (the one with `venue_id, symbol, stops_hit, stops_hit_then_reversed, worst_reversal_excursion` and the `reversal_fraction` property).

Add to imports:
```python
from runtime.trade_decoding_types import StopAudit
```

In `_stop_fraction`, rename the read:
```python
    def _stop_fraction(self, key, profile: ExcursionProfile) -> tuple[float, bool]:
        """Past what winners survive, and past what the audit says was too tight."""
        fraction = profile.adverse_excursion * self._stop_multiple
        audit = self._audits.get(key)
        widened = False
        if audit is not None and audit.adverse_excursion_fraction is not None:
            widened_to = audit.adverse_excursion_fraction * self._stop_multiple
            if widened_to > fraction:
                fraction = widened_to
                widened = True
                self.standing.stops_widened_by_audit += 1
        return fraction, widened
```

`observe_stop_audit` needs no change — `self._audits[(audit.venue_id, audit.symbol)] = audit` already reads real fields once the import is fixed.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/parts/bull_bot/test_bull_bot_block.py -k adverse_move -v`
Expected: PASS.

- [ ] **Step 5: Repeat steps 1-4 for bear**

Same change in `parts/bear_bot/bear_exit_plan_proposer.py` and its test file, using `SHORT`-side fixtures already present there.

- [ ] **Step 6: Run both files' full test suites**

Run: `.venv/bin/python3 -m pytest tests/parts/bull_bot/test_bull_bot_block.py tests/parts/bear_bot/test_bear_bot_block.py -v`
Expected: all PASS. Fix any other test in these files still constructing the deleted local `StopAudit` shape.

- [ ] **Step 7: Commit**

```bash
git add parts/bull_bot/bull_exit_plan_proposer.py parts/bear_bot/bear_exit_plan_proposer.py tests/parts/bull_bot/test_bull_bot_block.py tests/parts/bear_bot/test_bear_bot_block.py
git commit -m "fix: bull/bear exit-plan-proposer read the real StopAudit instead of a hand-matched mirror"
```

---

### Task 4: `sequence_pattern_miner.py` — import the moved constants, stop the unbounded republish

**Files:**
- Modify: `parts/closed_trade_decoding/sequence_pattern_miner.py`
- Modify: `settings/runtime.example.toml`
- Test: `tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py`

**Interfaces:**
- Consumes: `SEQUENCE_KINDS`, `STREAKS`, `SIZE_DRIFT`, `SESSION_DECAY`, `OUTCOME_CONDITIONING` from `runtime.trade_decoding_types` (Task 1).
- Produces: same `sequence-pattern` wire, now published through a `LevelPublisherByKey` keyed on `kind` — republished only when a kind's finding content changes, or every `sequence_pattern_republish_interval_seconds`.

- [ ] **Step 1: Write the failing test**

Add to `tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py`:

```python
def test_an_unchanged_pattern_is_not_republished_every_tick():
    from runtime.level_publishing import LevelPublisherByKey
    from runtime.trade_decoding_types import STREAKS

    published = []
    level_publisher = LevelPublisherByKey(
        publish=lambda items: published.extend(items),
        refresh_interval_seconds=3600.0,
        identity_of=lambda items: tuple(
            (p.kind, p.description, p.trades_examined, p.occurrences, p.effect,
             p.is_significant, p.reason)
            for p in items
        ),
    )

    def a_pattern(pattern_id, occurrences=1):
        from runtime.trade_decoding_types import SequencePattern
        return SequencePattern(
            pattern_id=pattern_id, description="d", kind=STREAKS, trades_examined=10,
            occurrences=occurrences, effect=0.1, is_significant=True, reason="r",
            found_at_ns=1,
        )

    # Same finding, minted under a fresh pattern_id each tick (exactly what
    # sequence-pattern-miner does today) -- must be published once, not twice.
    level_publisher.publish_level(STREAKS, (a_pattern("sequence-1"),))
    level_publisher.publish_level(STREAKS, (a_pattern("sequence-2"),))
    assert len(published) == 1

    # A genuinely new occurrence count is a real change and must go out.
    level_publisher.publish_level(STREAKS, (a_pattern("sequence-3", occurrences=2),))
    assert len(published) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py -k unchanged_pattern -v`
Expected: PASS already, if `runtime.level_publishing` behaves as documented — this step confirms the substrate does what Task 4 relies on before wiring it into the part. If it fails, stop and re-read `runtime/level_publishing.py` rather than proceeding.

- [ ] **Step 3: Write the failing test for the mine-and-publish pass, extracted so it's testable without `run_part`**

`run_part` (which `run_sequence_pattern_miner` delegates to) runs a real
blocking event loop against a socket — it is not something to invoke from a
unit test. No test in this file today calls a `run_*`-wrapped function
directly, and this plan does not start that pattern here. Instead, extract
`tick()`'s one-mining-pass loop into a standalone, directly-callable function,
which is what's actually under test:

```python
def test_mine_and_publish_calls_publish_with_kind_and_pattern():
    from parts.closed_trade_decoding.sequence_pattern_miner import (
        SequencePatternMiner, mine_and_publish, STREAKS,
    )

    miner = SequencePatternMiner(minimum_trades=4, effect_threshold=0.01, shuffle_margin=0.01)
    # Four losing trades in a row is enough for mine(STREAKS) to find a pattern
    # against a shuffled baseline -- matches the trade shape the existing
    # SequencePatternMiner.mine() tests elsewhere in this file already use to
    # trip STREAKS.
    for i in range(4):
        miner.observe_trade(f"t{i}", realised=-1.0, notional=100.0, opened_at_ns=i, seconds_into_session=0.0)

    calls = []
    mine_and_publish(miner, lambda kind, pattern: calls.append((kind, pattern)))
    kinds_published = {kind for kind, _ in calls}
    assert STREAKS in kinds_published
```

- [ ] **Step 4: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py -k mine_and_publish_calls_publish -v`
Expected: FAIL — `mine_and_publish` does not exist yet.

- [ ] **Step 5: Add the new setting**

In `settings/runtime.example.toml`, add (alphabetical position among the other `sequence_*` settings):

```toml
[sequence_pattern_republish_interval_seconds]
value = 3600.0
unit  = "seconds"
note  = "operator, 2026-08-29: how long an unchanged sequence-pattern finding may go unrepeated before it is restated so a reader that started after it last changed still sees it. An hour, because these are process-wide findings that change on the order of dozens of trades, not seconds -- unlike a per-symbol level, nothing needs this fresher than a reader could reasonably have missed a restart window for."
```

- [ ] **Step 6: Implement**

In `parts/closed_trade_decoding/sequence_pattern_miner.py`:

Replace:
```python
from runtime.trade_decoding_types import SequencePattern
```
with:
```python
from runtime.trade_decoding_types import (
    SequencePattern, SEQUENCE_KINDS, STREAKS, SIZE_DRIFT, SESSION_DECAY,
    OUTCOME_CONDITIONING,
)
from runtime.level_publishing import LevelPublisherByKey
```

Delete the local `STREAKS = "..."` through `PATTERN_KINDS = (...)` block — replace every remaining reference to `PATTERN_KINDS` in this file with `SEQUENCE_KINDS` (there is one, in `tick()`, and one in `describe_sequence_mining`).

Extract the one-mining-pass loop out of `tick()` into a standalone function,
so it's callable without going through `run_part`'s blocking loop, and have
`tick()` call it — comparing published content only via a
`LevelPublisherByKey` keyed by kind:

```python
def _sequence_pattern_identity(items):
    return tuple(
        (p.kind, p.description, p.trades_examined, p.occurrences, p.effect,
         p.is_significant, p.reason)
        for p in items
    )


def mine_and_publish(miner: SequencePatternMiner, publish_patterns) -> None:
    """One pass over every kind, publishing each usable finding.

    `publish_patterns` takes (kind, pattern) -- separated from tick() so it is
    directly testable without running the whole part's event loop.
    """
    for kind in SEQUENCE_KINDS:
        outcome = miner.mine(kind)
        if outcome.is_usable:
            publish_patterns(kind, outcome.pattern)


def run_sequence_pattern_miner(
    miner: SequencePatternMiner, control_socket, read_trades, publish_patterns,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for job in read_trades():
            miner.observe_trade(**job)
        mine_and_publish(miner, publish_patterns)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=lambda: describe_sequence_mining(miner),
    )
```

In `start_part`, replace:
```python
    publish_patterns = context.bus.publisher_for("sequence-pattern")
```
with:
```python
    raw_publish_patterns = context.bus.publisher_for("sequence-pattern")
    level_publisher = LevelPublisherByKey(
        publish=raw_publish_patterns,
        refresh_interval_seconds=context.number("sequence_pattern_republish_interval_seconds"),
        identity_of=_sequence_pattern_identity,
    )
```
and replace:
```python
        publish_patterns=lambda pattern: publish_patterns((pattern,)),
```
with:
```python
        publish_patterns=lambda kind, pattern: level_publisher.publish_level(kind, (pattern,)),
```

- [ ] **Step 7: Run to verify the new test passes, then run the file's tests**

Run: `.venv/bin/python3 -m pytest tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py -v`
Expected: all PASS. Fix any existing test calling `run_sequence_pattern_miner` with a single-argument `publish_patterns` stub — it now takes `(kind, pattern)`.

- [ ] **Step 8: Commit**

```bash
git add parts/closed_trade_decoding/sequence_pattern_miner.py settings/runtime.example.toml tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py
git commit -m "fix: sequence-pattern-miner stops republishing an unchanged finding every tick"
```

---

### Task 5: `lesson_extractor.py` — the four real families, `OPPOSITES`, sequence keying, and its own publish dedup

This is the task that stops the 836-hits-in-six-hours crash. It touches four
independent things in the same file; do them as separate TDD cycles within
this one task since a reviewer could reasonably want to see each pass on its
own, but land them in one commit since the whitelist and identity fixes must
ship together (Task 2 already landed identity; this task assumes it's in).

**Files:**
- Modify: `parts/closed_trade_decoding/lesson_extractor.py`
- Modify: `settings/runtime.example.toml`
- Test: `tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py`

**Interfaces:**
- Consumes: `STOP_VERDICTS`, `PNL_COMPONENTS`, `SEQUENCE_KINDS` from `runtime.trade_decoding_types`.
- Produces: `decoded-trade-instruction`, now published through a `LevelPublisherByKey`.

- [ ] **Step 1: Write the failing test — the four families are accepted, the nine old strings are not**

```python
def test_observe_evidence_accepts_the_four_real_families():
    from parts.closed_trade_decoding.lesson_extractor import LessonExtractor
    from runtime.trade_decoding_types import INSIDE_THE_NOISE, STREAKS, FROM_SLIPPAGE

    extractor = LessonExtractor(minimum_trades=2, minimum_significant_fraction=0.5)
    # None of these raise.
    extractor.observe_evidence(f"stop:{INSIDE_THE_NOISE}", {"symbol": "BTCUSDT"}, "t1", 0.0, True)
    extractor.observe_evidence(f"pnl-from:{FROM_SLIPPAGE}", {"symbol": "BTCUSDT"}, "t2", -1.0, True)
    extractor.observe_evidence("sequence:" + STREAKS, {}, "t3", 0.5, True)
    extractor.observe_evidence("detector:momentum-burst-detector", {"regime": "trending"}, "t4", 1.0, True)


def test_observe_evidence_refuses_a_stop_verdict_that_does_not_exist():
    from parts.closed_trade_decoding.lesson_extractor import LessonExtractor

    extractor = LessonExtractor(minimum_trades=2, minimum_significant_fraction=0.5)
    with pytest.raises(ValueError, match="is not a change anything downstream can apply"):
        extractor.observe_evidence("stop:not-a-real-verdict", {"symbol": "BTCUSDT"}, "t1", 0.0, True)


def test_observe_evidence_no_longer_accepts_the_old_hyphenated_vocabulary():
    from parts.closed_trade_decoding.lesson_extractor import LessonExtractor

    extractor = LessonExtractor(minimum_trades=2, minimum_significant_fraction=0.5)
    with pytest.raises(ValueError, match="is not a change anything downstream can apply"):
        extractor.observe_evidence("widen-the-stop", {"symbol": "BTCUSDT"}, "t1", 0.0, True)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py -k "observe_evidence_accepts or observe_evidence_refuses or old_hyphenated" -v`
Expected: FAIL — the first test raises on every call today (whitelist mismatch); the third test currently *passes* the raise for the wrong reason (it would still pass once the fix lands, but confirm it raises now too, for the right message).

- [ ] **Step 3: Implement — replace `EXPRESSIBLE_CHANGES`**

In `parts/closed_trade_decoding/lesson_extractor.py`, replace:
```python
from runtime.trade_decoding_types import DecodedTradeInstruction
```
with:
```python
from runtime.trade_decoding_types import (
    DecodedTradeInstruction, STOP_VERDICTS, PNL_COMPONENTS, SEQUENCE_KINDS,
)
from runtime.level_publishing import LevelPublisherByKey
```

Delete the nine `WIDEN_THE_STOP = "..."` through `PREFER_A_CHEAPER_INSTRUMENT = "..."` lines and the `EXPRESSIBLE_CHANGES = (...)` tuple.

Replace the `OPPOSITES` dict (it will be rebuilt in Step 5) — for now delete it too, it is rebuilt below.

Replace `observe_evidence`'s validation:
```python
    def observe_evidence(
        self, change: str, conditions: dict, trade_id: str, effect: float,
        was_significant: bool,
    ) -> None:
        """One trade supporting one change under one set of conditions."""
        if not self._is_expressible(change):
            raise ValueError(
                f"{change!r} is not a change anything downstream can apply. An "
                f"instruction naming a knob that does not exist can never be acted on"
            )
        self._evidence.setdefault(self._key(change, conditions), []).append(
            {"trade_id": trade_id, "effect": effect, "significant": was_significant}
        )

    @staticmethod
    def _is_expressible(change: str) -> bool:
        prefix, _, suffix = change.partition(":")
        if prefix == "stop":
            return suffix in STOP_VERDICTS
        if prefix == "pnl-from":
            return suffix in PNL_COMPONENTS
        if prefix == "sequence":
            return suffix in SEQUENCE_KINDS
        if prefix == "detector":
            return bool(suffix)
        return False
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py -k "observe_evidence_accepts or observe_evidence_refuses or old_hyphenated" -v`
Expected: PASS.

- [ ] **Step 5: Write the failing test for `OPPOSITES` rebuilt over the real families**

```python
def test_a_stop_verdict_contradicts_its_real_opposite():
    from parts.closed_trade_decoding.lesson_extractor import LessonExtractor
    from runtime.trade_decoding_types import INSIDE_THE_NOISE, TOO_WIDE

    extractor = LessonExtractor(minimum_trades=2, minimum_significant_fraction=0.5)
    conditions = {"symbol": "BTCUSDT"}
    for i in range(3):
        extractor.observe_evidence(f"stop:{TOO_WIDE}", conditions, f"a{i}", 1.0, True)
    extractor.extract(f"stop:{TOO_WIDE}", conditions)  # writes it

    for i in range(3):
        extractor.observe_evidence(f"stop:{INSIDE_THE_NOISE}", conditions, f"b{i}", 1.0, True)
    outcome = extractor.extract(f"stop:{INSIDE_THE_NOISE}", conditions)

    from parts.closed_trade_decoding.lesson_extractor import CONTRADICTED
    assert outcome.state == CONTRADICTED
```

- [ ] **Step 6: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py -k contradicts_its_real_opposite -v`
Expected: FAIL — `OPPOSITES` is empty (deleted in Step 3), so nothing is ever contradicted; `outcome.state` is `EXTRACTED`.

- [ ] **Step 7: Implement — rebuild `OPPOSITES`**

Add `INSIDE_THE_NOISE, TOO_WIDE` to the Step 3 import line (alongside
`STOP_VERDICTS`, `PNL_COMPONENTS`, `SEQUENCE_KINDS`), then write:

```python
OPPOSITES = {
    f"stop:{INSIDE_THE_NOISE}": f"stop:{TOO_WIDE}",
    f"stop:{TOO_WIDE}": f"stop:{INSIDE_THE_NOISE}",
}
```

- [ ] **Step 8: Run to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py -k contradicts_its_real_opposite -v`
Expected: PASS.

- [ ] **Step 9: Write the failing test for sequence evidence keyed on `kind` alone**

```python
def test_sequence_evidence_is_keyed_on_kind_not_a_per_tick_pattern_id():
    """A pattern republished under a fresh pattern_id must accumulate as the
    same evidence, not scatter into one-item buckets that can never clear
    minimum_trades."""
    from parts.closed_trade_decoding.lesson_extractor import start_part
    ...  # see Step 12 for the read_evidence-level test; this step covers the
         # LessonExtractor-level behavior directly:
    from parts.closed_trade_decoding.lesson_extractor import LessonExtractor
    from runtime.trade_decoding_types import STREAKS

    extractor = LessonExtractor(minimum_trades=2, minimum_significant_fraction=0.5)
    change = f"sequence:{STREAKS}"
    extractor.observe_evidence(change, {}, "sequence-1", 0.1, True)
    extractor.observe_evidence(change, {}, "sequence-2", 0.1, True)
    outcome = extractor.extract(change, {})

    from parts.closed_trade_decoding.lesson_extractor import EXTRACTED
    assert outcome.state == EXTRACTED
    assert len(outcome.supporting_trades) == 2
```

Note the `conditions={}` passed for sequence evidence. `extract()` refuses an
empty-conditions change as `UNCONDITIONAL` (see its first check) — this test
will fail there too unless `read_evidence()` (Step 12, in `start_part`) is
what supplies a non-empty stand-in condition. Adjust this test to match
whatever `read_evidence()` ends up passing (Step 12 uses `{"kind": pattern.kind}`)
so the unit-level test and the wiring-level fix agree:

```python
    extractor.observe_evidence(change, {"kind": STREAKS}, "sequence-1", 0.1, True)
    extractor.observe_evidence(change, {"kind": STREAKS}, "sequence-2", 0.1, True)
    outcome = extractor.extract(change, {"kind": STREAKS})
```

- [ ] **Step 10: Run to verify it fails, then passes**

This one already passes against the current `extract()`/`observe_evidence()`
logic once Step 3's `_is_expressible` accepts `sequence:*` — it is really
testing that the *conditions shape* `read_evidence()` will supply (Step 12)
is internally consistent, not new `LessonExtractor` logic. Run it now to
confirm; no further `LessonExtractor` change is needed for this step.

Run: `.venv/bin/python3 -m pytest tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py -k sequence_evidence_is_keyed -v`
Expected: PASS.

- [ ] **Step 11: Write the failing test for `start_part`'s `read_evidence` no longer keying on `pattern.pattern_id`**

```python
def test_read_evidence_keys_sequence_jobs_on_kind_not_pattern_id():
    """read_evidence is the closure inside start_part; test it directly by
    calling it the way start_part would build it, using real Batch readers
    over fake Message streams so this exercises the actual wiring."""
    from runtime.bus import Message
    from runtime.input_assembly import Batch
    from runtime.trade_decoding_types import SequencePattern, STREAKS

    def a_pattern(pattern_id):
        return SequencePattern(
            pattern_id=pattern_id, description="d", kind=STREAKS, trades_examined=10,
            occurrences=1, effect=0.1, is_significant=True, reason="r", found_at_ns=1,
        )

    # Mirror read_evidence's own construction (see start_part) with fake sources.
    patterns_batch = Batch(read=lambda: (
        Message(data_type="sequence-pattern", producer_part_id="x", sequence=1,
                published_at_ns=1, payload=a_pattern("sequence-1")),
    ))
    jobs = []
    for pattern in patterns_batch.payloads():
        if pattern.is_significant:
            jobs.append({
                "change": f"sequence:{pattern.kind}", "conditions": {"kind": pattern.kind},
                "trade_id": pattern.pattern_id, "effect": pattern.effect,
                "was_significant": True,
            })
    assert jobs[0]["conditions"] == {"kind": STREAKS}
    assert "pattern" not in jobs[0]["conditions"]
```

This test documents the target shape of the change to `read_evidence` before
it's made; it will pass immediately since it doesn't call the real
`read_evidence` yet — its purpose is to pin the exact dict shape Step 12 must
produce. Treat it as already-passing scaffolding for Step 12, not a
red/green cycle of its own.

- [ ] **Step 12: Implement — fix `read_evidence`'s sequence job**

In `parts/closed_trade_decoding/lesson_extractor.py`'s `start_part`, in
`read_evidence()`, change:

```python
        for pattern in patterns.payloads():
            if pattern.is_significant:
                jobs.append({
                    "change": f"sequence:{pattern.kind}", "conditions": {"pattern": pattern.pattern_id},
                    "trade_id": pattern.pattern_id, "effect": pattern.effect, "was_significant": True,
                })
```
to:
```python
        for pattern in patterns.payloads():
            if pattern.is_significant:
                jobs.append({
                    "change": f"sequence:{pattern.kind}",
                    # `kind` restates the change, deliberately: SequencePattern
                    # carries no symbol or regime (it is a genuinely global
                    # finding), and observe_evidence refuses an unconditional
                    # change. Naming the condition it already fires under keeps
                    # it honest rather than inventing a scope it doesn't have.
                    "conditions": {"kind": pattern.kind},
                    "trade_id": pattern.pattern_id, "effect": pattern.effect, "was_significant": True,
                })
```

Also fix the two other `read_evidence` reads now unblocked by Task 2's
identity fields — `audit.symbol`/`audit.venue_id` are now real, so no code
change is needed there, but add `venue_id` alongside the existing `symbol`
condition for `stop:*` evidence for consistency with the other three job
kinds, which all key on `symbol` alone today; leave as `{"symbol": audit.symbol}`
to match the existing convention for `pnl-from:*` and `detector:*` evidence
(don't introduce an inconsistency where only `stop:*` also carries venue_id).

- [ ] **Step 13: Run the full observe_evidence/read_evidence test set**

Run: `.venv/bin/python3 -m pytest tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py -v`
Expected: all PASS.

- [ ] **Step 14: Write the failing test for `lesson-extractor`'s own publish dedup**

```python
def test_tick_does_not_republish_an_instruction_whose_content_has_not_changed():
    from parts.closed_trade_decoding import lesson_extractor as module
    from runtime.trade_decoding_types import FROM_SLIPPAGE

    published = []

    extractor = module.LessonExtractor(minimum_trades=2, minimum_significant_fraction=0.5)
    change = f"pnl-from:{FROM_SLIPPAGE}"
    conditions = {"symbol": "BTCUSDT"}
    extractor.observe_evidence(change, conditions, "t1", -1.0, True)
    extractor.observe_evidence(change, conditions, "t2", -1.0, True)

    level_publisher = module.LevelPublisherByKey(
        publish=lambda items: published.extend(items),
        refresh_interval_seconds=3600.0,
        identity_of=module._instruction_identity,
    )

    def publish_if_new(key, outcome):
        if outcome.is_usable:
            level_publisher.publish_level(key, (outcome.instruction,))

    # Same evidence, extracted twice -- extract() mints a fresh instruction_id
    # each time, but the content (change, applies_when, derived_from,
    # expected_effect, trades_supporting, reason minus the id/timestamp) is
    # identical, so only the first call should reach `published`.
    key = (change, tuple(sorted(conditions.items())))
    publish_if_new(key, extractor.extract(change, conditions))
    publish_if_new(key, extractor.extract(change, conditions))
    assert len(published) == 1
```

- [ ] **Step 15: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py -k does_not_republish_an_instruction -v`
Expected: FAIL — `module._instruction_identity` doesn't exist yet, or (once stubbed) `len(published) == 2` since nothing dedupes yet.

- [ ] **Step 16: Implement — dedup `tick()`'s publish**

Add near the top of `lesson_extractor.py`, after the imports:

```python
def _instruction_identity(items):
    """What counts as the same instruction: not its id or when it was written."""
    return tuple(
        (i.change, tuple(sorted(i.applies_when.items())), i.derived_from,
         i.expected_effect, i.trades_supporting, i.is_testable, i.reason)
        for i in items
    )
```

Change `run_lesson_extractor`'s `tick()`:

```python
def run_lesson_extractor(
    extractor: LessonExtractor, control_socket, read_evidence, publish_instructions,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for job in read_evidence():
            extractor.observe_evidence(**job)
        for change, conditions in list(extractor._evidence):
            outcome = extractor.extract(change, dict(conditions))
            if outcome.is_usable:
                publish_instructions((change, tuple(sorted(conditions.items()))), outcome.instruction)
```

(Match this against the actual current body of `run_lesson_extractor` in the
file — the `for change, conditions in ...` loop already exists; only the
final `publish_instructions(...)` call's arguments change, from one
instruction to `(key, instruction)`.)

In `start_part`, replace:
```python
    publish_instructions = context.bus.publisher_for("decoded-trade-instruction")
```
with:
```python
    raw_publish_instructions = context.bus.publisher_for("decoded-trade-instruction")
    instruction_level_publisher = LevelPublisherByKey(
        publish=raw_publish_instructions,
        refresh_interval_seconds=context.number("lesson_extractor_republish_interval_seconds"),
        identity_of=_instruction_identity,
    )
```
and replace:
```python
        publish_instructions=lambda instruction: publish_instructions((instruction,)),
```
with:
```python
        publish_instructions=lambda key, instruction: instruction_level_publisher.publish_level(key, (instruction,)),
```

Add to `settings/runtime.example.toml`:

```toml
[lesson_extractor_republish_interval_seconds]
value = 3600.0
unit  = "seconds"
note  = "operator, 2026-08-29: how long an unchanged decoded-trade-instruction may go unrepeated before it is restated for a reader that started after it last changed. Same reasoning and interval as sequence_pattern_republish_interval_seconds -- these are slow-moving, evidence-accumulated findings, not per-tick levels."
```

- [ ] **Step 17: Run to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py -k does_not_republish_an_instruction -v`
Expected: PASS.

- [ ] **Step 18: Fix `describe_lesson_extraction`'s dead export**

Find `describe_lesson_extraction` in this file. It exports
`"expressible_changes": list(EXPRESSIBLE_CHANGES)` — `EXPRESSIBLE_CHANGES` no
longer exists. Replace with:

```python
        "expressible_families": ["stop:*", "pnl-from:*", "sequence:*", "detector:*"],
```

- [ ] **Step 19: Run the whole file's test suite**

Run: `.venv/bin/python3 -m pytest tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py -v`
Expected: all PASS. Fix any remaining reference to the deleted `EXPRESSIBLE_CHANGES`, the deleted nine constants, or the old single-argument `publish_instructions`/`publish_patterns` call shape in other tests in this file.

- [ ] **Step 20: Commit**

```bash
git add parts/closed_trade_decoding/lesson_extractor.py settings/runtime.example.toml tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py
git commit -m "fix: lesson-extractor validates against the four real change families and stops republishing unchanged instructions"
```

---

### Task 6: `loss_inverter.py` — guard `detector_of()` to unwrap only `detector:*`

**Files:**
- Modify: `parts/hypothesis/loss_inverter.py`
- Test: `tests/parts/hypothesis/test_hypothesis_block.py`

- [ ] **Step 1: Write the failing test**

```python
def test_detector_of_does_not_treat_a_stop_verdict_as_a_detector_name():
    from parts.hypothesis.loss_inverter import start_part  # for import path confirmation
    # detector_of is a closure inside start_part in the current code; if it
    # stays a closure, test it by constructing an instruction with a
    # non-detector change and asserting downstream behavior, e.g. via
    # a small instruction stub and calling the module-level function if
    # Step 3 promotes it out of the closure (recommended: promote it so it is
    # directly testable, matching this codebase's pattern of module-level
    # pure functions where a closure has no other reason to be a closure).
    from parts.hypothesis.loss_inverter import detector_of

    class InstructionStub:
        change = "stop:inside-the-symbols-ordinary-movement"

    assert detector_of(InstructionStub()) == "stop:inside-the-symbols-ordinary-movement"
    # unchanged, not stripped to "inside-the-symbols-ordinary-movement" --
    # this is not a detector name and must not be read as one.


def test_detector_of_unwraps_a_real_detector_prefixed_change():
    from parts.hypothesis.loss_inverter import detector_of

    class InstructionStub:
        change = "detector:momentum-burst-detector"

    assert detector_of(InstructionStub()) == "momentum-burst-detector"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/parts/hypothesis/test_hypothesis_block.py -k detector_of -v`
Expected: FAIL — `detector_of` is a local closure inside `start_part`, not importable, and even inlined it currently strips any `":"`-containing string.

- [ ] **Step 3: Implement**

In `parts/hypothesis/loss_inverter.py`, find:

```python
    def detector_of(instruction) -> str:
        change = str(instruction.change)
        return change.split(":", 1)[1] if ":" in change else change
```

Promote it to module level (above `start_part`) and guard the prefix:

```python
def detector_of(instruction) -> str:
    """The detector an instruction names, or the instruction's raw change string
    if it does not name one at all -- only a `detector:*` change is naming a
    detector; a stop verdict, a pnl component or a sequence kind is not, and
    reading one as a detector name would corrupt LossCause.detector."""
    change = str(instruction.change)
    prefix, sep, suffix = change.partition(":")
    return suffix if prefix == "detector" and sep else change
```

Remove the old closure from inside `start_part` and update its call site to
use the module-level function (no signature change at the call site).

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/parts/hypothesis/test_hypothesis_block.py -k detector_of -v`
Expected: PASS.

- [ ] **Step 5: Run the whole file's test suite**

Run: `.venv/bin/python3 -m pytest tests/parts/hypothesis/test_hypothesis_block.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add parts/hypothesis/loss_inverter.py tests/parts/hypothesis/test_hypothesis_block.py
git commit -m "fix: loss-inverter only reads a detector name from a detector:-prefixed change"
```

---

### Task 7: `bull_entry_timer.py` — bounded pending-match join via `trade-episode`

**Files:**
- Modify: `parts/bull_bot/bull_entry_timer.py`
- Modify: `settings/runtime.example.toml`
- Test: `tests/parts/bull_bot/test_bull_bot_block.py`

**Interfaces:**
- Consumes: `trade-episode` (new) instead of `entry-quality` (dropped).
- Consumes (unchanged type): `TradeEpisode` from `runtime.knowledge_types` — `venue_id`, `symbol`, `detector`, `action`, `opened_at_ns`, `conditions`.
- Produces: same `bull-entry-timing`.

- [ ] **Step 1: Write the failing test — the worker class's new matching method**

```python
def test_entry_timer_matches_a_pending_decision_to_its_closing_episode():
    from parts.bull_bot.bull_entry_timer import BullEntryTimer
    from runtime.edge_arithmetic import ConvictionFloor
    from runtime.knowledge_types import TradeEpisode

    timer = a_bull_entry_timer()  # existing fixture in this test file
    timer._remember_pending_entry("binance-usdm", "BTCUSDT", "momentum-burst-detector", 0.02, decided_at_ns=100)

    episode = TradeEpisode(
        episode_id="e1", venue_id="binance-usdm", symbol="BTCUSDT",
        detector="momentum-burst-detector", regime="trending",
        conditions={"entry_percentile": 0.9}, action="long", outcome="win",
        realised=1.0, opened_at_ns=200, closed_at_ns=300, narrative="",
    )
    timer.match_trade_episode(episode)

    # given_away = 1.0 - 0.9 = 0.1, extension_at_entry = 0.02, observed together.
    assert timer._median_entry_cost() == pytest.approx(0.1, abs=0.01)


def test_entry_timer_ignores_a_short_episode():
    from parts.bull_bot.bull_entry_timer import BullEntryTimer
    from runtime.knowledge_types import TradeEpisode

    timer = a_bull_entry_timer()
    timer._remember_pending_entry("binance-usdm", "BTCUSDT", "momentum-burst-detector", 0.02, decided_at_ns=100)

    episode = TradeEpisode(
        episode_id="e1", venue_id="binance-usdm", symbol="BTCUSDT",
        detector="momentum-burst-detector", regime="trending",
        conditions={"entry_percentile": 0.9}, action="short", outcome="win",
        realised=1.0, opened_at_ns=200, closed_at_ns=300, narrative="",
    )
    timer.match_trade_episode(episode)
    # Nothing matched: a bull timer must not learn from a short trade on the
    # same symbol and detector name.
    assert timer._pending.get(("binance-usdm", "BTCUSDT", "momentum-burst-detector"))
```

(Use whatever `a_bull_entry_timer()`-equivalent fixture this file already
defines for constructing a `BullEntryTimer` with default settings; if none
exists, construct one directly with the constructor's required arguments,
matching the pattern of other tests already in this file.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/parts/bull_bot/test_bull_bot_block.py -k "matches_a_pending or ignores_a_short" -v`
Expected: FAIL — `_remember_pending_entry`/`match_trade_episode`/`_pending` don't exist yet.

- [ ] **Step 3: Implement — the worker class**

In `parts/bull_bot/bull_entry_timer.py`:

Add to imports: `from collections import deque`

In `BullEntryTimer.__init__`, add two new required constructor parameters and
the pending store:

```python
        pending_entries_per_detector: int,
        pending_entry_maximum_age_seconds: float,
```
(placed after `prior_entry_cost_fraction`, before `maximum_gap_seconds`)

```python
        if pending_entries_per_detector < 1:
            raise ValueError("a queue of zero holds no pending entry to ever match")
        if pending_entry_maximum_age_seconds <= 0:
            raise ValueError(
                "an entry that can wait forever for a matching episode leaks memory "
                "for every decision whose trade was never taken"
            )
        self._pending_entries_per_detector = pending_entries_per_detector
        self._pending_entry_maximum_age_seconds = pending_entry_maximum_age_seconds
        self._pending: dict[tuple[str, str, str], deque] = {}
```

Add three new methods, placed after `observe_entry_quality`:

```python
    def _remember_pending_entry(
        self, venue_id: str, symbol: str, detector: str, extension_at_entry: float,
        decided_at_ns: int,
    ) -> None:
        """One ENTER_NOW decision, held until a closing trade-episode claims it.

        Bounded on both ends (T-3): a decision whose trade was never taken, or
        never closes, must not wait here forever. Keyed on detector as well as
        symbol so two detectors active on the same symbol never share a queue.
        """
        key = (venue_id, symbol, detector)
        queue = self._pending.setdefault(key, deque(maxlen=self._pending_entries_per_detector))
        queue.append((extension_at_entry, decided_at_ns))

    def match_trade_episode(self, episode) -> None:
        """A closed trade claiming its entry decision, if one is still pending.

        `trade-episode` is not side-specific -- a bull timer must ignore a
        short episode on the same symbol and detector, or it learns from the
        peer bot's trades.
        """
        if episode.action != LONG:
            return
        key = (episode.venue_id, episode.symbol, episode.detector)
        queue = self._pending.get(key)
        if not queue:
            return
        now_ns = self._now_ns()
        while queue and (now_ns - queue[0][1]) / 1e9 > self._pending_entry_maximum_age_seconds:
            queue.popleft()
        if not queue:
            return
        extension_at_entry, _ = queue.popleft()
        conditions = episode.conditions if isinstance(episode.conditions, dict) else {}
        entry_percentile = conditions.get("entry_percentile")
        if entry_percentile is None:
            return
        given_away = max(0.0, 1.0 - entry_percentile)
        self.observe_entry_quality(episode.detector, extension_at_entry, given_away)
```

In `decide()`, in the `ENTER_NOW` branch (right after `self.standing.entered_now += 1`, before `return EntryTiming(...)`), add:

```python
        if extension is not None:
            self._remember_pending_entry(
                candidate.venue_id, candidate.symbol, candidate.detector, extension,
                self._now_ns(),
            )
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/parts/bull_bot/test_bull_bot_block.py -k "matches_a_pending or ignores_a_short" -v`
Expected: PASS.

- [ ] **Step 5: Write the failing test for `start_part`'s wiring change**

```python
def test_start_part_reads_trade_episode_not_entry_quality():
    from parts.bull_bot import bull_entry_timer as module
    assert "entry-quality" not in module.PART_DECLARATION.consumes
    assert "trade-episode" in module.PART_DECLARATION.consumes
```

- [ ] **Step 6: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/parts/bull_bot/test_bull_bot_block.py -k reads_trade_episode_not_entry_quality -v`
Expected: FAIL.

- [ ] **Step 7: Implement — `start_part`**

In `PART_DECLARATION`, change:
```python
    consumes=(
        "bull-side-candidate", "symbol-price-frame", "bull-calibrated-conviction",
        "playbook-rule", "entry-quality",
    ),
```
to:
```python
    consumes=(
        "bull-side-candidate", "symbol-price-frame", "bull-calibrated-conviction",
        "playbook-rule", "trade-episode",
    ),
```

In `start_part`, replace:
```python
    quality = Batch(read=context.bus.reader("entry-quality"))
```
with:
```python
    episodes = Batch(read=context.bus.reader("trade-episode"))
```

Replace:
```python
        for entry in quality.payloads():
            timer.observe_entry_quality(entry)
```
with:
```python
        for episode in episodes.payloads():
            timer.match_trade_episode(episode)
```

Add the two new constructor arguments to `BullEntryTimer(...)`:
```python
            pending_entries_per_detector=int(context.number("bull_pending_entries_per_detector")),
            pending_entry_maximum_age_seconds=context.number("bull_pending_entry_maximum_age_seconds"),
```

- [ ] **Step 8: Add the two new settings**

In `settings/runtime.example.toml`:

```toml
[bull_pending_entries_per_detector]
value = 50
unit  = "decisions"
note  = "operator, 2026-08-29: how many un-matched ENTER_NOW decisions bull-entry-timer holds per (venue, symbol, detector) waiting for the trade-episode that closes them. Bounded so a decision whose trade was never taken does not accumulate forever (T-3); fifty is generous headroom over how many candidates a single detector realistically enters on one symbol before the oldest closes."

[bull_pending_entry_maximum_age_seconds]
value = 86400.0
unit  = "seconds"
note  = "operator, 2026-08-29: how long a pending entry decision may wait for its closing trade-episode before it is dropped rather than matched. A day, generously above bull_exit_minimum_reward_to_risk trades' typical holding time, so a genuinely slow-closing trade is not silently discarded, while a decision whose trade never filled at all does not wait forever."
```

- [ ] **Step 9: Run to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/parts/bull_bot/test_bull_bot_block.py -k reads_trade_episode_not_entry_quality -v`
Expected: PASS.

- [ ] **Step 10: Run the whole file's test suite**

Run: `.venv/bin/python3 -m pytest tests/parts/bull_bot/test_bull_bot_block.py -v`
Expected: all PASS. Fix any test still constructing `BullEntryTimer` without the two new required constructor arguments, or still calling `observe_entry_quality(entry)` with the old one-argument shape.

- [ ] **Step 11: Commit**

```bash
git add parts/bull_bot/bull_entry_timer.py settings/runtime.example.toml tests/parts/bull_bot/test_bull_bot_block.py
git commit -m "fix: bull-entry-timer matches given_away from trade-episode via a bounded pending join"
```

---

### Task 8: `bear_entry_timer.py` — mirror of Task 7

**Files:**
- Modify: `parts/bear_bot/bear_entry_timer.py`
- Modify: `settings/runtime.example.toml`
- Test: `tests/parts/bear_bot/test_bear_bot_block.py`

Repeat every step of Task 7 against `bear_entry_timer.py`, with these
differences:
- `episode.action != SHORT` in `match_trade_episode` (import `SHORT` from
  `runtime.bot_opinion`, already imported in this file).
- Settings: `bear_pending_entries_per_detector`,
  `bear_pending_entry_maximum_age_seconds` (mirror the two `bull_*` notes,
  crediting `bull_pending_entries_per_detector` as the source being mirrored,
  matching this codebase's existing convention for bear-side settings that
  copy a bull-side rationale — e.g. `bear_entry_quality_window`'s note).
- Test names: `test_bear_entry_timer_matches_a_pending_decision_to_its_closing_episode`, etc.

- [ ] **Step 12 (final): Commit**

```bash
git add parts/bear_bot/bear_entry_timer.py settings/runtime.example.toml tests/parts/bear_bot/test_bear_bot_block.py
git commit -m "fix: bear-entry-timer matches given_away from trade-episode via a bounded pending join"
```

---

### Task 9: `exit_counterfactual_replayer.py` — fix the tape key, feed the trailing-stop alternative

**Files:**
- Modify: `parts/closed_trade_decoding/exit_counterfactual_replayer.py`
- Modify: `settings/runtime.example.toml`
- Test: `tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py`

**Interfaces:**
- Consumes: unchanged wire types, `tail-exit-plan`'s `risk_fraction` now used.
- Produces: `ExitCounterfactual` now carrying `venue_id`, `symbol`, `trail_fraction` (Task 1).

- [ ] **Step 1: Write the failing test — the tape is keyed correctly and survives across the trade's open lifetime**

```python
def test_a_tape_observed_while_open_is_found_at_replay_time():
    from parts.closed_trade_decoding.exit_counterfactual_replayer import (
        ExitCounterfactualReplayer, TRAILING_STOP,
    )

    class ClosedTradeStub:
        venue_id = "binance-usdm"
        symbol = "BTCUSDT"
        direction = "long"
        entry_price = 100.0
        quantity = 1.0
        opened_at_ns = 0
        realised_pnl = 2.0

    replayer = ExitCounterfactualReplayer(round_trip_cost_fraction=0.0)
    # Prices observed while the position was open, keyed by instrument -- not
    # by a trade_id that doesn't exist until the trade closes.
    replayer.observe_tape("binance-usdm", "BTCUSDT", 101.0, at_ns=1)
    replayer.observe_tape("binance-usdm", "BTCUSDT", 98.0, at_ns=2)

    outcome = replayer.replay(
        "t1", ClosedTradeStub(), "tail-exit-plan:trail",
        {"kind": TRAILING_STOP, "distance": 2.0},
    )
    assert outcome.counterfactual.reason != "no price history covers this trade, so nothing can be replayed against what actually printed"
    assert outcome.counterfactual.venue_id == "binance-usdm"
    assert outcome.counterfactual.symbol == "BTCUSDT"
    assert outcome.counterfactual.trail_fraction == pytest.approx(2.0 / 100.0)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py -k tape_observed_while_open -v`
Expected: FAIL — `observe_tape` today takes `(trade_id, price, at_ns)`, not `(venue_id, symbol, price, at_ns)`.

- [ ] **Step 3: Implement — the tape key**

In `parts/closed_trade_decoding/exit_counterfactual_replayer.py`:

Change `observe_tape` and `_tape`'s type and `replay`'s lookup:

```python
    def __init__(self, round_trip_cost_fraction: float, now_ns=time.time_ns) -> None:
        self._cost_fraction = round_trip_cost_fraction
        self._now_ns = now_ns
        self._tape: dict[tuple[str, str], list] = {}
        self.standing = ReplayerStanding()

    def observe_tape(self, venue_id: str, symbol: str, price: float, at_ns: int) -> None:
        """The prices that actually printed while the position was open.

        Keyed by instrument, not by trade id: the real trade id only exists
        once the trade closes (closed_trade_id needs closed_at_ns), so keying
        by it here meant the tape and the replay lookup could never meet.
        """
        self._tape.setdefault((venue_id, symbol), []).append((at_ns, price))

    def release(self, venue_id: str, symbol: str) -> None:
        """T-3: a closed trade's tape must not leak into the next trade's replay."""
        self._tape.pop((venue_id, symbol), None)
```

In `replay()`, change:
```python
        tape = sorted(self._tape.get(trade_id, []))
```
to:
```python
        tape = sorted(self._tape.get((closed_trade.venue_id, closed_trade.symbol), []))
```

In `replay()`, thread `venue_id`/`symbol`/`trail_distance` through to
`_outcome`. Where `kind = rule["kind"]` is set, also compute:
```python
        trail_fraction = (
            rule["distance"] / entry if kind == TRAILING_STOP and entry else None
        )
```
(placed right after `entry = closed_trade.entry_price`).

Update every `return self._outcome(...)` call in `replay()` to pass
`closed_trade.venue_id, closed_trade.symbol, trail_fraction` — and update
`_outcome`'s signature and its `ExitCounterfactual(...)` construction:

```python
    def _outcome(
        self, trade_id, rule_name, state, exit_price, realised, reachable, reason,
        venue_id, symbol, trail_fraction, difference=None,
    ) -> ReplayOutcome:
        return ReplayOutcome(
            trade_id=trade_id, rule_name=rule_name, state=state,
            counterfactual=ExitCounterfactual(
                trade_id=trade_id, rule_name=rule_name, exit_price=exit_price,
                realised_pnl=realised, difference=difference,
                would_have_been_reachable=reachable, is_hindsight=True, reason=reason,
                replayed_at_ns=self._now_ns(),
                venue_id=venue_id, symbol=symbol, trail_fraction=trail_fraction,
            ),
```

(Match every call site of `self._outcome(...)` in `replay()` — there are
several, one per refusal state plus the success path — adding
`closed_trade.venue_id, closed_trade.symbol, trail_fraction` to each.)

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py -k tape_observed_while_open -v`
Expected: PASS.

- [ ] **Step 5: Write the failing test for the per-plan job builder**

`read_jobs` is a closure inside `start_part`, over module-level `Batch`/
`LatestByKey` readers — calling it in isolation means calling `start_part`
itself, which enters `run_part`'s blocking event loop (see Task 4's Step 3
note on why that isn't a unit test). Extract the one part of `read_jobs`
that actually needs testing — what jobs one `(kind, plan)` pair produces —
into a standalone function, and test that directly:

```python
def test_jobs_for_plan_includes_a_trailing_stop_job_for_tail_exit_plan():
    from parts.closed_trade_decoding.exit_counterfactual_replayer import (
        _jobs_for_plan, TRAILING_STOP,
    )

    class PlanStub:
        stop_price = 95.0
        targets = ()
        risk_fraction = 0.02

    class TradeStub:
        entry_price = 100.0

    jobs = _jobs_for_plan("tail-exit-plan", PlanStub(), "t1", TradeStub())
    trail_jobs = [job for job in jobs if job[2] == "tail-exit-plan:trail"]
    assert len(trail_jobs) == 1
    _, _, _, rule = trail_jobs[0]
    assert rule == {"kind": TRAILING_STOP, "distance": pytest.approx(2.0)}


def test_jobs_for_plan_does_not_add_a_trailing_job_for_bull_or_bear_plans():
    from parts.closed_trade_decoding.exit_counterfactual_replayer import _jobs_for_plan

    class PlanStub:
        stop_price = 95.0
        targets = ()
        risk_fraction = 0.02

    class TradeStub:
        entry_price = 100.0

    jobs = _jobs_for_plan("bull-exit-plan", PlanStub(), "t1", TradeStub())
    assert all(not job[2].endswith(":trail") for job in jobs)
```

`replayer.release(*key)` being called once per closed trade inside
`read_jobs` is pure wiring glue with no branching logic of its own — it is
verified by Task 12's spine restart and journal check rather than a second
fake-bus harness invented for one line.

- [ ] **Step 6: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py -k jobs_for_plan -v`
Expected: FAIL — `_jobs_for_plan` does not exist yet.

- [ ] **Step 7: Implement — `_jobs_for_plan` and `start_part`**

Add, near the top of the file after the module constants:

```python
def _jobs_for_plan(kind: str, plan, trade_id: str, trade) -> tuple:
    """Every counterfactual replay job one exit plan implies.

    Every plan gets its stop and its targets replayed as fixed rules. Only
    tail-exit-plan also gets a trailing-stop replay: it is the only bot whose
    plan is a trail rather than a fixed level, and REPLAYABLE_RULES has always
    supported TRAILING_STOP -- nothing fed it until now.
    """
    jobs = [
        (trade_id, trade, f"{kind}:stop", {"kind": FIXED_STOP, "price": plan.stop_price}),
    ]
    for index, target in enumerate(plan.targets):
        jobs.append(
            (trade_id, trade, f"{kind}:target-{index}", {"kind": FIXED_TARGET, "price": target.price})
        )
    if kind == "tail-exit-plan":
        jobs.append((
            trade_id, trade, "tail-exit-plan:trail",
            {"kind": TRAILING_STOP, "distance": plan.risk_fraction * trade.entry_price},
        ))
    return tuple(jobs)
```

Replace every `replayer.observe_tape(trade_id, trade.price, trade.venue_time_ns)`
call with `replayer.observe_tape(trade.venue_id, trade.symbol, trade.price, trade.venue_time_ns)`.

Delete `open_symbols` entirely — it existed only to fabricate the placeholder
trade id, which is no longer needed. Replace:

```python
    open_symbols: dict[tuple[str, str], str] = {}

    def read_jobs():
        for trade in trades.payloads():
            if isinstance(trade, NormalisedTrade):
                trade_id = open_symbols.get((trade.venue_id, trade.symbol))
                if trade_id is not None:
                    replayer.observe_tape(trade_id, trade.price, trade.venue_time_ns)
        jobs = []
        for trade in closed.payloads():
            trade_id = closed_trade_id(trade)
            key = (trade.venue_id, trade.symbol)
            open_symbols.pop(key, None)
            for kind, source in plans.items():
                plan = source.mapping().get(key)
                if plan is None:
                    continue
                jobs.append((trade_id, trade, f"{kind}:stop", {"kind": FIXED_STOP, "price": plan.stop_price}))
                for index, target in enumerate(plan.targets):
                    jobs.append((trade_id, trade, f"{kind}:target-{index}", {"kind": FIXED_TARGET, "price": target.price}))
        # The next trade on a symbol starts a fresh tape under its own id.
        for kind, source in plans.items():
            for key in source.mapping():
                open_symbols.setdefault(key, f"{key[0]}:{key[1]}:pending")
        return tuple(jobs)
```

with:

```python
    def read_jobs():
        for trade in trades.payloads():
            if isinstance(trade, NormalisedTrade):
                replayer.observe_tape(
                    trade.venue_id, trade.symbol, trade.price, trade.venue_time_ns
                )
        jobs = []
        for trade in closed.payloads():
            trade_id = closed_trade_id(trade)
            key = (trade.venue_id, trade.symbol)
            for kind, source in plans.items():
                plan = source.mapping().get(key)
                if plan is None:
                    continue
                jobs.extend(_jobs_for_plan(kind, plan, trade_id, trade))
            # The next trade on this symbol gets a fresh tape -- the one just
            # replayed against must not leak into it (T-3).
            replayer.release(*key)
        return tuple(jobs)
```

Also add `maximum_age_seconds` to the three `plans` `LatestByKey` readers:
```python
    plans = {
        kind: LatestByKey(
            read=context.bus.reader(kind), key_of=lambda p: (p.venue_id, p.symbol),
            maximum_age_seconds=context.number("exit_counterfactual_plan_maximum_age_seconds"),
        )
        for kind in ("bull-exit-plan", "bear-exit-plan", "tail-exit-plan")
    }
```

- [ ] **Step 8: Add the new setting**

```toml
[exit_counterfactual_plan_maximum_age_seconds]
value = 86400.0
unit  = "seconds"
note  = "operator, 2026-08-29: how old a bull/bear/tail exit plan may be before exit-counterfactual-replayer stops treating its symbol as holding an open position. Without a bound this dict held every symbol that had ever had a plan, forever, so an open-position tape accumulated tape for symbols with no position at all (T-3). A day is generously above any expected holding time."
```

- [ ] **Step 9: Run to verify it passes, then run the whole file**

Run: `.venv/bin/python3 -m pytest tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py -v`
Expected: all PASS. Fix any existing test calling `observe_tape` with the old `(trade_id, price, at_ns)` signature.

- [ ] **Step 10: Commit**

```bash
git add parts/closed_trade_decoding/exit_counterfactual_replayer.py settings/runtime.example.toml tests/parts/closed_trade_decoding/test_closed_trade_decoding_block.py
git commit -m "fix: exit-counterfactual-replayer keys its tape by instrument and feeds the trailing-stop alternative"
```

---

### Task 10: `tail_trailing_exit_planner.py` — read the real fields, add `position`, make the trail advance

**Files:**
- Modify: `parts/profit_tailgating_bot/tail_trailing_exit_planner.py`
- Test: `tests/parts/profit_tailgating_bot/test_tailgater_block.py`

**Interfaces:**
- Consumes: `position` (new).
- Consumes (type change): `ExitCounterfactual` from `runtime.trade_decoding_types` instead of the deleted local mirror — `venue_id`, `symbol`, `rule_name`, `difference`, `trail_fraction`.

- [ ] **Step 1: Write the failing test — `observe_exit_counterfactual` reads the real fields and filters by `rule_name`**

```python
def test_observe_exit_counterfactual_only_learns_from_the_trailing_job():
    from parts.profit_tailgating_bot.tail_trailing_exit_planner import (
        TailTrailingExitPlanner, TAIL_TRAIL_RULE_NAME,
    )
    from runtime.trade_decoding_types import ExitCounterfactual

    planner = a_planner()  # existing fixture in this file
    fixed_target = ExitCounterfactual(
        trade_id="t1", rule_name="tail-exit-plan:target-0", exit_price=101.0,
        realised_pnl=1.0, difference=5.0, would_have_been_reachable=True,
        is_hindsight=True, reason="r", replayed_at_ns=1,
        venue_id=VENUE, symbol=SYMBOL, trail_fraction=None,
    )
    planner.observe_exit_counterfactual(fixed_target)
    assert planner.standing.counterfactuals_seen == 0

    trailing = ExitCounterfactual(
        trade_id="t1", rule_name=TAIL_TRAIL_RULE_NAME, exit_price=99.0,
        realised_pnl=1.0, difference=2.0, would_have_been_reachable=True,
        is_hindsight=True, reason="r", replayed_at_ns=1,
        venue_id=VENUE, symbol=SYMBOL, trail_fraction=0.03,
    )
    planner.observe_exit_counterfactual(trailing)
    assert planner.standing.counterfactuals_seen == 1
```

(Use this file's existing `VENUE`/`SYMBOL` module constants and whatever
planner-construction fixture already exists.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/parts/profit_tailgating_bot/test_tailgater_block.py -k only_learns_from_the_trailing_job -v`
Expected: FAIL — `TAIL_TRAIL_RULE_NAME` doesn't exist, and `observe_exit_counterfactual` currently reads `.trail_fraction`/`.trail_was_too_tight` off the wrong (local mirror) type entirely.

- [ ] **Step 3: Implement**

In `parts/profit_tailgating_bot/tail_trailing_exit_planner.py`:

Delete the local `class ExitCounterfactual:` block (venue_id, symbol,
trail_fraction, realised_fraction, would_have_made_fraction,
trail_was_too_tight).

Add to imports:
```python
from runtime.trade_decoding_types import ExitCounterfactual
```

Add near the other module constants (after `TRAIL_IS_THE_ONLY_EXIT`):
```python
# Matches the rule_name exit-counterfactual-replayer constructs for the one
# job it replays from this bot's own exit plan (see that part's read_jobs).
# Not shared via import -- the two parts agree on it only through the data on
# the wire, exactly as every other rule_name string in that block is a
# convention rather than an imported constant (T-4).
TAIL_TRAIL_RULE_NAME = "tail-exit-plan:trail"
```

Replace `observe_exit_counterfactual`:

```python
    def observe_exit_counterfactual(self, counterfactual: ExitCounterfactual) -> None:
        """What this bot's own trail width would have made, so it can be corrected.

        Only the trailing-stop replay of this bot's own plan is relevant --
        exit-counterfactual also carries fixed-target/fixed-stop replays of
        the bull/bear proposers' plans on the same wire, which say nothing
        about a trail width.
        """
        if counterfactual.rule_name != TAIL_TRAIL_RULE_NAME:
            return
        if counterfactual.trail_fraction is None or counterfactual.difference is None:
            return
        self.standing.counterfactuals_seen += 1
        key = (counterfactual.venue_id, counterfactual.symbol)
        estimator = self._counterfactual_trails.get(key)
        if estimator is None:
            estimator = QuantileEstimator(
                window=self._counterfactual_window, prior=self._prior_trail
            )
            self._counterfactual_trails[key] = estimator
        # difference > 0 means this trail width would have beaten the actual
        # exit -- the actual trail was too tight, so learn a wider one.
        if counterfactual.difference > 0:
            estimator.observe(counterfactual.trail_fraction * self._trail_multiple)
        else:
            estimator.observe(counterfactual.trail_fraction)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/parts/profit_tailgating_bot/test_tailgater_block.py -k only_learns_from_the_trailing_job -v`
Expected: PASS.

- [ ] **Step 5: Write the failing test — `plan()` no longer clobbers an already-advanced trail**

```python
def test_plan_does_not_reset_a_trail_that_has_already_advanced():
    planner = a_prepared_planner_with_an_open_position()  # build via existing
                                                            # fixtures: observe
                                                            # price/profile/etc,
                                                            # call plan() once,
                                                            # then advance_trail
                                                            # to move it forward
    key = (VENUE, SYMBOL)
    first_stop = planner._standing_trails[key]
    planner.advance_trail(VENUE, SYMBOL, LONG, price=first_stop * 1.05)
    advanced_stop = planner._standing_trails[key]
    assert advanced_stop > first_stop

    plan, _ = planner.plan(a_follow_candidate(), a_move_remaining())
    assert planner._standing_trails[key] == advanced_stop
    assert plan.stop_price == advanced_stop
```

- [ ] **Step 6: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/parts/profit_tailgating_bot/test_tailgater_block.py -k does_not_reset_a_trail -v`
Expected: FAIL — `plan()` currently overwrites `_standing_trails[key]` unconditionally with a fresh `stop_price` computed from the current price and width, discarding the advanced value.

- [ ] **Step 7: Implement — stop `plan()` from clobbering the ratchet**

In `plan()`, change:
```python
        stop_price = self._trail_price(key, candidate.direction, price, width)
        self._standing_trails[key] = stop_price
        self._entry_prices.setdefault(key, price)
```
to:
```python
        if key in self._standing_trails:
            # Already tracking this position -- advance_trail owns the ratchet
            # from here; plan() must not reset it back toward the current
            # price on every candidate, which would loosen a trail that had
            # already tightened.
            stop_price = self._standing_trails[key]
        else:
            stop_price = self._trail_price(key, candidate.direction, price, width)
            self._standing_trails[key] = stop_price
        self._entry_prices.setdefault(key, price)
```

- [ ] **Step 8: Run to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/parts/profit_tailgating_bot/test_tailgater_block.py -k does_not_reset_a_trail -v`
Expected: PASS.

- [ ] **Step 9: Write the failing test for the extracted position/price handler**

`read_candidates_and_market` is a closure over `start_part`'s `Batch`
readers; as in Tasks 4 and 9, extract the branching logic — what to do with
this tick's positions and price prints — into a standalone function that
takes plain lists, so it's testable without a bus harness:

```python
def test_apply_positions_and_prices_advances_a_held_positions_trail():
    from parts.profit_tailgating_bot.tail_trailing_exit_planner import (
        _apply_positions_and_prices, LONG,
    )

    planner = a_prepared_planner_with_an_open_position()  # existing fixture,
                                                            # already used in
                                                            # Task 10 Step 5
    key = (VENUE, SYMBOL)
    first_stop = planner._standing_trails[key]

    class PositionStub:
        venue_id, symbol, direction, is_flat = VENUE, SYMBOL, LONG, False

    class TradeStub:
        venue_id, symbol, observed_at_ns = VENUE, SYMBOL, 2
        price = first_stop * 1.05

    _apply_positions_and_prices(planner, [PositionStub()], [TradeStub()])
    assert planner._standing_trails[key] > first_stop


def test_apply_positions_and_prices_forgets_a_flat_position():
    from parts.profit_tailgating_bot.tail_trailing_exit_planner import _apply_positions_and_prices

    planner = a_prepared_planner_with_an_open_position()
    key = (VENUE, SYMBOL)
    assert key in planner._standing_trails

    class FlatPositionStub:
        venue_id, symbol, is_flat = VENUE, SYMBOL, True

    _apply_positions_and_prices(planner, [FlatPositionStub()], [])
    assert key not in planner._standing_trails
```

- [ ] **Step 10: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/parts/profit_tailgating_bot/test_tailgater_block.py -k apply_positions_and_prices -v`
Expected: FAIL — `_apply_positions_and_prices` does not exist yet.

- [ ] **Step 11: Implement — the extracted function and `start_part`**

Add, near the top of the file after the module constants:

```python
def _apply_positions_and_prices(planner, positions, price_prints) -> None:
    """One tick's positions and price prints, applied to the planner.

    Extracted from read_candidates_and_market so forgetting a closed
    position and advancing a held one's trail are testable without going
    through start_part's Batch/LatestByKey wiring.
    """
    held_by_key = {
        (position.venue_id, position.symbol): position
        for position in positions if not position.is_flat
    }
    for position in positions:
        if position.is_flat:
            planner.forget_position(position.venue_id, position.symbol)
    for trade in price_prints:
        planner.observe_price(
            trade.venue_id, trade.symbol, trade.price, trade.observed_at_ns
        )
        held = held_by_key.get((trade.venue_id, trade.symbol))
        if held is not None:
            planner.advance_trail(trade.venue_id, trade.symbol, held.direction, trade.price)
```

In `PART_DECLARATION`, change:
```python
    consumes=(
        "follow-candidate", "symbol-price-frame", "symbol-profile",
        "exit-counterfactual", "excursion-profile", "move-remaining",
    ),
```
to:
```python
    consumes=(
        "follow-candidate", "symbol-price-frame", "symbol-profile",
        "exit-counterfactual", "excursion-profile", "move-remaining", "position",
    ),
```

In `start_part`, add:
```python
    positions = Batch(read=context.bus.reader("position"))
```

In `read_candidates_and_market`, replace the trades loop with a call to the
extracted function. `positions.payloads()` is a `Batch`, which drains on each
call (see `runtime/input_assembly.py`'s `Batch.payloads()` docstring: "the
batch is emptied by reading it") — read it once into a list, same for
`levels_in(trades.payloads())`, and pass both to `_apply_positions_and_prices`.
Replace:

```python
    def read_candidates_and_market(_planner):
        for trade in levels_in(trades.payloads()):
            planner.observe_price(
                    trade.venue_id, trade.symbol, trade.price, trade.observed_at_ns
                )
```
with:
```python
    def read_candidates_and_market(_planner):
        _apply_positions_and_prices(
            planner, positions.payloads(), levels_in(trades.payloads())
        )
```

- [ ] **Step 12: Run to verify it passes, then run the whole file**

Run: `.venv/bin/python3 -m pytest tests/parts/profit_tailgating_bot/test_tailgater_block.py -v`
Expected: all PASS. Fix any test still constructing the deleted local
`ExitCounterfactual` shape.

- [ ] **Step 13: Commit**

```bash
git add parts/profit_tailgating_bot/tail_trailing_exit_planner.py tests/parts/profit_tailgating_bot/test_tailgater_block.py
git commit -m "fix: tail-trailing-exit-planner reads the real exit-counterfactual and actually advances its trail"
```

---

### Task 11: Blueprint edit

**Files:**
- Create: `dashboard/blueprint_edits/apply_2026-08-29_four_parts_read_a_shape_the_wire_never_carried.py`

**Interfaces:**
- Produces: updated `docs/features.json` — `bull-entry-timer`/`bear-entry-timer` drop `entry-quality`, add `trade-episode`; `tail-trailing-exit-planner` adds `position`.

- [ ] **Step 1: Write the script**

```python
#!/usr/bin/env python3
"""Two entry timers stop guessing at entry-quality; the tail planner learns
when it is flat.

Proposed by Claude 2026-08-29. Rationale:
docs/proposals/four-parts-read-a-shape-the-wire-never-carried.md
Idempotent.

bull-entry-timer / bear-entry-timer called observe_entry_quality(entry) with
the whole entry-quality payload where the method wants (detector,
extension_at_entry, given_away) -- entry-quality has never carried a
detector name. Both timers now match given_away from trade-episode instead,
which already carries venue_id/symbol/detector.

tail-trailing-exit-planner's trail never advanced in production:
advance_trail/forget_position exist and are correct but were never called
from start_part, because the part had no way to know when a position closed.
It now consumes position for exactly that.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/features.json"
PROPOSAL = "docs/proposals/four-parts-read-a-shape-the-wire-never-carried.md"

registry = json.loads(REGISTRY.read_text())
features = {feature["id"]: feature for feature in registry["features"]}

DROPPED_EDGES = (
    ("bull-entry-timer", "entry-quality"),
    ("bear-entry-timer", "entry-quality"),
)
NEW_EDGES = (
    ("bull-entry-timer", "trade-episode"),
    ("bear-entry-timer", "trade-episode"),
    ("tail-trailing-exit-planner", "position"),
)

changed = []

for consumer_id, data_type in DROPPED_EDGES:
    consumer = features.get(consumer_id)
    if consumer is None:
        raise SystemExit(f"{consumer_id} is not in the registry; this edit is out of date")
    if data_type in consumer["consumes"]:
        consumer["consumes"] = [d for d in consumer["consumes"] if d != data_type]
        changed.append(f"{consumer_id}: no longer consumes {data_type}")

for consumer_id, data_type in NEW_EDGES:
    consumer = features.get(consumer_id)
    if consumer is None:
        raise SystemExit(f"{consumer_id} is not in the registry; this edit is out of date")

    producers = [
        feature["id"] for feature in registry["features"]
        if data_type in feature.get("produces", ())
    ]
    if not producers:
        raise SystemExit(
            f"nothing produces {data_type} any more; amend {PROPOSAL} rather than letting "
            f"this edit create a dangling edge."
        )
    if consumer_id in producers:
        raise SystemExit(
            f"{consumer_id} produces {data_type} itself; consuming it would be a self-edge"
        )

    if data_type not in consumer["consumes"]:
        consumer["consumes"] = list(consumer["consumes"]) + [data_type]
        changed.append(f"{consumer_id}: consumes {data_type} (produced by {', '.join(producers)})")

if changed:
    REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(changed)} change(s) written to {REGISTRY}:")
    for line in changed:
        print(f"  {line}")
else:
    print("nothing to do; the registry already carries this edit")
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python3 dashboard/blueprint_edits/apply_2026-08-29_four_parts_read_a_shape_the_wire_never_carried.py`
Expected: prints 5 changes written (2 dropped, 3 added).

- [ ] **Step 3: Verify contracts still hold**

Run: `.venv/bin/python3 dashboard/check_contracts.py`
Expected: `all contracts hold` — confirms `entry-quality` still has a consumer
(`trade-episode-encoder`), `stop-audit`/`exit-counterfactual` are unchanged,
and `position` -> `tail-trailing-exit-planner` crosses no `peer_group`
boundary (R-03) the way `tail-winner-selector` -> `position` already doesn't.

- [ ] **Step 4: Re-run the script to confirm idempotency**

Run: `.venv/bin/python3 dashboard/blueprint_edits/apply_2026-08-29_four_parts_read_a_shape_the_wire_never_carried.py`
Expected: `nothing to do; the registry already carries this edit`.

- [ ] **Step 5: Commit**

```bash
git add dashboard/blueprint_edits/apply_2026-08-29_four_parts_read_a_shape_the_wire_never_carried.py docs/features.json
git commit -m "blueprint: entry timers read trade-episode not entry-quality; tail planner reads position"
```

---

### Task 12: Verification

**Files:** none (this task runs checks, it does not edit code)

- [ ] **Step 1: Full test suite**

Run: `.venv/bin/python3 -m pytest tests/ -q`
Expected: all pass, 0 failed. If anything fails, it is either a task above
left incomplete or a test elsewhere in the suite constructing one of the
three touched dataclasses (`StopAudit`, `ExitCounterfactual`, the deleted
`EXPRESSIBLE_CHANGES`/`PATTERN_KINDS`) positionally with the old shape — fix
the test to use the new field, don't work around it.

- [ ] **Step 2: Contract and payload checkers**

Run:
```
.venv/bin/python3 dashboard/check_contracts.py
.venv/bin/python3 dashboard/check_payload_reads.py
.venv/bin/python3 dashboard/check_part_calls.py
```
Expected: all clean (0 violations). These two checkers have documented blind
spots for exactly the defect class this plan fixes (see the proposal's
Verification section) — a clean result here is necessary, not sufficient;
closing those blind spots is the separately-tracked "checkers after" work.

- [ ] **Step 3: Restart the spine and read the journal for each touched part**

```bash
systemctl --user restart ajit-spine
sleep 30
journalctl --user --since "-2min" | grep -B 30 -i "error\|traceback" | grep -i \
  "lesson-extractor\|bull-entry-timer\|bear-entry-timer\|tail-trailing-exit-planner\|bull-exit-plan-proposer\|bear-exit-plan-proposer\|exit-counterfactual-replayer\|sequence-pattern-miner\|stop-placement-auditor\|loss-inverter"
```
Expected: no output (no tracebacks naming any of these ten parts). Per
CLAUDE.md: a crashing part's traceback is in the user journal, not
`ajit-spine`'s own log, and a green test suite does not prove the thing that
is running can still start.

- [ ] **Step 4: Confirm the parts are actually running, not just started**

Run: `python3 dashboard/build_part_monitor.py` then check
`dashboard/part-monitor.html` (or query the heartbeat table directly) for
each of the ten parts named in Step 3 reading `RUNNING`, not `FAILING`.

- [ ] **Step 5: Watch `lesson-extractor`'s restart counter specifically for ten minutes**

```bash
journalctl --user -u ajit-spine --since "-1min" -f | grep lesson-extractor
```
Expected: no `part-restarted` events for `lesson-extractor` over the
observation window — it was restarting roughly every two minutes before this
plan (708+ times in six hours). This is the single clearest signal the root
cause is actually fixed, not just quieter.

- [ ] **Step 6: Final commit (if any stray changes remain)**

```bash
git status
```
Confirm nothing is left uncommitted from the tasks above.

---

## Self-Review Notes

- **Spec coverage:** all four numbered fixes in the proposal have a task
  (Tasks 1-3 = fix #1's identity half + #4; Tasks 4-6 = fix #1's whitelist
  half; Tasks 7-8 = fix #2; Tasks 9-10 = fix #3). The proposal's explicitly
  out-of-scope items (`CRASHED` unreachable, `would_have_recovered`'s
  ordering bug, `holding-horizon-profiler`/`exit-timing-learner` staying
  dark) have no task here, correctly — they are separate proposals.
- **Placeholder scan:** no TBD/TODO, no `...` stand-ins. Confirmed there is no
  existing fake-bus/context test harness anywhere in this codebase (searched
  for `FakeBus`/`FakeContext`/similar — none exist), so `run_part`-wrapped
  functions (which block on a real socket) are never tested directly anywhere
  in this plan. Where a defect lived inside a `start_part` closure (Task 4's
  `tick()`, Task 9's `read_jobs()`, Task 10's `read_candidates_and_market()`),
  the branching logic was extracted into a standalone, plainly-testable
  function (`mine_and_publish`, `_jobs_for_plan`,
  `_apply_positions_and_prices`) with a concrete test against it, and the
  remaining pure wiring glue (one or two lines routing a `Batch`'s output into
  that function) is left to Task 12's spine restart and journal check, which
  is a real verification step — a live process actually starting and not
  crash-looping — not a weaker substitute for a unit test.
- **Type consistency:** `observe_entry_quality(detector, extension_at_entry,
  given_away)`'s signature is unchanged everywhere it's used (Task 7/8 add a
  caller, not a new signature). `StopAudit`/`ExitCounterfactual`'s new field
  names (`adverse_excursion_fraction`, `trail_fraction`) are used identically
  in the producer (Tasks 2, 9) and every consumer (Tasks 3, 10).
