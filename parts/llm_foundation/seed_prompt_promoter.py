"""seed-prompt-promoter: promote a purpose's first version, which no score can reach.

**Sixteen parts publish `llm-request` and not one LLM call has ever been made.**
Nothing was broken. Measured on the live spine, 2026-09-12:

    prompt-registry      2 versions registered, purposes_with_an_active_version 0
    prompt-renderer       906 requests seen, 906 refused_no_active_version, 0 rendered
    llm-request-router    0 requests_seen -- nothing is ever rendered to route
    every caller          0 calls
    structured-output-enforcer  0 responses seen, 0 validated-llm-output ever
    golden-case-keeper    0 cases offered -- a case needs a validated output
    prompt-evaluator      0 cases run    -- a score needs cases and output
    prompt-promotion-gate 0 decisions    -- it consumes prompt-score, and none exists

An active version needs a promotion; a promotion needs a score; a score needs
golden cases and validated output; validated output needs an active version. Every
arrow is right. The ring has no entrance, and this part is the entrance.

## Why this is a separate part and not a flag on the gate

`prompt-promotion-gate` exists to refuse exactly this:

> A version with no incumbent is promoted only if it clears an absolute bar.
> Being the first is not the same as being good enough, and "it is all we have"
> is how an unusable prompt becomes production.

That bar is measured by running golden cases through the model, so for a
purpose's first version it is unreachable rather than strict. Teaching the gate
to promote without a score would delete the one property that makes it worth
having — and it reports `promotes_without_a_score: False` as a standing claim,
so the deletion would be silent. Feeding it a manufactured score is worse: a
fabricated number that clears a bar is treated as a measurement by everything
downstream, `prompt-drift-monitor` included.

So the entrance is its own part, with its own counters, and the gate is
untouched (T-6: grow by adding parts, never by making a part cleverer).

## One door, once per purpose, and never a replacement

- **Only a purpose with no active version, ever.** Any version arriving with
  `is_active` closes its purpose to this part permanently — including one the
  gate promoted on real evidence.
- **Only once.** A purpose this part has seeded is never seeded again, even if
  the seeded version is later retired. A second seed would be a rollback taken
  without evidence, which is the oscillation the gate exists to prevent.
- **`replaces` is None and the reason says the version was never scored**, in
  words, on the promotion itself — so a reader of the registry's history can see
  which versions entered by this door.
- **Countable.** `purposes_seeded` and the purpose names are in the standing, so
  "how much of this system is running on a prompt nobody scored" is a number on
  a board rather than something to remember (Rule 8).

Everything after the first version goes through the gate on evidence, unchanged.
The seed is what makes that evidence obtainable at all: once one prompt runs,
outputs exist, so cases can exist, so scores can exist.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.llm_types import PromptPromotion
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "seed-prompt-promoter"

PART_DECLARATION = PartDeclaration(
    part_id="seed-prompt-promoter",
    consumes=("prompt-version",),
    produces=("prompt-promotion", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="delays",
)

SEEDED = "seeded-as-the-first-version-for-this-purpose"
ALREADY_HAS_AN_ACTIVE_VERSION = "this-purpose-already-has-an-active-version"
ALREADY_SEEDED = "this-purpose-has-already-been-seeded-once"
SEEDING_IS_OFF = "seeding-is-switched-off-by-the-operator"

# What goes on the promotion in place of a margin. Not 0.0 dressed up as a
# measurement: there is no incumbent and no score, and the reason field says so.
NO_MARGIN = 0.0
NO_CASES_RUN = 0


@dataclass
class SeederStanding:
    versions_seen: int = 0
    purposes_seeded: int = 0
    refused_already_active: int = 0
    refused_already_seeded: int = 0
    refused_seeding_is_off: int = 0
    # The purposes now running on a prompt nobody scored. Named, not just
    # counted: "which ones" is the question an operator actually asks.
    seeded_purposes: tuple[str, ...] = ()
    # Purposes closed to this part because a promotion reached them properly.
    purposes_with_an_active_version: int = 0


@dataclass(frozen=True)
class SeedDecision:
    purpose: str
    version_id: str
    state: str
    promotion: PromptPromotion | None
    reason: str
    decided_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == SEEDED and self.promotion is not None


class SeedPromptPromoter:
    """The first version of a purpose, promoted once, on no evidence, in the open."""

    def __init__(self, seeding_is_allowed: bool = True, now_ns=time.time_ns) -> None:
        self._seeding_is_allowed = bool(seeding_is_allowed)
        self._now_ns = now_ns
        self._active_purposes: set[str] = set()
        self._seeded_purposes: list[str] = []
        # The first version seen for a purpose, which is the one that gets
        # seeded. First rather than newest: a purpose whose templates are being
        # rewritten while nothing runs would otherwise never settle on one, and
        # "whichever arrived last" is not a choice anybody made.
        self._first_version_by_purpose: dict[str, object] = {}
        self.standing = SeederStanding()

    def observe_version(self, version) -> None:
        """One version from the registry. Whether its purpose is still open."""
        self.standing.versions_seen += 1
        purpose = str(getattr(version, "purpose", "") or "")
        if not purpose:
            return
        if getattr(version, "is_active", False):
            # Something promoted a version for this purpose. Whether that was
            # this part or the gate, the purpose is closed either way.
            if purpose not in self._active_purposes:
                self._active_purposes.add(purpose)
                self.standing.purposes_with_an_active_version = len(self._active_purposes)
            return
        self._first_version_by_purpose.setdefault(purpose, version)

    def purposes_waiting(self) -> tuple[str, ...]:
        """Purposes with a registered version, no active one, and no seed yet."""
        return tuple(
            sorted(
                purpose
                for purpose in self._first_version_by_purpose
                if purpose not in self._active_purposes
                and purpose not in self._seeded_purposes
            )
        )

    def seed(self, purpose: str) -> SeedDecision:
        """Promote this purpose's first version, or say why not."""
        version = self._first_version_by_purpose[purpose]
        version_id = str(getattr(version, "version_id", "") or "")

        if not self._seeding_is_allowed:
            self.standing.refused_seeding_is_off += 1
            return self._decision(
                purpose, version_id, SEEDING_IS_OFF, None,
                "seeding is switched off, so this purpose stays refused by "
                "prompt-renderer -- which is the state the whole block has been in "
                "since it was built, not a new failure",
            )
        if purpose in self._active_purposes:
            self.standing.refused_already_active += 1
            return self._decision(
                purpose, version_id, ALREADY_HAS_AN_ACTIVE_VERSION, None,
                f"{purpose} already has an active version. This part is the entrance "
                f"to the ring, not a second promotion path",
            )
        if purpose in self._seeded_purposes:
            self.standing.refused_already_seeded += 1
            return self._decision(
                purpose, version_id, ALREADY_SEEDED, None,
                f"{purpose} has been seeded once already. A second seed is a rollback "
                f"taken without evidence, which is the oscillation prompt-promotion-gate "
                f"exists to prevent",
            )

        promotion = PromptPromotion(
            version_id=version_id,
            template_id=str(getattr(version, "template_id", "") or ""),
            purpose=purpose,
            # Nothing is being replaced, and this is not a comparison.
            replaces=None,
            margin=NO_MARGIN,
            cases_run=NO_CASES_RUN,
            reason=(
                f"seeded: the first prompt version for {purpose}, promoted on NO score. "
                f"A first version cannot be scored -- scoring runs golden cases through "
                f"the model, and there are no cases until a prompt has run. Every later "
                f"version for this purpose goes through prompt-promotion-gate on measured "
                f"evidence"
            ),
            promoted_at_ns=self._now_ns(),
        )
        self._seeded_purposes.append(purpose)
        self.standing.purposes_seeded = len(self._seeded_purposes)
        self.standing.seeded_purposes = tuple(self._seeded_purposes)
        return self._decision(
            purpose, version_id, SEEDED, promotion, promotion.reason,
        )

    def _decision(self, purpose, version_id, state, promotion, reason) -> SeedDecision:
        return SeedDecision(
            purpose=purpose, version_id=version_id, state=state, promotion=promotion,
            reason=reason, decided_at_ns=self._now_ns(),
        )


def describe_seeding(seeder: SeedPromptPromoter) -> dict:
    standing = seeder.standing
    return {
        "part_id": PART_ID,
        "versions_seen": standing.versions_seen,
        "purposes_seeded": standing.purposes_seeded,
        "purposes_waiting": len(seeder.purposes_waiting()),
        "purposes_with_an_active_version": standing.purposes_with_an_active_version,
        "refused_already_active": standing.refused_already_active,
        "refused_already_seeded": standing.refused_already_seeded,
        "refused_seeding_is_off": standing.refused_seeding_is_off,
        # Named, because "which purposes are running unscored" is the question.
        "seeded_purposes": list(standing.seeded_purposes),
        # A standing claim, and true by construction: this part promotes only a
        # purpose's first version and only once, and it never compares two.
        "never_promotes_a_replacement": True,
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    versions = Batch(read=context.bus.reader("prompt-version"))
    publish_promotions = context.bus.publisher_for("prompt-promotion")
    seeder = SeedPromptPromoter(
        seeding_is_allowed=bool(context.number("prompt_seeding_is_allowed")),
    )

    def tick() -> None:
        for version in versions.payloads():
            seeder.observe_version(version)
        promotions = []
        for purpose in seeder.purposes_waiting():
            decision = seeder.seed(purpose)
            if decision.is_usable:
                promotions.append(decision.promotion)
        if promotions:
            publish_promotions(tuple(promotions))

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_seeding(seeder),
    )


__all__ = [
    "ALREADY_HAS_AN_ACTIVE_VERSION",
    "ALREADY_SEEDED",
    "PART_DECLARATION",
    "PART_ID",
    "SEEDED",
    "SEEDING_IS_OFF",
    "SeedDecision",
    "SeedPromptPromoter",
    "SeederStanding",
    "describe_seeding",
    "start_part",
]
