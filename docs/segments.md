# Segments

> "according to the bot segment like futures , spot or options every segment is a
> bot with all this"

**Every segment is a bot, with all of this inside it.** The segment bot is not one
thing that handles three markets — the structure is instantiated per segment.

| segment | |
|---|---|
| `spot` | Spot |
| `futures` | Futures |
| `options` | Options |

## Why this matters to the blueprint

**The condition is shared, the response is not.** A sudden price jump implying a
pullback is the same observation in all three. Acting on it is a put in options, a
short in futures, and something else again in spot. So an opportunity instruction
describes the condition, and the segment decides the response — otherwise the
instruction would have to know about every segment, which breaks T-4.

**Each segment bot contains the same parts.** The universal opportunity scanner,
the bull bot, the bear bot, the profit-tailgating bot. Same shape, per T-1 — three
instances of one design, not three bespoke bots.

## Not yet decided

Whether the blocks outside the segment bot are **shared across segments or
instantiated per segment**. It matters and it is not a detail:

- **Risk and capital allocation** — one allocator sees total exposure across all
  three segments and can cap it. Three allocators each stay within their own
  budget and none can see the whole.
- **Portfolio and position state** — the same question, and it decides whether a
  position in one segment is visible to another segment's scanner.
- **Ledger, market data feed, hypothesis, closed trade decoding** — sharing these
  means a lesson learned in futures can reach the spot bot. Splitting them means
  each segment learns only from itself.

This is the user's to decide, and it is asked rather than assumed.
