"""``cassetta upload`` subcommand.

Streams a tar archive to ``POST /upload/{bundle_path}`` with the JWT from
``cassetta_send_init``. Tar entry names are the positional arguments
**verbatim** after :func:`cassetta.path_validation.validate_path`-equivalent
normalisation (no flattening).
"""

from __future__ import annotations

import io
import sys
import tarfile
from collections.abc import Iterator

import httpx
import typer

from cassetta.path_validation import PathValidationError, validate_path


def _normalise_arg(arg: str) -> str:
    """Mirror the server-side `validate_path` contract.

    - Strip leading ``./``.
    - Reject absolute paths, ``..`` segments, out-of-charset characters.
    - Reject empty strings (after strip).
    """
    if arg.startswith("./"):
        arg = arg[2:]
    if not arg:
        raise typer.BadParameter("file argument cannot be empty")
    if arg.startswith("/"):
        raise typer.BadParameter(f"absolute path not allowed: {arg!r}")
    try:
        validate_path(arg)
    except PathValidationError as exc:
        raise typer.BadParameter(f"invalid path {arg!r}: {exc}") from exc
    return arg


def _tar_chunks(files: list[str], compress: bool) -> Iterator[bytes]:
    """Yield tar bytes as entries are added, for streaming POST bodies."""
    buf = io.BytesIO()
    mode = "w|gz" if compress else "w|"
    with tarfile.open(fileobj=buf, mode=mode) as tf:  # type: ignore[call-overload]
        for path in files:
            tf.add(path, arcname=path, filter=_strip_owner)
            chunk = buf.getvalue()
            if chunk:
                yield chunk
            buf.seek(0)
            buf.truncate()
    # Final flush on close (the `with` block emits trailing blocks).
    tail = buf.getvalue()
    if tail:
        yield tail


def _strip_owner(info: "tarfile.TarInfo") -> "tarfile.TarInfo | None":
    """Normalise tar metadata; reject non-regular entries."""
    if not info.isreg():
        # Symlinks/hardlinks/devices: drop the entry entirely.
        return None
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mode = 0o644
    return info


def upload(
    files: list[str] = typer.Argument(..., help="Local file paths to include in the tar."),
    url: str = typer.Option(..., "--url", help="Upload URL from send_init (batch mode)."),
    token: str = typer.Option(..., "--token", help="JWT batch_token from send_init."),
    no_compress: bool = typer.Option(
        False,
        "--no-compress",
        help="Send application/x-tar without gzip (default is gzip).",
    ),
) -> None:
    """Stream a tar archive to the server via a single POST.

    On success: prints ``bundle_id=<uuid>`` to stdout and exits 0.
    On 4xx: prints the server body to stderr and exits 2.
    On 5xx: prints the server body to stderr and exits 3.
    On other errors (network, bad args, I/O): exits 1.
    """
    normalised = [_normalise_arg(f) for f in files]

    compress = not no_compress
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-tar",
    }
    if compress:
        headers["Content-Encoding"] = "gzip"

    try:
        resp = httpx.post(
            url,
            content=_tar_chunks(normalised, compress),
            headers=headers,
            timeout=None,
        )
    except httpx.HTTPError as exc:
        print(f"upload failed: {exc}", file=sys.stderr)
        raise typer.Exit(code=1) from exc

    if 200 <= resp.status_code < 300:
        try:
            body = resp.json()
        except ValueError:
            print(f"unexpected non-json 2xx response: {resp.text}", file=sys.stderr)
            raise typer.Exit(code=1) from None
        bundle_id = body.get("bundle_id", "")
        print(f"bundle_id={bundle_id}")
        raise typer.Exit(code=0)

    body_text = resp.text
    print(body_text, file=sys.stderr)
    if 400 <= resp.status_code < 500:
        raise typer.Exit(code=2)
    raise typer.Exit(code=3)
