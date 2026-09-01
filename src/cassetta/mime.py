"""MIME type inference for bundle files.

Uses Python's stdlib ``mimetypes`` with ``strict=False`` to cover the common
extensions seen in agent interchange (.md, .py, .json, .txt, images). Falls
back to ``application/octet-stream`` when the extension is unknown, emitting
a DEBUG structured log so operators can spot recurring misses.
"""

from __future__ import annotations

import logging
import mimetypes

from cassetta.structured_log import struct_log

DEFAULT_MIME = "application/octet-stream"

logger = logging.getLogger("cassetta")


def infer_mime(name: str) -> str:
    """Guess a MIME type for ``name`` using stdlib ``mimetypes``.

    Falls back to ``application/octet-stream`` when no match is found and
    emits a DEBUG ``mime_inference_fallback`` structured log.
    """
    guessed, _ = mimetypes.guess_type(name, strict=False)
    if guessed is None:
        struct_log(
            logger,
            logging.DEBUG,
            "mime_inference_fallback",
            detail={"name": name, "mime": DEFAULT_MIME},
        )
        return DEFAULT_MIME
    return guessed


def pick_mime(name: str, *, explicit: str | None) -> str:
    """Return an explicit caller-supplied mime if present, else infer one."""
    if explicit:
        return explicit
    return infer_mime(name)
