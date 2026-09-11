#!/usr/bin/env python3
"""Point the version literal in the documents' sample output at the release this repository is on.

`cassetta capabilities` prints `Server version: X.Y.Z`, and `docs/` shows a sample of what it prints.
That literal is a claim about the running server, it is nowhere near an install command, and until
0.28.3 nothing rewrote it — it sat two releases behind while three separate checks reported the
documents current, because each of them was looking at pins.

**There used to be a second shape, and there is no longer.** While no index carried this package, the
documents installed it from the repository with `uv`, pinned to a release tag, and this script kept
the pin current. The package is on the index now, and the documents install it from there with no
version at all: the index carries only releases, so an unpinned install takes the newest, which is
the release the documents describe. With no version in any install command there is nothing for a
pin half to rewrite, so it is gone. What replaces it is a guard rather than a generator:
`tests/test_docs_install.py` holds that no install command names a version at all, and a rule that
forbids the literal leaves nothing to keep current.

This is the fix half of the sample output. `tests/test_docs_examples.py` is the detect half, and it
cannot repair what it finds. A guard whose fix half cannot see its shape does not remove the manual
step; it reschedules it to the moment the guard goes red.

Deliberately boring, in three ways:

* **It rewrites the version and nothing else.** Not the sample's shape, not the surrounding prose,
  not the line ending. A generator that reformats what it touches makes its own diffs unreadable,
  which is how a wrong one survives review.
* **It is idempotent.** Run against documents that already agree, it changes nothing and says so.
  That is what lets it be run reflexively rather than decided about, and it is what the release check
  leans on.
* **It writes a file only when the content actually differs**, so a no-op run leaves mtimes alone and
  `git diff` empty.

Finding the sample output nowhere is a **failure**, not a success. A `docs/` tree that had lost it
would satisfy every check by having nothing to be wrong about, and this script would report success
having done nothing — which is precisely the silence it exists to break.

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

# A sample of what `cassetta capabilities` prints (cli/capabilities.py), split so the version can be
# replaced without touching anything around it.
#
# The version part is `\d+\.\d+\.\d+` rather than the `\S+` the detect half in
# tests/test_docs_examples.py matches, and the asymmetry is deliberate: this half only ever replaces
# something that is already a version. A sample printing `Server version: ?` — what the CLI shows
# when the server answers without the field — is a broken *sample*, not a stale version, and a
# generator that quietly turned it into a release number would be inventing a claim rather than
# refreshing one. The detect half being the wider of the two is the right way round: it can still
# report such a line, and a person decides what it should say.
SAMPLE_OUTPUT_RE = re.compile(r"(Server version:\s*)(\d+\.\d+\.\d+)")


def declared_version() -> str:
    """The version `src/cassetta/__init__.py` declares — the only place one is declared."""
    match = DUNDER_VERSION_RE.search(PACKAGE_INIT.read_text(encoding="utf-8"))
    if match is None:
        raise SystemExit(f"sync-docs-version: {PACKAGE_INIT} declares no `__version__` to sync to")
    return match.group(1)


def documents() -> list[Path]:
    """Every file under `docs/`, in a stable order.

    Every file rather than every `*.md`: a sample in an included fragment is read as the same claim as
    a sample in prose.
    """
    if not DOCS.is_dir():
        raise SystemExit(f"sync-docs-version: {DOCS} is missing — there is nothing to sync")
    return sorted(path for path in DOCS.rglob("*") if path.is_file())


def rewrite(text: str, version: str) -> str:
    """Every sample-output version in `text`, set to `version`.

    Text in, text out, and nothing else — no files, no reporting, no decisions. Kept apart from the
    file handling so there is one place where "what counts as a version literal" is answered.

    Idempotent by construction: a literal already at `version` is replaced by itself.
    """
    return SAMPLE_OUTPUT_RE.sub(rf"\g<1>{version}", text)


def sync(version: str) -> int:
    """Rewrite every documented sample-output version to `version`, and return how many were found."""
    found = 0
    changed: list[str] = []

    for path in documents():
        original = path.read_text(encoding="utf-8")
        samples = SAMPLE_OUTPUT_RE.findall(original)
        if not samples:
            continue
        found += len(samples)

        updated = rewrite(original, version)
        if updated == original:
            continue

        # Only on a real difference, so an already-current run leaves the tree untouched.
        path.write_text(updated, encoding="utf-8")
        stale = sorted({old for _, old in samples if old != version})
        changed.append(f"  {path.relative_to(REPO_ROOT)}: {len(samples)} sample(s), was {', '.join(stale)}")

    if changed:
        print(f"sync-docs-version: {version} — updated:")
        print("\n".join(changed))
    else:
        print(f"sync-docs-version: {version} — all {found} documented sample output(s) were already current")

    return found


def main() -> int:
    version = declared_version()
    if sync(version) == 0:
        print(
            "sync-docs-version: no sample output (`Server version: X.Y.Z`) found anywhere under docs/. "
            "It is what `cassetta capabilities` prints, and the documented sample of it is the one "
            "place a version literal hides from every other check. A generator that matches nothing "
            "reports success for doing nothing.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
