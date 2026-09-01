"""Integration tests for ``GET /capabilities`` (Brief 516 US3).

Covers FR-002, FR-013, FR-015, FR-009.
"""

from __future__ import annotations

import httpx
import pytest

import cassetta as _cassetta


@pytest.mark.asyncio
async def test_get_capabilities_200_and_schema(client: httpx.AsyncClient) -> None:
    resp = await client.get("/capabilities")
    assert resp.status_code == 200

    doc = resp.json()
    assert set(doc.keys()) == {
        "schema_version",
        "server_version",
        "supported_modes",
        "limits",
        "ttls",
        "features",
    }
    assert doc["schema_version"] == 1
    assert doc["server_version"] == _cassetta.__version__
    assert doc["supported_modes"] == ["inline", "batch", "reference"]
    assert doc["features"] == ["peek", "batch_upload", "reference_download", "rest_send_init"]

    # limits dict: integer or null per field
    for key in (
        "per_file_max",
        "per_bundle_total_max",
        "per_bundle_file_count_max",
        "max_inline_size",
    ):
        assert key in doc["limits"]
        value = doc["limits"][key]
        assert value is None or isinstance(value, int)

    # ttls dict: every field an integer
    for key in (
        "upload_token_ttl",
        "download_claim_ttl",
        "passive_gc_min_age",
        "passive_gc_interval",
    ):
        assert isinstance(doc["ttls"][key], int)


@pytest.mark.asyncio
async def test_get_capabilities_dev_mode_no_auth_required(
    client: httpx.AsyncClient,
) -> None:
    # The client fixture starts the server in dev mode (SETUP_TOKEN="").
    # No API-key header is passed, and we expect 200.
    resp = await client.get("/capabilities")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_capabilities_response_is_flat_json_object(
    client: httpx.AsyncClient,
) -> None:
    """FR-015: no ``detail``/``data``/``capabilities`` wrapping."""
    resp = await client.get("/capabilities")
    doc = resp.json()
    assert "detail" not in doc
    assert "data" not in doc
    assert "capabilities" not in doc
    # The document fields are present at the top level.
    assert "schema_version" in doc
    assert "limits" in doc


@pytest.mark.asyncio
async def test_get_capabilities_reflects_env_vars(
    client: httpx.AsyncClient,
) -> None:
    # The env_setup fixture sets max_file_size=1048576, which is the retired
    # CASSETTA_MAX_FILE_SIZE; the active caps come from LimitsConfig defaults
    # unless we override. Verify defaults:
    resp = await client.get("/capabilities")
    doc = resp.json()
    # LimitsConfig() defaults: per_file_max=None, per_bundle_total_max=None,
    # per_bundle_file_count_max=25, max_inline_size=102400.
    assert doc["limits"]["per_file_max"] is None
    assert doc["limits"]["per_bundle_total_max"] is None
    assert doc["limits"]["per_bundle_file_count_max"] == 25
    assert doc["limits"]["max_inline_size"] == 102400
