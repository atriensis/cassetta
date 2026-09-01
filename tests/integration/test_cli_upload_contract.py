"""T028 — CLI contract tests for ``cassetta upload`` (Brief 514).

We spawn the installed console entry point (`cassetta upload`) in a
subprocess and assert that:

- ``--help`` lists all the expected flags + positional args.
- Client-side path normalisation rejects absolute paths, traversal,
  out-of-charset characters, and empty arguments, exiting ≠ 0 **before
  opening any HTTP request**.

The full e2e with real network + tar + server is covered in
``test_send_init_batch.py``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _plain_env() -> dict[str, str]:
    """Env that disables Rich color + forces a wide terminal.

    Typer/Rich inject ANSI escape sequences and hard-wrap the --help
    output based on terminal width. Under CI (no TTY, narrow default
    columns) a flag like ``--url`` can land on a wrapped line with
    embedded styling, breaking a plain substring check. Forcing
    ``NO_COLOR`` + ``TERM=dumb`` strips color and ``COLUMNS=200`` keeps
    each option on one line.
    """
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
    # Fall back to PATH.
    return "cassetta"


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Spawn ``cassetta upload ...`` using the installed entry point."""
    return subprocess.run(
        [_cassetta_bin(), "upload", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_help_lists_all_flags() -> None:
    proc = subprocess.run(
        [_cassetta_bin(), "upload", "--help"],
        capture_output=True, text=True, check=True,
        env=_plain_env(),
    )
    out = _strip_ansi(proc.stdout)
    assert "--url" in out
    assert "--token" in out
    assert "--no-compress" in out
    assert "FILES" in out.upper()


def test_cli_rejects_absolute_path() -> None:
    proc = _cli(
        "--url", "http://localhost:16001/upload/does-not-matter",
        "--token", "irrelevant.irrelevant.irrelevant",
        "/absolute/path.py",
    )
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert "absolute" in combined.lower() or "invalid" in combined.lower()


def test_cli_rejects_traversal() -> None:
    proc = _cli(
        "--url", "http://localhost:16001/upload/nope",
        "--token", "irrelevant.irrelevant.irrelevant",
        "../escape.py",
    )
    assert proc.returncode != 0


def test_cli_rejects_out_of_charset() -> None:
    proc = _cli(
        "--url", "http://localhost:16001/upload/nope",
        "--token", "irrelevant.irrelevant.irrelevant",
        "weird<char.py",
    )
    assert proc.returncode != 0


def test_cli_accepts_nested_relative_path(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Nested relative paths must pass normalisation (they shouldn't reach HTTP).

    We don't actually complete the upload; we assert that normalisation
    did not reject it (the failure will be a network error exit-1).
    """
    # Create a real file so tarfile.add succeeds.
    f = tmp_path / "src" / "main.py"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"print('hi')\n")

    # Use an unreachable URL to force an HTTP failure AFTER normalisation.
    proc = subprocess.run(
        [
            _cassetta_bin(), "upload",
            "--url", "http://127.0.0.1:1/does-not-matter",
            "--token", "irrelevant.irrelevant.irrelevant",
            "src/main.py",
        ],
        cwd=str(tmp_path),
        capture_output=True, text=True, check=False,
    )
    # HTTP error path returns exit 1; argument-validation errors exit != 0
    # with "typer" / "invalid" / "absolute" keywords. We assert we reached
    # the HTTP layer (no "invalid path" message) by checking for *absence*
    # of normalisation-rejection markers.
    assert proc.returncode != 0  # network failure or similar
    combined = (proc.stdout + proc.stderr).lower()
    assert "invalid path" not in combined
    assert "absolute" not in combined
    assert sys.executable  # just to silence the unused-import warning


def test_cli_rejects_empty_positional() -> None:
    # Passing an empty-string arg should fail.
    proc = subprocess.run(
        [
            _cassetta_bin(), "upload",
            "--url", "http://localhost:16001/upload/nope",
            "--token", "irrelevant.irrelevant.irrelevant",
            "",
        ],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode != 0
