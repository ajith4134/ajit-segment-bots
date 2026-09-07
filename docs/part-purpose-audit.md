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

## The probe's own three states, and why none of them is `SERVING ITS PURPOSE`

`python3 dashboard/audit_part_purpose.py` walks all 373 parts against the live
spine and fills every row's evidence. It reads counters, so it can see whether a
part is fed and whether anything comes out — and it **cannot** see whether the
number that comes out is right. A state named `SERVING ITS PURPOSE` coming from
a counter reader would be the green tile this whole goal exists to prevent, so
the probe writes what it actually proved:

| probe state | what it proves | what it does not |
|---|---|---|
| `PRODUCES REAL OUTPUT` | real input reached it, it published on a declared output, and its own work counters are above zero | that the values are correct — that is a person reading the numbers |
| `FED BUT PRODUCES NOTHING` | real input reached it and nothing has ever come of it | that it is hollow: a part that acts on an event is silent until the event happens |
| `ONLY REFUSALS` | input arrived and every counter above zero is a refusal or an attempt | which of the two it is — a gate correctly refusing everything and a part that cannot do its job look identical here |
| `NOT MEASURED` | nothing reached it, or it is not running | anything at all |

Only a row that says `SERVING ITS PURPOSE` or `SKELETON` has had a person look at
the actual output. The probe's three states are the map of **where to look
next**, ranked: `FED BUT PRODUCES NOTHING` and `ONLY REFUSALS` first, because a
part being fed thousands of messages and producing nothing is either a defect or
a trigger that never fires, and both are worth knowing.

**Publishing nothing is disqualifying, whatever the internal counters say.** The
probe was writing `PRODUCES REAL OUTPUT` for `arxiv-feed-reader` off a
`fetches_attempted` counter at 60,780 while it had never published one paper —
every fetch `refused_no_search_installed`. Attempting is not producing. A part
that declares a real output in the blueprint and never sends it cannot be green;
one that declares none is a genuine sink and is judged on its work alone.

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

**5 of 373 hand-judged; all 373 now carry measured evidence** as of 2026-09-07.

    hand-judged                4 serving their purpose, 1 skeleton (cointegration-pair-finder)
    PRODUCES REAL OUTPUT     136   fed, publishing, own work counters above zero
    FED BUT PRODUCES NOTHING  64   fed and nothing has ever come of it
    ONLY REFUSALS             26   fed, and everything above zero is a refusal or an attempt
    NOT MEASURED             142   nothing has reached it, or it is not running

Measured against the live spine on a real trading day, 2026-09-07, over a 45
second observed window with the cumulative counters since the spine started at
08:57:32 UTC. Re-run it with `python3 dashboard/audit_part_purpose.py`; every row
below carries what was fed in and what came out, so a re-run that disagrees is a
finding rather than a refresh.

`correlation-cluster-mapper` was judged on 2026-09-06 after two defects were fixed that had it crash-looping on the live spine (139 restarts, exit code 1). A part that cannot stay up cannot be judged, so the fix came first: `correlation` has always returned None for a series with no variation and `map()` used the result without asking, and `describe_correlation_clusters` recomputed the whole quadratic mapping on every health read, defeating the remap pacing. Both are covered by real-tape tests.

The user said 370; the blueprint holds **373** parts across 29 categories, and every part is what the instruction means.


## The parts, by block


### Stock market news data (`stock-market-news-data`) — 29 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `broker-news-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `corporate-action-adjuster` | PRODUCES REAL OUTPUT | broker-instrument-listing 63,531; corporate-action-report 20 | corporate-action 19 — work: actions_understood 19 |
| `corporate-action-reader` | NOT MEASURED | nothing has reached it | corporate-action-report 20 — work: is_warm 1; requests_made 1 |
| `exchange-filing-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `financial-press-feed-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `instrument-restriction-state` | PRODUCES REAL OUTPUT | instrument-restriction-report 442 | instrument-restriction 3,978 — work: symbols_restricted 221 |
| `macro-event-calendar-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `market-session-calendar` | NOT MEASURED | nothing has reached it | market-session-state 73 — work: is_warm 1; requests_made 1 |
| `news-category-classifier` | NOT MEASURED | nothing has reached it | nothing published |
| `news-credibility-scorer` | NOT MEASURED | nothing has reached it | nothing published |
| `news-history-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `news-impact-forecaster` | NOT MEASURED | nothing has reached it | nothing published |
| `news-item-deduplicator` | NOT MEASURED | nothing has reached it | nothing published |
| `news-latency-meter` | NOT MEASURED | nothing has reached it | nothing published |
| `news-novelty-scorer` | NOT MEASURED | nothing has reached it | nothing published |
| `news-reaction-labeller` | NOT MEASURED | nothing has reached it | nothing published |
| `news-segment-classifier` | NOT MEASURED | nothing has reached it | nothing published |
| `news-sentiment-model` | NOT MEASURED | nothing has reached it | nothing published |
| `news-source-health-monitor` | NOT MEASURED | nothing has reached it | nothing published |
| `news-surprise-scorer` | NOT MEASURED | nothing has reached it | nothing published |
| `news-symbol-resolver` | NOT MEASURED | nothing has reached it | nothing published |
| `news-tape-writer` | NOT MEASURED | nothing has reached it | nothing published |
| `news-text-structurer` | NOT MEASURED | nothing has reached it | nothing published |
| `regulator-circular-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `results-calendar-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `social-chatter-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `trading-restriction-reader` | NOT MEASURED | nothing has reached it | instrument-restriction-report 442 — work: requests_made 4; is_warm 1 |
| `unexplained-move-investigator` | NOT MEASURED | nothing has reached it | nothing published |
| `web-news-searcher` | NOT MEASURED | nothing has reached it | nothing published |


### Market data feed (`market-data-feed`) — 24 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `api-key-pool-rotator` | NOT MEASURED | nothing has reached it | nothing published |
| `ban-signal-detector` | NOT MEASURED | nothing has reached it | nothing published |
| `broker-candle-bridge` | PRODUCES REAL OUTPUT | broker-candle 332,826; broker-subscribed-instrument-listing 1,964 | candle 405,484 — work: updates_released_after_listing 914 |
| `broker-market-data-bridge` | PRODUCES REAL OUTPUT | broker-market-data 166,406; broker-subscribed-instrument-listing 1,964 | market-data 2,184,641 — work: updates_released_after_listing 1,284 |
| `broker-order-book-bridge` | PRODUCES REAL OUTPUT | broker-order-book-snapshot 162,141; broker-subscribed-instrument-listing 1,965 | order-book-snapshot 1,481,228 — work: updates_released_after_listing 1,283 |
| `broker-quote-bridge` | PRODUCES REAL OUTPUT | broker-order-book-snapshot 162,032; broker-subscribed-instrument-listing 1,964 | market-quote 211,186 — work: updates_released_after_listing 1,282 |
| `broker-symbol-universe-bridge` | PRODUCES REAL OUTPUT | broker-instrument-listing 63,531; broker-price-frame 3,312; cash-equity-shortlist 486 | symbol-universe 255,475 — work: equities_listed 2,191; contracts_published 320; underlyings_priced 13; underlyings_published 13; +1 more |
| `broker-underlying-price-frame-bridge` | PRODUCES REAL OUTPUT | broker-price-frame 3,312; broker-subscribed-instrument-listing 1,969 | symbol-price-frame 100,320 — work: levels_matched 24,867; frames_published 3,135; underlyings_resolved 13 |
| `cash-equity-shortlist-ranker` | PRODUCES REAL OUTPUT | liquidity-grade 105,960; candle 102,183; broker-instrument-listing 63,499; +2 more | cash-equity-shortlist 962 — work: ranks_computed 14,178; priced_symbols 11 |
| `ccxt-venue-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `cross-venue-price-consolidator` | NOT MEASURED | nothing has reached it | nothing published |
| `equity-opportunity-profiler` | PRODUCES REAL OUTPUT | broker-instrument-listing 63,499; broker-token-standing 1,105 | equity-historical-profile 222 — work: requests_planned 4,417; profiles_published 222; windows_already_read 222 |
| `feed-coverage-auditor` | PRODUCES REAL OUTPUT | market-data 109,479; order-book-snapshot 105,964; symbol-universe 36,691 | feed-coverage 901,979 — work: symbols_expected 679; covered 650; uncovered 27; partial 2 |
| `feed-gap-detector` | SERVING ITS PURPOSE | 12,000 real Upstox prints, 6 NIFTY contracts, 2026-09-04 tape; then the live feed | 5,262 messages seen on `upstox`, 4 streams tracked, 0 false sequence gaps, 2 real silence gaps. **Was SKELETON until 2026-09-06** — watched only two dead crypto streams and dropped every Upstox print on a silent `continue`. |
| `feed-jump-detector` | PRODUCES REAL OUTPUT | candle 102,072 | feed-jump 5,429 — work: checks_inside_a_widened_bound 35,849; jumps_found 24,440; levels_published 5,429; level_refreshes 2,978; +3 more |
| `order-book-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `price-level-sampler` | PRODUCES REAL OUTPUT | market-data 109,547 | symbol-price-frame 94,629 — work: trades_observed 104,588; frames_published 2,992 |
| `quote-level-sampler` | PRODUCES REAL OUTPUT | market-quote 105,765 | symbol-quote-frame 5,818 — work: quotes_observed 105,765; frames_published 2,909 |
| `stream-budget-planner` | NOT MEASURED | nothing has reached it | nothing published |
| `symbol-catalogue-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `tick-size-resolver` | PRODUCES REAL OUTPUT | order-book-snapshot 105,758; symbol-universe 36,756 | price-increment 49,357 — work: increments_published 49,529; increment_refreshes 47,738; declared_inferred_disagreements 19,076; increment_changes 1,791; +3 more |
| `venue-pool-rotator` | NOT MEASURED | nothing has reached it | nothing published |
| `venue-quote-stream-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `venue-trade-stream-reader` | NOT MEASURED | nothing has reached it | nothing published |


### Closed trade decoding (`closed-trade-decoding`) — 20 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `entry-quality-scorer` | ONLY REFUSALS | market-data 109,397; journal-entry 100,895; closed-trade 2 | nothing published — work: unmeasurable_no_signal_time 2 |
| `excursion-profiler` | FED BUT PRODUCES NOTHING | peak-excursion 520 | nothing published |
| `exit-counterfactual-replayer` | FED BUT PRODUCES NOTHING | market-data 109,376; bull-exit-plan 149; bear-exit-plan 20; +1 more | nothing published |
| `exit-quality-scorer` | FED BUT PRODUCES NOTHING | peak-excursion 520 | nothing published |
| `exploration-pair-decoder` | FED BUT PRODUCES NOTHING | directional-opinion 24,152; closed-trade 2 | nothing published |
| `holding-horizon-profiler` | NOT MEASURED | nothing has reached it | nothing published |
| `lesson-extractor` | ONLY REFUSALS | pnl-attribution 2; stop-audit 2 | nothing published — work: refused_too_few_trades 676 |
| `loss-cause-classifier` | FED BUT PRODUCES NOTHING | peak-excursion 520 | nothing published |
| `luck-skill-separator` | PRODUCES REAL OUTPUT | volatility-forecast 255,098; symbol-profile 103,284; closed-trade 2 | outcome-significance 10 — work: indistinguishable_from_noise 2; outcomes_assessed 2 |
| `near-miss-recorder` | PRODUCES REAL OUTPUT | entry-candidate 56,443; directional-opinion 19,521; trade-intent 14,236; +1 more | near-miss-episode 34,672 — work: never_resolved_for_want_of_a_price 7,189; near_misses_recorded 6,810; by_reason.all 1 bot(s) stood down: bear-bot -- conviction-below-threshold 6,489; unresolved 4,952; +3 more |
| `pnl-attributor` | PRODUCES REAL OUTPUT | cost-estimate 382,128; peak-excursion 520; closed-trade 2; +1 more | pnl-attribution 12 — work: total_absolute_residual 25,638; trades_attributed 2; trades_where_costs_exceeded_the_move 2 |
| `regime-transition-tagger` | PRODUCES REAL OUTPUT | market-regime 59,277; closed-trade 2 | regime-transition-flag 6 — work: trades_tagged 2 |
| `sequence-pattern-miner` | ONLY REFUSALS | closed-trade 2 | nothing published — work: refused_thin_samples 4,428 |
| `shortfall-decomposer` | FED BUT PRODUCES NOTHING | order-book-snapshot 105,886; symbol-price-frame 6,053; closed-trade 2; +1 more | nothing published |
| `stop-placement-auditor` | PRODUCES REAL OUTPUT | market-data 109,366; stop-target-plan 7,125; peak-excursion 520; +1 more | stop-audit 10 — work: stops_audited 2; trades_without_a_stop 2 |
| `trade-cluster-detector` | FED BUT PRODUCES NOTHING | correlation-cluster 22,621; closed-trade 2 | nothing published |
| `trade-episode-encoder` | ONLY REFUSALS | journal-entry 100,858; near-miss-episode 8,904; closed-trade 2; +3 more | nothing published — work: re_encodes_skipped 29,212; refused_incomplete 3 |
| `trade-narrative-writer` | FED BUT PRODUCES NOTHING | journal-entry 100,842; decision-rationale 9,253 | nothing published |
| `trade-replay-verifier` | ONLY REFUSALS | journal-entry 100,904; closed-trade 2; fill 2 | nothing published — work: paper_fills_not_verifiable 2; trades_not_verifiable 2 |
| `winner-pattern-miner` | FED BUT PRODUCES NOTHING | outcome-significance 2 | nothing published |


### Learning loop (`learning-loop`) — 18 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `bot-scorekeeper` | FED BUT PRODUCES NOTHING | directional-opinion 24,147; learning-reward 2; outcome-significance 2 | nothing published |
| `champion-challenger-gate` | NOT MEASURED | nothing has reached it | nothing published — work: a_tie_keeps_the_champion 1 |
| `edge-graduation-gate` | FED BUT PRODUCES NOTHING | coverage-report 63,276 | nothing published |
| `exit-timing-learner` | NOT MEASURED | nothing has reached it | nothing published |
| `feature-attribution-tracker` | PRODUCES REAL OUTPUT | bear-feature-vector 58,302; bear-raw-conviction 56,927; bull-feature-vector 1,701; +1 more | feature-attribution 175,440 — work: attributions_recorded 58,480; by_bot.bear-bot 56,927; by_bot.bull-bot 1,553; recent_kept 200 |
| `feature-reliability-scorer` | FED BUT PRODUCES NOTHING | vol-feature-set 102,046; feature-attribution 58,480 | nothing published |
| `forecast-trust-learner` | NOT MEASURED | nothing has reached it | nothing published |
| `instruction-performance-tracker` | NOT MEASURED | nothing has reached it | nothing published |
| `label-builder` | PRODUCES REAL OUTPUT | cost-estimate 382,148; peak-excursion 520; closed-trade 2 | training-label 36 — work: by_component.the-entry-was-timed:false 2; by_component.the-setup-was-right:true 2; by_component.the-size-was-right:true 2; labels_built 2; +3 more |
| `model-registry` | FED BUT PRODUCES NOTHING | retrain-request 32 | nothing published |
| `regret-tracker` | FED BUT PRODUCES NOTHING | market-regime 59,277; trade-intent 16,410 | nothing published |
| `retrain-scheduler` | PRODUCES REAL OUTPUT | duty-cycle 256; training-label 17 | retrain-request 160 — work: requests 32; duty_cycle_granted 1 |
| `reward-shaper` | PRODUCES REAL OUTPUT | peak-excursion 520; closed-trade 2; outcome-significance 2; +2 more | learning-reward 8 — work: bounded_at_the_cap 2; by_component.capital-tied-up 2; by_component.distinguishable-from-noise 2; by_component.risk-taken-to-earn-it 2; +1 more |
| `sample-weight-assigner` | PRODUCES REAL OUTPUT | training-label 17 | sample-weight 68 — work: by_reason.age 17; weights_assigned 17; by_reason.it-did-not-resolve-inside-its-horizon 2; smallest_weight 0 |
| `signal-excursion-profiler` | PRODUCES REAL OUTPUT | training-label 17 | excursion-profile 495,039 — work: profiles_published 1,001,188; profiles_fitted 334,950; profile_refreshes 100,300; claims_that_came_wrong 54,014; +4 more |
| `signal-horizon-profiler` | PRODUCES REAL OUTPUT | training-label 17 | horizon-profile 22,130 — work: claims_recorded 112,132; profiles_fitted 5,570; profiles_published 5,570; slowest_median_seconds 250; +1 more |
| `signal-outcome-labeller` | PRODUCES REAL OUTPUT | entry-candidate 60,107; market-regime 59,645; symbol-price-frame 6,049 | training-label 270 — work: prices_observed 1,123,882; claims_opened 114; by_detector.spread-reversion-detector 102; unresolved 74; +7 more |
| `slippage-learner` | FED BUT PRODUCES NOTHING | fill 2 | nothing published |


### Intelligence (`intelligence`) — 18 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `abstention-coverage-auditor` | PRODUCES REAL OUTPUT | directional-opinion 24,147; near-miss-episode 8,904 | coverage-report 189,576 — work: acted_on 16,455; abstained 7,692; near_misses_scored 1,963; overall_coverage 1 |
| `causal-refutation-battery` | FED BUT PRODUCES NOTHING | feature-attribution 58,480 | nothing published |
| `correlation-cluster-mapper` | SERVING ITS PURPOSE — with a caveat on feed density | real 5-minute bars for 15 NSE shares over 60 days (Yahoo, free source), 4,365-4,374 bars each, fed synchronised by timestamp at production settings (window 256, minimum 64, threshold 0.7); and separately the full captured tape of 2026-09-04, 64,721 real prints across 582 NSE_EQ symbols | On the dense data it is a reading an operator would recognise: one cluster, HCLTECH/INFY/WIPRO at 0.78 average and 0.72 weakest — the IT trio. Same-sector pairs positive (INFY/WIPRO 0.807, SBIN/AXISBANK 0.477), cross-sector pairs at nothing (HDFCBANK/TCS -0.062, HINDUNILVR/NESTLEIND -0.033, TITAN/SBIN 0.08). It is measuring returns, not prices, and it says so. **On the captured tape the same settings left 167,693 of 169,071 pairs (99.2%) unmeasured** for want of 64 shared observations, and the one cluster it did form — 360ONE/CDSL/HINDZINC/IOLCP/TMCV — is five unrelated businesses. That is the feed's per-symbol density, not the part: it correctly refuses to call a thin pair uncorrelated. What it means live is that this part is only worth its CPU on symbols the feed samples densely, and `unmeasured_pairs` on its standing is the number that says so. |
| `counterfactual-replayer` | FED BUT PRODUCES NOTHING | directional-opinion 24,166; trade-intent 16,403; symbol-price-frame 6,057 | nothing published |
| `cross-segment-exposure-watch` | PRODUCES REAL OUTPUT | position 28,246 | exposure-view 5,766 — work: by_segment.stock-options 3,235,541; gross_notional 3,235,541; net_notional 3,235,541; by_underlying.KOTAKBANK 941,008; +10 more |
| `cross-segment-lesson-bridge` | NOT MEASURED | nothing has reached it | nothing published |
| `cross-segment-signal-bridge` | PRODUCES REAL OUTPUT | broker-open-interest 154,240; position 28,158; symbol-price-frame 6,044; +1 more | cross-segment-signal 105,497 — work: by_receiving_segment.index-futures 105,497; by_receiving_segment.stock-futures 105,497; by_receiving_segment.stock-options 105,497; signals_sent 105,497; +5 more |
| `decision-quality-critic` | FED BUT PRODUCES NOTHING | verified-snapshot 156,395; directional-opinion 24,172; counter-argument 9,272; +4 more | nothing published |
| `edge-decay-tracker` | NOT MEASURED | nothing has reached it | nothing published |
| `forgetting-auditor` | NOT MEASURED | nothing has reached it | nothing published |
| `idea-generator` | NOT MEASURED | regime-memory 25,000 | llm-request 56,300 |
| `market-anomaly-detector` | PRODUCES REAL OUTPUT | feed-coverage 303,427; market-data 109,366; order-book-snapshot 105,847; +1 more | market-anomaly 570,795 — work: checks 191,408; anomalies 33,749; anomaly_this_venue_has_stopped_updating 32,114; by_anomaly.this-venue-has-stopped-updating 32,114; +3 more |
| `market-event-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `open-web-reader` | ONLY REFUSALS | skill-gap 91,855 | nothing published — work: fetch_failures 91,855 |
| `regime-break-detector` | FED BUT PRODUCES NOTHING | journal-entry 100,888; symbol-price-frame 6,124 | nothing published |
| `self-model-reporter` | PRODUCES REAL OUTPUT | coverage-report 63,208 | competence-map 95,918 — work: maps_produced 25,064 |
| `trial-count-accountant` | NOT MEASURED | nothing has reached it | nothing published — work: nominal_significance 0 |
| `turbulence-index-gauge` | PRODUCES REAL OUTPUT | symbol-price-frame 6,096 | turbulence-index 5,776 — work: measurements 5,781; measured 1,201; mean_shrinkage 1 |


### Risk and capital allocation (`risk-capital-allocation`) — 17 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `capital-allotment-reader` | NOT MEASURED | main-account-setting 1,105 | capital-allotment 26,892; trade-capital-bounds 13,461; leverage-ceiling 13,446 |
| `drawdown-breaker` | NOT MEASURED | account-balance 11,718; closed-trade 2 | risk-limit 15,402 |
| `event-risk-limiter` | PRODUCES REAL OUTPUT | market-anomaly 191,488; turbulence-index 5,775 | risk-limit 4,387,468 — work: events_registered 34,052; by_kind.anomaly 33,767; events_restated 32,888; limits_issued 16,991; +5 more |
| `exit-order-chainer` | PRODUCES REAL OUTPUT | stop-target-plan 7,125; fill 2 | stop-adjustment 6 — work: fills_without_a_plan 2 |
| `exposure-limiter` | NOT MEASURED | position 28,369; correlation-cluster 22,699; account-balance 11,757; +2 more | risk-limit 27,670 |
| `halt-enforcer` | PRODUCES REAL OUTPUT | trading-halt 135,261; policy-decision 9,257; instrument-restriction 3,978; +2 more | risk-limit 127,869 — work: limits_issued 128,956; zero_limits_issued 121,793; symbol_scoped_limits_issued 121,719; halts_raised 4; +2 more |
| `intraday-square-off-placer` | ONLY REFUSALS | position 28,239; money-mode 3,255; market-session-state 18 | nothing published — work: refused_no_session 171 |
| `leverage-selector` | FED BUT PRODUCES NOTHING | volatility-forecast 245,689; trade-intent 16,244; leverage-ceiling 3,339; +1 more | nothing published |
| `margin-liquidation-watch` | NOT MEASURED | nothing has reached it | nothing published |
| `participation-capped-order-splitter` | FED BUT PRODUCES NOTHING | volatility-forecast 255,075; order-book-snapshot 105,909 | nothing published |
| `position-flattener` | ONLY REFUSALS | position 28,262; money-mode 3,258; human-override 1,098 | nothing published — work: reads_with_no_override 8 |
| `position-sizer` | ONLY REFUSALS | risk-limit 4,563,996; price-increment 49,497; trade-intent 16,414; +5 more | nothing published — work: missing_stop_price 8,104; intents_that_stood_aside 7,136; missing_entry_price 1,174 |
| `pre-expiry-position-closer` | ONLY REFUSALS | position 28,262; money-mode 3,258; broker-subscribed-instrument-listing 1,964; +1 more | nothing published — work: refused_no_session 181; positions_with_no_listing 5 |
| `profit-lock` | PRODUCES REAL OUTPUT | excursion-profile 100,312; position 28,246; symbol-price-frame 6,125 | stop-adjustment 9,429 — work: retracements_learned 33,481; adjustments 31,741; held 23,701; trailed 17; +1 more |
| `stop-frequency-breaker` | PRODUCES REAL OUTPUT | closed-trade 2 | risk-limit 231 — work: baseline_stop_rate 0 |
| `stop-target-placer` | PRODUCES REAL OUTPUT | volatility-forecast 255,098; symbol-profile 103,320; excursion-profile 100,313; +5 more | stop-target-plan 21,375 — work: excursions_learned 33,586; plans 7,125 |
| `trade-capital-bounds-gate` | FED BUT PRODUCES NOTHING | trade-capital-bounds 3,366; capital-settings-verdict 1,106 | nothing published |


### Autonomous operation (`autonomous`) — 17 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `autonomy-boundary` | PRODUCES REAL OUTPUT | survival-tier 114,157; money-mode 3,258 | autonomy-envelope 224,396 — work: by_narrowing_reason.a-recent-self-modification-broke-something 112,198; by_narrowing_reason.measured-competence-fell-below-the-level 112,198; issues 112,198; issued_on_the_paper_floor 112,197; +2 more |
| `autonomy-policy-engine` | PRODUCES REAL OUTPUT | autonomy-envelope 112,205; competence-map 24,001; trade-intent 16,414 | policy-decision 37,112 — work: allowed 9,278; decisions 9,278 |
| `capability-gap-finder` | FED BUT PRODUCES NOTHING | folded-circuit-map 79,757 | nothing published |
| `conservation-planner` | PRODUCES REAL OUTPUT | survival-tier 114,151 | conservation-plan 114,151 — work: parts_stopped 542,520; plans_made 3,288; plans_are_nested 1 |
| `failing-part-detector` | NOT MEASURED | nothing has reached it | part-fault 3,876 — work: checks 343,937; by_kind.taking-longer-every-tick 2,801; faults_found 2,801; silent_faults_found 2,801; +4 more |
| `folded-circuit-view` | NOT MEASURED | nothing has reached it | folded-circuit-map 159,486 — work: folds 80,185; dark_blocks_reported 2,143; parts_declared 373; blocks_declared 29 |
| `human-override-reader` | NOT MEASURED | nothing has reached it | human-override 3,302 — work: longest_active_seconds 660,760; expired_overrides 1,104; reads 1,104 |
| `no-progress-detector` | PRODUCES REAL OUTPUT | journal-entry 100,830 | part-fault 4 — work: checks 125,557; quiet_periods 125,556; stages_watched 5; by_stage.bounded-order 1; +1 more |
| `part-admission-gate` | FED BUT PRODUCES NOTHING | policy-decision 9,268 | nothing published |
| `part-author` | FED BUT PRODUCES NOTHING | folded-circuit-map 79,743 | nothing published |
| `part-replacement-planner` | ONLY REFUSALS | part-fault 969 | nothing published — work: refused_no_replacement 968; refused_unknown_part 1 |
| `self-modification-journal` | FED BUT PRODUCES NOTHING | policy-decision 9,268 | nothing published |
| `survival-tier-monitor` | PRODUCES REAL OUTPUT | llm-quota-state 1,094; llm-spend-state 1,094 | survival-tier 457,600 — work: readings 114,650; times_bound_by_quota 114,160; unmeasured_readings 490; tier_changes 1 |
| `trading-halt-decider` | PRODUCES REAL OUTPUT | market-anomaly 191,488; survival-tier 114,614; autonomy-envelope 112,160; +3 more | trading-halt 270,552 — work: decisions 135,309; times_closing_was_permitted_during_a_halt 113,049; by_cause.the-market-data-does-not-make-sense 112,972; by_cause.the-autonomy-envelope-does-not-permit-trading 71; +5 more |
| `unattended-run-warden` | ONLY REFUSALS | part-fault 970 | nothing published — work: refused_unrecognised_fault 970 |
| `upstream-improvement-watch` | NOT MEASURED | nothing has reached it | nothing published — work: dependencies_declared 128 |
| `venue-outage-rider` | PRODUCES REAL OUTPUT | symbol-price-frame 6,125; feed-gap 1,531 | outage-state 84,545 — work: longest_silence_seconds 1,570,714; readings 84,545; quiet_markets 79,178; flowing 5,367 |


### Prediction (`prediction`) — 15 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `entropy-magnitude-forecaster` | PRODUCES REAL OUTPUT | flow-entropy 152,944; vol-feature-set 102,162 | volatility-forecast 911,896 — work: forecasts_made 152,941; forecasts_produced 113,496; outcomes_observed 64,921; measured_multiple_by_quintile.4 1; +4 more |
| `flow-entropy-meter` | PRODUCES REAL OUTPUT | order-flow-state 155,226 | flow-entropy 152,944 — work: uniform_fallback_rows_used 1,430,237; measurements 153,073; measured 121,237; window_seconds 120 |
| `forecast-distribution-gate` | PRODUCES REAL OUTPUT | kline-window 102,091; price-forecast 102,091 | forecast-out-of-distribution-flag 306,099 — work: checks 102,033; flagged_out_of_distribution 102,033 |
| `forecast-ensembler` | PRODUCES REAL OUTPUT | volatility-forecast 255,032; price-forecast 102,090; forecast-out-of-distribution-flag 102,032 | ensemble-forecast 324,120 — work: combinations 324,120 |
| `forecast-scorer` | ONLY REFUSALS | price-forecast 102,092; symbol-price-frame 6,124 | nothing published — work: unusable_forecasts_counted_not_scored 102,092 |
| `implied-vol-reader` | PRODUCES REAL OUTPUT | broker-option-greeks 162,013; market-quote 105,574; symbol-price-frame 6,121; +1 more | implied-vol-surface 270,171 — work: reads 90,057; surfaces_published 79,987; reads_too_thin_for_a_surface 10,070; options_feed_connected 1 |
| `kline-window-builder` | PRODUCES REAL OUTPUT | candle 102,183; corporate-action 19 | kline-window 715,099 — work: candles_observed 102,183; windows_built 102,157; windows_still_filling 85,156; gaps_found 23,622; +1 more |
| `kronos-finetuner` | FED BUT PRODUCES NOTHING | kline-window 102,135; accelerator-slot 75,140; retrain-request 32; +1 more | nothing published |
| `kronos-forecaster` | PRODUCES REAL OUTPUT | kline-window 102,110; accelerator-slot 75,041 | price-forecast 612,660 — work: forecasts_requested 102,110; model_is_loaded 1 |
| `kronos-size-selector` | NOT MEASURED | nothing has reached it | nothing published |
| `liquidation-cluster-mapper` | NOT MEASURED | nothing has reached it | nothing published |
| `model-drift-monitor` | NOT MEASURED | nothing has reached it | nothing published |
| `order-flow-state-encoder` | PRODUCES REAL OUTPUT | market-data 107,176; order-book-snapshot 103,700 | order-flow-state 155,740 — work: seconds_encoded 771,981; empty_seconds_encoded 750,685; by_state.(+0,5) 724,351; by_state.(+0,3) 19,704; +15 more |
| `realised-vol-regressor` | PRODUCES REAL OUTPUT | vol-feature-set 102,144 | volatility-forecast 609,785 — work: forecasts_made 102,144; mean_absolute_error_in_log_space 2; is_fitted 1 |
| `volatility-feature-builder` | PRODUCES REAL OUTPUT | kline-window 102,157; implied-vol-surface 90,096 | vol-feature-set 306,471 — work: sets_built 102,157; sets_with_an_implied_surface 7,093; complete_sets 4,372 |


### Skills (`skills`) — 15 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `book-and-paper-fetcher` | ONLY REFUSALS | skill-gap 91,700 | nothing published — work: failures 91,700 |
| `community-chat-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `skill-composer` | NOT MEASURED | nothing has reached it | nothing published |
| `skill-conflict-detector` | NOT MEASURED | nothing has reached it | nothing published — work: checks 1,100 |
| `skill-distiller` | NOT MEASURED | nothing has reached it | nothing published |
| `skill-gap-finder` | PRODUCES REAL OUTPUT | llm-request 49,649 | skill-gap 367,360 — work: gaps_open 91,840; by_reason.nothing-in-the-index-addresses-it 49,649 |
| `skill-index` | NOT MEASURED | nothing has reached it | nothing published |
| `skill-loader` | NOT MEASURED | nothing has reached it | nothing published — work: budget 4,000 |
| `skill-provenance-stamper` | NOT MEASURED | nothing has reached it | nothing published |
| `skill-refresher` | NOT MEASURED | nothing has reached it | nothing published |
| `skill-scorer` | NOT MEASURED | nothing has reached it | nothing published |
| `skill-tester` | NOT MEASURED | nothing has reached it | nothing published |
| `skill-version-keeper` | NOT MEASURED | nothing has reached it | nothing published |
| `source-ingester` | NOT MEASURED | nothing has reached it | nothing published |
| `video-lecture-reader` | NOT MEASURED | nothing has reached it | nothing published |


### LLM foundation (`llm-foundation`) — 15 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `context-assembler` | FED BUT PRODUCES NOTHING | verified-snapshot 156,376 | nothing published |
| `decision-cost-accountant` | FED BUT PRODUCES NOTHING | trade-intent 16,400; usdt-pnl-statement 2 | nothing published |
| `golden-case-keeper` | FED BUT PRODUCES NOTHING | closed-trade 2 | nothing published |
| `knowledge-embedder` | FED BUT PRODUCES NOTHING | journal-entry 100,793 | nothing published |
| `part-token-budgeter` | FED BUT PRODUCES NOTHING | llm-spend-state 1,099; llm-quota-state 1,098 | nothing published |
| `prompt-drift-monitor` | NOT MEASURED | nothing has reached it | nothing published |
| `prompt-evaluator` | NOT MEASURED | nothing has reached it | nothing published |
| `prompt-promotion-gate` | NOT MEASURED | nothing has reached it | nothing published |
| `prompt-registry` | NOT MEASURED | nothing has reached it | nothing published |
| `prompt-renderer` | ONLY REFUSALS | llm-request 49,608 | nothing published — work: refused_no_active_version 49,608 |
| `prompt-template-author` | NOT MEASURED | nothing has reached it | nothing published |
| `retrieval-index` | FED BUT PRODUCES NOTHING | retrieval-query 14,075 | nothing published |
| `retrieval-quality-scorer` | NOT MEASURED | nothing has reached it | nothing published |
| `retrieval-querier` | PRODUCES REAL OUTPUT | llm-request 49,618 | retrieval-query 14,071 — work: queries_made 14,071 |
| `structured-output-enforcer` | NOT MEASURED | nothing has reached it | nothing published |


### AI brain (`ai-brain`) — 14 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `bot-weight-sampler` | FED BUT PRODUCES NOTHING | market-regime 59,238 | nothing published |
| `brain-self-reflector` | FED BUT PRODUCES NOTHING | decision-rationale 9,276; counter-argument 9,275; premortem-note 9,275; +1 more | nothing published |
| `devils-advocate` | PRODUCES REAL OUTPUT | verified-snapshot 156,214; directional-opinion 20,681; trade-intent 16,388; +1 more | llm-request 37,008; counter-argument 27,756 — work: objections_raised 18,504; arguments_made 9,252; by_objection.the-conviction-is-the-model's-own-number-not-a-measured-frequency 9,252; by_objection.there-is-nowhere-this-trade-is-wrong 9,252 |
| `exploration-pair-opener` | ONLY REFUSALS | market-regime 59,238; directional-opinion 24,152 | nothing published — work: refused_by_reason.the-bots-do-not-actually-disagree 19,653 |
| `forecast-bias-weigher` | FED BUT PRODUCES NOTHING | ensemble-forecast 324,267 | nothing published |
| `intent-explainer` | PRODUCES REAL OUTPUT | verified-snapshot 156,267; feature-attribution 58,480; directional-opinion 20,691; +1 more | decision-rationale 37,028; llm-request 37,028 — work: fully_supported 9,257; rationales_written 9,257; written_without_a_model 9,257 |
| `intent-timing-gate` | PRODUCES REAL OUTPUT | bear-entry-timing 58,324; trade-intent 16,404; symbol-price-frame 6,058; +1 more | timed-intent 18,536 — work: acted_now 9,268; by_reason.nothing-is-waiting-for-anything 9,268; intents_timed 9,268 |
| `market-thesis-reasoner` | FED BUT PRODUCES NOTHING | verified-snapshot 156,324; market-regime 59,238 | nothing published |
| `opinion-arbiter` | PRODUCES REAL OUTPUT | coverage-report 63,176; market-regime 59,238; regime-memory 24,990; +4 more | trade-intent 244,739 — work: symbols_arbitrated 16,391; intents_formed 9,255; intents_by_agreement.all-bots-agree 9,254; intents_by_agreement.only-one-bot-had-a-view 1 |
| `opinion-conflict-resolver` | NOT MEASURED | market-regime 59,361; regime-memory 25,020; directional-opinion 24,182 | conflict-ruling 11,954 |
| `premortem-writer` | PRODUCES REAL OUTPUT | verified-snapshot 156,327; directional-opinion 20,708; trade-intent 16,401 | llm-request 37,060; premortem-note 18,530 — work: failure_modes_recorded 37,060; by_failure_mode.the-book-that-sized-this-is-not-there-when-it-is-exited 9,265; by_failure_mode.the-conviction-was-never-a-measured-frequency 9,265; by_failure_mode.the-regime-the-models-were-fitted-on-ends 9,265; +2 more |
| `setup-second-opinion-reasoner` | PRODUCES REAL OUTPUT | verified-snapshot 156,482; directional-opinion 16,392 | directional-opinion 91,986; llm-request 31,160 — work: confirmed 7,790; reviews_written 7,790 |
| `size-hint-writer` | PRODUCES REAL OUTPUT | bear-calibrated-conviction 56,927; trade-intent 16,396; bull-calibrated-conviction 1,553 | size-hint 9,260 — work: hints_written 9,260; sized_down_for_unmeasured_conviction 9,260; by_agreement.all-bots-agree 9,259; by_agreement.only-one-bot-had-a-view 1 |
| `strategy-review-reasoner` | FED BUT PRODUCES NOTHING | competence-map 23,973; closed-trade 2 | nothing published |


### Hardware resource governor (`resource-governor`) — 14 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `accelerator-scheduler` | NOT MEASURED | part-priority 74,868; hardware-capacity 1,108 | accelerator-slot 150,113 |
| `duty-cycle-planner` | PRODUCES REAL OUTPUT | part-resource-usage 307,596; market-data 109,439 | duty-cycle 512 — work: activity_by_hour.9 94,181; activity_by_hour.8 5,747; activity_by_hour.18 4,953; activity_by_hour.7 1,540; +8 more |
| `gate-actuator` | ONLY REFUSALS | switch-plan 1,086 | nothing published — work: plans_refused 4 |
| `hardware-scanner` | NOT MEASURED | nothing has reached it | hardware-capacity 5,540 — work: capacity.total_ram_bytes 31,530,676,224; capacity.available_ram_bytes 9,124,634,624; readings 1,109; capacity.logical_cpus 12; +3 more |
| `hog-detector` | PRODUCES REAL OUTPUT | part-resource-usage 307,915; hardware-capacity 1,109 | hog-report 40,041 — work: reports 20,055; by_part.keys_not_reported 45 |
| `io-pressure-meter` | NOT MEASURED | nothing has reached it | io-pressure 1,106 — work: pressure.network_receive_bytes_per_second 114,024; pressure.network_transmit_bytes_per_second 36,244; readings 1,111; pressure.some_stalled_10s 0 |
| `memory-pressure-forecaster` | PRODUCES REAL OUTPUT | part-resource-usage 307,740; hardware-capacity 1,108 | memory-forecast 1,050 — work: forecasts 1,055; samples_by_part.keys_not_reported 318 |
| `off-state-verifier` | FED BUT PRODUCES NOTHING | part-resource-usage 307,596 | nothing published |
| `part-appetite-meter` | NOT MEASURED | nothing has reached it | part-resource-usage 1,539,981 — work: parts_measured 308,181; readings 308,181 |
| `part-priority-reader` | NOT MEASURED | nothing has reached it | part-priority 224,672 — work: reads 1,103; stated 68 |
| `part-restart-budgeter` | PRODUCES REAL OUTPUT | part-fault 969 | restart-budget 89,729 — work: granted 498 |
| `resource-reservation-ledger` | PRODUCES REAL OUTPUT | part-priority 74,936; hardware-capacity 1,109 | resource-reservation 11,020 — work: memory_reserved 2,684,354,560; reservations 10; cpu_reserved 5 |
| `switch-oscillation-damper` | NOT MEASURED | nothing has reached it | nothing published |
| `switching-planner` | PRODUCES REAL OUTPUT | part-resource-usage 307,596; conservation-plan 114,095; restart-budget 89,729; +7 more | switch-plan 1,087 — work: plans 1,087; hog_reports_read 20 |


### Knowledge (`knowledge`) — 13 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `contradiction-detector` | NOT MEASURED | nothing has reached it | nothing published — work: checks 1,101 |
| `episode-embedder` | NOT MEASURED | nothing has reached it | nothing published — work: is_deterministic 1 |
| `episodic-trade-store` | NOT MEASURED | nothing has reached it | nothing published |
| `fact-provenance-tracker` | NOT MEASURED | nothing has reached it | nothing published |
| `forgetting-curve-scheduler` | NOT MEASURED | nothing has reached it | nothing published — work: half_lives_days.listing-age-seconds 365; half_lives_days.tick-size 365; half_lives_days.funding-interval-seconds 180; half_lives_days.liquidation-behaviour 90; +5 more |
| `instruction-archive` | NOT MEASURED | nothing has reached it | nothing published |
| `knowledge-graph-linker` | NOT MEASURED | nothing has reached it | nothing published |
| `knowledge-pruner` | NOT MEASURED | nothing has reached it | nothing published |
| `knowledge-snapshot-versioner` | NOT MEASURED | nothing has reached it | knowledge-snapshot 2 — work: snapshots_taken 1 |
| `procedural-playbook` | NOT MEASURED | nothing has reached it | nothing published |
| `regime-memory-store` | PRODUCES REAL OUTPUT | market-regime 59,277; regime-transition-flag 2 | regime-memory 110,687 — work: too_few_occurrences 25,000 |
| `semantic-fact-store` | FED BUT PRODUCES NOTHING | journal-entry 100,815 | nothing published |
| `symbol-profile-store` | PRODUCES REAL OUTPUT | market-data 109,440; order-book-snapshot 105,904 | symbol-profile 911,202 — work: absent_fields.funding-interval-seconds 103,284; absent_fields.tick-size 103,284; new_listings 103,284; profiles_built 103,284; +3 more |


### Universal opportunity scanner (`opportunity-scanner`) — 11 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `cointegration-pair-finder` | SKELETON | its own restored checkpoint | 5,949 symbols held, **5,512 holding a single price**, newest observation 2.4 days old, 7,814,961 pairs tested, 7,804,577 verdicts suppressed, board reads WORKING 1,619/s. No forgetting of any kind — no age bound, no subscription check. |
| `expiry-day-zero-to-hero-detector` | ONLY REFUSALS | broker-market-data 166,454; broker-option-greeks 162,010; broker-subscribed-instrument-listing 1,964; +1 more | nothing published — work: instruments_that_are_not_options 13 |
| `liquidity-grader` | PRODUCES REAL OUTPUT | market-data 109,479; order-book-snapshot 105,961 | liquidity-grade 313,527 — work: grades_computed 30,414; by_grade.thin 12,309; by_grade.untradeable 8,525; by_grade.tradeable 7,461; +4 more |
| `mean-reversion-detector` | SERVING ITS PURPOSE | 51,438 real in-session NIFTY/BANKNIFTY prints, 2026-09-04 | 0 candidates at the crypto-derived floor; **1,470** after it was re-derived from Indian economics. Fires now; the single global floor across a 1,721-symbol universe is still the open defect. |
| `momentum-burst-detector` | ONLY REFUSALS | symbol-profile 103,177; symbol-price-frame 6,107; training-label 17 | nothing published — work: not_a_burst 62,004; no_playbook_rule 1,328 |
| `news-catalyst-detector` | NOT MEASURED | nothing has reached it | nothing published |
| `regime-classifier` | PRODUCES REAL OUTPUT | symbol-price-frame 6,106 | market-regime 829,157 — work: classifications 1,215,180; unclassified 1,201,135; regimes_published 59,645; regime_refreshes 30,179; +5 more |
| `spread-reversion-detector` | PRODUCES REAL OUTPUT | cointegrated-pair 10,411; symbol-price-frame 6,101; symbol-quote-frame 2,902; +1 more | entry-candidate 4,081 — work: leg_priced_from_a_trade 1,186,361; tests 930,024; stale_leg 447,055; leg_quote_too_wide 277,320; +4 more |
| `universal-symbol-sweeper` | ONLY REFUSALS | liquidity-grade 105,550; cross-segment-signal 105,549; symbol-universe 35,888; +3 more | nothing published — work: skipped_untradeable 168,582; skipped_unmeasurable 83,997; skipped_already_held 3,796 |
| `volatility-gap-detector` | PRODUCES REAL OUTPUT | volatility-forecast 255,104; implied-vol-surface 90,135; training-label 17 | entry-candidate 346,771 — work: tests 168,119; candidates 58,229; implied_rich 58,229 |
| `watch-condition-compiler` | NOT MEASURED | nothing has reached it | nothing published |


### Hypothesis (`hypothesis`) — 11 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `expectancy-decomposer` | FED BUT PRODUCES NOTHING | horizon-profile 5,495; pnl-attribution 2 | nothing published |
| `hypothesis-deduplicator` | NOT MEASURED | nothing has reached it | nothing published |
| `hypothesis-falsifier` | NOT MEASURED | nothing has reached it | nothing published |
| `hypothesis-mutator` | FED BUT PRODUCES NOTHING | near-miss-episode 8,904 | nothing published |
| `hypothesis-ranker` | NOT MEASURED | nothing has reached it | nothing published — work: maximum_reachable_trades 10,000; rankings 1,099 |
| `hypothesis-regime-tagger` | FED BUT PRODUCES NOTHING | market-regime 59,269 | nothing published |
| `instruction-retirer` | NOT MEASURED | nothing has reached it | nothing published |
| `instruction-writer` | FED BUT PRODUCES NOTHING | regime-memory 24,990; horizon-profile 5,495 | nothing published |
| `loss-inverter` | NOT MEASURED | nothing has reached it | nothing published |
| `power-estimator` | NOT MEASURED | nothing has reached it | nothing published — work: maximum_testable_trades 10,000; power 1 |
| `symbolic-hypothesis-miner` | FED BUT PRODUCES NOTHING | kline-window 102,147; training-label 17 | nothing published |


### Execution and venue adapter (`execution-venue-adapter`) — 11 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `ccxt-order-router` | NOT MEASURED | nothing has reached it | nothing published |
| `limit-price-walker` | FED BUT PRODUCES NOTHING | market-data 109,541; order-book-snapshot 106,013; order-request 16 | nothing published |
| `order-not-found-debouncer` | NOT MEASURED | nothing has reached it | nothing published |
| `order-reject-classifier` | NOT MEASURED | nothing has reached it | nothing published |
| `order-resubmitter` | NOT MEASURED | nothing has reached it | nothing published |
| `order-state-poller` | NOT MEASURED | nothing has reached it | nothing published |
| `resting-order-cancel-policy` | FED BUT PRODUCES NOTHING | symbol-price-frame 6,098; order-request 16 | nothing published |
| `venue-balance-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `venue-order-status-translator` | NOT MEASURED | nothing has reached it | nothing published |
| `venue-position-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `venue-rate-budgeter` | NOT MEASURED | nothing has reached it | nothing published |


### Online research (`online-research`) — 11 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `arxiv-feed-reader` | ONLY REFUSALS | skill-gap 91,840 | nothing published — work: refused_no_search_installed 91,840 |
| `copy-latency-estimator` | FED BUT PRODUCES NOTHING | symbol-price-frame 6,060 | nothing published |
| `copy-worthiness-scorer` | NOT MEASURED | nothing has reached it | nothing published |
| `edge-comparator` | NOT MEASURED | nothing has reached it | nothing published |
| `exchange-announcement-reader` | FED BUT PRODUCES NOTHING | broker-instrument-listing 63,118 | nothing published |
| `github-strategy-miner` | FED BUT PRODUCES NOTHING | skill-gap 91,720 | nothing published |
| `leaderboard-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `onchain-position-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `options-flow-reader` | FED BUT PRODUCES NOTHING | symbol-universe 36,803 | nothing published |
| `strategy-decoder` | NOT MEASURED | nothing has reached it | nothing published |
| `trader-record-verifier` | NOT MEASURED | nothing has reached it | nothing published |


### Observability (`observability`) — 10 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `ablation-harness` | NOT MEASURED | nothing has reached it | nothing published |
| `alert-raiser` | PRODUCES REAL OUTPUT | feed-coverage 292,928; market-anomaly 187,965; trading-halt 135,212; +3 more | alert 3,182 — work: suppressed_duplicates 250,160; raised 3,182; by_severity.high 3,109; active 2,590; +9 more |
| `board-publisher` | PRODUCES REAL OUTPUT | board-snapshot 1,097 | board-link 64 — work: publishes 64 |
| `board-snapshot-builder` | PRODUCES REAL OUTPUT | feed-coverage 304,349; competence-map 23,987; account-balance 11,715; +13 more | board-snapshot 2,196 — work: tiles_offered 643,264; by_state.OK 517,657; by_state.NOT MEASURED 125,607; snapshots_built 1,098 |
| `clock-skew-monitor` | PRODUCES REAL OUTPUT | broker-market-data 166,422 | alert 593 — work: alerts_raised 593; venues_watched 1 |
| `drawdown-episode-tracker` | FED BUT PRODUCES NOTHING | account-balance 11,718 | nothing published |
| `fund-conservation-auditor` | FED BUT PRODUCES NOTHING | journal-entry 100,892; fill 2 | nothing published |
| `heartbeat-collector` | NOT MEASURED | nothing has reached it | heartbeat-table 6 — work: reports_received 344,833; tables_built 2,060; parts_expected 324; reporting 321; +4 more |
| `probe-runner` | PRODUCES REAL OUTPUT | journal-gap 5; heartbeat-table 3 | probe-result 5,152 — work: runs 3,282; measured 3,279; slowest_seconds 631; probes_registered 16; +1 more |
| `stale-board-watch` | PRODUCES REAL OUTPUT | board-snapshot 1,097; board-link 64 | alert 1 — work: checks 1,799; longest_lag_seconds 1; alerts_raised 1 |


### LLM services (`llm-services`) — 10 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `ground-truth-snapshot-builder` | PRODUCES REAL OUTPUT | market-data 109,366; order-book-snapshot 105,847 | verified-snapshot 1,093,631 — work: snapshots_built 156,233 |
| `llm-backpressure-gauge` | PRODUCES REAL OUTPUT | survival-tier 114,638; llm-quota-state 1,099; llm-spend-state 1,099 | llm-backpressure 112,757 — work: readings 112,757; times_bound_by_quota 112,256; times_open 112,256; times_bound_by_survival_tier 500; +1 more |
| `llm-model-picker` | FED BUT PRODUCES NOTHING | llm-request 49,649 | nothing published |
| `llm-request-router` | FED BUT PRODUCES NOTHING | llm-backpressure 112,757; llm-quota-state 1,099; llm-spend-state 1,099 | nothing published |
| `llm-response-cache` | NOT MEASURED | nothing has reached it | nothing published |
| `local-model-caller` | NOT MEASURED | nothing has reached it | nothing published |
| `metered-api-caller` | NOT MEASURED | nothing has reached it | nothing published |
| `paid-spend-ledger` | NOT MEASURED | nothing has reached it | llm-spend-state 4,391 — work: ceiling 1 |
| `subscription-quota-watch` | NOT MEASURED | nothing has reached it | llm-quota-state 4,391 — work: readings 1,099 |
| `subscription-session-caller` | NOT MEASURED | nothing has reached it | nothing published |


### Backtesting (`backtesting`) — 10 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `backtest-scorer` | NOT MEASURED | nothing has reached it | nothing published — work: confidence_multiple 2 |
| `execution-cost-model` | PRODUCES REAL OUTPUT | market-data 109,217 | cost-estimate 1,909,609 — work: estimates_made 381,962; unfitted_estimates 381,962; quantile 1 |
| `fill-volume-capper` | PRODUCES REAL OUTPUT | historical-window 10,540 | fillable-size 34,939 — work: requests 34,939; filled_in_full 26,498; total_fillable 26,498; total_intended 26,498; +1 more |
| `historical-bar-store` | PRODUCES REAL OUTPUT | candle 98,386 | historical-window 41,800 — work: windows_built 236,629; windows_with_gaps 236,629; duplicate_bars 94,434; windows_published 10,450; +4 more |
| `instruction-promotion-gate` | NOT MEASURED | nothing has reached it | nothing published |
| `instruction-replayer` | FED BUT PRODUCES NOTHING | cost-estimate 382,609; fill-sequence 35,388; fillable-size 35,388; +1 more | nothing published |
| `intra-bar-fill-sequencer` | PRODUCES REAL OUTPUT | cost-estimate 382,708; historical-window 10,585 | fill-sequence 35,299 — work: ambiguous 35,299; bars_sequenced 35,299; ambiguous_fraction 1 |
| `live-vs-replay-reconciler` | NOT MEASURED | nothing has reached it | nothing published |
| `lookahead-auditor` | NOT MEASURED | nothing has reached it | nothing published |
| `walk-forward-splitter` | ONLY REFUSALS | historical-window 10,532 | nothing published — work: refused_gappy_windows 10,532 |


### Paper trading on live data (`paper-live-trading`) — 10 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `book-walk-fill-pricer` | PRODUCES REAL OUTPUT | order-book-snapshot 105,964; order-request 16 | fill-price-estimate 14 — work: estimates 14; complete_fills 11; partial_fills 3 |
| `live-switch-guard` | PRODUCES REAL OUTPUT | money-mode 3,273; closed-trade 2 | risk-limit 2,322 — work: days_traded 0 |
| `money-mode-reader` | NOT MEASURED | nothing has reached it | money-mode 36,018 |
| `order-destination-router` | FED BUT PRODUCES NOTHING | money-mode 3,285 | nothing published |
| `order-idempotency-stamper` | NOT MEASURED | nothing has reached it | nothing published |
| `order-latency-simulator` | PRODUCES REAL OUTPUT | money-mode 3,285; order-request 16 | delayed-order-request 32 — work: orders_released 16; longest_delay_seconds 0 |
| `paper-account-keeper` | SERVING ITS PURPOSE | the three built segments' real accounts | index-options/stock-options/cash-equity accounts present and correct. The retired `paper-account-futures` component holding BTCUSDT on binance-usdm was removed 2026-09-06 and did not return through a restart. |
| `paper-fill-simulator` | PRODUCES REAL OUTPUT | cost-estimate 382,492; market-data 109,464; feed-jump 5,436; +5 more | fill 26 — work: fees_charged 1,209; feed_jumps_cleared 817; resting 7; cancelled 3; +4 more |
| `paper-liquidation-simulator` | NOT MEASURED | nothing has reached it | nothing published |
| `stop-order-manager` | PRODUCES REAL OUTPUT | position 28,370; money-mode 3,285; stop-adjustment 3,141 | order-request 96 — work: replaced 14; restored_symbols 12; stops_resting 10; exits_withdrawn 2 |


### Bull bot (`bull-bot`) — 10 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `bull-conviction-calibrator` | PRODUCES REAL OUTPUT | market-regime 59,684; bull-raw-conviction 1,553; training-label 17 | bull-calibrated-conviction 6,212 — work: convictions_calibrated 1,553; passed_through_unfitted 1,553 |
| `bull-conviction-model` | PRODUCES REAL OUTPUT | kline-window 102,171; price-forecast 102,162; forecast-out-of-distribution-flag 102,104; +6 more | bull-raw-conviction 3,106 — work: challenger.observations 97,255; champion.observations 97,255; labels_trained_on 97,255; challenger.positives 48,127; +8 more |
| `bull-entry-timer` | PRODUCES REAL OUTPUT | symbol-price-frame 6,088; bull-side-candidate 1,701; bull-calibrated-conviction 1,553 | bull-entry-timing 3,276 — work: decisions 1,638; entered_now 1,638; detectors_with_an_entry_quality_record 2 |
| `bull-exit-plan-proposer` | PRODUCES REAL OUTPUT | symbol-profile 101,174; excursion-profile 99,152; symbol-price-frame 6,072; +4 more | bull-exit-plan 774 — work: plans_requested 1,681; plans_built 258; plans_from_the_live_range 258; detectors_with_a_horizon_profile 5 |
| `bull-feature-builder` | PRODUCES REAL OUTPUT | broker-open-interest 162,141; order-book-snapshot 105,968; symbol-profile 103,331; +3 more | bull-feature-vector 8,505 — work: vectors_built 1,701; book_snapshots_absent 374 |
| `bull-opinion-composer` | PRODUCES REAL OUTPUT | bull-feature-vector 1,701; bull-entry-timing 1,638; bull-calibrated-conviction 1,553; +1 more | directional-opinion 4,754 — work: opinions_composed 406 |
| `bull-outlier-rejector` | PRODUCES REAL OUTPUT | bull-feature-vector 1,701 | bull-feature-out-of-distribution-flag 1,701 — work: vectors_judged 612,291; flagged_out_of_distribution 149; checkpoints_written 68; features_with_a_learned_normal 14 |
| `bull-position-invalidation-watcher` | PRODUCES REAL OUTPUT | market-data 109,484; position 28,262; bull-feature-vector 1,701 | directional-opinion 111,257 — work: by_reason.past-the-horizon-it-was-given 8,693; checks 8,693; close_calls 8,693; positions_watched 1; +1 more |
| `bull-setup-filter` | PRODUCES REAL OUTPUT | entry-candidate 60,133; bull-setup-weight 286 | bull-side-candidate 5,103 — work: accepted 1,701; accepted_by_detector.mean-reversion-detector 1,223; accepted_by_detector.spread-reversion-detector 478; setup_weights_learned 2 |
| `bull-setup-weight-learner` | PRODUCES REAL OUTPUT | training-label 17 | bull-setup-weight 286 — work: weights_published 286; trades_learned_from 5; weights.spread-reversion-detector 1; weights.unknown 1; +1 more |


### Bear bot (`bear-bot`) — 10 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `bear-conviction-calibrator` | PRODUCES REAL OUTPUT | market-regime 59,645; bear-raw-conviction 56,927; training-label 17 | bear-calibrated-conviction 225,867 — work: convictions_calibrated 56,927; fell_back_to_the_overall_record 56,927; passed_through_unfitted 56,927 |
| `bear-conviction-model` | PRODUCES REAL OUTPUT | kline-window 102,091; price-forecast 102,090; forecast-out-of-distribution-flag 102,032; +6 more | bear-raw-conviction 113,854 — work: convictions_formed 56,927; challenger.observations 45,831; champion.observations 45,831; labels_trained_on 45,831; +6 more |
| `bear-entry-timer` | PRODUCES REAL OUTPUT | bear-side-candidate 58,445; bear-calibrated-conviction 56,927; symbol-price-frame 6,082 | bear-entry-timing 116,664 — work: decisions 58,332; entered_now 11,261; detectors_with_an_entry_quality_record 2 |
| `bear-exit-plan-proposer` | PRODUCES REAL OUTPUT | excursion-profile 94,576; symbol-profile 86,024; bear-side-candidate 56,046; +4 more | bear-exit-plan 60 — work: plans_requested 55,970; plans_built 20; detectors_with_a_horizon_profile 5 |
| `bear-feature-builder` | PRODUCES REAL OUTPUT | broker-open-interest 161,496; order-book-snapshot 105,540; symbol-profile 102,947; +3 more | bear-feature-vector 291,510 — work: vectors_built 58,302; book_snapshots_absent 76 |
| `bear-opinion-composer` | PRODUCES REAL OUTPUT | bear-entry-timing 58,332; bear-feature-vector 58,302; bear-calibrated-conviction 56,927; +1 more | directional-opinion 84,501 — work: opinions_composed 7,286 |
| `bear-outlier-rejector` | PRODUCES REAL OUTPUT | bear-feature-vector 58,302 | bear-feature-out-of-distribution-flag 58,302 — work: vectors_judged 2,113,027; flagged_out_of_distribution 1,389; features_with_a_learned_normal 15 |
| `bear-position-invalidation-watcher` | FED BUT PRODUCES NOTHING | market-data 109,512; bear-feature-vector 58,302; position 28,392 | nothing published |
| `bear-setup-filter` | PRODUCES REAL OUTPUT | entry-candidate 60,130; bear-setup-weight 301 | bear-side-candidate 173,257 — work: accepted 58,429; accepted_by_detector.volatility-gap-detector 58,205; accepted_by_detector.spread-reversion-detector 206; accepted_by_detector.mean-reversion-detector 18; +1 more |
| `bear-setup-weight-learner` | PRODUCES REAL OUTPUT | training-label 17 | bear-setup-weight 301 — work: weights_published 301; trades_learned_from 12; tail_ratios.spread-reversion-detector 1; weights.spread-reversion-detector 1 |


### Profit tailgating bot (`profit-tailgating-bot`) — 9 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `tail-copy-selector` | NOT MEASURED | nothing has reached it | nothing published |
| `tail-crowding-detector` | FED BUT PRODUCES NOTHING | broker-open-interest 162,141; order-book-snapshot 105,970; broker-subscribed-instrument-listing 1,969 | nothing published |
| `tail-follow-conviction-model` | FED BUT PRODUCES NOTHING | retrain-request 32; sample-weight 17; training-label 17; +1 more | nothing published |
| `tail-move-remaining-estimator` | FED BUT PRODUCES NOTHING | price-forecast 102,132; symbol-price-frame 6,069 | nothing published |
| `tail-mover-qualifier` | ONLY REFUSALS | symbol-profile 103,177; entry-candidate 60,105; symbol-price-frame 6,071 | nothing published — work: rejected_by_reason.one-print-is-not-a-move 57,278; rejected_by_reason.move-has-not-run-far-enough-to-be-established 2,825; rejected_by_reason.move-has-gone-further-than-this-symbol-normally-goes 1; rejected_by_reason.spread-costs-more-than-the-move-has-left 1 |
| `tail-opinion-composer` | NOT MEASURED | nothing has reached it | nothing published |
| `tail-setup-weight-learner` | NOT MEASURED | nothing has reached it | nothing published |
| `tail-trailing-exit-planner` | FED BUT PRODUCES NOTHING | symbol-profile 103,357; excursion-profile 100,313; position 28,392; +1 more | nothing published |
| `tail-winner-selector` | ONLY REFUSALS | market-data 109,484; position 28,392; peak-excursion 520 | nothing published — work: rejected_by_reason.the-pair-experiment-has-not-resolved 295,520 |


### Broker adapter (Indian markets) (`broker-adapter`) — 9 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `broker-account-funds-reader` | PRODUCES REAL OUTPUT | broker-token-standing 1,107 | broker-account-funds 74 — work: reads 37 |
| `broker-history-reader` | ONLY REFUSALS | broker-instrument-listing 63,531; broker-token-standing 1,110; market-session-state 19 | nothing published — work: skipped_because_the_market_is_open 3,866; instruments_awaiting_a_window 2,642; skipped_because_the_session_is_unknown 1 |
| `broker-instrument-catalogue-reader` | NOT MEASURED | nothing has reached it | broker-instrument-listing 508,090 — work: listings_restated 63,559; listings_per_second 57 |
| `broker-margin-quoter` | PRODUCES REAL OUTPUT | broker-market-data 165,422; symbol-universe 36,804; broker-token-standing 1,109 | broker-margin-requirement 5 — work: calls_made 10,379; quotes_read 123 |
| `broker-market-feed-reader` | PRODUCES REAL OUTPUT | broker-instrument-listing 63,439; symbol-universe 35,882; broker-token-standing 1,109 | broker-market-data 1,131,724; broker-open-interest 781,081; broker-option-greeks 617,385; +3 more — work: decoded_messages 6,200; subscribed_instruments 1,402; connected 1 |
| `broker-market-tape-writer` | PRODUCES REAL OUTPUT | broker-candle 263,590; broker-market-data 141,398; broker-order-book-snapshot 140,181; +2 more | nothing published — work: records_written 823,585; open_tapes 6,960 |
| `broker-price-level-sampler` | PRODUCES REAL OUTPUT | broker-market-data 166,545 | broker-price-frame 9,939 — work: updates_observed 159,769; frames_published 3,313 |
| `broker-token-refresh-scheduler` | NOT MEASURED | nothing has reached it | broker-token-standing 5,555 — work: has_token 1; is_valid 1 |
| `subscribed-instrument-listing-filter` | PRODUCES REAL OUTPUT | broker-instrument-listing 63,531; broker-subscription-state 1,087 | broker-subscribed-instrument-listing 25,597 — work: listings_restated 1,969; subscribed_instruments 1,402; listings_per_second 5 |


### Capital desk (`capital-desk`) — 8 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `allocation-conservation-checker` | PRODUCES REAL OUTPUT | capital-allotment 3,354; main-account-setting 1,100 | alert 2,543; allocation-headroom 2,538 — work: checks 2,546; alerts_raised 2,543 |
| `allocation-rebalance-proposer` | PRODUCES REAL OUTPUT | capital-utilisation 3,261; usdt-pnl-statement 2 | allocation-proposal 1,076 — work: rounds 1,079 |
| `capital-settings-change-recorder` | PRODUCES REAL OUTPUT | capital-allotment 3,366; leverage-ceiling 3,366; trade-capital-bounds 3,366; +1 more | journal-entry 145,015 — work: changes_recorded 14,581; by_setting.keys_not_reported 17; first_readings 17 |
| `capital-settings-validator` | PRODUCES REAL OUTPUT | instrument-choice 16,306; capital-allotment 3,366; leverage-ceiling 3,366; +2 more | capital-settings-verdict 3,303 — work: consistent 1,106; judgements 1,106 |
| `capital-utilisation-meter` | PRODUCES REAL OUTPUT | account-balance 11,715; capital-allotment 3,354 | capital-utilisation 6,522 — work: measurements 3,261; segments_measured 3 |
| `live-balance-divergence-watch` | FED BUT PRODUCES NOTHING | account-balance 11,715; capital-allotment 3,354; money-mode 3,270 | nothing published |
| `main-account-settings-reader` | NOT MEASURED | nothing has reached it | main-account-setting 6,620 — work: balance 150,000,000; reads 1,105; can_size_a_real_trade 1 |
| `paper-currency-converter` | FED BUT PRODUCES NOTHING | symbol-price-frame 6,053; main-account-setting 1,105 | nothing published |


### Portfolio and position state (`portfolio-state`) — 7 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `cost-basis-tracker` | PRODUCES REAL OUTPUT | fill 2 | cost-basis 26,544 — work: restored_symbols 12; checkpoint_restored 1; overshoots_too_small_to_reverse 1; residues_released_with_the_side 1 |
| `fill-reconciler` | PRODUCES REAL OUTPUT | fill 2 | position 453,232 — work: checks 28,392; symbols 26; fills_applied 2 |
| `fund-lock-ledger` | ONLY REFUSALS | account-balance 11,718; fill 2 | nothing published — work: releases_with_no_lock_to_match 2 |
| `liquidation-price-tracker` | NOT MEASURED | nothing has reached it | nothing published |
| `peak-excursion-tracker` | PRODUCES REAL OUTPUT | market-data 109,452; position 28,366; cost-basis 13,260 | peak-excursion 5,200 — work: prices_without_cost_basis 108,932; positions_closed 15,596; restored_symbols 13 |
| `position-close-detector` | PRODUCES REAL OUTPUT | position 28,366; peak-excursion 520; fill 2 | closed-trade 42 — work: restored_symbols 12; open_symbols 10; trades_closed 2; checkpoint_restored 1; +2 more |
| `usdt-pnl-accountant` | PRODUCES REAL OUTPUT | market-data 109,512; cost-basis 13,272; capital-allotment 3,369; +2 more | usdt-pnl-statement 8 — work: net_total_usdt 17,707; non_usdt_converted 2; statements 2; statements_without_capital 2 |


### Ledger and audit trail (`ledger`) — 6 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `control-recorder` | PRODUCES REAL OUTPUT | policy-decision 9,276; knowledge-snapshot 1 | journal-entry 92,769 — work: recorded 9,277; policy_decisions 9,276; knowledge_snapshots 1 |
| `funding-settlement-recorder` | NOT MEASURED | nothing has reached it | nothing published |
| `journal-integrity-checker` | PRODUCES REAL OUTPUT | journal-entry 100,891 | journal-gap 10 — work: entries_checked 100,891; checks 48,021; chain_breaks 5 |
| `learning-recorder` | PRODUCES REAL OUTPUT | decision-rationale 9,253; pnl-attribution 2 | journal-entry 92,550 — work: recorded 9,255; by_kind.decision-rationale 9,253; by_kind.pnl-attribution 2 |
| `position-recorder` | PRODUCES REAL OUTPUT | position 28,369; stop-adjustment 3,141; peak-excursion 520; +1 more | journal-entry 482 — work: recorded 69; opened 26; excursions 22; stop_adjustments 17; +2 more |
| `trade-lifecycle-recorder` | PRODUCES REAL OUTPUT | entry-candidate 60,146; trade-intent 16,404; order-request 16; +1 more | journal-entry 678,250 — work: recorded 67,825; stage_counts.entry-candidate 51,403; stage_counts.trade-intent 16,404; trades_started 41; +3 more |


### Segment bot (bull, bear, profit tailgating) (`segment-bot`) — 1 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `instrument-selector` | PRODUCES REAL OUTPUT | broker-market-data 157,259; broker-option-greeks 153,112; liquidity-grade 101,282; +8 more | instrument-choice 48,918 — work: chosen 14,882; chosen_by_kind.option 14,882; priced_from_a_quote 5,415; price_staleness.symbols_measured 590; +2 more |

