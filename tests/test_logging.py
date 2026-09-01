"""Tests for structured logging — JSON formatter, text formatter, and struct_log helper."""

import json
import logging

import pytest

from cassetta.structured_log import (
    JsonFormatter,
    TextFormatter,
    configure_logging,
    request_id_var,
    struct_log,
)

_TEST_JWT_KEY_B64 = "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"


@pytest.fixture(autouse=True)
def _clean_logger():
    """Reset the cassetta + cassetta_cloud loggers between tests."""
    cassetta_logger = logging.getLogger("cassetta")
    cassetta_cloud_logger = logging.getLogger("cassetta_cloud")
    cassetta_logger.handlers.clear()
    cassetta_cloud_logger.handlers.clear()
    yield
    cassetta_logger.handlers.clear()
    cassetta_cloud_logger.handlers.clear()


class TestJsonFormatter:
    """T018: JSON formatter output validation."""

    def test_basic_json_output(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="cassetta", level=logging.INFO, pathname="", lineno=0,
            msg="test.event", args=(), exc_info=None,
        )
        output = formatter.format(record)
        obj = json.loads(output)
        assert "timestamp" in obj
        assert obj["level"] == "INFO"
        assert obj["event"] == "test.event"

    def test_structured_fields_in_json(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="cassetta", level=logging.INFO, pathname="", lineno=0,
            msg="file.uploaded", args=(), exc_info=None,
        )
        record.event = "file.uploaded"  # type: ignore[attr-defined]
        record.identity_label = "alice"  # type: ignore[attr-defined]
        record.resource = "files:report.md"  # type: ignore[attr-defined]
        record.action = "put"  # type: ignore[attr-defined]
        record.result = "ok"  # type: ignore[attr-defined]
        record.duration_ms = 42.5  # type: ignore[attr-defined]
        record.detail = {"size": 1024}  # type: ignore[attr-defined]

        output = formatter.format(record)
        obj = json.loads(output)
        assert obj["event"] == "file.uploaded"
        assert obj["identity_label"] == "alice"
        assert obj["resource"] == "files:report.md"
        assert obj["action"] == "put"
        assert obj["result"] == "ok"
        assert obj["duration_ms"] == 42.5
        assert obj["detail"] == {"size": 1024}

    def test_request_id_from_contextvar(self):
        formatter = JsonFormatter()
        token = request_id_var.set("test-uuid-123")
        try:
            record = logging.LogRecord(
                name="cassetta", level=logging.INFO, pathname="", lineno=0,
                msg="test.event", args=(), exc_info=None,
            )
            output = formatter.format(record)
            obj = json.loads(output)
            assert obj["request_id"] == "test-uuid-123"
        finally:
            request_id_var.reset(token)

    def test_single_line_jsonl(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="cassetta", level=logging.INFO, pathname="", lineno=0,
            msg="test.event", args=(), exc_info=None,
        )
        output = formatter.format(record)
        assert "\n" not in output

    def test_optional_fields_omitted_when_none(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="cassetta", level=logging.INFO, pathname="", lineno=0,
            msg="test.event", args=(), exc_info=None,
        )
        output = formatter.format(record)
        obj = json.loads(output)
        assert "identity_label" not in obj
        assert "resource" not in obj
        assert "duration_ms" not in obj

    def test_timestamp_is_iso8601(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="cassetta", level=logging.INFO, pathname="", lineno=0,
            msg="test.event", args=(), exc_info=None,
        )
        output = formatter.format(record)
        obj = json.loads(output)
        # ISO 8601 timestamps contain 'T' separator
        assert "T" in obj["timestamp"]

    def test_identity_extra_included(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="cassetta", level=logging.INFO, pathname="", lineno=0,
            msg="test", args=(), exc_info=None,
        )
        record.identity_extra = {"team_id": "t1", "user_id": "u1"}  # type: ignore[attr-defined]
        output = formatter.format(record)
        obj = json.loads(output)
        assert obj["identity_extra"] == {"team_id": "t1", "user_id": "u1"}


class TestTextFormatter:
    """T019: Text formatter backward compatibility."""

    def test_basic_text_output(self):
        formatter = TextFormatter()
        record = logging.LogRecord(
            name="cassetta", level=logging.INFO, pathname="", lineno=0,
            msg="File uploaded: path='test.txt', size=100", args=(), exc_info=None,
        )
        output = formatter.format(record)
        # Should contain the message as-is
        assert "File uploaded: path='test.txt', size=100" in output
        assert "INFO" in output

    def test_text_format_has_timestamp(self):
        formatter = TextFormatter()
        record = logging.LogRecord(
            name="cassetta", level=logging.INFO, pathname="", lineno=0,
            msg="test message", args=(), exc_info=None,
        )
        output = formatter.format(record)
        # Should have date-like prefix
        assert "20" in output  # year prefix


class TestConfigureLogging:
    """T020: CASSETTA_LOG_FORMAT config parsing."""

    def test_configure_json(self):
        configure_logging("json")
        logger = logging.getLogger("cassetta")
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0].formatter, JsonFormatter)

    def test_configure_text(self):
        configure_logging("text")
        logger = logging.getLogger("cassetta")
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0].formatter, TextFormatter)

    def test_configure_default_is_text(self):
        configure_logging()
        logger = logging.getLogger("cassetta")
        assert isinstance(logger.handlers[0].formatter, TextFormatter)

    def test_repeated_configure_clears_handlers(self):
        configure_logging("json")
        configure_logging("text")
        logger = logging.getLogger("cassetta")
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0].formatter, TextFormatter)


def _seed_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boot-required env for ``load_config``.

    Brief 539: uses ``monkeypatch`` so nothing leaks. Replaces the old
    ``try/finally`` ``os.environ.pop`` dance; the canonical leak was each test
    setting ``CASSETTA_SETUP_TOKEN`` but not ``CASSETTA_JWT_KEY`` and free-riding
    on a key leaked by another file (Principle IX, research §2).
    """
    monkeypatch.setenv("CASSETTA_SETUP_TOKEN", "test")
    monkeypatch.setenv("CASSETTA_JWT_KEY", _TEST_JWT_KEY_B64)
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", "http://localhost:16001")


class TestLogFormatConfig:
    """T020: CASSETTA_LOG_FORMAT env var parsing."""

    def test_valid_json_value(self, monkeypatch: pytest.MonkeyPatch):
        _seed_config_env(monkeypatch)
        monkeypatch.setenv("CASSETTA_LOG_FORMAT", "json")
        from cassetta.config import load_config

        assert load_config().log_format == "json"

    def test_valid_text_value(self, monkeypatch: pytest.MonkeyPatch):
        _seed_config_env(monkeypatch)
        monkeypatch.setenv("CASSETTA_LOG_FORMAT", "text")
        from cassetta.config import load_config

        assert load_config().log_format == "text"

    def test_default_is_text(self, monkeypatch: pytest.MonkeyPatch):
        _seed_config_env(monkeypatch)
        monkeypatch.delenv("CASSETTA_LOG_FORMAT", raising=False)
        from cassetta.config import load_config

        assert load_config().log_format == "text"

    def test_invalid_value_falls_back_to_text(
        self, monkeypatch: pytest.MonkeyPatch, capsys,
    ):
        _seed_config_env(monkeypatch)
        monkeypatch.setenv("CASSETTA_LOG_FORMAT", "xml")
        from cassetta.config import load_config

        config = load_config()
        assert config.log_format == "text"
        captured = capsys.readouterr()
        assert "WARNING" in captured.err or "xml" in captured.err


class TestStructLog:
    """Test the struct_log helper."""

    def test_struct_log_emits_event(self, capsys):
        configure_logging("text")
        logger = logging.getLogger("cassetta")
        struct_log(logger, logging.INFO, "test.event", result="ok")
        captured = capsys.readouterr()
        assert "test.event" in captured.err

    def test_struct_log_json_output(self):
        configure_logging("json")
        logger = logging.getLogger("cassetta")
        handler = logger.handlers[0]
        record = logging.LogRecord(
            name="cassetta", level=logging.INFO, pathname="", lineno=0,
            msg="file.uploaded", args=(), exc_info=None,
        )
        record.event = "file.uploaded"  # type: ignore[attr-defined]
        record.identity_label = "bob"  # type: ignore[attr-defined]
        record.resource = "files:data.csv"  # type: ignore[attr-defined]
        record.action = "put"  # type: ignore[attr-defined]
        record.result = "ok"  # type: ignore[attr-defined]
        output = handler.formatter.format(record)
        obj = json.loads(output)
        assert obj["event"] == "file.uploaded"
        assert obj["identity_label"] == "bob"


class TestSiblingLoggerCoverage:
    """Brief 529 US3 — configure_logging covers ``cassetta.auth`` and
    ``cassetta_cloud.*`` logger trees through the same handler stack."""

    def test_cassetta_auth_record_renders_through_json_formatter(self, capsys):
        """US3 AS-1: cassetta.auth records render through the JSON formatter."""
        configure_logging("json")
        logging.getLogger("cassetta.auth").warning(
            "test_auth_log", extra={"event": "test_auth_log"},
        )
        captured = capsys.readouterr()
        # Records emit to stderr through the configured StreamHandler.
        assert "test_auth_log" in captured.err
        # JSON formatter wraps each record in `{...}` with a timestamp key.
        line = next(
            line for line in captured.err.splitlines() if "test_auth_log" in line
        )
        obj = json.loads(line)
        assert obj["event"] == "test_auth_log"

    def test_cassetta_cloud_record_renders_through_json_formatter(
        self, capsys,
    ):
        """US3 AS-2: cassetta_cloud.* records render through the same handler."""
        configure_logging("json")
        logging.getLogger("cassetta_cloud.foo").warning(
            "test_cloud_log", extra={"event": "test_cloud_log"},
        )
        captured = capsys.readouterr()
        assert "test_cloud_log" in captured.err
        line = next(
            line for line in captured.err.splitlines()
            if "test_cloud_log" in line
        )
        obj = json.loads(line)
        assert obj["event"] == "test_cloud_log"

    def test_repeated_configure_logging_does_not_duplicate_records(
        self, capsys,
    ):
        """US3 AS-3: configure_logging called twice → exactly one log line."""
        configure_logging("text")
        configure_logging("text")
        logger = logging.getLogger("cassetta.auth")
        logger.warning("once_only", extra={"event": "once_only"})
        captured = capsys.readouterr()
        # Filter for our specific marker — exactly one occurrence.
        marker_lines = [
            line for line in captured.err.splitlines()
            if "once_only" in line
        ]
        assert len(marker_lines) == 1, captured.err

    def test_cassetta_cloud_propagate_enabled_after_configure(self):
        """``cassetta_cloud`` keeps propagate=True so caplog (root-attached
        in pytest) captures records from cloud-side log-assertion tests.
        The local handler attached by configure_logging still emits;
        propagation merely lets pytest's LogCaptureHandler observe."""
        configure_logging("text")
        assert logging.getLogger("cassetta_cloud").propagate is True

    def test_repeated_configure_clears_handlers_on_both_trees(self):
        configure_logging("json")
        configure_logging("text")
        cassetta_logger = logging.getLogger("cassetta")
        cassetta_cloud_logger = logging.getLogger("cassetta_cloud")
        assert len(cassetta_logger.handlers) == 1
        assert len(cassetta_cloud_logger.handlers) == 1
        # Both should share the same formatter type after the second call.
        assert isinstance(
            cassetta_logger.handlers[0].formatter, TextFormatter,
        )
        assert isinstance(
            cassetta_cloud_logger.handlers[0].formatter, TextFormatter,
        )


class TestEventTaxonomy:
    """T041-T042: Verify event consistency across text and JSON formats."""

    def _emit_event(self, formatter, event, **kwargs):
        record = logging.LogRecord(
            name="cassetta", level=logging.INFO, pathname="", lineno=0,
            msg=event, args=(), exc_info=None,
        )
        record.event = event  # type: ignore[attr-defined]
        for k, v in kwargs.items():
            setattr(record, k, v)
        return formatter.format(record)

    def test_same_event_in_both_formats(self):
        json_fmt = JsonFormatter()
        text_fmt = TextFormatter()

        events = [
            "file.uploaded", "file.downloaded", "file.deleted", "file.listed",
            "inbox.sent", "inbox.picked", "inbox.listed",
            "key.created", "key.rotated", "key.revoked",
            "policy.denied", "ttl.cleanup",
            "request.completed",
        ]

        for event in events:
            json_out = self._emit_event(json_fmt, event)
            text_out = self._emit_event(text_fmt, event)

            # JSON output should contain the event name
            obj = json.loads(json_out)
            assert obj["event"] == event, f"JSON event mismatch for {event}"

            # Text output should contain the event name
            assert event in text_out, f"Text output missing event name: {event}"
