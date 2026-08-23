"""prompt-registry: the versions, which one is live, and what it replaced.

Every prompt this system runs is pinned to an immutable version, and the registry is
where that pinning lives. It exists because of one failure that is otherwise
undetectable: a prompt is edited, the answers change, and nothing anywhere records
that the two things are connected. Weeks later the drop in quality is attributed to
the market.

So the registry holds three rules that make attribution possible:

- **A version is immutable.** Editing an active version rewrites the history of
  every answer that named it. There is no edit method here at all -- the way to
  change a prompt is to register another version.
- **One active version per purpose, and activation happens only through a
  promotion.** A registry that lets anything be activated directly is a registry
  where the evaluation step is optional.
- **What each version replaced is recorded.** A rollback is then a promotion of a
  version that already exists and was already scored, rather than a recovery
  operation.

It also refuses two things that look harmless. A version identical to one already
registered is rejected, because two identical versions split the same evidence
across two score lines. And a version whose template no longer exists is rejected,
since a prompt that cannot be traced to its author's inputs cannot be reasoned about
when it fails.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field

from runtime.llm_types import PromptVersion
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "prompt-registry"

PART_DECLARATION = PartDeclaration(
    part_id="prompt-registry",
    consumes=("prompt-template", "prompt-promotion"),
    produces=("prompt-version", "part-health"),
    resource_class="io-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

REGISTERED = "registered"
IDENTICAL_TO_AN_EXISTING_VERSION = "identical-to-a-version-already-registered"
NO_SUCH_TEMPLATE = "its-template-is-not-registered"
PROMOTED = "promoted"
ALREADY_ACTIVE = "already-the-active-version"
NO_SUCH_VERSION = "no-such-version"


@dataclass(frozen=True)
class RegistryOutcome:
    version: PromptVersion | None
    state: str
    replaced: str | None
    reason: str
    at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state in (REGISTERED, PROMOTED) and self.version is not None


@dataclass
class RegistryStanding:
    templates_seen: int = 0
    versions_registered: int = 0
    duplicates_rejected: int = 0
    orphans_rejected: int = 0
    promotions: int = 0
    rollbacks: int = 0
    purposes_with_an_active_version: int = 0


class PromptRegistry:
    """Immutable versions, one active per purpose, promotion the only way in."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._templates: dict[str, object] = {}
        self._versions: dict[str, PromptVersion] = {}
        self._fingerprints: dict[str, str] = {}
        self._active: dict[str, str] = {}
        self._history: dict[str, list] = {}
        self.standing = RegistryStanding()

    def observe_template(self, template) -> None:
        self._templates[template.template_id] = template
        self.standing.templates_seen += 1

    def register(self, template_id: str) -> RegistryOutcome:
        template = self._templates.get(template_id)
        if template is None:
            self.standing.orphans_rejected += 1
            return self._outcome(
                None, NO_SUCH_TEMPLATE, None,
                f"{template_id} is not registered. A prompt that cannot be traced to its "
                f"author's inputs cannot be reasoned about when it fails",
            )

        fingerprint = self._fingerprint(template)
        existing = self._fingerprints.get(fingerprint)
        if existing is not None:
            self.standing.duplicates_rejected += 1
            return self._outcome(
                self._versions[existing], IDENTICAL_TO_AN_EXISTING_VERSION, None,
                f"identical to {existing}. Two identical versions split the same evidence "
                f"across two score lines",
            )

        sequence = len(self._history.get(template.purpose, [])) + 1
        version = PromptVersion(
            version_id=f"{template_id}:v{sequence}",
            template_id=template_id,
            purpose=template.purpose,
            instruction=template.instruction,
            output_schema=dict(template.output_schema),
            required_context_kinds=tuple(template.required_context_kinds),
            is_active=False,
            promoted_at_ns=None,
            created_at_ns=self._now_ns(),
            supersedes=self._active.get(template.purpose),
        )
        self._versions[version.version_id] = version
        self._fingerprints[fingerprint] = version.version_id
        self._history.setdefault(template.purpose, []).append(version.version_id)
        self.standing.versions_registered += 1
        return self._outcome(
            version, REGISTERED, None,
            f"{version.version_id} registered and inactive. Registration is not "
            f"activation: only a promotion makes a version live",
        )

    def apply_promotion(self, promotion) -> RegistryOutcome:
        """Activation happens only here, and only from a promotion decision."""
        version = self._versions.get(promotion.version_id)
        if version is None:
            return self._outcome(
                None, NO_SUCH_VERSION, None,
                f"{promotion.version_id} was never registered",
            )

        current = self._active.get(version.purpose)
        if current == version.version_id:
            return self._outcome(
                version, ALREADY_ACTIVE, current,
                f"{version.version_id} is already the active version for {version.purpose}",
            )

        promoted = PromptVersion(
            version_id=version.version_id,
            template_id=version.template_id,
            purpose=version.purpose,
            instruction=version.instruction,
            output_schema=version.output_schema,
            required_context_kinds=version.required_context_kinds,
            is_active=True,
            promoted_at_ns=self._now_ns(),
            created_at_ns=version.created_at_ns,
            supersedes=current,
        )
        self._versions[version.version_id] = promoted
        if current is not None:
            previous = self._versions[current]
            self._versions[current] = PromptVersion(
                version_id=previous.version_id,
                template_id=previous.template_id,
                purpose=previous.purpose,
                instruction=previous.instruction,
                output_schema=previous.output_schema,
                required_context_kinds=previous.required_context_kinds,
                is_active=False,
                promoted_at_ns=previous.promoted_at_ns,
                created_at_ns=previous.created_at_ns,
                supersedes=previous.supersedes,
            )
        # Registration order, not the wall clock: two versions registered inside the
        # same clock tick would otherwise never read as a rollback, and the ordering
        # this cares about is which was registered first, not which nanosecond it was.
        was_rollback = current is not None and self._registration_order(
            version.version_id
        ) < self._registration_order(current)
        if version.purpose not in self._active:
            self.standing.purposes_with_an_active_version += 1
        self._active[version.purpose] = version.version_id
        self.standing.promotions += 1
        if was_rollback:
            self.standing.rollbacks += 1

        return self._outcome(
            promoted, PROMOTED, current,
            f"{version.version_id} is now active for {version.purpose}"
            + (
                f", replacing {current}"
                + (
                    ". This is a rollback: the version already existed and was already "
                    "scored, so it is a decision rather than a recovery"
                    if was_rollback
                    else ""
                )
                if current
                else ", which had no active version"
            ),
        )

    def _registration_order(self, version_id: str) -> int:
        history = self._history.get(self._versions[version_id].purpose, [])
        return history.index(version_id) if version_id in history else -1

    def active_version(self, purpose: str) -> PromptVersion | None:
        version_id = self._active.get(purpose)
        return self._versions.get(version_id) if version_id else None

    def version(self, version_id: str) -> PromptVersion | None:
        return self._versions.get(version_id)

    def history(self, purpose: str) -> tuple:
        return tuple(self._history.get(purpose, ()))

    @staticmethod
    def _fingerprint(template) -> str:
        payload = json.dumps(
            {
                "purpose": template.purpose,
                "instruction": template.instruction,
                "schema": template.output_schema,
                "context": sorted(template.required_context_kinds),
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _outcome(self, version, state, replaced, reason) -> RegistryOutcome:
        return RegistryOutcome(
            version=version, state=state, replaced=replaced, reason=reason,
            at_ns=self._now_ns(),
        )


def describe_registry(registry: PromptRegistry) -> dict:
    return {
        "part_id": PART_ID,
        "templates_seen": registry.standing.templates_seen,
        "versions_registered": registry.standing.versions_registered,
        "duplicates_rejected": registry.standing.duplicates_rejected,
        "orphans_rejected": registry.standing.orphans_rejected,
        "promotions": registry.standing.promotions,
        "rollbacks": registry.standing.rollbacks,
        "purposes_with_an_active_version": (
            registry.standing.purposes_with_an_active_version
        ),
        "can_edit_a_version": False,
        "can_activate_without_a_promotion": False,
    }


def run_prompt_registry(
    registry: PromptRegistry, control_socket, read_templates, read_promotions,
    publish_versions, health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for template in read_templates():
            registry.observe_template(template)
            outcome = registry.register(template.template_id)
            if outcome.is_usable:
                publish_versions(outcome.version)
        for promotion in read_promotions():
            outcome = registry.apply_promotion(promotion)
            if outcome.is_usable:
                publish_versions(outcome.version)

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
    """The one entry point every part carries (T-1)."""
    from runtime.input_assembly import Batch

    templates = Batch(read=context.bus.reader("prompt-template"))
    promotions = Batch(read=context.bus.reader("prompt-promotion"))
    publish_versions = context.bus.publisher_for("prompt-version")
    registry = PromptRegistry()

    return run_prompt_registry(
        registry=registry,
        control_socket=context.control_socket,
        read_templates=lambda: templates.payloads(),
        read_promotions=lambda: promotions.payloads(),
        publish_versions=lambda version: publish_versions((version,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
