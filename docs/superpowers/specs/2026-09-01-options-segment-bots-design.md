# Technical spec — options segment bots (Phase A)

Status: draft, awaiting user review. Phase A of `docs/goal.md`'s corrected
build order (2026-09-01): index options and stock options, two segment
bots, each complete end-to-end — paper on historic data, then live-data
paper trading with all 332+ parts genuinely trading — before Phase B
(intraday equity with margin, futures, commodities) starts.

## 1. Reuse the crypto segment-bot template, audited — not rebuilt

Confirmed by the user (2026-09-01, "option C" of three proposed approaches).
The crypto build's per-segment stack — `opportunity-scanner` (13 detectors),
`bull-bot`/`bear-bot`/`profit-tailgating-bot` (peer bots, `peer_group`,
R-03), `ai-brain` (arbitration), `segment-bot` (`instrument-selector`) — is
the proven pipeline shape: scan → feature-build → conviction → arbitrate →
select-instrument → size → execute. That shape is reused verbatim, T-1's
"same template, no privileged parts" applied across segments, not just
within one.

**Each segment gets its own full stack, no sharing** — same rule
`docs/segments.md` already gave crypto's three segments 2026-08-20
("Everything is instantiated per segment... complete isolation"). Index
options and stock options are two `peer_group` blocks; R-03 forbids one
producing a type the other consumes, same as bull/bear/tailgater already
enforce within one segment. Only the resource governor and observability
stay global, per that same ruling.

What changes per segment is **content, not shape**: which detectors run,
what a bot's feature-builder reads, what `instrument-selector` picks.

## 2. Scope, confirmed 2026-09-01

- **Buy-only**: long calls (bull-bot), long puts (bear-bot). No writing,
  no spreads — undefined-risk short legs and multi-leg coordination are a
  separate future bot (`docs/future-upgrades.md`), not Phase A.
- **Stock options universe**: the same F&O-eligible ~180-200 symbols
  already targeted for equity (goal.md §2) — one universe feeds three
  segments. `liquidity-grader` (already in the template) filters per-scan
  for which of them actually have a tradeable option chain right now,
  rather than a separate universe-selection policy being designed for it.
- **Index options universe**: NIFTY 50, BANK NIFTY, SENSEX — the three
  Indian indices with liquid, broker-supported weekly options chains.
- **Nearest expiry only**, both segments.
- **Force-close before expiry**, a settings-driven buffer of days rather
  than held to exercise — avoids modelling ITM auto-exercise/assignment
  in Phase A. Exact buffer is a number to justify with real data at
  implementation time (RL-061), not decided here.

## 3. `instrument-selector` gains strike selection

Crypto's `instrument-selector` never needed this — a perpetual future has
no strike. Options do. **Default policy: ATM** (the strike whose delta is
closest to 0.5) — most liquid, simplest to reason about, no free parameter
to justify yet. A settings-driven delta target (e.g. always trade the
0.4-delta strike for cheaper premium / more leverage) is the natural
upgrade path, explicitly not built now.

Reads `broker-instrument-listing` (which strikes exist, at what expiry)
and `broker-option-greeks` (each strike's live delta, from the tape) to
make the pick — both already exist from the market-data-feed work.

## 4. Opportunity-scanner: the detector audit

Kept as-is, reading the **underlying's** price (index level or stock
price), not the option's — a detector's job is to spot a condition on the
thing that moves, `instrument-selector` picks what expresses it:

| Detector | Why it survives unchanged |
|---|---|
| `regime-classifier` | Hurst-proxy regime classification is price-generic |
| `mean-reversion-detector` | deviation-from-mean is price-generic |
| `cointegration-pair-finder` / `spread-reversion-detector` | pair statistics over underlying prices, segment-agnostic |
| `momentum-burst-detector` | a sudden jump is a sudden jump regardless of asset class |
| `liquidity-grader` | already segment-agnostic by design |
| `watch-condition-compiler` / `universal-symbol-sweeper` | infrastructure, not domain logic |

**`volatility-gap-detector` becomes more central, not swapped.** "Forecast
volatility disagrees with the option market" is already an options concept
— crypto had it for its own options-like instruments. For Phase A this is
one of the most directly relevant detectors in the whole scanner, not an
edge case.

**Dropped, no honest equivalent yet:** `funding-skew-detector`,
`whale-flow-detector`, `liquidation-cascade-detector`,
`sentiment-shift-detector` — all genuinely crypto-only concepts (funding
rate, on-chain flow). Not replaced with a guess; simply absent from
Phase A's scanner until a real Indian-markets equivalent is designed on
its own evidence.

**Added — new, 2026-09-01, user's own addition:**

### `expiry-day-zero-to-hero-detector`

**Index options only** (NIFTY, BANK NIFTY, SENSEX — the three with liquid
weekly expiries; stock options mostly don't have this dynamic at all).
Spots a deep out-of-the-money option cheap enough that a late move in the
underlying toward its strike could multiply its price many times over
before the session closes — the retail-known "zero to hero" trade, driven
by gamma exploding as expiry approaches.

**Role:** "spot a far-out-of-the-money index option cheap enough to spike
sharply if the underlying reaches its strike before today's close"

**Consumes:** `broker-instrument-listing` (is today this contract's
expiry?), `broker-market-data` (the option's own live LTP — is it cheap?),
`broker-option-greeks` (delta — how far OTM?), the underlying's own price
data (consolidated-price equivalent, same as every other detector).

**Produces:** `entry-candidate`, `part-health` — same shape as every other
detector; this is a new *condition*, not a new pipeline stage. What
happens after (sizing, whether it clears the risk gate, how much capital
it's allowed) is downstream machinery this detector has no say over,
same as any other entry-candidate.

**What makes an option a candidate — real thresholds, not guessed ones,
to be set at implementation time (RL-061):**
- Today's date equals this instrument's expiry (from `broker-instrument-listing`).
- The option's own premium is below some cheapness threshold (a rupee
  figure or a percentile of the day's range — which, is implementation's
  call, backed by real captured premium data, not chosen here).
- Delta below some far-OTM threshold (loosely, further from ATM than
  `instrument-selector`'s own ATM pick).
- Likely also gated by time-of-day — this pattern is overwhelmingly a
  last-hour phenomenon; whether to gate on session time or leave that to
  the conviction model to learn is an implementation decision.

**Named risk, not hidden:** the overwhelming majority of these options
expire worthless — that is *why* they are cheap. This detector produces a
candidate; it does not claim an edge. Sizing goes through the same
risk-capital-allocation gate every other candidate does, and whether this
detector's candidates are worth trading at all is exactly what paper
trading on historic and then live data is for.

## 5. Feature building — what replaces crypto's bull-feature-builder inputs

Crypto's bull-bot reads funding sign, VWAP gap, volume-against-hour-norm —
none apply. Options-native replacement reads what the market-data-feed
work already puts on the tape: `broker-option-greeks` (delta, theta,
gamma, vega, implied volatility) for the candidate strike, plus the
underlying's own price-action features (support/resistance distance, flow
imbalance) which stay unchanged from crypto's shape since they describe
the underlying, not the option.

## 6. Implementation is more than one plan

This spec covers architecture and content decisions, not a build order.
Two full segment-bot stacks (~9 kept detectors + 1 new one, 10-part
bull-bot, 10-part bear-bot, 10-part profit-tailgating-bot, 14-part
ai-brain, 1-part segment-bot, per segment) is on the order of 110 parts —
far past what one implementation plan should hold (this project's own
plans have run 139-2600 lines for far smaller scopes). Follows this
project's own build-order discipline (RL-050's "one vertical first,
fully"): implementation gets decomposed into its own sequence of plans,
likely by pipeline stage or by segment, decided at `writing-plans` time
against whichever segment (index options or stock options) goes first —
itself an open question, see below.

## 7. What's still open — yours to answer, not decided here

- ~~**Which of the two goes first**~~ **Confirmed 2026-09-01: index
  options first, fully.** Far smaller universe to prove the pipeline
  against (3 underlyings — NIFTY, BANKNIFTY, SENSEX — versus ~180-200 for
  stock options), typically deeper liquidity, and it's where the
  zero-to-hero detector lives. Stock options stays declared-but-skeleton
  (T-1, same template) until index options is proven end-to-end.
- **Zero-to-hero's exact numeric thresholds** (cheapness cutoff, delta
  cutoff, session-time gate) — need real captured option-chain data to set
  honestly, not a guess at spec time.
- **Paper-fill simulation for options specifically**: does Upstox's
  sandbox (the reason it was picked as primary, §goal.md) actually cover
  options order simulation, or only equity? Not yet verified — a spike
  question for implementation, not assumed either way.
- **Settings for the expiry force-close buffer** (how many days before
  expiry) — a number to justify from real data, not chosen in this spec.
