"""Brief 535 Fix 6 — every pydantic model in ``src/cassetta/models.py``
must be referenced by at least one production code path.

Brief 535 deletes 8 dead models and wires 4 routes / error handlers
with ``response_model=`` declarations. This regression test scans
``models.py`` for ``ClassDef`` names and asserts each appears in at
least one other file under ``src/cassetta/``. The check protects
against future drift: if someone adds a model without wiring it, the
test fails with the orphan's name.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_CORE_SRC = Path(__file__).resolve().parents[1] / "src" / "cassetta"
_MODELS_FILE = _CORE_SRC / "models.py"


def _model_class_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]


def _production_files() -> list[Path]:
    """All ``.py`` files under ``src/cassetta/`` except ``models.py``
    and ``__pycache__`` artefacts."""
    return [p for p in _CORE_SRC.rglob("*.py") if p != _MODELS_FILE and "__pycache__" not in p.parts]


def _intra_model_edges(path: Path, names: list[str]) -> dict[str, set[str]]:
    """For each model class in ``path``, the other model names it references in
    its body (e.g. a nested ``files: list[SendManifestFile]`` annotation)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nameset = set(names)
    edges: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in nameset:
            edges[node.name] = {
                child.id
                for child in ast.walk(node)
                if isinstance(child, ast.Name) and child.id in nameset and child.id != node.name
            }
    return edges


def test_no_orphan_models_in_core_models_py() -> None:
    """Every model defined in ``models.py`` is referenced at least once
    in another ``src/cassetta/`` file."""
    assert _MODELS_FILE.is_file(), f"missing: {_MODELS_FILE}"
    names = _model_class_names(_MODELS_FILE)
    assert names, "models.py exposes zero class definitions — unexpected"

    files = _production_files()
    references: dict[str, list[str]] = {n: [] for n in names}
    # ``\b<Name>\b`` so e.g. ``BundlePathConflict`` does not falsely
    # match ``BundlePathConflictError``.
    patterns = {name: re.compile(rf"\b{re.escape(name)}\b") for name in names}
    for f in files:
        text = f.read_text(encoding="utf-8")
        for name, pattern in patterns.items():
            if pattern.search(text):
                references[name].append(str(f.relative_to(_CORE_SRC)))

    # A model is "live" if referenced in production directly, OR reachable from
    # a production-referenced model through nesting inside models.py (e.g.
    # SendManifest.files: list[SendManifestFile]). Transitive reachability — added
    # with Brief 541's first nested model pair — keeps the guard accurate without
    # weakening it: a model reachable only from other dead models stays an orphan.
    live = {n for n, refs in references.items() if refs}
    edges = _intra_model_edges(_MODELS_FILE, names)
    changed = True
    while changed:
        changed = False
        for model in list(live):
            for ref in edges.get(model, set()):
                if ref not in live:
                    live.add(ref)
                    changed = True

    orphans = [n for n in names if n not in live]
    assert not orphans, (
        "Orphan pydantic models in models.py (Brief 535 Fix 6 "
        "deletes models with no production use, or wires them as "
        "response_model= on a route):\n  " + "\n  ".join(orphans)
    )
