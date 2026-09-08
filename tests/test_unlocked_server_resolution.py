"""The only check here that exercises what an installing user actually gets.

Every other gate in this repository runs against `uv.lock`, which pins `mcp==1.27.0`. A stranger
running `pip install "cassetta[server]"` has no lock file: they get whatever the index resolves
today. Until `0.28.1` that was `mcp 2.2.0`, where `FastMCP` was renamed to `MCPServer` and
`mcp.server.fastmcp` — which `src/cassetta/mcp_server.py` imports — no longer exists. Resolution
succeeded, the server would not start, and **nothing in this repository could tell**: the suite was
green, and always had been.

So the check has to leave the pinned world. It builds a throwaway virtual environment, installs this
working tree's `cassetta[server]` into it with the lock file out of the picture, and imports
`cassetta.app` there.

**`--no-config` is what makes it mean anything.** `uv` discovers `[tool.uv]` settings from the
working directory upward, and this project's `pyproject.toml` carries a `constraint-dependencies`
block pinning every distribution. If that block applied, the resolve would reproduce the lock and the
test would have passed on the broken tree — a gate that proves nothing. Two independent defences:
`--no-config` on both `uv` invocations, and a working directory outside the project so there is
nothing to discover in the first place.

**Marked `network`, deselected by default.** It reaches the public index and downloads some fifty
wheels. `addopts` carries `-m 'not slow and not network'`, so ordinary runs stay offline; ask for it
by name:

    uv run pytest -q -m network tests/test_unlocked_server_resolution.py

**It answers a different question every day, and that is the point.** Its job is to prove that this
tree, resolved fresh, produces an importable server — not to freeze today's version numbers. A
failure for a reason other than `mcp` is a finding to report, never a licence to add another cap to
make it green: a cap without evidence costs every consumer and protects nobody.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Never a literal path: this file's own location is the only thing that knows where the tree is.
PROJECT_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.network


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


def test_an_unlocked_server_install_produces_an_importable_server() -> None:
    """`cassetta[server]` resolved from the index, without the lock, imports `cassetta.app`."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("`uv` is not on PATH; this check builds a virtual environment with it")

    # `mkdtemp` rather than the `tmp_path` fixture: a virtual environment writes its own path into
    # every console-script shebang, and pytest's per-test directories are deep enough for the
    # platform limit to truncate them.
    work = Path(tempfile.mkdtemp(prefix="cassetta-unlocked-"))
    try:
        venv = work / "venv"
        # The interpreter running this suite satisfies `requires-python` by construction, so the
        # check cannot fail over interpreter discovery.
        created = _run([uv, "venv", "--no-config", "--python", sys.executable, str(venv)], cwd=work)
        assert created.returncode == 0, f"could not build a virtual environment:\n{created.stderr}"

        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        installed = _run(
            [uv, "pip", "install", "--no-config", "--python", str(python), f"{PROJECT_ROOT}[server]"],
            cwd=work,
        )
        assert installed.returncode == 0, (
            "resolving `cassetta[server]` from the index failed. If the conflict names `mcp`, the "
            "cap in `[project.optional-dependencies] server` is doing its job and something else in "
            "the environment wants 2.x. Any other conflict is a finding to report, not a cap to "
            f"add.\n{installed.stderr}"
        )

        imported = _run([str(python), "-c", "import cassetta.app"], cwd=work)
        assert imported.returncode == 0, (
            "`cassetta[server]` installed from the index and then failed to import, which is exactly "
            "the defect 0.28.1 fixed: resolution succeeds and the server cannot start. If this names "
            "`mcp.server.fastmcp`, the upper bound on `mcp` has been lost. If it names something "
            "else, a dependency has moved under us — say what broke and which version produced it, "
            "and do not reach for another cap to make this green.\n" + imported.stderr
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
