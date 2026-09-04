"""``_FILE`` wins over value-env for JWT key loading."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from cassetta.config import load_config


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CASSETTA_SETUP_TOKEN", "")
    monkeypatch.setenv("CASSETTA_STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("CASSETTA_DEFAULT_TTL", "0")
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", "http://localhost:16001")
    for v in (
        "CASSETTA_JWT_KEY",
        "CASSETTA_JWT_KEY_FILE",
        "CASSETTA_JWT_KEY_SECONDARY",
        "CASSETTA_JWT_KEY_SECONDARY_FILE",
    ):
        monkeypatch.delenv(v, raising=False)


def test_primary_file_wins_over_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _env(monkeypatch, tmp_path)
    file_b = base64.b64encode(b"F" * 36).decode("ascii")
    env_b = base64.b64encode(b"E" * 36).decode("ascii")
    key_path = tmp_path / "primary.key"
    key_path.write_text(file_b, encoding="ascii")
    monkeypatch.setenv("CASSETTA_JWT_KEY", env_b)
    monkeypatch.setenv("CASSETTA_JWT_KEY_FILE", str(key_path))
    cfg = load_config()
    assert cfg.jwt_primary_key == b"F" * 36
    assert cfg.jwt_primary_key_source == "file"


def test_primary_env_source_recorded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _env(monkeypatch, tmp_path)
    env_b = base64.b64encode(b"E" * 36).decode("ascii")
    monkeypatch.setenv("CASSETTA_JWT_KEY", env_b)
    cfg = load_config()
    assert cfg.jwt_primary_key == b"E" * 36
    assert cfg.jwt_primary_key_source == "env"


def test_secondary_file_wins_over_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _env(monkeypatch, tmp_path)
    primary_b = base64.b64encode(b"P" * 36).decode("ascii")
    monkeypatch.setenv("CASSETTA_JWT_KEY", primary_b)
    file_b = base64.b64encode(b"S" * 36).decode("ascii")
    env_b = base64.b64encode(b"T" * 36).decode("ascii")
    sec_path = tmp_path / "sec.key"
    sec_path.write_text(file_b, encoding="ascii")
    monkeypatch.setenv("CASSETTA_JWT_KEY_SECONDARY", env_b)
    monkeypatch.setenv("CASSETTA_JWT_KEY_SECONDARY_FILE", str(sec_path))
    cfg = load_config()
    assert cfg.jwt_secondary_key == b"S" * 36
