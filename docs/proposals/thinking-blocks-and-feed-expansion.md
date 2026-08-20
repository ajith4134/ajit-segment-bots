# The thinking blocks opened further, and the feed made to rotate

Proposed by Claude 2026-08-20 at the user's request: *"think of other features inside the
mini project foundation features inside intelligence, learning loop, hypothesis, knowledge,
online research, hardware resource governor, skills, ai brain"*. Applied to `docs/features.json`
by `dashboard/blueprint_edits/apply_2026-08-20_thinking_blocks_and_feed.py` as `origin:
proposed` — every part below is open to the user's veto. The feed section answers RL-049 and
is `origin: user`.

Where each idea came from is stated. Nothing here is invented from nowhere: each part closes a
gap the reconciliation measured (`trading-system/docs/RECONCILIATION-2026-08-19.md` §2, the
seventeen intelligence tests never run), copies a mechanism a read codebase proved
(`docs/research/repo-harvest/`), or applies a pattern from the sovereign-agent research.

## Intelligence — 8 parts (10 → 18)

The goal document sets 23 mechanical tests of intelligence. Four of the unrun ones are now
parts, so they are *measured*, not promised:

| part | closes | from |
|---|---|---|
| `causal-refutation-battery` | **R2** DoWhy refutation — "failing any one is a hard disqualifier" | reconciliation §2 |
| `trial-count-accountant` | **L7** trial counting — without it every edge is unverified by definition | reconciliation §2; `validation/promotion_gate.py` exists, uncalled |
| `forgetting-auditor` | **L9** catastrophic forgetting — calibration updates every close with no regression suite | reconciliation §2 |
| `abstention-coverage-auditor` | **R4** abstention with teeth — 223,987 abstentions, coverage unverified | reconciliation §2 |
| `counterfactual-replayer` | the road not taken, so the critic scores the choice, not the luck | TradingAgents reflection, done numerically |
| `cross-segment-signal-bridge` | a spot whale inflow is futures information; today no segment hears another | RL-019 split, with the one global thinker bridging |
| `self-model-reporter` | "calibrated knowledge of its own competence" — the plan's own words | AJIT-MASTER-PLAN line 1363 |
| `edge-decay-tracker` | edges decay; a half-life retires an instruction before its drawdown does | `~/research/crypto-alpha-decay-execution-costs.md` |

## Learning loop — 8 parts (7 → 15)

The bots' conviction models are learned (RL-026). Today nothing in the blueprint says *how*
they are trained. These are the training loop as parts:

`label-builder` (triple-barrier labels, already in `features/triple_barrier.py` unwired) →
`sample-weight-assigner` (uniqueness weighting, the one feature module that *is* wired today) →
`retrain-scheduler` → `model-registry` → `champion-challenger-gate` (the no-regression gate
that exists, now fed by refutation + trial count so the classifier meets the same bar as the
carry strategy). Plus `reward-shaper` (the learner optimises net, risk-adjusted reward, not raw
P&L), `feature-attribution-tracker` (SHAP-style, feeds reliability scoring and the explainer),
and `regret-tracker` (the arbiter as a bandit: regret against the best single bot per regime).

## Hypothesis — 6 parts (5 → 11)

`symbolic-hypothesis-miner` (genetic programming over features —
`~/research/alpha-discovery-gp-symbolic-regression.md`), `hypothesis-deduplicator` (a
rediscovery is not a discovery; 599 PRIOR-ART ledger rows say this matters),
`hypothesis-falsifier` (the kill condition written *before* the test),
`power-estimator` (how many trades before the edge is distinguishable from zero — MinBTL is
already in `promotion_gate.py`), `hypothesis-mutator` (vary a near-miss), and
`hypothesis-regime-tagger` (born in a regime, retired with it).

## Knowledge — 7 parts (6 → 13)

`knowledge-graph-linker`, `fact-provenance-tracker` ("provenance and expire" — the plan's
words), `contradiction-detector`, `episode-embedder` (embed the condition, never the outcome),
`regime-memory-store`, `knowledge-snapshot-versioner` (hash the whole knowledge state so a
past decision replays against what was known then — this is what makes the critic honest),
`forgetting-curve-scheduler` (confidence decays unless re-confirmed).

## Online research — 7 parts (7 → 14)

`trader-record-verifier` (a leaderboard claim is checked against chain data before it is
copied), `copy-latency-estimator` (how stale is the position by the time we see it — the
tailgater's chasing question), `exchange-announcement-reader` (listings and delistings are
intraday movers and the sweeper should hear them), `onchain-flow-aggregator` (stablecoin
mints, exchange reserves), `options-flow-reader` (block trades feed the volatility-gap
detector), `arxiv-feed-reader` and `github-strategy-miner` (today's fifteen-repo harvest is
this part, done by hand; it should be a part).

## Hardware resource governor — 7 parts (7 → 14)

From the sovereign-agent patterns (loop detection, graded scarcity) and from running one
machine 24/7: `io-pressure-meter`, `accelerator-scheduler` (off until a GPU exists — T-3 is
what makes that free), `memory-pressure-forecaster` (minutes to OOM, not "is it full"),
`part-restart-budgeter` (crash-loop detection), `duty-cycle-planner` (retrain in quiet
hours), `switch-oscillation-damper` (a flapping part is a fault), `resource-reservation-ledger`
(the risk gate and the halt path are never starved).

## Skills — 6 parts (9 → 15)

`skill-tester` (a distilled skill is scored on past episodes before it is indexed — a skill
is a hypothesis), `skill-composer`, `skill-version-keeper`, `skill-gap-finder` (a question
with no skill is the best research prompt there is; it feeds the open-web reader and the
paper fetcher), `video-lecture-reader` (the `ytgrab` tool exists; six videos built Rule 6),
`skill-provenance-stamper`.

## AI brain — 6 parts (5 → 11)

`opinion-conflict-resolver` (the `opposing_positions` rule becomes a part: straddle, pick or
abstain), `bot-weight-sampler` (Thompson-sampled trust from regret), `size-hint-writer`
(conviction as a capped Kelly fraction the sizer may only cut), `premortem-writer` and
`devils-advocate` (TradingAgents' debate, kept: an unanswered objection downgrades the
intent), `intent-timing-gate` (the intent leaves when the entry timer says, or never).

## Market data feed — 6 parts, RL-049 (11 → 17)

What the old project measured (`~/research/binance-fstream-connection-limits.md`,
`binance-withheld-streams.md`, `src/ops/rate_budget.py`): one venue per request class, one
file-backed budget per venue, polls dropped rather than risk a 418, 928 streams per fstream
connection, and Binance silently withholding streams from this host. It never rotated.

Public market data is rate-limited **per IP, not per key**, so spreading across *venues* is
what widens the feed; key rotation helps only private endpoints (balance, position, orders).
Both are parts:

| part | does |
|---|---|
| `ban-signal-detector` | 418/429/retry-after plus the withheld-stream signature (0 frames on a guaranteed 1 Hz stream) → `venue-standing` |
| `venue-pool-rotator` | each request class spread across every venue carrying the symbol, by standing plus headroom |
| `api-key-pool-rotator` | private calls rotated across every key a venue allows |
| `cross-venue-price-consolidator` | one price per symbol from every venue, weighted by staleness (`features/consolidated_price.py` exists in the old project, unwired) |
| `stream-budget-planner` | streams to connections to venues within the *measured* limits |
| `feed-coverage-auditor` | per symbol, who supplies what, including where nobody does — on the board |

## Counts

221 → **282 parts**, 169 → **225 data types**, 25 blocks unchanged. Contracts hold
(`check_contracts.py`). Every new data type has both a producer and a consumer.
