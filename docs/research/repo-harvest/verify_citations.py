#!/usr/bin/env python3
"""Check every `path/in/repo.ext` cited in a harvest note exists in its clone.

Usage: verify_citations.py <clones-dir>
Exit 1 if any cited path is missing. Prints per-note counts.
"""
import re, sys, pathlib
HERE = pathlib.Path(__file__).parent
clones = pathlib.Path(sys.argv[1])
cite = re.compile(r"`([A-Za-z0-9_./-]+\.(?:py|cs|pyx|pxd|rs|ts|js|json|yml|yaml|toml|md))(?::[^`]*)?`")
bad_total = 0
for note in sorted(HERE.glob("*.md")):
    if note.name.startswith("_"): continue
    name = note.stem
    root = clones / name
    if not root.exists():
        print(f"{name}: NO CLONE at {root}"); bad_total += 1; continue
    text = note.read_text()
    paths = sorted({m.group(1) for m in cite.finditer(text)})
    def found(p):
        if (root / p).exists(): return True
        if "/" not in p:  # bare basename: accept if any file in the clone carries it
            return any(True for _ in root.rglob(p))
        # relative path: accept if exactly one file ends with it
        return any(str(f).endswith("/" + p) for f in root.rglob(p.rsplit("/",1)[1]))
    missing = [p for p in paths if not found(p)]
    print(f"{name}: {len(paths)} cited, {len(missing)} missing" + (": " + ", ".join(missing[:8]) if missing else ""))
    bad_total += len(missing)
sys.exit(1 if bad_total else 0)
