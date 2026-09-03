"""Typer-based ``cassetta`` CLI entry point.

Subcommands:
- ``cassetta upload`` — stream a tar archive to ``POST /upload/{bundle_path}``.
- ``cassetta download`` — fetch every file in a reference envelope via
  ``GET /download/...`` into ``--out``.
- ``cassetta capabilities`` — print server's advertised limits/features.
- ``cassetta send`` — one-shot directed send: mint an upload session
  (``POST /uploads``) then stream a tar to the returned URL.
"""

from __future__ import annotations

import typer

from cassetta.cli import capabilities as _capabilities
from cassetta.cli import download as _download
from cassetta.cli import send as _send
from cassetta.cli import upload as _upload

app = typer.Typer(name="cassetta", no_args_is_help=True)


@app.callback()
def _main() -> None:
    """Cassetta command-line interface."""


app.command(name="upload")(_upload.upload)
app.command(name="download")(_download.download)
app.command(name="capabilities")(_capabilities.capabilities)
app.command(name="send")(_send.send)
