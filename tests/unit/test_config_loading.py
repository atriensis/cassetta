"""Tests for the config knobs the upload flow introduced.

Covers:

- ``CASSETTA_JWT_KEY`` / ``CASSETTA_JWT_KEY_FILE`` — primary signing key.
- ``CASSETTA_JWT_KEY_SECONDARY`` / ``CASSETTA_JWT_KEY_SECONDARY_FILE`` — verify-only secondary key.
- ``CASSETTA_PUBLIC_BASE_URL`` — unconditionally required base URL for ``upload_url`` composition.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from cassetta.config import load_config


def _clean_jwt_and_base_url_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "CASSETTA_JWT_KEY",
        "CASSETTA_JWT_KEY_FILE",
        "CASSETTA_JWT_KEY_SECONDARY",
        "CASSETTA_JWT_KEY_SECONDARY_FILE",
        "CASSETTA_PUBLIC_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def _base_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Set the minimum env the pre-514 config loader already requires."""
    monkeypatch.setenv("CASSETTA_SETUP_TOKEN", "")
    monkeypatch.setenv("CASSETTA_STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("CASSETTA_DEFAULT_TTL", "0")
    monkeypatch.delenv("CASSETTA_MAX_FILE_SIZE", raising=False)
    _clean_jwt_and_base_url_env(monkeypatch)


def _valid_primary_key_b64() -> str:
    """32+ bytes of key material, base64-encoded."""
    return base64.b64encode(b"x" * 32).decode("ascii")


def test_primary_key_loaded_from_env_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    primary = _valid_primary_key_b64()
    monkeypatch.setenv("CASSETTA_JWT_KEY", primary)
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", "http://localhost:16001")

    config = load_config()

    assert config.jwt_primary_key == base64.b64decode(primary)
    assert config.jwt_secondary_key is None


def test_key_file_wins_over_env_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    file_key_raw = _valid_primary_key_b64()
    key_path = tmp_path / "jwt.key"
    key_path.write_text(file_key_raw, encoding="ascii")

    env_key_raw = base64.b64encode(b"y" * 32).decode("ascii")
    monkeypatch.setenv("CASSETTA_JWT_KEY", env_key_raw)
    monkeypatch.setenv("CASSETTA_JWT_KEY_FILE", str(key_path))
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", "http://localhost:16001")

    config = load_config()

    # File key wins.
    assert config.jwt_primary_key == base64.b64decode(file_key_raw)


def test_secondary_key_from_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("CASSETTA_JWT_KEY", _valid_primary_key_b64())
    secondary = base64.b64encode(b"z" * 32).decode("ascii")
    monkeypatch.setenv("CASSETTA_JWT_KEY_SECONDARY", secondary)
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", "http://localhost:16001")

    config = load_config()

    assert config.jwt_secondary_key == base64.b64decode(secondary)


def test_secondary_key_from_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("CASSETTA_JWT_KEY", _valid_primary_key_b64())
    secondary = base64.b64encode(b"w" * 32).decode("ascii")
    sec_path = tmp_path / "jwt-secondary.key"
    sec_path.write_text(secondary, encoding="ascii")
    monkeypatch.setenv("CASSETTA_JWT_KEY_SECONDARY_FILE", str(sec_path))
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", "http://localhost:16001")

    config = load_config()

    assert config.jwt_secondary_key == base64.b64decode(secondary)


def test_missing_primary_key_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", "http://localhost:16001")
    # No CASSETTA_JWT_KEY / _KEY_FILE — expect SystemExit naming both.
    with pytest.raises(SystemExit):
        load_config()


def test_missing_public_base_url_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("CASSETTA_JWT_KEY", _valid_primary_key_b64())
    # No CASSETTA_PUBLIC_BASE_URL — expect SystemExit.
    with pytest.raises(SystemExit):
        load_config()


@pytest.mark.parametrize(
    "bad_url",
    ["no-scheme.example.com", "ftp://nope", "", "   "],
)
def test_public_base_url_without_scheme_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bad_url: str,
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("CASSETTA_JWT_KEY", _valid_primary_key_b64())
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", bad_url)
    with pytest.raises(SystemExit):
        load_config()


def test_public_base_url_trailing_slash_stripped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("CASSETTA_JWT_KEY", _valid_primary_key_b64())
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", "https://cassetta.example.com/")

    config = load_config()

    assert config.public_base_url == "https://cassetta.example.com"


def test_primary_key_too_short_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    # 16 bytes of key material is below pyjwt's 32-byte HS256 minimum.
    short_key = base64.b64encode(b"x" * 16).decode("ascii")
    monkeypatch.setenv("CASSETTA_JWT_KEY", short_key)
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", "http://localhost:16001")
    with pytest.raises(SystemExit):
        load_config()


def test_no_jwt_env_leakage_on_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Sanity: config exposes bytes, not the original env value, to discourage echoing."""
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("CASSETTA_JWT_KEY", _valid_primary_key_b64())
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", "http://localhost:16001")

    config = load_config()

    assert isinstance(config.jwt_primary_key, (bytes, bytearray))
    # Env still set (we don't clobber it) — but config exposes decoded bytes.
    assert "CASSETTA_JWT_KEY" in os.environ
