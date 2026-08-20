#!/usr/bin/env python3
"""Open closed-trade decoding further. Proposed by Claude 2026-08-20 at the user's request.
Rationale: docs/proposals/closed-trade-decoding-expansion.md. Idempotent."""
import json, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
d = json.loads(REG.read_text()); TODAY = "2026-08-20"
P = "docs/proposals/closed-trade-decoding-expansion.md"
feats = {f["id"]: f for f in d["features"]}; types = {t["id"]: t for t in d["data_types"]}
def T(tid, name, desc):
    if tid not in types:
        t = {"id": tid, "name": name, "description": desc}; d["data_types"].append(t); types[tid] = t
def A(pid, name, role, consumes, produces, cat="closed-trade-decoding"):
    produces = list(produces) + ["part-health"]
    f = {"id": pid, "name": name, "role": role, "category": cat, "consumes": list(consumes), "produces": produces,
         "switchable": True, "off_releases_resources": True, "states": ["off", "on"], "origin": "proposed", "evidence": P}
    if pid in feats: feats[pid].update(f)
    else: d["features"].append(f); feats[pid] = f
def C(pid, *tids):
    for t in tids:
        if t not in feats[pid]["consumes"]: feats[pid]["consumes"].append(t)

for tid, name, desc in [
 ("pnl-attribution","P&L attribution","One closed trade's result split into what direction, timing, size, fees, slippage plus funding each contributed."),
 ("entry-quality","entry quality","How close the entry was to the best price available in the window after the signal, in basis points plus bars."),
 ("exit-counterfactual","exit counterfactual","What the same trade would have earned under each other exit rail: earlier, later, trailing, time-stop."),
 ("horizon-profile","horizon profile","How return net of cost evolves with holding time for a setup class, so the best horizon is measured, not chosen."),
 ("trade-cluster","trade cluster","Closed trades that were effectively one bet: opened together on symbols that moved together."),
 ("outcome-significance","outcome significance","Whether a trade's result is distinguishable from noise given the symbol's volatility over the hold."),
 ("near-miss-episode","near-miss episode","A candidate that was raised but not traded, with what the market then did, so abstentions teach too."),
 ("regime-transition-flag","regime transition flag","Whether the regime changed between a trade's entry plus its exit."),
 ("shortfall-breakdown","shortfall breakdown","Implementation shortfall split into delay, spread, impact plus opportunity cost."),
 ("trade-narrative","trade narrative","The trade told in plain words from its journal: why entered, what happened, why exited."),
 ("sequence-pattern","sequence pattern","A pattern across consecutive trades: losses after losses, time-of-day clusters, size creep after wins."),
 ("stop-audit","stop audit","Whether a stop was hit by noise before price reached the target: too tight, too wide, or right."),
 ("excursion-profile","excursion profile","The distribution of best plus worst unrealised points per setup class, from every closed trade."),
 ("pair-verdict","pair verdict","For an exploration pair on one symbol: which side was right, by how much, and what distinguished the setup."),
 ("replay-mismatch","replay mismatch","A closed trade whose replay from its own journal does not reproduce the ledger's P&L."),
]: T(tid, name, desc)

A("pnl-attributor","P&L attributor","split one closed trade's result into direction, timing, size, fees, slippage plus funding",["closed-trade","fill","funding-settlement","cost-estimate","peak-excursion"],["pnl-attribution"])
A("entry-quality-scorer","Entry quality scorer","score how close the entry was to the best price in the window after the signal",["closed-trade","market-data","journal-entry"],["entry-quality"])
A("exit-counterfactual-replayer","Exit counterfactual replayer","replay the same trade under every other exit rail",["closed-trade","market-data","exit-plan"],["exit-counterfactual"])
A("holding-horizon-profiler","Holding horizon profiler","measure how net return evolves with holding time per setup class",["trade-episode","exit-counterfactual"],["horizon-profile"])
A("trade-cluster-detector","Trade cluster detector","group closed trades that were effectively one bet",["closed-trade","correlation-cluster"],["trade-cluster"])
A("luck-skill-separator","Luck-skill separator","test whether a trade's result is distinguishable from noise given the symbol's volatility",["closed-trade","volatility-forecast","symbol-profile"],["outcome-significance"])
A("near-miss-recorder","Near-miss recorder","keep every candidate raised but not traded, with what the market then did",["entry-candidate","trade-intent","directional-opinion","market-data"],["near-miss-episode"])
A("regime-transition-tagger","Regime transition tagger","flag a trade whose regime changed between entry plus exit",["closed-trade","market-regime","regime-break-alert"],["regime-transition-flag"])
A("shortfall-decomposer","Shortfall decomposer","split implementation shortfall into delay, spread, impact plus opportunity cost",["closed-trade","fill","sized-order","order-book-snapshot","market-data"],["shortfall-breakdown"])
A("trade-narrative-writer","Trade narrative writer","tell the trade in plain words from its journal",["trade-episode","journal-entry","decision-rationale","llm-response"],["trade-narrative","llm-request"])
A("sequence-pattern-miner","Sequence pattern miner","find patterns across consecutive trades: streak behaviour, time-of-day clusters, size creep",["trade-episode","closed-trade"],["sequence-pattern"])
A("stop-placement-auditor","Stop placement auditor","judge whether each stop was hit by noise before the target: too tight, too wide, or right",["closed-trade","peak-excursion","stop-target-plan","market-data"],["stop-audit"])
A("excursion-profiler","Excursion profiler","build the distribution of best plus worst unrealised points per setup class",["peak-excursion","trade-episode"],["excursion-profile"])
A("exploration-pair-decoder","Exploration pair decoder","decode an exploration pair: which side was right, by how much, what distinguished the setup",["closed-trade","trade-episode","directional-opinion"],["pair-verdict"])
A("trade-replay-verifier","Trade replay verifier","replay each closed trade from its own journal to confirm it reproduces the ledger's P&L",["closed-trade","journal-entry","fill"],["replay-mismatch"])

C("trade-episode-encoder","pnl-attribution","near-miss-episode","regime-transition-flag","entry-quality","outcome-significance","trade-cluster")
C("lesson-extractor","trade-narrative","pnl-attribution","stop-audit","sequence-pattern")
C("loss-cause-classifier","pnl-attribution","regime-transition-flag","stop-audit","shortfall-breakdown")
C("winner-pattern-miner","sequence-pattern","outcome-significance")
C("expectancy-decomposer","pnl-attribution","horizon-profile")
C("reward-shaper","pnl-attribution","outcome-significance")
C("exit-timing-learner","exit-counterfactual")
C("bot-scorekeeper","trade-cluster","outcome-significance","pair-verdict")
C("edge-graduation-gate","pair-verdict","trade-cluster")
C("decision-quality-critic","outcome-significance","trade-narrative","near-miss-episode")
C("abstention-coverage-auditor","near-miss-episode")
C("hypothesis-mutator","near-miss-episode")
C("regime-memory-store","regime-transition-flag")
C("slippage-learner","shortfall-breakdown")
C("execution-cost-model","shortfall-breakdown")
C("stop-target-placer","stop-audit","excursion-profile")
C("profit-lock","excursion-profile")
for s in ("bull","bear"):
    C(f"{s}-exit-plan-proposer","excursion-profile","horizon-profile","stop-audit")
    C(f"{s}-entry-timer","entry-quality")
C("tail-trailing-exit-planner","exit-counterfactual","excursion-profile")
C("tail-winner-selector","pair-verdict")
C("event-risk-limiter","sequence-pattern")
C("exposure-limiter","trade-cluster")
C("trial-count-accountant","trade-cluster")
C("brain-self-reflector","trade-narrative")
C("alert-raiser","replay-mismatch")
C("journal-integrity-checker","replay-mismatch")
C("instruction-writer","horizon-profile")

for c in d["categories"]:
    parts = [f for f in d["features"] if f["category"] == c["id"]]
    if not parts: continue
    c["consumes"] = sorted({x for f in parts for x in f["consumes"]})
    c["produces"] = sorted({x for f in parts for x in f["produces"]})
    c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}
d["_proposal_2026-08-20_closed_trade_decoding"] = {"what": "Closed-trade decoding opened from 5 to 20 parts.", "origin": "proposed by Claude at the user's request", "applied_by": "dashboard/blueprint_edits/apply_2026-08-20_closed_trade_decoding.py", "rationale": P}
REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(len(d["features"]), "features,", len(d["categories"]), "categories,", len(d["data_types"]), "data types")
