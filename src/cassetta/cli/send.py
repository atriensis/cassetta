"""``cassetta send`` subcommand.

One-shot directed send: **phase 1** mints an upload session (``POST /uploads`` →
``batch_token`` + ``upload_url``), **phase 2** streams a tar of the named files to
``upload_url``. Reuses ``upload.py``'s path normalisation + tar builder and ``download.py``'s
``asyncio.run`` helper (mirroring how ``capabilities.py`` imports ``download._run_async``).
Additive and client-side only — the server contract is unchanged.

Config resolves from ``--url`` / ``--api-key``, falling back to ``CASSETTA_URL`` /
``CASSETTA_API_KEY``. Missing config or a malformed file path fails before any network
request.
"""

from __future__ import annotations

import os
import sys
import urllib.parse
from collections.abc import AsyncIterator
from typing import Any

import httpx
import typer

from cassetta.cli.download import _run_async
from cassetta.cli.upload import _normalise_arg, _tar_chunks


async def _aiter_tar(files: list[str], compress: bool) -> AsyncIterator[bytes]:
    """Async wrapper over the sync ``_tar_chunks`` generator.

    httpx's ``AsyncClient`` requires an async bytes iterator for a streaming request body; a
    plain (sync) generator is rejected. The wrapper preserves the incremental, non-buffered
    streaming of ``_tar_chunks`` (chunks are yielded as each file is added).
    """
    for chunk in _tar_chunks(files, compress):
        yield chunk


def _raise_for_send(resp: httpx.Response) -> None:
    """Pass 2xx through; map a non-2xx phase response to upload-style exits (4xx→2, 5xx→3)."""
    if 200 <= resp.status_code < 300:
        return
    print(resp.text, file=sys.stderr)
    if 400 <= resp.status_code < 500:
        raise typer.Exit(code=2)
    raise typer.Exit(code=3)


async def _send_async(
    client: httpx.AsyncClient,
    *,
    to: str,
    path: str,
    files: list[str],
    api_key: str,
    compress: bool = True,
) -> str:
    """Run both phases of a directed send through ``client``; return the bundle_id.

    ``files`` are already-normalised logical paths — used both as the manifest names and the
    tar arcnames. The caller owns the ``client`` lifecycle. Server-side non-2xx responses
    raise ``typer.Exit``; transport errors propagate to the caller.
    """
    manifest: dict[str, Any] = {
        "file_count": len(files),
        "files": [{"name": f, "size": os.path.getsize(f)} for f in files],
    }
    init = await client.post(
        "/uploads",
        json={"to": to, "path": path, "manifest": manifest},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    _raise_for_send(init)
    body = init.json()
    upload_url = body["upload_url"]
    batch_token = body["batch_token"]
    bundle_id = body["bundle_id"]

    headers = {
        "Authorization": f"Bearer {batch_token}",
        "Content-Type": "application/x-tar",
    }
    if compress:
        headers["Content-Encoding"] = "gzip"
    # POST to the path relative to the client's base_url: robust when the client's URL differs
    # from the server's advertised upload_url host, and preserves the %2F-encoded bundle path
    # (the proven pattern from test_rest_upload_init.py / test_send_init_batch.py).
    upload_path = urllib.parse.urlparse(upload_url).path
    resp = await client.post(
        upload_path,
        content=_aiter_tar(files, compress),
        headers=headers,
    )
    _raise_for_send(resp)
    return str(resp.json().get("bundle_id", bundle_id))


def send(
    files: list[str] = typer.Argument(..., help="Local file paths to include in the tar."),
    to: str = typer.Option(..., "--to", help="Recipient alias, e.g. alice:main."),
    path: str = typer.Option(..., "--path", help="Destination bundle path, e.g. drop.tgz."),
    url: str | None = typer.Option(
        None,
        "--url",
        help="Server base URL. Overrides CASSETTA_URL.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help="API key (bearer). Overrides CASSETTA_API_KEY.",
    ),
) -> None:
    """Send local files to a recipient in one shot: mint an upload session, then stream a tar.

    On success prints ``bundle_id=<uuid>`` and exits 0. Missing configuration or a malformed
    file path exits non-zero **before** any network request; a 4xx exits 2, a 5xx exits 3, and
    a transport failure exits 1.
    """
    resolved_url = url or os.environ.get("CASSETTA_URL")
    resolved_key = api_key or os.environ.get("CASSETTA_API_KEY")
    if not resolved_url or not resolved_key:
        missing = " and ".join(
            label for label, value in (("url", resolved_url), ("api key", resolved_key)) if not value
        )
        print(
            f"missing {missing}: set --url/--api-key or CASSETTA_URL/CASSETTA_API_KEY",
            file=sys.stderr,
        )
        raise typer.Exit(code=2)

    normalised = [_normalise_arg(f) for f in files]

    async def _run() -> str:
        async with httpx.AsyncClient(base_url=resolved_url, timeout=None) as client:
            return await _send_async(
                client,
                to=to,
                path=path,
                files=normalised,
                api_key=resolved_key,
            )

    try:
        bundle_id = _run_async(_run())
    except httpx.HTTPError as exc:
        print(f"send failed: {exc}", file=sys.stderr)
        raise typer.Exit(code=1) from exc

    print(f"bundle_id={bundle_id}")
