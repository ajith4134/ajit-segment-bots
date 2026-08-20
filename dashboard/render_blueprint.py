#!/usr/bin/env python3
"""Turn the feature registry into the blueprint diagram and its contract checks.

The diagram is computed, never drawn. `docs/features.json` declares what each
feature consumes and produces; the edges between features are derived from those
declarations. A feature never names another feature, so a new part cannot invent
a private wire to an old one — it attaches to data that already exists, or
declares data of its own.

That is also what makes the flow checkable. These breaches are detectable without
knowing anything about what the bot does (the rules are in docs/contracts.md):

  R-01  dangling input   a feature consumes data no feature produces
  R-01  orphan output    a feature produces data no feature consumes
  R-01  isolated part    a feature neither reads nor feeds anything
  R-01  undeclared type  a feature references a data type nobody declared
  R-01  homeless part    a feature belongs to no declared category
  R-02  no switch        a feature cannot be turned off and on

Each is reported as a failing check rather than quietly drawn as a gap.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_HOME = Path(__file__).resolve().parent.parent
REGISTRY_PATH = PROJECT_HOME / "docs" / "features.json"


@dataclass
class FeatureRegistry:
    categories: list[dict] = field(default_factory=list)
    data_types: list[dict] = field(default_factory=list)
    features: list[dict] = field(default_factory=list)
    state_vocabulary: list[str] = field(default_factory=list)

    @property
    def has_features(self) -> bool:
        return bool(self.features)

    @property
    def has_categories(self) -> bool:
        return bool(self.categories)

    @property
    def has_declared_flow(self) -> bool:
        """True once data actually moves between parts, not merely blocks existing."""
        return bool(self.data_types) and bool(self.features)


def load_feature_registry(path: Path = REGISTRY_PATH) -> FeatureRegistry:
    """Read the registry. A missing or malformed file yields an empty registry."""
    if not path.is_file():
        return FeatureRegistry()
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        return FeatureRegistry()
    return FeatureRegistry(
        categories=raw.get("categories", []),
        data_types=raw.get("data_types", []),
        features=raw.get("features", []),
        state_vocabulary=raw.get("state_vocabulary", []),
    )


REQUIRED_FEATURE_FIELDS = (
    "id",
    "name",
    "role",
    "category",
    "consumes",
    "produces",
    "switchable",
    "off_releases_resources",
    "states",
)


def find_contract_violations(registry: FeatureRegistry) -> list[str]:
    """Return every breach of R-01 and of the transistor rule, tagged with the rule.

    See docs/contracts.md and docs/transistor-rule.md. Anything reported here is
    a defect in the design, not a warning to weigh up.
    """
    violations: list[str] = []
    known_types = {entry["id"] for entry in registry.data_types}
    known_categories = {entry["id"] for entry in registry.categories}
    known_features = {entry.get("id") for entry in registry.features}
    vocabulary = set(registry.state_vocabulary)

    produced: set[str] = set()
    consumed: set[str] = set()
    for feature in registry.features:
        produced.update(feature.get("produces", []))
        consumed.update(feature.get("consumes", []))

    for feature in registry.features:
        name = feature.get("name", feature.get("id", "?"))
        reads = feature.get("consumes", [])
        writes = feature.get("produces", [])

        # T-1 every feature is the same shape
        for required in REQUIRED_FEATURE_FIELDS:
            if required not in feature:
                violations.append(f"T-1 {name}: missing required field '{required}' — every part is the same shape")
        if feature.get("switchable") is not True:
            violations.append(f"T-1 {name}: not switchable — every part is a switch, no exemptions")

        # T-2 control path separate from data path
        if feature.get("controls"):
            violations.append(
                f"T-2 {name}: declares control over another part — only the resource governor "
                f"drives the control plane, or the clean flow gains a second invisible graph"
            )

        # T-3 off means genuinely off
        if feature.get("off_releases_resources") is not True:
            violations.append(
                f"T-3 {name}: does not release CPU and RAM when off — the governor would turn "
                f"parts off and find the machine just as full"
            )

        # T-4 a part knows nothing about the circuit
        if feature.get("category", "") not in known_categories:
            violations.append(f"T-4 {name}: belongs to no declared category — every part sits in exactly one")
        for type_id in list(reads) + list(writes):
            if type_id in known_features:
                violations.append(
                    f"T-4 {name}: names the part '{type_id}' instead of a data type — parts never know each other"
                )

        # T-5 states are explicit and countable
        declared_states = feature.get("states", [])
        if not declared_states:
            violations.append(f"T-5 {name}: declares no states — a part is always in exactly one known state")
        for state in declared_states:
            if state not in vocabulary:
                violations.append(
                    f"T-5 {name}: state '{state}' is outside the declared vocabulary — a new state is "
                    f"named and added deliberately, never smuggled in"
                )

        # T-6 one part, one responsibility (the checkable symptom only)
        role = feature.get("role", "")
        if " and " in role.lower():
            violations.append(
                f"T-6 {name}: role names more than one responsibility — that is two parts welded together"
            )

        # R-01 the data plane holds together
        if not reads and not writes:
            violations.append(f"R-01 {name}: isolated — neither consumes nor produces anything")
        for type_id in reads:
            if type_id not in known_types:
                violations.append(f"R-01 {name}: consumes undeclared data type '{type_id}'")
            elif type_id not in produced:
                violations.append(f"R-01 {name}: dangling input — nothing produces '{type_id}'")
        for type_id in writes:
            if type_id not in known_types:
                violations.append(f"R-01 {name}: produces undeclared data type '{type_id}'")
            elif type_id not in consumed:
                violations.append(f"R-01 {name}: orphan output — nothing consumes '{type_id}'")

    return violations


def count_derived_edges(registry: FeatureRegistry) -> int:
    return len(derive_edges(registry))


def derive_edges(registry: FeatureRegistry) -> list[tuple[str, str, str]]:
    """Compute (producer id, consumer id, data type id) for every real connection."""
    edges = []
    for producer in registry.features:
        for type_id in producer.get("produces", []):
            for consumer in registry.features:
                if consumer is producer:
                    continue
                if type_id in consumer.get("consumes", []):
                    edges.append((producer["id"], consumer["id"], type_id))
    return edges


def _mermaid_safe(text: str) -> str:
    """Mermaid label text: strip the characters that break its parser."""
    return text.replace('"', "'").replace("\n", " ").replace("|", "/")


def render_category_blocks(registry: FeatureRegistry) -> str:
    """The foundation blocks alone, before any feature or flow has been declared.

    Drawn without edges on purpose: which category feeds which has not been said,
    and an inferred arrow here would later be mistaken for a decision the user made.
    """
    lines = ["flowchart TB"]
    for category in registry.categories:
        label = _mermaid_safe(category.get("name", category["id"]))
        lines.append(f'  {category["id"]}["{label}"]')
    return "\n".join(lines)


def render_mermaid_flowchart(registry: FeatureRegistry) -> str:
    """Emit the diagram source. Features grouped into the categories they belong to."""
    if not registry.has_features:
        return render_category_blocks(registry)

    lines = ["flowchart LR"]
    grouped = {category["id"]: [] for category in registry.categories}
    ungrouped = []
    for feature in registry.features:
        target = grouped.get(feature.get("category", ""))
        (target if target is not None else ungrouped).append(feature)

    for category in registry.categories:
        members = grouped[category["id"]]
        if not members:
            continue
        lines.append(f'  subgraph {category["id"]}["{_mermaid_safe(category.get("name", category["id"]))}"]')
        for feature in members:
            lines.append(f'    {feature["id"]}["{_mermaid_safe(feature.get("name", feature["id"]))}"]')
        lines.append("  end")

    for feature in ungrouped:
        lines.append(f'  {feature["id"]}["{_mermaid_safe(feature.get("name", feature["id"]))}"]')

    type_names = {entry["id"]: entry.get("name", entry["id"]) for entry in registry.data_types}
    for producer_id, consumer_id, type_id in derive_edges(registry):
        label = _mermaid_safe(type_names.get(type_id, type_id))
        lines.append(f"  {producer_id} -- {label} --> {consumer_id}")

    return "\n".join(lines)


def render_category_list(registry: FeatureRegistry) -> str:
    """Every foundation block, labelled with where it came from."""
    cards = []
    for category in registry.categories:
        origin = category.get("origin", "unknown")
        cards.append(
            f'<article class="category {html.escape(origin)}">'
            f'<div class="category-name">{html.escape(category.get("name", category["id"]))}</div>'
            f'<div class="category-origin">{html.escape(origin)}</div>'
            f'<p>{html.escape(category.get("summary", ""))}</p>'
            f"</article>"
        )
    return f'<div class="category-grid">{"".join(cards)}</div>'


def render_blueprint_section(registry: FeatureRegistry) -> str:
    """The blueprint's region of the board: the diagram, or an honest gap."""
    if not registry.has_categories:
        return (
            '<div class="empty-frame">'
            '<div class="headline">Awaiting the foundation blocks</div>'
            "<p>The setup is ready. <code>docs/features.json</code> is the single source this "
            "diagram is drawn from, and it holds nothing yet.</p>"
            "</div>"
        )

    if not registry.has_features:
        return (
            f'<div class="diagram-meta">'
            f"<span>{len(registry.categories)} categories</span>"
            f"<span>0 features described</span>"
            f"<span>flow not declared</span>"
            f"</div>"
            f'<div class="diagram"><pre class="mermaid">'
            f"{html.escape(render_category_blocks(registry))}</pre></div>"
            f'<div class="empty-frame">'
            f'<div class="headline">Blocks stand, flow not yet drawn</div>'
            f"<p>These are the foundation blocks. They are shown without arrows on purpose: which "
            f"category feeds which has not been said yet, and an inferred arrow here would later be "
            f"mistaken for a decision that was made.</p>"
            f"<p>The flow is the next thing to establish, and it is what the whole blueprint is "
            f"judged on.</p>"
            f"</div>"
            f"{render_category_list(registry)}"
        )

    violations = find_contract_violations(registry)
    violation_markup = ""
    if violations:
        items = "".join(f"<li>{html.escape(v)}</li>" for v in violations)
        violation_markup = (
            f'<div class="violations"><div class="violations-head">'
            f"{len(violations)} contract violation(s)</div><ul>{items}</ul></div>"
        )

    return (
        f'<div class="diagram-meta">'
        f"<span>{len(registry.categories)} categories</span>"
        f"<span>{len(registry.features)} features</span>"
        f"<span>{len(registry.data_types)} data types</span>"
        f"<span>{count_derived_edges(registry)} derived edges</span>"
        f"</div>"
        f"{violation_markup}"
        f'<div class="diagram"><pre class="mermaid">{html.escape(render_mermaid_flowchart(registry))}</pre></div>'
        f"{render_category_list(registry)}"
    )


if __name__ == "__main__":
    loaded = load_feature_registry()
    print(f"categories: {len(loaded.categories)}  features: {len(loaded.features)}  data types: {len(loaded.data_types)}")
    print(f"derived edges: {count_derived_edges(loaded)}")
    for violation in find_contract_violations(loaded):
        print(f"VIOLATION  {violation}")
    print()
    print(render_mermaid_flowchart(loaded))
