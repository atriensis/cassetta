"""T040 — belt-and-braces: no jti store exists in core (Brief 514 FR-014).

Scans ``src/cassetta/**.py`` for references to a jti blacklist /
ledger / store outside the single allowed location (``auth/jwt_tokens.py``).
Fails the build if anyone introduces a server-side jti-keyed state.
"""

from __future__ import annotations

import re
from pathlib import Path

_CORE_SRC = Path(__file__).resolve().parents[3] / "core" / "src" / "cassetta"
_ALLOWED = {_CORE_SRC / "auth" / "jwt_tokens.py"}

# Any of these patterns suggests a jti-keyed server-side ledger.
_BAD_PATTERNS = [
    re.compile(r"jti_store", re.IGNORECASE),
    re.compile(r"jti_blacklist", re.IGNORECASE),
    re.compile(r"revocation[_ ]store", re.IGNORECASE),
    re.compile(r"jti_ledger", re.IGNORECASE),
    # Explicit "set of consumed jtis" shapes.
    re.compile(r"consumed_jtis?", re.IGNORECASE),
]


def test_no_jti_store_in_core() -> None:
    offenders: list[tuple[Path, int, str, str]] = []
    for p in _CORE_SRC.rglob("*.py"):
        if p in _ALLOWED:
            continue
        # Skip tests and caches.
        if "__pycache__" in p.parts:
            continue
        text = p.read_text()
        for i, line in enumerate(text.splitlines(), 1):
            for pattern in _BAD_PATTERNS:
                if pattern.search(line):
                    offenders.append((p.relative_to(_CORE_SRC), i, pattern.pattern, line.strip()))
    assert not offenders, "Found jti-store references outside auth/jwt_tokens.py:\n" + "\n".join(
        f"  {p}:{i} — /{pat}/ — {line}" for p, i, pat, line in offenders
    )
