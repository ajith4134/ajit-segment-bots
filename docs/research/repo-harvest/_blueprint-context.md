# Blueprint context for repo readers

Rules: every part = one responsibility, names DATA TYPES it consumes/produces, never other parts (T-4). Off releases resources (T-3). No part switches another (T-2). Grow by adding parts (T-6).

## 22 foundation blocks and the parts already inside each

### paper-live-trading — Paper trading on live data [per-segment]
- `money-mode-reader`: read the user's paper-or-live setting for this segment  (in:  | out: money-mode)
- `order-destination-router`: address a sized order to the paper book or the live venue, by the money mode  (in: sized-order, money-mode | out: order-request)
- `stop-order-manager`: turn a stop adjustment into an order at whichever destination the money mode names  (in: stop-adjustment, position, money-mode | out: order-request)
- `paper-fill-simulator`: fill a paper order at the live price with the measured cost applied  (in: order-request, market-data, cost-estimate, money-mode | out: fill)
- `paper-account-keeper`: keep the paper balance as paper fills land against the allotted capital  (in: fill, capital-allotment, money-mode | out: account-balance)
- `live-switch-guard`: refuse live money while the segment's bots have not graduated on paper (RL-005)  (in: money-mode, bot-maturity | out: risk-limit)

### opportunity-scanner — Universal opportunity scanner [per-segment]
- `regime-classifier`: classify the current regime with a Hurst proxy  (in: market-data | out: market-regime)
- `mean-reversion-detector`: flag a symbol whose price has deviated far enough from its mean  (in: market-data, market-regime, playbook-rule | out: entry-candidate)
- `volatility-gap-detector`: flag a symbol where forecast volatility disagrees with the option market  (in: volatility-forecast, implied-vol-surface, playbook-rule | out: entry-candidate)
- `cointegration-pair-finder`: find pairs of symbols whose spread has held together  (in: market-data, market-regime | out: cointegrated-pair)
- `spread-reversion-detector`: flag a cointegrated spread that has stretched far enough to snap back  (in: cointegrated-pair, market-data | out: entry-candidate)
- `watch-condition-compiler`: compile each proven instruction into a condition testable on every symbol  (in: proven-instruction, retired-instruction | out: watch-condition)
- `universal-symbol-sweeper`: test every symbol in the universe against every watch condition on every tick  (in: market-data, symbol-universe, watch-condition, liquidity-grade, position | out: entry-candidate)
- `liquidity-grader`: grade how tradeable each symbol is at the bot's size  (in: order-book-snapshot, market-data | out: liquidity-grade)
- `momentum-burst-detector`: spot a sudden jump that the playbook says tends to pull back  (in: market-data, symbol-profile, playbook-rule | out: entry-candidate)
- `funding-skew-detector`: spot a funding rate stretched far enough that the crowded side tends to unwind  (in: market-data, funding-forecast, playbook-rule | out: entry-candidate)
- `whale-flow-detector`: spot an exchange inflow or outflow large enough to precede a move  (in: whale-transfer, market-data | out: entry-candidate)
- `liquidation-cascade-detector`: spot price approaching a liquidation cluster dense enough to cascade  (in: liquidation-map, market-data | out: entry-candidate)
- `sentiment-shift-detector`: spot crowd sentiment turning faster than price has  (in: sentiment-reading, market-data, playbook-rule | out: entry-candidate)

### segment-bot — Segment bot (bull, bear, profit tailgating) [per-segment]
- `bull-bot`: issue a long-side opinion on an entry candidate  (in: entry-candidate, market-data, bot-scorecard, price-forecast | out: directional-opinion)
- `bear-bot`: issue a short-side opinion on an entry candidate  (in: entry-candidate, market-data, bot-scorecard, price-forecast | out: directional-opinion)
- `profit-tailgater`: take a position in something already proven to be working  (in: position, external-position, market-data, copy-score | out: directional-opinion)
- `exit-opinion-bot`: issue a flat opinion on an open position once its reason to exist has gone  (in: position, market-data, price-forecast, regime-break-alert | out: directional-opinion)
- `instrument-selector`: choose the contract, strike, expiry or pair that expresses a trade intent in this segment  (in: trade-intent, market-data, implied-vol-surface, liquidity-grade | out: instrument-choice)

### intelligence — Intelligence [global]
- `cross-segment-exposure-watch`: watch every segment at once to report total capital at risk  (in: position | out: exposure-view)
- `decision-quality-critic`: score whether a decision was sound separately from whether it won  (in: directional-opinion, trade-episode, llm-response, decision-rationale | out: decision-quality-score, llm-request)
- `regime-break-detector`: detect when the market has changed enough that what was learned no longer applies  (in: market-data, journal-entry, market-event | out: regime-break-alert)
- `open-web-reader`: read the open web for ideas nobody pointed it at  (in:  | out: web-idea)
- `idea-generator`: propose strategies nobody here has tried  (in: web-idea, trade-episode, bot-scorecard, llm-response, sentiment-reading, instruction-history | out: novel-idea, llm-request)
- `cross-segment-lesson-bridge`: restate a lesson decoded in one segment so the other two can read it  (in: decoded-trade-instruction, loss-cause | out: cross-segment-lesson)
- `correlation-cluster-mapper`: group symbols that move together across all segments  (in: market-data | out: correlation-cluster)
- `market-event-reader`: read scheduled or breaking events that move markets  (in:  | out: market-event)
- `market-anomaly-detector`: flag a print or feed that does not look like a market  (in: market-data, feed-gap | out: market-anomaly)

### learning-loop — Learning loop [per-segment]
- `bot-scorekeeper`: keep each bot's record of right calls against wrong ones  (in: directional-opinion, trade-episode | out: bot-scorecard)
- `feature-reliability-scorer`: score which features preceded profitable trades rather than losing ones  (in: trade-episode, vol-feature-set | out: feature-reliability)
- `edge-graduation-gate`: graduate a bot out of exploration when its own numbers clear the bar  (in: bot-scorecard, decision-quality-score | out: bot-maturity)
- `instruction-performance-tracker`: keep score of what each live instruction has earned  (in: trade-episode, opportunity-instruction | out: instruction-scorecard)
- `forecast-trust-learner`: learn how much each forecast deserves from the trades it preceded  (in: forecast-accuracy, trade-episode | out: forecast-trust)
- `slippage-learner`: measure intended against filled price by symbol, size, hour  (in: fill, sized-order | out: slippage-profile)
- `exit-timing-learner`: learn how much of the peak the exits keep, per instruction  (in: exit-quality, trade-episode | out: instruction-scorecard)

### hypothesis — Hypothesis [per-segment]
- `expectancy-decomposer`: decompose expected value into separately measurable factors  (in: trade-episode, forecast-accuracy | out: expectancy-breakdown)
- `instruction-writer`: write the opportunity instruction the scanner will watch for  (in: decoded-trade-instruction, expectancy-breakdown, recalled-episode, semantic-fact, loaded-skill-section, strategy-gap, feature-reliability, regime-break-alert, novel-idea, cross-segment-lesson, inverted-hypothesis, hypothesis-priority, winner-pattern, reflection-note | out: opportunity-instruction)
- `loss-inverter`: turn a losing trade's information into the hypothesis for the opposite profitable entry  (in: decoded-trade-instruction, loss-cause | out: inverted-hypothesis)
- `hypothesis-ranker`: rank hypotheses by measured expectancy so the best are written first  (in: expectancy-breakdown, instruction-scorecard, exit-quality | out: hypothesis-priority)
- `instruction-retirer`: withdraw an instruction that stopped earning or whose regime broke  (in: instruction-scorecard, regime-break-alert, live-vs-replay-gap | out: retired-instruction)

### knowledge — Knowledge [per-segment]
- `semantic-fact-store`: hold durable per-symbol facts under a closed-set key  (in: journal-entry | out: semantic-fact)
- `episodic-trade-store`: keep every closed trade as an immutable episode keyed on the condition observed  (in: trade-episode | out: recalled-episode)
- `procedural-playbook`: hold the capped always-injected rule set that never gets searched  (in: opportunity-instruction, retired-instruction | out: playbook-rule)
- `symbol-profile-store`: hold what is normal for each symbol, updated as the market teaches it  (in: market-data, semantic-fact | out: symbol-profile)
- `instruction-archive`: keep every instruction ever issued with what it was derived from  (in: opportunity-instruction, retired-instruction | out: instruction-history)
- `knowledge-pruner`: name the facts, skills, instructions that measurably stopped helping  (in: skill-usefulness, semantic-fact, instruction-scorecard | out: stale-knowledge)

### prediction — Prediction [per-segment]
- `kline-window-builder`: shape live market data into the fixed candlestick window the model expects  (in: market-data | out: kline-window)
- `kronos-forecaster`: run the Kronos model over a candlestick window to produce a price forecast  (in: kline-window, finetuned-model, model-choice | out: price-forecast)
- `kronos-finetuner`: adapt the Kronos tokenizer plus predictor to the assets actually traded here  (in: kline-window, model-drift-alert | out: finetuned-model)
- `forecast-scorer`: score each past forecast against what the market actually did  (in: price-forecast, market-data | out: forecast-accuracy)
- `kronos-size-selector`: choose which Kronos model size to run based on measured forecast accuracy  (in: forecast-accuracy | out: model-choice)
- `implied-vol-reader`: read the option surface into implied volatilities at the strikes that matter  (in: market-data | out: implied-vol-surface)
- `volatility-feature-builder`: compute the realised-volatility feature set from a candlestick window  (in: kline-window, implied-vol-surface | out: vol-feature-set)
- `realised-vol-regressor`: estimate forward realised volatility from the feature set  (in: vol-feature-set | out: volatility-forecast)
- `order-flow-state-encoder`: label each second of trade flow as one of fifteen states  (in: market-data, order-book-snapshot | out: order-flow-state)
- `flow-entropy-meter`: measure how structured the recent order flow is  (in: order-flow-state | out: flow-entropy)
- `entropy-magnitude-forecaster`: forecast how far price moves next from how structured the flow is  (in: flow-entropy, vol-feature-set | out: volatility-forecast)
- `funding-rate-forecaster`: forecast next period's funding rate  (in: market-data | out: funding-forecast)
- `liquidation-cluster-mapper`: map the price levels where leveraged positions would be liquidated  (in: market-data, order-book-snapshot | out: liquidation-map)
- `forecast-ensembler`: combine price plus volatility forecasts by their earned trust  (in: price-forecast, volatility-forecast, forecast-trust | out: ensemble-forecast)
- `model-drift-monitor`: raise an alert when a forecaster's accuracy decays past its floor  (in: forecast-accuracy | out: model-drift-alert)

### online-research — Online research [per-segment]
- `leaderboard-reader`: read the public copy-trading leaderboards each venue publishes  (in:  | out: tracked-trader)
- `onchain-position-reader`: read a tracked trader's on-chain positions from public explorers  (in: tracked-trader | out: external-position)
- `strategy-decoder`: infer the strategy behind a tracked trader's position history  (in: external-position, llm-response | out: research-finding, llm-request)
- `edge-comparator`: compare a decoded strategy against what this bot already does  (in: research-finding, trade-episode | out: strategy-gap)
- `copy-worthiness-scorer`: score how worth copying a tracked trader's position is, here, now  (in: external-position, tracked-trader, trade-episode | out: copy-score)
- `whale-transfer-reader`: read large on-chain transfers into or out of exchanges  (in:  | out: whale-transfer)
- `social-sentiment-reader`: read how the crowd is talking about each symbol, source counted  (in: symbol-universe | out: sentiment-reading)

### llm-services — LLM services [per-segment]
- `llm-request-router`: route each llm-request to the cheapest route that still has allowance  (in: llm-request, llm-quota-state, llm-spend-state, llm-model-choice, llm-backpressure | out: subscription-llm-request, paid-llm-request, local-llm-request)
- `subscription-session-caller`: answer a subscription-routed request through the Claude subscription session  (in: subscription-llm-request | out: llm-response, llm-call-record)
- `metered-api-caller`: answer a paid-routed request through a cloud model API key  (in: paid-llm-request | out: llm-response, llm-call-record)
- `subscription-quota-watch`: report how much subscription allowance is left before the next reset  (in: llm-call-record | out: llm-quota-state)
- `paid-spend-ledger`: record what the metered route spends against its ceiling  (in: llm-call-record | out: llm-spend-state)
- `llm-model-picker`: name which model answers a given class of request  (in: llm-request, llm-call-record | out: llm-model-choice)
- `llm-response-cache`: return the stored answer for a repeated llm-request  (in: llm-request, llm-response | out: llm-response)
- `local-model-caller`: answer a request with a model running on this machine  (in: local-llm-request | out: llm-response, llm-call-record)
- `llm-backpressure-gauge`: set how hard to throttle asking as quota, spend or runway run low  (in: llm-quota-state, llm-spend-state, survival-tier | out: llm-backpressure)

### resource-governor — Hardware resource governor [global]
- `hardware-scanner`: measure what the machine has, what is free  (in:  | out: hardware-capacity)
- `part-appetite-meter`: measure what each running part actually costs in CPU plus RAM  (in: part-health | out: part-resource-usage)
- `hog-detector`: name a part taking far more than its share while another waits  (in: part-resource-usage, hardware-capacity | out: hog-report)
- `part-priority-reader`: read the user's order of which parts matter most under contention  (in:  | out: part-priority)
- `switching-planner`: decide which parts turn on or off next from capacity, priority, every pending request  (in: hardware-capacity, part-resource-usage, hog-report, part-priority, restart-request, replacement-plan, conservation-plan, admitted-part | out: switch-plan)
- `gate-actuator`: flip the gates a switch plan names, one at a time, recording each  (in: switch-plan | out: switch-record)
- `off-state-verifier`: confirm a part turned off actually released its CPU plus RAM (T-3)  (in: switch-record, part-resource-usage | out: part-fault)

### market-data-feed — Market data feed [per-segment]
- `ccxt-venue-reader`: read live candles from every venue through one unified interface  (in:  | out: market-data)
- `venue-trade-stream-reader`: stream live trades as they print  (in:  | out: market-data)
- `order-book-reader`: snapshot bids plus asks to depth  (in:  | out: order-book-snapshot)
- `symbol-catalogue-reader`: list every symbol the venue offers in this segment  (in:  | out: symbol-universe)
- `feed-gap-detector`: flag a symbol whose data went missing or stale  (in: market-data | out: feed-gap)

### risk-capital-allocation — Risk and capital allocation [per-segment]
- `profit-lock`: raise the stop as an open position gains so a pullback keeps some profit  (in: position, market-data | out: stop-adjustment)
- `capital-allotment-reader`: read the paper capital the user assigned this segment (RL-040)  (in:  | out: capital-allotment)
- `leverage-selector`: choose leverage per trade from volatility plus funding (RL-041)  (in: trade-intent, volatility-forecast, funding-forecast | out: leverage-choice)
- `stop-target-placer`: place the initial stop away from the crowd's liquidation levels  (in: trade-intent, volatility-forecast, liquidation-map, symbol-profile | out: stop-target-plan)
- `exposure-limiter`: cap what may be risked from open positions, total exposure, correlated bets  (in: position, exposure-view, correlation-cluster | out: risk-limit)
- `drawdown-breaker`: cut the limit to zero when the segment's drawdown breaches its floor  (in: account-balance, closed-trade | out: risk-limit)
- `halt-enforcer`: turn a trading halt, a refused policy or a human override into a zero limit  (in: trading-halt, policy-decision, human-override | out: risk-limit)
- `event-risk-limiter`: shrink the limit around events plus anomalies  (in: market-event, market-anomaly | out: risk-limit)
- `position-sizer`: size one trade from intent, instrument, leverage, stop plus the smallest limit, or refuse  (in: trade-intent, instrument-choice, leverage-choice, stop-target-plan, account-balance, risk-limit, slippage-profile | out: sized-order)

### ledger — Ledger and audit trail [per-segment]
- `trade-lifecycle-recorder`: journal every step from candidate to fill  (in: entry-candidate, trade-intent, sized-order, order-request, fill | out: journal-entry)
- `position-recorder`: journal positions as they open, change, close  (in: position, closed-trade, peak-excursion | out: journal-entry)
- `learning-recorder`: journal what was learned, researched, forecast, scored  (in: research-finding, forecast-accuracy, ablation-scorecard, skill-usefulness, opportunity-instruction, decision-rationale | out: journal-entry)
- `control-recorder`: journal every gate flip, policy ruling, self-modification  (in: switch-record, policy-decision, modification-record | out: journal-entry)
- `journal-integrity-checker`: detect a break in the journal's sequence or hash chain  (in: journal-entry | out: journal-gap)

### portfolio-state — Portfolio and position state [per-segment]
- `fill-reconciler`: build the held position from fills, checked against what the venue reports  (in: fill, venue-position-report | out: position)
- `position-close-detector`: emit a closed trade when a position goes flat, with its peak excursion attached  (in: position, fill, peak-excursion | out: closed-trade)
- `peak-excursion-tracker`: track the best plus worst unrealised points each open position reaches (RL-042)  (in: position, market-data | out: peak-excursion)
- `usdt-pnl-accountant`: state each trade's profit or loss in USDT against the capital it used (RL-028)  (in: closed-trade, fill, market-data, capital-allotment | out: usdt-pnl-statement)

### observability — Observability [global]
- `ablation-harness`: measure what breaks when each part is switched off  (in: part-health | out: ablation-scorecard)
- `heartbeat-collector`: gather every part's latest health into one table with its age  (in: part-health | out: heartbeat-table)
- `probe-runner`: run each measurement, keep its output with the command that ran it  (in: heartbeat-table, journal-gap | out: probe-result)
- `alert-raiser`: raise what a human should look at now, proof attached  (in: part-fault, trading-halt, journal-gap, market-anomaly, hog-report | out: alert)
- `board-snapshot-builder`: render the whole system's measured state, every tile traced to a probe (RL-012)  (in: heartbeat-table, probe-result, usdt-pnl-statement, alert, switch-record, account-balance | out: board-snapshot)
- `board-publisher`: push the snapshot to the public link the user can open  (in: board-snapshot | out: board-link)
- `stale-board-watch`: raise an alert when the published board is older than its source  (in: board-link, board-snapshot | out: alert)

### execution-venue-adapter — Execution and venue adapter [per-segment]
- `ccxt-order-router`: place each order on its venue through one unified interface  (in: order-request, venue-rate-budget | out: fill)
- `venue-balance-reader`: fetch the live balance from the venue  (in:  | out: account-balance)
- `venue-position-reader`: fetch what the venue says is held  (in:  | out: venue-position-report)
- `order-state-poller`: follow each open order until it fills, part-fills or is cancelled  (in: order-request | out: fill)
- `order-reject-classifier`: classify why the venue refused an order  (in: fill | out: order-reject-reason)
- `order-resubmitter`: resubmit an order whose rejection was transient, within the rate budget  (in: order-reject-reason, order-request, venue-rate-budget | out: order-request)
- `venue-rate-budgeter`: count what the venue still allows this window  (in: order-request | out: venue-rate-budget)

### ai-brain — AI brain [per-segment]
- `opinion-arbiter`: weigh the bots' opinions into one trade intent  (in: directional-opinion, market-regime, bot-maturity, regime-break-alert, forecast-bias | out: trade-intent)
- `exploration-pair-opener`: open both sides while a bot is still exploring, so the pair's outcome teaches  (in: directional-opinion, bot-maturity, market-regime | out: trade-intent)
- `forecast-bias-weigher`: decide how far to lean toward the ensemble forecast this time  (in: ensemble-forecast, forecast-trust | out: forecast-bias)
- `intent-explainer`: write why an intent was chosen before its outcome is known  (in: trade-intent, directional-opinion, llm-response | out: decision-rationale, llm-request)
- `brain-self-reflector`: reread each rationale against what happened, state what it concluded  (in: decision-rationale, trade-episode, llm-response | out: reflection-note, llm-request)

### closed-trade-decoding — Closed trade decoding [per-segment]
- `trade-episode-encoder`: encode each closed trade keyed on the market condition that was observed  (in: closed-trade, journal-entry | out: trade-episode)
- `lesson-extractor`: turn a trade episode into an instruction the hypothesis feature can work on  (in: trade-episode | out: decoded-trade-instruction)
- `loss-cause-classifier`: classify why a trade lost: entry, exit, size, regime or execution  (in: trade-episode, peak-excursion | out: loss-cause)
- `winner-pattern-miner`: find conditions that recur across winning trades  (in: trade-episode | out: winner-pattern)
- `exit-quality-scorer`: score how much of the peak a closed trade kept  (in: trade-episode, peak-excursion | out: exit-quality)

### skills — Skills [per-segment]
- `source-ingester`: pull a source into text whatever form it arrived in  (in: research-finding, skill-refresh-request | out: source-document)
- `skill-distiller`: distil a source document into a skill with its trigger described  (in: source-document, llm-response | out: skill, llm-request)
- `skill-index`: hold every skill so one can be found by what it is for  (in: skill, skill-conflict, stale-knowledge | out: available-skill)
- `skill-loader`: load only the section of a skill that the current question needs  (in: available-skill | out: loaded-skill-section)
- `skill-scorer`: score whether a loaded skill changed the outcome  (in: loaded-skill-section, trade-episode | out: skill-usefulness)
- `community-chat-reader`: read community chats into source documents  (in:  | out: source-document)
- `book-and-paper-fetcher`: fetch the books or papers the web reader pointed at  (in: web-idea | out: source-document)
- `skill-conflict-detector`: find two skills giving opposite advice for one question  (in: skill | out: skill-conflict)
- `skill-refresher`: ask for a skill to be re-distilled when its usefulness falls or it goes stale  (in: skill-usefulness, stale-knowledge | out: skill-refresh-request)

### autonomous — Autonomous operation [global]
- `unattended-run-warden`: keep the whole system running with nobody watching  (in: part-health, part-fault | out: restart-request)
- `venue-outage-rider`: carry the bot through an exchange outage without human help  (in: market-data, part-health, feed-gap | out: outage-state)
- `capability-gap-finder`: find what the circuit cannot yet do  (in: bot-scorecard, research-finding, part-health, folded-circuit-map | out: capability-gap)
- `part-author`: write a new part that fills a named gap  (in: capability-gap, llm-response, folded-circuit-map | out: proposed-part, llm-request)
- `part-admission-gate`: admit a proposed part only after it passes every contract  (in: proposed-part, upstream-change, policy-decision | out: admitted-part)
- `autonomy-boundary`: name which decisions the bot may take without a human  (in: bot-maturity, modification-record, survival-tier | out: autonomy-envelope)
- `trading-halt-decider`: decide when the whole bot stops trading  (in: autonomy-envelope, exposure-view, outage-state, regime-break-alert, survival-tier, human-override, market-anomaly | out: trading-halt)
- `failing-part-detector`: spot a part that has stopped behaving  (in: part-health | out: part-fault)
- `part-replacement-planner`: choose the replacement for a faulted part  (in: part-fault, admitted-part | out: replacement-plan)
- `survival-tier-monitor`: grade how much runway the bot has left before it must conserve  (in: llm-quota-state, llm-spend-state, part-health | out: survival-tier)
- `conservation-planner`: name what the bot sheds at each level of scarcity  (in: survival-tier | out: conservation-plan)
- `autonomy-policy-engine`: rule on each act the bot proposes to take by itself  (in: autonomy-envelope, trade-intent, proposed-part | out: policy-decision)
- `self-modification-journal`: record every change the bot makes to itself  (in: admitted-part, policy-decision | out: modification-record)
- `no-progress-detector`: spot the bot repeating itself without making progress  (in: part-health, journal-entry | out: part-fault)
- `upstream-improvement-watch`: notice when a better version of the bot's own code exists  (in: research-finding, part-health | out: upstream-change)
- `folded-circuit-view`: compress the whole circuit into a view small enough to reason over  (in: part-health | out: folded-circuit-map)
- `human-override-reader`: read the user's direct halt, resume or cap, which outranks the bot  (in:  | out: human-override)

### backtesting — Backtesting [per-segment]
- `historical-bar-store`: keep the recorded history a replay reads from  (in: market-data | out: historical-window)
- `walk-forward-splitter`: split history into training windows that never overlap the test window  (in: historical-window | out: walk-forward-split)
- `execution-cost-model`: charge each simulated fill what it would really have cost  (in: market-data, slippage-profile | out: cost-estimate)
- `instruction-replayer`: replay one opportunity instruction over recorded history  (in: opportunity-instruction, walk-forward-split, cost-estimate | out: backtest-run)
- `lookahead-auditor`: refuse a replay that used information it could not have had  (in: backtest-run | out: backtest-verdict)
- `backtest-scorer`: score what a replay earned after costs  (in: backtest-run | out: backtest-result)
- `instruction-promotion-gate`: let an instruction reach the scanner only after it has survived replay  (in: backtest-result, backtest-verdict | out: proven-instruction)
- `live-vs-replay-reconciler`: measure how far paper results drift from what replay promised  (in: backtest-result, trade-episode | out: live-vs-replay-gap)

## Existing data types

- `market-data`: Live prices, trades and book for every symbol, normalised across venues.
- `entry-candidate`: A symbol and the moment it should enter trading. Carries nothing about paper or live.
- `trade-intent`: A direction and conviction for a candidate, from the bull, bear and profit-tailgating bots.
- `sized-order`: A trade intent with size, leverage and stops applied — or a refusal, which is also an answer.
- `order-request`: An order addressed to a destination, after the paper/live switch has chosen one.
- `fill`: What actually executed, at what price, including partials and rejects.
- `position`: What is held right now, per segment, reconciled against the venue.
- `journal-entry`: An immutable record of something that happened, with the time it happened.
- `research-finding`: A decoded strategy or portfolio from a public high-profit trader.
- `part-health`: A part's own state and health, emitted by the part rather than inferred about it.
- `opportunity-instruction`: What the scanner should watch for and what it implies. The user's words: opportunities are instructions or feeds derived from the hypothesis feature, not rules built into the scanner.
- `closed-trade`: A finished trade with its outcome, profit or loss, and everything that led to it.
- `decoded-trade-instruction`: What a closed trade taught, drawn from winners and losers alike, expressed as an instruction for the hypothesis feature to work on.
- `llm-request`: A question a block asks a language model. The asking block names this data type, never the LLM block.
- `llm-response`: The model's answer, returned to whichever block asked.
- `kline-window`: A fixed-length run of OHLCV candles, the shape the Kronos tokenizer expects. Bounded by the model's context limit.
- `price-forecast`: Predicted candles for the periods ahead, with the model version that produced them.
- `forecast-accuracy`: How a past forecast actually scored against what the market did. A forecast nobody scores is an assertion.
- `finetuned-model`: Kronos weights adapted to the assets actually traded here, tokenizer included.
- `model-choice`: Which Kronos size to run: mini, small or base.
- `vol-feature-set`: The ten realised- and implied-volatility features from the reel, computed for one symbol.
- `volatility-forecast`: Forward realised volatility estimated from the feature set. Distinct from a price forecast: it says how much, not which way.
- `implied-vol-surface`: ATM implied vol, the 30-60 term slope and the 25-delta put skew. Only exists where an options venue does.
- `market-regime`: Trending, mean-reverting, or mixed, from a Hurst proxy. Mixed is a real answer meaning stand down.
- `trade-episode`: A closed trade keyed on the market condition that was observed, never on the outcome. Immutable.
- `recalled-episode`: Past episodes matching a condition seen now.
- `semantic-fact`: A durable fact about one symbol, filed under a closed-set key so a contradiction overwrites by construction.
- `playbook-rule`: An always-injected, never-searched, capped rule. Behaviour only, never diagnosis.
- `expectancy-breakdown`: Expected value split into separately measurable factors, so the weakest one is visible rather than the total merely being poor.
- `ablation-scorecard`: What measurably broke when each part was switched off, one row per part.
- `source-document`: A source pulled into text, whatever form it arrived in: book, paper, chat log, page.
- `skill`: A source distilled into structure -- frameworks, decision rules, anti-patterns -- with the trigger that says when it applies. Never a summary.
- `available-skill`: An indexed skill, findable by what it is for rather than by its title.
- `loaded-skill-section`: Only the part of a skill the current question needs. This is what lets the library grow without a token cost.
- `skill-usefulness`: Whether loading a skill changed the outcome. A skill nobody scores is an assertion.
- `tracked-trader`: A high-performing trader worth following, with the venue or wallet that identifies them.
- `external-position`: What a tracked trader currently holds, from public explorers or venue leaderboards.
- `strategy-gap`: What a decoded strategy does that this bot does not. The 'step up beyond it' half of C-09.
- `directional-opinion`: One bot's direction plus conviction on a candidate. An opinion, never an order.
- `bot-scorecard`: A bot's own record of right calls against wrong ones. What it learns from.
- `bot-maturity`: Whether a bot has graduated out of exploration on its own measured edge.
- `feature-reliability`: Which features preceded profitable trades rather than losing ones.
- `stop-adjustment`: A protective stop moved up as a position gains, so a pullback keeps some profit rather than giving it all back.
- `exposure-view`: Total capital at risk across every segment at once. No per-segment block can compute this; that is the point of it.
- `decision-quality-score`: Whether the reasoning was sound, scored separately from whether the trade won. A win on luck and a loss on a correct call are different events.
- `regime-break-alert`: The market has changed enough that what was learned no longer applies.
- `web-idea`: Something found by looking rather than by being pointed at it.
- `novel-idea`: A strategy nobody here has tried, proposed rather than derived from what already happened.
- `subscription-llm-request`: An llm-request routed to the Claude subscription session, because subscription allowance remains.
- `paid-llm-request`: An llm-request routed to a metered cloud model API key, because the subscription allowance is spent.
- `llm-call-record`: What one answered call actually cost: model, tokens, latency, which route answered it.
- `llm-quota-state`: How much subscription allowance is left before the next reset.
- `llm-spend-state`: How much money the metered route has spent against its ceiling.
- `llm-model-choice`: Which language model answers a given class of request. Distinct from model-choice, which sizes a Kronos forecaster.
- `restart-request`: A statement that something has stopped that should be running. It asks; only the resource governor acts on it.
- `outage-state`: Whether a venue is reachable right now, how long it has been unreachable, what is stale because of it.
- `capability-gap`: Something the circuit cannot yet do, named precisely enough that a part could be written to fill it.
- `proposed-part`: A new part the bot wrote for itself: role, consumes, produces, states. Not yet in the circuit.
- `admitted-part`: A proposed part that passed every contract check. Only this may enter the circuit, which is what keeps self-authoring from becoming self-corrupting.
- `autonomy-envelope`: Which decisions the bot may take without a human, and at what size. The boundary is data in one place, never an assumption spread across parts.
- `trading-halt`: A decision that the whole bot stops trading. Risk allocation owns the kill switch; this is what asks it to be pulled.
- `part-fault`: A part has stopped behaving: dead, stalled, or reporting health that its output contradicts.
- `replacement-plan`: Which admitted part takes over from a faulted one. A plan, not an act -- the governor performs the swap.
- `survival-tier`: How much runway is left, graded: full, conserving, critical. A graded scarcity reading, not a part state -- states stay off/on per T-5.
- `conservation-plan`: What the bot sheds at a given level of scarcity: which work stops, which model steps down, how much the poll slows.
- `policy-decision`: The ruling on one act the bot proposed to take by itself: allow, hold, or refuse, with the rule that decided it and the reason.
- `modification-record`: One change the bot made to itself: what changed, the diff, whether it can be undone. Append-only.
- `upstream-change`: A newer version of the bot's own code that exists but is not running here yet.
- `folded-circuit-map`: The whole circuit compressed to a readable size: groups folded to one box each, opened only where a question needs the detail.
- `order-flow-state`: One second of trade flow labelled as one of fifteen states: the sign of the price change crossed with the volume quintile.
- `flow-entropy`: How structured the recent order flow is, on a normalised scale. Low means informed traders are leaving a footprint. Carries no direction, by mathematical necessity.
- `cointegrated-pair`: Two symbols whose spread A - beta*B has tested stationary, carrying the hedge ratio, the reversion speed and the measured half-life.
- `historical-window`: A bounded run of recorded market history for one symbol, at the resolution a replay needs.
- `walk-forward-split`: Training and test windows that never overlap, in time order, with every threshold frozen after training.
- `cost-estimate`: What a simulated fill would really have cost: spread crossed, slippage, fees, at that moment on that venue.
- `backtest-run`: One replay of one instruction over one split: every simulated entry, exit and fill it produced.
- `backtest-verdict`: Whether a replay can be trusted at all -- look-ahead, overlapping windows, survivorship, or a clean bill.
- `backtest-result`: What a replay actually earned after costs, per fold, with the concentration of the profit across days.
- `proven-instruction`: An opportunity instruction that survived replay. The only kind the scanner is given.
- `money-mode`: Paper or live, per segment, set by the user. The only place the money question is answered; the scanner never sees it.
- `capital-allotment`: How much paper capital the user has assigned to this segment out of the total portfolio. Editable by the user, journalled on change (RL-040).
- `account-balance`: Free plus used balance for the segment right now, paper or live, in the quote currency.
- `watch-condition`: A proven instruction compiled into a condition the sweeper can test against every symbol on every tick.
- `symbol-universe`: Every symbol the segment can trade right now, as listed by the venue, refreshed as listings change.
- `liquidity-grade`: How tradeable a symbol is at the size the bot trades: spread, depth, turnover.
- `instrument-choice`: The segment's own answer to a trade intent: which contract, strike, expiry or pair actually expresses it.
- `cross-segment-lesson`: A lesson decoded in one segment, restated so another segment can read it. Reported, never enforced.
- `correlation-cluster`: Symbols that move together, across segments, so two positions that look different can be seen as one bet.
- `market-event`: A scheduled or breaking event that moves markets: token unlock, listing, macro print, exchange notice.
- `market-anomaly`: A print, gap or feed that does not look like a market: flash move, stale tick, outlier.
- `instruction-scorecard`: What each opportunity instruction has actually earned since it went live, per segment.
- `forecast-trust`: How much weight a forecast has earned from its measured accuracy on trades, not on candles.
- `slippage-profile`: Measured difference between intended price against filled price, by symbol, size, hour.
- `inverted-hypothesis`: A losing trade's information turned around: what the opposite entry would have needed to be true.
- `hypothesis-priority`: Which hypotheses are worth writing into instructions first, ranked by measured expectancy.
- `retired-instruction`: An instruction withdrawn because it stopped earning or its regime broke. Kept, never deleted.
- `symbol-profile`: What is normal for one symbol: typical range, spread, volume by hour, how it reacts to its own jumps.
- `instruction-history`: Every instruction ever issued, retired or live, with what it was derived from.
- `stale-knowledge`: A fact, skill or instruction that measurably stopped helping, named so it can be pruned rather than rot.
- `funding-forecast`: Where the funding rate is headed next period, so a short that pays funding is priced before it is opened.
- `liquidation-map`: Price levels where leveraged positions would be liquidated, from open interest, so a stop is not placed where the crowd's is.
- `ensemble-forecast`: Price plus volatility forecasts combined by their earned trust into one view ahead.
- `model-drift-alert`: A forecast model's accuracy has decayed past its floor; retrain it or stand it down.
- `copy-score`: How worth copying a tracked trader's current position is, given their verified record plus this segment's own history.
- `whale-transfer`: A large on-chain transfer into or out of an exchange, which often precedes a move.
- `sentiment-reading`: How the crowd is talking about a symbol right now, graded, with the source counted.
- `local-llm-request`: An llm-request routed to a model running on this machine, because no subscription or paid route is usable.
- `llm-backpressure`: How hard to throttle asking: when quota or spend runs low, low-value questions wait.
- `hardware-capacity`: What the machine has, what is free right now: CPU, RAM, disk, measured.
- `part-resource-usage`: What each running part actually costs in CPU plus RAM, measured at runtime, not declared.
- `hog-report`: A part using far more than its share while another waits.
- `part-priority`: Which parts matter most when there is not enough machine for all of them. The user's order.
- `switch-plan`: Which parts to turn on or off next, with the reason, before any gate is touched.
- `switch-record`: One gate flipped: which part, on or off, when, by which plan. The control plane's own journal.
- `order-book-snapshot`: Bids plus asks to depth at one moment, for one symbol.
- `feed-gap`: A missing or stale run in market data: which symbol, how long, whether it is still open.
- `leverage-choice`: The leverage for one trade, chosen per trade from volatility plus funding, never a fixed setting (RL-041).
- `stop-target-plan`: Where the initial stop plus take-profit sit for one trade, before it is sized.
- `risk-limit`: The most the segment may risk right now, from every brake at once; the sizer takes the smallest.
- `usdt-pnl-statement`: Profit or loss in USDT for one trade against the capital it used, converted at the live rate recorded per fill (RL-028, RL-029).
- `peak-excursion`: The best plus worst unrealised points a position reached while open (RL-042).
- `venue-position-report`: What the venue says is held, fetched rather than computed, to reconcile against.
- `journal-gap`: A break in the journal: missing sequence, broken hash, or a recorder that stopped.
- `heartbeat-table`: Every part's latest health in one table, with how old each entry is.
- `board-snapshot`: One rendering of the whole system's measured state, timestamped, every tile traceable to a probe (RL-012).
- `alert`: Something a human should look at now, with the proof attached.
- `probe-result`: The output of one measurement that ran, with the command that ran it.
- `order-reject-reason`: Why the venue refused an order, classified: balance, rate limit, price band, size step, outage.
- `venue-rate-budget`: How many requests the venue still allows this window.
- `decision-rationale`: Why the brain chose this intent, in words, written before the outcome is known.
- `reflection-note`: What the brain concluded on rereading its own rationale against what happened.
- `forecast-bias`: How far to lean the arbiter toward a forecast this time, given its trust.
- `loss-cause`: Why a trade lost, classified: entry, exit, size, regime, execution.
- `winner-pattern`: A condition that recurs across winning trades, stated so an instruction can test it.
- `exit-quality`: How much of the peak a trade kept when it closed.
- `skill-conflict`: Two skills that give opposite advice for the same question.
- `skill-refresh-request`: A skill that should be re-distilled because its usefulness fell or its source moved.
- `human-override`: The user's direct instruction: halt, resume, or cap, which outranks everything the bot decides (RL-038).
- `live-vs-replay-gap`: How far paper results drift from what the backtest promised for the same instruction.
- `board-link`: The public URL the board was pushed to, with the snapshot time it carries.
