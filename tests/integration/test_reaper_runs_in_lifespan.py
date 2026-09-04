"""The reaper task is spawned + cancelled under app lifespan."""

from __future__ import annotations

import asyncio
import tempfile

import pytest

from cassetta.app import create_app


@pytest.mark.asyncio
async def test_reaper_task_alive_in_lifespan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CASSETTA_SETUP_TOKEN", "")
    monkeypatch.setenv("CASSETTA_STORAGE_PATH", tempfile.mkdtemp())
    monkeypatch.setenv("CASSETTA_DEFAULT_TTL", "0")
    monkeypatch.setenv(
        "CASSETTA_JWT_KEY",
        "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0",
    )
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", "http://localhost:16001")
    monkeypatch.delenv("CASSETTA_KEYS_FILE", raising=False)

    app = create_app()
    async with app.router.lifespan_context(app):
        tasks = [t for t in asyncio.all_tasks() if t.get_name() == "cassetta-gc-reaper"]
        assert len(tasks) == 1
        assert not tasks[0].done()
    # After lifespan exit: task should be cancelled/finished.
    stale = [t for t in asyncio.all_tasks() if t.get_name() == "cassetta-gc-reaper" and not t.done()]
    assert not stale
