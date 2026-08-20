#!/usr/bin/env python3
"""Open the eight thinking blocks further, and give the market data feed rotation (RL-049).

Proposed by Claude 2026-08-20 at the user's request ("think of other features inside ...
intelligence, learning loop, hypothesis, knowledge, online research, hardware resource
governor, skills, ai brain"). Every part is origin "proposed" except the feed rotation
parts, which answer RL-049. Rationale: docs/proposals/thinking-blocks-and-feed-expansion.md.

Idempotent: re-running changes nothing once applied.
"""
import json, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
d = json.loads(REG.read_text())
TODAY = "2026-08-20"
P = "docs/proposals/thinking-blocks-and-feed-expansion.md"
feats = {f["id"]: f for f in d["features"]}
types = {t["id"]: t for t in d["data_types"]}

def T(tid, name, desc):
    if tid not in types:
        t = {"id": tid, "name": name, "description": desc}; d["data_types"].append(t); types[tid] = t

def A(pid, name, role, cat, consumes, produces, evidence=P, origin="proposed"):
    produces = list(produces) + (["part-health"] if "part-health" not in produces else [])
    f = {"id": pid, "name": name, "role": role, "category": cat, "consumes": list(consumes),
         "produces": produces, "switchable": True, "off_releases_resources": True,
         "states": ["off", "on"], "origin": origin, "evidence": evidence}
    if pid in feats: feats[pid].update(f)
    else: d["features"].append(f); feats[pid] = f

def C(pid, *tids):
    for t in tids:
        if t not in feats[pid]["consumes"]: feats[pid]["consumes"].append(t)

# ------------------------------------------------------------- data types
for tid, name, desc in [
 ("refutation-verdict","refutation verdict","Whether a claimed edge survived the refutation battery: placebo treatment, random common cause, data-subset, bootstrap. One failure is a hard disqualifier."),
 ("trial-ledger","trial ledger","Every hypothesis, retune or parameter trial ever run on a family, counted, so a reported edge can be deflated by how many tries produced it."),
 ("forgetting-report","forgetting report","How a current model scores on frozen past episodes it once got right, so learning something new is caught overwriting something old."),
 ("coverage-report","coverage report","Realised abstention coverage against the coverage the conformal gate promised, per bot, per window."),
 ("counterfactual-outcome","counterfactual outcome","What the opinion the arbiter did not take would have earned, replayed against the same prices."),
 ("cross-segment-signal","cross-segment signal","A reading from one segment restated for another: a spot whale inflow for the futures sweeper, a basis stretch for the spot bots."),
 ("competence-map","competence map","What the system currently knows it is good at, per setup class, regime plus segment, with how sure it is, from its own measured record."),
 ("edge-half-life","edge half-life","How fast each instruction's measured edge is decaying, stated as a half-life in trades plus in hours."),
 ("training-label","training label","The outcome a closed trade teaches: which barrier hit first, within what horizon, at what return net of cost."),
 ("sample-weight","sample weight","How much one labelled episode should count in training, lower where its holding window overlaps others."),
 ("retrain-request","retrain request","A learned part should refit now, with the reason: drift, forgetting, schedule, or enough new labels."),
 ("model-version","model version","One trained model with its data window, feature list, gate verdicts plus trial count, addressable by id."),
 ("champion-choice","champion choice","Which model version a learned part should serve, chosen by the gate, never by recency."),
 ("learning-reward","learning reward","What a trade earned restated as the reward a learner optimises: net of cost, risk-adjusted, horizon-discounted."),
 ("feature-attribution","feature attribution","How much each feature moved each decision, per model version."),
 ("bot-regret","bot regret","Cumulative regret of the arbiter's choices against the best single bot in hindsight, per regime."),
 ("candidate-formula","candidate formula","A machine-found expression over features that predicted an outcome in sample, awaiting falsification."),
 ("novelty-score","novelty score","How different a hypothesis is from every instruction ever issued, so a rediscovery is not retested as new."),
 ("falsification-criterion","falsification criterion","Written before the test: the result that would refute this hypothesis."),
 ("required-sample-size","required sample size","How many trades an instruction needs before its edge is distinguishable from zero at the stated confidence."),
 ("mutated-hypothesis","mutated hypothesis","A near-miss instruction with one threshold, time-frame or filter varied."),
 ("hypothesis-regime-tag","hypothesis regime tag","The regime a hypothesis was born in, carried with it so it can be retired when that regime ends."),
 ("knowledge-link","knowledge link","An edge between two knowledge items that share a symbol, regime, setup or source."),
 ("fact-provenance","fact provenance","Where a fact came from, when, with what confidence, plus how fast that confidence decays."),
 ("knowledge-contradiction","knowledge contradiction","Two knowledge items that cannot both hold."),
 ("episode-embedding","episode embedding","A vector for an episode's observed condition, never its outcome, for similarity recall."),
 ("regime-memory","regime memory","What worked plus what failed in each regime, summarised from episodes."),
 ("knowledge-snapshot","knowledge snapshot","A hashed snapshot of the whole knowledge state at one moment, so a past decision can be replayed against what was known then."),
 ("fact-confidence","fact confidence","A fact's current confidence after decay along its forgetting curve, restored when re-confirmed."),
 ("verified-record","verified record","A tracked trader's claimed record checked against what chain or venue data actually shows."),
 ("copy-latency","copy latency","How stale an external position is by the time this system sees it, measured."),
 ("venue-announcement","venue announcement","A listing, delisting, maintenance window or rule change a venue published."),
 ("onchain-flow","on-chain flow","Stablecoin mints plus burns, exchange reserve changes, bridge flows: aggregate money moving toward or away from trading."),
 ("options-flow","options flow","Large option block trades: strike, expiry, side, size."),
 ("io-pressure","I/O pressure","How saturated disk plus network are right now, separate from CPU plus RAM."),
 ("accelerator-slot","accelerator slot","A GPU or other accelerator allocation granted to one part for one window, if the machine has one."),
 ("memory-forecast","memory forecast","Minutes until memory runs out at the current growth rate, per part."),
 ("restart-budget","restart budget","How many restarts a part has left in its window before it is held off as crash-looping."),
 ("duty-cycle","duty cycle","The hours each heavy part is allowed to run, so retraining lands in quiet hours."),
 ("flap-report","flap report","A part that has been switched on plus off repeatedly in a short window."),
 ("resource-reservation","resource reservation","A guaranteed floor of CPU plus RAM for a part that must never be starved: the risk gate, the halt path."),
 ("skill-backtest","skill backtest","How a distilled skill's rules would have scored on past episodes, before it is indexed."),
 ("skill-version","skill version","One distillation of a skill with its diff from the previous one."),
 ("skill-gap","skill gap","A question a brain asked for which no skill was found."),
 ("skill-provenance","skill provenance","Source, date, licence plus distiller version of a skill."),
 ("conflict-ruling","conflict ruling","What to do when bull plus bear both hold high conviction on one symbol: straddle, pick, or abstain, per regime plus maturity."),
 ("bot-weight","bot weight","How much the arbiter trusts each bot right now, sampled from its regret record."),
 ("size-hint","size hint","A conviction restated as a capped fraction of capital, for the sizer to cut further, never raise."),
 ("premortem-note","premortem note","Written before the intent leaves: how this trade most plausibly fails."),
 ("counter-argument","counter-argument","The strongest case against an intent, written by a part whose job is to disagree."),
 ("timed-intent","timed intent","A trade intent released at the moment its entry timing named, or dropped at the give-up time."),
 ("venue-standing","venue standing","Whether a venue is currently safe to ask: rate headroom, recent 418/429s, withheld streams, measured latency."),
 ("key-standing","key standing","Whether one API key on one venue is currently safe to use for private calls."),
 ("consolidated-price","consolidated price","One price per symbol from every venue that quotes it, with each venue's weight plus staleness."),
 ("stream-plan","stream plan","Which streams go on which connection to which venue, within measured per-connection plus per-IP limits."),
 ("feed-coverage","feed coverage","Per symbol, which venues currently supply which data, and where nothing does."),
]: T(tid, name, desc)

# ------------------------------------------------------------- intelligence
A("causal-refutation-battery","Causal refutation battery","test a claimed edge with placebo treatment, random common cause, data-subset plus bootstrap refutations","intelligence",["trade-episode","instruction-scorecard","feature-attribution"],["refutation-verdict"])
A("trial-count-accountant","Trial count accountant","count every hypothesis, retune plus parameter trial per family so an edge is deflated by how many tries produced it","intelligence",["opportunity-instruction","mutated-hypothesis","model-version"],["trial-ledger"])
A("forgetting-auditor","Forgetting auditor","replay frozen past episodes against the current model to catch new learning overwriting old","intelligence",["model-version","recalled-episode","trade-episode"],["forgetting-report"])
A("abstention-coverage-auditor","Abstention coverage auditor","measure realised abstention coverage against what the conformal gate promised","intelligence",["directional-opinion","trade-episode"],["coverage-report"])
A("counterfactual-replayer","Counterfactual replayer","replay the opinion the arbiter did not take against the same prices","intelligence",["directional-opinion","trade-intent","market-data","trade-episode"],["counterfactual-outcome"])
A("cross-segment-signal-bridge","Cross-segment signal bridge","restate a reading from one segment for the sweepers of the other two","intelligence",["whale-transfer","market-data","funding-forecast","position"],["cross-segment-signal"])
A("self-model-reporter","Self-model reporter","state what the system currently knows it is good at, per setup, regime plus segment, from its own record","intelligence",["bot-scorecard","instruction-scorecard","coverage-report","forgetting-report"],["competence-map"])
A("edge-decay-tracker","Edge decay tracker","measure how fast each instruction's edge is decaying, as a half-life","intelligence",["instruction-scorecard","trade-episode"],["edge-half-life"])
C("edge-graduation-gate","refutation-verdict","trial-ledger","coverage-report")
C("instruction-promotion-gate","refutation-verdict","trial-ledger","required-sample-size","falsification-criterion")
C("decision-quality-critic","counterfactual-outcome","premortem-note")
C("bot-scorekeeper","counterfactual-outcome","learning-reward")
C("universal-symbol-sweeper","cross-segment-signal","venue-announcement")
C("autonomy-policy-engine","competence-map")
C("opinion-arbiter","competence-map","coverage-report","conflict-ruling","bot-weight","counter-argument","regime-memory")
C("instruction-retirer","edge-half-life","hypothesis-regime-tag","falsification-criterion")
C("hypothesis-ranker","edge-half-life","novelty-score","required-sample-size")
C("model-drift-monitor","forgetting-report")

# ------------------------------------------------------------- learning loop
A("label-builder","Label builder","turn each closed trade into a training label: which barrier hit first, within what horizon, at what net return","learning-loop",["closed-trade","peak-excursion","cost-estimate"],["training-label"])
A("sample-weight-assigner","Sample weight assigner","weight each labelled episode down where its holding window overlaps others","learning-loop",["training-label","trade-episode"],["sample-weight"])
A("retrain-scheduler","Retrain scheduler","ask a learned part to refit when drift, forgetting, schedule or enough new labels say so","learning-loop",["model-drift-alert","forgetting-report","training-label","duty-cycle"],["retrain-request"])
A("model-registry","Model registry","keep every trained model version with its window, features, gate verdicts plus trial count","learning-loop",["retrain-request","refutation-verdict","trial-ledger"],["model-version"])
A("champion-challenger-gate","Champion-challenger gate","choose which model version a learned part serves, by its gate verdicts, never by recency","learning-loop",["model-version","refutation-verdict","trial-ledger","forgetting-report"],["champion-choice"])
A("reward-shaper","Reward shaper","restate what a trade earned as the reward a learner optimises: net of cost, risk-adjusted, horizon-discounted","learning-loop",["closed-trade","usdt-pnl-statement","peak-excursion"],["learning-reward"])
A("feature-attribution-tracker","Feature attribution tracker","record how much each feature moved each decision, per model version","learning-loop",["model-version","feature-vector","raw-conviction"],["feature-attribution"])
A("regret-tracker","Regret tracker","track the arbiter's cumulative regret against the best single bot in hindsight, per regime","learning-loop",["trade-intent","counterfactual-outcome","trade-episode","market-regime"],["bot-regret"])
C("feature-reliability-scorer","feature-attribution")
C("intent-explainer","feature-attribution")
for side in ("bull","bear"):
    C(f"{side}-conviction-model","training-label","sample-weight","retrain-request","champion-choice","learning-reward")
C("tail-follow-conviction-model","training-label","sample-weight","retrain-request","champion-choice","learning-reward")
C("kronos-finetuner","retrain-request","sample-weight")
C("kronos-forecaster","champion-choice")

# ------------------------------------------------------------- hypothesis
A("symbolic-hypothesis-miner","Symbolic hypothesis miner","search expressions over features by genetic programming for ones that predicted an outcome in sample","hypothesis",["feature-reliability","trade-episode","kline-window","training-label"],["candidate-formula"])
A("hypothesis-deduplicator","Hypothesis deduplicator","score a hypothesis by how different it is from every instruction ever issued","hypothesis",["candidate-formula","mutated-hypothesis","instruction-history"],["novelty-score"])
A("hypothesis-falsifier","Hypothesis falsifier","write, before any test, the result that would refute the hypothesis","hypothesis",["candidate-formula","inverted-hypothesis","novel-idea"],["falsification-criterion"])
A("power-estimator","Power estimator","compute how many trades an instruction needs before its edge is distinguishable from zero","hypothesis",["expectancy-breakdown","candidate-formula"],["required-sample-size"])
A("hypothesis-mutator","Hypothesis mutator","vary one threshold, time-frame or filter of a near-miss instruction","hypothesis",["instruction-scorecard","retired-instruction","instruction-history"],["mutated-hypothesis"])
A("hypothesis-regime-tagger","Hypothesis regime tagger","tag each hypothesis with the regime it was born in","hypothesis",["candidate-formula","market-regime"],["hypothesis-regime-tag"])
C("instruction-writer","candidate-formula","mutated-hypothesis","falsification-criterion","knowledge-link","regime-memory")

# ------------------------------------------------------------- knowledge
A("knowledge-graph-linker","Knowledge graph linker","link facts, episodes, instructions plus skills that share a symbol, regime, setup or source","knowledge",["semantic-fact","recalled-episode","instruction-history","available-skill"],["knowledge-link"])
A("fact-provenance-tracker","Fact provenance tracker","keep where each fact came from, when, at what confidence, plus its decay rate","knowledge",["semantic-fact","research-finding","source-document"],["fact-provenance"])
A("contradiction-detector","Contradiction detector","find two knowledge items that cannot both hold","knowledge",["semantic-fact","playbook-rule","knowledge-link"],["knowledge-contradiction"])
A("episode-embedder","Episode embedder","embed an episode's observed condition, never its outcome, for similarity recall","knowledge",["trade-episode"],["episode-embedding"])
A("regime-memory-store","Regime memory store","summarise what worked plus what failed in each regime from the episodes","knowledge",["trade-episode","market-regime","instruction-scorecard"],["regime-memory"])
A("knowledge-snapshot-versioner","Knowledge snapshot versioner","hash the whole knowledge state at one moment so a past decision can be replayed against what was known then","knowledge",["semantic-fact","playbook-rule","instruction-history","available-skill"],["knowledge-snapshot"])
A("forgetting-curve-scheduler","Forgetting curve scheduler","decay each fact's confidence along its curve, restoring it when re-confirmed","knowledge",["fact-provenance","semantic-fact"],["fact-confidence"])
C("episodic-trade-store","episode-embedding")
C("semantic-fact-store","fact-confidence","knowledge-contradiction")
C("knowledge-pruner","fact-provenance","knowledge-contradiction","fact-confidence")
C("idea-generator","knowledge-link","regime-memory")
C("control-recorder","knowledge-snapshot")
C("brain-self-reflector","knowledge-snapshot")

# ------------------------------------------------------------- online research
A("trader-record-verifier","Trader record verifier","check a leaderboard's claimed record against what chain or venue data actually shows","online-research",["tracked-trader","external-position"],["verified-record"])
A("copy-latency-estimator","Copy latency estimator","measure how stale an external position is by the time this system sees it","online-research",["external-position","market-data"],["copy-latency"])
A("exchange-announcement-reader","Exchange announcement reader","read listings, delistings, maintenance windows plus rule changes each venue publishes","online-research",["symbol-universe"],["venue-announcement"])
A("onchain-flow-aggregator","On-chain flow aggregator","aggregate stablecoin mints, burns, exchange reserves plus bridge flows into money moving toward or away from trading","online-research",[],["onchain-flow"])
A("options-flow-reader","Options flow reader","read large option block trades: strike, expiry, side, size","online-research",["symbol-universe"],["options-flow"])
A("arxiv-feed-reader","arXiv feed reader","read new quantitative-finance papers into source documents","online-research",["skill-gap"],["source-document"])
A("github-strategy-miner","GitHub strategy miner","read public open-source strategy code for rules worth testing","online-research",["skill-gap","web-idea"],["research-finding"])
C("copy-worthiness-scorer","verified-record","copy-latency")
C("tail-copy-selector","copy-latency")
C("event-risk-limiter","venue-announcement")
C("whale-flow-detector","onchain-flow")
C("volatility-gap-detector","options-flow")
C("market-event-reader","venue-announcement")

# ------------------------------------------------------------- resource governor
A("io-pressure-meter","I/O pressure meter","measure disk plus network saturation apart from CPU plus RAM","resource-governor",[],["io-pressure"])
A("accelerator-scheduler","Accelerator scheduler","grant GPU or other accelerator slots to parts for a window, when the machine has one","resource-governor",["hardware-capacity","part-priority"],["accelerator-slot"])
A("memory-pressure-forecaster","Memory pressure forecaster","forecast minutes to memory exhaustion from each part's growth rate","resource-governor",["part-resource-usage","hardware-capacity"],["memory-forecast"])
A("part-restart-budgeter","Part restart budgeter","hold off a part that is crash-looping once its restart budget for the window is spent","resource-governor",["restart-request","part-fault"],["restart-budget"])
A("duty-cycle-planner","Duty cycle planner","plan the hours each heavy part may run so retraining lands in quiet hours","resource-governor",["part-resource-usage","market-data"],["duty-cycle"])
A("switch-oscillation-damper","Switch oscillation damper","name a part being switched on plus off repeatedly in a short window","resource-governor",["switch-record"],["flap-report"])
A("resource-reservation-ledger","Resource reservation ledger","hold a guaranteed CPU plus RAM floor for parts that must never be starved","resource-governor",["part-priority","hardware-capacity"],["resource-reservation"])
C("switching-planner","io-pressure","memory-forecast","restart-budget","duty-cycle","flap-report","resource-reservation")
C("kronos-forecaster","accelerator-slot")
C("kronos-finetuner","accelerator-slot")

# ------------------------------------------------------------- skills
A("skill-tester","Skill tester","score a distilled skill's rules on past episodes before it is indexed","skills",["skill","recalled-episode","trade-episode"],["skill-backtest"])
A("skill-composer","Skill composer","combine two skills that answer adjacent questions into one compound procedure","skills",["skill","knowledge-link"],["skill"])
A("skill-version-keeper","Skill version keeper","keep every distillation of a skill with its diff from the one before","skills",["skill"],["skill-version"])
A("skill-gap-finder","Skill gap finder","record every question a brain asked for which no skill was found","skills",["loaded-skill-section","llm-request"],["skill-gap"])
A("video-lecture-reader","Video lecture reader","read a trading lecture's transcript plus frames into a source document","skills",["web-idea"],["source-document"])
A("skill-provenance-stamper","Skill provenance stamper","stamp each skill with source, date, licence plus distiller version","skills",["skill","source-document"],["skill-provenance"])
C("skill-index","skill-backtest","skill-provenance","skill-version")
C("skill-scorer","skill-version")
C("open-web-reader","skill-gap")
C("book-and-paper-fetcher","skill-gap")

# ------------------------------------------------------------- ai brain
A("opinion-conflict-resolver","Opinion conflict resolver","rule what to do when bull plus bear both hold high conviction on one symbol: straddle, pick, or abstain","ai-brain",["directional-opinion","market-regime","bot-maturity","regime-memory"],["conflict-ruling"])
A("bot-weight-sampler","Bot weight sampler","sample how much to trust each bot right now from its regret record","ai-brain",["bot-regret","bot-scorecard","market-regime"],["bot-weight"])
A("size-hint-writer","Size hint writer","restate a conviction as a capped fraction of capital the sizer may only cut","ai-brain",["trade-intent","calibrated-conviction","bot-scorecard"],["size-hint"])
A("premortem-writer","Premortem writer","write, before the intent leaves, how this trade most plausibly fails","ai-brain",["trade-intent","directional-opinion","llm-response","verified-snapshot"],["premortem-note","llm-request"])
A("devils-advocate","Devil's advocate","write the strongest case against an intent, so an unanswered objection downgrades it","ai-brain",["trade-intent","directional-opinion","llm-response","verified-snapshot","regime-memory"],["counter-argument","llm-request"])
A("intent-timing-gate","Intent timing gate","release an intent at the moment its entry timing named, or drop it at the give-up time","ai-brain",["trade-intent","entry-timing","market-data"],["timed-intent"])
C("position-sizer","size-hint","timed-intent")
C("instrument-selector","timed-intent")
C("brain-self-reflector","premortem-note","counter-argument")
C("decision-quality-critic","counter-argument")

# ------------------------------------------------------------- market data feed (RL-049)
R = "RL-049; trading-system src/ops/rate_budget.py, src/capture/venues/, ~/research/binance-fstream-connection-limits.md, ~/research/binance-withheld-streams.md"
A("ban-signal-detector","Ban signal detector","read 418s, 429s, retry-after headers plus withheld streams into each venue's current standing","market-data-feed",["market-data","feed-gap","venue-rate-budget"],["venue-standing"],R,"user")
A("venue-pool-rotator","Venue pool rotator","spread each request class across every venue that carries the symbol, by standing plus rate headroom, so no one venue is leaned on","market-data-feed",["symbol-universe","venue-standing","venue-rate-budget","stream-plan"],["market-data","order-book-snapshot"],R,"user")
A("api-key-pool-rotator","API key pool rotator","rotate private calls across every key a venue allows, by each key's standing","market-data-feed",["venue-standing","key-standing"],["key-standing"],R,"user")
A("cross-venue-price-consolidator","Cross-venue price consolidator","combine every venue's quote for a symbol into one price with each venue's weight plus staleness","market-data-feed",["market-data","venue-standing"],["consolidated-price"],R,"user")
A("stream-budget-planner","Stream budget planner","assign streams to connections plus venues within the measured per-connection plus per-IP limits","market-data-feed",["symbol-universe","venue-standing","hardware-capacity"],["stream-plan"],R,"user")
A("feed-coverage-auditor","Feed coverage auditor","state per symbol which venues currently supply which data, including where none does","market-data-feed",["market-data","order-book-snapshot","symbol-universe","venue-standing"],["feed-coverage"],R,"user")
C("ccxt-venue-reader","stream-plan","venue-standing")
C("venue-trade-stream-reader","stream-plan","venue-standing")
C("order-book-reader","stream-plan","venue-standing")
C("venue-balance-reader","key-standing")
C("venue-position-reader","key-standing")
C("ccxt-order-router","key-standing")
C("venue-rate-budgeter","venue-standing")
C("paper-fill-simulator","consolidated-price")
C("universal-symbol-sweeper","consolidated-price")
C("market-anomaly-detector","consolidated-price","feed-coverage")
C("alert-raiser","feed-coverage")
C("board-snapshot-builder","feed-coverage","competence-map")

# ------------------------------------------------------------- recompute contracts
for c in d["categories"]:
    parts = [f for f in d["features"] if f["category"] == c["id"]]
    if not parts: continue
    c["consumes"] = sorted({x for f in parts for x in f["consumes"]})
    c["produces"] = sorted({x for f in parts for x in f["produces"]})
    c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}
d["_proposal_2026-08-20_thinking_blocks"] = {
    "what": "The eight thinking blocks opened further (intelligence, learning loop, hypothesis, knowledge, online research, resource governor, skills, AI brain), plus feed rotation for RL-049.",
    "origin": "thinking-block parts proposed by Claude at the user's request; feed rotation parts are RL-049",
    "applied_by": "dashboard/blueprint_edits/apply_2026-08-20_thinking_blocks_and_feed.py",
    "rationale": P,
}
REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(len(d["features"]), "features,", len(d["categories"]), "categories,", len(d["data_types"]), "data types")
