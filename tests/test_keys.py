import httpx
import pytest


@pytest.mark.parametrize("stale_value", ["user-123", None], ids=["set", "null"])
async def test_a_key_request_carrying_an_unrecognised_field_is_still_accepted(
    auth_client: tuple[httpx.AsyncClient, str],
    stale_value: str | None,
) -> None:
    """A ``POST /keys`` body that still carries the retired owner field creates the key as before.

    This request model used to declare ``user_id``, and the server ignored it — the only keystore
    here is single-user and has no per-key owner to record. The field was removed rather than
    deprecated, and this is the compatibility claim that came with the removal, written as a test
    rather than as a sentence in the changelog.

    It holds because no request model in this repository sets ``extra="forbid"``, so Pydantic's
    default applies and an unrecognised field is ignored rather than rejected. That default is the
    whole guarantee, and it is invisible in the source of the model — nothing at
    ``src/cassetta/models.py`` says "and unknown fields are fine". Adding ``extra="forbid"`` to
    ``KeyCreateRequest`` reads as tightening validation and would turn every caller written against
    the old shape into a 422 on upgrade; this is what stands in the way of that.

    Both values a caller could have sent are exercised. ``null`` was the documented way to say "no
    owner" and reaches a different branch of a request body parser than a string does, so a guard
    that checked one of them would only be watching half the callers it claims to protect.
    """
    client, token = auth_client
    await client.post(
        "/setup",
        json={"host": "test", "project": "admin"},
        headers={"X-Setup-Token": token},
    )

    response = await client.post(
        "/keys",
        json={"host": "test", "project": "home-pi", "user_id": stale_value},
        headers={"X-Setup-Token": token},
    )

    assert response.status_code == 201, response.text
    data = response.json()
    assert data["label"] == "test:home-pi"
    assert data["api_key"].startswith("cst_")


class TestSetup:
    async def test_setup_creates_first_key(self, auth_client: tuple[httpx.AsyncClient, str]) -> None:
        client, token = auth_client
        response = await client.post(
            "/setup",
            json={"host": "test", "project": "first-agent"},
            headers={"X-Setup-Token": token},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["label"] == "test:first-agent"
        assert "api_key" in data
        assert data["api_key"].startswith("cst_")
        assert "created_at" in data

    async def test_setup_rejects_second_call(self, auth_client: tuple[httpx.AsyncClient, str]) -> None:
        client, token = auth_client
        await client.post(
            "/setup",
            json={"host": "test", "project": "first"},
            headers={"X-Setup-Token": token},
        )
        response = await client.post(
            "/setup",
            json={"host": "test", "project": "second"},
            headers={"X-Setup-Token": token},
        )
        assert response.status_code == 409

    async def test_setup_rejects_wrong_token(self, auth_client: tuple[httpx.AsyncClient, str]) -> None:
        client, _ = auth_client
        response = await client.post(
            "/setup",
            json={"host": "test", "project": "agent"},
            headers={"X-Setup-Token": "wrong-token"},
        )
        # Under the unified auth dependency, a wrong setup-token
        # with no bearer is 401 Unauthorized (we don't know who you are),
        # not 403 Forbidden.
        assert response.status_code == 401


class TestKeyManagement:
    async def _setup_first_key(
        self,
        client: httpx.AsyncClient,
        token: str,
    ) -> str:
        """Helper: run setup and return the API key."""
        resp = await client.post(
            "/setup",
            json={"host": "test", "project": "admin"},
            headers={"X-Setup-Token": token},
        )
        return resp.json()["api_key"]

    async def test_create_additional_key(self, auth_client: tuple[httpx.AsyncClient, str]) -> None:
        client, token = auth_client
        await self._setup_first_key(client, token)
        response = await client.post(
            "/keys",
            json={"host": "test", "project": "home-pi"},
            headers={"X-Setup-Token": token},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["label"] == "test:home-pi"
        assert data["api_key"].startswith("cst_")

    async def test_create_duplicate_label_rejected(self, auth_client: tuple[httpx.AsyncClient, str]) -> None:
        client, token = auth_client
        await self._setup_first_key(client, token)
        await client.post(
            "/keys",
            json={"host": "test", "project": "dup"},
            headers={"X-Setup-Token": token},
        )
        response = await client.post(
            "/keys",
            json={"host": "test", "project": "dup"},
            headers={"X-Setup-Token": token},
        )
        assert response.status_code == 409

    async def test_list_keys(self, auth_client: tuple[httpx.AsyncClient, str]) -> None:
        client, token = auth_client
        await self._setup_first_key(client, token)
        await client.post(
            "/keys",
            json={"host": "test", "project": "second"},
            headers={"X-Setup-Token": token},
        )
        response = await client.get("/keys", headers={"X-Setup-Token": token})
        assert response.status_code == 200
        data = response.json()
        labels = [k["label"] for k in data["keys"]]
        assert "test:admin" in labels
        assert "test:second" in labels
        # Key values should NOT be in the response
        for key in data["keys"]:
            assert "api_key" not in key

    async def test_rotate_key(self, auth_client: tuple[httpx.AsyncClient, str]) -> None:
        client, token = auth_client
        old_key = await self._setup_first_key(client, token)
        response = await client.post(
            "/keys/test:admin/rotate",
            headers={"X-Setup-Token": token},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["label"] == "test:admin"
        new_key = data["api_key"]
        assert new_key != old_key

        # Old key should no longer work
        resp = await client.put(
            "/files/test.txt",
            content=b"data",
            headers={"Authorization": f"Bearer {old_key}"},
        )
        assert resp.status_code == 401

        # New key should work
        resp = await client.put(
            "/files/test.txt",
            content=b"data",
            headers={"Authorization": f"Bearer {new_key}"},
        )
        assert resp.status_code == 201

    async def test_revoke_key(self, auth_client: tuple[httpx.AsyncClient, str]) -> None:
        client, token = auth_client
        key = await self._setup_first_key(client, token)
        response = await client.delete(
            "/keys/test:admin",
            headers={"X-Setup-Token": token},
        )
        assert response.status_code == 200
        assert response.json()["revoked"] == "test:admin"

        # Revoked key should not work
        resp = await client.put(
            "/files/test.txt",
            content=b"data",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 401

    async def test_revoke_missing_key(self, auth_client: tuple[httpx.AsyncClient, str]) -> None:
        client, token = auth_client
        await self._setup_first_key(client, token)
        response = await client.delete(
            "/keys/nonexistent",
            headers={"X-Setup-Token": token},
        )
        assert response.status_code == 404

    async def test_rotate_missing_key(self, auth_client: tuple[httpx.AsyncClient, str]) -> None:
        client, token = auth_client
        await self._setup_first_key(client, token)
        response = await client.post(
            "/keys/nonexistent/rotate",
            headers={"X-Setup-Token": token},
        )
        assert response.status_code == 404
