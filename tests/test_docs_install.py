"""Guard: the documents install this package from the index, and none of them says it is not there.

The package reached PyPI, and two things the documents had said while the repository was private
stopped being true at the same moment.

**They denied the index.** Three documents said there was no package to ``pip install``, and one of
them is written for an agent. An agent following ``docs/AGENT_SETUP.md`` does what it is told, so an
instruction not to use ``pip install`` makes it refuse the install that works. The sentence was true
when it was written and had no way to notice when it stopped being true, which is why the second guard
here reads for the claim rather than for the three sentences that made it.

**They pinned a git tag.** A git install needs a ref, because without one the client follows the
default branch and changes between runs. So every install command named a release, and a generator
kept that name current. The index carries only releases: an unpinned install takes the newest one, and
the newest release is what the documents on the default branch describe. A pinned one has to be bumped
with every release, and it also breaks during each release: the merge places the tag at once while the
upload waits for a reviewer, so for that whole window a documented ``cassetta==X.Y.Z`` names a version
the index does not have yet.

So the first guard does not check that the pin is current. It checks that there is no pin, **whichever
installer the command uses**: ``uv tool install``, ``uvx``, ``pip``, ``pipx``. A rule written for
``uv`` alone would pass a pinned ``pip install`` in the next paragraph.

``CHANGELOG.md`` is outside both guards. Its sections record what was true when each release shipped,
and the one saying that no index carried this package is a correct record of its release.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_DOCS = REPO_ROOT / "docs"
_README = REPO_ROOT / "README.md"
_CHANGELOG = REPO_ROOT / "CHANGELOG.md"


def _package_name() -> str:
    """The distribution name the packaging metadata declares, which is the name the index serves.

    Read rather than restated, for the reason ``tests/test_public_surface.py`` reads the repository
    address: a second literal could go stale without failing anything. These guards would then stop
    recognising the package and pass over every command that installs it.
    """
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return str(project["name"])


# --- reading a document --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_QUOTE_MARKER_RE = re.compile(r"^\s*>\s?")

# A line that opens a block of its own even without a blank line before it: a list item, a heading, a
# table row. Folding across one would join two separate statements into one sentence, and the denial
# rule below reads a sentence.
_BLOCK_START_RE = re.compile(r"^(?:[-*+]\s|\d+[.)]\s|#{1,6}\s|\|)")


@dataclass(frozen=True)
class _Passage:
    """What a reader takes in as a unit: one prose paragraph, or one command inside a fence."""

    path: Path
    line: int
    text: str

    @property
    def where(self) -> str:
        return f"{self.path.relative_to(REPO_ROOT)}:{self.line}"


def _passages(path: Path) -> list[_Passage]:
    """Every prose paragraph and every fenced command in one document, each folded onto one line.

    The folding matters. A sentence wraps wherever the editor wrapped it, and a command wraps wherever
    a backslash continues it. Two of the sentences the denial guard exists for were written across a
    line break, and the one-off install in ``docs/AGENT_SETUP.md`` is a continued command. A per-line
    scan sees the negation on one line and the thing denied on the next, and reports nothing.

    Block-quote markers are dropped first, so a fence or a paragraph inside a quote reads like any other.
    """
    passages: list[_Passage] = []
    collected: list[str] = []
    start = 0
    inside_fence = False

    def flush() -> None:
        text = " ".join(part for part in collected if part)
        if text:
            passages.append(_Passage(path=path, line=start, text=text))
        collected.clear()

    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = _QUOTE_MARKER_RE.sub("", raw).strip()
        if _FENCE_RE.match(line):
            flush()
            inside_fence = not inside_fence
            continue
        if inside_fence:
            if not collected:
                start = lineno
            continued = line.endswith("\\")
            collected.append(line[:-1].strip() if continued else line)
            if not continued:
                flush()
            continue
        if not line or _BLOCK_START_RE.match(line):
            flush()
        if line:
            if not collected:
                start = lineno
            collected.append(line)
    flush()
    return passages


# --- item one: every install command takes the newest release from the index --------------------

# An installer invocation. Runners (`uvx`, `uv tool run`, `pipx run`) take the package as their first
# positional argument, and everything after it belongs to the command they run. Installers take every
# positional argument as a package. The lookbehind keeps `uv pip install` from also being read as a
# second, bare `pip install`.
_INSTALLER_RE = re.compile(
    r"(?<![\w./-])(?:"
    r"(?P<runner>uvx|uv\s+tool\s+run|pipx\s+run)"
    r"|(?P<installer>uv\s+tool\s+install|uv\s+pip\s+install|uv\s+add|pipx\s+install"
    r"|(?:python3?\s+-m\s+)?pip3?\s+install)"
    r")(?![\w-])"
)

# Where a command stops: an inline code span closing, a shell operator or comment, a sentence ending
# in prose, or a dash introducing a comment on the command. A full stop ends a sentence only on its own,
# since `--url ...` is an elision inside a command and not the end of one.
_COMMAND_END_RE = re.compile(r"`|&&|\|\||[|;]|\s#|(?<!\.)\.(?!\.)(?=\s|$)|\s[—–]\s")

# Options naming the package to install, when the argument after a runner is the command to run.
_FROM_OPTIONS = frozenset({"--from", "--spec"})

# Options that take a separate value, so the value is not mistaken for the package.
_VALUE_OPTIONS = frozenset(
    {"--python", "-p", "--with", "-w", "--index", "--index-url", "-i", "--extra-index-url", "--default-index"}
)


@dataclass(frozen=True)
class _InstallCommand:
    """A command that installs this package, and what it asks for by way of the package."""

    passage: _Passage
    command: str
    packages: tuple[str, ...]


def _install_documents() -> list[Path]:
    """Every file under ``docs/``, and ``README.md``: the documents a reader installs from.

    Every file under ``docs/`` rather than every ``*.md``, as the pin guard before this one did: a
    command in a shell snippet installs the same release as a command in prose.
    """
    return sorted(path for path in _DOCS.rglob("*") if path.is_file()) + [_README]


def _tokens(arguments: str) -> list[str]:
    """The arguments as a shell would split them, with the punctuation prose leaves on them removed.

    Prose is not always balanced shell (an apostrophe opens a quote that never closes), so an argument
    string that ``shlex`` refuses is split on whitespace instead.
    """
    try:
        raw = shlex.split(arguments)
    except ValueError:
        raw = arguments.split()
    return [token.strip("\"'").rstrip(".,:;)") for token in raw]


def _package_arguments(tokens: list[str], *, runner: bool) -> list[str]:
    """The arguments in package position, whatever they name."""
    positionals: list[str] = []
    skip_value = False
    for index, token in enumerate(tokens):
        if skip_value:
            skip_value = False
            continue
        option, has_value, value = token.partition("=")
        if option in _FROM_OPTIONS:
            return [value] if has_value else tokens[index + 1 : index + 2]
        if token.startswith("-"):
            skip_value = option in _VALUE_OPTIONS and not has_value
            continue
        if runner:
            return [token]
        positionals.append(token)
    return positionals


def _names_the_package(token: str, name: str) -> bool:
    """Whether an argument asks for this package, as a requirement or as a location.

    As a requirement: ``cassetta``, ``cassetta[server]``, ``cassetta==0.30.1``, ``cassetta@0.30.1``,
    ``cassetta>=0.30``. As a location: a URL or path whose last segment is the package, as in
    ``git+https://github.com/atriensis/cassetta.git@v0.30.0`` or ``dist/cassetta-0.30.1-py3-none-any.whl``.
    A host that begins with the name is neither, so ``http://cassetta.example.com:16001`` passed to
    ``--url`` is not read as an install.
    """
    lowered = token.lower()
    if re.match(rf"{re.escape(name)}(?:$|[\[=<>!~@;\s])", lowered):
        return True
    last = lowered.rstrip("/").rsplit("/", 1)[-1]
    return "/" in lowered and re.match(rf"{re.escape(name)}(?:$|\.git\b|[-_@])", last) is not None


def _takes_the_latest_release(token: str, name: str) -> bool:
    """The package by name alone, extras allowed: what the index answers with its newest release."""
    return re.fullmatch(rf"{re.escape(name)}(?:\[[\w,\s-]*\])?", token, re.IGNORECASE) is not None


def _install_commands() -> list[_InstallCommand]:
    """Every command in the install documents that installs this package, by any installer."""
    name = _package_name()
    found: list[_InstallCommand] = []
    for path in _install_documents():
        for passage in _passages(path):
            for match in _INSTALLER_RE.finditer(passage.text):
                arguments = passage.text[match.end() :]
                end = _COMMAND_END_RE.search(arguments)
                if end is not None:
                    arguments = arguments[: end.start()]
                candidates = _package_arguments(_tokens(arguments), runner=match.group("runner") is not None)
                packages = tuple(token for token in candidates if _names_the_package(token, name))
                if packages:
                    command = " ".join([*match.group(0).split(), *arguments.split()])
                    found.append(_InstallCommand(passage=passage, command=command, packages=packages))
    return found


def test_every_install_command_takes_the_latest_release_from_the_index() -> None:
    """No install command names a version, whichever installer it uses, and at least one exists.

    "Names a version" is read broadly, as anything other than the bare name. A specifier or an ``@``
    picks a release, and a URL or path leaves the index altogether: a git ref is a pin, and a git URL
    with no ref follows a branch rather than a release. Either way the reader is not getting the
    newest release, which is the one the surrounding documents describe.

    The non-vacuity half is scoped to ``docs/``, because that is where installation is documented. A
    ``docs/`` tree that had stopped documenting it would give this guard nothing to object to, and a
    mention of ``pip install`` in ``README.md`` would keep it green over that loss.
    """
    commands = _install_commands()
    name = _package_name()

    in_docs = [command for command in commands if command.passage.path.is_relative_to(_DOCS)]
    assert in_docs, (
        f"found no command under docs/ that installs {name!r}. Either the install instructions have "
        "gone from the documentation, or this guard has stopped recognising them. A guard that "
        "recognises nothing passes over anything"
    )

    pinned = [
        f"{command.passage.where}: `{command.command}` asks for {package!r}"
        for command in commands
        for package in command.packages
        if not _takes_the_latest_release(package, name)
    ]
    assert not pinned, (
        f"an install command asks for something other than {name!r} from the index. The index holds "
        "only releases, so the bare name takes the newest, which is what these documents describe. A "
        "version goes stale with every bump and names a release the index does not carry yet while a "
        "publish waits for approval, and a URL or path is not the index at all:\n  " + "\n  ".join(pinned)
    )


# --- item two: no document denies the index ------------------------------------------------------

# A negation and then, within the same clause, the index, PyPI, `pip install`, or the act of publishing
# this package. Written as that relationship rather than as the sentences it was found in. "Not on PyPI",
# "no package index carries it", "do not `pip install`" and "nothing publishes this package" are all the
# same claim, and the next spelling of it will be none of them.
#
# `publish` counts only when the package is its object. Without that condition, a sentence about Docker
# not publishing a port reads the same as a sentence about this package not being published.
_NEGATION = r"(?:\b(?:no|not|never|nothing|none|nowhere|neither|nor|cannot)\b|\b\w+n['’]t\b)"
_DENIAL_RE = re.compile(
    _NEGATION
    + r"[^.;:!?]{0,40}?"
    + r"(?:\bpypi\b|\bindex\b|\bpip3?\s+install\b|\bpublish\w*[^.;:!?]{0,20}?\b(?:package|client|cassetta)\b)",
    re.IGNORECASE,
)

# The sentences this module was written to remove, as they read in the documents that carried them.
# They are held here as a control on the pattern above, not as a list of banned strings: each one must
# still match, so a looser pattern cannot quietly let one of them back in.
_REMOVED_DENIALS = (
    ("docs/REST_API.md", "**There is no package on PyPI or any other index**, so there is nothing to `pip install`."),
    ("docs/CLIENT_SETUP.md", "**No package index carries Cassetta.** There is no `pip install cassetta` to run"),
    ("docs/AGENT_SETUP.md", "Do not invent a `pip install` line: nothing publishes this package, so one would fail."),
)


def _tracked_documents() -> list[Path]:
    """Every markdown document git tracks, apart from ``CHANGELOG.md``.

    A rule rather than a list. A contributor's guide saying the package is not on the index would be
    this defect in a file no install document names. Tracked rather than globbed, because a draft in a
    working tree is not published.
    """
    listing = subprocess.run(
        ["git", "ls-files", "-z", "*.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return sorted(REPO_ROOT / name for name in listing.split("\0") if name and REPO_ROOT / name != _CHANGELOG)


def test_no_document_denies_the_index() -> None:
    """No document says the package is not on an index, or that ``pip install`` cannot get it.

    Two controls make this more than a scan that happens to find nothing. The pattern has to recognise
    each sentence this rule was written to remove. The scan has to cover every document that carries
    an install command, which is where such a denial would otherwise hide.
    """
    unrecognised = [f"{origin}: {sentence}" for origin, sentence in _REMOVED_DENIALS if not _DENIAL_RE.search(sentence)]
    assert not unrecognised, (
        "the denial pattern no longer recognises a sentence it exists to keep out, so it would pass "
        "that sentence if it came back:\n  " + "\n  ".join(unrecognised)
    )

    documents = _tracked_documents()
    installing = {command.passage.path for command in _install_commands()}
    unscanned = sorted(str(path.relative_to(REPO_ROOT)) for path in installing - set(documents))
    assert installing and not unscanned, (
        "the denial scan does not reach every document that installs the package, and those are the "
        f"documents most likely to say it cannot be installed. Install documents: {len(installing)}, "
        f"unscanned: {unscanned}"
    )

    denials = [
        f"{passage.where}: {passage.text}"
        for path in documents
        for passage in _passages(path)
        if _DENIAL_RE.search(passage.text)
    ]
    assert not denials, (
        "a document says the package cannot be had from an index. It is on PyPI, and a reader who "
        "believes the sentence, an agent above all, refuses the install that works:\n  " + "\n  ".join(denials)
    )
