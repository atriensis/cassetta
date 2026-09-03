"""Tests for manifest validation.

Covers the rejection catalogue:
- ``empty``, ``absolute``, ``traversal``, ``invalid_char`` — per-entry path checks
- ``reserved_name`` — ``meta.json`` is the sidecar
- ``duplicate`` — two entries share a ``name``
- ``prefix_collision`` — one entry is a strict path-prefix of another

Positive cases: nested paths, sibling paths, and names that share a
non-component prefix without colliding (e.g. ``a`` + ``b/a``).
"""

from __future__ import annotations

import pytest

from cassetta.auth import manifest_validation as mv


def _manifest(*names: str) -> dict[str, object]:
    return {
        "file_count": len(names),
        "files": [{"name": n, "size": 1, "mime": None} for n in names],
    }


def test_accepts_simple_nested_paths() -> None:
    mv.validate_manifest(_manifest("src/main.py", "docs/index.md", "README.md"))


def test_accepts_sibling_paths_with_shared_prefix_fragment() -> None:
    # "a" and "b/a" are not a prefix of each other despite sharing a name "a".
    mv.validate_manifest(_manifest("a", "b/a", "c"))


def test_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match=r"invalid_manifest: reason=empty"):
        mv.validate_manifest(_manifest(""))


def test_rejects_absolute_name() -> None:
    with pytest.raises(ValueError, match=r"invalid_manifest: reason=absolute"):
        mv.validate_manifest(_manifest("/etc/passwd"))


def test_rejects_traversal() -> None:
    with pytest.raises(ValueError, match=r"invalid_manifest: reason=traversal"):
        mv.validate_manifest(_manifest("../escape"))


def test_rejects_invalid_char() -> None:
    with pytest.raises(ValueError, match=r"invalid_manifest: reason=invalid_char"):
        mv.validate_manifest(_manifest("x<y"))


def test_rejects_reserved_name_meta_json() -> None:
    with pytest.raises(ValueError, match=r"invalid_manifest: reason=reserved_name"):
        mv.validate_manifest(_manifest("meta.json"))


def test_rejects_duplicate_entry() -> None:
    with pytest.raises(ValueError, match=r"invalid_manifest: reason=duplicate"):
        mv.validate_manifest(_manifest("foo", "foo"))


def test_rejects_prefix_collision() -> None:
    with pytest.raises(ValueError, match=r"invalid_manifest: reason=prefix_collision"):
        mv.validate_manifest(_manifest("src", "src/main.py"))


def test_error_message_includes_name() -> None:
    with pytest.raises(ValueError) as excinfo:
        mv.validate_manifest(_manifest("x<y"))
    # Message carries `name='x<y'` or `name="x<y"` depending on repr choice.
    assert "x<y" in str(excinfo.value)
    assert str(excinfo.value).startswith("invalid_manifest: reason=invalid_char")
