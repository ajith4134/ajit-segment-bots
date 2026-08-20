#!/usr/bin/env python3
"""Apply RL-050..056 to docs/features.json: futures first, the capital desk, the
LLM foundation. Proposal: docs/proposals/capital-desk-and-llm-foundation.md.

  RL-050  futures segment bot is built first; spot and options stay skeleton
  RL-051  per segment bot, editable: main paper balance, paper currency, the
          balance allocated to the bot, max and min capital per trade, leverage
  RL-053  leverage is a ceiling the bot chooses under (RL-041 stands)
  RL-054  below the minimum, the order is bumped up to the minimum
  RL-055  settings live in a file on the server; the board shows them
  RL-052/056  LLM foundation: prompt registry, evaluation and replay, budget
          and routing, memory and retrieval

Idempotent: re-running changes nothing once applied.
"""
import json, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "docs/features.json"
d = json.loads(REG.read_text())
TODAY = "2026-08-20"
P = "docs/proposals/capital-desk-and-llm-foundation.md"
feats = {f["id"]: f for f in d["features"]}
types = {t["id"]: t for t in d["data_types"]}
cats = {c["id"]: c for c in d["categories"]}


def add_type(tid, name, desc):
    if tid not in types:
        t = {"id": tid, "name": name, "description": desc, "origin": "proposed", "added": TODAY}
        d["data_types"].append(t); types[tid] = t


def add_part(pid, name, role, cat, consumes, produces, evidence, origin="proposed"):
    produces = list(produces) + (["part-health"] if "part-health" not in produces else [])
    f = {"id": pid, "name": name, "role": role, "category": cat, "consumes": list(consumes),
         "produces": produces, "switchable": True, "off_releases_resources": True,
         "states": ["off", "on"], "origin": origin, "evidence": evidence}
    if pid in feats: feats[pid].update(f)
    else: d["features"].append(f); feats[pid] = f


def add_block(cid, name, summary, scope, rulings):
    if cid not in cats:
        c = {"id": cid, "name": name, "summary": summary, "origin": "user", "approved": True,
             "consumes": [], "produces": [], "flow_origin": "agreed-provisional",
             "scope": scope, "scope_origin": "user", "rulings": rulings}
        d["categories"].append(c); cats[cid] = c


def consume_also(pid, *tids):
    for t in tids:
        if t not in feats[pid]["consumes"]: feats[pid]["consumes"].append(t)


def repoint(pid, old, new):
    f = feats[pid]
    f["consumes"] = [new if x == old else x for x in f["consumes"]]
    if new not in f["consumes"]: f["consumes"].append(new)


def set_role(pid, role, note):
    feats[pid]["role"] = role; feats[pid]["role_refined"] = {"on": TODAY, "from": note}


# ------------------------------------------------------------------ RL-050 build order
d["segments"]["build_order"] = {
    "ruling": "RL-050", "given": TODAY,
    "implement_now": ["futures"],
    "skeleton_only": ["spot", "options"],
    "note": "Futures is built first and fully. Spot and options keep every block declared "
            "(same template, T-1) and get placeholders, not working code, until futures is done.",
}

# ------------------------------------------------------------------ RL-051 capital desk
add_block("capital-desk", "Capital desk",
          "The user's money settings, read from one file per scope on the server (RL-055) and "
          "shown on the board: the main paper balance and its currency, and for every segment bot "
          "the balance allocated to it, the largest and smallest capital one trade may use, and "
          "the leverage ceiling (RL-051). The desk checks that the allocations add up, converts "
          "between currencies at the live rate, journals every change, and proposes — never "
          "applies — a rebalance.", "shared", ["RL-028", "RL-040", "RL-041", "RL-051", "RL-053", "RL-054", "RL-055"])

add_type("main-account-setting", "main account setting",
         "The main paper account as the user set it: total balance and its currency. Edited in the server settings file, never by a part.")
add_type("trade-capital-bounds", "trade capital bounds",
         "The largest and smallest capital one trade in this segment may use, as the user set them.")
add_type("leverage-ceiling", "leverage ceiling",
         "The highest leverage this segment bot may choose; the bot picks per trade at or under it (RL-053).")
add_type("paper-currency-rate", "paper currency rate",
         "Live rate from the main account's currency into a segment's quote currency, journalled per conversion (RL-029).")
add_type("allocation-headroom", "allocation headroom",
         "Main balance minus the sum of segment allocations. Negative means the user has allocated money that does not exist.")
add_type("capital-settings-verdict", "capital settings verdict",
         "Whether the capital settings are consistent: min <= max <= allocation, allocation sum <= main balance, ceiling within the chosen instrument's limit. Failing settings zero the risk limit.")
add_type("bounded-order", "bounded order",
         "A sized order after the capital bounds: bumped up to the minimum (RL-054), capped at the maximum. The only order shape execution ever sees.")
add_type("capital-utilisation", "capital utilisation",
         "How much of a segment's allocation is in positions, locked against orders, or free, right now.")
add_type("allocation-proposal", "allocation proposal",
         "A proposed move of allocation between segments with its reason and evidence. Shown on the board; the user edits the file or does not.")

add_part("main-account-settings-reader", "Main account settings reader",
         "read the main paper balance plus its currency from the server settings file, reloading when the file changes",
         "capital-desk", [], ["main-account-setting"], P)
set_role("capital-allotment-reader",
         "read this segment's slice of the settings file: allocated balance, min plus max capital per trade, leverage ceiling (RL-051)",
         "RL-051: one reader for the segment's whole capital slice, not only the allotment")
feats["capital-allotment-reader"]["produces"] = sorted(set(feats["capital-allotment-reader"]["produces"]) | {"trade-capital-bounds", "leverage-ceiling"})
add_part("allocation-conservation-checker", "Allocation conservation checker",
         "compare the sum of every segment's allocation against the main balance, alerting when the user has allocated more than exists",
         "capital-desk", ["main-account-setting", "capital-allotment"], ["allocation-headroom", "alert"], P)
add_part("capital-settings-validator", "Capital settings validator",
         "judge the settings consistent or not: min at most max, max at most the allocation, ceiling within what the chosen instrument allows",
         "capital-desk", ["main-account-setting", "capital-allotment", "trade-capital-bounds", "leverage-ceiling", "instrument-choice"], ["capital-settings-verdict"], P)
add_part("capital-settings-change-recorder", "Capital settings change recorder",
         "journal every change to a capital setting: which value, old, new, when, so the equity curve can be read against what the user changed",
         "capital-desk", ["main-account-setting", "capital-allotment", "trade-capital-bounds", "leverage-ceiling"], ["journal-entry"], P)
add_part("paper-currency-converter", "Paper currency converter",
         "convert the main account's currency into each segment's quote currency at the live price, journalling the rate used (RL-029)",
         "capital-desk", ["main-account-setting", "market-data"], ["paper-currency-rate"], P)
add_part("capital-utilisation-meter", "Capital utilisation meter",
         "measure how much of a segment's allocation is in positions, locked, or free",
         "capital-desk", ["account-balance", "locked-allocation", "capital-allotment"], ["capital-utilisation"], P)
add_part("allocation-rebalance-proposer", "Allocation rebalance proposer",
         "propose moving allocation toward the segments that earn it, from realised USDT results plus utilisation; proposed only, the user edits the file",
         "capital-desk", ["usdt-pnl-statement", "capital-utilisation", "bot-scorecard"], ["allocation-proposal"], P)
add_part("live-balance-divergence-watch", "Live balance divergence watch",
         "alert when the venue's real balance differs from the allocation the settings promise, once the segment is live",
         "capital-desk", ["account-balance", "capital-allotment", "money-mode"], ["alert"], P)

# in the risk block: the bounds gate sits between the sizer and everything that sends
add_part("trade-capital-bounds-gate", "Trade capital bounds gate",
         "bump a sized order up to the segment's minimum capital (RL-054) or cap it at the maximum, so execution only ever sees an order inside the user's bounds",
         "risk-capital-allocation", ["sized-order", "trade-capital-bounds", "capital-settings-verdict"], ["bounded-order"], P)
for pid in ("order-destination-router", "slippage-learner", "trade-lifecycle-recorder", "order-idempotency-stamper",
            "fund-lock-ledger", "participation-capped-order-splitter", "shortfall-decomposer"):
    repoint(pid, "sized-order", "bounded-order")
consume_also("leverage-selector", "leverage-ceiling")
set_role("leverage-selector", "choose leverage per trade from volatility plus funding (RL-041), never above the user's ceiling (RL-053)",
         "RL-053: the ceiling is the user's, the choice under it is the bot's")
consume_also("halt-enforcer", "capital-settings-verdict")
consume_also("paper-account-keeper", "paper-currency-rate")
consume_also("usdt-pnl-accountant", "paper-currency-rate")
consume_also("board-snapshot-builder", "main-account-setting", "capital-allotment", "trade-capital-bounds", "leverage-ceiling",
             "allocation-headroom", "capital-settings-verdict", "capital-utilisation", "allocation-proposal")

# ------------------------------------------------------------------ RL-052/056 LLM foundation
add_block("llm-foundation", "LLM foundation",
          "The layer every LLM-using part calls through. Prompts are versioned and rendered with "
          "retrieved context; every answer is checked against its output schema before any part "
          "reads it; prompt versions are scored on golden cases and promoted or refused; each part "
          "has a token budget and each decision a cost; memory is embedded and retrieved before "
          "reasoning (RL-056). llm-services stays the plumbing that places the call.",
          "shared", ["RL-010", "RL-013", "RL-026", "RL-052", "RL-056"])

add_type("prompt-template", "prompt template", "A drafted prompt with its output schema and the part it serves. Drafted, not yet a version.")
add_type("prompt-version", "prompt version", "An immutable, numbered prompt plus output schema that the registry currently serves for one part.")
add_type("prompt-promotion", "prompt promotion", "The gate's decision to promote a prompt template to the serving version, or to refuse it, with the scores.")
add_type("prompt-context", "prompt context", "Retrieved passages plus the verified market snapshot, assembled for one request.")
add_type("rendered-llm-request", "rendered LLM request", "An llm-request with its prompt version and context filled in: what actually goes to the model.")
add_type("validated-llm-output", "validated LLM output", "A model answer that passed its output schema. The only LLM output any part reads.")
add_type("golden-case", "golden case", "A past request with the answer that turned out right, from a closed trade's outcome.")
add_type("prompt-score", "prompt score", "How one prompt version scored across the golden cases: accuracy, schema failures, tokens, latency.")
add_type("llm-part-budget", "LLM part budget", "Tokens one part may spend in the window, from its record and the system's allowance.")
add_type("decision-cost", "decision cost", "What one trade intent cost in LLM tokens and money, set against what the trade made.")
add_type("embedding", "embedding", "A vector for one passage of knowledge, journal or decoded trade, with its source.")
add_type("retrieval-query", "retrieval query", "What to look up for one request, derived from the request.")
add_type("retrieval-hit", "retrieval hit", "Passages ranked for one query, each with its source and score.")
add_type("retrieval-score", "retrieval score", "How useful retrieved passages proved, measured against the prompt score they fed.")

L = "llm-foundation"
add_part("prompt-template-author", "Prompt template author",
         "draft a new prompt template for a part from research, skills plus the scores of the current version; proposed, never serving until promoted",
         L, ["research-finding", "skill", "prompt-score", "validated-llm-output"], ["prompt-template", "llm-request"], P)
add_part("prompt-registry", "Prompt registry", "keep every prompt version by part, serve the promoted one, never lose an old one",
         L, ["prompt-template", "prompt-promotion"], ["prompt-version"], P)
add_part("retrieval-querier", "Retrieval querier", "derive what to look up from a request: symbol, setup, regime, question",
         L, ["llm-request"], ["retrieval-query"], P)
add_part("knowledge-embedder", "Knowledge embedder", "embed knowledge, journal entries plus skills as they arrive, each with its source",
         L, ["source-document", "journal-entry", "skill"], ["embedding"], P)
add_part("retrieval-index", "Retrieval index", "answer a query with the best-ranked passages, re-ranked by how useful past hits proved",
         L, ["embedding", "retrieval-query", "retrieval-score"], ["retrieval-hit"], P)
add_part("context-assembler", "Context assembler", "assemble retrieved passages plus the verified snapshot into the context for one request, within the part's budget",
         L, ["retrieval-hit", "verified-snapshot", "llm-part-budget"], ["prompt-context"], P)
add_part("prompt-renderer", "Prompt renderer", "fill the serving prompt version plus the context into a request: what actually goes to the model",
         L, ["llm-request", "prompt-version", "prompt-context"], ["rendered-llm-request"], P)
add_part("structured-output-enforcer", "Structured output enforcer",
         "check every answer against its output schema; pass it on validated, or re-ask once with the failure named",
         L, ["llm-response", "prompt-version"], ["validated-llm-output", "llm-request"], P)
add_part("golden-case-keeper", "Golden case keeper", "keep the requests whose answers a closed trade later proved right or wrong, as the cases prompts are scored on",
         L, ["validated-llm-output", "closed-trade"], ["golden-case"], P)
add_part("prompt-evaluator", "Prompt evaluator", "score a prompt version by replaying it across the golden cases: accuracy, schema failures, tokens, latency",
         L, ["golden-case", "prompt-version", "prompt-template", "validated-llm-output"], ["prompt-score", "llm-request"], P)
add_part("prompt-promotion-gate", "Prompt promotion gate", "promote a template only when it beats the serving version on the golden cases; refuse otherwise",
         L, ["prompt-score"], ["prompt-promotion"], P)
add_part("prompt-drift-monitor", "Prompt drift monitor", "alert when the serving version's score falls over time, before the parts reading it go quietly wrong",
         L, ["prompt-score"], ["alert"], P)
add_part("part-token-budgeter", "Part token budgeter", "set how many tokens each part may spend in the window from its record plus the system's allowance",
         L, ["llm-call-record", "llm-spend-state", "llm-quota-state"], ["llm-part-budget"], P)
add_part("decision-cost-accountant", "Decision cost accountant", "set what each trade intent cost in tokens plus money against what the trade made",
         L, ["llm-call-record", "trade-intent", "usdt-pnl-statement"], ["decision-cost"], P)
add_part("retrieval-quality-scorer", "Retrieval quality scorer", "measure how useful retrieved passages proved, from the prompt scores they fed",
         L, ["retrieval-hit", "prompt-score"], ["retrieval-score"], P)

# the call path goes through the foundation: router and cache take the rendered request
repoint("llm-request-router", "llm-request", "rendered-llm-request")
repoint("llm-response-cache", "llm-request", "rendered-llm-request")
consume_also("llm-request-router", "llm-part-budget")
consume_also("llm-backpressure-gauge", "llm-part-budget")
# every LLM-using part reads validated output, never the raw response
for pid in ("skill-distiller", "strategy-decoder", "decision-quality-critic", "idea-generator", "part-author",
            "intent-explainer", "brain-self-reflector", "premortem-writer", "devils-advocate", "trade-narrative-writer"):
    repoint(pid, "llm-response", "validated-llm-output")
consume_also("bot-scorekeeper", "decision-cost")
consume_also("board-snapshot-builder", "decision-cost", "prompt-score")

# ------------------------------------------------------------------ recompute block contracts
for c in d["categories"]:
    parts = [f for f in d["features"] if f["category"] == c["id"]]
    if not parts: continue
    c["consumes"] = sorted({x for f in parts for x in f["consumes"]})
    c["produces"] = sorted({x for f in parts for x in f["produces"]})
    c["contract_recomputed"] = {"on": TODAY, "from": "its parts", "origin": "proposed"}

REG.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print("applied:", len(d["features"]), "features,", len(d["categories"]), "blocks,", len(d["data_types"]), "types")
