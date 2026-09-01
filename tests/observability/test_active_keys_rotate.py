"""Brief 533 FR-009 / SC-003 — ``cassetta.active_keys`` gauge re-asserted on rotate."""

from __future__ import annotations


async def test_rotate_reasserts_gauge_with_current_count(obs_client) -> None:
    client, metrics = obs_client
    # Seed: create a key so a rotation is possible.
    resp = await client.post(
        "/keys", json={"host": "rot", "project": "k1"},
    )
    assert resp.status_code == 201
    metrics.calls.clear()
    # Rotate it.
    resp = await client.post("/keys/rot:k1/rotate")
    assert resp.status_code == 200
    gauges = metrics.find("cassetta.active_keys", "gauge")
    assert gauges, "rotate MUST re-assert cassetta.active_keys (SC-003)"
    # Current count should be 1 active key.
    assert gauges[-1].value >= 1.0


async def test_rotate_reasserts_even_when_count_unchanged(obs_client) -> None:
    """FR-009 freshness invariant — re-assertion is unconditional on rotate."""
    client, metrics = obs_client
    resp = await client.post(
        "/keys", json={"host": "rot2", "project": "k2"},
    )
    assert resp.status_code == 201
    metrics.calls.clear()
    # Rotate twice; gauge re-asserts both times.
    for _ in range(2):
        resp = await client.post("/keys/rot2:k2/rotate")
        assert resp.status_code == 200
    gauges = metrics.find("cassetta.active_keys", "gauge")
    assert len(gauges) >= 2, (
        f"each rotation re-asserts the gauge; got {len(gauges)}"
    )
