"""Brief 535 Fix 1 — ``AppConfig`` drops three never-read fields and
their env-var parsers are removed.

``CASSETTA_IDENTITY_PROVIDER``, ``CASSETTA_ACCESS_POLICY``, and
``CASSETTA_ALIAS_RESOLVER`` were parsed into ``AppConfig`` fields that
no production code read. After the fix, the fields are absent, the
parsers no-op, and operators setting the legacy env vars observe no
warning or error.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest

from cassetta.config import AppConfig, load_config

_DEAD_FIELDS = ("identity_provider", "access_policy", "alias_resolver")
_DEAD_ENV_VARS = (
    "CASSETTA_IDENTITY_PROVIDER",
    "CASSETTA_ACCESS_POLICY",
    "CASSETTA_ALIAS_RESOLVER",
)


def test_app_config_lacks_dead_fields() -> None:
    """``AppConfig`` exposes none of the three retired fields."""
    field_names = {f.name for f in dataclasses.fields(AppConfig)}
    present = field_names & set(_DEAD_FIELDS)
    assert not present, (
        f"AppConfig still exposes dead fields: {present}. "
        "Brief 535 Fix 1 removes identity_provider, access_policy, alias_resolver."
    )


def test_dead_envvars_are_silently_ignored(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: object,
) -> None:
    """Setting the legacy env vars produces no warning, no error, no
    behavior difference."""
    # Minimal valid environment for load_config().
    monkeypatch.setenv("CASSETTA_SETUP_TOKEN", "x")
    monkeypatch.setenv("CASSETTA_STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("CASSETTA_DEFAULT_TTL", "0")
    monkeypatch.setenv(
        "CASSETTA_JWT_KEY",
        "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0",
    )
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", "http://localhost:16001")
    monkeypatch.delenv("CASSETTA_JWT_KEY_FILE", raising=False)
    monkeypatch.delenv("CASSETTA_KEYS_FILE", raising=False)

    # Set the three legacy env vars to non-default values.
    for var in _DEAD_ENV_VARS:
        monkeypatch.setenv(var, "cloud")

    with caplog.at_level(logging.WARNING):
        config = load_config()

    assert isinstance(config, AppConfig)
    # No warning or error referenced any of the three legacy vars.
    for record in caplog.records:
        for var in _DEAD_ENV_VARS:
            assert var not in record.getMessage(), (
                f"unexpected warning mentioning {var}: {record.getMessage()}"
            )
