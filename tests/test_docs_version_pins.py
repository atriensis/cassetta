"""Guard: every documented install command pins the release this repository is on.

No index carries this package, so the documents install it from the repository, pinned to a release
tag — and they say why, at length: a client that follows the default branch changes under you
between runs. The advice is right, and it is exactly what makes the pin dangerous to leave alone.

**A stale pin does not fail.** It resolves, it installs, and the code it installs works. The reader
following the instruction gets a release that is not the one the documents around it describe, and
nothing anywhere says so. That is the whole reason this is a test rather than a convention: the
defect has no symptom, so it can only be caught by looking.

It has already happened. Six pins named ``v0.26.4`` across three documents while the repository was
on ``0.26.5``; the release went out and the documented command went a version stale in the same
merge, silently.

``scripts/sync-docs-version.py`` is the other half. This guard says what is wrong; that script fixes
it, and is idempotent so that running it is never a decision.

**``CHANGELOG.md`` is outside this and must stay outside.** Its version literals are records of what
shipped — a statement about the past, not a reference to the present. Rewriting one would not fix a
stale pin, it would falsify the record. Today the changelog is excluded because it lives at the
repository root rather than under ``docs/``, which is geography rather than a decision; the second
test here turns it into a decision, so a later widening of the scan cannot quietly take it in.
"""

from __future__ import annotations

import re
from pathlib import Path

import cassetta

REPO_ROOT = Path(__file__).resolve().parents[1]

_DOCS = REPO_ROOT / "docs"
_CHANGELOG = REPO_ROOT / "CHANGELOG.md"

# A repository install reference, in the one shape the documents use:
#
#   uv tool install git+https://github.com/atriensis/cassetta.git@v0.26.6
#   uvx --from git+https://github.com/atriensis/cassetta.git@v0.26.6 cassetta --help
#
# Anchored on `git+https://` at one end and `cassetta.git@v` at the other, so it matches a *pinned
# install reference* and not any other place a version-shaped string might appear. Prose that
# mentions a version is not a pin, and rewriting it would be an edit nobody asked for.
_PIN_RE = re.compile(r"git\+https://\S*?cassetta\.git@v(?P<version>\d+\.\d+\.\d+)")

# What exists today. Non-vacuity: a scan that finds nothing is green, and would stay green if the
# pattern stopped matching, if `docs/` moved, or if the install instructions were deleted outright.
_KNOWN_PIN_COUNT = 6


def _scanned_files() -> list[Path]:
    """Every file under ``docs/``, in a stable order.

    Every file rather than every ``*.md``: a pin in a shell snippet or an included fragment installs
    the same wrong version as a pin in prose, and the reader cannot tell which kind of file they
    copied it from.
    """
    return sorted(path for path in _DOCS.rglob("*") if path.is_file())


def _pins() -> list[tuple[Path, int, str]]:
    """Every documented install pin: its file, its line number, and the version it names."""
    found = []
    for path in _scanned_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for match in _PIN_RE.finditer(line):
                found.append((path, lineno, match.group("version")))
    return found


def test_every_documented_install_pins_the_declared_version() -> None:
    """Every ``@vX.Y.Z`` in a repository install reference under ``docs/`` is the current release."""
    assert _DOCS.is_dir(), "docs/ is missing — it is where a reader is sent to install this"

    pins = _pins()
    assert len(pins) >= _KNOWN_PIN_COUNT, (
        f"found {len(pins)} documented install pin(s), expected at least {_KNOWN_PIN_COUNT}. Either "
        "the install instructions have been removed from the documentation, or this guard has "
        "stopped recognising them — and a guard that recognises nothing passes over anything"
    )

    declared = cassetta.__version__

    stale = [
        f"{path.relative_to(REPO_ROOT)}:{lineno}: pins v{version}, the release is {declared}"
        for path, lineno, version in pins
        if version != declared
    ]

    assert not stale, (
        "documented install commands pin a release this repository is no longer on. A stale pin "
        "installs working code, so nobody finds out; run `uv run python scripts/sync-docs-version.py`:\n  "
        + "\n  ".join(stale)
    )


def test_the_changelog_is_not_scanned() -> None:
    """``CHANGELOG.md`` is outside the scanned set, and stays outside.

    Not a formality. Every version literal in that file is a heading recording a release that
    happened, and the guard above would call each of them stale the moment it could see them — after
    which the obvious fix is to make them all say the current version, which turns a history into a
    file that says the same thing eleven times.

    Written as a test rather than a comment because the exclusion is currently an accident of where
    the file lives, and an accident is not a decision anyone can be held to.
    """
    assert _CHANGELOG.is_file(), "CHANGELOG.md is missing — it is the record this guard must not touch"

    scanned = {path.resolve() for path in _scanned_files()}
    assert _CHANGELOG.resolve() not in scanned, (
        "CHANGELOG.md has come inside the scan. Its version literals record what shipped; holding "
        "them to the current release would not correct a stale pin, it would rewrite the history"
    )
