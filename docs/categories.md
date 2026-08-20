# Foundation categories — ajit-segment-bots

Given by the user on 2026-08-20. These are the main blocks: the foundation of an
advanced multi-storey skyscraper. Every future idea or feature belongs to one of
these categories. A feature that fits none of them means a category is missing,
and a missing category is raised with the user rather than invented.

## The user's words

> I will tell you the features and the main blocks or the consider them the
> foundation to a advance d multi store sky scraper so in future any idea or
> features should be in the categories I mentioned or if you think of a new
> category other than I mentioned then tell me and add it the categories are
> papper trading with live data in that trading option or switch to live real
> money or papper money, universal opportunity scanner this is constantly
> monitoring all the symbols in the entire segment it picks the time when a
> symbol should enter trading it don't know there is papper or live money, and 3
> bots bull , bear and profit tail gating those 3 bots live in side the segment
> bot we will discuss detail when implementing these features, intelligence
> category, learning loop category, hypothesis features, knowledge feature,
> prediction features, online research on internet on decoding the famous or high
> profit crypto trades portfolio to copy their strategy or step up into us, for
> now we start with this and open to others in the future

## The nine categories as given

### C-01 — Paper trading on live data
Trading runs on live market data. Inside it sits the switch: real money or paper
money. Paper is not a replay of old data — it is the live market, with simulated
money.

### C-02 — Universal opportunity scanner
Constantly monitors **all** symbols in the entire segment, not a shortlist. Its
job is to pick the moment a symbol should enter trading.

**Stated constraint:** it does not know whether the money is paper or live. That
is an architectural boundary the user drew, not an implementation detail — the
scanner's output must be identical either way, and the paper/live switch sits
downstream of it.

### C-03 — Segment bot, holding three bots
Three bots live inside the segment bot: **bull**, **bear**, and **profit
tailgating**. Their internals are deliberately not specified yet — the user said
these are discussed in detail at implementation time.

### C-04 — Intelligence

### C-05 — Learning loop

### C-06 — Hypothesis

### C-07 — Knowledge

### C-08 — Prediction

### C-09 — Online research
Research on the internet, decoding the portfolios of famous or high-profit crypto
traders — to copy their strategy, or to step it up beyond theirs.

## What is deliberately not recorded here

The user said "for now we start with this and open to others in the future", and
that the three bots are detailed at implementation time. So:

- **No internals.** What each category contains is not written down until the
  user describes it.
- **No data flow between categories.** Which category feeds which is not yet
  declared. The diagram shows the blocks and says so, rather than drawing an
  inferred flow that would then be mistaken for a decision.

The flow contract is the next thing to establish, and it is the thing the whole
blueprint is judged on.
