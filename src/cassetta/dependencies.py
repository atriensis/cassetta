"""Shared FastAPI dependencies for route handlers."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from cassetta.defaults.factory import BackendConfig
from cassetta.protocols.access import AccessPolicy
from cassetta.protocols.alias import AliasResolver
from cassetta.protocols.limits import LimitsPolicy
from cassetta.protocols.metrics import MetricsProvider


def get_backends(request: Request) -> BackendConfig:
    """Return the ``BackendConfig`` bundle stashed eagerly by ``create_app``."""
    return request.app.state.backends  # type: ignore[no-any-return]


def get_metrics(
    backends: Annotated[BackendConfig, Depends(get_backends)],
) -> MetricsProvider:
    return backends.metrics_provider


def get_alias_resolver(
    backends: Annotated[BackendConfig, Depends(get_backends)],
) -> AliasResolver:
    return backends.alias_resolver


def get_limits_policy(
    backends: Annotated[BackendConfig, Depends(get_backends)],
) -> LimitsPolicy:
    return backends.limits_policy


def get_access_policy(
    backends: Annotated[BackendConfig, Depends(get_backends)],
) -> AccessPolicy:
    return backends.access_policy
