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

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

import cassetta

REPO_ROOT = Path(__file__).resolve().parents[1]

_DOCS = REPO_ROOT / "docs"
_CHANGELOG = REPO_ROOT / "CHANGELOG.md"
_GENERATOR = REPO_ROOT / "scripts" / "sync-docs-version.py"

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


# --- the fix half -------------------------------------------------------------------------------
#
# The two guards above say what is stale. ``scripts/sync-docs-version.py`` is what makes it current,
# and until 0.28.3 it could only see one of the two shapes they check — so the sample-output guard
# added in 0.28.2 had no fix half at all, and the next bump would have been repaired by hand.
#
# The script is exercised here, beside the guards it serves, rather than in a file of its own: a fix
# half that drifts from its detect half is the defect both exist to prevent, and keeping them in one
# file is the cheapest way to notice.


def _generator() -> ModuleType:
    """``scripts/sync-docs-version.py``, loaded as a module.

    ``scripts/`` is not a package and the filename is not an identifier, so a plain import cannot
    reach it. That is not an oversight to work around — the script is meant to be *run*, and giving
    it an importable home would invite production code to depend on it.

    **Bytecode writing is suppressed for the load, and that is not a micro-optimisation.** Executing
    a module normally leaves ``scripts/__pycache__/sync-docs-version.cpython-3XX.pyc`` behind, and
    ``tests/test_public_surface.py`` scans this repository's files as text — it reads that ``.pyc``,
    fails to decode it as UTF-8, and errors. The artifact is gitignored, so the damage is invisible
    to review and appears only as an unrelated test breaking for whoever runs this one first.

    The flag is global, so it is restored in ``finally`` (Principle IX: a test that mutates global
    state puts it back).
    """
    spec = importlib.util.spec_from_file_location("sync_docs_version", _GENERATOR)
    assert spec is not None and spec.loader is not None, f"{_GENERATOR} could not be loaded as a module"
    module = importlib.util.module_from_spec(spec)

    previously = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previously

    return module


def test_the_generator_rewrites_a_sample_output_version() -> None:
    """The substitution updates an install pin and a ``Server version:`` sample in one pass.

    Asserted on a string rather than on ``docs/``. The rewrite is the only interesting thing the
    script does; reading files and deciding whether to write them are not, and a test that had to
    build a documentation tree to ask this question would be mostly scaffolding.

    Two properties, and the second is the one that keeps the script safe to run reflexively: a line
    already naming the target version comes back **byte-identical**. That is what makes the whole run
    idempotent, which is what lets a Makefile call it without anyone deciding first.
    """
    generator = _generator()

    stale = (
        "Install it:\n"
        "\n"
        "    uv tool install git+https://github.com/atriensis/cassetta.git@v0.1.2\n"
        "\n"
        "Then ask the server what it supports:\n"
        "\n"
        '    $ cassetta capabilities --url http://localhost:16001 --api-key "$API_KEY"\n'
        "    Server version: 0.1.2\n"
        "    Schema version: 1\n"
    )

    rewritten = generator.rewrite(stale, "9.9.9")

    assert "cassetta.git@v9.9.9" in rewritten, "the install pin was not rewritten"
    assert "Server version: 9.9.9" in rewritten, (
        "the `Server version:` sample was not rewritten. It is the shape `tests/test_docs_examples.py` "
        "holds to the declared version, and a guard whose fix half cannot see it is repaired by hand"
    )
    assert "0.1.2" not in rewritten, f"a stale version survived the rewrite:\n{rewritten}"

    # Everything that is not a version is left exactly as it was found. A generator that reformats
    # what it touches makes its own diffs unreadable, which is how a wrong one survives review.
    assert rewritten == stale.replace("0.1.2", "9.9.9")

    # Idempotence, at the level of the substitution: the property the whole script rests on.
    assert generator.rewrite(rewritten, "9.9.9") == rewritten, (
        "rewriting an already-current text changed it, so the script is not idempotent and cannot be "
        "run without deciding to"
    )


def test_the_generator_refuses_when_it_finds_no_sample_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``docs/`` tree with install pins but no sample output is a failure, not a success.

    This mirrors the refusal the script already has for finding no pins, and the reason is the one
    written in its own docstring: **nothing to compare is not agreement**. A documentation tree that
    had lost its sample output would otherwise satisfy this generator by having nothing to be wrong
    about, and the guard in ``tests/test_docs_examples.py`` would go quiet for the same reason.

    It also mirrors the non-vacuity control that guard carries, so the detect half and the fix half
    refuse for the same reason rather than for two reasons that could drift apart.
    """
    generator = _generator()

    # Written at the declared version rather than a stale one, on purpose: this test is about what
    # the script does when a *shape* is absent, and a tree needing no rewrite keeps it to that
    # question. It also leaves the script with nothing to write, which matters when the tree it is
    # pointed at is a temporary directory outside the repository.
    current = cassetta.__version__

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "INSTALL.md").write_text(
        f"    uv tool install git+https://github.com/atriensis/cassetta.git@v{current}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(generator, "DOCS", docs)

    assert generator.main() != 0, (
        "the generator reported success on a documentation tree carrying no `Server version:` sample "
        "at all. Finding nothing is not the same as finding everything current — it means either the "
        "sample has gone from the documents or this script has stopped recognising it"
    )

    # Non-vacuity: the same tree with a sample output present must succeed, or the assertion above
    # would pass on a script that refuses every temporary tree for some unrelated reason.
    (docs / "CAPABILITIES.md").write_text(f"Server version: {current}\n", encoding="utf-8")
    assert generator.main() == 0, (
        "the generator refused a tree carrying both shapes, so the refusal above proves nothing"
    )
