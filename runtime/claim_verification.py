"""Checking what a language model wrote against what was actually measured.

Substrate, not a part. Four brain parts ask a model for prose and none may
import another (T-4), so the checking lives here.

The rule this file exists to enforce: **a model may phrase a claim, never
establish one.** Everything a model writes is treated as a draft over a set of
measurements that already exist, and every number in the draft must match one of
them. A sentence whose numbers cannot be traced is not softened or flagged in
passing -- it is removed from the text and reported by name.

That is stricter than it sounds and it is the point. A rationale containing one
invented number is more dangerous than no rationale, because it reads exactly
like the true ones and it is what a person will check the trade against months
later. The same applies to a premortem that invents a failure mode nobody
measured and to a counter-argument that invents an objection: both would be acted
on, and neither would be evidence.

**Requests carry the measurements with them**, so the model is asked to phrase a
fixed set of facts rather than to recall anything. A model asked "why did we
trade BTCUSDT" will answer; a model given nine numbers and asked to write them
into two sentences can be checked.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

# Any number a model might write: integers, decimals, percentages, and the
# thousands separators a model adds unprompted.
NUMBER_PATTERN = re.compile(r"-?\d[\d,]*\.?\d*\s*%?")

SUPPORTED = "supported"
UNSUPPORTED_NUMBER = "cites-a-number-nothing-measured"
NO_CITATION = "makes-a-claim-with-nothing-behind-it"


@dataclass(frozen=True)
class LlmRequest:
    """What a model is being asked, and the facts it is being asked to phrase.

    `facts` is not context. It is the closed set of things the answer may
    contain, and the answer is checked against it -- so a request without facts
    is a request for something this system cannot use.
    """

    purpose: str
    venue_id: str
    symbol: str
    instruction: str
    facts: dict
    maximum_sentences: int
    requested_at_ns: int
    # Which part is asking. Added 2026-09-12 because a budget is per part and
    # nothing on this wire named one: `part-token-budgeter` learned a part
    # existed only from an `llm-call-record`, a record needs a call, and a call
    # needs a budget -- so no part could ever get its first allowance. Measured:
    # 0 budgets issued, ever.
    #
    # Defaulted rather than required so a producer that has not been updated
    # keeps working instead of crash-looping; the parts that cannot be budgeted
    # because they did not say who they are are counted by name in
    # `llm-request-router`'s standing rather than silently refused.
    asked_by: str = ""
    # The answer's shape, when the asking part has one of its own. Added
    # 2026-09-13: `prompt-template-author` wrote every purpose's first template
    # with one shared shape -- venue, symbol, text -- and a generic instruction,
    # so a part that correlates its answers by a key it sends
    # (`news-text-structurer`'s `story_key`) could never get that key back and
    # would have paid for every call and used none. A part that states a shape
    # has its own instruction and shape written as the purpose's first template;
    # None keeps the shared one for every part that does not.
    output_schema: dict | None = None
    # A repair of an answer `structured-output-enforcer` rejected. Added 2026-09-13:
    # the enforcer published its repair on this wire as a plain dict, which
    # `prompt-renderer` and `llm-model-picker` read as `request.purpose` -- so the
    # first rejected answer would have crashed both -- and which carried no
    # `asked_by`, so no budget could pay for it. `repair_of` is the rendered id of
    # the first answer in the chain and `repair_attempt` how many repairs this is,
    # so the enforcer's bound counts the chain rather than one render (every
    # repair is a new render, and counting per render never reached the bound).
    # `previous_failures` is what the renderer shows the model.
    repair_of: str = ""
    repair_attempt: int = 0
    previous_failures: str = ""

    @property
    def is_answerable_from_facts(self) -> bool:
        return bool(self.facts)

    @property
    def names_the_asking_part(self) -> bool:
        return bool(self.asked_by)


@dataclass(frozen=True)
class VerifiedText:
    """A model's draft with every unsupported sentence removed, and named."""

    text: str
    kept_sentences: tuple
    removed_sentences: tuple
    unsupported_claims: tuple
    citations: dict
    numbers_checked: int
    was_written_by_a_model: bool

    @property
    def is_fully_supported(self) -> bool:
        return not self.removed_sentences

    @property
    def is_empty(self) -> bool:
        return not self.kept_sentences


def numbers_in(text: str) -> list:
    """Every number a sentence asserts, normalised so 1,234.5% and 1234.5 match."""
    found = []
    for raw in NUMBER_PATTERN.findall(text):
        cleaned = raw.replace(",", "").replace("%", "").strip()
        if not cleaned or cleaned == "-":
            continue
        try:
            value = float(cleaned)
        except ValueError:
            continue
        found.append((raw.strip(), value, raw.strip().endswith("%")))
    return found


def _matches_a_fact(value: float, is_percentage: bool, facts: dict, tolerance: float) -> str | None:
    """The fact this number came from, if any. Percentages match their fractions.

    A model writes 2.5% for a measured 0.025 and 2.5 for a measured 2.5, and both
    are the same claim. Refusing the first would delete true sentences, which
    trains whoever reads these to ignore the removals.
    """
    candidates = [value, value / 100.0] if is_percentage else [value]
    for name, fact in facts.items():
        for stated in _numbers_a_fact_states(fact):
            for candidate in candidates:
                if abs(stated) <= tolerance:
                    if abs(candidate - stated) <= tolerance:
                        return name
                elif abs(candidate - stated) <= abs(stated) * tolerance:
                    return name
    return None


def _numbers_a_fact_states(fact) -> list:
    """The numbers one fact carries: itself if a number, what its text states if text.

    Text facts were skipped until 2026-09-13, and that refused every real answer
    `news-text-structurer` ever got: a news item's facts are its headline and
    body, so "$110 per barrel" copied straight out of the headline traced to
    nothing and both captured `haiku` answers were rejected whole. A number
    written in a fact's own text is as measured as that text is; it is parsed with
    `numbers_in`, the same parser the answer goes through, so the two sides cannot
    disagree about what a number is.
    """
    if isinstance(fact, bool):
        return []
    if isinstance(fact, (int, float)):
        return [fact]
    if isinstance(fact, str):
        return [value for _raw, value, _is_percentage in numbers_in(fact)]
    return []


def split_sentences(text: str) -> list:
    """Sentences, kept whole. The unit of removal is a claim, not a clause."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [part.strip() for part in parts if part.strip()]


def verify_against_facts(
    text: str,
    facts: dict,
    relative_tolerance: float,
    require_a_citation: bool = True,
) -> VerifiedText:
    """Keep only the sentences whose numbers trace to a measurement.

    `require_a_citation` also removes sentences that assert nothing checkable.
    That is the right default for a rationale, where an unfalsifiable sentence is
    the one a reader will believe hardest, and the wrong one for a premortem,
    where "the venue could halt withdrawals" is a real failure mode with no
    number attached.
    """
    kept = []
    removed = []
    unsupported = []
    citations: dict[str, str] = {}
    numbers_checked = 0

    for sentence in split_sentences(text):
        found = numbers_in(sentence)
        numbers_checked += len(found)

        if not found:
            if require_a_citation:
                removed.append(sentence)
                unsupported.append(f"{NO_CITATION}: {sentence}")
            else:
                kept.append(sentence)
            continue

        unmatched = []
        for raw, value, is_percentage in found:
            name = _matches_a_fact(value, is_percentage, facts, relative_tolerance)
            if name is None:
                unmatched.append(raw)
            else:
                citations[raw] = name

        if unmatched:
            removed.append(sentence)
            unsupported.append(
                f"{UNSUPPORTED_NUMBER} ({', '.join(unmatched)}): {sentence}"
            )
        else:
            kept.append(sentence)

    return VerifiedText(
        text=" ".join(kept),
        kept_sentences=tuple(kept),
        removed_sentences=tuple(removed),
        unsupported_claims=tuple(unsupported),
        citations=citations,
        numbers_checked=numbers_checked,
        was_written_by_a_model=True,
    )


def make_request(
    purpose: str,
    venue_id: str,
    symbol: str,
    instruction: str,
    facts: dict,
    maximum_sentences: int,
    now_ns=time.time_ns,
    asked_by: str = "",
    output_schema: dict | None = None,
    repair_of: str = "",
    repair_attempt: int = 0,
    previous_failures: str = "",
) -> LlmRequest:
    """One request, carrying the facts its answer will be checked against.

    `asked_by` is the asking part's own id. A request that does not name it
    cannot be given an allowance -- budgets are per part -- so it will be
    refused by the router and counted there rather than quietly dropped.
    """
    if not facts:
        raise ValueError(
            "a request with no facts asks the model to recall rather than to phrase, and "
            "nothing it returns could be checked"
        )
    if maximum_sentences < 1:
        raise ValueError("a request permitting no sentences cannot be answered")
    return LlmRequest(
        purpose=purpose,
        venue_id=venue_id,
        symbol=symbol,
        instruction=instruction,
        facts=dict(facts),
        maximum_sentences=maximum_sentences,
        requested_at_ns=now_ns(),
        asked_by=asked_by,
        output_schema=dict(output_schema) if output_schema else None,
        repair_of=repair_of,
        repair_attempt=repair_attempt,
        previous_failures=previous_failures,
    )


def written_without_a_model(text: str, facts: dict) -> VerifiedText:
    """The fallback when no model answered: the facts themselves, as one sentence.

    Deliberately plain. A part whose explanation depends on a model being
    available would go silent exactly when the model is down, and a decision with
    no recorded reason is worse than one with a blunt one.
    """
    body = "; ".join(f"{name} {value}" for name, value in sorted(facts.items()))
    return VerifiedText(
        text=body,
        kept_sentences=(body,) if body else (),
        removed_sentences=(),
        unsupported_claims=(),
        citations={str(value): name for name, value in facts.items()},
        numbers_checked=0,
        was_written_by_a_model=False,
    )
