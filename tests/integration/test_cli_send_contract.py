"""Brief 548 — CLI contract tests for ``cassetta send``.

Three concerns, all *without* a live server:

- ``--help`` lists the option/argument surface (subprocess on the installed entry point,
  mirroring ``test_cli_upload_contract.py``).
- Config resolution: ``CASSETTA_URL`` / ``CASSETTA_API_KEY`` are read when no flags are given,
  and ``--url`` / ``--api-key`` override them. Proven by patching the send core and asserting it
  is reached with the resolved values (no live network).
- Fail-fast: missing config and malformed file paths are rejected *before* the send core is
  reached (the patched core asserts it is never called).

The full phase-1 + phase-2 round-trip lives in ``test_send_roundtrip.py``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Any
from unittest import mock

import pytest
from typer.testing import CliRunner

from cassetta.cli import app as cli_app

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _plain_env() -> dict[str, str]:
    """Env that disables Rich color and forces a wide terminal for stable --help text."""
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    env["COLUMNS"] = "200"
    return env


def _cassetta_bin() -> str:
    """Locate the ``cassetta`` entry point alongside the current python."""
    here = os.path.dirname(sys.executable)
    candidate = os.path.join(here, "cassetta")
    if os.path.isfile(candidate):
        return candidate
    return "cassetta"


# --- US3: help surface -------------------------------------------------------


def test_send_help_lists_flags() -> None:
    proc = subprocess.run(
        [_cassetta_bin(), "send", "--help"],
        capture_output=True,
        text=True,
        check=True,
        env=_plain_env(),
    )
    out = _strip_ansi(proc.stdout)
    assert "--to" in out
    assert "--path" in out
    assert "--url" in out
    assert "--api-key" in out
    assert "FILES" in out.upper()


# --- helpers for the CliRunner-based config/guard tests ----------------------


def _clear_send_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CASSETTA_URL", raising=False)
    monkeypatch.delenv("CASSETTA_API_KEY", raising=False)


def _invoke_capturing(
    args: list[str],
) -> tuple[Any, dict[str, Any]]:
    """Invoke ``cassetta send`` with the send core patched to CAPTURE resolved config.

    Returns (result, capture) where capture holds the client base_url + api_key the core
    would have been driven with. No real network is used.
    """
    capture: dict[str, Any] = {}

    async def _stub(client, *, to, path, files, api_key, compress=True):  # noqa: ANN001, ANN202
        capture["base_url"] = str(client.base_url)
        capture["api_key"] = api_key
        capture["to"] = to
        capture["path"] = path
        capture["files"] = list(files)
        return "stub-bundle-id"

    runner = CliRunner()
    with mock.patch("cassetta.cli.send._send_async", _stub):
        result = runner.invoke(cli_app, ["send", *args])
    return result, capture


def _invoke_expecting_no_core(
    args: list[str],
) -> Any:
    """Invoke with the send core patched to FAIL if reached — proves a pre-HTTP guard fired."""

    async def _stub(client, *, to, path, files, api_key, compress=True):  # noqa: ANN001, ANN202
        raise AssertionError("send core reached — a pre-HTTP guard should have fired first")

    runner = CliRunner()
    with mock.patch("cassetta.cli.send._send_async", _stub):
        result = runner.invoke(cli_app, ["send", *args])
    return result


# --- US2: config resolution --------------------------------------------------


def test_send_resolves_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env vars supply url/key when no flags are given; flags override env."""
    # Env-only: reaches the send core (no "missing config" error) with env values.
    monkeypatch.setenv("CASSETTA_URL", "http://env-host:16001")
    monkeypatch.setenv("CASSETTA_API_KEY", "cst_env_key")
    result, capture = _invoke_capturing(["--to", "alice:main", "--path", "d.tgz", "note.txt"])
    assert result.exit_code == 0, result.output
    assert capture["api_key"] == "cst_env_key"
    assert capture["base_url"].rstrip("/") == "http://env-host:16001"
    assert "bundle_id=stub-bundle-id" in result.output

    # Flags override env.
    result2, capture2 = _invoke_capturing(
        [
            "--to",
            "alice:main",
            "--path",
            "d.tgz",
            "--url",
            "http://flag-host:16001",
            "--api-key",
            "cst_flag_key",
            "note.txt",
        ]
    )
    assert result2.exit_code == 0, result2.output
    assert capture2["api_key"] == "cst_flag_key"
    assert capture2["base_url"].rstrip("/") == "http://flag-host:16001"


# --- US3: fail fast before any network ---------------------------------------


def test_send_errors_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither env nor flag → clear error, exit != 0, before the send core is reached."""
    _clear_send_env(monkeypatch)
    result = _invoke_expecting_no_core(["--to", "alice:main", "--path", "d.tgz", "note.txt"])
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit), result.output


def test_send_rejects_bad_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absolute / traversal / out-of-charset / empty file args rejected before the send core."""
    # Config present so the path guard (not the config guard) is what fires.
    monkeypatch.setenv("CASSETTA_URL", "http://env-host:16001")
    monkeypatch.setenv("CASSETTA_API_KEY", "cst_env_key")
    for bad in ("/absolute/path.py", "../escape.py", "weird<char.py", ""):
        result = _invoke_expecting_no_core(["--to", "alice:main", "--path", "d.tgz", bad])
        assert result.exit_code != 0, f"expected rejection for {bad!r}: {result.output}"
