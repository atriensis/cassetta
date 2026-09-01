"""T002 — unit tests for ``CoreReferenceTransport`` URL construction.

The transport is a Layer-2 helper that builds per-file download URLs
from a public base URL + a bundle path + a file name. For core the
token is accepted as a parameter but NOT embedded in the URL — it
travels in the ``Authorization: Bearer`` header (contract
``download-endpoint.md``). Cloud implementations are free to embed
the token into a presigned URL; that is a different impl.

Covered:
- Slash in bundle_path is URL-encoded (single segment under /download/).
- Trailing slashes on the public base URL are normalised.
- The token argument is accepted but does not appear in the URL.
- Slashes inside file names (sub-directory entries) are URL-encoded.
"""

from __future__ import annotations

from cassetta.defaults.default_transport import CoreReferenceTransport
from cassetta.protocols.reference_transport import ReferenceTransport


def test_builds_url_with_encoded_bundle_path() -> None:
    transport = CoreReferenceTransport("https://cassetta.local")
    url = transport.build_download_url(
        "inbox/alice/notes.md", "notes.md", "jwt-placeholder",
    )
    assert url == (
        "https://cassetta.local/download/inbox%2Falice%2Fnotes.md/notes.md"
    )


def test_normalises_trailing_slash_on_base() -> None:
    transport = CoreReferenceTransport("https://cassetta.local/")
    url = transport.build_download_url(
        "store/archive.zip", "archive.zip", "jwt",
    )
    assert url == (
        "https://cassetta.local/download/store%2Farchive.zip/archive.zip"
    )


def test_token_not_embedded_in_url() -> None:
    transport = CoreReferenceTransport("https://cassetta.local")
    url = transport.build_download_url(
        "inbox/alice/docs", "README.md", "eyJhbGciOiJIUzI1NiJ9.secret",
    )
    assert "secret" not in url
    assert "eyJ" not in url
    # Token lives in the Authorization header, not the URL.


def test_encodes_slash_in_file_name() -> None:
    """Sub-directory entries in the manifest travel as part of `name`."""
    transport = CoreReferenceTransport("https://cassetta.local")
    url = transport.build_download_url(
        "inbox/alice/project", "src/main.py", "jwt",
    )
    # Both the bundle_path's slash AND the name's slash must be encoded.
    assert url == (
        "https://cassetta.local/download/inbox%2Falice%2Fproject/src%2Fmain.py"
    )


def test_protocol_conformance() -> None:
    """CoreReferenceTransport satisfies the ReferenceTransport Protocol."""
    transport = CoreReferenceTransport("https://cassetta.local")
    assert isinstance(transport, ReferenceTransport)


def test_multiple_trailing_slashes_normalised() -> None:
    transport = CoreReferenceTransport("https://cassetta.local////")
    url = transport.build_download_url("store/x", "x", "t")
    assert url.startswith("https://cassetta.local/download/")
    assert "///" not in url[len("https://") :]
