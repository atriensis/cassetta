"""Guard: what this repository says about itself, said once and said consistently.

A release is cut from a tree, not from a command. This tree used to say its version in two places
and record what shipped in a third; either version site could be forgotten in a bump, and the
resulting defect was only visible after the tag was public — which is the point at which it is
expensive.

There is now **one** version site, ``src/cassetta/__init__.py``, and ``pyproject.toml`` derives its
metadata from it at build time. That changes what is worth guarding: not "do the two agree" — there
is no second one to disagree — but "is there still only one".

Sibling in spirit to ``test_public_surface.py``: the same idea that a rule worth keeping is a rule
a test holds, rather than one a checklist asks about.

Five things are held here.

* **The changelog's shape.** Sections are unique and newest-first. A duplicated version makes "the
  top section" and "the section for this version" two different things, which is exactly the
  ambiguity the release check downstream must not have to resolve.
* **The changelog's top section names the version the package declares.** A release whose changelog
  has not caught up ships a document that describes the previous one.
* **The version is declared in exactly one place.** ``pyproject.toml`` states no literal and points
  the build backend at the package module. A second site re-added "so it is visible" reintroduces
  the whole defect while every other guard here stays green.
* **``release-check`` reads that one place.** Its recipe is shell, so it cannot import anything and
  cannot be caught by the rest of this file; what it *reads* is asserted by shape instead.
* **Every declared project link names this repository**, and none names a package index or the
  personal account this project used to live under. The package is not published, so an index link
  would promise a page that does not exist; the old account is where this project *was*.

The version comparison is on integer triples throughout, never on strings. ``"0.9.0" > "0.26.2"``
lexicographically, and a guard that sorted that way would call a correct changelog broken the first
time this project reached a two-digit minor — which it already has.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_CHANGELOG = REPO_ROOT / "CHANGELOG.md"
_PYPROJECT = REPO_ROOT / "pyproject.toml"
_PACKAGE_INIT = REPO_ROOT / "src" / "cassetta" / "__init__.py"
_MAKEFILE = REPO_ROOT / "Makefile"

# Where the build backend must be told to look for the version. Written as a string because that is
# what ``pyproject.toml`` carries and what the assertion compares — building it from ``_PACKAGE_INIT``
# would compare the file against a path derived from itself and pass for either answer.
_VERSION_SOURCE_PATH = "src/cassetta/__init__.py"

# A released version's section header, in the Keep a Changelog shape this file uses:
#
#   ## [0.26.2] - 2026-09-05
#
# Anchored at the start of a line so a version mentioned inside an entry's prose is not mistaken for
# a section of its own.
_SECTION_RE = re.compile(r"^## \[(\d+)\.(\d+)\.(\d+)\]", re.MULTILINE)

# ``__version__ = "0.26.2"`` in the package's ``__init__``. Read as text rather than imported: this
# guard is about what the *file* declares, and reading it needs no installed package — the same
# property that lets ``make release-check`` run in a clean clone.
_DUNDER_VERSION_RE = re.compile(r'^__version__ = "([^"]+)"', re.MULTILINE)

# Where this project lives. Every declared project link must be under it.
_REPOSITORY_URL = "https://github.com/atriensis/cassetta"

# Written without a scheme on purpose: a bare host is not matched by the URL extractor in
# ``test_public_surface.py``, so naming these here as things to *reject* does not register them as
# links this repository publishes.
_FORBIDDEN_LINK_SUBSTRINGS = (
    "pypi.org",
    "pythonhosted.org",
    "github.com/ximera239",
)


def _changelog_versions() -> list[tuple[int, int, int]]:
    """Every released version the changelog carries a section for, in file order."""
    text = _CHANGELOG.read_text(encoding="utf-8")
    return [(int(major), int(minor), int(patch)) for major, minor, patch in _SECTION_RE.findall(text)]


def _as_text(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def _pyproject() -> dict[str, object]:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))


def _package_version() -> str:
    """The version ``src/cassetta/__init__.py`` declares — the only place one is declared.

    Read as text rather than imported, deliberately: every other reader of this literal that is not
    a Python process reads it the same way. The build backend does, ``make release-check`` does, and
    the workflow that tags a merge does. A guard that imported it would be the one reader whose
    answer could differ from all of theirs.
    """
    match = _DUNDER_VERSION_RE.search(_PACKAGE_INIT.read_text(encoding="utf-8"))
    assert match is not None, f"{_PACKAGE_INIT.name} declares no `__version__` — the release check reads it too"
    return match.group(1)


def _project_urls() -> dict[str, str]:
    """The ``[project.urls]`` table, as declared."""
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    urls = project.get("urls")
    assert isinstance(urls, dict), (
        "pyproject.toml declares no [project.urls] table, so the built package says nothing about "
        "where this project lives — a reader holding the wheel has no route back to it"
    )
    return {str(name): str(url) for name, url in urls.items()}


def test_changelog_sections_are_unique_and_ordered() -> None:
    """Changelog sections are unique and in descending version order, newest first.

    Uniqueness first, because a duplicate breaks the two guards that follow rather than this one:
    with two ``[0.26.2]`` sections, "the section for the current version" stops being a single thing
    and ``make release-check`` can be satisfied by whichever one it happens to find.

    Order second, and compared as integer triples. A string comparison puts ``0.9.0`` above
    ``0.26.2``, so a guard written that way fails on a correct file the moment a project reaches a
    two-digit minor version.
    """
    assert _CHANGELOG.is_file(), "CHANGELOG.md is missing — it is what a reader consults before upgrading"

    versions = _changelog_versions()

    # Non-vacuity: an empty list is both unique and sorted.
    assert len(versions) >= 2, (
        f"the changelog carries {len(versions)} version section(s); with fewer than two there is "
        "nothing for this guard to compare, which means it is reading the wrong shape"
    )

    duplicates = sorted({_as_text(v) for v in versions if versions.count(v) > 1})
    assert not duplicates, (
        "the changelog carries more than one section for the same version. Two records of one "
        "release make 'the entry for this version' ambiguous:\n  " + "\n  ".join(duplicates)
    )

    out_of_order = [
        f"{_as_text(earlier)} is listed above {_as_text(later)}"
        for earlier, later in zip(versions, versions[1:], strict=False)
        if earlier < later
    ]
    assert not out_of_order, (
        "the changelog is not in descending version order. A reader opens this file to find out "
        "what the newest release changed, and reads from the top:\n  " + "\n  ".join(out_of_order)
    )


def test_changelog_top_section_is_the_declared_version() -> None:
    """The changelog's newest section names the version this package declares.

    This is the one that catches the ordinary mistake: the version is bumped and the changelog is
    not, so the published document describes the release before the one being shipped.
    """
    assert _CHANGELOG.is_file(), "CHANGELOG.md is missing — it is what a reader consults before upgrading"

    versions = _changelog_versions()
    assert versions, "the changelog carries no version sections at all"

    top = _as_text(versions[0])
    declared = _package_version()

    assert top == declared, (
        f"{_PACKAGE_INIT.name} declares version {declared}, but the changelog's newest section is "
        f"{top}. Either the bump has not been recorded, or the record is for a release that is not "
        "this one."
    )


def test_the_version_is_declared_in_exactly_one_place() -> None:
    """``pyproject.toml`` states no version of its own and derives one from the package module.

    This replaces a guard that asserted the two version sites agreed. That guard's subject no longer
    exists — there is one site — and a test whose subject has gone is not passing, it is empty.

    What is worth holding now is the property that made it redundant. The obvious "improvement" is
    to put the literal back into ``pyproject.toml`` so a reader can see it in the file they expect
    it in; that single edit reinstates the original defect, and every other guard in this file stays
    green over it, because each version site would still be internally consistent.

    Three things, because three separate edits could each break it: the literal is gone, the
    metadata is declared dynamic, and the backend is pointed at the file that has the literal.
    """
    project = _pyproject()["project"]
    assert isinstance(project, dict)

    assert "version" not in project, (
        "pyproject.toml states a version literal again. It is derived from "
        f"{_VERSION_SOURCE_PATH} at build time; a second site is the defect this arrangement "
        "removed, not a convenience"
    )

    assert project.get("dynamic") == ["version"], (
        'pyproject.toml must declare `dynamic = ["version"]` — without it the build backend is '
        f"never asked for a version and the metadata ships with none. Found: {project.get('dynamic')!r}"
    )

    tool = _pyproject().get("tool")
    assert isinstance(tool, dict), "pyproject.toml declares no [tool] table, so the version source is unconfigured"
    hatch = tool.get("hatch")
    assert isinstance(hatch, dict), (
        "pyproject.toml declares no [tool.hatch] table, so the version source is unconfigured"
    )
    version_source = hatch.get("version")
    assert isinstance(version_source, dict), (
        'pyproject.toml declares `dynamic = ["version"]` but no [tool.hatch.version] block, so '
        "nothing tells the backend where to read it"
    )

    assert version_source.get("path") == _VERSION_SOURCE_PATH, (
        f"the build backend must read the version from {_VERSION_SOURCE_PATH}, which is where the "
        f"package declares it. Found: {version_source.get('path')!r}"
    )

    # Non-vacuity: the path above is only a single source of truth if the file it names has one.
    assert _DUNDER_VERSION_RE.search(_PACKAGE_INIT.read_text(encoding="utf-8")) is not None, (
        f"{_VERSION_SOURCE_PATH} is configured as the version source but declares no `__version__`"
    )


def test_release_check_reads_the_package_module() -> None:
    """``make release-check`` reads the one version site, and compares the documented pins.

    Asserted by shape rather than by running it, and the reason is the recipe's whole purpose: it is
    shell so that it works in a clean clone where no virtual environment exists, which is the state
    a release is cut from. Nothing about that state can be reproduced from inside a test session that
    is, by definition, running in the environment the recipe must not need.

    So this holds the three things a wrong recipe would get wrong, and the operator runs it for real:
    it reads the package module, it no longer extracts a version out of ``pyproject.toml`` (the site
    that no longer has one — a recipe still reading it would find nothing and would have to either
    fail always or, worse, silently treat "no version" as "nothing to compare"), and it checks the
    documented install pins, which is the surface that went stale unnoticed and prompted all this.
    """
    assert _MAKEFILE.is_file(), "Makefile is missing — it is what a release is checked with"
    recipe = _MAKEFILE.read_text(encoding="utf-8")

    assert _VERSION_SOURCE_PATH in recipe, (
        f"the release check does not name {_VERSION_SOURCE_PATH}, which is the only place this "
        "repository declares its version"
    )

    # Two separate ways of saying "it does not read pyproject.toml for a version", because the
    # recipe could keep either half without the other and both halves are wrong.
    #
    # The extraction pattern first: `^version = ` is what the old recipe matched, and it is what a
    # reflex edit would restore. It is not a substring of the `^__version__ = ` the recipe uses now.
    assert "^version = " not in recipe, (
        "the release check still extracts a version with the `^version = ` pattern, which matched "
        "the line pyproject.toml no longer carries. It would find nothing, and a check that finds "
        "nothing either fails always or treats absence as agreement"
    )

    # And the file itself, because a recipe that still reads pyproject.toml is reading a file that
    # has nothing to say about the version. There is no other reason for this target to open it.
    assert "pyproject" not in recipe.lower(), (
        "the release check still names pyproject.toml. It no longer declares a version — the build "
        f"backend derives one from {_VERSION_SOURCE_PATH} — so there is nothing for this check to "
        "read there"
    )

    assert "docs" in recipe, (
        "the release check does not look at docs/. The documented install pins are the surface that "
        "went a release stale without anyone noticing, which is the defect this check exists to catch"
    )


def _release_check_success_line() -> str:
    """The line ``release-check`` prints when everything it compared agrees.

    Singled out because it is the one operator-visible string this target produces on the happy path,
    and the only thing most people will ever read of it.
    """
    recipe = _MAKEFILE.read_text(encoding="utf-8")
    lines = [
        line for line in recipe.splitlines() if "release-check: $$version" in line and line.lstrip().startswith("echo")
    ]
    assert len(lines) == 1, (
        f"expected exactly one success line in the release-check recipe, found {len(lines)}. This "
        "guard reads that line to check what it claims, and cannot do so if there are two"
    )
    return lines[0]


def test_release_check_compares_the_sample_output_version() -> None:
    """``make release-check`` compares the documented sample output, and claims only what it compared.

    The second half is the point, and it is a lesson this repository paid for. Until 0.28.3 the
    recipe closed with

        the declared version, CHANGELOG.md and the docs/ pins all agree

    which is **true about pins and read as being about the documents**. A ``Server version:`` literal
    in ``docs/CLIENT_SETUP.md`` sat two releases behind while this check, and two tests, reported
    agreement — none of them was wrong, and all three were narrower than they sounded.

    So two things are held here: that the sample output is compared at all, and that the sentence
    announcing success names what was actually looked at. A check whose success message overstates
    its own coverage is worse than one that does not run, because it answers the question that would
    otherwise have been asked.

    Asserted by shape rather than by running it, for the reason
    :func:`test_release_check_reads_the_package_module` gives at length: the recipe is shell so that
    it works in a clean clone with no virtual environment, which is the state a release is cut from
    and is not a state reproducible from inside this test session.
    """
    assert _MAKEFILE.is_file(), "Makefile is missing — it is what a release is checked with"
    recipe = _MAKEFILE.read_text(encoding="utf-8")

    assert "Server version" in recipe, (
        "the release check does not look at the `Server version:` sample output. It is a version "
        "literal in docs/ that no install-pin check can see — which is exactly how it went stale"
    )

    # A comparison that cannot fail on absence is not a comparison. The recipe already has this
    # branch for pins, and the reason is written into it: nothing to compare is not agreement.
    assert "no documented sample output" in recipe, (
        "the release check has no branch for finding *no* sample output under docs/. Without it a "
        "documentation tree that had lost the sample satisfies this check by having nothing to be "
        "wrong about — the same silence the pin branch above already refuses"
    )

    # The pin comparison is not replaced by the new one; both shapes go stale independently.
    assert "cassetta\\.git@v" in recipe, (
        "the release check no longer extracts the documented install pins. The sample output is a "
        "second shape to compare, not a substitute for the first"
    )

    success = _release_check_success_line()
    assert "sample output" in success, (
        "the release check's success line does not mention the sample output, so it goes on claiming "
        f"agreement about a narrower set than it now checks. Found:\n  {success.strip()}"
    )
    assert "pins" in success, (
        f"the release check's success line no longer mentions the install pins it compares. Found:\n  {success.strip()}"
    )


def test_project_urls_all_name_this_repository() -> None:
    """Every declared project link points into this repository.

    The check is on the repository path rather than on the host, so a link to some other project on
    the same forge fails here. That is the intent: this table is what a reader holding the built
    package follows to find the source, and every entry in it is a claim about where that source is.
    """
    urls = _project_urls()

    # Non-vacuity: an empty table satisfies "every entry names this repository" trivially.
    assert urls, "[project.urls] is empty — declare where this project lives, or remove the table"

    strays = sorted(f"{name} = {url}" for name, url in urls.items() if not url.startswith(_REPOSITORY_URL))

    assert not strays, (
        f"declared project links do not point into this repository ({_REPOSITORY_URL}):\n  " + "\n  ".join(strays)
    )


def test_project_urls_name_no_index_and_no_previous_account() -> None:
    """No declared project link names a package index or the account this project has moved off.

    The mirror of the guard above, and it fails for different reasons, which is why it is a separate
    test. An index link promises a page that does not exist — this package is not published, and a
    link that 404s is worse than an absent one, because it reads as a distribution channel. A link
    under the previous personal account points at where this project used to be.

    ``test_public_surface.py`` rejects that account across shipped prose; this rejects it in the
    packaging metadata, which is not prose and which travels inside the built wheel.
    """
    urls = _project_urls()
    assert urls, "[project.urls] is empty — declare where this project lives, or remove the table"

    offenders = sorted(
        f"{name} = {url} (names {forbidden})"
        for name, url in urls.items()
        for forbidden in _FORBIDDEN_LINK_SUBSTRINGS
        if forbidden in url
    )

    assert not offenders, (
        "declared project links name a package index this project does not publish to, or the "
        "account it has moved off:\n  " + "\n  ".join(offenders)
    )
