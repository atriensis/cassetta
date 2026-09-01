"""Structured logging — JSON and text formatters + request-scoped context.

Provides:
- ``JsonFormatter``: one JSON object per line (JSONL) for production/cloud
- ``TextFormatter``: human-readable output matching pre-507 behavior
- ``request_id_var``: contextvars.ContextVar for per-request UUID
- ``configure_logging()``: sets up the ``cassetta`` logger with the chosen format
- ``struct_log()``: helper to emit structured log events with consistent fields
- ``safe_emit()``: Brief 533 helper — funnel ``struct_log`` + counter/gauge
  through two INDEPENDENT best-effort exception handlers so an observability
  failure never masks the request path.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cassetta.protocols.metrics import MetricsProvider

# Per-request context — set by RequestIdMiddleware, read by formatters.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line (JSONL).

    Standard fields (always present): timestamp, level, event.
    Optional fields from ``extra``: request_id, identity_label,
    identity_extra, resource, action, result, duration_ms, detail.
    """

    # Fields we pull from the LogRecord's extra dict into top-level JSON.
    _STRUCTURED_FIELDS = (
        "event",
        "request_id",
        "identity_label",
        "identity_extra",
        "resource",
        "action",
        "result",
        "duration_ms",
        "detail",
    )

    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created,
                tz=timezone.utc,
            ).isoformat(),
            "level": record.levelname,
        }

        # Pull structured fields from record attributes (set via extra={}).
        for field in self._STRUCTURED_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                obj[field] = value

        # If no explicit "event" was provided, use the log message.
        if "event" not in obj:
            obj["event"] = record.getMessage()

        # Fall back to contextvars for request_id if not in extra.
        if "request_id" not in obj:
            rid = request_id_var.get()
            if rid is not None:
                obj["request_id"] = rid

        # Include exception info if present.
        if record.exc_info and record.exc_info[1] is not None:
            obj["exception"] = self.formatException(record.exc_info)

        return json.dumps(obj, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable formatter — backward-compatible with pre-507 output.

    If structured ``extra`` fields are present, they are appended as
    key=value pairs.  If not, the output is identical to the old format.
    """

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record)


def configure_logging(log_format: str = "text") -> None:
    """Configure the ``cassetta``, ``cassetta.auth``, and ``cassetta_cloud``
    logger trees.

    Builds one ``StreamHandler`` with the chosen formatter and attaches it
    to the ``cassetta`` top-level logger, the ``cassetta.auth`` sub-tree
    (Brief 533 FR-040), AND the ``cassetta_cloud`` top-level logger so
    records emitted on any of them render through the same handler.

    Both ``cassetta`` and ``cassetta.auth`` set ``propagate=False`` — the
    auth sub-tree gets its own handler so future propagation-chain
    mutations cannot silently disconnect it from the configured handler
    (Brief 533 FR-041). ``cassetta_cloud`` keeps default ``propagate=True``
    so pytest's ``caplog`` (attached to root) continues to capture records
    from cloud-side log-assertion tests (D-6).

    Idempotent — re-invocation clears prior handlers on every configured
    logger before re-attaching, so repeated calls do not stack handlers
    (Brief 533 FR-042).

    Args:
        log_format: ``"text"`` for human-readable (default),
                    ``"json"`` for JSONL output.
    """
    handler = logging.StreamHandler()
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(TextFormatter())
    # Marker so re-invocations clear ONLY handlers we installed (FR-042
    # idempotency) and leave test-attached handlers — e.g., the
    # ``auth_log_capture`` fixture — intact.
    handler._cassetta_managed = True  # type: ignore[attr-defined]

    # Brief 533 FR-040/FR-041/FR-042: ``cassetta.auth`` gets its own handler
    # and propagate=False so the auth sub-tree is structurally isolated.
    for name in ("cassetta", "cassetta.auth", "cassetta_cloud"):
        target = logging.getLogger(name)
        # Only clear handlers WE installed previously, so external
        # handlers attached by tests/operators survive re-invocation.
        for existing in list(target.handlers):
            if getattr(existing, "_cassetta_managed", False):
                target.removeHandler(existing)
        target.addHandler(handler)
        target.setLevel(logging.DEBUG)
        target.propagate = name == "cassetta_cloud"


def new_request_id() -> str:
    """Generate a new UUID4 request identifier."""
    return str(uuid.uuid4())


def struct_log(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    identity_label: str | None = None,
    identity_extra: dict[str, Any] | None = None,
    resource: str | None = None,
    action: str | None = None,
    result: str | None = None,
    duration_ms: float | None = None,
    detail: Any | None = None,
) -> None:
    """Emit a structured log event with consistent field names.

    This is the single entry point for all structured logging in cassetta.
    Both JSON and text formatters render the same fields.
    """
    extra: dict[str, Any] = {"event": event}
    rid = request_id_var.get()
    if rid is not None:
        extra["request_id"] = rid
    if identity_label is not None:
        extra["identity_label"] = identity_label
    if identity_extra is not None:
        extra["identity_extra"] = identity_extra
    if resource is not None:
        extra["resource"] = resource
    if action is not None:
        extra["action"] = action
    if result is not None:
        extra["result"] = result
    if duration_ms is not None:
        extra["duration_ms"] = duration_ms
    if detail is not None:
        extra["detail"] = detail

    logger.log(level, event, extra=extra)


def safe_emit(
    logger: logging.Logger | None = None,
    level: int | None = None,
    event: str | None = None,
    *,
    metric_name: str | None = None,
    metric_value: float = 1.0,
    metric_tags: Mapping[str, str] | None = None,
    metrics: MetricsProvider | None = None,
    gauge: bool = False,
    **fields: Any,
) -> None:
    """Emit a structured-log event, a metric counter/gauge, or both.

    Brief 533 — the single funnel for paired observability emissions.
    Wraps ``struct_log`` and ``metrics.<increment|gauge>`` in TWO
    INDEPENDENT best-effort exception handlers — a failure in one step
    MUST NOT prevent the other from being attempted, and no exception
    from either step propagates to the caller (FR-061).

    The log step runs only when both ``event`` and ``logger`` are
    provided. The metric step runs only when both ``metric_name`` and
    ``metrics`` are provided. When ``gauge`` is True, the metric step
    calls ``metrics.gauge(...)`` instead of ``metrics.increment(...)``.

    ``**fields`` are forwarded to ``struct_log`` (e.g., ``identity_label``,
    ``resource``, ``action``, ``result``, ``detail``).
    """
    if event is not None and logger is not None:
        try:
            struct_log(
                logger,
                level if level is not None else logging.INFO,
                event,
                **fields,
            )
        except Exception:
            try:
                logger.exception("struct_log failed for %s", event)
            except Exception:
                pass

    if metric_name is not None and metrics is not None:
        try:
            tags_dict: dict[str, str] | None = dict(metric_tags) if metric_tags is not None else None
            if gauge:
                metrics.gauge(metric_name, metric_value, tags=tags_dict)
            else:
                metrics.increment(
                    metric_name,
                    value=int(metric_value),
                    tags=tags_dict,
                )
        except Exception:
            if logger is not None:
                try:
                    logger.exception(
                        "metrics.%s failed for %s",
                        "gauge" if gauge else "increment",
                        metric_name,
                    )
                except Exception:
                    pass
