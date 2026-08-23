"""semantic-fact-store: what the system currently believes about each symbol.

The first of three memory tiers, and the only one that is overwritten. That makes
its discipline different from the others':

- **Closed-set keys.** A fact has exactly one place to live. An open key set lets
  "typical spread" and "usual spread" both exist, both be believed, and diverge
  -- and nothing would notice, because neither contradicts the other.
- **Upsert, with the old value kept.** A new value supersedes rather than
  replaces, so the history of what was believed survives. A store that overwrote
  could not answer "when did we start thinking this", which is the question that
  follows every surprise.
- **Every fact carries a source and a confidence.** A fact whose source cannot be
  named cannot be rechecked; a fact with no confidence is believed as firmly as
  one measured over a year, and the second is how a guess becomes an assumption.
- **A contradiction is recorded, not resolved.** Two sources disagreeing is
  information; overwriting one with the other silently discards it. The
  contradiction detector decides what to do; this store's job is to keep both
  visible.

**A fact never expires on its own.** The forgetting-curve scheduler lowers
confidence over time and the pruner removes what has become useless -- a store
that deleted its own facts would decide, alone, that something had stopped being
true.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.knowledge_types import (
    FACT_KEYS, FROM_INFERENCE, FROM_MEASUREMENT, SemanticFact,
)
from runtime.learned_estimator import Estimate
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "semantic-fact-store"

PART_DECLARATION = PartDeclaration(
    part_id="semantic-fact-store",
    consumes=("journal-entry", "fact-confidence", "knowledge-contradiction"),
    produces=("semantic-fact", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

STORED = "stored"
SUPERSEDED = "superseded-an-earlier-value"
UNKNOWN_KEY = "not-a-key-this-store-holds"
CONTRADICTS = "it-contradicts-what-is-held-and-both-are-kept"


@dataclass
class StoreStanding:
    facts_offered: int = 0
    facts_stored: int = 0
    supersessions: int = 0
    unknown_keys_refused: int = 0
    contradictions_recorded: int = 0
    confidence_updates: int = 0
    symbols_held: int = 0
    by_key: dict = field(default_factory=dict)
    by_source: dict = field(default_factory=dict)


class SemanticFactStore:
    """Holds one current value per key per symbol, and keeps what it superseded."""

    def __init__(
        self,
        prior_confidence: float,
        contradiction_tolerance: float,
        now_ns=time.time_ns,
    ) -> None:
        if not 0.0 <= contradiction_tolerance <= 1.0:
            raise ValueError(
                "the tolerance is how far two values may differ before they contradict, as a "
                "fraction of the larger"
            )
        self._prior_confidence = prior_confidence
        self._tolerance = contradiction_tolerance
        self._now_ns = now_ns
        self._current: dict[tuple[str, str, str], SemanticFact] = {}
        self._history: dict[tuple[str, str, str], list] = {}
        self._contradictions: list = []
        self.standing = StoreStanding()

    def upsert(
        self,
        venue_id: str,
        symbol: str,
        key: str,
        value,
        source: str,
        source_reference: str | None = None,
        confidence: float | None = None,
        observations: int = 0,
    ) -> tuple[SemanticFact | None, str]:
        """One fact. A new value supersedes the old rather than erasing it."""
        self.standing.facts_offered += 1

        if key not in FACT_KEYS:
            # An open key set lets the same fact exist twice under two spellings,
            # both be believed, and diverge with nothing to notice.
            self.standing.unknown_keys_refused += 1
            return None, UNKNOWN_KEY

        store_key = (venue_id, symbol, key)
        existing = self._current.get(store_key)
        outcome = STORED

        if existing is not None:
            if self._contradicts(existing.value, value):
                # Recorded rather than resolved: two sources disagreeing is
                # information, and overwriting discards it.
                self.standing.contradictions_recorded += 1
                self._contradictions.append(
                    {
                        "subject": f"{venue_id}:{symbol}:{key}",
                        "held": existing.value,
                        "offered": value,
                        "held_source": existing.source,
                        "offered_source": source,
                        "at_ns": self._now_ns(),
                    }
                )
                outcome = CONTRADICTS
            else:
                outcome = SUPERSEDED
                self.standing.supersessions += 1

            # The history survives, so "when did we start thinking this" can be
            # answered -- the question that follows every surprise.
            self._history.setdefault(store_key, []).append(
                SemanticFact(
                    venue_id=existing.venue_id, symbol=existing.symbol, key=existing.key,
                    value=existing.value, source=existing.source,
                    source_reference=existing.source_reference, confidence=existing.confidence,
                    observed_at_ns=existing.observed_at_ns, superseded_at_ns=self._now_ns(),
                )
            )

        fact = SemanticFact(
            venue_id=venue_id,
            symbol=symbol,
            key=key,
            value=value,
            source=source,
            source_reference=source_reference,
            confidence=Estimate(
                value=self._prior_confidence if confidence is None else confidence,
                is_fitted=confidence is not None,
                observations=observations,
                prior=self._prior_confidence,
                was_clamped=False,
                bound_low=None,
                bound_high=None,
                reason=(
                    f"from {source}"
                    + (f" ({source_reference})" if source_reference else ", with no reference")
                ),
            ),
            observed_at_ns=self._now_ns(),
            superseded_at_ns=None,
        )

        self._current[store_key] = fact
        self.standing.facts_stored += 1
        self.standing.by_key[key] = self.standing.by_key.get(key, 0) + 1
        self.standing.by_source[source] = self.standing.by_source.get(source, 0) + 1
        self.standing.symbols_held = len({(venue, sym) for venue, sym, _ in self._current})
        return fact, outcome

    def fact(self, venue_id: str, symbol: str, key: str) -> SemanticFact | None:
        return self._current.get((venue_id, symbol, key))

    def facts_for(self, venue_id: str, symbol: str) -> tuple:
        return tuple(
            fact
            for (venue, sym, _), fact in sorted(self._current.items())
            if (venue, sym) == (venue_id, symbol)
        )

    def history_of(self, venue_id: str, symbol: str, key: str) -> tuple:
        """What was believed before, and when it stopped being believed."""
        return tuple(self._history.get((venue_id, symbol, key), ()))

    def update_confidence(self, venue_id: str, symbol: str, key: str, confidence: float) -> bool:
        """The forgetting curve lowers confidence; this store never expires a fact itself."""
        store_key = (venue_id, symbol, key)
        fact = self._current.get(store_key)
        if fact is None:
            return False
        self._current[store_key] = SemanticFact(
            venue_id=fact.venue_id, symbol=fact.symbol, key=fact.key, value=fact.value,
            source=fact.source, source_reference=fact.source_reference,
            confidence=Estimate(
                value=confidence, is_fitted=True, observations=fact.confidence.observations,
                prior=fact.confidence.prior, was_clamped=False, bound_low=None, bound_high=None,
                reason="updated by the forgetting curve",
            ),
            observed_at_ns=fact.observed_at_ns, superseded_at_ns=None,
        )
        self.standing.confidence_updates += 1
        return True

    def remove(self, venue_id: str, symbol: str, key: str) -> bool:
        """Removal is the pruner's decision, never this store's own."""
        return self._current.pop((venue_id, symbol, key), None) is not None

    @property
    def contradictions(self) -> tuple:
        return tuple(self._contradictions)

    def _contradicts(self, held, offered) -> bool:
        if isinstance(held, (int, float)) and isinstance(offered, (int, float)):
            larger = max(abs(held), abs(offered))
            if larger == 0:
                return False
            return abs(held - offered) / larger > self._tolerance
        return held != offered


def describe_semantic_facts(store: SemanticFactStore) -> dict:
    return {
        "part_id": PART_ID,
        "facts_offered": store.standing.facts_offered,
        "facts_stored": store.standing.facts_stored,
        "supersessions": store.standing.supersessions,
        "unknown_keys_refused": store.standing.unknown_keys_refused,
        "contradictions_recorded": store.standing.contradictions_recorded,
        "confidence_updates": store.standing.confidence_updates,
        "symbols_held": store.standing.symbols_held,
        "by_key": dict(sorted(store.standing.by_key.items())),
        "by_source": dict(sorted(store.standing.by_source.items())),
        "keys": list(FACT_KEYS),
        "expires_facts_itself": False,
    }


def run_semantic_fact_store(
    store: SemanticFactStore, control_socket, read_journal, publish_facts,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        offers = read_journal(store)
        facts = []
        for offer in offers:
            fact, _ = store.upsert(**offer)
            if fact is not None:
                facts.append(fact)
        publish_facts(tuple(facts))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=tick,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
    )


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The journal is read for what it measures about a symbol: consecutive
    funding settlements give the funding interval. A fact confidence from
    the scheduler updates the held estimate; a contradiction is read and
    drained, since the store already keeps both sides of one.
    """
    from runtime.input_assembly import Batch
    from runtime.knowledge_types import FROM_MEASUREMENT, FUNDING_INTERVAL

    entries = Batch(read=context.bus.reader("journal-entry"))
    confidences = Batch(read=context.bus.reader("fact-confidence"))
    contradictions = Batch(read=context.bus.reader("knowledge-contradiction"))
    publish_facts = context.bus.publisher_for("semantic-fact")
    store = SemanticFactStore(
        prior_confidence=context.number("learning_prior_hit_rate"),
        contradiction_tolerance=context.number("contradiction_value_tolerance"),
    )
    last_settlement_ns: dict[tuple[str, str], int] = {}
    settlements_seen: dict[tuple[str, str], int] = {}

    def read_journal(_store):
        contradictions.payloads()
        for confidence in confidences.payloads():
            parts_of_key = confidence.fact_key.split(":", 2)
            if len(parts_of_key) == 3:
                store.update_confidence(parts_of_key[0], parts_of_key[1], parts_of_key[2], float(confidence.confidence))
        offers = []
        for entry in entries.payloads():
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if entry.kind != "funding-settlement":
                continue
            venue_id, symbol = payload.get("venue_id"), payload.get("symbol")
            settled_at = payload.get("settled_at_ns")
            if venue_id is None or symbol is None or settled_at is None:
                continue
            key = (str(venue_id), str(symbol))
            previous = last_settlement_ns.get(key)
            last_settlement_ns[key] = int(settled_at)
            if previous is None or int(settled_at) <= previous:
                continue
            settlements_seen[key] = settlements_seen.get(key, 0) + 1
            offers.append({
                "venue_id": key[0], "symbol": key[1], "key": FUNDING_INTERVAL,
                "value": (int(settled_at) - previous) / 1e9, "source": FROM_MEASUREMENT,
                "source_reference": f"journal:{getattr(entry, 'sequence', getattr(entry, 'entry_id', ''))}",
                "observations": settlements_seen[key],
            })
        return tuple(offers)

    def publish(items) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            publish_facts(kept)

    return run_semantic_fact_store(
        store=store,
        control_socket=context.control_socket,
        read_journal=read_journal,
        publish_facts=publish,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
