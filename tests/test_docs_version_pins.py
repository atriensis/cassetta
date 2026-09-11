"""Guard: what rewrites version literals in the documents, and what it must never touch.

This file used to hold a third guard, and its name still says so. While no index carried this
package, the documents installed it from the repository pinned to a release tag, and a guard here
held every pin to the declared version: a stale pin resolves, installs working code, and reports
nothing. The package is on the index now, and the documents install it from there with no version at
all. A pin cannot go stale if there is none, so that guard retired, and ``tests/test_docs_install.py``
holds the rule that replaced it: no install command names a version.

Two things stay, because the shape they serve stays. ``cassetta capabilities`` prints
``Server version: X.Y.Z``, the documents show a sample of it, and ``scripts/sync-docs-version.py``
keeps that sample current. ``tests/test_docs_examples.py`` says when it is stale; the generator is the
half that fixes it, and it is exercised here.

**``CHANGELOG.md`` is outside the generator and must stay outside.** Its version literals are records
of what shipped — a statement about the past, not a reference to the present. Rewriting one would not
fix a stale sample, it would falsify the record. Today the changelog is excluded because it lives at
the repository root rather than under ``docs/``, which is geography rather than a decision; the first
test here turns it into a decision, so a later widening of the generator cannot quietly take it in.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

import cassetta

REPO_ROOT = Path(__file__).resolve().parents[1]

_CHANGELOG = REPO_ROOT / "CHANGELOG.md"
_GENERATOR = REPO_ROOT / "scripts" / "sync-docs-version.py"


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


def test_the_changelog_is_outside_what_the_generator_rewrites() -> None:
    """``CHANGELOG.md`` is not among the files ``scripts/sync-docs-version.py`` rewrites, and stays out.

    Not a formality. Every version literal in that file records a release that happened. A generator
    that could reach it would treat each one as stale the moment it matched, and the obvious repair
    turns a history into a file that says the same version eleven times.

    Held against the set the generator itself answers with, not against a copy of it kept here. The
    copy used to be the pin guard's scan, and that guard is gone. Only the generator rewrites
    anything, so its set is the one that has to leave the changelog out.
    """
    assert _CHANGELOG.is_file(), "CHANGELOG.md is missing — it is the record the generator must not touch"

    rewritten = {path.resolve() for path in _generator().documents()}
    # Non-vacuity: an empty set leaves out the changelog along with everything else.
    assert rewritten, "the generator reports no documents to rewrite, so the exclusion below proves nothing"

    assert _CHANGELOG.resolve() not in rewritten, (
        "CHANGELOG.md has come inside the generator's reach. Its version literals record what shipped; "
        "holding them to the current release would not correct a stale sample, it would rewrite the history"
    )


# --- the fix half -------------------------------------------------------------------------------
#
# ``tests/test_docs_examples.py`` says when the sample output is stale. ``scripts/sync-docs-version.py``
# is what makes it current, and until 0.28.3 it could not see that shape at all — the sample-output
# guard added in 0.28.2 had no fix half, and the next bump would have been repaired by hand.


def test_the_generator_rewrites_a_sample_output_version() -> None:
    """The substitution updates a ``Server version:`` sample and nothing around it.

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
        "    uv tool install cassetta\n"
        "\n"
        "Then ask the server what it supports:\n"
        "\n"
        '    $ cassetta capabilities --url http://localhost:16001 --api-key "$API_KEY"\n'
        "    Server version: 0.1.2\n"
        "    Schema version: 1\n"
    )

    rewritten = generator.rewrite(stale, "9.9.9")

    assert "Server version: 9.9.9" in rewritten, (
        "the `Server version:` sample was not rewritten. It is the shape `tests/test_docs_examples.py` "
        "holds to the declared version, and a guard whose fix half cannot see it is repaired by hand"
    )
    assert "0.1.2" not in rewritten, f"a stale version survived the rewrite:\n{rewritten}"

    # Everything that is not a version is left exactly as it was found, the install command included.
    # A generator that reformats what it touches makes its own diffs unreadable, which is how a wrong
    # one survives review.
    assert rewritten == stale.replace("0.1.2", "9.9.9")

    # Idempotence, at the level of the substitution: the property the whole script rests on.
    assert generator.rewrite(rewritten, "9.9.9") == rewritten, (
        "rewriting an already-current text changed it, so the script is not idempotent and cannot be "
        "run without deciding to"
    )


def test_the_generator_refuses_when_it_finds_no_sample_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``docs/`` tree with install instructions but no sample output is a failure, not a success.

    The reason is the one written in the script's own docstring: **nothing to compare is not
    agreement**. A documentation tree that had lost its sample output would otherwise satisfy this
    generator by having nothing to be wrong about, and the guard in ``tests/test_docs_examples.py``
    would go quiet for the same reason.

    It also mirrors the non-vacuity control that guard carries, so the detect half and the fix half
    refuse for the same reason rather than for two reasons that could drift apart.
    """
    generator = _generator()

    # Written at the declared version rather than a stale one, on purpose: this test is about what
    # the script does when the shape is absent, and a tree needing no rewrite keeps it to that
    # question. It also leaves the script with nothing to write, which matters when the tree it is
    # pointed at is a temporary directory outside the repository.
    current = cassetta.__version__

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "INSTALL.md").write_text("    uv tool install cassetta\n", encoding="utf-8")

    monkeypatch.setattr(generator, "DOCS", docs)

    assert generator.main() != 0, (
        "the generator reported success on a documentation tree carrying no `Server version:` sample "
        "at all. Finding nothing is not the same as finding everything current — it means either the "
        "sample has gone from the documents or this script has stopped recognising it"
    )

    # Non-vacuity: the same tree with a sample output present must succeed, or the assertion above
    # would pass on a script that refuses every temporary tree for some unrelated reason. The tree
    # still carries no install pin, and the script used to refuse such a tree for that alone.
    (docs / "CAPABILITIES.md").write_text(f"Server version: {current}\n", encoding="utf-8")
    assert generator.main() == 0, (
        "the generator refused a tree carrying a sample output and an unpinned install. Either it "
        "still asks for a pin, which no document carries any more, or the refusal above proves nothing"
    )
