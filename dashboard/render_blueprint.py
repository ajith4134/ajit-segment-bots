#!/usr/bin/env python3
"""Turn the feature registry into the blueprint diagram and its contract checks.

The diagram is computed, never drawn. `docs/features.json` declares what each
feature consumes and produces; the edges between features are derived from those
declarations. A feature never names another feature, so a new part cannot invent
a private wire to an old one — it attaches to data that already exists, or
declares data of its own.

That is also what makes the flow checkable. Three violations are detectable
without knowing anything about what the bot does:

  dangling input   a feature consumes data no feature produces
  orphan output    a feature produces data no feature consumes
  isolated part    a feature neither reads nor feeds anything

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
    stages: list[dict] = field(default_factory=list)
    data_types: list[dict] = field(default_factory=list)
    features: list[dict] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.features


def load_feature_registry(path: Path = REGISTRY_PATH) -> FeatureRegistry:
    """Read the registry. A missing or malformed file yields an empty registry."""
    if not path.is_file():
        return FeatureRegistry()
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        return FeatureRegistry()
    return FeatureRegistry(
        stages=raw.get("stages", []),
        data_types=raw.get("data_types", []),
        features=raw.get("features", []),
    )


def find_contract_violations(registry: FeatureRegistry) -> list[str]:
    """Return every way the declared flow fails to hold together."""
    violations: list[str] = []
    known_types = {entry["id"] for entry in registry.data_types}
    produced: set[str] = set()
    consumed: set[str] = set()
    for feature in registry.features:
        produced.update(feature.get("produces", []))
        consumed.update(feature.get("consumes", []))

    for feature in registry.features:
        name = feature.get("name", feature.get("id", "?"))
        reads = feature.get("consumes", [])
        writes = feature.get("produces", [])
        if not reads and not writes:
            violations.append(f"{name}: isolated — neither consumes nor produces anything")
        for type_id in reads:
            if type_id not in known_types:
                violations.append(f"{name}: consumes undeclared data type '{type_id}'")
            elif type_id not in produced:
                violations.append(f"{name}: dangling input — nothing produces '{type_id}'")
        for type_id in writes:
            if type_id not in known_types:
                violations.append(f"{name}: produces undeclared data type '{type_id}'")
            elif type_id not in consumed:
                violations.append(f"{name}: orphan output — nothing consumes '{type_id}'")

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


def render_mermaid_flowchart(registry: FeatureRegistry) -> str:
    """Emit the diagram source. Grouped by stage when stages are declared."""
    lines = ["flowchart LR"]
    staged = {stage["id"]: [] for stage in registry.stages}
    unstaged = []
    for feature in registry.features:
        target = staged.get(feature.get("stage", ""))
        (target if target is not None else unstaged).append(feature)

    for stage in registry.stages:
        members = staged[stage["id"]]
        if not members:
            continue
        lines.append(f'  subgraph {stage["id"]}["{_mermaid_safe(stage.get("name", stage["id"]))}"]')
        for feature in members:
            lines.append(f'    {feature["id"]}["{_mermaid_safe(feature.get("name", feature["id"]))}"]')
        lines.append("  end")

    for feature in unstaged:
        lines.append(f'  {feature["id"]}["{_mermaid_safe(feature.get("name", feature["id"]))}"]')

    type_names = {entry["id"]: entry.get("name", entry["id"]) for entry in registry.data_types}
    for producer_id, consumer_id, type_id in derive_edges(registry):
        label = _mermaid_safe(type_names.get(type_id, type_id))
        lines.append(f"  {producer_id} -- {label} --> {consumer_id}")

    return "\n".join(lines)


def render_blueprint_section(registry: FeatureRegistry) -> str:
    """The blueprint's region of the board: the diagram, or an honest gap."""
    if registry.is_empty:
        return (
            '<div class="empty-frame">'
            '<div class="headline">Awaiting the feature list</div>'
            "<p>The setup is ready. The registry at <code>docs/features.json</code> is the single "
            "source this diagram is drawn from, and it is empty because the user has not described "
            "the features yet.</p>"
            "<p>Edges here are never hand-drawn. Each feature declares only the data it consumes and "
            "the data it produces, and a connection exists exactly where one part produces what "
            "another consumes. A part can therefore be swapped for a better one without touching "
            "anything else, and a new part cannot wire itself privately into an old one.</p>"
            "<p>Nothing is placeholdered in the meantime.</p>"
            "</div>"
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
        f"<span>{len(registry.features)} features</span>"
        f"<span>{len(registry.data_types)} data types</span>"
        f"<span>{count_derived_edges(registry)} derived edges</span>"
        f"</div>"
        f"{violation_markup}"
        f'<div class="diagram"><pre class="mermaid">{html.escape(render_mermaid_flowchart(registry))}</pre></div>'
    )


if __name__ == "__main__":
    loaded = load_feature_registry()
    print(f"features: {len(loaded.features)}  data types: {len(loaded.data_types)}")
    print(f"derived edges: {count_derived_edges(loaded)}")
    for violation in find_contract_violations(loaded):
        print(f"VIOLATION  {violation}")
    if loaded.features:
        print()
        print(render_mermaid_flowchart(loaded))
