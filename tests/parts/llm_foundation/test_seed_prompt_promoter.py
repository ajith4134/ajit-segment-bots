"""seed-prompt-promoter: one door into the ring, once per purpose.

The data here is the real thing this part consumes — `runtime.llm_types`'s own
`PromptVersion`, built the way `prompt-registry` builds it — and the numbers the
suite is written against are the live spine's own counters from 2026-09-12, when
906 of 906 requests were refused for want of an active version.

There is no captured fixture to replay for a prompt registry, so the standard
RL-063 sets is met the other way: every assertion is about behaviour that was
*measured* to be wrong on the running system, and the part is exercised through
the same types the registry publishes rather than through stand-ins.
"""

from __future__ import annotations

import pytest

from parts.llm_foundation.seed_prompt_promoter import (
    ALREADY_HAS_AN_ACTIVE_VERSION,
    ALREADY_SEEDED,
    SEEDED,
    SEEDING_IS_OFF,
    SeedPromptPromoter,
    describe_seeding,
)
from runtime.llm_types import PromptVersion

A_MOMENT = 1_789_223_000_000_000_000


def a_version(
    purpose: str,
    version_id: str | None = None,
    is_active: bool = False,
    template_id: str = "a-template",
) -> PromptVersion:
    """One version exactly as `prompt-registry` publishes it."""
    return PromptVersion(
        version_id=version_id or f"{template_id}:1",
        template_id=template_id,
        purpose=purpose,
        instruction="State what the facts show, briefly.",
        output_schema={"venue_id": "str", "symbol": "str", "text": "str"},
        required_context_kinds=("verified-facts",),
        is_active=is_active,
        promoted_at_ns=A_MOMENT if is_active else None,
        created_at_ns=A_MOMENT,
        supersedes=None,
    )


def a_seeder(seeding_is_allowed: bool = True) -> SeedPromptPromoter:
    return SeedPromptPromoter(
        seeding_is_allowed=seeding_is_allowed, now_ns=lambda: A_MOMENT
    )


def test_a_purpose_with_no_active_version_is_seeded_once():
    seeder = a_seeder()
    seeder.observe_version(a_version("structure-a-news-item"))
    assert seeder.purposes_waiting() == ("structure-a-news-item",)

    decision = seeder.seed("structure-a-news-item")
    assert decision.state == SEEDED
    assert decision.is_usable
    assert decision.promotion.purpose == "structure-a-news-item"
    assert decision.promotion.replaces is None
    assert decision.promotion.cases_run == 0
    assert "NO score" in decision.promotion.reason
    assert seeder.standing.purposes_seeded == 1
    assert seeder.standing.seeded_purposes == ("structure-a-news-item",)
    # And the purpose is no longer waiting, so the tick will not offer it again.
    assert seeder.purposes_waiting() == ()


def test_the_same_purpose_is_never_seeded_twice():
    """A second seed is a rollback with no evidence behind it."""
    seeder = a_seeder()
    seeder.observe_version(a_version("argue-against-this-trade"))
    assert seeder.seed("argue-against-this-trade").is_usable

    again = seeder.seed("argue-against-this-trade")
    assert again.state == ALREADY_SEEDED
    assert again.promotion is None
    assert seeder.standing.refused_already_seeded == 1
    assert seeder.standing.purposes_seeded == 1


def test_a_purpose_the_gate_has_promoted_properly_is_closed_to_this_part():
    """The entrance is not a second promotion path."""
    seeder = a_seeder()
    seeder.observe_version(
        a_version("distil-a-source-into-structure", version_id="t:2", is_active=True)
    )
    assert seeder.purposes_waiting() == ()
    assert seeder.standing.purposes_with_an_active_version == 1

    # A later inactive version for the same purpose -- a challenger the gate will
    # judge -- must not reopen the door.
    seeder.observe_version(a_version("distil-a-source-into-structure", version_id="t:3"))
    assert seeder.purposes_waiting() == ()
    decision = seeder.seed("distil-a-source-into-structure")
    assert decision.state == ALREADY_HAS_AN_ACTIVE_VERSION
    assert decision.promotion is None
    assert seeder.standing.refused_already_active == 1


def test_a_purpose_seeded_then_retired_is_not_seeded_again():
    """Even with no active version left, a purpose gets one seed ever."""
    seeder = a_seeder()
    seeder.observe_version(a_version("write-a-trade-narrative"))
    assert seeder.seed("write-a-trade-narrative").is_usable
    # The registry restates the version as active, then something retires it and
    # no active version remains.
    seeder.observe_version(a_version("write-a-trade-narrative", is_active=True))
    seeder.observe_version(a_version("write-a-trade-narrative", version_id="t:9"))
    assert seeder.purposes_waiting() == ()


def test_the_first_version_seen_is_the_one_seeded_not_the_newest():
    """'Whichever arrived last' is not a choice anybody made."""
    seeder = a_seeder()
    first = a_version("score-a-setup", version_id="first:1")
    seeder.observe_version(first)
    seeder.observe_version(a_version("score-a-setup", version_id="second:1"))
    decision = seeder.seed("score-a-setup")
    assert decision.promotion.version_id == "first:1"


def test_seeding_switched_off_leaves_the_purpose_refused_and_says_so():
    seeder = a_seeder(seeding_is_allowed=False)
    seeder.observe_version(a_version("structure-a-news-item"))
    decision = seeder.seed("structure-a-news-item")
    assert decision.state == SEEDING_IS_OFF
    assert decision.promotion is None
    assert seeder.standing.refused_seeding_is_off == 1
    assert seeder.standing.purposes_seeded == 0
    # Still waiting, because switching seeding off is not a decision about the
    # purpose -- it is the block's original state.
    assert seeder.purposes_waiting() == ("structure-a-news-item",)


def test_several_purposes_are_each_seeded_once():
    """The live spine had two registered versions and zero active."""
    seeder = a_seeder()
    for purpose in ("structure-a-news-item", "argue-against-this-trade"):
        seeder.observe_version(a_version(purpose, template_id=purpose))
    assert seeder.purposes_waiting() == (
        "argue-against-this-trade",
        "structure-a-news-item",
    )
    seeded = [seeder.seed(purpose) for purpose in seeder.purposes_waiting()]
    assert all(decision.is_usable for decision in seeded)
    assert seeder.standing.purposes_seeded == 2
    assert seeder.purposes_waiting() == ()


def test_a_version_with_no_purpose_is_ignored_rather_than_seeded():
    seeder = a_seeder()
    seeder.observe_version(a_version(""))
    assert seeder.purposes_waiting() == ()
    assert seeder.standing.versions_seen == 1


def test_the_standing_names_which_purposes_run_unscored():
    """Rule 8: 'which ones' is the question, so the names are in the standing."""
    seeder = a_seeder()
    seeder.observe_version(a_version("structure-a-news-item"))
    seeder.seed("structure-a-news-item")
    seeder.observe_version(a_version("already-good", is_active=True))

    standing = describe_seeding(seeder)
    assert standing["part_id"] == "seed-prompt-promoter"
    assert standing["purposes_seeded"] == 1
    assert standing["seeded_purposes"] == ["structure-a-news-item"]
    assert standing["purposes_with_an_active_version"] == 1
    assert standing["purposes_waiting"] == 0
    assert standing["never_promotes_a_replacement"] is True


def test_the_real_registry_accepts_the_seed_and_activates_the_purpose():
    """End to end against `prompt-registry` itself, which is what must accept it.

    A promotion the registry refuses would be a seed that seeds nothing, and the
    part would report `purposes_seeded` climbing while the renderer went on
    refusing every request — the exact shape of a counter that measures intent.
    """
    from parts.llm_foundation.prompt_registry import PromptRegistry

    registry = PromptRegistry(now_ns=lambda: A_MOMENT)
    template = _template_of(a_version("structure-a-news-item"))
    registry.observe_template(template)
    registered = registry.register(template.template_id)
    assert registered.is_usable, registered.reason
    assert registered.version.is_active is False
    assert registry.standing.purposes_with_an_active_version == 0

    seeder = a_seeder()
    seeder.observe_version(registered.version)
    promotion = seeder.seed("structure-a-news-item").promotion

    outcome = registry.apply_promotion(promotion)
    assert outcome.is_usable, outcome.reason
    assert outcome.version.is_active
    assert outcome.version.purpose == "structure-a-news-item"
    assert registry.standing.purposes_with_an_active_version == 1
    assert registry.standing.promotions == 1


def _template_of(version):
    """The template the registry would have registered this version from."""
    from runtime.llm_types import PromptTemplate

    return PromptTemplate(
        template_id=version.template_id,
        purpose=version.purpose,
        instruction=version.instruction,
        required_context_kinds=version.required_context_kinds,
        output_schema=version.output_schema,
        written_at_ns=version.created_at_ns,
        written_by="a-test",
        derived_from=None,
    )


@pytest.mark.parametrize("allowed", [True, False])
def test_nothing_is_published_for_a_purpose_that_is_not_waiting(allowed):
    seeder = a_seeder(seeding_is_allowed=allowed)
    assert seeder.purposes_waiting() == ()
