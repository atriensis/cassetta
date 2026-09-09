"""Guard: the client can be installed without the server.

This distribution contains two things — a four-command HTTP client and a server — and until
``0.28.0`` an install of either was an install of both. The documented way to get the client put 57
packages and 55 MB on the machine, ``uvicorn``, ``starlette``, ``uvloop``, ``watchfiles``,
``websockets`` and ``httptools`` among them, and every person on a team paid it to run
``cassetta upload``.

Nothing under ``src/cassetta/cli/`` ever needed any of that. The leak was **the package root**:
``src/cassetta/__init__.py`` re-exported two names from ``cassetta.defaults.factory``, whose import
chain reaches ``fastapi``, and importing any submodule executes ``__init__.py`` first. So the console
script could not start without FastAPI installed — ``ModuleNotFoundError: No module named 'fastapi'``,
raised before any command was reached.

Two tests, because the defect had two halves and only one of them is where anyone would look:

* :func:`test_the_cli_imports_nothing_from_the_server_stack` — the half people check.
* :func:`test_the_package_root_pulls_in_no_server_module` — the half that was actually broken. A
  future convenience re-export at the root would reintroduce the whole thing in exactly the same way.

**Why a subprocess rather than a static walk of the import graph.** ``pytest`` has already imported
FastAPI by the time any test here runs, so the question cannot be answered in this interpreter. A
clean subprocess under a finder that refuses the server stack reproduces the reported failure
exactly, follows re-exports and module-level side effects for free, and needs no rule about
``if TYPE_CHECKING:`` blocks — which this tree does contain, and which a static walk would have to
either flag as false positives or learn to skip, at which point a real leak hidden in one becomes
invisible to it. A guarded import does not execute; that is the whole question being asked.

**Non-vacuity.** A negative assertion passes when its subject is missing, so the finder is checked in
both directions: every forbidden module must import cleanly *without* it (proving the name is real
and installed here) and must fail *with* it, naming this guard. Without that pair, a typo in
:data:`_FORBIDDEN` would leave two permanently green tests that check nothing.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = REPO_ROOT / "pyproject.toml"

# The server stack, as top-level import names. One list, read by both tests and by the two
# non-vacuity controls: adding a server dependency later means adding it here and nowhere else.
#
# **Written here rather than derived from the `server` extra**, and the reason has inverted since it
# was first written down. It used to be that the extra was too *narrow*: `starlette` was imported by
# four modules under `src/` and declared nowhere, so a list derived from `pyproject.toml` would have
# gone quiet about it. `0.28.1` declared it, along with three others.
#
# The extra is now too *wide* instead. It names **`anyio`**, and every correct client install
# contains `anyio`: `httpx` is a base dependency of this package and declares `anyio` unconditionally
# (no extra, no marker). A derived list would therefore name as forbidden a distribution that a
# right-shaped installation has, which is a false statement about the thing this file exists to
# describe.
#
# What the client must not reach is the server *stack*. What the extra declares is the server
# *install profile*. Those overlap and are not the same set — held apart by
# `test_the_forbidden_list_is_not_the_server_extra` below, rather than by this comment asking the
# next reader to remember.
_FORBIDDEN = ("fastapi", "starlette", "uvicorn", "mcp", "slowapi")

# Said in the refusal so the two controls can tell "this guard refused it" from "it is not installed".
# Those are the same exception with the same type, and confusing them is how the control goes vacuous.
_REFUSAL_MARKER = "refused by the client-install guard"

_PROBE = '''\
import sys

FORBIDDEN = {forbidden!r}


class RefuseServerStack:
    """Answer imports of the server stack as an install without it would: it is not there."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in FORBIDDEN:
            raise ModuleNotFoundError(f"No module named {{fullname!r}} — {marker}", name=fullname)
        return None


sys.meta_path.insert(0, RefuseServerStack())

{statement}
'''


def _probe(statement: str, *, block_server_stack: bool) -> subprocess.CompletedProcess[str]:
    """Run ``statement`` in a clean interpreter, optionally with the server stack made unimportable.

    A fresh process rather than this one: the test session has FastAPI imported already, and
    ``sys.modules`` cannot be un-rung.
    """
    source = (
        _PROBE.format(forbidden=_FORBIDDEN, marker=_REFUSAL_MARKER, statement=statement)
        if block_server_stack
        else statement
    )
    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_cli_imports_nothing_from_the_server_stack() -> None:
    """``cassetta.cli`` imports in an environment that has no server libraries in it."""
    result = _probe("import cassetta.cli", block_server_stack=True)

    assert result.returncode == 0, (
        "importing cassetta.cli reached the server stack, so a client-only install cannot run the "
        "CLI. The client needs httpx, typer and pyjwt; if it now needs a fourth library that is a "
        "finding to report, not a dependency to add. Traceback:\n" + result.stderr
    )


def test_the_package_root_pulls_in_no_server_module() -> None:
    """``import cassetta`` alone imports no server library either.

    This is the regression that produced the whole defect. The root was the leak, not the client:
    every submodule import executes ``__init__.py`` first, so one convenience re-export there put
    FastAPI in front of a client that never asked for it.
    """
    result = _probe("import cassetta", block_server_stack=True)

    assert result.returncode == 0, (
        "importing the cassetta package root reached the server stack. Something in "
        "src/cassetta/__init__.py imports server-side code — most likely a convenience re-export of "
        "a name that already lives where ADR 002 puts it, cassetta.defaults.factory. Import it from "
        "there instead. Traceback:\n" + result.stderr
    )


def _server_extra() -> set[str]:
    """The distributions the ``server`` extra declares, as canonical names.

    Read at run time rather than transcribed. A copy of this list kept here would be a second
    statement of the same fact, and would go stale exactly the way the comment above `_FORBIDDEN`
    did — which is the defect this test was written to retire.
    """
    project = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]
    extras = project.get("optional-dependencies", {})
    assert "server" in extras, "pyproject.toml declares no `server` extra — this guard compares against it"
    return {canonicalize_name(Requirement(spec).name) for spec in extras["server"]}


def test_the_forbidden_list_is_not_the_server_extra() -> None:
    """``_FORBIDDEN`` is not the ``server`` extra, and specifically must not gain ``anyio``.

    The obvious tidy-up for the list above is to stop maintaining it by hand and derive it from
    ``pyproject.toml``. That would be wrong, and it would be wrong in a way no other test here
    notices: every assertion in this file would stay green while the guard started refusing correct
    installations.

    ``anyio`` is the reason. It is declared in the ``server`` extra, and it is also in every install
    of the *client* — ``httpx`` is a base dependency of this package and declares ``anyio``
    unconditionally, with no extra and no marker. A ``_FORBIDDEN`` derived from the extra would
    therefore name as forbidden a distribution that a correct client installation has.

    **Measured, because the obvious version of that sentence is wrong.** Blocking ``anyio`` does
    *not* currently break ``import cassetta.cli``: ``httpx`` is imported at module level but defers
    importing ``anyio`` until an async path needs it. So a derived list would not turn the two guards
    above red — it would sit there being false, and the day ``httpx`` stopped deferring, the failure
    would arrive looking like a defect in this package. Resting on that timing is the same mistake as
    depending on somebody else's dependency graph, which is what
    ``tests/test_declared_dependencies.py`` exists to stop.

    Two assertions, because they fail differently and a wrong change could pass either one alone:

    * the inequality catches the derivation, but would also pass on a ``_FORBIDDEN`` that differs
      only by somebody having *added* ``anyio`` to it;
    * the ``anyio`` clause catches that, but would pass on a list derived from the extra minus
      ``anyio`` — the derivation, done carefully, which is still a list nobody is maintaining.

    The comparison is between import names and distribution names, which coincide for every entry on
    both sides today. That they coincide is what makes the inequality meaningful rather than
    accidental; if a future entry breaks it, the two sets stop being comparable and this test should
    be re-thought rather than patched.
    """
    extra = _server_extra()
    forbidden = {canonicalize_name(name) for name in _FORBIDDEN}

    # Non-vacuity: an empty extra would satisfy the inequality below for the wrong reason.
    assert len(extra) >= 2, f"the `server` extra declares {len(extra)} distribution(s) — too few for this comparison"

    assert forbidden != extra, (
        "`_FORBIDDEN` has become the `server` extra. It must not be derived from pyproject.toml: the "
        "extra declares `anyio`, which a correct client install contains (httpx requires it), so a "
        "derived list refuses installations that are exactly right. The client must not reach the "
        f"server *stack*; the extra describes the server *install profile*.\n  extra: {sorted(extra)}"
    )

    assert "anyio" in extra, (
        "the `server` extra no longer declares `anyio`, so the argument above no longer has its "
        "example. Either the declaration was dropped — which would undeclare something src/ imports "
        "by name — or this guard is now reading the wrong table"
    )

    assert "anyio" not in forbidden, (
        "`anyio` has been added to `_FORBIDDEN`. It belongs in a client install: `httpx` is a base "
        "dependency of this package and requires it unconditionally, so blocking it makes the two "
        "guards above assert that a correct installation is broken"
    )


@pytest.mark.parametrize("module", _FORBIDDEN)
def test_the_forbidden_modules_are_real_and_present(module: str) -> None:
    """Non-vacuity, half one: each forbidden name imports fine when nothing is blocking it.

    A misspelled entry would raise ``ModuleNotFoundError`` on its own, which the guard's own refusal
    is indistinguishable from — leaving a list that blocks nothing and two tests that pass over
    anything. This also states, as an executable fact, that the development environment carries the
    server libraries: ``dev`` implies ``server``, which every gate command in this repository relies
    on.
    """
    result = _probe(f"import {module}", block_server_stack=False)

    assert result.returncode == 0, (
        f"{module!r} does not import in this environment, so listing it as forbidden proves nothing. "
        "Either the name is misspelled in _FORBIDDEN, or `uv sync --extra dev` has stopped installing "
        "the server libraries. Traceback:\n" + result.stderr
    )


@pytest.mark.parametrize("module", _FORBIDDEN)
def test_the_finder_actually_refuses_the_server_stack(module: str) -> None:
    """Non-vacuity, half two: each forbidden name is unimportable once the finder is installed."""
    result = _probe(f"import {module}", block_server_stack=True)

    assert result.returncode != 0, f"{module!r} imported despite the guard, so the guard blocks nothing"
    assert _REFUSAL_MARKER in result.stderr, (
        f"{module!r} failed to import, but not because this guard refused it — so the two tests above "
        "would pass on an environment that simply lacks the library rather than on a clean import "
        f"closure. stderr:\n{result.stderr}"
    )
