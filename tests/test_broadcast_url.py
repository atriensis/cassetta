"""Brief 525 — REST broadcast URL contract regression.

Pre-Brief-525 the broadcast endpoint accepted ``POST /broadcast?path=...``.
Brief 525 migrates the URL to path-style ``POST /broadcast/{path:path}`` —
SC-009 / FR-019. The query-string form returns 404; the path-style form
delivers the bundle.
"""

from __future__ import annotations

import httpx
import pytest


class TestBroadcastUrl:
    @pytest.mark.asyncio
    async def test_path_style_url_works(
        self, auth_client: tuple[httpx.AsyncClient, str]
    ) -> None:
        """POST /broadcast/<path> is the canonical Brief 525 form."""
        client, token = auth_client
        setup_resp = await client.post(
            "/setup",
            json={"host": "agent", "project": "sender"},
            headers={"X-Setup-Token": token},
        )
        api_key = setup_resp.json()["api_key"]

        resp = await client.post(
            "/broadcast/announcement.md",
            content=b"Hello broadcast!",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["path"] == "announcement.md"
        assert "delivered_to" in body
        assert "total_delivered" in body

    @pytest.mark.asyncio
    async def test_query_string_form_returns_404(
        self, auth_client: tuple[httpx.AsyncClient, str]
    ) -> None:
        """POST /broadcast?path=... is gone — no compat shim."""
        client, token = auth_client
        setup_resp = await client.post(
            "/setup",
            json={"host": "agent", "project": "sender"},
            headers={"X-Setup-Token": token},
        )
        api_key = setup_resp.json()["api_key"]

        resp = await client.post(
            "/broadcast",
            params={"path": "announcement.md"},
            content=b"Hello broadcast!",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        # Route does not match — FastAPI redirects (307) or returns 404/405.
        assert resp.status_code in (307, 404, 405)

    @pytest.mark.asyncio
    async def test_encoded_slash_in_path_is_decoded(
        self, auth_client: tuple[httpx.AsyncClient, str]
    ) -> None:
        """%2F in the URL is decoded by FastAPI's `:path` converter."""
        client, token = auth_client
        setup_resp = await client.post(
            "/setup",
            json={"host": "agent", "project": "sender"},
            headers={"X-Setup-Token": token},
        )
        api_key = setup_resp.json()["api_key"]

        resp = await client.post(
            "/broadcast/foo%2Fbar.md",
            content=b"hi",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["path"] == "foo/bar.md"

    @pytest.mark.asyncio
    async def test_no_segment_returns_404(
        self, auth_client: tuple[httpx.AsyncClient, str]
    ) -> None:
        """POST /broadcast (no segment) does not match the route."""
        client, token = auth_client
        setup_resp = await client.post(
            "/setup",
            json={"host": "agent", "project": "sender"},
            headers={"X-Setup-Token": token},
        )
        api_key = setup_resp.json()["api_key"]

        resp = await client.post(
            "/broadcast",
            content=b"hi",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        # FastAPI redirects /broadcast → /broadcast/ (307) or returns 404/405.
        assert resp.status_code in (307, 404, 405)

    @pytest.mark.asyncio
    async def test_empty_trailing_segment(
        self, auth_client: tuple[httpx.AsyncClient, str]
    ) -> None:
        """POST /broadcast/ — observed behavior pinned (FastAPI returns 404 or 422)."""
        client, token = auth_client
        setup_resp = await client.post(
            "/setup",
            json={"host": "agent", "project": "sender"},
            headers={"X-Setup-Token": token},
        )
        api_key = setup_resp.json()["api_key"]

        resp = await client.post(
            "/broadcast/",
            content=b"hi",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        # FastAPI's :path converter rejects empty segments — observed 4xx.
        assert resp.status_code >= 400
