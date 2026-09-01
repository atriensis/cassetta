"""FR-015 / SC-002 — zero vendor-specific imports in ``src/``.

Grep-style assertion: walks every .py under ``src/`` and fails
if any of ``azure``, ``boto3``, ``google.cloud`` tokens appear. Test
file itself is excluded (self-reference).
"""

from __future__ import annotations

from pathlib import Path

CORE_SRC = Path(__file__).resolve().parents[1] / "src"
_FORBIDDEN = ("azure", "boto3", "google.cloud")


def test_core_src_has_no_vendor_imports() -> None:
    assert CORE_SRC.is_dir(), f"src/ not found at {CORE_SRC}"
    offenders: list[tuple[str, int, str]] = []
    for py in CORE_SRC.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            for token in _FORBIDDEN:
                if token in stripped:
                    offenders.append((str(py), lineno, stripped))
    assert not offenders, (
        "Vendor imports found in src/:\n" + "\n".join(
            f"  {p}:{lineno}  {line}" for p, lineno, line in offenders
        )
    )
