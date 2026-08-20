#!/usr/bin/env python3
"""Refuse a design that breaks the contract rules. Exits non-zero on any breach.

The rules are in docs/contracts.md and docs/transistor-rule.md. This is what
makes them hold without anyone being reminded: the git pre-commit hook runs it,
so a commit carrying a violation is refused rather than debated.

    python3 dashboard/check_contracts.py

Bypass, only when you mean it and can say why:  git commit --no-verify
"""

from __future__ import annotations

import sys

from render_blueprint import find_contract_violations, load_feature_registry


def report_contract_violations() -> int:
    registry = load_feature_registry()
    violations = find_contract_violations(registry)

    if not registry.features:
        print("no features declared yet — nothing to check")
        return 0

    if not violations:
        print(f"{len(registry.features)} features, {len(registry.categories)} categories: all contracts hold")
        return 0

    print(f"{len(violations)} contract violation(s):\n", file=sys.stderr)
    for violation in violations:
        print(f"  {violation}", file=sys.stderr)
    print(
        "\nRules: docs/contracts.md and docs/transistor-rule.md."
        "\nThese are defects in the design, not warnings to weigh up.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(report_contract_violations())
