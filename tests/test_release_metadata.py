"""Guard: the three places this repository states what it is must agree with each other.

A release is cut from a tree, not from a command, and this tree says its version in two places and
records what shipped in a third. Any one of them can be forgotten in a bump, and the resulting
defect is only visible after the tag is public — which is the point at which it is expensive.

Sibling in spirit to ``test_public_surface.py``: the same idea that a rule worth keeping is a rule
a test holds, rather than one a checklist asks about.

Four things are held here.

* **The changelog's shape.** Sections are unique and newest-first. A duplicated version makes "the
  top section" and "the section for this version" two different things, which is exactly the
  ambiguity the release check downstream must not have to resolve.
* **The changelog's top section names the version the package declares.** A release whose changelog
  has not caught up ships a document that describes the previous one.
* **The two version sites agree.** ``pyproject.toml`` is what a consumer installs; the package's own
  ``__version__`` is what a running deployment reports. When they diverge, the second lies to
  whoever is holding an incident.
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


def _packaging_version() -> str:
    """The version ``pyproject.toml`` declares."""
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    version = project["version"]
    assert isinstance(version, str)
    return version


def _package_version() -> str:
    """The version ``src/cassetta/__init__.py`` declares."""
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
    declared = _packaging_version()

    assert top == declared, (
        f"pyproject.toml declares version {declared}, but the changelog's newest section is {top}. "
        "Either the bump has not been recorded, or the record is for a release that is not this one."
    )


def test_the_two_version_sites_agree() -> None:
    """``pyproject.toml`` and ``src/cassetta/__init__.py`` declare the same version.

    Two sites, two audiences: the first is what a consumer resolves and pins, the second is what a
    running deployment reports about itself. A disagreement is not cosmetic — it means a version
    number taken from a live server does not identify the code that is running, which is discovered
    while someone is trying to work out what broke.

    ``make release-check`` holds this same invariant before a tag is cut. Held twice on purpose: this
    guard catches it during development, that one catches it in a clean clone with no environment,
    which is the state a release is actually cut from.
    """
    packaging = _packaging_version()
    package = _package_version()

    assert packaging == package, (
        f"the two version sites disagree: pyproject.toml says {packaging}, src/cassetta/__init__.py says {package}"
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
