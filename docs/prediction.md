# Prediction — deep dive

The first foundation block opened up. Every block is a mini project with parts
inside it; this is prediction's.

**What it does:** predicts what the market does next, from candles alone.

---

## The model it is built on

The user supplied Kronos via a screenshot. **The claims were checked against the
repository before anything was designed around them**, because a dependency taken
from a social post is a dependency nobody verified.

| | verified 2026-08-20 via `gh` |
|---|---|
| repository | `shiyu-coder/Kronos` |
| licence | **MIT** |
| stars / forks | 37,616 / 6,254 |
| last push | 2026-04-13 |
| paper | arXiv [2508.02739](https://arxiv.org/abs/2508.02739), accepted to **AAAI 2026** |
| language | Python |

**Where the post was wrong or stale:**

- It said 32K stars. The repository has **37,616** — the post is out of date, not
  incorrect.
- It implied the whole family is open. **Kronos-large (499.2M) is not
  open-source.** Only mini, small and base are.

**What it actually is:** a family of decoder-only foundation models pre-trained on
K-line sequences from over 45 exchanges, in two stages. A tokenizer quantises
continuous OHLCV into *hierarchical discrete tokens* — coarse-grained and
fine-grained subtokens — and an autoregressive transformer is pre-trained on those
tokens. That is what the screenshot's two diagrams show.

| model | tokenizer | context | params | open |
|---|---|---|---|---|
| Kronos-mini | Tokenizer-2k | **2048** | 4.1M | yes |
| Kronos-small | Tokenizer-base | 512 | 24.7M | yes |
| Kronos-base | Tokenizer-base | 512 | 102.3M | yes |
| Kronos-large | Tokenizer-base | 512 | 499.2M | **no** |

Two facts that shape the design rather than decorate it:

1. **Context is 512 candles** for small and base. On 1-minute bars that is roughly
   eight and a half hours of lookback — comfortable for intraday, and a hard
   ceiling the window builder has to respect rather than discover at runtime.
   Kronos-mini trades capacity for a 2048 context.
2. **These models are small.** 4M to 102M parameters runs on CPU. This machine has
   12 cores and 29 GiB, and nothing in the model zoo needs a GPU to be useful —
   which is what makes T-3 achievable here: a forecaster that is switched off can
   genuinely hand its memory back.

The live demo forecasts **BTC/USDT** over 24 hours, so the model is already being
exercised on exactly this project's asset class.

---

## The parts inside prediction

Five. Each is a transistor: one responsibility, a switch, states, and it names
data rather than other parts.

| part | its one responsibility | reads | writes | origin |
|---|---|---|---|---|
| K-line window builder | shape live market data into the fixed candlestick window the model expects | market data | K-line window | proposed |
| Kronos forecaster | run the Kronos model over a candlestick window to produce a price forecast | K-line window, finetuned model, model choice | price forecast | **user** |
| Kronos finetuner | adapt the Kronos tokenizer plus predictor to the assets actually traded here | K-line window | finetuned model | **user** |
| Forecast scorer | score each past forecast against what the market actually did | price forecast, market data | forecast accuracy | proposed |
| Kronos size selector | choose which Kronos model size to run based on measured forecast accuracy | forecast accuracy | model choice | proposed |

**Why the window builder is separate from the forecaster.** The 512-candle limit
is a property of the model, not of the market. Keeping the shaping apart means
swapping Kronos for something with a different context is a change to one part —
which is what T-6 asks for, and what makes the forecaster a genuine spare part.

**Why the scorer exists at all.** The user did not ask for it. A forecast nobody
scores is an assertion, and three blocks downstream — hypothesis, learning loop,
closed-trade decoding — are supposed to learn from something. `forecast-accuracy`
also goes to the ledger, so how well prediction is doing is recorded rather than
believed.

**Why the size selector reads accuracy and not the hardware.** Choosing a model by
what the machine has free would be the control plane, and T-2 forbids a feature
from reaching into it. It chooses by measured accuracy instead; the governor
independently decides whether the chosen part runs at all.

---

## The block's boundary

    market data ──▶ [ prediction ] ──▶ price forecast
                                   └─▶ forecast accuracy

Everything else — the window, the finetuned weights, the model choice — never
leaves the block. That is the contract the rest of the system sees, and it does
not change if every part inside is replaced tomorrow.

---

## Open

**Nothing consumes `price-forecast` yet.** Prediction produces it and only its own
scorer reads it. Something has to *act* on a forecast — hypothesis is the obvious
candidate, since it already turns lessons into opportunity instructions — but the
user has not said so, and the board shows the gap rather than an invented edge.

**Finetuning cadence is not decided.** The finetuner has no trigger: nightly, on
drift, on a schedule, on command. Not defaulted.
