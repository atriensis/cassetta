"""Unified response envelope shapes for the read surfaces.

Two TypedDicts describe the two mode-branches returned by
``cassetta_pick`` / ``cassetta_get`` (MCP) and the REST read routes
(``GET /files/{path}``, ``GET /inbox/{agent}/{path}``, REST pick):

- :class:`InlineEnvelope` — bundle metadata + per-file ``content``
  for bundles under ``max_inline_size``. Single-file and multi-file
  share one shape; the raw-UTF-8-string single-file return they
  replaced is retired.
- :class:`ReferenceEnvelope` — bundle metadata + per-file URLs + a
  single download credential for bundles over ``max_inline_size``.
  Bytes are fetched out of band via ``GET /download/...``.

Two helper functions build each envelope so the pick/get/REST code
paths share one source of truth.
"""

from __future__ import annotations

import base64
from typing import Any, Literal, TypedDict

from cassetta.protocols.reference_transport import ReferenceTransport


class InlineFile(TypedDict, total=False):
    """One file inside an inline envelope.

    ``encoding`` is the transport encoding over the MCP string
    channel: ``utf8`` for text that round-trips as a JSON string,
    ``base64`` for bytes that are not valid UTF-8 (symmetric with
    inline upload).
    """

    name: str
    content: str
    encoding: Literal["utf8", "base64"]


class InlineEnvelope(TypedDict):
    mode: Literal["inline"]
    bundle: dict[str, Any]
    files: list[InlineFile]


class ReferenceFile(TypedDict):
    name: str
    size: int
    mime: str
    url: str


class ReferenceEnvelope(TypedDict):
    mode: Literal["reference"]
    bundle: dict[str, Any]
    files: list[ReferenceFile]
    download_token: str
    expires_at: str


def build_inline_envelope(
    meta: dict[str, Any],
    file_payloads: list[tuple[str, bytes]],
) -> InlineEnvelope:
    """Construct an inline envelope from manifest + raw file bytes.

    Each file's bytes are UTF-8-decoded when possible
    (``encoding: "utf8"``); on decode failure they are ``base64``-
    encoded (``encoding: "base64"``). Symmetric with the inline-upload
    encoding hint.
    """
    files: list[InlineFile] = []
    for name, data in file_payloads:
        try:
            text = data.decode("utf-8")
            files.append(
                {
                    "name": name,
                    "content": text,
                    "encoding": "utf8",
                }
            )
        except UnicodeDecodeError:
            files.append(
                {
                    "name": name,
                    "content": base64.b64encode(data).decode("ascii"),
                    "encoding": "base64",
                }
            )
    return {
        "mode": "inline",
        "bundle": meta,
        "files": files,
    }


def build_reference_envelope(
    meta: dict[str, Any],
    transport: ReferenceTransport,
    token: str,
    expires_at: str,
) -> ReferenceEnvelope:
    """Construct a reference envelope from manifest + transport + token.

    Per-file URLs come from ``transport.build_download_url``. The
    single ``download_token`` is the same JWT across every
    ``files[i]`` — one credential authorizes the whole claim.
    """
    bundle_path = _infer_bundle_path(meta)
    ref_files: list[ReferenceFile] = []
    for record in meta.get("files", []):
        name = str(record["name"])
        ref_files.append(
            {
                "name": name,
                "size": int(record.get("size", 0)),
                "mime": str(record.get("mime") or "application/octet-stream"),
                "url": transport.build_download_url(bundle_path, name, token),
            }
        )
    return {
        "mode": "reference",
        "bundle": meta,
        "files": ref_files,
        "download_token": token,
        "expires_at": expires_at,
    }


def _infer_bundle_path(meta: dict[str, Any]) -> str:
    """Fallback extractor if the caller didn't pin the path in meta.

    In practice all callers pass the bundle_path explicitly via
    :func:`build_reference_envelope_with_path`; this helper exists
    for safety and returns an empty string if the meta dict lacks a
    path hint.
    """
    return str(meta.get("_bundle_path", ""))


def build_reference_envelope_with_path(
    meta: dict[str, Any],
    bundle_path: str,
    transport: ReferenceTransport,
    token: str,
    expires_at: str,
) -> ReferenceEnvelope:
    """Same as :func:`build_reference_envelope` but with explicit path.

    Preferred call site: callers know the bundle's routing path from
    their own resolution step; passing it explicitly avoids stashing
    it inside the manifest dict.
    """
    ref_files: list[ReferenceFile] = []
    for record in meta.get("files", []):
        name = str(record["name"])
        ref_files.append(
            {
                "name": name,
                "size": int(record.get("size", 0)),
                "mime": str(record.get("mime") or "application/octet-stream"),
                "url": transport.build_download_url(bundle_path, name, token),
            }
        )
    return {
        "mode": "reference",
        "bundle": meta,
        "files": ref_files,
        "download_token": token,
        "expires_at": expires_at,
    }
