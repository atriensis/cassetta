"""Unit tests for ``cassetta.limits.check_manifest_against_limits``.

Covers the invariants from contracts/check-manifest-against-limits.md:
determinism, ordering of checks, null handling, purity (zero I/O, zero
logging), and re-exported public surface.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
from typing import Any
from unittest.mock import patch

from cassetta.limits import (
    LimitsAdvertisement,
    ManifestFile,
    UploadDecision,
    UploadManifest,
    check_manifest_against_limits,
)
from cassetta.protocols import limits as _proto_limits


def _manifest(files: list[tuple[str, int]]) -> UploadManifest:
    entries: list[ManifestFile] = [{"name": name, "size": size, "mime": None} for name, size in files]
    return {"file_count": len(entries), "files": entries}


def _all_null() -> LimitsAdvertisement:
    return {
        "per_file_max": None,
        "per_bundle_total_max": None,
        "per_bundle_file_count_max": None,
        "max_inline_size": None,
    }


# ---------------------------------------------------------------------------
# Re-exported public surface
# ---------------------------------------------------------------------------


def test_reexported_typeddicts_are_identical_to_protocol_module() -> None:
    assert UploadManifest is _proto_limits.UploadManifest
    assert ManifestFile is _proto_limits.ManifestFile
    assert LimitsAdvertisement is _proto_limits.LimitsAdvertisement
    assert UploadDecision is _proto_limits.UploadDecision


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def _fingerprint_1000_runs() -> str:
    rng = random.Random(42)
    acc: list[str] = []
    for _ in range(1_000):
        file_count = rng.randint(1, 30)
        files = [(f"f{i}.bin", rng.randint(0, 200_000)) for i in range(file_count)]
        limits: LimitsAdvertisement = {
            "per_file_max": rng.choice([None, 50_000, 100_000]),
            "per_bundle_total_max": rng.choice([None, 500_000]),
            "per_bundle_file_count_max": rng.choice([None, 10, 25]),
            "max_inline_size": rng.choice([None, 100_000]),
        }
        decision = check_manifest_against_limits(_manifest(files), limits)
        acc.append(json.dumps(decision, sort_keys=True))
    return hashlib.sha256("\n".join(acc).encode()).hexdigest()


def test_determinism_across_invocations() -> None:
    first = _fingerprint_1000_runs()
    second = _fingerprint_1000_runs()
    assert first == second


# ---------------------------------------------------------------------------
# Ordering invariants (contracts/check-manifest-against-limits.md §Ordering)
# ---------------------------------------------------------------------------


def test_ordering_file_count_beats_per_file_max() -> None:
    # 3 files violating per_bundle_file_count_max = 2; also one file
    # violating per_file_max = 10. The count check must fire first.
    files = [("a", 100), ("b", 5), ("c", 5)]
    limits: LimitsAdvertisement = {
        "per_file_max": 10,
        "per_bundle_total_max": None,
        "per_bundle_file_count_max": 2,
        "max_inline_size": None,
    }
    decision = check_manifest_against_limits(_manifest(files), limits)
    assert decision["error"] == "cap_exceeded"
    assert decision["constraint"] == "per_bundle_file_count_max"
    assert decision["limit"] == 2
    assert decision["observed"] == 3


def test_ordering_per_file_max_beats_per_bundle_total_max() -> None:
    # Both fire: one file size 1000 (per_file_max=500) and total 1010
    # (per_bundle_total_max=900). Per-file must win.
    files = [("a", 1000), ("b", 10)]
    limits: LimitsAdvertisement = {
        "per_file_max": 500,
        "per_bundle_total_max": 900,
        "per_bundle_file_count_max": None,
        "max_inline_size": None,
    }
    decision = check_manifest_against_limits(_manifest(files), limits)
    assert decision["error"] == "cap_exceeded"
    assert decision["constraint"] == "per_file_max"
    assert decision["limit"] == 500
    assert decision["observed"] == 1000


def test_per_file_max_triggers_on_first_violating_file_not_largest() -> None:
    # First file is the violator; second file is larger but must not
    # shadow the first.
    files = [("first.bin", 600), ("bigger.bin", 800)]
    limits: LimitsAdvertisement = {
        "per_file_max": 500,
        "per_bundle_total_max": None,
        "per_bundle_file_count_max": None,
        "max_inline_size": None,
    }
    decision = check_manifest_against_limits(_manifest(files), limits)
    assert decision["error"] == "cap_exceeded"
    assert decision["constraint"] == "per_file_max"
    assert decision["observed"] == 600  # first, not 800


def test_ordering_per_bundle_total_max_beats_max_inline_size() -> None:
    files = [("a", 500), ("b", 500)]
    limits: LimitsAdvertisement = {
        "per_file_max": None,
        "per_bundle_total_max": 600,
        "per_bundle_file_count_max": None,
        "max_inline_size": 100,
    }
    decision = check_manifest_against_limits(_manifest(files), limits)
    assert decision["error"] == "cap_exceeded"
    assert decision["constraint"] == "per_bundle_total_max"
    assert decision["limit"] == 600
    assert decision["observed"] == 1000


# ---------------------------------------------------------------------------
# Null handling
# ---------------------------------------------------------------------------


def test_all_null_limits_always_inline() -> None:
    limits = _all_null()
    for files in (
        [("a", 1)],
        [("a", 10**9)],
        [(f"f{i}", 10**6) for i in range(100)],
    ):
        decision = check_manifest_against_limits(_manifest(files), limits)
        assert decision == {"mode": "inline", "reason": None}


def test_null_per_file_max_skips_check() -> None:
    limits: LimitsAdvertisement = {
        "per_file_max": None,
        "per_bundle_total_max": None,
        "per_bundle_file_count_max": None,
        "max_inline_size": None,
    }
    files = [("huge.bin", 10**12)]
    decision = check_manifest_against_limits(_manifest(files), limits)
    assert decision == {"mode": "inline", "reason": None}


def test_null_max_inline_size_always_inline() -> None:
    limits: LimitsAdvertisement = {
        "per_file_max": None,
        "per_bundle_total_max": None,
        "per_bundle_file_count_max": None,
        "max_inline_size": None,
    }
    files = [("f", 10**9)]
    decision = check_manifest_against_limits(_manifest(files), limits)
    assert decision == {"mode": "inline", "reason": None}


# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------


def test_inline_when_total_at_inline_threshold() -> None:
    limits: LimitsAdvertisement = {
        "per_file_max": None,
        "per_bundle_total_max": None,
        "per_bundle_file_count_max": None,
        "max_inline_size": 1000,
    }
    decision = check_manifest_against_limits(
        _manifest([("a", 500), ("b", 500)]),
        limits,
    )
    assert decision == {"mode": "inline", "reason": None}


def test_batch_when_total_exceeds_inline_threshold() -> None:
    limits: LimitsAdvertisement = {
        "per_file_max": None,
        "per_bundle_total_max": None,
        "per_bundle_file_count_max": None,
        "max_inline_size": 1000,
    }
    decision = check_manifest_against_limits(
        _manifest([("a", 600), ("b", 600)]),
        limits,
    )
    assert decision == {"mode": "batch", "reason": None}


# ---------------------------------------------------------------------------
# Purity: no logging, no I/O
# ---------------------------------------------------------------------------


def test_no_logging_during_1000_invocations() -> None:
    limits = _all_null()
    files = [("f", 10)]
    manifest = _manifest(files)
    with patch.object(logging.Logger, "log") as log_mock:
        for _ in range(1_000):
            check_manifest_against_limits(manifest, limits)
    assert log_mock.call_count == 0


def test_no_file_io(monkeypatch: Any) -> None:
    open_calls: list[Any] = []
    original_open = __builtins__["open"] if isinstance(__builtins__, dict) else __builtins__.open

    def spy(*args: Any, **kwargs: Any) -> Any:
        open_calls.append(args)
        return original_open(*args, **kwargs)

    monkeypatch.setattr("builtins.open", spy)
    limits = _all_null()
    for _ in range(100):
        check_manifest_against_limits(_manifest([("f", 10)]), limits)
    assert open_calls == []


# ---------------------------------------------------------------------------
# Inputs not mutated
# ---------------------------------------------------------------------------


def test_inputs_are_not_mutated() -> None:
    limits: LimitsAdvertisement = {
        "per_file_max": 100,
        "per_bundle_total_max": None,
        "per_bundle_file_count_max": None,
        "max_inline_size": None,
    }
    manifest = _manifest([("a", 50)])
    before_manifest = json.dumps(manifest, sort_keys=True)
    before_limits = json.dumps(limits, sort_keys=True)
    check_manifest_against_limits(manifest, limits)
    assert json.dumps(manifest, sort_keys=True) == before_manifest
    assert json.dumps(limits, sort_keys=True) == before_limits
