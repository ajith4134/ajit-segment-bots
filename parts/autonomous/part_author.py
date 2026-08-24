"""part-author: the system writing a new part for itself, inside the same rules.

This is the part that writes parts, and the only thing that makes it safe is that it
has no privileges. What it produces is a **proposal**, a different type from a running
part, and it goes through exactly the gates a human-written part would.

Everything here is a constraint on what may be proposed, because a part author with a
free hand does not produce a system that grows -- it produces one that accumulates
special cases:

- **It fills a recorded gap or it writes nothing.** A part with no gap behind it is a
  part nobody could say was needed, and a system that writes those grows without
  getting better at anything.
- **It obeys the transistor rules.** Same shape as every other part (T-1), names data
  and never another part (T-4), declares its resource class and rate risk so the
  governor can switch it (T-2, T-5). A proposal violating any of these is refused
  here, before it reaches a gate.
- **It must produce something.** A part that consumes and produces nothing is a
  consumer of resources with no output, and it will pass every functional test.
- **Its data types must already exist, or it must declare the new one explicitly.** A
  part inventing a data type nobody produces will never receive an input and will look
  healthy forever.
- **Tests come with it.** A proposal without tests cannot be admitted, so writing it
  without them is writing something that can never run.

The model may draft the code; the constraints above decide admission, and they are
checked structurally rather than by reading what the model says about its own work.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.autonomy_types import ProposedPart
from runtime.claim_verification import make_request
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part

PART_ID = "part-author"

PART_DECLARATION = PartDeclaration(
    part_id="part-author",
    consumes=("capability-gap", "validated-llm-output", "folded-circuit-map"),
    produces=("proposed-part", "llm-request", "part-health"),
    resource_class="compute-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

PROPOSED = "proposed"
AWAITING_A_DRAFT = "the-shape-is-decided-and-the-code-is-being-drafted"
NO_GAP = "no-recorded-gap-justifies-this-part"
PRODUCES_NOTHING = "it-produces-nothing"
NAMES_ANOTHER_PART = "it-names-another-part-instead-of-data"
UNDECLARED_DATA = "it-consumes-a-data-type-nothing-produces"
NO_TESTS = "it-comes-without-tests-so-it-could-never-be-admitted"
INCOMPLETE_DECLARATION = "it-does-not-declare-what-the-governor-needs"
ALREADY_PROPOSED = "a-proposal-for-this-gap-already-exists"

RESOURCE_CLASSES = ("io-bound", "compute-bound", "bandwidth-bound")
RATE_RISKS = ("latency-only", "changes-the-answer")
SKIPPED_TICK_EFFECTS = ("delays", "corrupts")


@dataclass(frozen=True)
class AuthorOutcome:
    part_id: str
    state: str
    proposal: ProposedPart | None
    request: object | None
    violations: tuple
    reason: str
    proposed_at_ns: int

    @property
    def is_usable(self) -> bool:
        return self.state == PROPOSED and self.proposal is not None


@dataclass
class AuthorStanding:
    proposals_attempted: int = 0
    proposals_written: int = 0
    refused_no_gap: int = 0
    refused_produces_nothing: int = 0
    refused_names_a_part: int = 0
    refused_undeclared_data: int = 0
    refused_no_tests: int = 0
    refused_incomplete_declaration: int = 0
    duplicates: int = 0
    requests_made: int = 0
    parts_admitted_by_itself: int = 0


class PartAuthor:
    """Writes proposals that obey the transistor rules, or writes nothing."""

    def __init__(self, now_ns=time.time_ns) -> None:
        self._now_ns = now_ns
        self._gaps: dict[str, object] = {}
        self._known_parts: set = set()
        self._produced_data: set = set()
        self._proposals: dict[str, ProposedPart] = {}
        self._by_gap: dict[str, str] = {}
        self.standing = AuthorStanding()

    def observe_gap(self, gap) -> None:
        if gap.is_actionable and gap.would_be_a_new_part:
            self._gaps[gap.gap_id] = gap

    def observe_existing_part(self, part_id: str, produces) -> None:
        self._known_parts.add(part_id)
        self._produced_data.update(produces)

    def violations_in(
        self, part_id: str, consumes, produces, resource_class, rate_risk,
        skipped_tick_effect, tests, declares_new_data,
    ) -> tuple:
        found = []

        if not produces:
            found.append(PRODUCES_NOTHING)

        # T-4: a part names data, never another part.
        if set(consumes) & self._known_parts or set(produces) & self._known_parts:
            found.append(NAMES_ANOTHER_PART)

        unknown = (
            set(consumes) - self._produced_data - set(declares_new_data) - {"part-health"}
        )
        if unknown:
            found.append(UNDECLARED_DATA)

        if not tests.strip():
            found.append(NO_TESTS)

        if (
            resource_class not in RESOURCE_CLASSES
            or rate_risk not in RATE_RISKS
            or skipped_tick_effect not in SKIPPED_TICK_EFFECTS
        ):
            found.append(INCOMPLETE_DECLARATION)

        return tuple(found)

    def propose(
        self, gap_id: str, part_id: str, consumes, produces, resource_class: str,
        rate_risk: str, skipped_tick_effect: str, source: str, tests: str,
        declares_new_data=(), written_by: str = PART_ID,
    ) -> AuthorOutcome:
        self.standing.proposals_attempted += 1

        if gap_id not in self._gaps:
            self.standing.refused_no_gap += 1
            return self._outcome(
                part_id, NO_GAP, None, None, (NO_GAP,),
                f"{gap_id} is not a recorded, actionable gap. A part with no gap behind "
                f"it is a part nobody could say was needed, and writing those grows a "
                f"system without making it better at anything",
            )

        if gap_id in self._by_gap:
            self.standing.duplicates += 1
            return self._outcome(
                part_id, ALREADY_PROPOSED, self._proposals[self._by_gap[gap_id]], None,
                (),
                f"a proposal for {gap_id} already exists",
            )

        violations = self.violations_in(
            part_id, consumes, produces, resource_class, rate_risk,
            skipped_tick_effect, tests, declares_new_data,
        )
        if violations:
            for violation in violations:
                counter = {
                    PRODUCES_NOTHING: "refused_produces_nothing",
                    NAMES_ANOTHER_PART: "refused_names_a_part",
                    UNDECLARED_DATA: "refused_undeclared_data",
                    NO_TESTS: "refused_no_tests",
                    INCOMPLETE_DECLARATION: "refused_incomplete_declaration",
                }[violation]
                setattr(self.standing, counter, getattr(self.standing, counter) + 1)
            return self._outcome(
                part_id, violations[0], None, None, violations,
                "; ".join(self._explain(violation) for violation in violations),
            )

        proposal = ProposedPart(
            proposal_id=f"proposal-{part_id}",
            part_id=part_id,
            consumes=tuple(consumes),
            produces=tuple(produces),
            resource_class=resource_class,
            rate_risk=rate_risk,
            skipped_tick_effect=skipped_tick_effect,
            source=source,
            tests=tests,
            fills_gap=gap_id,
            written_by=written_by,
            proposed_at_ns=self._now_ns(),
        )
        self._proposals[proposal.proposal_id] = proposal
        self._by_gap[gap_id] = proposal.proposal_id
        self.standing.proposals_written += 1

        return self._outcome(
            part_id, PROPOSED, proposal, None, (),
            f"{part_id} proposed for {gap_id}, consuming {len(consumes)} data type(s) "
            f"and producing {len(produces)}. It is a proposal, not a part: it goes "
            f"through exactly the gates a human-written part would",
        )

    def ask_for_a_draft(self, gap, part_id: str, consumes, produces) -> object:
        """A model may draft the code; the constraints above still decide admission."""
        request = make_request(
            purpose="draft-a-part",
            venue_id="none",
            symbol="none",
            instruction=(
                f"Write a part named {part_id} that fills this gap: {gap.description}. "
                f"It must consume only {', '.join(consumes)}, produce "
                f"{', '.join(produces)}, name data rather than any other part, and come "
                f"with tests."
            ),
            facts={"evidence-count": float(len(gap.evidence))},
            maximum_sentences=200,
            now_ns=self._now_ns,
        )
        self.standing.requests_made += 1
        return request

    @staticmethod
    def _explain(violation: str) -> str:
        return {
            PRODUCES_NOTHING: (
                "it produces nothing, which is a consumer of resources with no output "
                "that will pass every functional test"
            ),
            NAMES_ANOTHER_PART: (
                "it names another part instead of data, which breaks T-4 and makes the "
                "part impossible to swap out"
            ),
            UNDECLARED_DATA: (
                "it consumes a data type nothing produces, so it would never receive an "
                "input and would look healthy forever"
            ),
            NO_TESTS: (
                "it comes without tests, so it could never be admitted -- writing it "
                "without them is writing something that can never run"
            ),
            INCOMPLETE_DECLARATION: (
                "it does not declare its resource class, rate risk and skipped-tick "
                "effect, so the governor could not switch it"
            ),
        }.get(violation, violation)

    def _outcome(
        self, part_id, state, proposal, request, violations, reason,
    ) -> AuthorOutcome:
        return AuthorOutcome(
            part_id=part_id, state=state, proposal=proposal, request=request,
            violations=violations, reason=reason, proposed_at_ns=self._now_ns(),
        )


def describe_authoring(author: PartAuthor) -> dict:
    return {
        "part_id": PART_ID,
        "proposals_attempted": author.standing.proposals_attempted,
        "proposals_written": author.standing.proposals_written,
        "refused_no_gap": author.standing.refused_no_gap,
        "refused_produces_nothing": author.standing.refused_produces_nothing,
        "refused_names_another_part": author.standing.refused_names_a_part,
        "refused_undeclared_data": author.standing.refused_undeclared_data,
        "refused_no_tests": author.standing.refused_no_tests,
        "refused_incomplete_declaration": (
            author.standing.refused_incomplete_declaration
        ),
        "duplicates": author.standing.duplicates,
        "requests_made": author.standing.requests_made,
        "admits_its_own_parts": False,
        "parts_admitted_by_itself": author.standing.parts_admitted_by_itself,
        "writes_a_part_without_a_gap": False,
    }


def run_part_author(
    author: PartAuthor, control_socket, read_gaps, publish_proposals, publish_requests,
    health_interval_seconds: float, emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    def tick() -> None:
        for job in read_gaps(author):
            outcome = author.propose(**job)
            if outcome.request is not None:
                publish_requests(outcome.request)
            if outcome.is_usable:
                publish_proposals(outcome.proposal)

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

    A new actionable gap goes out as one draft request; the request leaves the
    contract to the draft, because the author cannot know what a part it has
    not seen should consume. A draft comes back as a validated output for this
    part's purpose whose value declares the whole part -- contract, source and
    tests -- and only then is a proposal attempted, against the same checks a
    hand-written part would face. Existing parts and their produced data come
    from the blueprint; the folded map is consumed as the evidence a gap rests
    on and drained here, since the gap finder has already turned it into gaps.
    """
    from runtime.input_assembly import Batch

    gaps = Batch(read=context.bus.reader("capability-gap"))
    outputs = Batch(read=context.bus.reader("validated-llm-output"))
    maps = Batch(read=context.bus.reader("folded-circuit-map"))
    publish_proposals = context.bus.publisher_for("proposed-part")
    publish_requests = context.bus.publisher_for("llm-request")

    from runtime.wiring_plan import load_blueprint

    author = PartAuthor()
    for feature in load_blueprint()["features"]:
        author.observe_existing_part(
            feature["id"], produces=tuple(feature.get("produces", ()))
        )

    requested: set = set()

    def read_gaps(_author):
        maps.payloads()
        for gap in gaps.payloads():
            author.observe_gap(gap)
            if (
                gap.is_actionable
                and gap.would_be_a_new_part
                and gap.gap_id not in requested
            ):
                requested.add(gap.gap_id)
                part_id = f"authored-{gap.gap_id.replace(':', '-')}"
                publish_requests(
                    (author.ask_for_a_draft(gap, part_id, (), ()),)
                )
        jobs = []
        for output in outputs.payloads():
            if output.purpose != "draft-a-part":
                continue
            value = output.value if isinstance(output.value, dict) else {}
            jobs.append(
                {
                    "gap_id": str(value.get("gap_id", "")),
                    "part_id": str(value.get("part_id", "")),
                    "consumes": tuple(value.get("consumes", ()) or ()),
                    "produces": tuple(value.get("produces", ()) or ()),
                    "resource_class": str(value.get("resource_class", "")),
                    "rate_risk": str(value.get("rate_risk", "")),
                    "skipped_tick_effect": str(value.get("skipped_tick_effect", "")),
                    "source": str(value.get("source", "")),
                    "tests": str(value.get("tests", "")),
                    "declares_new_data": tuple(
                        value.get("declares_new_data", ()) or ()
                    ),
                }
            )
        return tuple(jobs)

    return run_part_author(
        author=author,
        control_socket=context.control_socket,
        read_gaps=read_gaps,
        publish_proposals=lambda proposal: publish_proposals((proposal,)),
        publish_requests=lambda request: publish_requests((request,)),
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
