# Is each part actually serving its purpose?

The ledger for the **third temporary goal**, given 2026-09-06. The full
statement in the user's own words is at the top of `docs/goal.md`; the short
form is in `CLAUDE.md`.

**The question this ledger answers is not the one `docs/feature-audit.md`
answers.** That one asked whether data *flows*, measured from each part's own
bus counters. This one asks whether what flows is **worth anything** — whether a
part is *"really providing or working with the data according to its intended
purpose, or if it is just a skeleton providing or inputting or outputing rubbish
data which is not useful just decorating data"*.

A part can be RUNNING, every wire can read CARRYING, all four checkers can pass,
and it can still publish a number that means nothing. A climbing counter proves
a message moved, never that the message was right.

## The verdicts, and what each one costs to claim

| verdict | what it means | what it takes to write it |
|---|---|---|
| `SERVING ITS PURPOSE` | fed real data, produced an output that is right | a named real-data input and the actual output, both recorded below |
| `SKELETON` | runs, publishes, and the output is decoration | the same evidence, showing the output is not usable |
| `NOT MEASURED` | nobody has looked yet | the honest default (Rule 8) |

**`NOT MEASURED` is the starting state of every row and is never green.** A bare
pass is not a verdict: a row must say what was fed in and what came out, or it
stays `NOT MEASURED`.

**Real data only (RL-063).** A fixture is exactly what makes a hollow part look
healthy, which is the whole reason this goal exists. A verdict may rest only on:
the captured tape, Upstox history, the free Yahoo / NSE sources
(`operate/historical_prints.py` and its siblings), or the real Upstox instrument
master.

## What the user asked for by name

| part kind | the question to actually ask |
|---|---|
| news parts | is the output a real reading — NIFTY support/resistance an operator would recognise — or a filled-in shape? |
| prediction parts | does it work **for every open trade**, not for one fixture? |
| online research | is it producing **real useful data**, or decoration? |

## Progress

**5 of 373 parts judged** as of 2026-09-06 — 4 serving their purpose, 1 skeleton (`cointegration-pair-finder`).

`correlation-cluster-mapper` was judged on 2026-09-06 after two defects were fixed that had it crash-looping on the live spine (139 restarts, exit code 1). A part that cannot stay up cannot be judged, so the fix came first: `correlation` has always returned None for a series with no variation and `map()` used the result without asking, and `describe_correlation_clusters` recomputed the whole quadratic mapping on every health read, defeating the remap pacing. Both are covered by real-tape tests.

The user said 370; the blueprint holds **373** parts across 29 categories, and every part is what the instruction means.


## The parts, by block


### Stock market news data (`stock-market-news-data`) — 29 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `broker-news-reader` | NOT MEASURED | | |
| `corporate-action-adjuster` | NOT MEASURED | | |
| `corporate-action-reader` | NOT MEASURED | | |
| `exchange-filing-reader` | NOT MEASURED | | |
| `financial-press-feed-reader` | NOT MEASURED | | |
| `instrument-restriction-state` | NOT MEASURED | | |
| `macro-event-calendar-reader` | NOT MEASURED | | |
| `market-session-calendar` | NOT MEASURED | | |
| `news-category-classifier` | NOT MEASURED | | |
| `news-credibility-scorer` | NOT MEASURED | | |
| `news-history-reader` | NOT MEASURED | | |
| `news-impact-forecaster` | NOT MEASURED | | |
| `news-item-deduplicator` | NOT MEASURED | | |
| `news-latency-meter` | NOT MEASURED | | |
| `news-novelty-scorer` | NOT MEASURED | | |
| `news-reaction-labeller` | NOT MEASURED | | |
| `news-segment-classifier` | NOT MEASURED | | |
| `news-sentiment-model` | NOT MEASURED | | |
| `news-source-health-monitor` | NOT MEASURED | | |
| `news-surprise-scorer` | NOT MEASURED | | |
| `news-symbol-resolver` | NOT MEASURED | | |
| `news-tape-writer` | NOT MEASURED | | |
| `news-text-structurer` | NOT MEASURED | | |
| `regulator-circular-reader` | NOT MEASURED | | |
| `results-calendar-reader` | NOT MEASURED | | |
| `social-chatter-reader` | NOT MEASURED | | |
| `trading-restriction-reader` | NOT MEASURED | | |
| `unexplained-move-investigator` | NOT MEASURED | | |
| `web-news-searcher` | NOT MEASURED | | |


### Market data feed (`market-data-feed`) — 24 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `api-key-pool-rotator` | NOT MEASURED | | |
| `ban-signal-detector` | NOT MEASURED | | |
| `broker-candle-bridge` | NOT MEASURED | | |
| `broker-market-data-bridge` | NOT MEASURED | | |
| `broker-order-book-bridge` | NOT MEASURED | | |
| `broker-quote-bridge` | NOT MEASURED | | |
| `broker-symbol-universe-bridge` | NOT MEASURED | | |
| `broker-underlying-price-frame-bridge` | NOT MEASURED | | |
| `cash-equity-shortlist-ranker` | NOT MEASURED | | |
| `ccxt-venue-reader` | NOT MEASURED | | |
| `cross-venue-price-consolidator` | NOT MEASURED | | |
| `equity-opportunity-profiler` | NOT MEASURED | | |
| `feed-coverage-auditor` | NOT MEASURED | | |
| `feed-gap-detector` | SERVING ITS PURPOSE | 12,000 real Upstox prints, 6 NIFTY contracts, 2026-09-04 tape; then the live feed | 5,262 messages seen on `upstox`, 4 streams tracked, 0 false sequence gaps, 2 real silence gaps. **Was SKELETON until 2026-09-06** — watched only two dead crypto streams and dropped every Upstox print on a silent `continue`. |
| `feed-jump-detector` | NOT MEASURED | | |
| `order-book-reader` | NOT MEASURED | | |
| `price-level-sampler` | NOT MEASURED | | |
| `quote-level-sampler` | NOT MEASURED | | |
| `stream-budget-planner` | NOT MEASURED | | |
| `symbol-catalogue-reader` | NOT MEASURED | | |
| `tick-size-resolver` | NOT MEASURED | | |
| `venue-pool-rotator` | NOT MEASURED | | |
| `venue-quote-stream-reader` | NOT MEASURED | | |
| `venue-trade-stream-reader` | NOT MEASURED | | |


### Closed trade decoding (`closed-trade-decoding`) — 20 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `entry-quality-scorer` | NOT MEASURED | | |
| `excursion-profiler` | NOT MEASURED | | |
| `exit-counterfactual-replayer` | NOT MEASURED | | |
| `exit-quality-scorer` | NOT MEASURED | | |
| `exploration-pair-decoder` | NOT MEASURED | | |
| `holding-horizon-profiler` | NOT MEASURED | | |
| `lesson-extractor` | NOT MEASURED | | |
| `loss-cause-classifier` | NOT MEASURED | | |
| `luck-skill-separator` | NOT MEASURED | | |
| `near-miss-recorder` | NOT MEASURED | | |
| `pnl-attributor` | NOT MEASURED | | |
| `regime-transition-tagger` | NOT MEASURED | | |
| `sequence-pattern-miner` | NOT MEASURED | | |
| `shortfall-decomposer` | NOT MEASURED | | |
| `stop-placement-auditor` | NOT MEASURED | | |
| `trade-cluster-detector` | NOT MEASURED | | |
| `trade-episode-encoder` | NOT MEASURED | | |
| `trade-narrative-writer` | NOT MEASURED | | |
| `trade-replay-verifier` | NOT MEASURED | | |
| `winner-pattern-miner` | NOT MEASURED | | |


### Learning loop (`learning-loop`) — 18 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `bot-scorekeeper` | NOT MEASURED | | |
| `champion-challenger-gate` | NOT MEASURED | | |
| `edge-graduation-gate` | NOT MEASURED | | |
| `exit-timing-learner` | NOT MEASURED | | |
| `feature-attribution-tracker` | NOT MEASURED | | |
| `feature-reliability-scorer` | NOT MEASURED | | |
| `forecast-trust-learner` | NOT MEASURED | | |
| `instruction-performance-tracker` | NOT MEASURED | | |
| `label-builder` | NOT MEASURED | | |
| `model-registry` | NOT MEASURED | | |
| `regret-tracker` | NOT MEASURED | | |
| `retrain-scheduler` | NOT MEASURED | | |
| `reward-shaper` | NOT MEASURED | | |
| `sample-weight-assigner` | NOT MEASURED | | |
| `signal-excursion-profiler` | NOT MEASURED | | |
| `signal-horizon-profiler` | NOT MEASURED | | |
| `signal-outcome-labeller` | NOT MEASURED | | |
| `slippage-learner` | NOT MEASURED | | |


### Intelligence (`intelligence`) — 18 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `abstention-coverage-auditor` | NOT MEASURED | | |
| `causal-refutation-battery` | NOT MEASURED | | |
| `correlation-cluster-mapper` | SERVING ITS PURPOSE — with a caveat on feed density | real 5-minute bars for 15 NSE shares over 60 days (Yahoo, free source), 4,365-4,374 bars each, fed synchronised by timestamp at production settings (window 256, minimum 64, threshold 0.7); and separately the full captured tape of 2026-09-04, 64,721 real prints across 582 NSE_EQ symbols | On the dense data it is a reading an operator would recognise: one cluster, HCLTECH/INFY/WIPRO at 0.78 average and 0.72 weakest — the IT trio. Same-sector pairs positive (INFY/WIPRO 0.807, SBIN/AXISBANK 0.477), cross-sector pairs at nothing (HDFCBANK/TCS -0.062, HINDUNILVR/NESTLEIND -0.033, TITAN/SBIN 0.08). It is measuring returns, not prices, and it says so. **On the captured tape the same settings left 167,693 of 169,071 pairs (99.2%) unmeasured** for want of 64 shared observations, and the one cluster it did form — 360ONE/CDSL/HINDZINC/IOLCP/TMCV — is five unrelated businesses. That is the feed's per-symbol density, not the part: it correctly refuses to call a thin pair uncorrelated. What it means live is that this part is only worth its CPU on symbols the feed samples densely, and `unmeasured_pairs` on its standing is the number that says so. |
| `counterfactual-replayer` | NOT MEASURED | | |
| `cross-segment-exposure-watch` | NOT MEASURED | | |
| `cross-segment-lesson-bridge` | NOT MEASURED | | |
| `cross-segment-signal-bridge` | NOT MEASURED | | |
| `decision-quality-critic` | NOT MEASURED | | |
| `edge-decay-tracker` | NOT MEASURED | | |
| `forgetting-auditor` | NOT MEASURED | | |
| `idea-generator` | NOT MEASURED | | |
| `market-anomaly-detector` | NOT MEASURED | | |
| `market-event-reader` | NOT MEASURED | | |
| `open-web-reader` | NOT MEASURED | | |
| `regime-break-detector` | NOT MEASURED | | |
| `self-model-reporter` | NOT MEASURED | | |
| `trial-count-accountant` | NOT MEASURED | | |
| `turbulence-index-gauge` | NOT MEASURED | | |


### Risk and capital allocation (`risk-capital-allocation`) — 17 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `capital-allotment-reader` | NOT MEASURED | | |
| `drawdown-breaker` | NOT MEASURED | | |
| `event-risk-limiter` | NOT MEASURED | | |
| `exit-order-chainer` | NOT MEASURED | | |
| `exposure-limiter` | NOT MEASURED | | |
| `halt-enforcer` | NOT MEASURED | | |
| `intraday-square-off-placer` | NOT MEASURED | | |
| `leverage-selector` | NOT MEASURED | | |
| `margin-liquidation-watch` | NOT MEASURED | | |
| `participation-capped-order-splitter` | NOT MEASURED | | |
| `position-flattener` | NOT MEASURED | | |
| `position-sizer` | NOT MEASURED | | |
| `pre-expiry-position-closer` | NOT MEASURED | | |
| `profit-lock` | NOT MEASURED | | |
| `stop-frequency-breaker` | NOT MEASURED | | |
| `stop-target-placer` | NOT MEASURED | | |
| `trade-capital-bounds-gate` | NOT MEASURED | | |


### Autonomous operation (`autonomous`) — 17 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `autonomy-boundary` | NOT MEASURED | | |
| `autonomy-policy-engine` | NOT MEASURED | | |
| `capability-gap-finder` | NOT MEASURED | | |
| `conservation-planner` | NOT MEASURED | | |
| `failing-part-detector` | NOT MEASURED | | |
| `folded-circuit-view` | NOT MEASURED | | |
| `human-override-reader` | NOT MEASURED | | |
| `no-progress-detector` | NOT MEASURED | | |
| `part-admission-gate` | NOT MEASURED | | |
| `part-author` | NOT MEASURED | | |
| `part-replacement-planner` | NOT MEASURED | | |
| `self-modification-journal` | NOT MEASURED | | |
| `survival-tier-monitor` | NOT MEASURED | | |
| `trading-halt-decider` | NOT MEASURED | | |
| `unattended-run-warden` | NOT MEASURED | | |
| `upstream-improvement-watch` | NOT MEASURED | | |
| `venue-outage-rider` | NOT MEASURED | | |


### Prediction (`prediction`) — 15 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `entropy-magnitude-forecaster` | NOT MEASURED | | |
| `flow-entropy-meter` | NOT MEASURED | | |
| `forecast-distribution-gate` | NOT MEASURED | | |
| `forecast-ensembler` | NOT MEASURED | | |
| `forecast-scorer` | NOT MEASURED | | |
| `implied-vol-reader` | NOT MEASURED | | |
| `kline-window-builder` | NOT MEASURED | | |
| `kronos-finetuner` | NOT MEASURED | | |
| `kronos-forecaster` | NOT MEASURED | | |
| `kronos-size-selector` | NOT MEASURED | | |
| `liquidation-cluster-mapper` | NOT MEASURED | | |
| `model-drift-monitor` | NOT MEASURED | | |
| `order-flow-state-encoder` | NOT MEASURED | | |
| `realised-vol-regressor` | NOT MEASURED | | |
| `volatility-feature-builder` | NOT MEASURED | | |


### Skills (`skills`) — 15 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `book-and-paper-fetcher` | NOT MEASURED | | |
| `community-chat-reader` | NOT MEASURED | | |
| `skill-composer` | NOT MEASURED | | |
| `skill-conflict-detector` | NOT MEASURED | | |
| `skill-distiller` | NOT MEASURED | | |
| `skill-gap-finder` | NOT MEASURED | | |
| `skill-index` | NOT MEASURED | | |
| `skill-loader` | NOT MEASURED | | |
| `skill-provenance-stamper` | NOT MEASURED | | |
| `skill-refresher` | NOT MEASURED | | |
| `skill-scorer` | NOT MEASURED | | |
| `skill-tester` | NOT MEASURED | | |
| `skill-version-keeper` | NOT MEASURED | | |
| `source-ingester` | NOT MEASURED | | |
| `video-lecture-reader` | NOT MEASURED | | |


### LLM foundation (`llm-foundation`) — 15 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `context-assembler` | NOT MEASURED | | |
| `decision-cost-accountant` | NOT MEASURED | | |
| `golden-case-keeper` | NOT MEASURED | | |
| `knowledge-embedder` | NOT MEASURED | | |
| `part-token-budgeter` | NOT MEASURED | | |
| `prompt-drift-monitor` | NOT MEASURED | | |
| `prompt-evaluator` | NOT MEASURED | | |
| `prompt-promotion-gate` | NOT MEASURED | | |
| `prompt-registry` | NOT MEASURED | | |
| `prompt-renderer` | NOT MEASURED | | |
| `prompt-template-author` | NOT MEASURED | | |
| `retrieval-index` | NOT MEASURED | | |
| `retrieval-quality-scorer` | NOT MEASURED | | |
| `retrieval-querier` | NOT MEASURED | | |
| `structured-output-enforcer` | NOT MEASURED | | |


### AI brain (`ai-brain`) — 14 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `bot-weight-sampler` | NOT MEASURED | | |
| `brain-self-reflector` | NOT MEASURED | | |
| `devils-advocate` | NOT MEASURED | | |
| `exploration-pair-opener` | NOT MEASURED | | |
| `forecast-bias-weigher` | NOT MEASURED | | |
| `intent-explainer` | NOT MEASURED | | |
| `intent-timing-gate` | NOT MEASURED | | |
| `market-thesis-reasoner` | NOT MEASURED | | |
| `opinion-arbiter` | NOT MEASURED | | |
| `opinion-conflict-resolver` | NOT MEASURED | | |
| `premortem-writer` | NOT MEASURED | | |
| `setup-second-opinion-reasoner` | NOT MEASURED | | |
| `size-hint-writer` | NOT MEASURED | | |
| `strategy-review-reasoner` | NOT MEASURED | | |


### Hardware resource governor (`resource-governor`) — 14 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `accelerator-scheduler` | NOT MEASURED | | |
| `duty-cycle-planner` | NOT MEASURED | | |
| `gate-actuator` | NOT MEASURED | | |
| `hardware-scanner` | NOT MEASURED | | |
| `hog-detector` | NOT MEASURED | | |
| `io-pressure-meter` | NOT MEASURED | | |
| `memory-pressure-forecaster` | NOT MEASURED | | |
| `off-state-verifier` | NOT MEASURED | | |
| `part-appetite-meter` | NOT MEASURED | | |
| `part-priority-reader` | NOT MEASURED | | |
| `part-restart-budgeter` | NOT MEASURED | | |
| `resource-reservation-ledger` | NOT MEASURED | | |
| `switch-oscillation-damper` | NOT MEASURED | | |
| `switching-planner` | NOT MEASURED | | |


### Knowledge (`knowledge`) — 13 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `contradiction-detector` | NOT MEASURED | | |
| `episode-embedder` | NOT MEASURED | | |
| `episodic-trade-store` | NOT MEASURED | | |
| `fact-provenance-tracker` | NOT MEASURED | | |
| `forgetting-curve-scheduler` | NOT MEASURED | | |
| `instruction-archive` | NOT MEASURED | | |
| `knowledge-graph-linker` | NOT MEASURED | | |
| `knowledge-pruner` | NOT MEASURED | | |
| `knowledge-snapshot-versioner` | NOT MEASURED | | |
| `procedural-playbook` | NOT MEASURED | | |
| `regime-memory-store` | NOT MEASURED | | |
| `semantic-fact-store` | NOT MEASURED | | |
| `symbol-profile-store` | NOT MEASURED | | |


### Universal opportunity scanner (`opportunity-scanner`) — 11 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `cointegration-pair-finder` | SKELETON | its own restored checkpoint | 5,949 symbols held, **5,512 holding a single price**, newest observation 2.4 days old, 7,814,961 pairs tested, 7,804,577 verdicts suppressed, board reads WORKING 1,619/s. No forgetting of any kind — no age bound, no subscription check. |
| `expiry-day-zero-to-hero-detector` | NOT MEASURED | | |
| `liquidity-grader` | NOT MEASURED | | |
| `mean-reversion-detector` | SERVING ITS PURPOSE | 51,438 real in-session NIFTY/BANKNIFTY prints, 2026-09-04 | 0 candidates at the crypto-derived floor; **1,470** after it was re-derived from Indian economics. Fires now; the single global floor across a 1,721-symbol universe is still the open defect. |
| `momentum-burst-detector` | NOT MEASURED | | |
| `news-catalyst-detector` | NOT MEASURED | | |
| `regime-classifier` | NOT MEASURED | | |
| `spread-reversion-detector` | NOT MEASURED | | |
| `universal-symbol-sweeper` | NOT MEASURED | | |
| `volatility-gap-detector` | NOT MEASURED | | |
| `watch-condition-compiler` | NOT MEASURED | | |


### Hypothesis (`hypothesis`) — 11 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `expectancy-decomposer` | NOT MEASURED | | |
| `hypothesis-deduplicator` | NOT MEASURED | | |
| `hypothesis-falsifier` | NOT MEASURED | | |
| `hypothesis-mutator` | NOT MEASURED | | |
| `hypothesis-ranker` | NOT MEASURED | | |
| `hypothesis-regime-tagger` | NOT MEASURED | | |
| `instruction-retirer` | NOT MEASURED | | |
| `instruction-writer` | NOT MEASURED | | |
| `loss-inverter` | NOT MEASURED | | |
| `power-estimator` | NOT MEASURED | | |
| `symbolic-hypothesis-miner` | NOT MEASURED | | |


### Execution and venue adapter (`execution-venue-adapter`) — 11 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `ccxt-order-router` | NOT MEASURED | | |
| `limit-price-walker` | NOT MEASURED | | |
| `order-not-found-debouncer` | NOT MEASURED | | |
| `order-reject-classifier` | NOT MEASURED | | |
| `order-resubmitter` | NOT MEASURED | | |
| `order-state-poller` | NOT MEASURED | | |
| `resting-order-cancel-policy` | NOT MEASURED | | |
| `venue-balance-reader` | NOT MEASURED | | |
| `venue-order-status-translator` | NOT MEASURED | | |
| `venue-position-reader` | NOT MEASURED | | |
| `venue-rate-budgeter` | NOT MEASURED | | |


### Online research (`online-research`) — 11 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `arxiv-feed-reader` | NOT MEASURED | | |
| `copy-latency-estimator` | NOT MEASURED | | |
| `copy-worthiness-scorer` | NOT MEASURED | | |
| `edge-comparator` | NOT MEASURED | | |
| `exchange-announcement-reader` | NOT MEASURED | | |
| `github-strategy-miner` | NOT MEASURED | | |
| `leaderboard-reader` | NOT MEASURED | | |
| `onchain-position-reader` | NOT MEASURED | | |
| `options-flow-reader` | NOT MEASURED | | |
| `strategy-decoder` | NOT MEASURED | | |
| `trader-record-verifier` | NOT MEASURED | | |


### Observability (`observability`) — 10 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `ablation-harness` | NOT MEASURED | | |
| `alert-raiser` | NOT MEASURED | | |
| `board-publisher` | NOT MEASURED | | |
| `board-snapshot-builder` | NOT MEASURED | | |
| `clock-skew-monitor` | NOT MEASURED | | |
| `drawdown-episode-tracker` | NOT MEASURED | | |
| `fund-conservation-auditor` | NOT MEASURED | | |
| `heartbeat-collector` | NOT MEASURED | | |
| `probe-runner` | NOT MEASURED | | |
| `stale-board-watch` | NOT MEASURED | | |


### LLM services (`llm-services`) — 10 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `ground-truth-snapshot-builder` | NOT MEASURED | | |
| `llm-backpressure-gauge` | NOT MEASURED | | |
| `llm-model-picker` | NOT MEASURED | | |
| `llm-request-router` | NOT MEASURED | | |
| `llm-response-cache` | NOT MEASURED | | |
| `local-model-caller` | NOT MEASURED | | |
| `metered-api-caller` | NOT MEASURED | | |
| `paid-spend-ledger` | NOT MEASURED | | |
| `subscription-quota-watch` | NOT MEASURED | | |
| `subscription-session-caller` | NOT MEASURED | | |


### Backtesting (`backtesting`) — 10 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `backtest-scorer` | NOT MEASURED | | |
| `execution-cost-model` | NOT MEASURED | | |
| `fill-volume-capper` | NOT MEASURED | | |
| `historical-bar-store` | NOT MEASURED | | |
| `instruction-promotion-gate` | NOT MEASURED | | |
| `instruction-replayer` | NOT MEASURED | | |
| `intra-bar-fill-sequencer` | NOT MEASURED | | |
| `live-vs-replay-reconciler` | NOT MEASURED | | |
| `lookahead-auditor` | NOT MEASURED | | |
| `walk-forward-splitter` | NOT MEASURED | | |


### Paper trading on live data (`paper-live-trading`) — 10 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `book-walk-fill-pricer` | NOT MEASURED | | |
| `live-switch-guard` | NOT MEASURED | | |
| `money-mode-reader` | NOT MEASURED | | |
| `order-destination-router` | NOT MEASURED | | |
| `order-idempotency-stamper` | NOT MEASURED | | |
| `order-latency-simulator` | NOT MEASURED | | |
| `paper-account-keeper` | SERVING ITS PURPOSE | the three built segments' real accounts | index-options/stock-options/cash-equity accounts present and correct. The retired `paper-account-futures` component holding BTCUSDT on binance-usdm was removed 2026-09-06 and did not return through a restart. |
| `paper-fill-simulator` | NOT MEASURED | | |
| `paper-liquidation-simulator` | NOT MEASURED | | |
| `stop-order-manager` | NOT MEASURED | | |


### Bull bot (`bull-bot`) — 10 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `bull-conviction-calibrator` | NOT MEASURED | | |
| `bull-conviction-model` | NOT MEASURED | | |
| `bull-entry-timer` | NOT MEASURED | | |
| `bull-exit-plan-proposer` | NOT MEASURED | | |
| `bull-feature-builder` | NOT MEASURED | | |
| `bull-opinion-composer` | NOT MEASURED | | |
| `bull-outlier-rejector` | NOT MEASURED | | |
| `bull-position-invalidation-watcher` | NOT MEASURED | | |
| `bull-setup-filter` | NOT MEASURED | | |
| `bull-setup-weight-learner` | NOT MEASURED | | |


### Bear bot (`bear-bot`) — 10 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `bear-conviction-calibrator` | NOT MEASURED | | |
| `bear-conviction-model` | NOT MEASURED | | |
| `bear-entry-timer` | NOT MEASURED | | |
| `bear-exit-plan-proposer` | NOT MEASURED | | |
| `bear-feature-builder` | NOT MEASURED | | |
| `bear-opinion-composer` | NOT MEASURED | | |
| `bear-outlier-rejector` | NOT MEASURED | | |
| `bear-position-invalidation-watcher` | NOT MEASURED | | |
| `bear-setup-filter` | NOT MEASURED | | |
| `bear-setup-weight-learner` | NOT MEASURED | | |


### Profit tailgating bot (`profit-tailgating-bot`) — 9 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `tail-copy-selector` | NOT MEASURED | | |
| `tail-crowding-detector` | NOT MEASURED | | |
| `tail-follow-conviction-model` | NOT MEASURED | | |
| `tail-move-remaining-estimator` | NOT MEASURED | | |
| `tail-mover-qualifier` | NOT MEASURED | | |
| `tail-opinion-composer` | NOT MEASURED | | |
| `tail-setup-weight-learner` | NOT MEASURED | | |
| `tail-trailing-exit-planner` | NOT MEASURED | | |
| `tail-winner-selector` | NOT MEASURED | | |


### Broker adapter (Indian markets) (`broker-adapter`) — 9 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `broker-account-funds-reader` | NOT MEASURED | | |
| `broker-history-reader` | NOT MEASURED | | |
| `broker-instrument-catalogue-reader` | NOT MEASURED | | |
| `broker-margin-quoter` | NOT MEASURED | | |
| `broker-market-feed-reader` | NOT MEASURED | | |
| `broker-market-tape-writer` | NOT MEASURED | | |
| `broker-price-level-sampler` | NOT MEASURED | | |
| `broker-token-refresh-scheduler` | NOT MEASURED | | |
| `subscribed-instrument-listing-filter` | NOT MEASURED | | |


### Capital desk (`capital-desk`) — 8 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `allocation-conservation-checker` | NOT MEASURED | | |
| `allocation-rebalance-proposer` | NOT MEASURED | | |
| `capital-settings-change-recorder` | NOT MEASURED | | |
| `capital-settings-validator` | NOT MEASURED | | |
| `capital-utilisation-meter` | NOT MEASURED | | |
| `live-balance-divergence-watch` | NOT MEASURED | | |
| `main-account-settings-reader` | NOT MEASURED | | |
| `paper-currency-converter` | NOT MEASURED | | |


### Portfolio and position state (`portfolio-state`) — 7 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `cost-basis-tracker` | NOT MEASURED | | |
| `fill-reconciler` | NOT MEASURED | | |
| `fund-lock-ledger` | NOT MEASURED | | |
| `liquidation-price-tracker` | NOT MEASURED | | |
| `peak-excursion-tracker` | NOT MEASURED | | |
| `position-close-detector` | NOT MEASURED | | |
| `usdt-pnl-accountant` | NOT MEASURED | | |


### Ledger and audit trail (`ledger`) — 6 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `control-recorder` | NOT MEASURED | | |
| `funding-settlement-recorder` | NOT MEASURED | | |
| `journal-integrity-checker` | NOT MEASURED | | |
| `learning-recorder` | NOT MEASURED | | |
| `position-recorder` | NOT MEASURED | | |
| `trade-lifecycle-recorder` | NOT MEASURED | | |


### Segment bot (bull, bear, profit tailgating) (`segment-bot`) — 1 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `instrument-selector` | NOT MEASURED | | |

