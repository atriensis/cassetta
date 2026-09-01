"""Shape tests for the LimitsPolicy Protocol (brief 513)."""

from __future__ import annotations

import dataclasses

import pytest

from cassetta.protocols.identity import Identity
from cassetta.protocols.limits import (
    DownloadDecision,
    DownloadEntry,
    LimitsAdvertisement,
    LimitsPolicy,
    PolicyContext,
    TTLSettings,
    UploadDecision,
    UploadManifest,
)


def test_policy_context_is_frozen_dataclass() -> None:
    ctx = PolicyContext(identity=Identity(label="x"))
    assert ctx.identity.label == "x"
    assert dataclasses.is_dataclass(ctx)
    assert dataclasses.fields(ctx)[0].name == "identity"
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.identity = Identity(label="y")  # type: ignore[misc]


def test_limits_policy_is_runtime_checkable() -> None:
    # Every Protocol decorated with @runtime_checkable stores a marker.
    # The cleanest check is that isinstance succeeds for a minimal stub.
    class StubPolicy:
        kind = "test"

        async def evaluate_upload(
            self,
            ctx: PolicyContext,
            manifest: UploadManifest,
        ) -> UploadDecision:
            return {"mode": "inline", "reason": None}

        async def evaluate_download(
            self,
            ctx: PolicyContext,
            entry: DownloadEntry,
        ) -> DownloadDecision:
            return {"mode": "inline", "reason": None}

        def advertise_limits(self, ctx: PolicyContext) -> LimitsAdvertisement:
            return {
                "per_file_max": None,
                "per_bundle_total_max": None,
                "per_bundle_file_count_max": None,
                "max_inline_size": None,
            }

        def ttls(self, ctx: PolicyContext) -> TTLSettings:
            return {
                "upload_token_ttl": 1,
                "download_claim_ttl": 1,
                "passive_gc_min_age": 1,
                "passive_gc_interval": 1,
            }

        async def advertise_features(self, ctx: PolicyContext) -> list[str]:
            return []

    stub = StubPolicy()
    assert isinstance(stub, LimitsPolicy)
