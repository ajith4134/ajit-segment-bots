# Six Instagram posts, assessed against the foundation blocks

Read 2026-08-20: 45 carousel images, 6 carousel videos, 30 frames extracted from
those videos, 41 reel frames, one Whisper transcript. Every falsifiable claim
checked with `gh` rather than repeated.

**Verdict up front:** four of the six carry something usable. Two do not.

---

## 1. Reel — a complete volatility signal, feature by feature

`reel/Dby33G4thmA` · josholdmixon · 73 transcript cues, formulas on the board

The most valuable item in the set. It walks the whole path from idea to signal
without a single profit claim.

**The idea:** don't predict direction, trade *how much* the market moves.
**The target:** estimate 10-day realised volatility, `E[RV₁₀]`.
**The signal:** compare it to 30-day implied volatility.

    E[RV₁₀] > IV₃₀   options are pricing too little movement
    E[RV₁₀] < IV₃₀   options are pricing too much

**Ten features**, combined in a linear regression:

| # | feature | formula where given |
|---|---|---|
| 1 | 1-day realised vol | `RV₁ = √(252 Σᵢ rᵢ²)` |
| 2 | vol persistence | does today's vol survive |
| 3 | longer-horizon typical vol | |
| 4 | downside variance, 5-day | is the vol coming from sell-offs |
| 5 | jump variance | `RV = Σrᵢ²`, `BV = (π/2)Σ\|rᵢ\|\|rᵢ₋₁\|`, `JumpVar = max(RV−BV, 0)` |
| 6 | vol-of-vol | 40% steady and 20%→60% are not the same 40% |
| 7 | 30-day ATM implied vol | `C_market = BS(S,K,T,…)`, solve for `σ` |
| 8 | IV term structure | `IV₃₀ − IV₆₀`, near-term event pricing |
| 9 | put skew | `IV₂₅Δput − IV_ATM`, what fear costs |
| 10 | market vol | S&P 5-day realised |

**Where it lands:** `prediction` (a feature set that is not Kronos and can be
scored against it), `hypothesis` (this is exactly what an opportunity instruction
looks like when written out), and the **options** segment.

**Honest limit:** features 7–9 need an options surface. In crypto that means
Deribit or similar. Spot and futures can compute 1–6 and 10 from candles alone;
they cannot compute IV, term structure or skew without an options venue. That
split is real and should not be papered over.

---

## 2. Agent memory — episodic, semantic, procedural

`p/Db9xNeMmJ5g` · 20 pages · datasciencebrain

Captioned as a RAG rant. It is actually a complete build guide with verified
dependency versions (langgraph 1.2.11, sqlite-vec 0.1.9,
sentence-transformers 5.7.0, `all-MiniLM-L6-v2` at 384 dims, ~90 MB).

Three memory tiers, and the differences are the design:

| | semantic | episodic | procedural |
|---|---|---|---|
| holds | this customer's facts | resolved cases: symptom, what was tried, root cause, fix | the playbook |
| scope | one customer | everyone | the whole desk |
| indexed | yes | yes | **no** |
| read | top-K by similarity | top-K above a floor | **all of it, every turn** |
| write | upsert, supersedes contradictions | **append-only, immutable** | replace a rule, version bump, capped |
| trigger | any turn | **on resolution** | only on human feedback |

Two lines carry the whole thing:

> **Procedural memory is never searched. It is always in the prompt.** That makes
> it a fixed token tax on every turn, which is why you cap it, and why it holds
> *behaviour* only and never diagnosis.

> **Episodic memory is global while semantic memory is per customer.**

**Where it lands — this is the strongest structural find.** It maps onto blocks
the user has named but not described:

- **episodic** ≙ closed trades. Append-only, written *on resolution*. That is
  precisely `closed-trade-decoding`'s input, and it says the record should be
  immutable.
- **semantic** ≙ per-symbol facts, upserted, contradictions superseded.
- **procedural** ≙ the playbook that is always injected and never searched. That
  is what an **opportunity instruction** is — and the tier's rules say the
  scanner's instruction set must be **capped**, must hold behaviour rather than
  diagnosis, and changes only on deliberate approval.

If `knowledge` needs a shape, this is a tested one.

---

## 3. Three reference sheets — mean reversion, neural nets, stat arb

`p/DbPaMI1ndH2` · 3 dense single-page sheets

- **Mean reversion** — z-score `Z = (Price − Mean)/StdDev`, entry at `|Z| > 2`,
  exit at `|Z| < 0.5`, Bollinger, RSI 70/30, and — more useful — *when it fails*:
  strong trends, news spikes, low liquidity, structural breaks.
- **Statistical arbitrage** — cointegration, `Spread = A − βB`, Ornstein-Uhlenbeck
  `dXₜ = κ(μ − Xₜ)dt + σdWₜ`, ADF/KPSS, Engle-Granger, Johansen, half-life.
- **Neural networks in trading** — architectures, input features, evaluation
  metrics, and a best-practice list worth lifting wholesale: walk-forward
  validation, time-based splits, avoid look-ahead bias, always evaluate
  out-of-sample **with transaction costs**.

**Where it lands:** the first two are catalogues of *opportunity instruction
types* for the scanner — mean reversion and pairs trading are both "a condition
plus what it implies". The third is a checklist for `forecast-scorer`, which
currently has no stated method.

---

## 4. Ten repositories — verified, and the licences are the story

`p/DbdYc4MCeNi` · 13 slides

All ten exist. Checked with `gh` on 2026-08-20:

| repo | stars | licence | last push |
|---|---|---|---|
| TauricResearch/TradingAgents | 99,004 | Apache-2.0 | 2026-07-18 |
| freqtrade/freqtrade | 53,453 | **GPL-3.0** | 2026-08-20 |
| ccxt/ccxt | 43,672 | **MIT** | 2026-08-20 |
| nautechsystems/nautilus_trader | 26,600 | LGPL-3.0 | 2026-08-20 |
| mementum/backtrader | 22,901 | **GPL-3.0** | **2024-08-19** |
| hummingbot/hummingbot | 19,519 | Apache-2.0 | 2026-08-20 |
| AI4Finance-Foundation/FinRL | 16,048 | MIT | 2026-07-13 |
| polakowo/vectorbt | 8,732 | *other* | 2026-08-02 |
| Lumiwealth/lumibot | 1,948 | GPL-3.0 | 2026-08-13 |
| Polymarket/py-clob-client | 1,232 | MIT | 2026-05-25 |

**The post never mentions licences. It turned out not to matter here** — see
D-003: this is a personal project that is not distributed, and GPL and LGPL
obligations attach to distribution. Running and modifying them privately triggers
nothing, and a private repository is a backup rather than a distribution. Licence
is recorded above as a fact about each project; it constrains nothing today.

**So the ranking below is on merit alone.**

### 1. CCXT — the single most actionable item in all six posts
One unified API over 100+ exchanges: OHLCV, order placement, balances. That is
`market-data-feed` and `execution-venue-adapter` — **two foundation blocks, one
dependency**, shipping daily at 43k stars.

### 2. Freqtrade — the closest existing thing to what is being built
53k stars, pushed the same day it was checked. A complete crypto bot: strategy
framework, backtesting, **paper trading before real money**, live automation. That
is C-01's whole shape already solved by someone else, and worth reading against
our design before writing a line of it.

### 3. NautilusTrader — the event-driven core, done properly
26.6k stars, shipping daily, explicitly built for correctness under load. The
reference for how an event-driven engine is structured when it is meant to be
trusted rather than demonstrated.

### 4. TradingAgents — a reference for the AI brain
99k stars, the largest in the list by a wide margin. Splits a trading brain into
news, market, sentiment, strategy and execution agents. Directly relevant to
`ai-brain`, which is named and undescribed.

### 5. Hummingbot — an independent architecture that reached a similar split
Core engine, arbitrage scanner, risk manager, strategy executor. A separate team
arriving at nearly our block boundaries is evidence worth weighing.

### 6. FinRL — the closest worked `learning-loop`
Load data, build environment, train agent, backtest, evaluate, deploy.

### 7. VectorBT — the fast backtesting engine
Vectorised, built for sweeping thousands of parameter sets rather than one run.

### 8–10. Lumibot, Polymarket, Backtrader
Lumibot overlaps Freqtrade with less momentum. Polymarket only matters if
prediction markets enter scope. **Backtrader stays last — not for its licence, but
because it has not been pushed since 2024-08-19** while everything above it
shipped this month. That was always a quality objection.

## 5. OmniPhi — nothing to use

`p/DbYieIWmUxW` · 1 image + 6 videos

An "Agentic Integrated Trading Environment": describe an agent, it builds and
runs it; writes, tests and discards strategies until one clears a bar, then goes
live.

**Not verifiable.** No GitHub presence for the product. The site answers, and
that is all. Closed beta, application only, "comment BETA for early access".

The *pattern* is worth noting — generate, backtest, discard, promote on merit —
because it is the same shape as `hypothesis → test → promote`. But there is no
code, no paper, and no way to evaluate the claims.

---

## 6. Six equations — not relevant

`p/DblhZgvkjbZ` · 8 slides · Schrödinger, Einstein field equations, and four more.

Physics. Well made, and unrelated to a crypto trading bot. Recorded so it does
not get re-read later hoping for something.

---

## Every performance number in these posts is decoration

`+24.37%`, `+128.47%`, `63.42%` win rate, Sharpe `1.89`, `+67.89%`, `+78.41%`,
`532` trades, and NautilusTrader's "1M+ events/sec, 0.3 ms, 99.99% uptime" are
rendered mockups on marketing slides. **None is a benchmark and none should reach
the design.** The one post that teaches the most — the volatility reel — makes no
profit claim at all, which is the reliable filter.

---

## A gap these posts expose

**Five of the ten repositories are backtesting frameworks, and this blueprint has
no backtesting block.**

`paper-live-trading` (C-01) runs on *live* data — that is forward testing. Replaying
history is a different thing, and it is how an opportunity instruction would be
tested before the scanner is ever told to watch for it. Without it, every
hypothesis has to be proven in forward time at real cost.

Raised, not added. The blueprint is paused, and a twentieth category is the
user's call.
