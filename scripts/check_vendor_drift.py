#!/usr/bin/env python
"""Check a vendored copy of ``massingbill.core`` against its manifest.

    python check_vendor_drift.py [kit-dir]

Ships inside the kit and runs in the consumer's repo, so it is **standard
library only and not pytest** -- it has to work in a harness that may have
neither installed.

Exits non-zero on any drift, so it belongs in CI. A pin nobody re-checks records
what was true once; a pin checked on every build records what is true now, and
the difference is the entire point of recording it.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(root: Path) -> int:
    manifest_path = root / "MANIFEST.json"
    if not manifest_path.exists():
        print(f"No MANIFEST.json in {root}", file=sys.stderr)
        return 2

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected: dict[str, str] = manifest["files"]

    missing: list[str] = []
    changed: list[str] = []

    for name, want in sorted(expected.items()):
        path = root / name
        if not path.exists():
            missing.append(name)
        elif digest(path) != want:
            changed.append(name)

    # The other direction: a file added to the vendored copy is drift too, and
    # the one most likely to be someone's local fix that upstream never hears
    # about.
    present = {
        p.relative_to(root).as_posix()
        for p in (root / "massingbill").rglob("*.py")
        if "__pycache__" not in p.parts
    }
    extra = sorted(present - set(expected))

    if not (missing or changed or extra):
        print(
            f"massingbill.core matches its pin "
            f"({manifest['commit'][:12]}, {len(expected)} files, "
            f"{len(manifest.get('runtime_dependencies', []))} runtime deps)."
        )
        return 0

    print(f"Vendored massingbill.core has drifted from {manifest['commit'][:12]}:", file=sys.stderr)
    for name in missing:
        print(f"  missing  {name}", file=sys.stderr)
    for name in changed:
        print(f"  changed  {name}", file=sys.stderr)
    for name in extra:
        print(f"  added    {name}", file=sys.stderr)
    print(
        "\nRe-vendor from upstream, or send the change back so the next kit "
        "carries it. A local edit that upstream never hears about is how two "
        "implementations start.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(check(Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()))
