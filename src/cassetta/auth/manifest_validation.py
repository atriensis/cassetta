"""Manifest-level validation (Brief 514 FR-001a).

Complements the per-path checks in :mod:`cassetta.path_validation` with
collection-level checks required by the upload flow:

- reserved ``meta.json`` sidecar name,
- duplicate entry detection,
- prefix-collision detection (no entry is a strict path prefix of another).

Rejections raise :class:`ValueError` with the stable
``invalid_manifest: reason=<reason>, name="<name>"`` prefix consumed by MCP
and REST surfaces.
"""

from __future__ import annotations

from typing import Any

from cassetta.path_validation import PathValidationError, validate_path

_RESERVED_NAMES = {"meta.json"}


def _fail(reason: str, name: str, **extra: str) -> ValueError:
    extras = "".join(f", {k}={v!r}" for k, v in extra.items())
    return ValueError(f"invalid_manifest: reason={reason}, name={name!r}{extras}")


def validate_path_for_manifest(name: str) -> str:
    """Run single-path validation; return a reason label on failure.

    Raises :class:`ValueError` with the structured prefix on rejection.
    Returns the input unchanged on success so callers can chain.
    """
    if not name:
        raise _fail("empty", name)
    if name.startswith("/"):
        raise _fail("absolute", name)
    try:
        validate_path(name)
    except PathValidationError as exc:
        text = str(exc)
        if "traversal" in text.lower() or ".." in text:
            raise _fail("traversal", name) from exc
        raise _fail("invalid_char", name) from exc
    return name


def validate_manifest(manifest: dict[str, Any] | Any) -> None:
    """Validate every entry in ``manifest["files"]`` and the collection as a whole.

    Raises :class:`ValueError` with the ``invalid_manifest:`` prefix on first
    failure. Returns ``None`` on success.
    """
    files = manifest.get("files", [])
    if not isinstance(files, list):
        raise ValueError("invalid_manifest: reason=malformed, name=''")

    seen: set[str] = set()
    for entry in files:
        name = str(entry.get("name", ""))
        validate_path_for_manifest(name)
        if name in _RESERVED_NAMES:
            raise _fail("reserved_name", name)
        if name in seen:
            raise _fail("duplicate", name)
        seen.add(name)

    # Prefix-collision: sort names, compare each adjacent pair.
    names = sorted(seen)
    for i in range(1, len(names)):
        prev, curr = names[i - 1], names[i]
        if curr.startswith(prev + "/"):
            raise _fail("prefix_collision", prev, conflicts_with=curr)
