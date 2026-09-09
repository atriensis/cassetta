#!/usr/bin/env python3
"""Point every version literal in the documents at the release this repository is on.

Two shapes carry one, and both go stale the same silent way.

**The install pin.** No index carries this package, so the documents install it from the repository
with `uv`, pinned to a release tag. The pin has to be updated with every bump, and the reason it gets
forgotten is that forgetting it costs nothing visible: a tag one release behind still resolves, still
installs working code, and reports nothing. The reader ends up on a release the surrounding documents
do not describe.

**The sample output.** `cassetta capabilities` prints `Server version: X.Y.Z`, and `docs/` shows a
sample of what it prints. That literal is a claim about the running server, it is nowhere near an
install command, and until 0.28.3 nothing rewrote it — it sat two releases behind while three
separate checks reported the documents current, because each of them was looking at pins.

This is the fix half of both. `tests/test_docs_version_pins.py` is the detect half of the first,
`tests/test_docs_examples.py` of the second, and neither can repair what it finds. A guard whose fix
half cannot see its shape does not remove the manual step; it reschedules it to the moment the guard
goes red.

Deliberately boring, in three ways:

* **It rewrites the version and nothing else.** Not the reference's shape, not the surrounding prose,
  not the line ending. A generator that reformats what it touches makes its own diffs unreadable,
  which is how a wrong one survives review.
* **It is idempotent.** Run against documents that already agree, it changes nothing and says so.
  That is what lets it be run reflexively rather than decided about, and it is what the release check
  leans on.
* **It writes a file only when the content actually differs**, so a no-op run leaves mtimes alone and
  `git diff` empty.

Finding **either** shape nowhere is a **failure**, not a success, and the two are counted separately
so that six healthy pins cannot mask a sample output that has vanished. A `docs/` tree that had
stopped documenting installation would satisfy every check by having nothing to be wrong about, and
this script would report success having done nothing — which is precisely the silence it exists to
break.

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

# A sample of what `cassetta capabilities` prints (cli/capabilities.py). Split the same way as
# PIN_RE, for the same reason, so one substitution serves both.
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

    Every file rather than every `*.md`: a pin in a shell snippet installs the same wrong version as
    a pin in prose.
    """
    if not DOCS.is_dir():
        raise SystemExit(f"sync-docs-version: {DOCS} is missing — there is nothing to sync")
    return sorted(path for path in DOCS.rglob("*") if path.is_file())


def rewrite(text: str, version: str) -> str:
    """Every version literal this script knows about in `text`, set to `version`.

    Text in, text out, and nothing else — no files, no reporting, no decisions. Both shapes are
    substituted here so there is one place where "what counts as a version literal" is answered; the
    pin and the sample output drifted apart precisely because that answer lived in two.

    Idempotent by construction: a literal already at `version` is replaced by itself.
    """
    text = PIN_RE.sub(rf"\g<1>{version}", text)
    return SAMPLE_OUTPUT_RE.sub(rf"\g<1>{version}", text)


def sync(version: str) -> tuple[int, int]:
    """Rewrite every documented version literal to `version`.

    Returns `(pins, samples)` — how many of each shape were found. Counted separately rather than
    totalled: the two can go missing independently, and a total lets six healthy pins report success
    for a documentation tree whose sample output has vanished.
    """
    pins_found = 0
    samples_found = 0
    changed: list[str] = []

    for path in documents():
        original = path.read_text(encoding="utf-8")
        pins = PIN_RE.findall(original)
        samples = SAMPLE_OUTPUT_RE.findall(original)
        if not pins and not samples:
            continue
        pins_found += len(pins)
        samples_found += len(samples)

        updated = rewrite(original, version)
        if updated == original:
            continue

        # Only on a real difference, so an already-current run leaves the tree untouched.
        path.write_text(updated, encoding="utf-8")
        stale = sorted({old for _, old in pins + samples if old != version})
        found_here = f"{len(pins)} pin(s), {len(samples)} sample(s)"
        changed.append(f"  {path.relative_to(REPO_ROOT)}: {found_here}, was {', '.join(stale)}")

    if changed:
        print(f"sync-docs-version: {version} — updated:")
        print("\n".join(changed))
    else:
        print(
            f"sync-docs-version: {version} — all {pins_found} documented pin(s) and "
            f"{samples_found} sample output(s) were already current"
        )

    return pins_found, samples_found


def main() -> int:
    version = declared_version()
    pins_found, samples_found = sync(version)

    # Each shape refuses on its own. Reported together so a tree missing both is one run rather than
    # two, and named individually so the reader knows which half to go looking for.
    missing = []
    if pins_found == 0:
        missing.append(
            "  no documented install pin (`…cassetta.git@vX.Y.Z`) found anywhere under docs/. "
            "Either the install instructions have gone, or this script no longer recognises them."
        )
    if samples_found == 0:
        missing.append(
            "  no sample output (`Server version: X.Y.Z`) found anywhere under docs/. It is what "
            "`cassetta capabilities` prints, and the documented sample of it is the one place a "
            "version literal hides from every other check."
        )

    if missing:
        print(
            "sync-docs-version: a shape this script maintains is not present, and a generator that "
            "matches nothing reports success for doing nothing:\n" + "\n".join(missing),
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
