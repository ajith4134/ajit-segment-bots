"""llm-response-cache: the same call answered twice should be paid for once.

Caching a language model looks trivial and is the easiest place in this block to
introduce a silent correctness bug, because the failure is not a wrong answer -- it
is a *stale* answer that is perfectly well-formed. Two rules make it safe:

- **The key is the rendered request's fingerprint**, which covers the prompt version,
  the exact text, the assembled context and every measured fact. "Same purpose, same
  symbol" is not the same call: the facts differ, and answering from a cache keyed on
  the shape rather than the content returns yesterday's market with today's confidence.
- **Entries expire on the age of the facts, not on their own age.** A cached answer
  about a market from four minutes ago is stale even if it was cached one second ago,
  and one about an unchanging fact is fine after an hour. The time-to-live therefore
  travels per purpose rather than being one global number.

Three further properties, each earning its place:

- **A cached response is marked as cached.** Downstream accounting must not count a
  free answer as a call that consumed quota, and a scorer must not treat one answer
  reused fifty times as fifty pieces of evidence.
- **Nothing is cached that failed.** A truncated or refused response cached is a
  failure served instantly for as long as the entry lives.
- **Eviction is by least-recently-used with a bounded size**, because an unbounded
  cache of prompt text is a slow memory leak that looks like a feature.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field

from runtime.llm_types import LlmResponse
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "llm-response-cache"

PART_DECLARATION = PartDeclaration(
    part_id="llm-response-cache",
    consumes=("rendered-llm-request", "llm-response"),
    produces=("llm-response", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

HIT = "hit"
MISS = "miss"
EXPIRED = "the-facts-behind-it-are-too-old"
NOT_CACHEABLE = "this-response-is-not-worth-serving-again"
STORED = "stored"
EVICTED = "evicted"


@dataclass
class CacheEntry:
    fingerprint: str
    response: LlmResponse
    purpose: str
    facts_measured_at_ns: int
    stored_at_ns: int
    hits: int = 0


@dataclass(frozen=True)
class CacheLookup:
    fingerprint: str
    state: str
    response: LlmResponse | None
    age_of_facts_seconds: float | None
    reason: str
    looked_up_at_ns: int

    @property
    def is_a_hit(self) -> bool:
        return self.state == HIT and self.response is not None


@dataclass
class CacheStanding:
    lookups: int = 0
    hits: int = 0
    misses: int = 0
    expired: int = 0
    stored: int = 0
    refused_to_store: int = 0
    evictions: int = 0
    calls_avoided: int = 0


class LlmResponseCache:
    """Keyed on the full fingerprint, expiring on the age of the facts."""

    def __init__(
        self,
        maximum_entries: int,
        time_to_live_by_purpose: dict,
        default_time_to_live_seconds: float,
        now_ns=time.time_ns,
    ) -> None:
        if maximum_entries < 1:
            raise ValueError(
                "an unbounded cache of prompt text is a slow memory leak that looks like "
                "a feature"
            )
        if default_time_to_live_seconds <= 0:
            raise ValueError(
                "a cached answer with no expiry serves yesterday's market with today's "
                "confidence"
            )
        self._maximum_entries = maximum_entries
        self._time_to_live = dict(time_to_live_by_purpose)
        self._default_time_to_live = default_time_to_live_seconds
        self._now_ns = now_ns
        self._entries: OrderedDict = OrderedDict()
        self.standing = CacheStanding()

    def time_to_live_for(self, purpose: str) -> float:
        """Per purpose: a price is stale in seconds, a tick size is not."""
        return self._time_to_live.get(purpose, self._default_time_to_live)

    def look_up(self, rendered, facts_measured_at_ns: int) -> CacheLookup:
        self.standing.lookups += 1
        entry = self._entries.get(rendered.fingerprint)
        if entry is None:
            self.standing.misses += 1
            return self._lookup(
                rendered.fingerprint, MISS, None, None,
                "not cached. The key is the full fingerprint, so a call differing only in "
                "one measured fact is a different call",
            )

        age = (self._now_ns() - facts_measured_at_ns) / 1e9
        limit = self.time_to_live_for(rendered.purpose)
        if age > limit:
            self.standing.expired += 1
            del self._entries[rendered.fingerprint]
            return self._lookup(
                rendered.fingerprint, EXPIRED, None, age,
                f"the facts behind it are {age:.1f}s old against a {limit:.1f}s limit for "
                f"{rendered.purpose}. Entries expire on the age of the facts, not their own",
            )

        entry.hits += 1
        self._entries.move_to_end(rendered.fingerprint)
        self.standing.hits += 1
        self.standing.calls_avoided += 1

        cached = LlmResponse(
            response_id=entry.response.response_id,
            rendered_id=rendered.rendered_id,
            version_id=entry.response.version_id,
            model_id=entry.response.model_id,
            text=entry.response.text,
            finish_reason=entry.response.finish_reason,
            input_tokens=entry.response.input_tokens,
            output_tokens=entry.response.output_tokens,
            latency_seconds=0.0,
            payment_kind=entry.response.payment_kind,
            was_cached=True,
            responded_at_ns=self._now_ns(),
        )
        return self._lookup(
            rendered.fingerprint, HIT, cached, age,
            f"served from cache, {entry.hits} time(s) so far. It is marked as cached: "
            f"accounting must not count a free answer as a call, and a scorer must not "
            f"treat one answer reused as many pieces of evidence",
        )

    def store(self, rendered, response, facts_measured_at_ns: int) -> str:
        if response.was_cut_off or not response.text.strip():
            self.standing.refused_to_store += 1
            return NOT_CACHEABLE

        self._entries[rendered.fingerprint] = CacheEntry(
            fingerprint=rendered.fingerprint,
            response=response,
            purpose=rendered.purpose,
            facts_measured_at_ns=facts_measured_at_ns,
            stored_at_ns=self._now_ns(),
        )
        self._entries.move_to_end(rendered.fingerprint)
        self.standing.stored += 1

        while len(self._entries) > self._maximum_entries:
            self._entries.popitem(last=False)
            self.standing.evictions += 1
        return STORED

    def size(self) -> int:
        return len(self._entries)

    def hit_rate(self) -> float | None:
        if self.standing.lookups == 0:
            return None
        return self.standing.hits / self.standing.lookups

    def _lookup(self, fingerprint, state, response, age, reason) -> CacheLookup:
        return CacheLookup(
            fingerprint=fingerprint, state=state, response=response,
            age_of_facts_seconds=age, reason=reason, looked_up_at_ns=self._now_ns(),
        )


def describe_cache(cache: LlmResponseCache) -> dict:
    return {
        "part_id": PART_ID,
        "lookups": cache.standing.lookups,
        "hits": cache.standing.hits,
        "misses": cache.standing.misses,
        "expired_on_the_age_of_the_facts": cache.standing.expired,
        "stored": cache.standing.stored,
        "refused_to_store": cache.standing.refused_to_store,
        "evictions": cache.standing.evictions,
        "calls_avoided": cache.standing.calls_avoided,
        "entries": cache.size(),
        "hit_rate": cache.hit_rate(),
        "keys_on_purpose_and_symbol": False,
        "caches_a_truncated_response": False,
    }


def run_llm_response_cache(
    cache: LlmResponseCache, control_socket, read_lookups, publish_responses,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for rendered, facts_measured_at_ns, response in read_lookups():
            if response is not None:
                cache.store(rendered, response, facts_measured_at_ns)
                continue
            lookup = cache.look_up(rendered, facts_measured_at_ns)
            if lookup.is_a_hit:
                publish_responses(lookup.response)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )
