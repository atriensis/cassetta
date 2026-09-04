"""Server boot aborts with a clear error when no signing keys are configured."""

from __future__ import annotations

import tempfile

import pytest

from cassetta.config import load_config


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CASSETTA_SETUP_TOKEN", "")
    monkeypatch.setenv("CASSETTA_STORAGE_PATH", tempfile.mkdtemp())
    monkeypatch.setenv("CASSETTA_DEFAULT_TTL", "0")
    for var in (
        "CASSETTA_JWT_KEY",
        "CASSETTA_JWT_KEY_FILE",
        "CASSETTA_JWT_KEY_SECONDARY",
        "CASSETTA_JWT_KEY_SECONDARY_FILE",
        "CASSETTA_PUBLIC_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_exits_when_no_primary_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", "http://localhost:16001")
    with pytest.raises(SystemExit):
        load_config()
    err = capsys.readouterr().err
    assert "CASSETTA_JWT_KEY" in err
    assert "CASSETTA_JWT_KEY_FILE" in err


def test_exits_when_public_base_url_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv(
        "CASSETTA_JWT_KEY",
        "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0",
    )
    with pytest.raises(SystemExit):
        load_config()
    err = capsys.readouterr().err
    assert "CASSETTA_PUBLIC_BASE_URL" in err


def test_exits_when_public_base_url_lacks_scheme(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv(
        "CASSETTA_JWT_KEY",
        "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0",
    )
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", "example.com")
    with pytest.raises(SystemExit):
        load_config()
    err = capsys.readouterr().err
    assert "CASSETTA_PUBLIC_BASE_URL" in err
    assert "scheme" in err.lower()
