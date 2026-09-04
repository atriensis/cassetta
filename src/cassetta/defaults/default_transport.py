"""Default :class:`ReferenceTransport` implementation.

``CoreReferenceTransport`` builds authenticated REST URLs of the form
``{public_base}/download/{bundle_path_urlencoded}/{name_urlencoded}``.
The token travels in the ``Authorization: Bearer`` header; it is
accepted as a parameter for Protocol conformance but is NOT embedded
in the URL.

Layer-2 default — no vendor imports, no I/O.
"""

from __future__ import annotations

import urllib.parse
from typing import ClassVar


class CoreReferenceTransport:
    """Construct local ``/download/{bundle_path}/{name}`` URLs."""

    kind: ClassVar[str] = "core"

    def __init__(self, public_base_url: str) -> None:
        self._base = public_base_url.rstrip("/")

    def build_download_url(
        self,
        bundle_path: str,
        name: str,
        token: str,
    ) -> str:
        # token is accepted for ReferenceTransport conformance; header-auth
        # is the core transport's token channel, not a query parameter.
        del token
        encoded_path = urllib.parse.quote(bundle_path, safe="")
        encoded_name = urllib.parse.quote(name, safe="")
        return f"{self._base}/download/{encoded_path}/{encoded_name}"
