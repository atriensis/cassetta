"""Guard: the published surface must not carry the monorepo it was split out of.

Sibling in spirit to ``test_openapi_leakage.py`` — a source-level invariant expressed as a test,
so the next contributor is told rather than trusted.

This repository is the flattened ``core/`` subtree of a private monorepo. Three kinds of residue
survived that move, and each has a guard here:

* stale ``core/``-prefixed paths in text a reader can see;
* a README quickstart documenting an API the server does not serve;
* a deployment surface that is either missing or still built for the whole workspace.

A seventh guard keeps the name of the private half's Python package out of ``src/``. The library
used to configure that package's top-level logger by name and branch on it, which published a
private arrangement as shipped code and pointed a reader at a dependency they cannot install. The
extension point that replaced it takes the tree name from the caller, so nothing under ``src/``
needs to know it any more.

A sixth guard keeps links out of the private repository this one was split from. The concrete
route back is known: the coder's inherited archive index carries a pull-request URL per historical
brief, so an ADR written with provenance taken from that index reintroduces them by hand.

A fifth guard catches the same defect spelled differently: a pointer into a directory that
stayed behind in the monorepo (``specs/…``, ``deploy/helm/…``) is just as broken as a ``core/``
path, and reads to an outside contributor as a repository with missing parts. It reads ``src/``
as well as prose, and extracts candidates by token rather than by quoting style — every pointer
that outlived the split was written as an RST span or bare in a docstring, and its first version
looked at neither.

A fourth guard keeps the private half's environment variables out of ``.env.example``. Per
Constitution V this repository has *no knowledge* of cloud features, so even a commented-out
"cloud-only" line is the leak, not a courtesy.

Two more guards hold ``docs/CONFIG.md`` to what it claims to be. The file is titled "Cassetta
configuration reference", and ``.env.example`` sends the reader to it as the full reference, so a
variable the server reads but the file omits is a promise the repository does not keep — the eighth
guard derives the expected set from ``src/`` and refuses the omission. The ninth is its mirror: a
name belonging to the private half must not appear there either, for the same reason it must not
appear in ``.env.example``.

The tenth is the ``core/`` guard's twin for the other half of the split. A path beginning ``cloud/``
names a directory that went to the private repository, so shipped prose citing one describes a tree
the reader cannot open — and, where the path was a module path, discloses the internal layout of a
distribution nobody outside can install.

The eleventh has the widest reach of all of them and the least to say about any one file: the
numbering the project was built under — brief, requirement, task and story ids — refers to documents
in that same private repository. It is the only guard that scans everything git tracks rather than
the shipped surface, because the numbering reached the tests too, and the only one that reads the
tracked *paths* as well as their contents: a test package named after the chain that produced it
carries the number where no scan of file contents can find it.

The twelfth is the only one whose subject is not the split at all. No URL here may name a machine on
a private network — and the worst instance was not in documentation but in the error the server
printed when it refused to start, which reached operators who had nothing to do with that network.
Everything that is not loopback, a private address, a name reserved for documentation, or a link
this repository deliberately publishes is reported.

The thirteenth is the mirror of the eighth, and the pair is what makes either honest: one fails when
the configuration reference omits a variable the server reads, the other when it documents one no
source file reads. An operator who sets a knob with no wire behind it and finds it in the reference
concludes it took effect.

The fourteenth is about the other thirteen. Almost every guard here reads the shipped surface
through ``_SHIPPED_FILES``, which is an enumeration — so a new document at the repository root
arrives outside all of them, and they stay green because they never look at it. That is not a
hypothetical: the changelog is assembled from commit bodies, the likeliest carrier of a private
identifier in the whole tree, and it would have landed unchecked. The guard requires every
root-level markdown file git tracks to be inside the tuple, which is a rule rather than a longer
list, and so has no next document to forget.

These are regression **locks**, not a one-off cleanup script: each must keep failing if what it
describes comes back.
"""

from __future__ import annotations

import ipaddress
import json
import re
import subprocess
from pathlib import Path

from cassetta.models import SetupRequest

REPO_ROOT = Path(__file__).resolve().parents[1]

# ``core/`` used as a repository-path prefix. The lookbehind is the whole design:
#
#   matches        core/src  core/tests  core/README.md  core/docs  cd core/  `core/`
#   not the word   open-core  build_core_defaults  CoreLimitsPolicy  "the open-core split"
#   not a slug     cassetta-core/  — nor the same slug inside a URL, wherever it is hosted
#
# "core" without a slash is legitimate prose in an open-core project and must stay readable;
# "core/" with one names a directory that does not exist here.
_MONOREPO_PATH_RE = re.compile(r"(?<![\w-])core/")

# The other half of the same split, and deliberately the same shape of rule:
#
#   matches        cloud/src  cloud/LICENSE  cloud/extensions/  cloud/src/…/claim_storage.py
#   not a slug     cassetta-cloud/  — nor the same slug inside a URL, wherever it is hosted
#
# "cloud" without a slash is ordinary English and stays readable; "cloud/" with one names a
# directory that went to the private repository. Where such a path was a *module* path it did
# more than dangle — it published the internal layout of a distribution nobody outside can
# install.
_PRIVATE_HALF_PATH_RE = re.compile(r"(?<![\w-])cloud/")

# The shipped surface: everything a reader of the public repository can see.
#
# This tuple is an enumeration, and every guard below that reads shipped prose reads it through
# here — so a root-level document missing from it is a document no guard checks, silently.
# `test_every_root_markdown_is_guarded` is what closes it: it requires the tuple to hold every
# root-level markdown file git tracks, so the next document added cannot escape the way these three
# would have.
_SHIPPED_ROOTS = ("src", "docs")
_SHIPPED_FILES = ("README.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md", "CLA.md")

# A suffix allowlist rather than a swallowed UnicodeDecodeError: an allowlist states what is
# covered, where a bare ``except`` silently skips a file that should have been read
# (``src/**/__pycache__/*.pyc`` is the case in point).
_TEXT_SUFFIXES = frozenset({".cfg", ".ini", ".json", ".md", ".py", ".toml", ".txt", ".yaml", ".yml"})

# Environment variables belonging to the private cloud repository. ``CASSETTA_AZURE_`` is a
# prefix, so every ``CASSETTA_AZURE_*`` name is caught rather than one specific spelling. The last
# two were parsed here once and governed nothing here; a name this repository stopped reading
# belongs on this list for the same reason as one it never read.
_CLOUD_ENV_VARS = (
    "CASSETTA_AZURE_",
    "CASSETTA_STORAGE_BACKEND",
    "CASSETTA_KEYSTORE_BACKEND",
    "CASSETTA_ADMIN_ROUTES",
    "CASSETTA_RATE_LIMIT_ONBOARD",
    "CASSETTA_INVITE_TTL_SECONDS",
)

# A whole ``CASSETTA_*`` variable name. The trailing ``[A-Z0-9]`` is what makes it a *name*: prose
# writes the family as ``CASSETTA_`` or ``CASSETTA_RATE_LIMIT_``, and neither is a variable anyone
# can set. Whole-name matching also keeps ``CASSETTA_JWT_KEY`` from standing in for
# ``CASSETTA_JWT_KEY_FILE`` — they are four separate entries, not one prefix.
_ENV_VAR_RE = re.compile(r"CASSETTA_[A-Z0-9_]*[A-Z0-9]")

_CONFIG_REFERENCE = "docs/CONFIG.md"

# The Python package of the private half. Named once, here, and referenced by both guards that
# check for it — the Dockerfile marker list below and the ``src/`` scan further down — so the two
# can never drift apart into two spellings of the same rule.
_PRIVATE_PACKAGE = "cassetta_cloud"

# Workspace machinery from the monorepo's Dockerfile: ``--package`` selected one member of a uv
# workspace and ``ARG BACKEND`` switched the entrypoint to a proprietary factory. This repository
# has one package and one app factory, so none of these can ever be correct here.
_WORKSPACE_BUILD_MARKERS = ("--package", "ARG BACKEND", "COPY cloud/", _PRIVATE_PACKAGE)

_DEPLOY_FILES = ("Dockerfile", "docker-compose.yml", ".dockerignore", ".env.example")

# ``build: .`` short form and the ``build:``/``context: .`` long form both count.
_COMPOSE_ROOT_CONTEXT_RE = re.compile(r"^\s*(?:build|context):\s*['\"]?\.['\"]?\s*$", re.MULTILINE)

# The ``-d '{...}'`` payload of a curl example.
_CURL_JSON_PAYLOAD_RE = re.compile(r"-d\s+'(\{.*?\})'", re.DOTALL)


def _text_files_under(root: str) -> list[Path]:
    """Every readable text file below one repository directory, in a stable order."""
    return [path for path in sorted((REPO_ROOT / root).rglob("*")) if path.is_file() and path.suffix in _TEXT_SUFFIXES]


def _shipped_text_files() -> list[Path]:
    """Every text file on the shipped surface, in a stable order."""
    files = [REPO_ROOT / name for name in _SHIPPED_FILES]
    for root in _SHIPPED_ROOTS:
        files.extend(_text_files_under(root))
    return files


def _readme_setup_example() -> str:
    """The fenced block in README.md that documents ``POST /setup``."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for block in re.findall(r"```[a-zA-Z]*\n(.*?)```", readme, re.DOTALL):
        if "/setup" in block:
            return str(block)
    raise AssertionError("README.md documents no POST /setup example — the quickstart lost a step")


def test_no_monorepo_paths_in_shipped_surfaces() -> None:
    """No ``core/``-prefixed path in ``src/``, ``README.md`` or ``docs/``."""
    scanned = _shipped_text_files()
    # Non-vacuity: a mistyped root would otherwise make this guard silently green forever.
    assert len(scanned) >= 10, f"scanned only {len(scanned)} files — the shipped roots are wrong"

    offenders: list[str] = []
    for path in scanned:
        rel = path.relative_to(REPO_ROOT)
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _MONOREPO_PATH_RE.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Monorepo paths survive on the shipped surface. This repository is the flattened "
        "`core/` subtree — `core/src` means `src`, `core/README.md` means `README.md`:\n" + "\n".join(offenders)
    )


def test_no_private_half_paths_in_shipped_prose() -> None:
    """No ``cloud/``-prefixed path in ``src/``, ``README.md`` or ``docs/``.

    The mirror of the guard above, for the subtree that went the other way. A ``cloud/`` path is
    broken in the same way a ``core/`` path is — it points into a tree this repository does not
    have — and, when it is a module path, it also says how the private distribution is laid out
    inside. An architecture decision record can keep its decision, its constraint and its outcome
    while describing the participant as what it is to a reader here: something downstream.

    Same lookbehind as ``_MONOREPO_PATH_RE``, so ``cassetta-cloud`` as a repository slug keeps
    reading normally.
    """
    scanned = _shipped_text_files()
    # Non-vacuity: a mistyped root would otherwise make this guard silently green forever.
    assert len(scanned) >= 10, f"scanned only {len(scanned)} files — the shipped roots are wrong"

    offenders: list[str] = []
    for path in scanned:
        rel = path.relative_to(REPO_ROOT)
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _PRIVATE_HALF_PATH_RE.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "shipped prose points into `cloud/`, the half of the former monorepo that went to a "
        "private repository. Nothing there can be opened from here, and a module path also "
        "discloses that distribution's internal layout:\n" + "\n".join(offenders)
    )


def test_src_does_not_name_the_private_package() -> None:
    """``src/`` names no Python package belonging to the private half.

    The library used to configure that package's top-level logger by name, and its propagation line
    branched on that one name — so the shipped code both depended on a package nobody outside can
    install and disclosed which consumer it was written for. ``configure_logging`` now takes the
    tree names from its caller, and this guard is what keeps the name from coming back the next time
    someone wants "just one more" tree wired up here.

    ``src/`` only, deliberately. This guard is about what the shipped *package* imports and
    configures, which is a stricter rule than what prose may mention: an ADR recording why three
    classes in the private half needed a ``kind`` marker names them because that is the decision
    it records. Narrow the scope to code and the guard says one thing precisely.
    """
    scanned = _text_files_under("src")
    # Non-vacuity: a mistyped root would otherwise make this guard silently green forever.
    assert len(scanned) >= 30, f"scanned only {len(scanned)} files — the source root is wrong"

    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}"
        for path in scanned
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _PRIVATE_PACKAGE in line
    ]

    assert not offenders, (
        f"the shipped package names `{_PRIVATE_PACKAGE}`, which lives in a private repository and "
        "cannot be installed from this one. A caller supplies its own logger trees via "
        "`configure_logging(..., extra_log_trees=...)`; nothing here needs to know their names:\n"
        + "\n".join(offenders)
    )


def test_readme_quickstart_matches_api() -> None:
    """The README quickstart agrees with the code it documents."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "x_setup_token" not in readme, (
        "README documents the setup token as a query parameter; the server reads the "
        "`X-Setup-Token` header (see src/cassetta/auth/dependencies.py)"
    )
    assert "X-Setup-Token" in readme, "README quickstart never shows the X-Setup-Token header"
    assert "cd core/" not in readme, "README tells the reader to cd into a directory that is gone"

    example = _readme_setup_example()
    assert "X-Setup-Token" in example, "the /setup example does not send the X-Setup-Token header"

    payload_match = _CURL_JSON_PAYLOAD_RE.search(example)
    assert payload_match is not None, "the /setup example sends no JSON request body"
    payload = json.loads(payload_match.group(1))

    # Read the field names off the model, so a future rename breaks this guard instead of
    # quietly desynchronising the README.
    assert set(payload) == set(SetupRequest.model_fields), (
        f"documented /setup body has fields {sorted(payload)}, "
        f"but SetupRequest declares {sorted(SetupRequest.model_fields)}"
    )
    assert "label" not in payload, "`label` is a derived property of SetupRequest (host:project), never an input field"


def test_deploy_files_present_and_single_package() -> None:
    """The deployment surface exists, and it builds one package rather than a workspace."""
    missing = [name for name in _DEPLOY_FILES if not (REPO_ROOT / name).is_file()]
    assert not missing, f"deployment surface incomplete — missing: {', '.join(missing)}"

    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    leaked = [marker for marker in _WORKSPACE_BUILD_MARKERS if marker in dockerfile]
    assert not leaked, "Dockerfile carries monorepo workspace machinery: " + ", ".join(
        repr(marker) for marker in leaked
    )

    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert _COMPOSE_ROOT_CONTEXT_RE.search(compose) is not None, (
        "docker-compose.yml must build from the repository root (`context: .`)"
    )


def test_env_example_carries_no_cloud_vars() -> None:
    """``.env.example`` names no variable belonging to the private cloud repository."""
    env_example = REPO_ROOT / ".env.example"
    assert env_example.is_file(), ".env.example is missing — the README quickstart copies it"

    text = env_example.read_text(encoding="utf-8")
    leaked = [name for name in _CLOUD_ENV_VARS if name in text]
    assert not leaked, ".env.example names variables of the private cloud repository: " + ", ".join(
        repr(name) for name in leaked
    )


def _env_var_names(text: str) -> set[str]:
    """Every whole ``CASSETTA_*`` variable name mentioned in one file."""
    return set(_ENV_VAR_RE.findall(text))


def test_config_reference_documents_every_env_var() -> None:
    """``docs/CONFIG.md`` documents every ``CASSETTA_*`` variable the source reads.

    The expected set is derived from ``src/``, never written down here. A literal list would be a
    second place to forget a variable, and forgetting one is the defect this guard exists to catch:
    the reference is what ``.env.example`` calls the full reference, so a name the server reads and
    the document omits sends an operator looking through source for the value that starts the server.
    """
    sources = _text_files_under("src")
    # Non-vacuity: a mistyped root would otherwise make this guard silently green forever.
    assert len(sources) >= 30, f"scanned only {len(sources)} files — the source root is wrong"

    expected: set[str] = set()
    for path in sources:
        expected |= _env_var_names(path.read_text(encoding="utf-8"))
    assert len(expected) >= 25, f"found only {len(expected)} variable names in src/ — the pattern is wrong"

    reference = REPO_ROOT / _CONFIG_REFERENCE
    assert reference.is_file(), f"{_CONFIG_REFERENCE} is missing — .env.example points readers at it"

    missing = sorted(expected - _env_var_names(reference.read_text(encoding="utf-8")))

    assert not missing, (
        f"the source reads {len(expected)} environment variables; {_CONFIG_REFERENCE} documents "
        f"{len(expected) - len(missing)} of them. Undocumented:\n  " + "\n  ".join(missing)
    )


def test_config_reference_documents_nothing_the_source_does_not_read() -> None:
    """``docs/CONFIG.md`` documents no ``CASSETTA_*`` variable that no source file reads.

    The mirror of ``test_config_reference_documents_every_env_var``, and the pair is what makes
    either honest: one fails when the reference omits a variable the server reads, this one fails
    when the reference invents one it does not. Both derive the same two sets the same way, so
    neither can be satisfied by editing a list.

    A documented variable nothing reads is worse than an undocumented one. An operator sets it,
    finds it in the reference, and concludes it took effect — where an omission at least sends
    them to the source, which is the truth.
    """
    sources = _text_files_under("src")
    # Non-vacuity: a mistyped root would otherwise make this guard silently green forever.
    assert len(sources) >= 30, f"scanned only {len(sources)} files — the source root is wrong"

    read: set[str] = set()
    for path in sources:
        read |= _env_var_names(path.read_text(encoding="utf-8"))
    assert len(read) >= 25, f"found only {len(read)} variable names in src/ — the pattern is wrong"

    reference = REPO_ROOT / _CONFIG_REFERENCE
    assert reference.is_file(), f"{_CONFIG_REFERENCE} is missing — .env.example points readers at it"

    documented = _env_var_names(reference.read_text(encoding="utf-8"))
    assert len(documented) >= 20, (
        f"{_CONFIG_REFERENCE} names only {len(documented)} variables — it is not the reference"
    )

    invented = sorted(documented - read)

    assert not invented, (
        f"{_CONFIG_REFERENCE} documents {len(invented)} variable(s) that no file under src/ reads. "
        "Setting one of these does nothing, so the table promises a knob with no wire behind it — "
        "delete the row, or wire the variable up:\n  " + "\n  ".join(invented)
    )


def test_config_reference_names_no_private_half_var() -> None:
    """``docs/CONFIG.md`` names no variable belonging to the private cloud repository.

    The mirror of ``test_env_example_carries_no_cloud_vars``, and the same rule: this repository has
    no knowledge of cloud features, so a reference entry for one — even phrased as an absence, even
    as a note that other variables exist elsewhere — is the leak rather than a courtesy. Both guards
    read the same ``_CLOUD_ENV_VARS``, so the rule cannot drift into two spellings.
    """
    reference = REPO_ROOT / _CONFIG_REFERENCE
    assert reference.is_file(), f"{_CONFIG_REFERENCE} is missing — .env.example points readers at it"

    text = reference.read_text(encoding="utf-8")
    # Non-vacuity: an empty or truncated file names no forbidden variable either.
    documented = _env_var_names(text)
    assert len(documented) >= 20, (
        f"{_CONFIG_REFERENCE} names only {len(documented)} variables — it is not the reference"
    )

    leaked = [name for name in _CLOUD_ENV_VARS if name in text]
    assert not leaked, f"{_CONFIG_REFERENCE} names variables of the private cloud repository: " + ", ".join(
        repr(name) for name in leaked
    )


# Repository-relative references in prose. A pointer is checked when it names one of these roots
# (or climbs out of the tree with ``../``) — enough to catch ``specs/513-…`` and
# ``../deploy/helm/…``, the two forms this repository actually shipped, without flagging every
# slash in a sentence. Anchors, URLs and bare fragments are not paths and are skipped.
_REPO_PATH_ROOTS = ("src/", "docs/", "tests/", "specs/", "deploy/", "infra/", "contracts/", "scripts/", "../")
# Paths that look repo-relative but are illustrative — a path in the *reader's* bundle, not a
# pointer into this repository. Named explicitly so the exception is visible rather than bought
# by dropping ``src/`` from the roots and losing every real ``src/`` pointer with it.
_ILLUSTRATIVE_PATHS = frozenset({"src/main.py", "./src/main.py"})

# A repository-relative pointer, however it is quoted. Extraction is by token rather than by
# quoting style because the same pointer is written four ways in this tree: as a markdown link,
# as a single-backtick span, as an RST double-backtick span, and bare in running docstring text.
# Matching the roots directly catches all four and anything else someone invents.
#
# The lookbehind is the whole design. ``str.startswith`` could only ever match at index 0 of an
# already-extracted token, so it could not see a root buried inside a longer path; a free-text
# scan gives that property up and has to buy it back. Without it, two pieces of perfectly correct
# text are reported as broken pointers:
#
#   GET /inbox/.../peek     an elided REST path — `../peek` sits inside `.../peek`
#   ./drop/src/lib.py       a path in the *reader's* upload bundle — `src/lib.py` sits inside it
#
# `(?<![\w./-])` says "the character before this is not part of a path", which is exactly what
# ``startswith`` used to guarantee.
_REPO_PATH_TOKEN_RE = re.compile(
    r"(?<![\w./-])(?:" + "|".join(re.escape(root) for root in _REPO_PATH_ROOTS) + r")[\w./*-]*"
)


def _pointer_files() -> list[Path]:
    """Every file that can carry a repository-relative pointer a reader will try to follow."""
    return _prose_files() + _text_files_under("src")


def _prose_files() -> list[Path]:
    return [REPO_ROOT / name for name in _SHIPPED_FILES] + sorted((REPO_ROOT / "docs").rglob("*.md"))


def test_no_dangling_repo_links() -> None:
    """Every repository-relative pointer in shipped prose and source resolves to something present.

    The ``core/`` guard above catches the residue that is spelled as a stale prefix. This one
    catches the residue spelled as a *destination*: ``specs/513-peek-and-limits-policy/`` and
    ``../deploy/helm/cassetta/README.md`` both survived the split as pointers into directories
    that did not come across, so the published documentation sent readers to a tree only the
    private repository can see.

    ``src/`` is scanned as well as prose. Docstrings are documentation — a contributor reads them
    in the editor rather than on a documentation site, which makes a pointer there *more* likely
    to be followed, not less. This guard's own docstring named ``specs/…`` as its quarry while
    its file set never looked at the one directory where every surviving ``specs/`` pointer
    lived.
    """
    scanned = _pointer_files()
    # Non-vacuity: a mistyped root would otherwise make this guard silently green forever.
    assert len(scanned) >= 40, f"scanned only {len(scanned)} files — the pointer roots are wrong"

    dangling: list[str] = []
    for doc in scanned:
        text = doc.read_text(encoding="utf-8")
        for match in _REPO_PATH_TOKEN_RE.finditer(text):
            token = match.group(0).split("#", 1)[0].strip().rstrip(".,;:)")
            if not token or "://" in token or token in _ILLUSTRATIVE_PATHS:
                continue
            # Backticked paths are repo-root-relative by convention here; a markdown link may be
            # relative to its own file. Accept either resolution before calling it dangling.
            if (REPO_ROOT / token).exists() or (doc.parent / token).exists():
                continue
            dangling.append(f"{doc.relative_to(REPO_ROOT)} -> {token}")

    assert not dangling, (
        "shipped documentation points at paths that do not exist in this repository:\n  "
        + "\n  ".join(sorted(set(dangling)))
    )


# The personal account this repository used to live under, and which the private monorepo it was
# split out of still does. The whole account, not one repository under it.
#
# This pattern used to end `…/cassetta(?![\w-])`, and the lookahead had exactly one job: to let this
# repository's own links through while rejecting its predecessor's. That job no longer exists —
# this project moved, and none of its links are under this account any more. What remains under it
# is either the private predecessor or a stale path to where this project used to be, and both are
# defects in shipped prose, so the exemption is gone and the rule is the simpler one.
#
# `(?![\w-])` is kept for a different reason: it stops the pattern from matching an unrelated
# account whose name merely begins with this one. Same idiom as the two path guards above.
_PREDECESSOR_URL_RE = re.compile(r"github\.com/ximera239(?![\w-])")


def test_no_links_into_the_private_predecessor() -> None:
    """Shipped prose must not link into the personal account this repository has moved off.

    Two ADRs cited the pull request and issue that implemented their decision. Those live in the
    monorepo this repository was split out of, which is private and stays that way, so a published
    document was sending readers to a 404 and disclosing the shape of a backlog they cannot read.

    The rule is now the account rather than one repository within it, because the move made the
    narrower version incoherent: a link under that account can no longer be one of ours. Either it
    names the private predecessor, or it names where this project used to be — a reader following it
    gets a 404 in the first case and someone else's tree in the second.

    The route back is specific rather than hypothetical: the coder's inherited archive index lists
    one such URL per historical brief, and an ADR that takes its provenance from there picks them
    up again.
    """
    offenders = [
        f"{doc.relative_to(REPO_ROOT)}:{i}"
        for doc in _prose_files()
        for i, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1)
        if _PREDECESSOR_URL_RE.search(line)
    ]
    assert not offenders, "shipped documentation links into the private predecessor repository:\n  " + "\n  ".join(
        offenders
    )


# The numbering the project was built under, in the shapes it actually took:
#
#   matches       Brief 533   brief 513   Brief-529   brief-529   brief_512
#                 FR-026   SC-014   T018   US3
#   not           Brief NNN   FRAGMENT   SC   brief-taking   _T018   ABCT018   USD   BUS3
#
# Each names a document in the private repository this one was split out of. `FR-026` resolves
# for an outside reader exactly as well as `Brief 533` does — not at all — so it tells them
# nothing while telling everyone the shape of a backlog they cannot read.
#
# The word boundaries are load-bearing on the last two and on nothing else: the older shapes are
# self-delimiting, but a bare `T018` would otherwise match inside an identifier or a hex digest.
# `\b` also correctly declines `_T018`, because an underscore is a word character.
#
# `[Bb]rief[ _-]` puts BOTH the case and the separator in a character class, rather than spending
# one alternative per spelling, because `Brief 512`, `brief 512`, `Brief-512`, `brief-512` and
# `brief_512` all name the same document — and every extra alternative is another place to forget
# a spelling.
#
# That is not a hypothetical worry. Two sweeps have now ended with survivors sitting in a
# combination nobody had enumerated: the first left the separator uncovered, the second left
# lower-case-with-a-space (25 of them) and capital-with-a-hyphen (3). Both times this guard was
# green over every single survivor, because it was listing spellings instead of describing the
# shape. Do not "simplify" the classes back into a list of alternatives — the coverage test below
# fails if you do, and it names the combinations that got through twice.
_INTERNAL_IDENTIFIER_RE = re.compile(r"[Bb]rief[ _-][0-9]{3}|FR-[0-9]{3}|SC-[0-9]+|\bT[0-9]{3}\b|\bUS[0-9]+\b")


def test_identifier_pattern_matches_every_brief_spelling() -> None:
    """The brief numbering is recognised in every combination of case and separator.

    This is what makes the merged character classes a decision rather than a formatting choice.
    Collapsing them back into one alternative per spelling reads as a harmless simplification and
    is the exact mistake that left 28 citations in the tree with the guard green over all of them,
    so the six combinations are asserted here one by one.

    The pattern is fed strings rather than the repository: the point is what the recogniser
    accepts, which the tracked-file scan cannot show — a scan over a clean tree is green under a
    correct pattern and under a broken one alike.
    """
    spellings = [
        "Brief 512",
        "Brief-512",
        "Brief_512",
        "brief 512",
        "brief-512",
        "brief_512",
    ]

    missed = [spelling for spelling in spellings if not _INTERNAL_IDENTIFIER_RE.search(spelling)]

    assert not missed, (
        "the identifier pattern no longer covers every spelling of the private brief numbering. "
        "Each combination of case and separator names the same unreachable document, so a "
        "spelling the pattern misses is a citation that ships:\n  " + "\n  ".join(missed)
    )


# Writing the shapes out is this file's job, and nothing else's — so it is the one path the
# scan below skips.
_GUARD_SELF_PATH = Path(__file__).resolve().relative_to(REPO_ROOT)


def _tracked_paths() -> list[str]:
    """Every path git tracks, repository-relative, in a stable order.

    ``git ls-files`` rather than a tree walk: "tracked" is exactly the set wanted, it excludes
    ``.venv/`` without naming it, and any hand-written exclusion list here would be a second copy
    of ``.gitignore`` that drifts from the first.

    Returned as strings rather than paths because one caller reads the path *as text*: a directory
    can carry the private numbering in its own name, where no scan of file contents can see it.
    """
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return sorted(name for name in listing.split("\0") if name)


def _tracked_text_files() -> list[Path]:
    """Every text file git tracks, in a stable order.

    Wider than ``_shipped_text_files()`` deliberately. The numbering this feeds reached the test
    suite and ``pyproject.toml`` as well as the shipped surface, so a guard scoped to what a
    reader of the published repository sees would have stayed green with hundreds of citations
    still in the tree.
    """
    tracked = (REPO_ROOT / name for name in _tracked_paths())
    return sorted(path for path in tracked if path.suffix in _TEXT_SUFFIXES and path.is_file())


def test_no_internal_identifiers_in_tracked_files() -> None:
    """No tracked path or text file cites the private chain's brief, task or story numbers.

    They are not noise — most annotated a real decision, and the sentence around them was worth
    keeping. But the number itself points at a document in a repository the reader has no access
    to, so it answers nothing and publishes the contours of a private backlog by existing.

    Paths are scanned as well as contents, because a number can hide in a directory name where no
    content scan reaches it: a test package called after the chain that produced it says exactly
    as much to an outside reader as a citation in a comment does.

    The exclusion below is by path and only by path. Weakening the pattern until it no longer
    matched this file's own examples would buy the same green at the cost of a guard that no
    longer says what it checks.
    """
    tracked = _tracked_paths()
    # Non-vacuity: an empty or truncated listing would otherwise make this guard green forever.
    assert len(tracked) >= 150, f"listed only {len(tracked)} paths — the tracked-file listing is wrong"

    offenders = [
        f"{name}: (in the path itself)"
        for name in tracked
        if name != str(_GUARD_SELF_PATH) and _INTERNAL_IDENTIFIER_RE.search(name)
    ]

    scanned = _tracked_text_files()
    assert len(scanned) >= 150, f"scanned only {len(scanned)} files — the tracked-file listing is wrong"

    for path in scanned:
        rel = path.relative_to(REPO_ROOT)
        if rel == _GUARD_SELF_PATH:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _INTERNAL_IDENTIFIER_RE.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "tracked files cite the private chain's numbering. The briefs, tasks and stories these "
        "name are not in this repository and cannot be looked up from it — drop the number and "
        "keep the sentence:\n" + "\n".join(offenders)
    )


def test_every_root_markdown_is_guarded() -> None:
    """Every root-level markdown document is inside the guarded shipped set.

    This is the only guard here whose subject is the *other* guards. ``_SHIPPED_FILES`` is an
    enumeration, and every guard above that reads shipped prose reads it through that tuple — so a
    new document at the repository root arrives with no identifier check, no dangling-pointer check
    and no predecessor-link check, and nothing says so. The failure is silent by construction: the
    guards stay green because they never look.

    That is not hypothetical. This repository's changelog is assembled from commit bodies, which is
    the likeliest place in the whole tree for a private identifier to be sitting, and it would have
    landed outside every one of these checks.

    The rule is deliberately not "the tuple contains these four names" — that is the same
    enumeration one level up, and the next document escapes it identically. It is "nothing at the
    root is outside the tuple", which has no next document to forget.

    ``_tracked_paths()`` rather than a glob of the directory: an untracked draft in someone's
    working tree is not published, and failing the suite over one would be a false positive about a
    file no reader can see. The same reasoning already governs the identifier guard above.
    """
    root_markdown = {name for name in _tracked_paths() if name.endswith(".md") and "/" not in name}

    # Non-vacuity, and deliberately not a count: a floor of "at least four" would re-encode today's
    # document list as a number, and go red the day one is legitimately retired. Naming the one file
    # that has been at this root since the initial import catches the failure that actually matters
    # here — a listing that returned nothing, under which the subset test below is trivially true.
    assert "README.md" in root_markdown, (
        f"the tracked listing found no README.md at the repository root — it returned "
        f"{sorted(root_markdown)}, so this guard is reading the wrong thing"
    )

    unguarded = sorted(root_markdown - set(_SHIPPED_FILES))

    assert not unguarded, (
        "root-level documents are outside the guarded shipped set, so none of the guards above "
        "reads them — not the private-identifier scan, not the dangling-pointer check, not the "
        "predecessor-link rule. Add them to `_SHIPPED_FILES`:\n  " + "\n  ".join(unguarded)
    )


# Every ``http(s)://`` occurrence, up to the first character that cannot be part of an authority.
# `\\` is excluded so an escape sequence in a Python string literal ends the match rather than
# being swallowed into the host.
_URL_AUTHORITY_RE = re.compile(r"https?://([^\s/?#\"'`<>\\)\]}]+)", re.IGNORECASE)

# Names reserved by RFC 2606 and RFC 6761 for documentation, testing and local use. None of them
# can be registered, so none of them can name a real deployment.
_RESERVED_HOSTS = frozenset({"localhost", "example.com", "example.net", "example.org"})
_RESERVED_SUFFIXES = (
    ".localhost",
    ".example",
    ".test",
    ".invalid",
    ".local",
    ".example.com",
    ".example.net",
    ".example.org",
)

# Hosts this repository links to on purpose. Written out rather than admitted by a looser rule,
# because any rule broad enough to let `caddyserver.com` through also lets a private deployment's
# hostname through — which is the one thing this guard exists to stop. Five visible exceptions beat
# a rule that cannot do its job.
#
# `github.com` is the forge this project is published on, and it is here because the packaging
# metadata and the README now link to it. It does not weaken the guard: this guard's subject, stated
# in its docstring below, is a machine on someone's private network, and a public forge is neither
# private nor a deployment. The question this one does *not* answer — whether a forge link points
# where this project actually is — belongs to `test_no_links_into_the_private_predecessor`, which
# rejects the account this repository used to live under. Host here, account there.
_ALLOWED_EXTERNAL_HOSTS = frozenset(
    {
        "docs.docker.com",
        "caddyserver.com",
        "doc.traefik.io",
        "www.apache.org",
        "github.com",
    }
)


def _url_scanned_files() -> list[Path]:
    """Every file whose URLs a reader can see.

    ``_tracked_text_files()`` plus the deployment surface it cannot reach: ``Dockerfile`` has no
    suffix at all and ``scripts/*`` are shell. Both ship, and both carry URLs.

    ``_TEXT_SUFFIXES`` is deliberately not widened to cover them — three other guards derive their
    file sets from it, and adding an extensionless-file rule there would silently change what
    those guards police. The extra handful belongs to the guard that needs it.
    """
    extra = [REPO_ROOT / "Dockerfile", REPO_ROOT / "docker-compose.yml"]
    extra.extend(sorted(p for p in (REPO_ROOT / "scripts").rglob("*") if p.is_file()))
    seen = {path.resolve() for path in _tracked_text_files()}
    files = _tracked_text_files()
    files.extend(p for p in extra if p.is_file() and p.resolve() not in seen)
    return files


def _url_host(authority: str) -> str:
    """The bare lower-case host of a URL authority — userinfo and port removed."""
    host = authority.rpartition("@")[2]
    head, sep, tail = host.rpartition(":")
    if sep and tail.isdigit():
        host = head
    return host.strip("[]").lower()


def _is_publishable_host(host: str) -> bool:
    """Whether a URL host is safe to publish, by the five rules this guard enforces."""
    if not any(char.isalnum() for char in host):
        # `http://...` in prose is an elision standing in for a URL, not a hostname.
        return True
    if host in _RESERVED_HOSTS or host.endswith(_RESERVED_SUFFIXES):
        return True
    if "." not in host:
        # A single-label host cannot be registered, so it can only ever be a placeholder.
        return True
    if host in _ALLOWED_EXTERNAL_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


def test_no_private_hosts_in_urls() -> None:
    """No URL in this repository names a machine on someone's private network.

    The worst instance of this was not in documentation but in a *runtime* surface: the error the
    server printed when its public base URL was unset offered, as its first example, the hostname
    of one machine on the network this project was developed on. It reached operators who had
    nothing to do with that network, and it named the machine's role while it was at it.

    A public commit stays in the history after the file changes, which is what makes this the one
    class of mistake that cannot be taken back — and why the rule is an allowlist. Anything that
    is not demonstrably un-routable, reserved, or a link this repository means to publish is
    reported.

    The IP test is delegated to ``ipaddress`` rather than written as a CIDR list: the module
    already knows every reserved range, and a hand-written list would be a second, worse copy of
    the same table.
    """
    scanned = _url_scanned_files()
    # Non-vacuity: a mistyped root would otherwise make this guard silently green forever.
    assert len(scanned) >= 150, f"scanned only {len(scanned)} files — the file set is wrong"

    offenders: list[str] = []
    seen_urls = 0
    for path in scanned:
        rel = path.relative_to(REPO_ROOT)
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for match in _URL_AUTHORITY_RE.finditer(line):
                seen_urls += 1
                host = _url_host(match.group(1))
                if not _is_publishable_host(host):
                    offenders.append(f"{rel}:{lineno}: {host}")

    # Non-vacuity: an extractor that matches nothing finds no bad hosts either.
    assert seen_urls >= 10, f"found only {seen_urls} URLs — the extractor is wrong"

    assert not offenders, (
        "URLs name hosts that are neither loopback, nor a private address, nor a name reserved "
        "for documentation, nor an allowlisted third-party link. A hostname that resolves only on "
        "one network tells every other reader nothing and tells everyone that network's shape:\n  "
        + "\n  ".join(sorted(set(offenders)))
    )
