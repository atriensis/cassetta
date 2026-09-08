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


def _imported_roots() -> dict[str, list[str]]:
    """Third-party import roots under `src/`, mapped to the files that import them.

    `ast.walk` rather than a scan of module-level statements: an import inside a function, and an
    import inside `if TYPE_CHECKING:`, both name a distribution someone has to supply. The guarded
    one is not hypothetical here — `rate_limit/limiter.py` has one, and `mypy` resolves it for real.

    `ast.parse` is deliberately not wrapped: a file under `src/` that does not parse is a defect to
    surface, not to route around.
    """
    roots: dict[str, list[str]] = defaultdict(list)
    for path in sorted(SRC.rglob("*.py")):
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


def test_every_third_party_import_is_a_declared_dependency() -> None:
    """No module under `src/` imports a distribution `pyproject.toml` does not ask for."""
    declared = _declared_distributions()
    roots = _imported_roots()
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
