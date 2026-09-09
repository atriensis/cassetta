"""Layer 2 MUST NOT name any of the nine concrete Layer 3
default-implementation classes.

Walks the full Layer 2 surface: every ``src/cassetta/**/*.py`` except Layer 3
construction homes (``defaults/factory.py``, ``defaults/default_*.py``,
``backends/**``, ``protocols/**``, ``**/__init__.py``).

The exclusions are the rule, not holes in it: every excluded path is a place
where naming a concrete class is the job. A factory constructs them, a
``default_*`` module *is* one, ``backends/**`` is Layer 3 itself, ``protocols/**``
declares what they implement, and an ``__init__.py`` re-exports them. Everything
else is Layer 2, where a concrete name is the defect this locks out — an
annotation or import that binds product logic to one backend.
"""

from __future__ import annotations

import ast
from pathlib import Path

CORE_SRC = Path(__file__).resolve().parents[1] / "src" / "cassetta"

_FORBIDDEN: set[str] = {
    "FilesystemBackend",
    "FileKeyStore",
    "DefaultIdentityProvider",
    "DefaultAccessPolicy",
    "DefaultAliasResolver",
    "CoreLimitsPolicy",
    "DefaultMetricsProvider",
    "CoreReferenceTransport",
    "FilesystemClaimStorage",
}


def _is_excluded(path: Path) -> bool:
    """Return True for Layer 3 construction homes and package markers."""
    if path.name == "__init__.py":
        return True
    parts = path.relative_to(CORE_SRC).parts
    if not parts:
        return False
    if parts[0] in {"backends", "protocols"}:
        return True
    if parts[0] == "defaults":
        if path.name == "factory.py":
            return True
        if path.name.startswith("default_"):
            return True
    return False


def _walk_set() -> list[Path]:
    return sorted(p for p in CORE_SRC.rglob("*.py") if not _is_excluded(p))


def test_layer2_has_no_concrete_default_names() -> None:
    assert CORE_SRC.is_dir(), f"target missing: {CORE_SRC}"

    offenders: list[tuple[Path, str, int]] = []
    for path in _walk_set():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in _FORBIDDEN:
                offenders.append((path, node.id, node.lineno))
            elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN:
                offenders.append((path, node.attr, node.lineno))

    assert not offenders, (
        "Layer 2 files MUST NOT name concrete Layer 3 default classes. "
        "Move construction to "
        "cassetta.defaults.factory.build_core_defaults:\n"
        + "\n".join(f"  {path}:{lineno}  {name}" for path, name, lineno in offenders)
    )
