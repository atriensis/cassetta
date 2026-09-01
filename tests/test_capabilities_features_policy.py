"""Brief 535 Fix 3 — ``LimitsPolicy.advertise_features`` produces the
``features`` list in the capabilities document.

``CoreLimitsPolicy`` returns the full canonical tuple unmodified. The
capabilities builder calls ``policy.advertise_features(ctx)`` instead
of inlining ``list(FEATURES)``.
"""

from __future__ import annotations

import httpx
import pytest

from cassetta.capabilities import FEATURES, build_capabilities_document
from cassetta.config import LimitsConfig
from cassetta.defaults.default_limits import CoreLimitsPolicy
from cassetta.protocols.identity import Identity
from cassetta.protocols.limits import PolicyContext


@pytest.mark.asyncio
async def test_core_limits_policy_advertises_full_features() -> None:
    policy = CoreLimitsPolicy(LimitsConfig())
    ctx = PolicyContext(identity=Identity(label="dev"))
    features = await policy.advertise_features(ctx)
    assert features == list(FEATURES)
    assert features == ["peek", "batch_upload", "reference_download", "rest_send_init"]


@pytest.mark.asyncio
async def test_capabilities_document_uses_advertise_features() -> None:
    """The builder delegates the ``features`` field to
    ``policy.advertise_features``. Asserting at the builder layer
    avoids the need to spin up a full app."""
    policy = CoreLimitsPolicy(LimitsConfig())
    ctx = PolicyContext(identity=Identity(label="dev"))
    doc = await build_capabilities_document(policy, ctx, via="rest")
    assert doc["features"] == await policy.advertise_features(ctx)


@pytest.mark.asyncio
async def test_capabilities_route_features_unchanged_in_core(
    client: httpx.AsyncClient,
) -> None:
    """Wire format unchanged for the core configuration — features list
    equals the canonical tuple, regardless of the new policy hook."""
    resp = await client.get("/capabilities")
    assert resp.status_code == 200
    assert resp.json()["features"] == ["peek", "batch_upload", "reference_download", "rest_send_init"]
