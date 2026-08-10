#!/usr/bin/env python
"""Build a vendorable copy of ``massingbill/core/`` with a verifiable pin.

    python scripts/make_vendor_kit.py [output-dir]

**Refuses to run on a dirty working tree.** That is the whole reason this is a
script rather than a `cp -r`: massingplan's kit was generated from a dirty tree,
so the SHA it recorded did not describe the files it copied, and the consumer's
drift guard could not be trusted enough to install. A pin that might be wrong is
worse than no pin, because it is believed.

The kit is the package directory, not loose files. Consumers drop
``massingbill/core/`` somewhere on their path and the internal imports resolve
unchanged -- the same shape massingplan's ``massingplan/core/`` uses.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "massingbill" / "core"

#: What a consumer needs alongside the code to decide whether to trust it.
DOCS = ["docs/vendorable-core.md", "LICENSE"]


def git(*args: str) -> str:
    return subprocess.run(  # noqa: S603 - our own literal arguments
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def require_clean_tree() -> str:
    """The pin must describe what was copied. Return the commit it pins to."""
    dirty = git("status", "--porcelain")
    if dirty:
        print(
            "The working tree is dirty, so a recorded SHA would not describe the "
            "files this copies:\n\n"
            + "\n".join(f"  {line}" for line in dirty.splitlines())
            + "\n\nCommit first. A pin that might be wrong is worse than no pin, "
            "because a consumer believes it.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    return git("rev-parse", "HEAD")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(output: Path) -> None:
    commit = require_clean_tree()

    if output.exists():
        shutil.rmtree(output)
    (output / "massingbill" / "core").mkdir(parents=True)

    files: dict[str, str] = {}
    for source in sorted(CORE.rglob("*.py")):
        if "__pycache__" in source.parts:
            continue
        relative = source.relative_to(CORE)
        target = output / "massingbill" / "core" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        files[f"massingbill/core/{relative.as_posix()}"] = digest(source)

    for name in DOCS:
        source = ROOT / name
        if source.exists():
            shutil.copy2(source, output / Path(name).name)

    manifest = {
        "package": "massingbill",
        "component": "massingbill.core",
        "version": _version(),
        "commit": commit,
        "repository": "https://github.com/MassingCloud/massingbill",
        "licence": "MIT",
        "runtime_dependencies": [],
        "python_requires": ">=3.11",
        "files": files,
        "tests": sorted(
            name.rsplit("/", 1)[-1] for name in files if "/tests/test_mb_" in name
        ),
    }
    (output / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    shutil.copy2(ROOT / "scripts" / "check_vendor_drift.py", output / "check_vendor_drift.py")
    (output / "README.md").write_text(_readme(manifest), encoding="utf-8")

    print(f"Vendor kit written to {output.resolve()}")
    print(f"  pinned to  {commit}")
    print(f"  {len(files)} file(s), {len(manifest['tests'])} test module(s)")
    print("  runtime dependencies: none")


def _version() -> str:
    text = (ROOT / "massingbill" / "__init__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"')
    return "unknown"


def _readme(manifest: dict[str, object]) -> str:
    tests = "\n".join(f"python massingbill/core/tests/{name}" for name in manifest["tests"])  # type: ignore[union-attr]
    return f"""# massingbill.core — vendorable kit

Pinned to `{manifest["commit"]}` (massingbill {manifest["version"]}), MIT.

**Zero runtime dependencies.** Standard library only, Python {manifest["python_requires"]}.

## Install

Copy `massingbill/` to somewhere on your path. Internal imports resolve
unchanged; there is nothing to rewrite.

```
cp -r massingbill/ <your-project>/vendor/
```

## Verify what you received

```
python check_vendor_drift.py .
```

Checks every file against the SHA-256 in `MANIFEST.json`. Run it again after any
upgrade, and in CI — that is what makes the pin worth having.

## Run the tests

Deliberately not pytest: plain `assert`, a `__main__` runner, standard library
only, so they work in whatever harness you have. Filenames are `test_mb_`
prefixed so a bare name will not collide with yours.

```
{tests}
```

Each finds the core by searching upward for `massingbill/core/`, so it works
left in place or moved flat into your own test directory.

## What this is

The G702/G703 pay-application calculation with nothing attached: no ORM, no web
framework, no session. See `vendorable-core.md`, including the section on what
this replaces and what it does not.

Upstream: <https://github.com/MassingCloud/massingbill>
"""


if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1].strip():
        raise SystemExit("Output directory is empty. Pass a path, or omit it for ./vendor-kit.")
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "vendor-kit")
