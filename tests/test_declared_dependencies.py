"""Guard: every distribution ``src/`` imports is one this package asks for by name.

Four were not. ``starlette``, ``pydantic``, ``limits`` and ``anyio`` were written down across seven
import lines and declared nowhere — they reached the environment as transitives of ``fastapi``,
``slowapi`` and ``httpx``, which is to say **this package depended on the shape of somebody else's
dependency graph**. That works until the graph changes, and then it breaks for a reason that has
nothing to do with this project and nothing in this repository to point at.

``pydantic`` showed the sharper form of the same problem: ``src/cassetta/models.py`` imports
``field_validator``, which exists only in Pydantic 2, while the floor this package does declare —
``fastapi>=0.115`` — still admits Pydantic 1. A resolver was free to produce an installation this
code cannot run on, and no declaration here said otherwise.

The repository had already taken the opposite decision once, for ``pyyaml``, and written down why.
This is that decision applied to the general case, and made a test so it stays applied.

**The mapping is the part that must not be string equality.** An import name and a distribution name
are different things: ``import jwt`` is shipped by ``PyJWT`` and declared here as ``pyjwt``. The
resolution goes through :func:`importlib.metadata.packages_distributions`, and both sides are
compared as PEP 503 canonical names.

**One direction only.** Imports must be declared; declarations need not be imported. ``uvicorn`` is
declared and imported by nothing under ``src/`` — it is run as a process. A symmetric check would
demand deleting a correct declaration.

**Constraints are not declarations.** All four undeclared distributions were already pinned in
``[tool.uv] constraint-dependencies``. Constraints bind resolution and never reach wheel metadata, so
they do nothing for anyone installing from an index — which is why a constrained-but-undeclared
distribution is precisely the defect this test names rather than an exemption from it.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from collections import defaultdict
from importlib.metadata import packages_distributions
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
TESTS = PROJECT_ROOT / "tests"
PYPROJECT = PROJECT_ROOT / "pyproject.toml"

# This package's own import name. Excluded from the imported set rather than added to the declared
# one, so that `dev`'s `cassetta[server]` self-reference can never be the reason a root passes.
_OWN_PACKAGE = "cassetta"


def _declared_distributions() -> set[str]:
    """Canonical names of everything `pyproject.toml` asks for, in any list.

    Base dependencies and every extra, `dev` included: the question is whether *this project* has
    written the dependency down anywhere, not which install profile carries it.
    """
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    specs = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        specs.extend(extra)
    return {canonicalize_name(Requirement(spec).name) for spec in specs}


def _imported_roots(walk_root: Path) -> dict[str, list[str]]:
    """Third-party import roots under `walk_root`, mapped to the files that import them.

    `ast.walk` rather than a scan of module-level statements: an import inside a function, and an
    import inside `if TYPE_CHECKING:`, both name a distribution someone has to supply. The guarded
    one is not hypothetical here — `rate_limit/limiter.py` has one, and `mypy` resolves it for real.

    `ast.parse` is deliberately not wrapped: a file that does not parse is a defect to surface, not
    to route around.
    """
    roots: dict[str, list[str]] = defaultdict(list)
    for path in sorted(walk_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = str(path.relative_to(PROJECT_ROOT))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    roots[alias.name.split(".")[0]].append(relative)
            # `level > 0` is a relative import: it names a module in this package, never a
            # distribution.
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots[node.module.split(".")[0]].append(relative)

    return {
        root: sorted(set(files))
        for root, files in roots.items()
        if root not in sys.stdlib_module_names and root != _OWN_PACKAGE
    }


def _local_module_names(walk_root: Path) -> set[str]:
    """Top-level names that a module inside `walk_root` supplies, rather than a distribution.

    Only meaningful for a tree whose files are imported by bare name. `tests/` is such a tree: this
    project declares no `__init__.py` under it (see the `--import-mode=importlib` note in
    `pyproject.toml`), so a directory that wants a shared helper puts itself on `sys.path` from its
    `conftest.py` and imports the helper as a top-level module. `tests/observability/` does exactly
    that, for `_obs_helpers`.

    Both shapes count: a module file, and a directory that is a package. Nothing here asks whether
    the name is *reachable* from any particular test — a name this tree defines is a name this tree
    can supply, and the alternative is reimplementing pytest's path handling in a guard about
    dependency declarations.
    """
    names = {path.stem for path in walk_root.rglob("*.py")}
    names |= {path.name for path in walk_root.rglob("*") if path.is_dir() and (path / "__init__.py").is_file()}
    return names


def test_every_third_party_import_is_a_declared_dependency() -> None:
    """No module under `src/` imports a distribution `pyproject.toml` does not ask for."""
    declared = _declared_distributions()
    roots = _imported_roots(SRC)
    installed = packages_distributions()

    assert "fastapi" in roots, (
        f"the walk of {SRC} found no `fastapi` import, so it has stopped seeing this project's "
        f"source — it found {sorted(roots)}. A check that inspects nothing passes over everything."
    )

    undeclared: list[str] = []
    unresolved: list[str] = []

    for root, files in sorted(roots.items()):
        distributions = installed.get(root, [])
        if not distributions:
            unresolved.append(f"  {root} — imported by {', '.join(files)}")
        elif not any(canonicalize_name(dist) in declared for dist in distributions):
            shipped_by = " / ".join(sorted(distributions))
            undeclared.append(f"  {root} (shipped by {shipped_by}) — imported by {', '.join(files)}")

    assert not undeclared, (
        f"{len(undeclared)} import root(s) under src/ resolve to a distribution this package does "
        "not declare, so they arrive only as long as something else happens to depend on them:\n"
        + "\n".join(undeclared)
        + "\n\nDeclare each in `[project.optional-dependencies] server` (or in `[project] "
        "dependencies` if a module under src/cassetta/cli/ imports it — that is a larger finding, "
        "because it means the client/server split shipped in 0.28.0 is wrong). A pin in "
        "`[tool.uv] constraint-dependencies` does not count: constraints bind resolution here and "
        "never reach the metadata an installing user resolves against."
    )

    assert not unresolved, (
        f"{len(unresolved)} import root(s) under src/ map to no installed distribution at all:\n"
        + "\n".join(unresolved)
        + "\n\nEither the name is misspelled, or it is imported and installed by nothing — the same "
        "defect as an undeclared dependency, met one step earlier. Skipping these is how the check "
        "above goes quiet about exactly the roots it most needs to see."
    )


def test_every_third_party_import_under_tests_is_declared() -> None:
    """No module under `tests/` imports a distribution `pyproject.toml` does not ask for.

    The same rule as the guard above with a wider root, and it is a separate test rather than a
    parameterisation so that a failure says which half of the tree is wrong. The two differ in two
    ways, and both are the sort of thing a later reader gets wrong by assuming symmetry.

    **`dev` counts here, and does not count there.** A root imported under `tests/` may be satisfied
    by *any* declared group, `dev` included — a test dependency is exactly what `dev` is for. A root
    imported under `src/` may not: `src/` is what ships, and a shipped module whose import is
    satisfied only by the development extra is broken for everyone who installs the package. The
    guard above does not express that distinction because every group is pooled there too; what
    saves it is that `src/` imports nothing `dev`-only today. If it ever does, *that* is the test to
    split, not this one.

    **A name supplied by a module inside `tests/` is not a distribution.** This tree contains
    `tests/observability/_obs_helpers.py`, imported by six files as a bare `_obs_helpers` because
    that directory's `conftest.py` puts itself on `sys.path` — the documented convention here, since
    this project has no `__init__.py` under `tests/`. Under `src/` the question never arises: every
    module lives inside the `cassetta` package and is reached through it. So `unresolved` means
    something different in the two trees, and the wider walk has to say so or it is red on a correct
    tree.

    That exclusion is asked **only after** a root fails to resolve to an installed distribution,
    which is the part that matters. A test module that shadowed a real distribution name — a
    `tests/httpx.py` — would still take the ordinary path and still have to be declared; were
    locality asked first, such a file would quietly excuse the very import it shadows.
    """
    declared = _declared_distributions()
    roots = _imported_roots(TESTS)
    installed = packages_distributions()
    local = _local_module_names(TESTS)

    # Non-vacuity, and it carries this guard on its own: extending the walk finds nothing today, so
    # there is no list of violations to prove the walk happened. `pytest` is imported by most files
    # here and is the root whose absence would mean the scan has stopped seeing the test tree.
    assert "pytest" in roots, (
        f"the walk of {TESTS} found no `pytest` import, so it has stopped seeing this project's "
        f"tests — it found {sorted(roots)}. A check that inspects nothing passes over everything."
    )

    undeclared: list[str] = []
    unresolved: list[str] = []

    for root, files in sorted(roots.items()):
        distributions = installed.get(root, [])
        if not distributions:
            if root in local:
                continue
            unresolved.append(f"  {root} — imported by {', '.join(files)}")
        elif not any(canonicalize_name(dist) in declared for dist in distributions):
            shipped_by = " / ".join(sorted(distributions))
            undeclared.append(f"  {root} (shipped by {shipped_by}) — imported by {', '.join(files)}")

    assert not undeclared, (
        f"{len(undeclared)} import root(s) under tests/ resolve to a distribution this package does "
        "not declare, so the suite passes only as long as something else happens to depend on "
        "them:\n" + "\n".join(undeclared) + "\n\nDeclare each in `[project.optional-dependencies] "
        "dev`. That is where a dependency of the tests belongs, and it is the decision this "
        "repository has already taken twice, for `pyyaml` and for `packaging` — the second of which "
        "was caught by a person reading a diff, one file away from this guard."
    )

    assert not unresolved, (
        f"{len(unresolved)} import root(s) under tests/ map to no installed distribution and to no "
        "module inside tests/ either:\n" + "\n".join(unresolved) + "\n\nEither the name is "
        "misspelled, or it is imported and installed by nothing. A local helper module is not this "
        f"case — those are recognised by name from {TESTS.name}/ itself."
    )
