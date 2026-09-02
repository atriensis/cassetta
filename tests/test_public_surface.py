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
path, and reads to an outside contributor as a repository with missing parts.

A fourth guard keeps the private half's environment variables out of ``.env.example``. Per
Constitution V this repository has *no knowledge* of cloud features, so even a commented-out
"cloud-only" line is the leak, not a courtesy.

Two more guards hold ``docs/CONFIG.md`` to what it claims to be. The file is titled "Cassetta
configuration reference", and ``.env.example`` sends the reader to it as the full reference, so a
variable the server reads but the file omits is a promise the repository does not keep — the eighth
guard derives the expected set from ``src/`` and refuses the omission. The ninth is its mirror: a
name belonging to the private half must not appear there either, for the same reason it must not
appear in ``.env.example``.

These are regression **locks**, not a one-off cleanup script: each must keep failing if what it
describes comes back.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from cassetta.models import SetupRequest

REPO_ROOT = Path(__file__).resolve().parents[1]

# ``core/`` used as a repository-path prefix. The lookbehind is the whole design:
#
#   matches        core/src  core/tests  core/MIGRATION.md  core/docs  cd core/  `core/`
#   not the word   open-core  build_core_defaults  CoreLimitsPolicy  "the open-core split"
#   not a slug     cassetta-core/  github.com/ximera239/cassetta-core/
#
# "core" without a slash is legitimate prose in an open-core project and must stay readable;
# "core/" with one names a directory that does not exist here.
_MONOREPO_PATH_RE = re.compile(r"(?<![\w-])core/")

# The shipped surface: everything a reader of the public repository can see.
_SHIPPED_ROOTS = ("src", "docs")
_SHIPPED_FILES = ("README.md", "MIGRATION.md")

# A suffix allowlist rather than a swallowed UnicodeDecodeError: an allowlist states what is
# covered, where a bare ``except`` silently skips a file that should have been read
# (``src/**/__pycache__/*.pyc`` is the case in point).
_TEXT_SUFFIXES = frozenset({".cfg", ".ini", ".json", ".md", ".py", ".toml", ".txt", ".yaml", ".yml"})

# Environment variables belonging to the private cloud repository. ``CASSETTA_AZURE_`` is a
# prefix, so every ``CASSETTA_AZURE_*`` name is caught rather than one specific spelling.
_CLOUD_ENV_VARS = (
    "CASSETTA_AZURE_",
    "CASSETTA_STORAGE_BACKEND",
    "CASSETTA_KEYSTORE_BACKEND",
    "CASSETTA_ADMIN_ROUTES",
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
    """No ``core/``-prefixed path in ``src/``, ``README.md``, ``MIGRATION.md`` or ``docs/``."""
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
        "`core/` subtree — `core/src` means `src`, `core/MIGRATION.md` means `MIGRATION.md`:\n" + "\n".join(offenders)
    )


def test_src_does_not_name_the_private_package() -> None:
    """``src/`` names no Python package belonging to the private half.

    The library used to configure that package's top-level logger by name, and its propagation line
    branched on that one name — so the shipped code both depended on a package nobody outside can
    install and disclosed which consumer it was written for. ``configure_logging`` now takes the
    tree names from its caller, and this guard is what keeps the name from coming back the next time
    someone wants "just one more" tree wired up here.

    ``src/`` only, deliberately. ``MIGRATION.md`` names the same package legitimately — it is the
    operator-facing chronicle of changes that happened, including changes to that package — and
    rewriting history is not what this guard is for.
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
_BACKTICKED_RE = re.compile(r"`([^`\n]+)`")
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _looks_like_repo_path(token: str) -> bool:
    return token.startswith(_REPO_PATH_ROOTS)


def _prose_files() -> list[Path]:
    return [REPO_ROOT / name for name in _SHIPPED_FILES] + sorted((REPO_ROOT / "docs").rglob("*.md"))


def test_no_dangling_repo_links() -> None:
    """Every repository-relative pointer in shipped prose resolves to something present.

    The ``core/`` guard above catches the residue that is spelled as a stale prefix. This one
    catches the residue spelled as a *destination*: ``specs/513-peek-and-limits-policy/`` and
    ``../deploy/helm/cassetta/README.md`` both survived the split as pointers into directories
    that did not come across, so the published documentation sent readers to a tree only the
    private repository can see.
    """
    dangling: list[str] = []
    for doc in _prose_files():
        text = doc.read_text(encoding="utf-8")
        candidates = {m.group(1) for m in _BACKTICKED_RE.finditer(text)}
        candidates |= {m.group(1) for m in _MD_LINK_RE.finditer(text)}
        for raw in candidates:
            token = raw.split("#", 1)[0].strip().rstrip(".,;:)")
            if not token or "://" in token or not _looks_like_repo_path(token):
                continue
            if token in _ILLUSTRATIVE_PATHS:
                continue
            # Backticked paths are repo-root-relative by convention here; a markdown link may be
            # relative to its own file. Accept either resolution before calling it dangling.
            if (REPO_ROOT / token).exists() or (doc.parent / token).exists():
                continue
            dangling.append(f"{doc.relative_to(REPO_ROOT)} -> {token}")

    assert not dangling, (
        "shipped documentation points at paths that do not exist in this repository:\n  "
        + "\n  ".join(sorted(dangling))
    )


# The private monorepo this repository was split out of. ``(?![\w-])`` is what separates it from
# this repository's own ``…/cassetta-core`` URLs, which are legitimate and expected.
_PREDECESSOR_URL_RE = re.compile(r"github\.com/ximera239/cassetta(?![\w-])")


def test_no_links_into_the_private_predecessor() -> None:
    """Shipped prose must not link into the private repository this one was split from.

    Two ADRs cited the pull request and issue that implemented their decision. Those live in the
    monorepo, which is private and stays that way, so a published document was sending readers to
    a 404 and disclosing the shape of a backlog they cannot read.

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
