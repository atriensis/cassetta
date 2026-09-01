"""Guard: the published surface must not carry the monorepo it was split out of.

Sibling in spirit to ``test_openapi_leakage.py`` — a source-level invariant expressed as a test,
so the next contributor is told rather than trusted.

This repository is the flattened ``core/`` subtree of a private monorepo. Three kinds of residue
survived that move, and each has a guard here:

* stale ``core/``-prefixed paths in text a reader can see;
* a README quickstart documenting an API the server does not serve;
* a deployment surface that is either missing or still built for the whole workspace.

A sixth guard keeps links out of the private repository this one was split from. The concrete
route back is known: the coder's inherited archive index carries a pull-request URL per historical
brief, so an ADR written with provenance taken from that index reintroduces them by hand.

A fifth guard catches the same defect spelled differently: a pointer into a directory that
stayed behind in the monorepo (``specs/…``, ``deploy/helm/…``) is just as broken as a ``core/``
path, and reads to an outside contributor as a repository with missing parts.

A fourth guard keeps the private half's environment variables out of ``.env.example``. Per
Constitution V this repository has *no knowledge* of cloud features, so even a commented-out
"cloud-only" line is the leak, not a courtesy.

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

# Workspace machinery from the monorepo's Dockerfile: ``--package`` selected one member of a uv
# workspace and ``ARG BACKEND`` switched the entrypoint to a proprietary factory. This repository
# has one package and one app factory, so none of these can ever be correct here.
_WORKSPACE_BUILD_MARKERS = ("--package", "ARG BACKEND", "COPY cloud/", "cassetta_cloud")

_DEPLOY_FILES = ("Dockerfile", "docker-compose.yml", ".dockerignore", ".env.example")

# ``build: .`` short form and the ``build:``/``context: .`` long form both count.
_COMPOSE_ROOT_CONTEXT_RE = re.compile(r"^\s*(?:build|context):\s*['\"]?\.['\"]?\s*$", re.MULTILINE)

# The ``-d '{...}'`` payload of a curl example.
_CURL_JSON_PAYLOAD_RE = re.compile(r"-d\s+'(\{.*?\})'", re.DOTALL)


def _shipped_text_files() -> list[Path]:
    """Every text file on the shipped surface, in a stable order."""
    files = [REPO_ROOT / name for name in _SHIPPED_FILES]
    for root in _SHIPPED_ROOTS:
        files.extend(
            path for path in sorted((REPO_ROOT / root).rglob("*")) if path.is_file() and path.suffix in _TEXT_SUFFIXES
        )
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
