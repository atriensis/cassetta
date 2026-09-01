"""Branch tests for CoreLimitsPolicy.evaluate_upload / evaluate_download (brief 513 US2)."""

from __future__ import annotations

import logging

import pytest

from cassetta.config import LimitsConfig
from cassetta.defaults.default_limits import CoreLimitsPolicy
from cassetta.protocols.identity import Identity
from cassetta.protocols.limits import ManifestFile, PolicyContext, UploadManifest


def _ctx() -> PolicyContext:
    return PolicyContext(identity=Identity(label="alice"))


def _manifest(*sizes: int) -> UploadManifest:
    files: list[ManifestFile] = [
        {"name": f"f{i}.bin", "size": s, "mime": None}
        for i, s in enumerate(sizes)
    ]
    return {"file_count": len(files), "files": files}


@pytest.mark.asyncio
async def test_file_count_rejection() -> None:
    p = CoreLimitsPolicy(LimitsConfig(per_bundle_file_count_max=2))
    decision = await p.evaluate_upload(_ctx(), _manifest(1, 1, 1))
    assert decision == {
        "error": "cap_exceeded",
        "constraint": "per_bundle_file_count_max",
        "limit": 2,
        "observed": 3,
    }


@pytest.mark.asyncio
async def test_per_file_rejection() -> None:
    p = CoreLimitsPolicy(LimitsConfig(per_file_max=10))
    decision = await p.evaluate_upload(_ctx(), _manifest(20))
    assert decision == {
        "error": "cap_exceeded",
        "constraint": "per_file_max",
        "limit": 10,
        "observed": 20,
    }


@pytest.mark.asyncio
async def test_total_rejection() -> None:
    p = CoreLimitsPolicy(LimitsConfig(per_bundle_total_max=100))
    decision = await p.evaluate_upload(_ctx(), _manifest(60, 60))
    assert decision == {
        "error": "cap_exceeded",
        "constraint": "per_bundle_total_max",
        "limit": 100,
        "observed": 120,
    }


@pytest.mark.asyncio
async def test_rejection_priority_file_count_first() -> None:
    # All three caps violated — file count must fire first
    p = CoreLimitsPolicy(
        LimitsConfig(
            per_file_max=5,
            per_bundle_total_max=10,
            per_bundle_file_count_max=1,
        )
    )
    decision = await p.evaluate_upload(_ctx(), _manifest(50, 50, 50))
    assert decision["constraint"] == "per_bundle_file_count_max"


@pytest.mark.asyncio
async def test_inline_happy_path() -> None:
    p = CoreLimitsPolicy(LimitsConfig(max_inline_size=100))
    decision = await p.evaluate_upload(_ctx(), _manifest(30, 20))
    assert decision == {"mode": "inline", "reason": None}


@pytest.mark.asyncio
async def test_batch_happy_path() -> None:
    p = CoreLimitsPolicy(LimitsConfig(max_inline_size=100))
    decision = await p.evaluate_upload(_ctx(), _manifest(60, 60))
    assert decision == {"mode": "batch", "reason": None}


@pytest.mark.asyncio
async def test_boundary_equal_is_inline() -> None:
    p = CoreLimitsPolicy(LimitsConfig(max_inline_size=100))
    decision = await p.evaluate_upload(_ctx(), _manifest(100))
    assert decision["mode"] == "inline"


@pytest.mark.asyncio
async def test_boundary_one_over_is_batch() -> None:
    p = CoreLimitsPolicy(LimitsConfig(max_inline_size=100))
    decision = await p.evaluate_upload(_ctx(), _manifest(101))
    assert decision["mode"] == "batch"


@pytest.mark.asyncio
async def test_max_inline_none_always_inline() -> None:
    p = CoreLimitsPolicy(LimitsConfig(max_inline_size=None))
    decision = await p.evaluate_upload(_ctx(), _manifest(10_000_000))
    assert decision["mode"] == "inline"


@pytest.mark.asyncio
async def test_unset_caps_default_file_count() -> None:
    p = CoreLimitsPolicy(LimitsConfig())
    # default per_bundle_file_count_max=25 — 26 files should fail
    decision = await p.evaluate_upload(_ctx(), _manifest(*([1] * 26)))
    assert decision["constraint"] == "per_bundle_file_count_max"


@pytest.mark.asyncio
async def test_empty_files_returns_inline() -> None:
    p = CoreLimitsPolicy(LimitsConfig())
    decision = await p.evaluate_upload(_ctx(), {"file_count": 0, "files": []})
    assert decision["mode"] == "inline"


@pytest.mark.asyncio
async def test_download_inline_and_reference() -> None:
    p = CoreLimitsPolicy(LimitsConfig(max_inline_size=100))
    inline = await p.evaluate_download(
        _ctx(), {"file_count": 1, "total_size": 50},
    )
    ref = await p.evaluate_download(
        _ctx(), {"file_count": 1, "total_size": 500},
    )
    assert inline["mode"] == "inline"
    assert ref["mode"] == "reference"


@pytest.mark.asyncio
async def test_download_unset_always_inline() -> None:
    p = CoreLimitsPolicy(LimitsConfig(max_inline_size=None))
    decision = await p.evaluate_download(
        _ctx(), {"file_count": 99, "total_size": 10**9},
    )
    assert decision["mode"] == "inline"


@pytest.mark.asyncio
async def test_download_emits_policy_log() -> None:
    """Verifies FR-029 — evaluate_download emits a policy.download_decision log."""
    p = CoreLimitsPolicy(LimitsConfig(max_inline_size=100))
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    cassetta_logger = logging.getLogger("cassetta")
    handler = _Capture(level=logging.DEBUG)
    # Brief 539: pin the logger level so the DEBUG ``policy.download_decision``
    # record is emitted regardless of ordering (in isolation the level defaults
    # to WARNING and would filter it); restore at teardown to avoid leaking.
    prior_level = cassetta_logger.level
    cassetta_logger.addHandler(handler)
    cassetta_logger.setLevel(logging.DEBUG)
    try:
        await p.evaluate_download(_ctx(), {"file_count": 2, "total_size": 50})
    finally:
        cassetta_logger.removeHandler(handler)
        cassetta_logger.setLevel(prior_level)

    assert any(r.getMessage() == "policy.download_decision" for r in captured)
