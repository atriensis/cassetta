"""Layer-1 protocol — pluggable reference-mode URL construction.

A ``ReferenceTransport`` produces the per-file URL that reference-mode
download responses hand to the client. Core returns its own
authenticated REST URLs pointing at ``GET /download/{bundle_path}/{name}``;
cloud implementations (later track) return presigned S3 / Azure / GCS
URLs. Layer-2 code (MCP tools + REST read routes) depends on this
Protocol, never on a concrete impl.

Layer-1 invariants: no vendor imports, no defaults, no I/O.
"""

from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable


@runtime_checkable
class ReferenceTransport(Protocol):
    """Build a single-file download URL for a reference-mode response.

    Parameters
    ----------
    bundle_path:
        The bundle's on-disk routing path (e.g. ``"inbox/alice/notes.md"``
        or ``"store/archive.zip"``). Implementations SHOULD URL-encode
        slashes so the path survives as a single URL segment on the
        wire.
    name:
        The file name inside the bundle's manifest. May contain ``/``
        if the manifest entry is a sub-path (e.g. ``"src/main.py"``);
        implementations SHOULD URL-encode as needed.
    token:
        The download credential (JWT). Core implementations MAY ignore
        this parameter (the credential travels in the ``Authorization``
        header per ``contracts/download-endpoint.md``). Cloud
        implementations MAY embed it into a presigned URL as a query
        parameter.
    """

    kind: ClassVar[str]

    def build_download_url(
        self,
        bundle_path: str,
        name: str,
        token: str,
    ) -> str: ...
