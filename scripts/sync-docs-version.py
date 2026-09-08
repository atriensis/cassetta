#!/usr/bin/env python3
"""Point every documented install command at the release this repository is on.

No index carries this package, so the documents install it from the repository with `uv`, pinned to
a release tag. The pin has to be updated with every bump, and the reason it gets forgotten is that
forgetting it costs nothing visible: a tag one release behind still resolves, still installs working
code, and reports nothing. The reader ends up on a release the surrounding documents do not describe.

This is the fix half. `tests/test_docs_version_pins.py` is the detect half, and it is the one that
turns a forgotten bump into a red check.

Deliberately boring, in three ways:

* **It rewrites the version and nothing else.** Not the reference's shape, not the surrounding prose,
  not the line ending. A generator that reformats what it touches makes its own diffs unreadable,
  which is how a wrong one survives review.
* **It is idempotent.** Run against documents that already agree, it changes nothing and says so.
  That is what lets it be run reflexively rather than decided about, and it is what the release check
  leans on.
* **It writes a file only when the content actually differs**, so a no-op run leaves mtimes alone and
  `git diff` empty.

Finding no pins at all is a **failure**, not a success. A `docs/` tree that had stopped documenting
installation would satisfy every check by having nothing to be wrong about, and this script would
report success having done nothing — which is precisely the silence it exists to break.

Run it as:

    uv run python scripts/sync-docs-version.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DOCS = REPO_ROOT / "docs"
PACKAGE_INIT = REPO_ROOT / "src" / "cassetta" / "__init__.py"

# The version is read out of the module as *text*, not imported. This script describes a tree, and a
# tree can be checked out without the package being installed anywhere. It is also how the build
# backend and `make release-check` read it, so all three agree by construction on what "the declared
# version" means.
DUNDER_VERSION_RE = re.compile(r'^__version__ = "([^"]+)"', re.MULTILINE)

# A pinned repository install reference, split so the version can be replaced without touching
# anything around it. Kept in step with the pattern in tests/test_docs_version_pins.py by that
# test's non-vacuity check: if the two drift, it stops finding the pins this leaves stale.
PIN_RE = re.compile(r"(git\+https://\S*?cassetta\.git@v)(\d+\.\d+\.\d+)")


def declared_version() -> str:
    """The version `src/cassetta/__init__.py` declares — the only place one is declared."""
    match = DUNDER_VERSION_RE.search(PACKAGE_INIT.read_text(encoding="utf-8"))
    if match is None:
        raise SystemExit(f"sync-docs-version: {PACKAGE_INIT} declares no `__version__` to sync to")
    return match.group(1)


def documents() -> list[Path]:
    """Every file under `docs/`, in a stable order.

    Every file rather than every `*.md`: a pin in a shell snippet installs the same wrong version as
    a pin in prose.
    """
    if not DOCS.is_dir():
        raise SystemExit(f"sync-docs-version: {DOCS} is missing — there is nothing to sync")
    return sorted(path for path in DOCS.rglob("*") if path.is_file())


def sync(version: str) -> int:
    """Rewrite every documented pin to `version`. Returns the number of pins found."""
    found = 0
    changed: list[str] = []

    for path in documents():
        original = path.read_text(encoding="utf-8")
        pins = PIN_RE.findall(original)
        if not pins:
            continue
        found += len(pins)

        updated = PIN_RE.sub(rf"\g<1>{version}", original)
        if updated == original:
            continue

        # Only on a real difference, so an already-current run leaves the tree untouched.
        path.write_text(updated, encoding="utf-8")
        stale = sorted({old for _, old in pins if old != version})
        changed.append(f"  {path.relative_to(REPO_ROOT)}: {len(pins)} pin(s), was {', '.join(stale)}")

    if changed:
        print(f"sync-docs-version: {version} — updated:")
        print("\n".join(changed))
    else:
        print(f"sync-docs-version: {version} — all {found} documented pin(s) were already current")

    return found


def main() -> int:
    version = declared_version()
    found = sync(version)

    if found == 0:
        print(
            "sync-docs-version: no documented install pin found anywhere under docs/. Either the "
            "install instructions have gone, or this script no longer recognises them — and a "
            "generator that matches nothing reports success for doing nothing.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
