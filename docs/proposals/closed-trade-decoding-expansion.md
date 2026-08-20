# Closed-trade decoding opened further: 5 → 20 parts

Proposed by Claude 2026-08-20 at the user's request. Applied as `origin: proposed` by
`dashboard/blueprint_edits/apply_2026-08-20_closed_trade_decoding.py`; open to veto.

The block's job (the user's words): *decode finished trades, winners and losers both, into
instructions for the hypothesis feature; the loser is not discarded, its information is what
hypothesis works on.* Today it had five parts, all of which read the trade as one fact. These
fifteen take the trade apart.

## Take the result apart

| part | produces | why |
|---|---|---|
| `pnl-attributor` | `pnl-attribution` | direction, timing, size, fees, slippage, funding each get their share — "lost 2%" becomes "right direction, 3 bars early, stopped by noise, funding ate half". Jesse splits win-rate long vs short; this goes further. |
| `shortfall-decomposer` | `shortfall-breakdown` | implementation shortfall: delay, spread, impact, opportunity. Qlib's `price_advantage` measures one term; this measures all four. Feeds the slippage learner plus the backtest cost model so paper costs converge on real ones. |
| `luck-skill-separator` | `outcome-significance` | was this result distinguishable from noise given the symbol's volatility over the hold? Scorekeeper, critic plus reward shaper all read it, so a lucky win is not learned from with perfect discipline. |
| `entry-quality-scorer` | `entry-quality` | how far from the best price in the window after the signal. Feeds the bots' entry timers. |

## Replay what did not happen

| part | produces | why |
|---|---|---|
| `exit-counterfactual-replayer` | `exit-counterfactual` | the same trade under every other exit rail. The exit-timing learner plus the tailgater's trailing planner learn from rails that were not taken. |
| `near-miss-recorder` | `near-miss-episode` | every candidate raised but not traded, with what the market then did. RL-009's teacher learns from the students it held back. Feeds the abstention-coverage auditor (R4) plus the hypothesis mutator. |
| `exploration-pair-decoder` | `pair-verdict` | the exploration pair (bull plus bear on one symbol) decoded: which side was right, by how much, what distinguished the setup. This is the pair's whole purpose, and nothing read it before. |
| `trade-replay-verifier` | `replay-mismatch` | replay each closed trade from its own journal; if it does not reproduce the ledger's P&L, alert. tensortrade's conservation assertion, applied per trade. |

## Find structure across trades

| part | produces | why |
|---|---|---|
| `holding-horizon-profiler` | `horizon-profile` | net return against holding time per setup class — the horizon is measured, not picked (RL-043's 1m/5m/15m/30m become evidence). |
| `excursion-profiler` | `excursion-profile` | MAE/MFE distributions per setup class from every closed trade (RL-042's columns, used). Stop-target placer, profit lock plus the bots' exit-plan proposers read it. |
| `stop-placement-auditor` | `stop-audit` | stop hit by noise before the target: too tight, too wide, right. |
| `trade-cluster-detector` | `trade-cluster` | ten correlated longs opened in one minute are one bet. Scorekeeper, graduation gate, exposure limiter plus trial-count accountant all stop counting them as ten. |
| `sequence-pattern-miner` | `sequence-pattern` | losses after losses, time-of-day clusters, size creep after wins — behaviour of the system, not of one trade. |
| `regime-transition-tagger` | `regime-transition-flag` | regime changed mid-trade: the loss-cause classifier's "regime" bucket gets evidence. |

## Tell it

| part | produces | why |
|---|---|---|
| `trade-narrative-writer` | `trade-narrative` | the trade in plain words from its journal, for the lesson extractor, the brain's self-reflector plus the critic. The one LLM part here; it reads the ground-truth snapshot so it cannot invent numbers. |

## Counts
282 → **297 parts**, 225 → **240 data types**. Contracts hold. Every new type has a producer
and at least one consumer; 30 existing parts gained an input.
