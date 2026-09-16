# A prompt can be assembled with no retrieved passage

**Status:** proposed 2026-09-16, measured on the live spine.

## What is wrong

`context-assembler` builds its jobs from retrieval hits and nothing else:

```python
for hit in hits.payloads():
    by_query.setdefault(hit.query_id, []).append(hit)
...
for query_id, group in by_query.items():
    jobs.append((query_id, snapshot or latest_snapshot[0], tuple(group), budget))
```

No hits, no jobs. So a request whose retrieval found nothing relevant never becomes
a context at all, `prompt-renderer` refuses it for missing context, and no model is
ever called for it.

Measured live on 2026-09-16, after an embedding model was installed and the index
began answering:

    retrieval-index    75 queries, 14 vectors indexed,
                       67 with nothing above the 0.5 similarity floor, 0 hits
    context-assembler  84,217 verified snapshots in, 0 contexts out
    prompt-renderer    904 requests seen, 901 refused missing-context

The refusals are not the retrieval failing. The corpus is 14 chunks of arxiv
abstracts and the questions are about trades, so "nothing is close enough" is the
correct answer. The defect is that a correct empty retrieval silently cancels the
prompt.

## Why that is the wrong shape

The part's own docstring states the intended priority:

> **Verified facts are placed first and are never dropped.** They are the ground
> truth the answer will be checked against; a context without them cannot produce a
> checkable answer, so assembly fails rather than proceeding.
> **Retrieved passages are the compressible part**, dropped from the weakest first.

A context with facts and no passages is therefore valid by design -- it is the
budget-constrained end state the assembler already drops passages towards. What is
missing is only the ability to *start* from there.

It also inverts the block's own risk model. The failure the assembler exists to
prevent is a model answering fluently from retrieved prose with no measured facts
to be checked against. Refusing when there is no prose, while holding 84,217
verified snapshots, prevents the safe case and permits nothing.

## The change

`context-assembler` consumes `retrieval-query` in addition to `retrieval-hit`.

A query is the evidence that a request is in flight and wants context. The part
already keys everything by `query_id`, which is what a hit carries and what a query
carries. So:

- a query with hits assembles exactly as it does today;
- a query with no hits assembles from its verified snapshot, with zero passages,
  and counts itself (`contexts_with_no_retrieved_passage`);
- a query with no snapshot is refused by name inside the assembler, unchanged --
  that is the case the docstring is about, and it stays.

Nothing else changes. `prompt-renderer` already renders whatever context it is
given; the passages are optional to it.

## Why not the alternatives

- **Lower `retrieval_minimum_similarity`.** That does not fix the shape, it hides
  it: it would return the least-unrelated arxiv abstract as though it were
  relevant, which is the confident-nonsense failure the floor exists to prevent.
- **Have `retrieval-index` publish an empty hit.** A hit that hit nothing is a lie
  in the data model, and every consumer of `retrieval-hit` would have to learn to
  ignore it.
- **Wait for the corpus to fill.** The corpus fills from trade narratives and
  skills, which are themselves produced by parts that need prompts. The block would
  have to bootstrap through the one path that is closed.

## What it unblocks, and what it does not

It lets a prompt be rendered and routed. It does **not** make a model answer: the
three callers (`local-model-caller`, `metered-api-caller`,
`subscription-session-caller`) still need a local model or a provider key, and both
are absent on this box. The honest expectation is that the chain advances from
`prompt-renderer` to the router and stops at the callers, with the reason named.
