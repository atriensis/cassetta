"""Audit regression test — Brief 533 SC-012 / FR-064.

After FR-063 backfill lands, grepping ``metrics\\.(increment|gauge)\\(`` over
``src/cassetta/`` MUST find only the single call inside ``safe_emit``
itself in ``structured_log.py`` plus explicitly-fenced exceptions. Any
future code review that adds a raw ``metrics.*`` call fails this test
before merging.
"""

from __future__ import annotations

import re
from pathlib import Path

# Files allowed to contain raw ``metrics.<step>(`` calls. Each entry is a
# justified exception per contract C-SAFEEMIT-006:
#
# - ``structured_log.py`` hosts the ``safe_emit`` body itself.
# - ``auth/observability.py::emit_auth_failure`` is the brief-529
#   specialisation of the same independent-best-effort pattern. Its
#   PUBLIC fallback string ``"auth observability emission failed"`` is
#   regression-locked by SC-011 / FR-067 (the complete brief-529 test
#   suite MUST pass UNCHANGED after the brief-533 refactor), so the helper
#   cannot delegate to ``safe_emit`` whose fallback strings differ.
_ALLOWED_PATHS = {
    "structured_log.py",
    "auth/observability.py",
}

_PATTERN = re.compile(r"metrics\.(increment|gauge)\(")


def _core_src_root() -> Path:
    here = Path(__file__).resolve()
    # tests/observability/test_audit_no_raw_metric_calls.py
    # → src/cassetta/
    return here.parents[2] / "src" / "cassetta"


def test_no_raw_metric_calls_outside_safe_emit() -> None:
    root = _core_src_root()
    offenders: list[str] = []
    for py in root.rglob("*.py"):
        rel = str(py.relative_to(root))
        # Posix-style separator for the comparison.
        rel_posix = rel.replace("\\", "/")
        if rel_posix in _ALLOWED_PATHS or py.name in _ALLOWED_PATHS:
            continue
        text = py.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _PATTERN.search(line):
                offenders.append(f"{rel_posix}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Raw metrics.<increment|gauge>(...) call sites found outside "
        "safe_emit. Route them through cassetta.structured_log.safe_emit "
        "per FR-063:\n" + "\n".join(offenders)
    )
