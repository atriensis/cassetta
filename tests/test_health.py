import httpx


async def test_health_returns_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    # Brief 529: /health body always includes dev_mode field. The
    # ``client`` fixture boots in dev mode (empty CASSETTA_SETUP_TOKEN).
    assert response.json() == {"status": "ok", "dev_mode": True}
