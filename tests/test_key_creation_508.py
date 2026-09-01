"""Tests for structured key creation with host:project format (Brief 508)."""

import pytest
from pydantic import ValidationError

from cassetta.models import KeyCreateRequest, SetupRequest


class TestKeyCreateRequestModel:
    def test_valid_host_project(self) -> None:
        req = KeyCreateRequest(host="laptop", project="docs")
        assert req.label == "laptop:docs"

    def test_label_contains_colon(self) -> None:
        req = KeyCreateRequest(host="server", project="api")
        assert ":" in req.label

    def test_hyphen_underscore_allowed(self) -> None:
        req = KeyCreateRequest(host="my-host", project="my_project")
        assert req.label == "my-host:my_project"

    def test_empty_host_rejected(self) -> None:
        with pytest.raises(ValidationError, match="host"):
            KeyCreateRequest(host="", project="docs")

    def test_empty_project_rejected(self) -> None:
        with pytest.raises(ValidationError, match="project"):
            KeyCreateRequest(host="laptop", project="")

    def test_colon_in_host_rejected(self) -> None:
        with pytest.raises(ValidationError, match="host"):
            KeyCreateRequest(host="bad:host", project="docs")

    def test_colon_in_project_rejected(self) -> None:
        with pytest.raises(ValidationError, match="project"):
            KeyCreateRequest(host="laptop", project="bad:project")

    def test_slash_in_host_rejected(self) -> None:
        with pytest.raises(ValidationError, match="host"):
            KeyCreateRequest(host="bad/host", project="docs")

    def test_space_in_project_rejected(self) -> None:
        with pytest.raises(ValidationError, match="project"):
            KeyCreateRequest(host="laptop", project="bad project")


class TestSetupRequestModel:
    def test_valid_host_project(self) -> None:
        req = SetupRequest(host="pi", project="home-automation")
        assert req.label == "pi:home-automation"

    def test_empty_host_rejected(self) -> None:
        with pytest.raises(ValidationError, match="host"):
            SetupRequest(host="", project="docs")

    def test_empty_project_rejected(self) -> None:
        with pytest.raises(ValidationError, match="project"):
            SetupRequest(host="pi", project="")


class TestKeyCreationEndpoint:
    @pytest.mark.asyncio
    async def test_setup_with_host_project(self, auth_client: tuple) -> None:
        client, setup_token = auth_client
        resp = await client.post(
            "/setup",
            json={"host": "pi", "project": "home"},
            headers={"X-Setup-Token": setup_token},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["label"] == "pi:home"
        assert "api_key" in data

    @pytest.mark.asyncio
    async def test_create_key_with_host_project(self, auth_client: tuple) -> None:
        client, setup_token = auth_client
        resp = await client.post(
            "/setup",
            json={"host": "pi", "project": "home"},
            headers={"X-Setup-Token": setup_token},
        )
        api_key = resp.json()["api_key"]

        resp = await client.post(
            "/keys",
            json={"host": "laptop", "project": "docs"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["label"] == "laptop:docs"

    @pytest.mark.asyncio
    async def test_duplicate_label_rejected(self, auth_client: tuple) -> None:
        client, setup_token = auth_client
        resp = await client.post(
            "/setup",
            json={"host": "pi", "project": "home"},
            headers={"X-Setup-Token": setup_token},
        )
        api_key = resp.json()["api_key"]

        resp = await client.post(
            "/keys",
            json={"host": "pi", "project": "home"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_missing_host_rejected(self, auth_client: tuple) -> None:
        client, setup_token = auth_client
        resp = await client.post(
            "/setup",
            json={"project": "home"},
            headers={"X-Setup-Token": setup_token},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_project_rejected(self, auth_client: tuple) -> None:
        client, setup_token = auth_client
        resp = await client.post(
            "/setup",
            json={"host": "pi"},
            headers={"X-Setup-Token": setup_token},
        )
        assert resp.status_code == 422
