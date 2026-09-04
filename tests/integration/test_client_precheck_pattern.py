"""Acceptance — the documented client pre-check pattern.

Simulates the client-side flow from ``quickstart.md`` §5:

1. Client has a cached capabilities response (represented here as a
   hand-built ``limits`` dict — no server round-trip needed to get
   this for the test).
2. Client constructs a manifest.
3. Client calls ``cassetta.limits.check_manifest_against_limits``.
4. If the decision has an ``error`` key, client raises a local error
   and **never** calls ``cassetta_send_init``.

Covers the zero-server-round-trip path for oversized payloads and the
null-cap-means-no-limit rule. No server process is required; the
``send_init`` spy proves the rejection short-circuits the network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from cassetta.limits import (
    LimitsAdvertisement,
    ManifestFile,
    UploadManifest,
    check_manifest_against_limits,
)


class _ClientPrecheckError(Exception):
    """Raised by the client's control flow when the helper rejects."""

    def __init__(self, decision: dict[str, Any]) -> None:
        super().__init__(
            f"{decision['error']}: {decision['constraint']} observed={decision['observed']} limit={decision['limit']}"
        )
        self.decision = decision


def _send_manifest(
    manifest: UploadManifest,
    *,
    limits: LimitsAdvertisement,
    send_init: Any,
) -> Any:
    """Documented client flow — the exact pattern published in ``CLIENT_SETUP.md``.

    Calls the shared helper first; on rejection, raises locally and
    never reaches ``send_init``. On acceptance, invokes ``send_init``
    with the manifest.
    """
    decision = check_manifest_against_limits(manifest, limits)
    if "error" in decision:
        raise _ClientPrecheckError(dict(decision))
    return send_init(manifest)


def _cached_caps_with(per_file_max: int | None = 1000) -> LimitsAdvertisement:
    return {
        "per_file_max": per_file_max,
        "per_bundle_total_max": None,
        "per_bundle_file_count_max": 25,
        "max_inline_size": 102400,
    }


def _manifest(files: list[tuple[str, int]]) -> UploadManifest:
    entries: list[ManifestFile] = [{"name": name, "size": size, "mime": None} for name, size in files]
    return {"file_count": len(entries), "files": entries}


def test_oversized_payload_fails_before_send_init() -> None:
    """Client rejects locally, zero network calls."""
    send_init = MagicMock(name="send_init")
    caps = _cached_caps_with(per_file_max=1000)
    manifest = _manifest([("big.bin", 2000)])

    with pytest.raises(_ClientPrecheckError) as excinfo:
        _send_manifest(manifest, limits=caps, send_init=send_init)

    assert send_init.call_count == 0
    assert excinfo.value.decision == {
        "error": "cap_exceeded",
        "constraint": "per_file_max",
        "limit": 1000,
        "observed": 2000,
    }


def test_acceptable_payload_reaches_send_init() -> None:
    """Sanity: when the helper accepts, the client does call send_init."""
    send_init = MagicMock(name="send_init", return_value={"token": "t"})
    caps = _cached_caps_with(per_file_max=1000)
    manifest = _manifest([("ok.bin", 500)])

    _send_manifest(manifest, limits=caps, send_init=send_init)

    send_init.assert_called_once_with(manifest)


def test_null_cap_means_no_limit_on_that_axis() -> None:
    """A client cannot be stricter than the server on a null axis."""
    send_init = MagicMock(name="send_init", return_value={"token": "t"})
    caps: LimitsAdvertisement = {
        "per_file_max": None,  # ← no per-file limit
        "per_bundle_total_max": None,
        "per_bundle_file_count_max": None,
        "max_inline_size": None,
    }
    # 10 GiB per-file manifest — any sane server would reject, but the
    # cached caps say "no limit on this axis", so the client proceeds.
    manifest = _manifest([("huge.bin", 10 * 1024**3)])

    _send_manifest(manifest, limits=caps, send_init=send_init)

    send_init.assert_called_once_with(manifest)


def test_file_count_cap_rejects_before_send_init() -> None:
    send_init = MagicMock(name="send_init")
    caps: LimitsAdvertisement = {
        "per_file_max": None,
        "per_bundle_total_max": None,
        "per_bundle_file_count_max": 2,
        "max_inline_size": None,
    }
    manifest = _manifest([("a", 1), ("b", 1), ("c", 1)])

    with pytest.raises(_ClientPrecheckError) as excinfo:
        _send_manifest(manifest, limits=caps, send_init=send_init)

    assert send_init.call_count == 0
    assert excinfo.value.decision["constraint"] == "per_bundle_file_count_max"
    assert excinfo.value.decision["observed"] == 3
    assert excinfo.value.decision["limit"] == 2
