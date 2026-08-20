# Capital desk and LLM foundation — RL-050..056

Given by the user 2026-08-20 (RL-050, RL-051, RL-052), interviewed the same day
(RL-053..056), applied by
`dashboard/blueprint_edits/apply_2026-08-20_capital_desk_and_llm_foundation.py`.
24 parts, 2 blocks, 23 data types added; 297 → 321 parts, 25 → 27 blocks.

## 1. Build order (RL-050)

Futures segment bot first, fully. Spot and options keep every block declared —
same template, T-1 — and get placeholders, not working code, until futures is
done. Recorded in `features.json` → `segments.build_order`. The part monitor
will show the spot and options instances at `DECLARED` for as long as that is
true; nothing is inferred upward.

## 2. Capital desk (RL-051, RL-053, RL-054, RL-055) — new shared block

The user's money settings live in **one settings file per scope on the server**
(RL-055): one for the main account, one slice per segment. Edited over SSH or a
local form through an SSH tunnel; never by a part. The board shows the current
values and when they last changed. The dashboard is a **data producer**, never a
control switch — a setting changes what a part computes with, it does not turn a
part on or off (T-2).

| Setting | Scope | Type | Read by |
|---|---|---|---|
| main paper balance + currency | shared | `main-account-setting` | `main-account-settings-reader` |
| balance allocated to the bot | per segment | `capital-allotment` (existed, RL-040) | `capital-allotment-reader` |
| min and max capital per trade | per segment | `trade-capital-bounds` | same reader |
| leverage ceiling | per segment | `leverage-ceiling` | same reader |

| Part | Reads | Writes | Does |
|---|---|---|---|
| `main-account-settings-reader` | — (file) | main-account-setting | reload when the file changes |
| `capital-allotment-reader` *(refined)* | — (file) | capital-allotment, trade-capital-bounds, leverage-ceiling | the segment's whole slice |
| `allocation-conservation-checker` | main-account-setting, capital-allotment | allocation-headroom, alert | Σ allocations ≤ main balance |
| `capital-settings-validator` | the four settings, instrument-choice | capital-settings-verdict | min ≤ max ≤ allocation; ceiling within the instrument's limit |
| `capital-settings-change-recorder` | the four settings | journal-entry | old, new, when — equity read against edits |
| `paper-currency-converter` | main-account-setting, market-data | paper-currency-rate | RL-029: live rate, journalled |
| `capital-utilisation-meter` | account-balance, locked-allocation, capital-allotment | capital-utilisation | in positions / locked / free |
| `allocation-rebalance-proposer` | usdt-pnl-statement, capital-utilisation, bot-scorecard | allocation-proposal | proposes; user edits the file or not |
| `live-balance-divergence-watch` | account-balance, capital-allotment, money-mode | alert | venue balance ≠ promised allocation, once live |
| `trade-capital-bounds-gate` *(risk block)* | sized-order, trade-capital-bounds, capital-settings-verdict | bounded-order | **bump up to min (RL-054)**, cap at max |

Re-wired: everything that used to read `sized-order` (router, stamper, fund
lock, splitter, slippage learner, lifecycle recorder, shortfall decomposer) now
reads `bounded-order` — execution only ever sees an order inside the user's
bounds. `leverage-selector` reads `leverage-ceiling` and chooses under it
(RL-053; RL-041 stands). `halt-enforcer` reads `capital-settings-verdict` —
inconsistent settings zero the risk limit rather than trade on a guess.
`paper-account-keeper` and `usdt-pnl-accountant` read `paper-currency-rate`.
The board reads all of it.

## 3. LLM foundation (RL-052, RL-056) — new shared block

The layer every LLM-using part calls through. `llm-services` stays the plumbing
that places the call; the foundation decides *what* is sent and *whether the
answer counts*.

```
part ──llm-request──► retrieval-querier ──► retrieval-index ◄── knowledge-embedder
                            │                     │
                            └──► context-assembler ◄── verified-snapshot, llm-part-budget
                                        │
prompt-registry ──prompt-version──► prompt-renderer ──rendered-llm-request──► llm-request-router (services)
                                                                                      │
part ◄──validated-llm-output── structured-output-enforcer ◄──llm-response── callers ◄─┘
                                        │ (schema failed: re-ask once)
golden-case-keeper ◄── closed-trade     └──llm-request──►
        │
prompt-evaluator ──prompt-score──► prompt-promotion-gate ──prompt-promotion──► prompt-registry
        │                    └──► prompt-drift-monitor ──► alert
prompt-template-author ──prompt-template──► prompt-registry
```

| Layer (RL-056) | Parts |
|---|---|
| prompt + skill registry | `prompt-template-author`, `prompt-registry`, `prompt-renderer`, `structured-output-enforcer` |
| evaluation + replay | `golden-case-keeper`, `prompt-evaluator`, `prompt-promotion-gate`, `prompt-drift-monitor` |
| budget + routing | `part-token-budgeter`, `decision-cost-accountant` (router and backpressure gauge read the budget) |
| memory + retrieval | `knowledge-embedder`, `retrieval-querier`, `retrieval-index`, `context-assembler`, `retrieval-quality-scorer` |

Re-wired: the ten parts that read raw `llm-response` (skill distiller, strategy
decoder, critic, idea generator, part author, intent explainer, self-reflector,
premortem, devil's advocate, narrative writer) now read `validated-llm-output`.
Only the cache and the enforcer ever see a raw answer. The router and cache take
`rendered-llm-request`. `bot-scorekeeper` reads `decision-cost`, so a bot that
wins by spending more than it makes is scored for it.

## 4. Checked

`python3 dashboard/check_contracts.py` — 321 features, 27 categories, all
contracts hold (R-01, R-02/T-1..T-6, R-03). Deeper audit: no type without a part
producing it, none without a reader, none unused, no duplicate ids, no flow gaps,
0 wires between bull/bear/tailgater. 1 460 data edges.

## 5. Assumptions made, open to correction

1. Main account currency defaults to USDT; the file holds a currency code, the
   converter handles any quote the feed prices.
2. The leverage ceiling is checked against the chosen instrument's own maximum
   (`instrument-choice`), not a venue-wide number — futures contracts differ.
3. "Bump up to the minimum" still passes through the risk limit: if the minimum
   exceeds what the limit allows, the limit wins and the trade is a near-miss.
   Otherwise a zero limit could be overridden by a settings file.
