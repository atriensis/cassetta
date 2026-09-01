"""Reserved-future placeholder: ``jwt_aud_mismatch`` is in the Reason
enum but no callsite emits it (Brief 529 R3).

If a future brief introduces ``aud`` claim validation, both assertions
in this file MUST be updated alongside the new emission code.
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import cassetta as _cassetta
from cassetta.auth.observability import Reason


def test_jwt_aud_mismatch_is_in_reason_enum() -> None:
    """mypy contract: the Literal type advertises the reason value."""
    assert "jwt_aud_mismatch" in get_args(Reason)


def test_no_core_callsite_currently_emits_jwt_aud_mismatch() -> None:
    """No source file under ``src/cassetta/`` mentions the reason.

    The helper module declares the type alias and is excluded. If any
    other module references the literal string ``"jwt_aud_mismatch"``,
    that brief must update R3 documentation and adjust this test.
    """
    cassetta_init = _cassetta.__file__
    assert cassetta_init is not None, "cassetta package missing __file__"
    src_root = Path(cassetta_init).parent
    matches: list[str] = []
    for path in src_root.rglob("*.py"):
        if path.name == "observability.py":
            continue
        text = path.read_text(encoding="utf-8")
        if '"jwt_aud_mismatch"' in text or "'jwt_aud_mismatch'" in text:
            matches.append(str(path.relative_to(src_root)))
    assert matches == [], f"Brief 529 R3 reserves jwt_aud_mismatch as a future value. Unexpected callsite(s): {matches}"
