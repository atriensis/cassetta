# Cassetta — single-package container image.
#
# This repository contains one Python package (`src/cassetta`) and one app factory
# (`cassetta.app:create_app`). There is no workspace to select from and no backend to switch on:
# the filesystem backend is the only vendor here, wired by `build_core_defaults` (Constitution VII).
#
# Two stages. The builder resolves the locked dependency set and installs the project into a
# virtualenv; the runtime carries that virtualenv and nothing else — no uv, no build toolchain.

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Dependency layer first, from the lockfile alone: editing a source file must not re-resolve
# and re-download the dependency set. LICENSE is copied because pyproject declares
# `license = "FSL-1.1-ALv2"` and the build backend globs for the file at wheel time.
COPY pyproject.toml uv.lock LICENSE ./
RUN uv sync --locked --no-dev --no-install-project --no-editable

# Project layer.
COPY src ./src
RUN uv sync --locked --no-dev --no-editable


FROM python:3.13-slim

# Unprivileged runtime. /data holds stored files; /data.keys holds the API key store, which is
# kept out of the storage tree so key material never appears in a `/files/` listing.
RUN groupadd --system --gid 1001 cassetta \
 && useradd --system --uid 1001 --gid cassetta --no-create-home cassetta \
 && mkdir -p /data /data.keys \
 && chown cassetta:cassetta /data /data.keys

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    CASSETTA_STORAGE_PATH=/data

VOLUME ["/data", "/data.keys"]
EXPOSE 16001

USER cassetta

# `python:3.13-slim` ships neither curl nor wget, and adding one for a liveness probe would mean
# an apt layer. GET /health answers 200 when healthy and 503 when not; urlopen raises on 503, so
# the exit status follows without any status bookkeeping.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:16001/health', timeout=2)"]

ENTRYPOINT ["uvicorn", "--factory", "cassetta.app:create_app", "--host", "0.0.0.0", "--port", "16001"]
