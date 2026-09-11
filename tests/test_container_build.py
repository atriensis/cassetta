"""Guard: every file the wheel build reads reaches the image's builder stage before it is read.

The image stopped building at 0.30.0, and nothing on the pull request that broke it said so. That
release gave ``[project]`` a ``readme``, for the package's page on the index. The ``Dockerfile``'s
builder stage installs the project with ``uv sync``, which builds a wheel, and the build backend
validates every metadata field on the way — so it went looking for a file the stage had never been
given::

    OSError: Readme file does not exist: README.md

The dependency layer above that step passes ``--no-install-project``, never builds the wheel, and went
on succeeding, which is why the break sat halfway down the build rather than at its first line.
Building an image is the weekly smoke run's job and not the per-change gate's, by the decision written
at the top of ``ci.yml``, so the break reached a tag before anything built one.

**This holds a property, not a filename.** The files are derived from ``pyproject.toml`` the way the
build backend derives them:

* the ``readme`` that ``[project]`` declares — a path, or the ``file`` of a table;
* the licence files — every file ``[project] license-files`` matches, or the backend's own default
  patterns when that key is absent — and the ``file`` of a ``license`` table, if that form is used.

The licence file is the quieter half. A missing readme stops the build; a missing licence file does
not. The backend globs for it, matches nothing, and builds a wheel with no licence in it, so the image
would install this package without the document that says what may be done with it, and every step of
the build would report success.

A file *reaches* the builder when a ``COPY`` in that stage brings it — or a directory containing it —
in before the ``RUN`` that installs the project, and no ``.dockerignore`` pattern excludes it from the
build context. The second clause is not a formality: ``.dockerignore`` excludes ``docs/``, so a readme
moved there, with a ``COPY`` that names it, satisfies the first clause and still fails the build.

This is a text guard, like ``test_smoke_script.py``. Building the image needs Docker and this suite
must not; what is asserted here is that the recipe hands the build what the build reads. That the
image then builds is what ``scripts/smoke.sh`` observes.
"""

from __future__ import annotations

import glob
import json
import posixpath
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]

_DOCKERFILE = REPO_ROOT / "Dockerfile"
_DOCKERIGNORE = REPO_ROOT / ".dockerignore"
_PYPROJECT = REPO_ROOT / "pyproject.toml"

# The backend the default patterns below belong to. They are its knowledge rather than the project's,
# so the guard checks ``pyproject.toml`` still names it: a change of backend would otherwise leave the
# licence files derived by somebody else's rule, and the guard green over the wrong set.
_BACKEND = "hatchling.build"

# What that backend globs the project root for when ``[project]`` declares no ``license-files``, which
# this project does not: its ``license`` is an SPDX expression, and the file is found by these patterns
# alone. Non-recursive and files only, as the backend globs them.
_DEFAULT_LICENSE_FILES = ("LICEN[CS]E*", "COPYING*", "NOTICE*", "AUTHORS*")

# ``uv sync`` as a command anywhere in a ``RUN``. The one that installs the project is the one without
# ``--no-install-project``; the dependency layer's carries it and never builds the wheel.
_UV_SYNC_RE = re.compile(r"(?<![\w-])uv\s+sync(?![\w-])")
_NO_INSTALL_PROJECT = "--no-install-project"


@dataclass(frozen=True)
class _Instruction:
    """One Dockerfile instruction: the line it starts on, its keyword, and its arguments as one line."""

    line: int
    keyword: str
    arguments: str


@dataclass(frozen=True)
class _IgnoreRule:
    """One ``.dockerignore`` pattern, normalised, with the line it came from."""

    line: int
    pattern: str
    exception: bool


def _files_the_wheel_build_reads() -> dict[str, str]:
    """Each repository file the build backend reads while building the wheel, and the field that makes it.

    Derived from ``pyproject.toml`` by the backend's own rules, so a readme renamed or moved, or a
    licence file added, is followed without this guard being edited.
    """
    pyproject = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    backend = pyproject["build-system"]["build-backend"]
    assert backend == _BACKEND, (
        f"pyproject.toml now builds with {backend!r}, and this guard derives the licence files by the "
        f"default patterns of {_BACKEND!r}. Teach it the new backend's rule before trusting it"
    )
    project = pyproject["project"]
    read: dict[str, str] = {}

    readme = project.get("readme")
    if isinstance(readme, str):
        read[posixpath.normpath(readme)] = "`[project] readme`"
    elif isinstance(readme, dict) and "file" in readme:
        read[posixpath.normpath(readme["file"])] = "`[project] readme.file`"

    licence = project.get("license")
    if isinstance(licence, dict) and "file" in licence:
        read[posixpath.normpath(licence["file"])] = "`[project] license.file`"

    declared = "license-files" in project
    for pattern in project.get("license-files", _DEFAULT_LICENSE_FILES):
        origin = f"`[project] license-files` {pattern!r}" if declared else f"the backend's licence pattern {pattern!r}"
        for match in sorted(glob.glob(pattern, root_dir=REPO_ROOT)):
            if (REPO_ROOT / match).is_file():
                read.setdefault(PurePosixPath(match).as_posix(), origin)
    return read


def _instructions(text: str) -> list[_Instruction]:
    """The Dockerfile's instructions, with continuations joined and comment lines dropped."""
    instructions: list[_Instruction] = []
    pending: list[str] = []
    start = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        # Skipped inside a continuation as well as outside one: the parser drops comment lines before
        # it joins the lines around them.
        if not stripped or stripped.startswith("#"):
            continue
        if not pending:
            start = number
        continued = stripped.endswith("\\")
        pending.append(stripped[:-1].strip() if continued else stripped)
        if continued:
            continue
        keyword, _, arguments = " ".join(pending).partition(" ")
        instructions.append(_Instruction(start, keyword.upper(), arguments.strip()))
        pending = []
    return instructions


def _project_install(instructions: list[_Instruction]) -> tuple[int, int]:
    """The builder stage's ``FROM`` and the ``RUN`` in it that installs the project, as indexes.

    Found by what the ``RUN`` does rather than by what the stage is called: the first ``RUN`` that runs
    ``uv sync`` without ``--no-install-project`` is the step that builds the wheel, and the stage it
    sits in is the builder, whatever its name.
    """
    stage: int | None = None
    for index, instruction in enumerate(instructions):
        if instruction.keyword == "FROM":
            stage = index
        elif (
            instruction.keyword == "RUN"
            and _UV_SYNC_RE.search(instruction.arguments)
            and _NO_INSTALL_PROJECT not in instruction.arguments
        ):
            assert stage is not None, f"Dockerfile line {instruction.line} runs before any FROM"
            return stage, index
    raise AssertionError(
        f"no RUN in the Dockerfile installs the project with a `uv sync` lacking `{_NO_INSTALL_PROJECT}`, "
        "so there is no build step to hold the files against. If the image installs the project some "
        "other way now, this guard has to learn that way: it must not pass over a step it cannot find"
    )


def _context_sources(arguments: str) -> list[str]:
    """What a ``COPY`` takes from the build context: every argument but the destination.

    Nothing for ``COPY --from=…``, which copies out of another stage or image rather than out of the
    repository. Shell and JSON forms both, with any leading ``--flag`` set aside.
    """
    flags: list[str] = []
    rest = arguments
    while rest.startswith("--"):
        flag, _, rest = rest.partition(" ")
        flags.append(flag)
        rest = rest.lstrip()
    if any(flag.startswith("--from=") for flag in flags):
        return []
    tokens = json.loads(rest) if rest.startswith("[") else rest.split()
    return [str(token) for token in tokens[:-1]]


def _self_and_parents(path: str) -> list[PurePosixPath]:
    """``docs/a/b.md`` → ``docs/a/b.md``, ``docs/a``, ``docs``: the path and every directory above it."""
    target = PurePosixPath(path)
    return [target, *(parent for parent in target.parents if parent != PurePosixPath("."))]


def _brings(source: str, path: str) -> bool:
    """Whether a ``COPY`` source brings ``path`` in: the file itself, a directory above it, or a wildcard."""
    pattern = posixpath.normpath(source).lstrip("/")
    if pattern in ("", "."):
        return True
    return any(candidate.full_match(pattern) for candidate in _self_and_parents(path))


def _copies_of(path: str, instructions: list[_Instruction]) -> list[_Instruction]:
    """The ``COPY`` instructions among ``instructions`` that bring ``path`` in from the build context."""
    return [
        instruction
        for instruction in instructions
        if instruction.keyword == "COPY"
        and any(_brings(source, path) for source in _context_sources(instruction.arguments))
    ]


def _ignore_rules(text: str) -> list[_IgnoreRule]:
    """``.dockerignore``'s patterns, normalised the way Docker reads them.

    A line beginning ``#`` is a comment, the rest are trimmed, a leading ``!`` makes an exception, and
    the pattern is cleaned — so ``docs/`` is the pattern ``docs`` — with any leading ``/`` dropped.
    """
    rules: list[_IgnoreRule] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if raw.startswith("#"):
            continue
        pattern = raw.strip()
        if not pattern:
            continue
        exception = pattern.startswith("!")
        if exception:
            pattern = pattern[1:].strip()
        pattern = posixpath.normpath(pattern)
        if len(pattern) > 1:
            pattern = pattern.lstrip("/")
        rules.append(_IgnoreRule(number, pattern, exception))
    return rules


def _excluding_rule(path: str, rules: list[_IgnoreRule]) -> _IgnoreRule | None:
    """The ``.dockerignore`` rule that leaves ``path`` out of the build context, or ``None``.

    Docker matches a pattern against the path and against every directory above it, so ``docs``
    excludes ``docs/CONFIG.md``; and the last rule that matches decides, so a later ``!`` exception
    brings a file back.
    """
    candidates = _self_and_parents(path)
    decided: _IgnoreRule | None = None
    for rule in rules:
        if any(candidate.full_match(rule.pattern) for candidate in candidates):
            decided = None if rule.exception else rule
    return decided


def test_every_file_the_wheel_build_reads_reaches_the_builder() -> None:
    """Every file the backend reads is copied into the builder before the project is installed, and kept.

    Each file is reported with the ``pyproject.toml`` field that makes the backend read it, and with
    what is wrong: a ``.dockerignore`` line that leaves it out of the build context, a ``COPY`` that
    arrives only after the ``RUN`` that needed it, or no ``COPY`` at all.
    """
    read = _files_the_wheel_build_reads()
    # Non-vacuity: a derivation that stopped recognising the manifest would find nothing to hold.
    assert read, "pyproject.toml yields no file the build backend reads, so the derivation is reading the wrong shape"

    instructions = _instructions(_DOCKERFILE.read_text(encoding="utf-8"))
    stage, install = _project_install(instructions)
    stage_end = next(
        (index for index in range(install + 1, len(instructions)) if instructions[index].keyword == "FROM"),
        len(instructions),
    )
    before = instructions[stage + 1 : install]
    after = instructions[install + 1 : stage_end]
    rules = _ignore_rules(_DOCKERIGNORE.read_text(encoding="utf-8"))
    run = instructions[install]

    missing: list[str] = []
    for path, origin in sorted(read.items()):
        reasons: list[str] = []
        rule = _excluding_rule(path, rules)
        if rule is not None:
            reasons.append(f".dockerignore line {rule.line} (`{rule.pattern}`) leaves it out of the build context")
        if not _copies_of(path, before):
            late = _copies_of(path, after)
            reasons.append(
                f"it is copied only at Dockerfile line {late[0].line}, after the project is installed"
                if late
                else "no COPY in the builder stage brings it in before the project is installed"
            )
        if reasons:
            missing.append(f"{path}, read for {origin}: " + "; ".join(reasons))

    assert not missing, (
        f"the build backend reads these files when Dockerfile line {run.line} (`RUN {run.arguments}`) builds "
        "the wheel, and they do not reach it. A missing readme fails that step; a missing licence file lets "
        "it succeed and installs a distribution with no licence in it. Each must be COPYed into that stage "
        "before that line, and not excluded by .dockerignore:\n  " + "\n  ".join(missing)
    )
