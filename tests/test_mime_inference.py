"""Tests for MIME inference helper (Brief 512)."""

import logging
from collections.abc import Iterator

import pytest

from cassetta.mime import DEFAULT_MIME, infer_mime, pick_mime


@pytest.fixture
def cassetta_log_records() -> Iterator[list[logging.LogRecord]]:
    """Capture records emitted directly to the ``cassetta`` logger.

    The cassetta logger disables propagation at configure time, so pytest's
    ``caplog`` fixture (rooted at the root logger) doesn't see its records in
    non-isolated runs. Attaching a fresh handler sidesteps that.
    """
    records: list[logging.LogRecord] = []

    class _CollectingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _CollectingHandler(level=logging.DEBUG)
    logger = logging.getLogger("cassetta")
    prev_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)


class TestInferMime:
    def test_markdown_extension(self) -> None:
        assert infer_mime("notes.md") == "text/markdown"

    def test_png_extension(self) -> None:
        assert infer_mime("photo.png") == "image/png"

    def test_python_extension(self) -> None:
        assert infer_mime("script.py") == "text/x-python"

    def test_unknown_extension_fallback(self) -> None:
        assert infer_mime("mystery.zzz999") == DEFAULT_MIME

    def test_unknown_extension_emits_debug_log(
        self, cassetta_log_records: list[logging.LogRecord]
    ) -> None:
        infer_mime("mystery.zzz999")
        assert any(
            getattr(rec, "event", None) == "mime_inference_fallback"
            for rec in cassetta_log_records
        )

    def test_no_extension(self) -> None:
        assert infer_mime("README") == DEFAULT_MIME


class TestPickMime:
    def test_explicit_wins_over_inferred(self) -> None:
        assert pick_mime("notes.md", explicit="text/plain") == "text/plain"

    def test_empty_explicit_falls_back_to_inferred(self) -> None:
        assert pick_mime("notes.md", explicit=None) == "text/markdown"

    def test_empty_string_treated_as_absent(self) -> None:
        assert pick_mime("notes.md", explicit="") == "text/markdown"

    def test_unknown_extension_inferred(self) -> None:
        assert pick_mime("mystery.zzz999", explicit=None) == DEFAULT_MIME
