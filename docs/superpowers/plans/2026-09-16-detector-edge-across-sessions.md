# Detector Edge Across Sessions — Implementation Plan

> **For agentic workers:** this project's standing rule (Rule 1 point 7, measured
> 2026-08-21) is that **Claude writes the implementation itself** and subagents only
> review. Execute with superpowers:executing-plans inline; dispatch
> `review-adversarial` on the finished measurement, not implementers per task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** For every entry-candidate detector, measure net expectancy and win rate of
the option the bots would actually buy — fitted on earlier past sessions and scored
on later ones — so the detectors with a real edge are kept and the rest are named.

**Architecture:** An offline harness drives the detectors' own classes (not
reimplementations) over one-minute history, session by session. Each candidate is
turned into the buy the selector would make, held for the detector's own horizon,
and charged Upstox's real fee stack. A walk-forward step picks the buckets
(detector × state × regime) with a positive lower bound on the earlier sessions and
reports how those buckets did on the later ones.

**Tech Stack:** CPython 3.14.4 `.venv`, stdlib + numpy (already pinned). No scipy:
implied volatility is inverted by bisection over the Black-Scholes price in
`runtime/option_delta.py`'s existing basis.

**Spec:** `docs/goal.md`, "TEMPORARY GOAL — given 2026-09-16 (fourth)". The user's
three answers there are the requirements: profit first, many past sessions, measure
every detector first.

## Global Constraints

- RL-061: no numeric literal in decision code. Every threshold is a named setting or
  read from the detector's own settings via the same names `start_part` uses.
- RL-063: tests run on real data only — the captured tape, cached Upstox history,
  NSE files. No invented fixtures.
- RL-071: the harness is a measurement, never a trading input. It writes under
  `measurements/`, never under the live state roots, and never publishes on the bus.
- Rule 8: a detector the harness cannot feed is reported `NOT MEASURED` with the
  missing input named, never left out of the table.
- Profit first: the ranking key is **net expectancy per trade after fees**; win rate
  is reported beside it, never optimised on its own.
- A result from one session is never reported as a pattern. The report states how
  many sessions each number rests on.
- Upstox history has a daily quota. Every fetch goes through
  `operate/historical_prints.historical_candles` (cached), and free sources first via
  `prints_for_instrument`.
- Commit and push at the end of every task (Rule 9).

## Detector coverage in this plan

| detector | inputs the harness supplies | in this plan |
|---|---|---|
| mean-reversion-detector | price frames; regime from `RegimeClassifier` run on the same prints | yes |
| momentum-burst-detector | price frames; regime; playbook expectation from settings | yes |
| volatility-gap-detector | forecast from `realised-vol-regressor`'s class; implied volatility inverted from premium (Task 2) | yes |
| expiry-day-zero-to-hero-detector | listing from the master; premium from prints; delta from `estimated_delta` using the Task 2 IV | yes, expiry sessions only |
| spread-reversion-detector | needs `cointegrated-pair` over time-spaced bars, whose half-life is still open (CLAUDE.md, 2026-09-13) | **NOT MEASURED**, reason printed |
| universal-symbol-sweeper | needs watch-conditions, liquidity grades, announcements | **NOT MEASURED**, reason printed |

## File Structure

| file | responsibility |
|---|---|
| `runtime/implied_volatility_from_premium.py` | Black-Scholes price and IV inversion, one basis with `option_delta.py` |
| `operate/past_session_prints.py` | For one past session: each built segment's underlyings and their 8 nearest contracts (nearest expiry unexpired *that day*), with one-minute prints |
| `operate/detectors_for_measurement.py` | Build each detector object from the operator's settings, by the same setting names its `start_part` reads |
| `operate/measure_detector_edge.py` | Drive detectors over sessions, turn candidates into buys, score net, walk forward, write the report |
| `parts/paper_live_trading/paper_fill_simulator.py` | add public `fee_for_turnover` over the existing `_upstox_fee_for` |
| `tests/runtime/test_implied_volatility_from_premium.py` | IV inversion against Upstox's own stated IV on the tape |
| `tests/operate/test_past_session_prints.py` | a real past session yields contracts unexpired that day, with prints |
| `tests/operate/test_measure_detector_edge.py` | a real session produces scored candidates; the walk-forward never scores a session it fitted on |
| `measurements/2026-09-16-detector-edge-across-sessions/` | the run's output: `report.md`, `buckets.json` |

---

### Task 1: How deep does intraday option history really go?

The plan's "many sessions" depends on this and nothing in the repo has measured it.
Upstox serves history for *currently listed* contracts; a monthly contract lists about
three months before expiry, so the depth for options may be weeks, not years.

**Files:**
- Create: `measurements/2026-09-16-how-deep-option-history-goes/measure_option_history_depth.py`

- [ ] **Step 1: Write the probe.** For NIFTY, BANKNIFTY and five of the stock
  underlyings in `segment_underlying_trading_symbols`, take every currently listed
  expiry's nearest-the-money CE and PE from `instruments_by_key()`, fetch
  `historical_candles(key, from_date=<today − 120 days>, to_date=<yesterday>)`, and
  print per contract: first session with a bar, number of sessions with ≥ 60 bars.
  Also try Upstox's expired-instrument history endpoint once for one expired NIFTY
  contract and print the HTTP status verbatim (a refusal is a fact, not an empty
  market).
- [ ] **Step 2: Run it.**
  `.venv/bin/python measurements/2026-09-16-how-deep-option-history-goes/measure_option_history_depth.py | tee measurements/2026-09-16-how-deep-option-history-goes/result.txt`
- [ ] **Step 3: Decide the session range from the result, write it into this plan's
  Task 5 as `SESSIONS_FROM`, and stop to tell the user if fewer than 20 sessions are
  available** — the walk-forward is meaningless below that.
- [ ] **Step 4: Commit** `measurements/2026-09-16-how-deep-option-history-goes/`
  with message `docs: how many past sessions of intraday option history exist`.

### Task 2: Implied volatility from a premium

**Files:**
- Create: `runtime/implied_volatility_from_premium.py`
- Test: `tests/runtime/test_implied_volatility_from_premium.py`

**Interfaces:**
- Produces: `option_price(spot, strike, option_type, volatility, seconds_to_expiry, seconds_per_year) -> float | None`
  and `implied_volatility(premium, spot, strike, option_type, seconds_to_expiry, seconds_per_year, tolerance, maximum_volatility) -> float | None`.

- [ ] **Step 1: Write the failing test** — real data: read the `.option_greeks` stream for
  every contract on the tape for 2026-09-15, pair each greeks record with the
  underlying's print at the same second, invert the contract's premium, and compare to
  the `iv` Upstox stated.

```python
import bisect, datetime, json, pathlib, statistics
import pytest
from runtime.implied_volatility_from_premium import implied_volatility
from runtime.tape import read_payload, read_tape_index
from runtime.settings_reader import load_settings_document, settings_directory

TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
DAY = "2026-09-15"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def _series(path_stem: pathlib.Path, field: str):
    index = path_stem.with_name(path_stem.name + ".index")
    blob = path_stem.with_name(path_stem.name + ".blob")
    if not index.exists():
        return [], []
    times, values = [], []
    for record in read_tape_index(index):
        payload = json.loads(read_payload(blob, record))
        if payload.get(field):
            times.append(int(record[1]))          # broker time
            values.append(float(payload[field]))
    return times, values


def _seconds_to_expiry_close(at_ns: int, expiry_text: str) -> float:
    expiry = datetime.datetime.strptime(expiry_text, "%d %b %y").replace(
        hour=15, minute=30, tzinfo=IST)
    return expiry.timestamp() - at_ns / 1e9


def test_inverted_volatility_matches_what_upstox_states():
    settings = load_settings_document(settings_directory() / "runtime.toml", "runtime")
    number = lambda name: float(settings[name]["value"])
    errors = []
    for directory in sorted(TAPE.iterdir()):
        parts = directory.name.split()
        if len(parts) < 4 or parts[2] not in ("CE", "PE"):
            continue
        spot_times, spots = _series(TAPE / parts[0] / DAY, "last_traded_price")
        greek_times, stated = _series(directory / f"{DAY}.option_greeks", "implied_volatility")
        trade_times, premiums = _series(directory / DAY, "last_traded_price")
        if not spot_times or not greek_times or not trade_times:
            continue
        for at, iv in list(zip(greek_times, stated))[:: max(1, len(stated) // 5)]:
            p = bisect.bisect_right(trade_times, at) - 1
            s = bisect.bisect_right(spot_times, at) - 1
            if p < 0 or s < 0:
                continue
            ours = implied_volatility(
                premiums[p], spots[s], float(parts[1]), parts[2],
                _seconds_to_expiry_close(at, " ".join(parts[3:])),
                number("option_delta_seconds_per_year"),
                number("implied_volatility_search_tolerance"),
                number("implied_volatility_search_ceiling"),
            )
            if ours is not None:
                errors.append(abs(ours - iv))
        if len(errors) >= 500:
            break
    if len(errors) < 50:
        pytest.skip(f"only {len(errors)} greeks records could be paired on {DAY}")
    assert statistics.median(errors) < number("implied_volatility_agreement_tolerance")
```

  `option_delta_seconds_per_year` is the calendar basis `option_delta.py` measured;
  `implied_volatility_trading_seconds_per_year` is a different basis (trading time,
  used by the volatility-gap forecast) and must not be swapped in here.
  Premium and spot are the last prints at or before the greeks record, so a stale
  premium widens the error. That is a real limit of the tape, not a bug to tune away.

- [ ] **Step 2: Add the setting** `implied_volatility_agreement_tolerance` to
  `~/.config/ajit-segment-bots/settings/runtime.toml` with a provenance note, value
  taken from the first run's measured median, not chosen in advance. Also
  `implied_volatility_search_tolerance` and `implied_volatility_search_ceiling`.
- [ ] **Step 3: Run the test, expect ImportError.**
  `.venv/bin/python -m pytest tests/runtime/test_implied_volatility_from_premium.py -v`
- [ ] **Step 4: Implement.**

```python
"""An option's implied volatility, inverted from its premium on the basis option_delta uses.

Upstox states IV only for what the live feed subscribed to, and only live. A past
session has premiums and no stated IV, so volatility-gap-detector cannot be measured
on history without this. Same basis as `runtime/option_delta.py` -- no rate, calendar
time to the session close -- so a delta and an IV computed here agree with each other.
"""

from __future__ import annotations

import math

from runtime.option_delta import CALL, PUT


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def option_price(spot, strike, option_type, volatility, seconds_to_expiry, seconds_per_year):
    if seconds_to_expiry <= 0 or volatility <= 0 or spot <= 0 or strike <= 0:
        return None
    if option_type not in (CALL, PUT):
        return None
    years = seconds_to_expiry / seconds_per_year
    spread = volatility * math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * volatility * volatility * years) / spread
    d2 = d1 - spread
    call = spot * _normal_cdf(d1) - strike * _normal_cdf(d2)
    return call if option_type == CALL else call - spot + strike


def implied_volatility(premium, spot, strike, option_type, seconds_to_expiry,
                       seconds_per_year, tolerance, maximum_volatility):
    """None when no volatility in (0, maximum] prices the premium -- below intrinsic,
    above the ceiling, or an expired contract. Never a clamped guess."""
    if premium is None or premium <= 0:
        return None
    low, high = 0.0, maximum_volatility
    price_high = option_price(spot, strike, option_type, high, seconds_to_expiry, seconds_per_year)
    intrinsic = max(0.0, spot - strike) if option_type == CALL else max(0.0, strike - spot)
    if price_high is None or premium < intrinsic or premium > price_high:
        return None
    while high - low > tolerance:
        middle = 0.5 * (low + high)
        price = option_price(spot, strike, option_type, middle, seconds_to_expiry, seconds_per_year)
        if price is None or price < premium:
            low = middle
        else:
            high = middle
    return 0.5 * (low + high)


__all__ = ["implied_volatility", "option_price"]
```

- [ ] **Step 5: Run the test, expect PASS.** If the median disagreement is large,
  that is a finding about the basis — record it, do not widen the tolerance to pass.
- [ ] **Step 6: Commit** `runtime/implied_volatility_from_premium.py` and the test,
  `feat: an option's implied volatility is inverted from its premium`.

### Task 3: One past session's prints, as the segments would have held them

**Files:**
- Create: `operate/past_session_prints.py`
- Test: `tests/operate/test_past_session_prints.py`

**Interfaces:**
- Consumes: `operate.replay_a_captured_session.instruments_by_key`,
  `contracts_nearest_the_money`, `SettingsContext`;
  `operate.historical_prints.prints_for_instrument`;
  `runtime.segment_settings.built_segments`, `read_segment_symbols`.
- Produces:

```python
@dataclass(frozen=True)
class SessionInstrument:
    segment: str
    trading_symbol: str
    underlying: str
    option_type: str | None        # "CE", "PE", or None for the underlying
    strike: float | None
    expiry_ms: int | None
    lot_size: float
    prints: tuple[tuple[int, float], ...]   # (at_ns, price), oldest first
    source: str                    # which free or paid source served it

def past_session_instruments(day: str, contracts_per_underlying: int,
                             minimum_prints: int) -> tuple[SessionInstrument, ...]
```

- [ ] **Step 1: Write the failing test** on the most recent weekday that is not today
  (`the_most_recent_weekday_before_today()`): every contract returned has
  `expiry_ms` after that day's close, every one has ≥ `minimum_prints` prints, all
  prints fall inside that day, and each underlying appears once with
  `option_type is None`.
- [ ] **Step 2: Run, expect ImportError.**
- [ ] **Step 3: Implement** by lifting the loop in
  `contracts_from_history` (`operate/replay_a_captured_session.py:405-500`) into
  `past_session_instruments`, taking `contracts_per_underlying` from the settings
  name the live feed uses for "8 contracts each" (find it with
  `grep -rn "contracts_per_underlying\|per_underlying" ~/.config/ajit-segment-bots/settings`).
  Then make `contracts_from_history` call it, so there is one path, not two.
- [ ] **Step 4: Run the new test and `tests/operate/test_the_replay_runs_the_learning_half.py`, expect PASS.**
- [ ] **Step 5: Commit**, `feat: a past session's instruments are read the way the segments hold them`.

### Task 4: Detectors built from settings in one place

Each detector's constructor arguments are written inline inside its `start_part`. A
harness that re-typed them would drift the day a setting is renamed.

**Files:**
- Modify: `parts/opportunity_scanner/mean_reversion_detector.py:257-272`,
  `momentum_burst_detector.py`, `volatility_gap_detector.py`,
  `expiry_day_zero_to_hero_detector.py`, `parts/opportunity_scanner/regime_classifier.py`,
  `parts/prediction/realised_vol_regressor.py` — each gains
  `build_from_settings(number, now_ns)` returning the object, and `start_part` calls it
  with `context.number`.
- Create: `operate/detectors_for_measurement.py` —
  `detectors_for_measurement(number, clock) -> dict[str, object]` keyed by part id.
- Modify: `parts/paper_live_trading/paper_fill_simulator.py` — add

```python
    def fee_for_turnover(self, side: str, turnover: float, segment: str) -> float:
        """Upstox's charge on one leg, for a caller that has no order object.
        The same arithmetic `_upstox_fee_for` charges a paper fill."""
```

  Read `_upstox_fee_for` (line 378) first and build the minimal order shape it reads.

- [ ] **Step 1:** refactor one detector, run
  `python3 dashboard/check_contracts.py && python3 dashboard/check_part_calls.py` and
  that detector's existing tests; repeat per file.
- [ ] **Step 2:** restart the spine (`systemctl --user restart ajit-spine`) and read
  `journalctl --user --since "-3min" | grep -B 30 Error` — a green suite does not prove
  a running part still starts (CLAUDE.md, 2026-08-26).
- [ ] **Step 3: Commit**, `refactor: each detector is built from settings in one place`.

### Task 5: Drive, score, walk forward

**Files:**
- Create: `operate/measure_detector_edge.py`
- Test: `tests/operate/test_measure_detector_edge.py`

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces:

```python
@dataclass(frozen=True)
class ScoredCandidate:
    session: str
    detector: str
    bucket: str            # f"{detector}|{state or direction}|{regime}"
    bought: str            # trading symbol actually bought
    entry_price: float
    exit_price: float
    held_seconds: float
    net_return: float      # after both legs' fees, as a fraction of premium paid

def scored_candidates_for_session(day: str) -> list[ScoredCandidate]
def walk_forward(scored: list[ScoredCandidate], train_fraction: float,
                 minimum_trades: int) -> dict
```

Rules the implementation must follow:

1. **Replay order:** merge every instrument's prints by time; for each print, feed
   the regime classifier and the detectors that read that symbol, then call `detect`
   for that symbol. The clock handed to detectors is the print's time.
2. **Implied volatility:** for each contract print, invert with Task 2 against the
   underlying's latest print, and call `observe_implied`. The forecast is fed from the
   realised-vol regressor object on the underlying's prints.
3. **What is bought:** a candidate on a contract is bought as named when long; when
   short it becomes the opposite type, nearest strike, same expiry — the rule at
   `parts/segment_bot/instrument_selector.py:1105`. A candidate on an underlying buys
   the nearest-the-money CE when long, PE when short.
4. **Exit:** the first print of the bought contract at or after entry + the
   candidate's `horizon_seconds`, or the session's last print if the horizon runs
   past the close. No entry if the bought contract has no print within
   `price_series_maximum_gap_seconds` of detection.
5. **One trade per bucket per contract per minute**, as in the 2026-09-16 conversion
   measurement, so a detector firing every tick is not counted a hundred times.
6. **Fees:** `fee_for_turnover` on both legs at one lot.
7. **Outcomes fed back:** after the exit, call the detector's `observe_outcome` so its
   calibrator learns online exactly as live — this is part of what is measured.
8. **Walk forward:** sessions sorted by date; the first `train_fraction` choose the
   buckets whose mean net return has a lower one-sided 95% bound above zero and at
   least `minimum_trades` trades; the report scores only those buckets on the later
   sessions, and separately scores all buckets there for comparison.
9. **NOT MEASURED rows** for spread-reversion and the sweeper, with the reason.

- [ ] **Step 1: Write the failing test** on one real past session from Task 1's
  range: at least one `ScoredCandidate`, every `bought` has prints that session,
  `held_seconds > 0`, and `walk_forward` never lists a session in both its train and
  test sets.
- [ ] **Step 2: Run, expect ImportError.**
- [ ] **Step 3: Implement** `operate/measure_detector_edge.py` with a `main()` taking
  `--from`, `--to`, `--train-fraction`, `--minimum-trades`, writing
  `measurements/2026-09-16-detector-edge-across-sessions/report.md` and
  `buckets.json`. Settings for the last two go into `runtime.toml` with provenance.
- [ ] **Step 4: Run the test, expect PASS.**
- [ ] **Step 5: Commit**, `feat: detectors are scored net across past sessions, walked forward`.

### Task 6: Run it across every available session and record the finding

- [ ] **Step 1:** `.venv/bin/python operate/measure_detector_edge.py --from <SESSIONS_FROM> --to <yesterday>`
  (run in the background; it spends Upstox quota only on what the free sources could
  not serve, and the cache makes a rerun free).
- [ ] **Step 2:** dispatch `review-adversarial` (opus) on the report and the harness:
  look-ahead, survivorship (contracts chosen by *today's* listing), fee arithmetic,
  and whether the kept buckets are more than multiple-comparison luck.
- [ ] **Step 3:** append the table and the reviewer's surviving objections to
  `docs/feature-audit.md` under a 2026-09-xx entry, and add a line to the fourth goal
  in `docs/goal.md` stating which detectors have an out-of-sample edge.
- [ ] **Step 4: Commit and push.**
- [ ] **Step 5: Tell the user** the table, how many sessions it rests on, and the
  next decision: switch off the detectors with no edge, and feed the kept buckets'
  evidence to the conviction models (the 2026-09-16 AUC finding).

## How this is verified (Rule 0)

- Task 2's IV agrees with Upstox's stated IV on real captured greeks.
- Task 5's test proves no session is both fitted and scored.
- The final number is out-of-sample net expectancy, per detector, with its session
  count, from a run anyone can repeat with one command.
