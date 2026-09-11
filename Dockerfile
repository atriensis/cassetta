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
#
# `--extra server` is on **both** sync lines, and leaving it off either one is the failure this
# comment exists to prevent. Since 0.28.0 the base dependency set is the CLI client alone — three
# libraries, no FastAPI, no uvicorn — and the server lives in an extra that has to be asked for.
# `uv sync` is exact, so the project layer without the flag would *remove* what the dependency layer
# installed. Either omission produces an image that builds successfully, passes every static check,
# and has no server in it: the CMD below cannot start, and nothing says so until the container runs.
COPY pyproject.toml uv.lock LICENSE ./
RUN uv sync --locked --no-dev --extra server --no-install-project --no-editable

# Project layer. README.md is copied here, beside the source, because pyproject declares
# `readme = "README.md"` and the build backend reads it into the wheel's metadata: without it the
# sync below fails with "Readme file does not exist". The dependency layer never builds the project
# and does not need it, and copying it there would re-resolve and re-download the dependency set on
# every README edit. tests/test_container_build.py holds that every file the wheel build reads
# arrives before this sync.
COPY src ./src
COPY README.md ./
RUN uv sync --locked --no-dev --extra server --no-editable


FROM python:3.13-slim

# Unprivileged runtime. /data holds stored files; /data.keys holds the API key store, which is
# kept out of the storage tree so key material never appears in a `/files/` listing.
RUN groupadd --system --gid 1001 cassetta \
 && useradd --system --uid 1001 --gid cassetta --no-create-home cassetta \
 && mkdir -p /data /data.keys \
 && chown cassetta:cassetta /data /data.keys

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    CASSETTA_STORAGE_PATH=/data

VOLUME ["/data", "/data.keys"]
EXPOSE 16001

# The container starts as root and stops being root before the application exists.
#
# There is no `USER` line here, and its absence is deliberate. On Linux a bind mount passes the
# host's ownership through unchanged, so ./data and ./data.keys arrive owned by whoever created them
# — and the application, which is somebody else, cannot write into its own data directory. Declaring
# the user here would leave nothing able to correct that. The entrypoint corrects it instead, in the
# only privileged moment this container has, and then drops for good: see scripts/docker-entrypoint.sh,
# which ends in an unconditional `exec setpriv … --`.
#
# The application is therefore still unprivileged, by mechanism rather than by declaration. Two
# consequences worth knowing before they surprise someone:
#
#   * `docker exec` without `--user` now lands as root, where it used to land as cassetta. Pass
#     `--user cassetta` for a session with the application's own permissions.
#   * The health probe below runs outside the entrypoint — a command in exec form is executed
#     directly — so it carries its own drop. Without it, removing `USER` would silently move a
#     root process into every thirty-second interval for the life of the container. It is declared
#     here and nowhere else: a probe declared on the container replaces this one rather than
#     merging with it, so a second copy in docker-compose.yml would win and would not drop.

# `python:3.13-slim` ships neither curl nor wget, and adding one for a liveness probe would mean
# an apt layer. GET /health answers 200 when healthy and 503 when not; urlopen raises on 503, so
# the exit status follows without any status bookkeeping.
#
# The ids are written out because there is no shell here to resolve the name in. They are the ones
# created a few lines above, in this same file, where a drift between the two is visible at a glance.
#
# --no-new-privs sets the kernel's no_new_privs bit for the probe and anything it spawns, so the
# process cannot regain through a setuid binary the privilege this line has just given up. The
# entrypoint asks for it at both of its own identity changes, for the same reason.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["setpriv", "--reuid=1001", "--regid=1001", "--clear-groups", "--no-new-privs", "--", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:16001/health', timeout=2)"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# The application invocation, unchanged — moved from ENTRYPOINT to CMD so that the entrypoint can
# forward it. A command supplied on the command line still replaces it, exactly as before, and is
# still run unprivileged.
CMD ["uvicorn", "--factory", "cassetta.app:create_app", "--host", "0.0.0.0", "--port", "16001"]
