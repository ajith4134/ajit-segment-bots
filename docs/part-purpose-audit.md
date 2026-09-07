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
| `FED BUT PRODUCES NOTHING` | real input reached it **on every input it declares** and nothing has ever come of it | that it is hollow: a part that publishes only an alert is silent when there is nothing to alert about |
| `WAITING FOR AN INPUT IT HAS NEVER SEEN` | it produces nothing and has never received at least one input it declares | whether the missing input is a defect upstream or an event that has not happened |
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

**43 of 373 hand-judged; all 373 now carry measured evidence** as of 2026-09-07.

**Silence is often the correct output, and the probe cannot know that.** Ten
parts were reported as receiving every input they declare and publishing
nothing — the probe's strongest negative finding. Reading the code changed the
verdict for six of them: `fund-conservation-auditor` and
`live-balance-divergence-watch` publish an `alert` and only an alert, so no alert
is what a conserved book looks like; `drawdown-episode-tracker` needs a drawdown;
`limit-price-walker` and `resting-order-cancel-policy` act on resting orders and
every order this system places is a market order; `paper-currency-converter` has
no second currency to convert to. Those are `NOT MEASURED` with the reason
written down, not defects — and each names exactly what it would take to test it.

Two of the ten are real skeletons and both are the same shape:
`options-flow-reader` and `exchange-announcement-reader` both have a `read_rows`
that returns `()` because no source was ever wired to them. `options-flow-reader`
is now buildable rather than blocked — the tape already carries an
`open_interest` stream per contract and `operate/nse_fo_bhavcopy.py` carries
volume and open interest for every NSE option.

    hand-judged                        43   11 serving, 4 skeletons, 28 starved or
                                            correctly silent, each naming its cause

Four of the seven skeletons stopped being skeletons on 2026-09-07: the three web
readers were given the fetcher they were built to be handed, and
`fact-provenance-tracker` came alive the moment a document existed for it to
track.
    PRODUCES REAL OUTPUT              143   fed, publishing, own work counters above zero
    WAITING FOR AN INPUT NEVER SEEN    49   produces nothing and one of its declared inputs
                                            has never arrived -- starved, not broken
    ONLY REFUSALS                      22   everything above zero is a refusal or an attempt
    NOT MEASURED                      140   nothing has reached it, or it is not running
    FED BUT PRODUCES NOTHING            0   all ten were hand-judged; see below

**Splitting starvation from silence is what made the list actionable.** The probe
first reported 64 parts producing nothing. Comparing what each declares it
consumes against what actually arrived moved 49 of them to `WAITING FOR AN INPUT
IT HAS NEVER SEEN` -- a finding about something upstream, not about the part --
and left 10 that had received **every** input they asked for and still published
nothing. Ten is a list a person can read in an afternoon; 64 is not.

The two new skeletons and two new serving verdicts are the first rows judged by a
person reading the actual output rather than by the probe: `arxiv-feed-reader`
(60,780 fetches attempted, nothing ever published), and the two exit-plan
proposers, which build real plans and are starved by a horizon that was measured
on a market printing forty times faster
(`measurements/2026-09-07-exit-plan-starvation/`).

Measured against the live spine on a real trading day, 2026-09-07, over a 45
second observed window with the cumulative counters since the spine started at
08:57:32 UTC. Re-run it with `python3 dashboard/audit_part_purpose.py`; every row
below carries what was fed in and what came out, so a re-run that disagrees is a
finding rather than a refresh.

`correlation-cluster-mapper` was judged on 2026-09-06 after two defects were fixed that had it crash-looping on the live spine (139 restarts, exit code 1). A part that cannot stay up cannot be judged, so the fix came first: `correlation` has always returned None for a series with no variation and `map()` used the result without asking, and `describe_correlation_clusters` recomputed the whole quadratic mapping on every health read, defeating the remap pacing. Both are covered by real-tape tests.

The user said 370; the blueprint holds **373** parts across 29 categories, and every part is what the instruction means.


## What the walk found on 2026-09-07, beyond the per-part rows

Four defects that no per-part row could have shown, because each is a **pair** of
parts disagreeing rather than one part failing:

1. **`instrument-selector` priced the intent's symbol, not the contract it
   chose.** Every options order left on the underlying's scale -- INFY 1200 CE at
   1,088.40 against a premium of 21.10 -- and 84.6% of orders reaching a verdict
   were refused as `decision-price-stale`.
2. **A `stop-target-plan` carried one contract's price onto another's order.**
   The plan is keyed by the underlying while every price on it is a contract's
   premium, and the ATM strike moves during a session: 477 orders for
   `NIFTY 23750 CE` carried a decided price of 1.30 while that contract traded
   100-120 all day. None filled.
3. **`instrument-selector` registered no share at all**, so
   cash-equity-intraday could not express a view even in principle -- and
   recorded **zero** refusals, which reads exactly like nothing being wrong.
4. **The cash-equity shortlist had sealed itself shut**, ranking the alphabet
   because `percentile_rank` assigns a position and every signal was `None` on a
   cold start.

The shape they share is worth more than any of them: **a value and the thing it
describes travelling separately.** A price without the contract it is a price of;
a plan without the contract it was priced for; a symbol with no instrument behind
it; a rank with no measurement under it. Each was invisible to the contract
checkers, to the dataflow audit and to this ledger's own per-part probe, because
every part involved was individually healthy.


## One missing fetcher makes twelve parts inert

Measured 2026-09-07. Twelve parts, about **2.5 million messages in and zero
out**, all from one cause:

| part | in | out |
|---|---|---|
| open-web-reader | 380,660 | 0 — every fetch counted `fetch_failures` |
| arxiv-feed-reader | 380,660 | 0 — every fetch `refused_no_search_installed` |
| book-and-paper-fetcher | 380,660 | 0 — every fetch a failure |
| github-strategy-miner | 380,660 | 0 |
| prompt-template-author | **0** | 0 — it receives nothing at all |
| prompt-registry | 0 | 0 — `templates_seen` 0, `versions_registered` 0 |
| prompt-promotion-gate | 0 | 0 — `decisions` 0 |
| prompt-renderer | 210,951 | 0 — every one `refused_no_active_version` |
| llm-request-router | 195,832 | 0 |
| llm-model-picker | 210,943 | 0 |
| semantic-fact-store | 289,926 | 0 |
| instruction-writer | 67,289 | 0 |

The chain reads in one line: **no web fetcher → no `research-finding` and no
`skill` → `prompt-template-author` has no evidence → no `prompt-template` → the
registry has no version → no version is active → `prompt-renderer` refuses every
one of 210,951 `llm-request`s → nothing downstream of an LLM call has ever run.**

`open-web-reader`'s own docstring says it plainly: *"No fetcher is installed on
this machine: the box has no outbound web search."* The three readers are
honestly empty rather than decorative — the rate limit, the budget and the
backlog bound are all real and apply the moment a fetcher is installed through
`install_fetcher`. But the effect on the ledger is large and would otherwise read
as twelve separate problems.

**This is an operator decision, not a code fix.** The box has outbound network —
Upstox, NSE and Yahoo are all reached every session — so what is missing is a
search provider, not connectivity. Installing one makes an unattended 24/7 system
fetch from the open web on its own initiative, which is outward-facing in a way
nothing else here is. It is left for the operator to say yes to.

Until then these twelve rows are `SKELETON` for the three readers that have a
source to be given and `NOT MEASURED` for the nine downstream of them, each
naming this as the reason rather than appearing to be nine independent failures.


## The knowledge chain has a bootstrap cycle, and no model to break it

Wiring the readers on 2026-09-07 moved the wall four parts further along and then
hit something a fetcher cannot fix.

    open-web-reader          380,660 failures -> 0, 120 web-idea published
    book-and-paper-fetcher   0 documents -> 10 of 10 fetches, 0 paywalls
    arxiv-feed-reader        0 papers -> publishing
    skill-distiller          0 -> 88 llm-request
    fact-provenance-tracker  0 -> 44 published

`skill-distiller` turns a document into a skill **by asking a model**, so it
publishes an `llm-request` rather than a `skill`. `prompt-template-author` needs a
`skill` or a `research-finding` before it will write a template.
`prompt-registry` needs a template before it has a version. `prompt-renderer`
needs an active version before it will render the request. So:

**a model is needed to make a skill, a skill to write the prompt template, and
the template to call the model.**

`research-finding` is the one input that could break the cycle without a model,
and neither of its two producers can supply one right now:
`github-strategy-miner` has mined 0 repositories because the searches returned
web pages rather than github.com URLs, and `strategy-decoder` has received
nothing at all.

**And there is no model either way.** `llm-model-picker` reports
`models_declared` **0**, `local-model-caller` reports `is_available` **0**, and
neither `metered-api-caller` nor `subscription-session-caller` has ever made a
call. Installing a local model is a multi-GB download and CPU inference on a box
already running 323 parts; a paid API is spend. Rule 3 says confirm the cost
rather than the concept, so both are left for the operator.

Until one exists, the twenty-odd parts downstream of an LLM call are `NOT
MEASURED` with this as the named reason, rather than appearing to be twenty
independent failures.

## The parts, by block


### Stock market news data (`stock-market-news-data`) — 29 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `broker-news-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `corporate-action-adjuster` | PRODUCES REAL OUTPUT | broker-instrument-listing 109,714; corporate-action-report 20 | corporate-action 19 — work: actions_understood 19 |
| `corporate-action-reader` | NOT MEASURED | nothing has reached it | corporate-action-report 20 — work: is_warm 1; requests_made 1 |
| `exchange-filing-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `financial-press-feed-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `instrument-restriction-state` | PRODUCES REAL OUTPUT | instrument-restriction-report 663 | instrument-restriction 6,851 — work: symbols_restricted 221 |
| `macro-event-calendar-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `market-session-calendar` | NOT MEASURED | nothing has reached it | market-session-state 125 — work: is_warm 1; requests_made 1 |
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
| `trading-restriction-reader` | NOT MEASURED | nothing has reached it | instrument-restriction-report 663 — work: requests_made 6; is_warm 1 |
| `unexplained-move-investigator` | NOT MEASURED | nothing has reached it | nothing published |
| `web-news-searcher` | NOT MEASURED | nothing has reached it | nothing published |


### Market data feed (`market-data-feed`) — 24 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `api-key-pool-rotator` | NOT MEASURED | nothing has reached it | nothing published |
| `ban-signal-detector` | NOT MEASURED | nothing has reached it | nothing published |
| `broker-candle-bridge` | PRODUCES REAL OUTPUT | broker-candle 947,074; broker-subscribed-instrument-listing 5,915 | candle 1,332,228 — work: updates_released_after_listing 1,156 |
| `broker-market-data-bridge` | PRODUCES REAL OUTPUT | broker-market-data 474,940; broker-subscribed-instrument-listing 5,915 | market-data 7,698,774 — work: updates_released_after_listing 1,653 |
| `broker-order-book-bridge` | PRODUCES REAL OUTPUT | broker-order-book-snapshot 464,153; broker-subscribed-instrument-listing 5,919 | order-book-snapshot 5,196,404 — work: updates_released_after_listing 1,651 |
| `broker-quote-bridge` | PRODUCES REAL OUTPUT | broker-order-book-snapshot 464,769; broker-subscribed-instrument-listing 5,915 | market-quote 752,962 — work: updates_released_after_listing 1,651 |
| `broker-symbol-universe-bridge` | PRODUCES REAL OUTPUT | broker-instrument-listing 109,749; broker-price-frame 5,868; cash-equity-shortlist 1,131 | symbol-universe 1,106,742 — work: equities_listed 2,654; equities_outside_the_shortlist 2,417; contracts_published 430; equities_covered_by_a_derivative 210; +5 more |
| `broker-underlying-price-frame-bridge` | PRODUCES REAL OUTPUT | broker-subscribed-instrument-listing 5,918; broker-price-frame 5,868 | symbol-price-frame 182,112 — work: levels_matched 63,573; frames_published 5,691; underlyings_resolved 17 |
| `cash-equity-shortlist-ranker` | PRODUCES REAL OUTPUT | liquidity-grade 374,379; candle 368,462; broker-instrument-listing 109,775; +2 more | cash-equity-shortlist 2,252 — work: ranks_computed 19,440; priced_symbols 42 |
| `ccxt-venue-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `cross-venue-price-consolidator` | NOT MEASURED | nothing has reached it | nothing published |
| `equity-opportunity-profiler` | PRODUCES REAL OUTPUT | broker-instrument-listing 109,714; broker-token-standing 1,910 | equity-historical-profile 384 — work: requests_planned 7,657; profiles_published 384; windows_already_read 384 |
| `feed-coverage-auditor` | PRODUCES REAL OUTPUT | market-data 386,322; order-book-snapshot 376,122; symbol-universe 184,151 | feed-coverage 2,558,631 — work: symbols_expected 860; covered 646; uncovered 209; partial 5 |
| `feed-gap-detector` | SERVING ITS PURPOSE | 12,000 real Upstox prints, 6 NIFTY contracts, 2026-09-04 tape; then the live feed | 5,262 messages seen on `upstox`, 4 streams tracked, 0 false sequence gaps, 2 real silence gaps. **Was SKELETON until 2026-09-06** — watched only two dead crypto streams and dropped every Upstox print on a silent `continue`. |
| `feed-jump-detector` | PRODUCES REAL OUTPUT | candle 370,182 | feed-jump 18,253 — work: checks_inside_a_widened_bound 150,836; jumps_found 78,297; levels_published 18,253; level_refreshes 12,048; +3 more |
| `order-book-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `price-level-sampler` | PRODUCES REAL OUTPUT | market-data 386,376 | symbol-price-frame 160,278 — work: trades_observed 377,460; frames_published 5,916 |
| `quote-level-sampler` | PRODUCES REAL OUTPUT | market-quote 376,529 | symbol-quote-frame 15,940 — work: quotes_observed 376,529; frames_published 7,970; frames_split 2,508 |
| `stream-budget-planner` | NOT MEASURED | nothing has reached it | nothing published |
| `symbol-catalogue-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `tick-size-resolver` | PRODUCES REAL OUTPUT | order-book-snapshot 360,225; symbol-universe 145,253 | price-increment 154,721 — work: increments_published 172,735; increment_refreshes 169,545; declared_inferred_disagreements 26,888; increment_changes 3,190; +3 more |
| `venue-pool-rotator` | NOT MEASURED | nothing has reached it | nothing published |
| `venue-quote-stream-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `venue-trade-stream-reader` | NOT MEASURED | nothing has reached it | nothing published |


### Closed trade decoding (`closed-trade-decoding`) — 20 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `entry-quality-scorer` | ONLY REFUSALS | market-data 386,261; journal-entry 289,793; closed-trade 3 | nothing published — work: unmeasurable_no_signal_time 3 |
| `excursion-profiler` | WAITING FOR AN INPUT IT HAS NEVER SEEN | peak-excursion 3,656 | nothing published |
| `exit-counterfactual-replayer` | WAITING FOR AN INPUT IT HAS NEVER SEEN | market-data 386,307; bull-exit-plan 265; bear-exit-plan 23; +1 more | nothing published |
| `exit-quality-scorer` | WAITING FOR AN INPUT IT HAS NEVER SEEN | peak-excursion 3,656 | nothing published |
| `exploration-pair-decoder` | WAITING FOR AN INPUT IT HAS NEVER SEEN | directional-opinion 121,548; closed-trade 3 | nothing published |
| `holding-horizon-profiler` | NOT MEASURED | nothing has reached it | nothing published |
| `lesson-extractor` | ONLY REFUSALS | stop-audit 3; pnl-attribution 2 | nothing published — work: refused_too_few_trades 4,529 |
| `loss-cause-classifier` | WAITING FOR AN INPUT IT HAS NEVER SEEN | peak-excursion 3,657 | nothing published |
| `luck-skill-separator` | PRODUCES REAL OUTPUT | volatility-forecast 791,642; symbol-profile 371,283; closed-trade 3 | outcome-significance 15 — work: outcomes_assessed 3; indistinguishable_from_noise 2; significant 1 |
| `near-miss-recorder` | PRODUCES REAL OUTPUT | entry-candidate 90,731; directional-opinion 55,141; trade-intent 35,811; +1 more | near-miss-episode 76,228 — work: never_resolved_for_want_of_a_price 14,451; near_misses_recorded 10,958; resolved 8,099; unresolved 2,859; +1 more |
| `pnl-attributor` | PRODUCES REAL OUTPUT | cost-estimate 1,570,591; peak-excursion 3,654; fill 332; +1 more | pnl-attribution 12 — work: total_absolute_residual 25,638; trades_attributed 2; trades_where_costs_exceeded_the_move 2; trades_without_fills 1 |
| `regime-transition-tagger` | PRODUCES REAL OUTPUT | market-regime 197,387; closed-trade 3 | regime-transition-flag 9 — work: trades_tagged 3 |
| `sequence-pattern-miner` | ONLY REFUSALS | closed-trade 3 | nothing published — work: refused_thin_samples 7,652 |
| `shortfall-decomposer` | NOT MEASURED — waiting on a closed trade with a shortfall | 343,706 order-book-snapshot, 9,217 symbol-price-frame, 321 fill, 241 bounded-order | nothing published. It decomposes the gap between the decided price and the filled price, and needs a completed round trip to decompose. 3 closed trades exist so far |
| `stop-placement-auditor` | PRODUCES REAL OUTPUT | market-data 386,381; stop-target-plan 18,956; peak-excursion 3,655; +1 more | stop-audit 15 — work: stops_audited 3; trades_without_a_stop 3 |
| `trade-cluster-detector` | NOT MEASURED — waiting on closed trades | 81,179 correlation-cluster, 3 closed-trade | nothing published. It groups closed trades that were really one bet; three closed trades is not a cluster. The correlation input is live and real, so this is starved rather than broken |
| `trade-episode-encoder` | ONLY REFUSALS | journal-entry 290,033; near-miss-episode 19,730; closed-trade 3; +3 more | nothing published — work: re_encodes_skipped 214,546; refused_incomplete 5 |
| `trade-narrative-writer` | WAITING FOR AN INPUT IT HAS NEVER SEEN | journal-entry 289,990; decision-rationale 47,454 | nothing published |
| `trade-replay-verifier` | ONLY REFUSALS | journal-entry 290,059; fill 332; closed-trade 3 | nothing published — work: trades_not_verifiable 3; paper_fills_not_verifiable 2 |
| `winner-pattern-miner` | WAITING FOR AN INPUT IT HAS NEVER SEEN | outcome-significance 3 | nothing published |


### Learning loop (`learning-loop`) — 18 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `bot-scorekeeper` | WAITING FOR AN INPUT IT HAS NEVER SEEN | directional-opinion 121,709; learning-reward 3; outcome-significance 3 | nothing published |
| `champion-challenger-gate` | NOT MEASURED | nothing has reached it | nothing published — work: a_tie_keeps_the_champion 1 |
| `edge-graduation-gate` | WAITING FOR AN INPUT IT HAS NEVER SEEN | coverage-report 347,586 | nothing published |
| `exit-timing-learner` | NOT MEASURED | nothing has reached it | nothing published |
| `feature-attribution-tracker` | PRODUCES REAL OUTPUT | bear-feature-vector 148,883; bear-raw-conviction 146,197; bull-feature-vector 3,445; +1 more | feature-attribution 448,434 — work: attributions_recorded 149,478; by_bot.bear-bot 146,197; by_bot.bull-bot 3,281; recent_kept 200 |
| `feature-reliability-scorer` | WAITING FOR AN INPUT IT HAS NEVER SEEN | vol-feature-set 368,852; feature-attribution 149,478 | nothing published |
| `forecast-trust-learner` | NOT MEASURED | nothing has reached it | nothing published |
| `instruction-performance-tracker` | NOT MEASURED | nothing has reached it | nothing published |
| `label-builder` | PRODUCES REAL OUTPUT | cost-estimate 1,570,649; peak-excursion 3,654; closed-trade 3 | training-label 54 — work: by_component.the-entry-was-timed:false 3; by_component.the-setup-was-right:true 3; by_component.the-size-was-right:true 3; labels_built 3; +3 more |
| `model-registry` | WAITING FOR AN INPUT IT HAS NEVER SEEN | retrain-request 50 | nothing published |
| `regret-tracker` | WAITING FOR AN INPUT IT HAS NEVER SEEN | market-regime 197,387; trade-intent 65,016 | nothing published |
| `retrain-scheduler` | PRODUCES REAL OUTPUT | duty-cycle 354; training-label 26 | retrain-request 250 — work: requests 50; duty_cycle_granted 1 |
| `reward-shaper` | PRODUCES REAL OUTPUT | peak-excursion 3,656; closed-trade 3; outcome-significance 3; +2 more | learning-reward 12 — work: bounded_at_the_cap 3; by_component.capital-tied-up 3; by_component.distinguishable-from-noise 3; by_component.risk-taken-to-earn-it 3; +1 more |
| `sample-weight-assigner` | PRODUCES REAL OUTPUT | training-label 26 | sample-weight 104 — work: by_reason.age 26; weights_assigned 26; by_reason.it-did-not-resolve-inside-its-horizon 3; smallest_weight 0 |
| `signal-excursion-profiler` | PRODUCES REAL OUTPUT | training-label 26 | excursion-profile 807,357 — work: profiles_published 1,625,904; profiles_fitted 542,938; profile_refreshes 173,888; claims_that_came_wrong 54,018; +4 more |
| `signal-horizon-profiler` | PRODUCES REAL OUTPUT | training-label 26 | horizon-profile 38,270 — work: claims_recorded 112,141; profiles_fitted 9,605; profiles_published 9,605; slowest_median_seconds 259; +1 more |
| `signal-outcome-labeller` | PRODUCES REAL OUTPUT | market-regime 197,674; entry-candidate 153,217; symbol-price-frame 9,569 | training-label 414 — work: prices_observed 2,425,346; claims_opened 222; by_detector.spread-reversion-detector 187; unresolved 172; +7 more |
| `slippage-learner` | WAITING FOR AN INPUT IT HAS NEVER SEEN | fill 332; bounded-order 281 | nothing published |


### Intelligence (`intelligence`) — 18 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `abstention-coverage-auditor` | PRODUCES REAL OUTPUT | directional-opinion 121,643; near-miss-episode 19,730 | coverage-report 1,043,334 — work: acted_on 81,104; abstained 40,539; near_misses_scored 8,665; overall_coverage 1 |
| `causal-refutation-battery` | WAITING FOR AN INPUT IT HAS NEVER SEEN | feature-attribution 149,501 | nothing published |
| `correlation-cluster-mapper` | SERVING ITS PURPOSE — with a caveat on feed density | real 5-minute bars for 15 NSE shares over 60 days (Yahoo, free source), 4,365-4,374 bars each, fed synchronised by timestamp at production settings (window 256, minimum 64, threshold 0.7); and separately the full captured tape of 2026-09-04, 64,721 real prints across 582 NSE_EQ symbols | On the dense data it is a reading an operator would recognise: one cluster, HCLTECH/INFY/WIPRO at 0.78 average and 0.72 weakest — the IT trio. Same-sector pairs positive (INFY/WIPRO 0.807, SBIN/AXISBANK 0.477), cross-sector pairs at nothing (HDFCBANK/TCS -0.062, HINDUNILVR/NESTLEIND -0.033, TITAN/SBIN 0.08). It is measuring returns, not prices, and it says so. **On the captured tape the same settings left 167,693 of 169,071 pairs (99.2%) unmeasured** for want of 64 shared observations, and the one cluster it did form — 360ONE/CDSL/HINDZINC/IOLCP/TMCV — is five unrelated businesses. That is the feed's per-symbol density, not the part: it correctly refuses to call a thin pair uncorrelated. What it means live is that this part is only worth its CPU on symbols the feed samples densely, and `unmeasured_pairs` on its standing is the number that says so. |
| `counterfactual-replayer` | WAITING FOR AN INPUT IT HAS NEVER SEEN | directional-opinion 118,623; trade-intent 64,986; symbol-price-frame 9,953 | nothing published |
| `cross-segment-exposure-watch` | PRODUCES REAL OUTPUT | position 49,922 | exposure-view 11,210 — work: by_segment.stock-options 5,141,910; gross_notional 5,141,910; net_notional 5,141,910; by_underlying.KOTAKBANK 941,008; +11 more |
| `cross-segment-lesson-bridge` | NOT MEASURED | nothing has reached it | nothing published |
| `cross-segment-signal-bridge` | PRODUCES REAL OUTPUT | broker-open-interest 267,799; position 49,777; symbol-price-frame 9,859; +1 more | cross-segment-signal 118,543 — work: by_receiving_segment.index-futures 118,543; by_receiving_segment.stock-futures 118,543; by_receiving_segment.stock-options 118,543; signals_sent 118,543; +5 more |
| `decision-quality-critic` | WAITING FOR AN INPUT IT HAS NEVER SEEN | verified-snapshot 590,001; directional-opinion 121,710; counter-argument 47,456; +4 more | nothing published |
| `edge-decay-tracker` | NOT MEASURED | nothing has reached it | nothing published |
| `forgetting-auditor` | NOT MEASURED | nothing has reached it | nothing published |
| `idea-generator` | NOT MEASURED | regime-memory 57,756 | llm-request 124,596 |
| `market-anomaly-detector` | PRODUCES REAL OUTPUT | feed-coverage 920,148; market-data 386,262; order-book-snapshot 376,167; +1 more | market-anomaly 2,021,534 — work: checks 700,518; anomalies 109,591; anomaly_this_venue_has_stopped_updating 100,501; by_anomaly.this-venue-has-stopped-updating 100,501; +3 more |
| `market-event-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `open-web-reader` | SERVING ITS PURPOSE | 293 real skill-gap queries, searched against DuckDuckGo and the pages themselves | 120 web-idea published, 50 ideas returned, `fetch_failures` **0** against 380,660 of 380,660 before it was given a fetcher on 2026-09-07. `fetcher_is_installed` 1. Its own rate limit binds (`refused_rate_limited` 186), which is the reader working rather than failing |
| `regime-break-detector` | WAITING FOR AN INPUT IT HAS NEVER SEEN | journal-entry 289,838; symbol-price-frame 11,583 | nothing published |
| `self-model-reporter` | PRODUCES REAL OUTPUT | coverage-report 347,794 | competence-map 273,082 — work: maps_produced 70,150 |
| `trial-count-accountant` | NOT MEASURED | nothing has reached it | nothing published — work: nominal_significance 0 |
| `turbulence-index-gauge` | PRODUCES REAL OUTPUT | symbol-price-frame 10,887 | turbulence-index 6,867 — work: measurements 6,872; measured 1,650; mean_shrinkage 1 |


### Risk and capital allocation (`risk-capital-allocation`) — 17 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `capital-allotment-reader` | NOT MEASURED | main-account-setting 1,904 | capital-allotment 46,260; trade-capital-bounds 23,145; leverage-ceiling 23,130 |
| `drawdown-breaker` | NOT MEASURED | account-balance 18,785; closed-trade 3 | risk-limit 22,882 |
| `event-risk-limiter` | PRODUCES REAL OUTPUT | market-anomaly 699,769; turbulence-index 6,866 | risk-limit 8,256,928 — work: events_registered 110,008; by_kind.anomaly 109,500; events_restated 107,599; limits_issued 21,895; +5 more |
| `exit-order-chainer` | PRODUCES REAL OUTPUT | stop-target-plan 18,944; fill 332 | stop-adjustment 996 — work: fills_without_a_plan 332 |
| `exposure-limiter` | NOT MEASURED | correlation-cluster 92,148; position 50,052; account-balance 18,826; +2 more | risk-limit 47,136 |
| `halt-enforcer` | PRODUCES REAL OUTPUT | trading-halt 208,111; policy-decision 47,426; instrument-restriction 6,851; +2 more | risk-limit 182,404 — work: limits_issued 191,487; zero_limits_issued 184,324; symbol_scoped_limits_issued 184,250; halts_raised 4; +2 more |
| `intraday-square-off-placer` | ONLY REFUSALS | position 49,922; money-mode 5,595; market-session-state 31 | nothing published — work: refused_no_session 171 |
| `leverage-selector` | PRODUCES REAL OUTPUT | volatility-forecast 591,634; trade-intent 64,735; leverage-ceiling 5,772; +1 more | leverage-choice 9,898 — work: choices 9,898; unleveraged_for_want_of_a_broker_quote 532; average_chosen 3 |
| `margin-liquidation-watch` | NOT MEASURED | nothing has reached it | nothing published |
| `participation-capped-order-splitter` | PRODUCES REAL OUTPUT | volatility-forecast 791,778; order-book-snapshot 376,428; bounded-order 281 | execution-schedule 163 — work: symbols_measured 1,500; schedules 163; split 132; single_slice 16 |
| `position-flattener` | ONLY REFUSALS | position 49,922; money-mode 5,593; human-override 1,901 | nothing published — work: reads_with_no_override 8 |
| `position-sizer` | PRODUCES REAL OUTPUT | risk-limit 8,511,799; price-increment 154,721; trade-intent 64,967; +7 more | sized-order 281 — work: stop_from_absolute_fallback 33,546; stop_from_refined_plan 3,156; opens_without_an_instrument_choice 1,821; sized 1,757; +2 more |
| `pre-expiry-position-closer` | ONLY REFUSALS | position 49,922; broker-subscribed-instrument-listing 5,915; money-mode 5,593; +1 more | nothing published — work: refused_no_session 181 |
| `profit-lock` | PRODUCES REAL OUTPUT | excursion-profile 173,076; position 49,922; symbol-price-frame 11,592 | stop-adjustment 30,747 — work: adjustments 112,068; held 57,706; retracements_learned 57,619; trailed 30; +1 more |
| `stop-frequency-breaker` | PRODUCES REAL OUTPUT | closed-trade 3 | risk-limit 933 — work: baseline_stop_rate 0 |
| `stop-target-placer` | PRODUCES REAL OUTPUT | volatility-forecast 791,473; symbol-profile 371,369; excursion-profile 173,056; +5 more | stop-target-plan 56,829 — work: excursions_learned 57,790; plans 18,943 |
| `trade-capital-bounds-gate` | PRODUCES REAL OUTPUT | trade-capital-bounds 5,793; capital-settings-verdict 1,903; sized-order 281 | bounded-order 1,967 — work: capped_at_maximum 163 |


### Autonomous operation (`autonomous`) — 17 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `autonomy-boundary` | PRODUCES REAL OUTPUT | survival-tier 200,632; money-mode 5,595 | autonomy-envelope 384,026 — work: by_narrowing_reason.a-recent-self-modification-broke-something 192,013; by_narrowing_reason.measured-competence-fell-below-the-level 192,013; issues 192,013; issued_on_the_paper_floor 192,012; +2 more |
| `autonomy-policy-engine` | PRODUCES REAL OUTPUT | autonomy-envelope 191,998; competence-map 68,256; trade-intent 65,009 | policy-decision 189,688 — work: decisions 47,422; allowed 42,433 |
| `capability-gap-finder` | PRODUCES REAL OUTPUT | folded-circuit-map 111,640 | capability-gap 428 — work: duplicates 427; by_source.a-whole-block-has-nothing-running 1; gaps_found 1; reachable_gaps 1 |
| `conservation-planner` | PRODUCES REAL OUTPUT | survival-tier 200,664 | conservation-plan 200,664 — work: parts_stopped 940,500; plans_made 5,700; plans_are_nested 1 |
| `failing-part-detector` | NOT MEASURED | nothing has reached it | part-fault 6,444 — work: checks 587,932; by_kind.taking-longer-every-tick 4,263; faults_found 4,263; silent_faults_found 4,263; +4 more |
| `folded-circuit-view` | NOT MEASURED | nothing has reached it | folded-circuit-map 223,256 — work: folds 112,070; dark_blocks_reported 2,271; parts_declared 373; blocks_declared 29 |
| `human-override-reader` | NOT MEASURED | nothing has reached it | human-override 5,711 — work: longest_active_seconds 661,569; expired_overrides 1,907; reads 1,907 |
| `no-progress-detector` | PRODUCES REAL OUTPUT | journal-entry 289,968 | part-fault 4 — work: checks 226,118; quiet_periods 226,116; stages_watched 5; by_stage.bounded-order 1; +1 more |
| `part-admission-gate` | WAITING FOR AN INPUT IT HAS NEVER SEEN | policy-decision 47,455 | nothing published |
| `part-author` | WAITING FOR AN INPUT IT HAS NEVER SEEN | folded-circuit-map 111,649; capability-gap 428 | nothing published |
| `part-replacement-planner` | ONLY REFUSALS | part-fault 1,612 | nothing published — work: refused_no_replacement 1,611; refused_unknown_part 1 |
| `self-modification-journal` | WAITING FOR AN INPUT IT HAS NEVER SEEN | policy-decision 47,455 | nothing published |
| `survival-tier-monitor` | PRODUCES REAL OUTPUT | llm-spend-state 1,899; llm-quota-state 1,898 | survival-tier 803,464 — work: readings 201,116; times_bound_by_quota 200,626; unmeasured_readings 490; tier_changes 1 |
| `trading-halt-decider` | PRODUCES REAL OUTPUT | market-anomaly 700,718; survival-tier 201,117; autonomy-envelope 191,998; +3 more | trading-halt 415,003 — work: decisions 210,134; times_closing_was_permitted_during_a_halt 187,874; by_cause.the-market-data-does-not-make-sense 187,797; by_cause.the-autonomy-envelope-does-not-permit-trading 71; +5 more |
| `unattended-run-warden` | ONLY REFUSALS | part-fault 1,612 | nothing published — work: refused_unrecognised_fault 1,612 |
| `upstream-improvement-watch` | NOT MEASURED | nothing has reached it | nothing published — work: dependencies_declared 128 |
| `venue-outage-rider` | PRODUCES REAL OUTPUT | symbol-price-frame 11,552; feed-gap 8,564 | outage-state 107,425 — work: longest_silence_seconds 3,042,323; readings 107,425; quiet_markets 102,058; flowing 5,367 |


### Prediction (`prediction`) — 15 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `entropy-magnitude-forecaster` | PRODUCES REAL OUTPUT | flow-entropy 423,385; vol-feature-set 366,500 | volatility-forecast 2,429,220 — work: forecasts_made 423,334; forecasts_produced 345,962; outcomes_observed 249,456; measured_multiple_by_quintile.5 1; +4 more |
| `flow-entropy-meter` | PRODUCES REAL OUTPUT | order-flow-state 351,629 | flow-entropy 423,385 — work: uniform_fallback_rows_used 4,513,949; measurements 458,171; measured 383,477; window_seconds 120 |
| `forecast-distribution-gate` | PRODUCES REAL OUTPUT | kline-window 368,947; price-forecast 368,517 | forecast-out-of-distribution-flag 1,105,200 — work: checks 368,448; flagged_out_of_distribution 368,448 |
| `forecast-ensembler` | PRODUCES REAL OUTPUT | volatility-forecast 791,279; price-forecast 369,026; forecast-out-of-distribution-flag 368,467 | ensemble-forecast 1,064,615 — work: combinations 1,064,615 |
| `forecast-scorer` | ONLY REFUSALS | price-forecast 369,004; symbol-price-frame 11,605 | nothing published — work: unusable_forecasts_counted_not_scored 369,004 |
| `implied-vol-reader` | PRODUCES REAL OUTPUT | broker-option-greeks 463,712; market-quote 376,525; symbol-price-frame 11,609; +1 more | implied-vol-surface 549,729 — work: reads 184,644; surfaces_published 174,554; reads_too_thin_for_a_surface 10,090; options_feed_connected 1 |
| `kline-window-builder` | PRODUCES REAL OUTPUT | candle 370,295; corporate-action 19 | kline-window 2,583,501 — work: candles_observed 370,295; windows_built 369,105; windows_still_filling 282,740; gaps_found 154,147; +1 more |
| `kronos-finetuner` | WAITING FOR AN INPUT IT HAS NEVER SEEN | kline-window 369,026; accelerator-slot 129,948; retrain-request 50; +1 more | nothing published |
| `kronos-forecaster` | PRODUCES REAL OUTPUT | kline-window 368,992; accelerator-slot 129,917 | price-forecast 2,212,848 — work: forecasts_requested 368,992; model_is_loaded 1 |
| `kronos-size-selector` | NOT MEASURED | nothing has reached it | nothing published |
| `liquidation-cluster-mapper` | NOT MEASURED | nothing has reached it | nothing published |
| `model-drift-monitor` | NOT MEASURED | nothing has reached it | nothing published |
| `order-flow-state-encoder` | PRODUCES REAL OUTPUT | market-data 361,041; order-book-snapshot 352,264 | order-flow-state 352,396 — work: seconds_encoded 1,711,345; empty_seconds_encoded 1,631,293; by_state.(+0,5) 1,568,176; by_state.(+0,3) 41,660; +15 more |
| `realised-vol-regressor` | PRODUCES REAL OUTPUT | vol-feature-set 368,852 | volatility-forecast 2,122,195 — work: forecasts_made 368,852; features_used.close_to_close_long 159,270; features_used.close_to_close_medium 159,270; features_used.close_to_close_short 159,270; +14 more |
| `volatility-feature-builder` | PRODUCES REAL OUTPUT | kline-window 368,923; implied-vol-surface 184,500 | vol-feature-set 1,104,375 — work: sets_built 368,923; sets_with_an_implied_surface 23,906; complete_sets 19,585 |


### Skills (`skills`) — 15 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `book-and-paper-fetcher` | SERVING ITS PURPOSE | 41 skill-gaps worth fetching against, answered from Crossref then arXiv | 80 source-document published, `documents_returned` 10 of 10 fetches with `paywalled` **0** -- it was 0 documents and 10 paywalls until Crossref records with no abstract were made to fall through to arXiv |
| `community-chat-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `skill-composer` | NOT MEASURED | nothing has reached it | nothing published |
| `skill-conflict-detector` | NOT MEASURED | nothing has reached it | nothing published — work: checks 1,905 |
| `skill-distiller` | NOT MEASURED — blocked by the bootstrap cycle below | 22 source-document, the first this system has ever fetched | 88 llm-request published and 0 skills. It distils a document into a skill by asking a model, and no model can answer -- see the cycle below |
| `skill-gap-finder` | PRODUCES REAL OUTPUT | llm-request 210,939 | skill-gap 1,522,640 — work: gaps_open 380,660; by_reason.nothing-in-the-index-addresses-it 210,939 |
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
| `context-assembler` | WAITING FOR AN INPUT IT HAS NEVER SEEN | verified-snapshot 589,782 | nothing published |
| `decision-cost-accountant` | WAITING FOR AN INPUT IT HAS NEVER SEEN | trade-intent 65,061; usdt-pnl-statement 3 | nothing published |
| `golden-case-keeper` | WAITING FOR AN INPUT IT HAS NEVER SEEN | closed-trade 3 | nothing published |
| `knowledge-embedder` | WAITING FOR AN INPUT IT HAS NEVER SEEN | journal-entry 289,925 | nothing published |
| `part-token-budgeter` | WAITING FOR AN INPUT IT HAS NEVER SEEN | llm-quota-state 1,904; llm-spend-state 1,904 | nothing published |
| `prompt-drift-monitor` | NOT MEASURED | nothing has reached it | nothing published |
| `prompt-evaluator` | NOT MEASURED | nothing has reached it | nothing published |
| `prompt-promotion-gate` | NOT MEASURED — starved by the missing fetcher | nothing: `decisions` 0 | nothing. There is no version to promote |
| `prompt-registry` | NOT MEASURED — starved by the missing fetcher | nothing: `templates_seen` 0 | `versions_registered` 0, `purposes_with_an_active_version` 0. Nothing authors a template for it to register |
| `prompt-renderer` | NOT MEASURED — starved by the missing fetcher | 210,951 llm-request messages, real ones from live parts | nothing: all 210,951 `refused_no_active_version`. The requests are genuine and the registry has no active version to render them against, so every LLM call this system wants to make is dropped here |
| `prompt-template-author` | NOT MEASURED — starved by the missing fetcher | nothing at all: 0 research-finding, 0 skill, 0 prompt-score, 0 validated-llm-output | nothing. It writes a template once enough evidence names a purpose, and no evidence exists because the three web readers cannot fetch |
| `retrieval-index` | WAITING FOR AN INPUT IT HAS NEVER SEEN | retrieval-query 31,149 | nothing published |
| `retrieval-quality-scorer` | NOT MEASURED | nothing has reached it | nothing published |
| `retrieval-querier` | PRODUCES REAL OUTPUT | llm-request 210,943 | retrieval-query 31,148 — work: queries_made 31,148 |
| `structured-output-enforcer` | NOT MEASURED | nothing has reached it | nothing published |


### AI brain (`ai-brain`) — 14 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `bot-weight-sampler` | WAITING FOR AN INPUT IT HAS NEVER SEEN | market-regime 190,809 | nothing published |
| `brain-self-reflector` | WAITING FOR AN INPUT IT HAS NEVER SEEN | counter-argument 47,433; decision-rationale 47,433; premortem-note 47,433; +1 more | nothing published |
| `devils-advocate` | PRODUCES REAL OUTPUT | verified-snapshot 589,702; directional-opinion 118,063; trade-intent 64,989; +1 more | llm-request 189,612; counter-argument 142,209 — work: objections_raised 94,662; arguments_made 47,403; by_objection.the-conviction-is-the-model's-own-number-not-a-measured-frequency 47,403; by_objection.there-is-nowhere-this-trade-is-wrong 47,257; +1 more |
| `exploration-pair-opener` | PRODUCES REAL OUTPUT | market-regime 197,387; directional-opinion 121,565 | trade-intent 28 — work: pairs_by_regime.reverting 1; pairs_open_now 1; pairs_opened 1; most_expensive_experiment 0 |
| `forecast-bias-weigher` | WAITING FOR AN INPUT IT HAS NEVER SEEN | ensemble-forecast 1,064,222 | nothing published |
| `intent-explainer` | PRODUCES REAL OUTPUT | verified-snapshot 589,911; feature-attribution 149,501; directional-opinion 118,128; +1 more | decision-rationale 189,720; llm-request 189,720 — work: fully_supported 47,430; rationales_written 47,430; written_without_a_model 47,430 |
| `intent-timing-gate` | PRODUCES REAL OUTPUT | bear-entry-timing 149,620; trade-intent 65,034; symbol-price-frame 9,773; +1 more | timed-intent 94,866 — work: intents_timed 47,437; acted_now 47,436; by_reason.nothing-is-waiting-for-anything 47,436; by_reason.a-contributing-bot-is-waiting-for-a-trigger 1 |
| `market-thesis-reasoner` | WAITING FOR AN INPUT IT HAS NEVER SEEN | verified-snapshot 589,755; market-regime 197,387 | nothing published |
| `opinion-arbiter` | PRODUCES REAL OUTPUT | coverage-report 347,834; market-regime 197,387; directional-opinion 121,705; +4 more | trade-intent 947,236 — work: symbols_arbitrated 65,059; intents_formed 47,453; intents_by_agreement.all-bots-agree 42,454; intents_by_agreement.only-one-bot-had-a-view 4,870; +1 more |
| `opinion-conflict-resolver` | PRODUCES REAL OUTPUT | market-regime 197,387; directional-opinion 121,560; regime-memory 57,756 | conflict-ruling 70,372 — work: by_ruling.stand-aside 246 |
| `premortem-writer` | PRODUCES REAL OUTPUT | verified-snapshot 589,758; directional-opinion 118,074; trade-intent 64,999 | llm-request 189,652; premortem-note 94,826 — work: failure_modes_recorded 189,944; by_failure_mode.the-book-that-sized-this-is-not-there-when-it-is-exited 47,413; by_failure_mode.the-conviction-was-never-a-measured-frequency 47,413; by_failure_mode.the-regime-the-models-were-fitted-on-ends 47,413; +4 more |
| `setup-second-opinion-reasoner` | PRODUCES REAL OUTPUT | verified-snapshot 589,911; directional-opinion 84,057 | directional-opinion 428,985; llm-request 150,100 — work: confirmed 37,525; reviews_written 37,525 |
| `size-hint-writer` | PRODUCES REAL OUTPUT | bear-calibrated-conviction 146,220; trade-intent 65,028; bull-calibrated-conviction 3,281 | size-hint 47,433 — work: hints_written 47,433; sized_down_for_unmeasured_conviction 47,433; by_agreement.all-bots-agree 42,447; by_agreement.only-one-bot-had-a-view 4,855; +2 more |
| `strategy-review-reasoner` | WAITING FOR AN INPUT IT HAS NEVER SEEN | competence-map 68,268; closed-trade 3 | nothing published |


### Hardware resource governor (`resource-governor`) — 14 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `accelerator-scheduler` | NOT MEASURED | part-priority 128,248; hardware-capacity 1,909 | accelerator-slot 259,865 |
| `duty-cycle-planner` | PRODUCES REAL OUTPUT | part-resource-usage 464,688; market-data 386,295 | duty-cycle 708 — work: activity_by_hour.9 347,958; activity_by_hour.8 13,739; activity_by_hour.18 8,915; activity_by_hour.7 4,609; +8 more |
| `gate-actuator` | ONLY REFUSALS | switch-plan 1,828 | nothing published — work: plans_refused 4 |
| `hardware-scanner` | NOT MEASURED | nothing has reached it | hardware-capacity 9,540 — work: capacity.total_ram_bytes 31,530,676,224; capacity.available_ram_bytes 13,232,357,376; readings 1,909; capacity.logical_cpus 12; +3 more |
| `hog-detector` | PRODUCES REAL OUTPUT | part-resource-usage 465,273; hardware-capacity 1,909 | hog-report 222,590 — work: reports 111,460; by_part.keys_not_reported 59 |
| `io-pressure-meter` | NOT MEASURED | nothing has reached it | io-pressure 1,910 — work: pressure.network_transmit_bytes_per_second 635,615; pressure.network_receive_bytes_per_second 187,474; readings 1,915; pressure.some_stalled_10s 0 |
| `memory-pressure-forecaster` | PRODUCES REAL OUTPUT | part-resource-usage 464,832; hardware-capacity 1,908 | memory-forecast 1,761 — work: forecasts 1,766; samples_by_part.keys_not_reported 318 |
| `off-state-verifier` | WAITING FOR AN INPUT IT HAS NEVER SEEN | part-resource-usage 465,006 | nothing published |
| `part-appetite-meter` | NOT MEASURED | nothing has reached it | part-resource-usage 2,325,441 — work: parts_measured 465,273; readings 465,273 |
| `part-priority-reader` | NOT MEASURED | nothing has reached it | part-priority 384,608 — work: reads 1,887; stated 68 |
| `part-restart-budgeter` | PRODUCES REAL OUTPUT | part-fault 1,612 | restart-budget 235,770 — work: granted 896 |
| `resource-reservation-ledger` | PRODUCES REAL OUTPUT | part-priority 128,248; hardware-capacity 1,909 | resource-reservation 18,850 — work: memory_reserved 2,684,354,560; reservations 10; cpu_reserved 5 |
| `switch-oscillation-damper` | NOT MEASURED | nothing has reached it | nothing published |
| `switching-planner` | PRODUCES REAL OUTPUT | part-resource-usage 465,006; restart-budget 235,602; conservation-plan 200,589; +7 more | switch-plan 1,829 — work: plans 1,829; hog_reports_read 174 |


### Knowledge (`knowledge`) — 13 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `contradiction-detector` | NOT MEASURED | nothing has reached it | nothing published — work: checks 1,907 |
| `episode-embedder` | NOT MEASURED | nothing has reached it | nothing published — work: is_deterministic 1 |
| `episodic-trade-store` | NOT MEASURED | nothing has reached it | nothing published |
| `fact-provenance-tracker` | SERVING ITS PURPOSE | 22 source-document from the live readers | 44 messages published, where it had never published one. It came alive the moment a document existed to track the provenance of |
| `forgetting-curve-scheduler` | NOT MEASURED | nothing has reached it | nothing published — work: half_lives_days.listing-age-seconds 365; half_lives_days.tick-size 365; half_lives_days.funding-interval-seconds 180; half_lives_days.liquidation-behaviour 90; +5 more |
| `instruction-archive` | NOT MEASURED | nothing has reached it | nothing published |
| `knowledge-graph-linker` | NOT MEASURED | nothing has reached it | nothing published |
| `knowledge-pruner` | NOT MEASURED | nothing has reached it | nothing published |
| `knowledge-snapshot-versioner` | NOT MEASURED | nothing has reached it | knowledge-snapshot 2 — work: snapshots_taken 1 |
| `procedural-playbook` | NOT MEASURED | nothing has reached it | nothing published |
| `regime-memory-store` | PRODUCES REAL OUTPUT | market-regime 197,387; regime-transition-flag 3 | regime-memory 274,482 — work: too_few_occurrences 57,759 |
| `semantic-fact-store` | NOT MEASURED — starved by the missing fetcher | 289,926 messages | nothing: the facts it stores come from LLM output that is never produced |
| `symbol-profile-store` | PRODUCES REAL OUTPUT | market-data 385,755; order-book-snapshot 375,806 | symbol-profile 3,009,755 — work: absent_fields.funding-interval-seconds 371,298; absent_fields.tick-size 371,298; new_listings 371,298; profiles_built 371,298; +3 more |


### Universal opportunity scanner (`opportunity-scanner`) — 11 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `cointegration-pair-finder` | SKELETON | its own restored checkpoint | 5,949 symbols held, **5,512 holding a single price**, newest observation 2.4 days old, 7,814,961 pairs tested, 7,804,577 verdicts suppressed, board reads WORKING 1,619/s. No forgetting of any kind — no age bound, no subscription check. |
| `expiry-day-zero-to-hero-detector` | ONLY REFUSALS | broker-market-data 476,182; broker-option-greeks 465,578; broker-subscribed-instrument-listing 5,915; +1 more | nothing published — work: instruments_that_are_not_options 17 |
| `liquidity-grader` | PRODUCES REAL OUTPUT | market-data 386,334; order-book-snapshot 376,467 | liquidity-grade 999,275 — work: grades_computed 104,755; by_grade.thin 40,629; by_grade.untradeable 33,523; by_grade.tradeable 24,391; +4 more |
| `mean-reversion-detector` | SERVING ITS PURPOSE | 51,438 real in-session NIFTY/BANKNIFTY prints, 2026-09-04 | 0 candidates at the crypto-derived floor; **1,470** after it was re-derived from Indian economics. Fires now; the single global floor across a 1,721-symbol universe is still the open defect. |
| `momentum-burst-detector` | ONLY REFUSALS | symbol-profile 371,402; symbol-price-frame 11,276; training-label 26 | nothing published — work: not_a_burst 343,776; no_playbook_rule 5,745 |
| `news-catalyst-detector` | NOT MEASURED | nothing has reached it | nothing published |
| `regime-classifier` | PRODUCES REAL OUTPUT | symbol-price-frame 11,579 | market-regime 2,699,785 — work: classifications 2,438,270; unclassified 2,416,245; regimes_published 197,238; regime_refreshes 111,257; +6 more |
| `spread-reversion-detector` | PRODUCES REAL OUTPUT | symbol-price-frame 11,433; cointegrated-pair 10,828; symbol-quote-frame 7,968; +1 more | entry-candidate 6,105 — work: tests 1,938,178; leg_priced_from_a_trade 1,689,844; stale_leg 1,360,063; leg_quote_too_wide 452,372; +4 more |
| `universal-symbol-sweeper` | ONLY REFUSALS | liquidity-grade 366,416; symbol-universe 144,967; cross-segment-signal 118,473; +3 more | nothing published — work: skipped_untradeable 562,131; skipped_unmeasurable 111,188; skipped_already_held 10,939 |
| `volatility-gap-detector` | PRODUCES REAL OUTPUT | volatility-forecast 792,057; implied-vol-surface 184,532; training-label 26 | entry-candidate 829,933 — work: tests 631,112; candidates 148,250; implied_rich 148,250 |
| `watch-condition-compiler` | NOT MEASURED | nothing has reached it | nothing published |


### Hypothesis (`hypothesis`) — 11 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `expectancy-decomposer` | WAITING FOR AN INPUT IT HAS NEVER SEEN | horizon-profile 9,530; pnl-attribution 2 | nothing published |
| `hypothesis-deduplicator` | NOT MEASURED | nothing has reached it | nothing published |
| `hypothesis-falsifier` | NOT MEASURED | nothing has reached it | nothing published |
| `hypothesis-mutator` | WAITING FOR AN INPUT IT HAS NEVER SEEN | near-miss-episode 19,730 | nothing published |
| `hypothesis-ranker` | NOT MEASURED | nothing has reached it | nothing published — work: maximum_reachable_trades 10,000; rankings 1,904 |
| `hypothesis-regime-tagger` | WAITING FOR AN INPUT IT HAS NEVER SEEN | market-regime 197,387 | nothing published |
| `instruction-retirer` | NOT MEASURED | nothing has reached it | nothing published |
| `instruction-writer` | NOT MEASURED — starved by the missing fetcher | 67,289 messages | nothing: `written` 0, `requests` 0, `instructions_live` 0 |
| `loss-inverter` | NOT MEASURED | nothing has reached it | nothing published |
| `power-estimator` | NOT MEASURED | nothing has reached it | nothing published — work: maximum_testable_trades 10,000; power 1 |
| `symbolic-hypothesis-miner` | WAITING FOR AN INPUT IT HAS NEVER SEEN | kline-window 369,011; training-label 26 | nothing published |


### Execution and venue adapter (`execution-venue-adapter`) — 11 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `ccxt-order-router` | NOT MEASURED | nothing has reached it | nothing published |
| `limit-price-walker` | NOT MEASURED — nothing to walk | 352,284 market-data, 343,649 order-book-snapshot, 2,091 order-request | `orders_walking` 0 and nothing published, because every order this system places is a market order (`order_type: market` on every row of the lifecycle journal). It cannot be judged until a limit order exists. Its own `limit_walk_prior_step_fraction` is still the crypto 0.0002 against a measured Indian half spread of 0.3175% |
| `order-not-found-debouncer` | NOT MEASURED | nothing has reached it | nothing published |
| `order-reject-classifier` | NOT MEASURED | nothing has reached it | nothing published |
| `order-resubmitter` | NOT MEASURED | nothing has reached it | nothing published |
| `order-state-poller` | NOT MEASURED | nothing has reached it | nothing published |
| `resting-order-cancel-policy` | NOT MEASURED — nothing to judge | 10,359 symbol-price-frame, 2,121 order-request | `decisions` 0. It decides whether a resting order has drifted too far to keep, and market orders do not rest. Same gate as limit-price-walker and untestable for the same reason |
| `venue-balance-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `venue-order-status-translator` | NOT MEASURED | nothing has reached it | nothing published |
| `venue-position-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `venue-rate-budgeter` | NOT MEASURED | nothing has reached it | nothing published |


### Online research (`online-research`) — 11 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `arxiv-feed-reader` | SERVING ITS PURPOSE | 293 skill-gaps, searched against arXiv's own public API, sorted by relevance | 8 source-document published, 1 paper fetched, 9 already held, 0 failures. `refused_no_search_installed` fell from 380,660 to **0**. Sorted by date it answered 'options implied volatility' with an astronomy preprint; by relevance it returns option-pricing papers |
| `copy-latency-estimator` | WAITING FOR AN INPUT IT HAS NEVER SEEN | symbol-price-frame 9,816 | nothing published |
| `copy-worthiness-scorer` | NOT MEASURED | nothing has reached it | nothing published |
| `edge-comparator` | NOT MEASURED | nothing has reached it | nothing published |
| `exchange-announcement-reader` | SKELETON | 102,731 broker-instrument-listing messages | nothing, ever: `read_rows` observes the universe and returns `()`. Same shape as options-flow-reader -- a reader with no source wired to it |
| `github-strategy-miner` | SKELETON | 380,660 messages | nothing, ever, and it has never seen an input it declares. Same missing fetcher |
| `leaderboard-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `onchain-position-reader` | NOT MEASURED | nothing has reached it | nothing published |
| `options-flow-reader` | SKELETON | 135,175 symbol-universe messages | nothing, ever, and by construction: `read_rows` returns `()` and the docstring says 'no options flow feed is connected on this box'. It is honestly empty rather than decorative -- but the data now exists here (the tape carries an `open_interest` stream per contract, and operate/nse_fo_bhavcopy.py carries volume and OI for every NSE option), so this is buildable rather than blocked |
| `strategy-decoder` | NOT MEASURED | nothing has reached it | nothing published |
| `trader-record-verifier` | NOT MEASURED | nothing has reached it | nothing published |


### Observability (`observability`) — 10 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `ablation-harness` | NOT MEASURED | nothing has reached it | nothing published |
| `alert-raiser` | PRODUCES REAL OUTPUT | feed-coverage 714,887; market-anomaly 621,376; trading-halt 206,873; +3 more | alert 7,753 — work: suppressed_duplicates 537,248; raised 7,753; by_severity.high 7,569; active 4,501; +9 more |
| `board-publisher` | PRODUCES REAL OUTPUT | board-snapshot 1,896 | board-link 113 — work: publishes 113 |
| `board-snapshot-builder` | PRODUCES REAL OUTPUT | feed-coverage 923,596; competence-map 68,246; alert 19,200; +13 more | board-snapshot 3,792 — work: tiles_offered 1,604,170; by_state.OK 1,326,841; by_state.NOT MEASURED 277,329; snapshots_built 1,896 |
| `clock-skew-monitor` | PRODUCES REAL OUTPUT | broker-market-data 476,141 | alert 6,906 — work: alerts_raised 6,906; venues_watched 1 |
| `drawdown-episode-tracker` | NOT MEASURED — silence is the correct state | 17,808 account-balance messages, real per-segment paper balances | nothing published, and that is right: it publishes a `drawdown-episode` only when an episode happens, and no position has drawn down. Judging it needs a losing trade, not another probe |
| `fund-conservation-auditor` | NOT MEASURED — silence is the correct state | 260,343 journal-entry and 321 fill messages | no `alert`, which is what funds being conserved looks like. It only publishes a breach. What is untested is whether it WOULD fire -- that needs a deliberately unbalanced entry, which no probe here has fed it |
| `heartbeat-collector` | NOT MEASURED | nothing has reached it | heartbeat-table 6 — work: reports_received 589,478; tables_built 3,316; parts_expected 324; reporting 319; +4 more |
| `probe-runner` | PRODUCES REAL OUTPUT | journal-gap 5; heartbeat-table 3 | probe-result 14,128 — work: runs 8,559; measured 8,553; slowest_seconds 631; probes_registered 16; +1 more |
| `stale-board-watch` | PRODUCES REAL OUTPUT | board-snapshot 1,896; board-link 113 | alert 1 — work: checks 3,075; longest_lag_seconds 1; alerts_raised 1 |


### LLM services (`llm-services`) — 10 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `ground-truth-snapshot-builder` | PRODUCES REAL OUTPUT | market-data 386,382; order-book-snapshot 376,379 | verified-snapshot 4,129,384 — work: snapshots_built 589,912 |
| `llm-backpressure-gauge` | PRODUCES REAL OUTPUT | survival-tier 201,156; llm-quota-state 1,904; llm-spend-state 1,904 | llm-backpressure 192,047 — work: readings 192,047; times_bound_by_quota 191,546; times_open 191,546; times_bound_by_survival_tier 500; +1 more |
| `llm-model-picker` | NOT MEASURED — starved by the missing fetcher | 210,943 messages | nothing: `choices_made` 0, `models_declared` 0 |
| `llm-request-router` | NOT MEASURED — starved by the missing fetcher | 195,832 messages | nothing: it never sees a rendered request, because prompt-renderer publishes none |
| `llm-response-cache` | NOT MEASURED | nothing has reached it | nothing published |
| `local-model-caller` | NOT MEASURED | nothing has reached it | nothing published |
| `metered-api-caller` | NOT MEASURED | nothing has reached it | nothing published |
| `paid-spend-ledger` | NOT MEASURED | nothing has reached it | llm-spend-state 7,611 — work: ceiling 1 |
| `subscription-quota-watch` | NOT MEASURED | nothing has reached it | llm-quota-state 7,611 — work: readings 1,904 |
| `subscription-session-caller` | NOT MEASURED | nothing has reached it | nothing published |


### Backtesting (`backtesting`) — 10 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `backtest-scorer` | NOT MEASURED | nothing has reached it | nothing published — work: confidence_multiple 2 |
| `execution-cost-model` | PRODUCES REAL OUTPUT | market-data 383,151 | cost-estimate 7,841,635 — work: estimates_made 1,572,806; unfitted_estimates 1,572,806; quantile 1 |
| `fill-volume-capper` | PRODUCES REAL OUTPUT | historical-window 39,693 | fillable-size 214,697 — work: requests 215,395; filled_in_full 168,552; total_fillable 168,552; total_intended 168,552; +1 more |
| `historical-bar-store` | PRODUCES REAL OUTPUT | candle 222,127 | historical-window 158,500 — work: windows_built 487,469; windows_with_gaps 487,469; duplicate_bars 209,977; windows_published 39,625; +4 more |
| `instruction-promotion-gate` | NOT MEASURED | nothing has reached it | nothing published |
| `instruction-replayer` | WAITING FOR AN INPUT IT HAS NEVER SEEN | cost-estimate 1,560,714; fill-sequence 214,770; fillable-size 214,651; +1 more | nothing published |
| `intra-bar-fill-sequencer` | PRODUCES REAL OUTPUT | cost-estimate 1,569,226; historical-window 39,687 | fill-sequence 214,770 — work: ambiguous 215,349; bars_sequenced 215,349; ambiguous_fraction 1 |
| `live-vs-replay-reconciler` | NOT MEASURED | nothing has reached it | nothing published |
| `lookahead-auditor` | NOT MEASURED | nothing has reached it | nothing published |
| `walk-forward-splitter` | ONLY REFUSALS | historical-window 39,693 | nothing published — work: refused_gappy_windows 39,693 |


### Paper trading on live data (`paper-live-trading`) — 10 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `book-walk-fill-pricer` | PRODUCES REAL OUTPUT | order-book-snapshot 376,429; order-request 2,430 | fill-price-estimate 2,416 — work: estimates 2,416; complete_fills 2,403; partial_fills 13 |
| `live-switch-guard` | PRODUCES REAL OUTPUT | money-mode 5,607; closed-trade 3 | risk-limit 3,929 — work: days_traded 0 |
| `money-mode-reader` | NOT MEASURED | nothing has reached it | money-mode 61,725 |
| `order-destination-router` | PRODUCES REAL OUTPUT | money-mode 5,622; bounded-order 281; stamped-order 281; +1 more | order-request 14,400 — work: routed_to_paper 2,400; slices_routed 2,400; orders_split 229 |
| `order-idempotency-stamper` | PRODUCES REAL OUTPUT | bounded-order 281 | stamped-order 562 — work: stamped 281; distinct_ids 5 |
| `order-latency-simulator` | PRODUCES REAL OUTPUT | money-mode 5,622; order-request 2,430 | delayed-order-request 4,485 — work: orders_released 2,055; longest_delay_seconds 0 |
| `paper-account-keeper` | SERVING ITS PURPOSE | the three built segments' real accounts | index-options/stock-options/cash-equity accounts present and correct. The retired `paper-account-futures` component holding BTCUSDT on binance-usdm was removed 2026-09-06 and did not return through a restart. |
| `paper-fill-simulator` | PRODUCES REAL OUTPUT | cost-estimate 1,570,547; market-data 386,295; feed-jump 18,253; +5 more | fill 4,316 — work: fees_charged 11,393; feed_jumps_cleared 2,566; fills_priced_as_options 332; partially_filled 176; +5 more |
| `paper-liquidation-simulator` | NOT MEASURED | nothing has reached it | nothing published |
| `stop-order-manager` | PRODUCES REAL OUTPUT | position 50,052; stop-adjustment 10,581; money-mode 5,623 | order-request 180 — work: resized_to_the_position 107; replaced 22; stops_resting 14; restored_symbols 12; +2 more |


### Bull bot (`bull-bot`) — 10 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `bull-conviction-calibrator` | PRODUCES REAL OUTPUT | market-regime 197,794; bull-raw-conviction 3,281; training-label 26 | bull-calibrated-conviction 13,124 — work: convictions_calibrated 3,281; passed_through_unfitted 3,281 |
| `bull-conviction-model` | PRODUCES REAL OUTPUT | kline-window 368,988; price-forecast 368,443; forecast-out-of-distribution-flag 368,357; +6 more | bull-raw-conviction 6,562 — work: challenger.observations 97,260; champion.observations 97,260; labels_trained_on 97,260; challenger.positives 48,130; +8 more |
| `bull-entry-timer` | PRODUCES REAL OUTPUT | symbol-price-frame 10,681; bull-side-candidate 3,445; bull-calibrated-conviction 3,281 | bull-entry-timing 6,750 — work: decisions 3,375; entered_now 3,375; detectors_with_an_entry_quality_record 2 |
| `bull-exit-plan-proposer` | SERVING ITS PURPOSE — starved by a crypto-length horizon | 2,141 plan requests on live NSE options, real symbol-price-frame prints | 290 plans built, all from the live range; 1,406 refused for no excursion record and 445 for too few prints. The plans it does build are right; the refusals are the detectors' 60s horizon against an option that prints every 9.25s (measurements/2026-09-07-exit-plan-starvation/) |
| `bull-feature-builder` | PRODUCES REAL OUTPUT | broker-open-interest 465,646; order-book-snapshot 376,547; symbol-profile 371,423; +3 more | bull-feature-vector 17,225 — work: vectors_built 3,445; book_snapshots_absent 515 |
| `bull-opinion-composer` | PRODUCES REAL OUTPUT | bull-feature-vector 3,445; bull-entry-timing 3,375; bull-calibrated-conviction 3,281; +1 more | directional-opinion 17,887 — work: opinions_composed 1,459; calls_to_act 190 |
| `bull-outlier-rejector` | PRODUCES REAL OUTPUT | bull-feature-vector 3,445 | bull-feature-out-of-distribution-flag 3,445 — work: vectors_judged 614,035; flagged_out_of_distribution 166; checkpoints_written 136; features_with_a_learned_normal 14 |
| `bull-position-invalidation-watcher` | PRODUCES REAL OUTPUT | market-data 386,276; position 49,922; bull-feature-vector 3,445 | directional-opinion 538,753 — work: by_reason.past-the-horizon-it-was-given 43,360; checks 43,360; close_calls 43,360; positions_watched 1; +1 more |
| `bull-setup-filter` | PRODUCES REAL OUTPUT | entry-candidate 153,052; bull-setup-weight 1,882 | bull-side-candidate 10,335 — work: accepted 3,445; accepted_by_detector.mean-reversion-detector 2,768; accepted_by_detector.spread-reversion-detector 677; setup_weights_learned 2 |
| `bull-setup-weight-learner` | PRODUCES REAL OUTPUT | training-label 26 | bull-setup-weight 1,884 — work: weights_published 1,884; trades_learned_from 8; weights.spread-reversion-detector 1; weights.unknown 1; +1 more |


### Bear bot (`bear-bot`) — 10 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `bear-conviction-calibrator` | PRODUCES REAL OUTPUT | market-regime 197,794; bear-raw-conviction 146,220; training-label 26 | bear-calibrated-conviction 524,515 — work: convictions_calibrated 146,220; fell_back_to_the_overall_record 146,220; passed_through_unfitted 146,220 |
| `bear-conviction-model` | PRODUCES REAL OUTPUT | kline-window 369,029; price-forecast 368,991; forecast-out-of-distribution-flag 368,429; +6 more | bear-raw-conviction 292,394 — work: convictions_formed 146,197; challenger.observations 45,836; champion.observations 45,836; labels_trained_on 45,836; +6 more |
| `bear-entry-timer` | PRODUCES REAL OUTPUT | bear-side-candidate 149,787; bear-calibrated-conviction 146,439; symbol-price-frame 10,395 | bear-entry-timing 299,298 — work: decisions 149,649; entered_now 16,640; detectors_with_an_entry_quality_record 3 |
| `bear-exit-plan-proposer` | SERVING ITS PURPOSE — starved by a crypto-length horizon | 68,443 plan requests on live NSE options | 43 plans built; 57,702 refused 'too-few-prints-in-the-window' (84%) and 10,698 for no excursion record. The 20-print bar is statistically right — it recovers 92.9% of the true range at the median, against 80% at ten — so the number to re-derive is the horizon, not the bar |
| `bear-feature-builder` | PRODUCES REAL OUTPUT | broker-open-interest 416,093; order-book-snapshot 341,297; symbol-profile 338,516; +3 more | bear-feature-vector 744,305 — work: vectors_built 148,861; book_snapshots_absent 12,604 |
| `bear-opinion-composer` | PRODUCES REAL OUTPUT | bear-entry-timing 149,468; bear-feature-vector 148,861; bear-calibrated-conviction 146,197; +1 more | directional-opinion 478,127 — work: opinions_composed 39,188; calls_to_act 8 |
| `bear-outlier-rejector` | PRODUCES REAL OUTPUT | bear-feature-vector 148,884 | bear-feature-out-of-distribution-flag 148,884 — work: vectors_judged 2,203,609; flagged_out_of_distribution 2,655; features_with_a_learned_normal 15 |
| `bear-position-invalidation-watcher` | WAITING FOR AN INPUT IT HAS NEVER SEEN | market-data 386,381; bear-feature-vector 148,883; position 50,052 | nothing published |
| `bear-setup-filter` | PRODUCES REAL OUTPUT | entry-candidate 153,264; bear-setup-weight 1,100 | bear-side-candidate 387,067 — work: accepted 149,819; accepted_by_detector.volatility-gap-detector 148,339; accepted_by_detector.mean-reversion-detector 1,088; accepted_by_detector.spread-reversion-detector 392; +1 more |
| `bear-setup-weight-learner` | PRODUCES REAL OUTPUT | training-label 26 | bear-setup-weight 1,100 — work: weights_published 1,100; trades_learned_from 18; tail_ratios.spread-reversion-detector 1; weights.spread-reversion-detector 1 |


### Profit tailgating bot (`profit-tailgating-bot`) — 9 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `tail-copy-selector` | NOT MEASURED | nothing has reached it | nothing published |
| `tail-crowding-detector` | WAITING FOR AN INPUT IT HAS NEVER SEEN | broker-open-interest 465,543; order-book-snapshot 376,466; broker-subscribed-instrument-listing 5,915 | nothing published |
| `tail-follow-conviction-model` | WAITING FOR AN INPUT IT HAS NEVER SEEN | retrain-request 50; sample-weight 26; training-label 26; +1 more | nothing published |
| `tail-move-remaining-estimator` | WAITING FOR AN INPUT IT HAS NEVER SEEN | price-forecast 368,987; symbol-price-frame 10,114 | nothing published |
| `tail-mover-qualifier` | ONLY REFUSALS | symbol-profile 371,376; entry-candidate 153,154; symbol-price-frame 10,210 | nothing published — work: rejected_by_reason.one-print-is-not-a-move 144,993; rejected_by_reason.move-has-not-run-far-enough-to-be-established 8,152; rejected_by_reason.spread-costs-more-than-the-move-has-left 6; rejected_by_reason.move-has-gone-further-than-this-symbol-normally-goes 3 |
| `tail-opinion-composer` | NOT MEASURED | nothing has reached it | nothing published |
| `tail-setup-weight-learner` | NOT MEASURED | nothing has reached it | nothing published |
| `tail-trailing-exit-planner` | WAITING FOR AN INPUT IT HAS NEVER SEEN | symbol-profile 371,410; excursion-profile 172,913; position 50,052; +1 more | nothing published |
| `tail-winner-selector` | ONLY REFUSALS | market-data 386,262; position 50,052; peak-excursion 3,652 | nothing published — work: rejected_by_reason.the-pair-experiment-has-not-resolved 768,778 |


### Broker adapter (Indian markets) (`broker-adapter`) — 9 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `broker-account-funds-reader` | PRODUCES REAL OUTPUT | broker-token-standing 1,912 | broker-account-funds 128 — work: reads 64 |
| `broker-history-reader` | ONLY REFUSALS | broker-instrument-listing 109,718; broker-token-standing 1,914; market-session-state 32 | nothing published — work: skipped_because_the_market_is_open 7,072; instruments_awaiting_a_window 2,966; skipped_because_the_session_is_unknown 1 |
| `broker-instrument-catalogue-reader` | NOT MEASURED | nothing has reached it | broker-instrument-listing 877,818 — work: listings_restated 109,775; listings_per_second 57; cycles_completed 1 |
| `broker-margin-quoter` | PRODUCES REAL OUTPUT | broker-market-data 470,768; symbol-universe 183,625; broker-token-standing 1,914 | broker-margin-requirement 25 — work: calls_made 19,073; quotes_read 783 |
| `broker-market-feed-reader` | PRODUCES REAL OUTPUT | symbol-universe 153,744; broker-instrument-listing 109,653; broker-token-standing 1,912 | broker-market-data 3,044,122; broker-open-interest 1,961,875; broker-option-greeks 1,587,559; +3 more — work: decoded_messages 11,245; subscribed_instruments 1,737; connected 1 |
| `broker-market-tape-writer` | PRODUCES REAL OUTPUT | broker-candle 554,940; broker-market-data 349,808; broker-order-book-snapshot 348,254; +2 more | nothing published — work: records_written 1,943,410; open_tapes 9,794 |
| `broker-price-level-sampler` | PRODUCES REAL OUTPUT | broker-market-data 476,007 | broker-price-frame 17,601 — work: updates_observed 461,748; frames_published 5,867 |
| `broker-token-refresh-scheduler` | NOT MEASURED | nothing has reached it | broker-token-standing 9,570 — work: has_token 1; is_valid 1 |
| `subscribed-instrument-listing-filter` | PRODUCES REAL OUTPUT | broker-instrument-listing 109,749; broker-subscription-state 1,799 | broker-subscribed-instrument-listing 76,934 — work: listings_restated 5,918; subscribed_instruments 1,737; listings_per_second 6; cycles_completed 2 |


### Capital desk (`capital-desk`) — 8 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `allocation-conservation-checker` | PRODUCES REAL OUTPUT | capital-allotment 5,775; main-account-setting 1,900 | alert 4,548; allocation-headroom 4,543 — work: checks 4,551; alerts_raised 4,548 |
| `allocation-rebalance-proposer` | PRODUCES REAL OUTPUT | capital-utilisation 5,583; usdt-pnl-statement 3 | allocation-proposal 1,844 — work: rounds 1,847 |
| `capital-settings-change-recorder` | PRODUCES REAL OUTPUT | capital-allotment 5,790; leverage-ceiling 5,790; trade-capital-bounds 5,790; +1 more | journal-entry 250,055 — work: changes_recorded 25,085; by_setting.keys_not_reported 17; first_readings 17 |
| `capital-settings-validator` | PRODUCES REAL OUTPUT | instrument-choice 64,513; capital-allotment 5,793; leverage-ceiling 5,793; +2 more | capital-settings-verdict 5,694 — work: consistent 1,903; judgements 1,903 |
| `capital-utilisation-meter` | PRODUCES REAL OUTPUT | account-balance 18,785; locked-allocation 6,258; capital-allotment 5,778 | capital-utilisation 11,172 — work: measurements 5,586; segments_measured 3 |
| `live-balance-divergence-watch` | NOT MEASURED — silence is the correct state | 17,805 account-balance, 5,433 capital-allotment, 5,276 money-mode | no `alert`. It publishes only when the paper balance and the broker's diverge, and on paper there is no broker balance to diverge from. Correct, and untested for whether it would fire |
| `main-account-settings-reader` | NOT MEASURED | nothing has reached it | main-account-setting 11,420 — work: balance 150,000,000; reads 1,905; can_size_a_real_trade 1 |
| `paper-currency-converter` | NOT MEASURED — nothing to convert | account-balance and capital-allotment on real per-segment balances | nothing published. Every segment states `quote_currency` INR and the account settles in INR, so there is no currency pair to convert. It would only ever fire on a segment settling in something else |


### Portfolio and position state (`portfolio-state`) — 7 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `cost-basis-tracker` | PRODUCES REAL OUTPUT | fill 332 | cost-basis 49,542 — work: restored_symbols 12; residues_released_with_the_side 2; checkpoint_restored 1; overshoots_too_small_to_reverse 1 |
| `fill-reconciler` | PRODUCES REAL OUTPUT | fill 332 | position 800,256 — work: checks 50,081; fills_applied 332; symbols 29 |
| `fund-lock-ledger` | PRODUCES REAL OUTPUT | account-balance 18,785; fill 332; bounded-order 281; +1 more | locked-allocation 12,526 — work: balance 5,000,000; free_balance 4,035,022; locked_total 964,978; double_locks_prevented 153; +2 more |
| `liquidation-price-tracker` | NOT MEASURED | nothing has reached it | nothing published |
| `peak-excursion-tracker` | PRODUCES REAL OUTPUT | market-data 386,382; position 50,052; cost-basis 24,754 | peak-excursion 36,550 — work: prices_without_cost_basis 382,727; positions_closed 28,394; restored_symbols 13 |
| `position-close-detector` | PRODUCES REAL OUTPUT | position 50,052; peak-excursion 3,655; fill 332 | closed-trade 63 — work: open_symbols 14; restored_symbols 12; trades_closed 3; residues_absorbed_into_the_close 2; +3 more |
| `usdt-pnl-accountant` | PRODUCES REAL OUTPUT | market-data 386,382; cost-basis 24,754; capital-allotment 5,793; +2 more | usdt-pnl-statement 12 — work: net_total_usdt 20,811; non_usdt_converted 3; statements 3; statements_without_capital 3 |


### Ledger and audit trail (`ledger`) — 6 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `control-recorder` | PRODUCES REAL OUTPUT | policy-decision 47,403; knowledge-snapshot 1 | journal-entry 474,039 — work: recorded 47,404; policy_decisions 47,403; knowledge_snapshots 1 |
| `funding-settlement-recorder` | NOT MEASURED | nothing has reached it | nothing published |
| `journal-integrity-checker` | PRODUCES REAL OUTPUT | journal-entry 290,029 | journal-gap 10 — work: entries_checked 290,029; checks 139,407; chain_breaks 5 |
| `learning-recorder` | PRODUCES REAL OUTPUT | decision-rationale 47,457; pnl-attribution 2 | journal-entry 474,590 — work: recorded 47,459; by_kind.decision-rationale 47,457; by_kind.pnl-attribution 2 |
| `position-recorder` | PRODUCES REAL OUTPUT | position 50,052; stop-adjustment 10,567; peak-excursion 3,655; +1 more | journal-entry 2,312 — work: recorded 252; changed 107; excursions 78; opened 31; +3 more |
| `trade-lifecycle-recorder` | PRODUCES REAL OUTPUT | entry-candidate 153,054; trade-intent 64,988; order-request 2,430; +2 more | journal-entry 1,697,020 — work: recorded 169,702; stage_counts.entry-candidate 101,941; stage_counts.trade-intent 64,988; stage_counts.order-request 2,430; +4 more |


### Segment bot (bull, bear, profit tailgating) (`segment-bot`) — 1 parts

| part | verdict | fed in | came out |
|---|---|---|---|
| `instrument-selector` | SERVING ITS PURPOSE | live broker-market-data, broker-option-greeks, liquidity-grade and symbol-universe on real NSE instruments | 3,122 of 3,122 intents chosen with 0 refused and 0 without a price for the contract, after the price and cost-threshold fixes of 2026-09-07; before them, 753 of 2,257 chosen and 84.5% of those carrying no price for the contract they named |

