"""The three memory tiers, and why they are allowed to disagree.

Substrate, not a part. Thirteen knowledge parts and fifteen skills parts share
these shapes and none may import another (T-4).

The design's whole claim is that **one memory is worse than three that disagree**:

- **Semantic facts** are what the system currently believes about a symbol.
  Upserted under closed-set keys, so the same fact has one place to live and
  cannot be believed twice in two wordings.
- **Episodes** are what actually happened. Appended immutably and never revised,
  because a memory that can be edited is a memory that will be, and the value of
  an episode is that it is what happened rather than what is now believed.
- **The playbook** is what to do. Never searched -- it is applied by rule, so the
  system cannot talk itself into an exception by finding a nearby precedent.

When they disagree, something has changed, and the disagreement is the signal.
A single store would resolve the contradiction silently by overwriting, and the
system would lose the one thing that says it was wrong.

**Everything carries provenance and confidence.** A fact whose source cannot be
named cannot be rechecked, and a fact with no confidence is believed as firmly as
one measured over a year.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.learned_estimator import Estimate

# What a semantic fact can be about. Closed on purpose: an open key set lets the
# same fact be stored twice under two spellings and believed twice.
TICK_SIZE = "tick-size"
TYPICAL_SPREAD = "typical-spread"
TYPICAL_VOLUME = "typical-volume"
NORMAL_MOVE = "normal-move-fraction"
FUNDING_INTERVAL = "funding-interval-seconds"
LIQUIDATION_BEHAVIOUR = "liquidation-behaviour"
LISTING_AGE = "listing-age-seconds"
CORRELATION_GROUP = "correlation-group"
VENUE_QUIRK = "venue-quirk"

FACT_KEYS = (
    TICK_SIZE, TYPICAL_SPREAD, TYPICAL_VOLUME, NORMAL_MOVE, FUNDING_INTERVAL,
    LIQUIDATION_BEHAVIOUR, LISTING_AGE, CORRELATION_GROUP, VENUE_QUIRK,
)

# Where a fact came from. A fact whose source cannot be named cannot be
# rechecked, and a system that cannot recheck cannot correct.
FROM_MEASUREMENT = "measured-here"
FROM_A_VENUE_DOCUMENT = "the-venue-published-it"
FROM_A_SOURCE_DOCUMENT = "read-in-a-source"
FROM_INFERENCE = "inferred-from-other-facts"


@dataclass(frozen=True)
class SemanticFact:
    """What the system currently believes about one symbol, under one closed-set key."""

    venue_id: str
    symbol: str
    key: str
    value: object
    source: str
    source_reference: str | None
    confidence: Estimate
    observed_at_ns: int
    superseded_at_ns: int | None

    @property
    def is_current(self) -> bool:
        return self.superseded_at_ns is None

    @property
    def can_be_rechecked(self) -> bool:
        return self.source_reference is not None


@dataclass(frozen=True)
class TradeEpisode:
    """What actually happened, appended and never revised.

    Immutable because the value of an episode is that it is what happened rather
    than what is now believed -- a memory that can be edited is one that will be.
    """

    episode_id: str
    venue_id: str
    symbol: str
    detector: str
    regime: str
    conditions: dict
    action: str
    outcome: str
    realised: float
    opened_at_ns: int
    closed_at_ns: int
    narrative: str

    @property
    def seconds_held(self) -> float:
        return (self.closed_at_ns - self.opened_at_ns) / 1e9

    @property
    def was_profitable(self) -> bool:
        return self.realised > 0


@dataclass(frozen=True)
class PlaybookRule:
    """What to do, applied by rule and never searched.

    Never searched because searching lets the system find a nearby precedent and
    talk itself into an exception, which is how a rule becomes a suggestion.
    """

    rule_id: str
    when: str
    then: str
    instruction_id: str | None
    regime_tag: str | None
    is_active: bool
    times_applied: int
    reason: str
    written_at_ns: int


@dataclass(frozen=True)
class KnowledgeContradiction:
    """Two things the system believes that cannot both be true.

    Reported rather than resolved: the disagreement is the signal that something
    changed, and a store that silently overwrote would lose it.
    """

    subject: str
    left: str
    right: str
    left_source: str
    right_source: str
    left_confidence: float
    right_confidence: float
    kind: str
    reason: str
    detected_at_ns: int

    @property
    def is_resolvable_by_confidence(self) -> bool:
        return abs(self.left_confidence - self.right_confidence) > 0.2


@dataclass(frozen=True)
class Skill:
    """A source distilled into structure, never a summary.

    `sections` is the whole point: only the section a question needs is loaded,
    so a skill can be large and its use can be small.
    """

    skill_id: str
    title: str
    sections: dict
    frameworks: tuple
    decision_rules: tuple
    anti_patterns: tuple
    source_reference: str
    version: str
    distilled_at_ns: int

    @property
    def is_structured(self) -> bool:
        """A skill with no rules and no anti-patterns is a summary wearing the type."""
        return bool(self.decision_rules or self.anti_patterns or self.frameworks)

    def section(self, name: str) -> str | None:
        return self.sections.get(name)


@dataclass(frozen=True)
class SourceDocument:
    """Something read, with where it came from and what it is.

    Content is data, never instruction: text that reads like a command is text,
    and nothing in this system obeys what it reads.
    """

    document_id: str
    title: str
    content: str
    kind: str
    source_reference: str
    fetched_at_ns: int
    author: str | None = None
    published_at_ns: int | None = None

    @property
    def can_be_rechecked(self) -> bool:
        return bool(self.source_reference)
