"""FR-014 — Layer 2 code annotates ``ClaimStorage`` (Protocol), never
concrete classes like ``FilesystemClaimStorage`` or ``BlobClaimStorage``.

AST-walk-based check: each target file is parsed, and every
annotation where the parameter/attribute name matches ``claim_store``
(or ``_claim_store``) is inspected. The annotation's leftmost name
MUST be ``ClaimStorage`` — never ``ClaimStore`` (removed),
``FilesystemClaimStorage``, or ``BlobClaimStorage``.
"""

from __future__ import annotations

import ast
from pathlib import Path

CORE_SRC = Path(__file__).resolve().parents[1] / "src" / "cassetta"

_TARGETS = [
    CORE_SRC / "app.py",
    CORE_SRC / "downloads.py",
    CORE_SRC / "gc.py",
    CORE_SRC / "mcp_server.py",
    CORE_SRC / "routes" / "download.py",
    CORE_SRC / "routes" / "inbox.py",
]

_CLAIM_STORE_NAMES = {"claim_store", "_claim_store"}
_FORBIDDEN_ANNOTATIONS = {
    "ClaimStore",
    "FilesystemClaimStorage",
    "BlobClaimStorage",
}


def _annotation_root_names(node: ast.expr | None) -> set[str]:
    """Return the set of identifier names referenced in an annotation tree."""
    if node is None:
        return set()
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            names.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            names.add(sub.attr)
    return names


def _offenders_in_file(path: Path) -> list[tuple[str, int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        # Annotated assignments (module-level / class-level):
        #   _claim_store: ClaimStorage | None = None
        if isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id in _CLAIM_STORE_NAMES:
                names = _annotation_root_names(node.annotation)
                bad = names & _FORBIDDEN_ANNOTATIONS
                if bad:
                    offenders.append((
                        str(path), node.lineno, ast.unparse(node.annotation),
                    ))
        # Function parameters + return types: walk every Arg.
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in list(node.args.args) + list(node.args.kwonlyargs):
                if arg.arg in _CLAIM_STORE_NAMES and arg.annotation is not None:
                    names = _annotation_root_names(arg.annotation)
                    bad = names & _FORBIDDEN_ANNOTATIONS
                    if bad:
                        offenders.append((
                            str(path), arg.lineno,
                            ast.unparse(arg.annotation),
                        ))
    return offenders


def test_layer2_uses_only_protocol_annotations() -> None:
    all_offenders: list[tuple[str, int, str]] = []
    for target in _TARGETS:
        assert target.is_file(), f"target missing: {target}"
        all_offenders.extend(_offenders_in_file(target))
    assert not all_offenders, (
        "Layer 2 files must annotate `claim_store` with the Protocol "
        "type `ClaimStorage`, not a concrete class:\n"
        + "\n".join(f"  {p}:{lineno}  {ann}" for p, lineno, ann in all_offenders)
    )


# Brief 535 Fix 5 — Layer 2 must never use ``try/except TypeError`` as a
# backend-signature-divergence fallback. The fix unifies
# ``KeyStoreProtocol.create_key`` to accept ``user_id`` keyword-only on
# every backend, so the previous brittle catch becomes unnecessary; this
# lock prevents reintroduction.

_KEYS_ROUTE = CORE_SRC / "routes" / "keys.py"


def test_no_typeerror_fallback_in_keys_route() -> None:
    """``routes/keys.py`` must not catch ``TypeError`` anywhere."""
    assert _KEYS_ROUTE.is_file(), f"missing: {_KEYS_ROUTE}"
    tree = ast.parse(_KEYS_ROUTE.read_text(encoding="utf-8"))

    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            exc_type = handler.type
            if exc_type is None:
                continue
            if isinstance(exc_type, ast.Name) and exc_type.id == "TypeError":
                offenders.append((handler.lineno, "TypeError"))
            elif isinstance(exc_type, ast.Tuple):
                for elt in exc_type.elts:
                    if isinstance(elt, ast.Name) and elt.id == "TypeError":
                        offenders.append((handler.lineno, "TypeError"))
    assert not offenders, (
        "routes/keys.py must not catch TypeError (Brief 535 Fix 5). "
        "Layer 2 calls the unified KeyStoreProtocol.create_key signature "
        "unconditionally — no exception-based backend dispatch.\n"
        f"Offending handlers: {offenders}"
    )
