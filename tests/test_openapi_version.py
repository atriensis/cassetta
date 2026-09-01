"""Brief 535 Fix 2 — OpenAPI ``info.version`` matches the package version.

FastAPI's ``app.version`` flows into the generated OpenAPI document's
``info.version`` field. The hardcoded ``"0.1.0"`` baked into
``src/cassetta/app.py`` made ``/openapi.json`` and ``/capabilities``
disagree about the running version. After the fix, both surfaces must
report ``cassetta.__version__``.
"""

from __future__ import annotations

import httpx
import pytest

import cassetta as _cassetta


@pytest.mark.asyncio
async def test_openapi_info_version_matches_package(
    client: httpx.AsyncClient,
) -> None:
    """FR-005/FR-006: ``/openapi.json``'s ``info.version`` equals
    ``cassetta.__version__``."""
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["info"]["version"] == _cassetta.__version__


@pytest.mark.asyncio
async def test_capabilities_version_matches_openapi(
    client: httpx.AsyncClient,
) -> None:
    """FR-007: ``/openapi.json`` and ``/capabilities`` agree on the
    running version string."""
    openapi_resp = await client.get("/openapi.json")
    caps_resp = await client.get("/capabilities")
    assert openapi_resp.status_code == 200
    assert caps_resp.status_code == 200
    assert openapi_resp.json()["info"]["version"] == caps_resp.json()["server_version"]
