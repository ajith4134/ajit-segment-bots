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

## Decided 2026-08-20 — split, each segment gets its own

Everything is instantiated per segment. Three market data feeds, three risk
allocators, three portfolios, three ledgers, three hypothesis features, three
closed-trade decoders, three segment bots — each holding its own scanner, bull,
bear, profit-tailgating bot and AI brain.

**What this buys:** complete isolation. A fault in one segment cannot reach
another, and each segment's behaviour is attributable to that segment alone.

**What it costs**, stated once because the user chose it knowing this: nothing
sees total exposure across the three, and each segment learns only from itself —
a lesson paid for in futures does not reach spot.

## Flagged, not resolved — two blocks that cannot be split

The hardware resource governor (C-11) and observability are marked `global`
rather than per-segment, and this needs the user's confirmation.

**Why the governor cannot be split:** there is one machine. Three governors on
it, each blind to the other two, would compete for the same RAM and each would
believe it had freed capacity that another had already taken. That is precisely
the queueing C-11 exists to prevent — splitting it does not weaken the governor,
it inverts it.

**Why observability:** one board, one place to look. Three of them means no
single view of the system, which defeats the point of having it.

Both are marked `scope_origin: claude-flagged` in the registry so this is visibly
Claude's call awaiting a verdict, not something the user said.

## The AI brain sits inside each segment bot

Decided 2026-08-20. Alongside the scanner and the three bots. Three brains, one
per segment, each thinking about its own market. What it does is still not
described, so it appears in the diagram with its placement shown and no flow.
