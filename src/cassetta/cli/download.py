"""``cassetta download`` subcommand.

Takes a reference-envelope JSON manifest (from ``cassetta_pick``/
``cassetta_get``) and streams every file's bytes into ``--out/<name>``
via the download endpoint, one Authorization-header-bearer GET per
file. The envelope's ``download_token`` is the same credential on
every request; ``X-Sender`` is derived from the JWT's ``recipient``
claim so the agent isn't required to re-specify its own identity on
the command line.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import jwt as _jwt
import typer


def _decode_recipient(token: str) -> str | None:
    """Extract the ``recipient`` claim without signature verification.

    The CLI isn't authenticated to verify the JWT — the server is. All
    we need is the claim value to echo back on the ``X-Sender`` header
    (which the endpoint compares to the SIGNED ``recipient`` claim
    anyway — spoofing here would be caught server-side).
    """
    try:
        claims = _jwt.decode(token, options={"verify_signature": False})
    except Exception:
        return None
    recipient = claims.get("recipient")
    return str(recipient) if recipient else None


def _load_envelope(manifest_json: str) -> dict[str, Any]:
    """Read and parse the manifest JSON from a file path or ``-`` (stdin)."""
    if manifest_json == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(manifest_json).read_text(encoding="utf-8")
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"manifest is not valid JSON: {exc}", file=sys.stderr)
        raise typer.Exit(code=2) from exc
    if not isinstance(envelope, dict):
        print("manifest must be a JSON object", file=sys.stderr)
        raise typer.Exit(code=2)
    return envelope


async def _write_file_streaming(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    dest: Path,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with client.stream("GET", url, headers=headers) as resp:
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError:
            body_bytes = await resp.aread()
            body = body_bytes.decode("utf-8", errors="replace")
            print(body, file=sys.stderr)
            raise
        with open(dest, "wb") as f:
            async for chunk in resp.aiter_bytes():
                if chunk:
                    f.write(chunk)


async def _download_all(
    files: list[Any],
    headers: dict[str, str],
    out: Path,
) -> None:
    async with httpx.AsyncClient(timeout=None) as client:
        for entry in files:
            if not isinstance(entry, dict):
                print(
                    f"invalid file entry (not an object): {entry!r}",
                    file=sys.stderr,
                )
                raise typer.Exit(code=2)
            name = str(entry.get("name", ""))
            url = str(entry.get("url", ""))
            if not name or not url:
                print(
                    f"file entry missing name/url: {entry!r}",
                    file=sys.stderr,
                )
                raise typer.Exit(code=2)
            dest = out / name
            await _write_file_streaming(client, url, headers, dest)


def download(
    manifest_json: str = typer.Option(
        ...,
        "--manifest-json",
        help="Path to the reference envelope (JSON). Use '-' for stdin.",
    ),
    out: Path = typer.Option(
        ...,
        "--out",
        help="Destination directory. Created if missing.",
    ),
) -> None:
    """Fetch every file in a reference envelope via GET /download.

    On success: writes each ``files[i].content`` URL's bytes into
    ``<out>/<files[i].name>`` and exits 0.

    On a missing/malformed manifest, non-reference mode, or an HTTP/
    network error on any file: prints the server body (or exception)
    to stderr and exits non-zero.
    """
    envelope = _load_envelope(manifest_json)

    mode = envelope.get("mode")
    if mode != "reference":
        print(
            f"manifest mode is {mode!r}, not 'reference'; nothing to stream",
            file=sys.stderr,
        )
        raise typer.Exit(code=2)

    token = envelope.get("download_token")
    if not isinstance(token, str) or not token:
        print("manifest is missing 'download_token'", file=sys.stderr)
        raise typer.Exit(code=2)

    files = envelope.get("files") or []
    if not isinstance(files, list) or not files:
        print("manifest has no files to download", file=sys.stderr)
        raise typer.Exit(code=2)

    recipient = _decode_recipient(token)
    headers = {"Authorization": f"Bearer {token}"}
    if recipient:
        headers["X-Sender"] = recipient

    out.mkdir(parents=True, exist_ok=True)

    try:
        _run_async(_download_all(files, headers, out))
    except httpx.HTTPStatusError as exc:
        raise typer.Exit(code=2) from exc
    except httpx.HTTPError as exc:
        print(f"download failed: {exc}", file=sys.stderr)
        raise typer.Exit(code=1) from exc


def _run_async(coro: Any) -> Any:
    """Run an async coroutine to completion, from sync code.

    Uses ``asyncio.run`` when no event loop is active (the default CLI
    invocation), and falls back to a worker thread running its own
    loop when called from inside a running loop (the test harness
    drives this command via Typer's ``CliRunner`` from within a
    ``pytest-asyncio`` test).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import threading

    out_holder: list[Any] = []
    err_holder: list[BaseException] = []

    def _runner() -> None:
        try:
            out_holder.append(asyncio.run(coro))
        except BaseException as exc:  # noqa: BLE001
            err_holder.append(exc)

    thread = threading.Thread(target=_runner)
    thread.start()
    thread.join()
    if err_holder:
        raise err_holder[0]
    return out_holder[0] if out_holder else None
