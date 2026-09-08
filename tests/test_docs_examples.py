"""Guard: the worked ``curl`` examples under ``docs/`` are examples that complete.

Both defects these guards hold are invisible when the block carrying them is reviewed on its own.

**The manifest one is worse than wrong — it is wrong one call late.** ``POST /uploads`` validates the
manifest it is handed and answers ``201`` to anything well-formed. The archive's entries are checked
against that manifest in *phase 2*, so a manifest naming the archive rather than the file inside it
fails at ``POST /upload/{bundle_path}`` with ``400 manifest_violation / extra_file`` — and the name in
that message is the reader's own file. It points at their payload rather than at the manifest they
copied out of this repository.

**The download one is two lines disagreeing.** ``docs/REST_API.md`` promised a "download-token +
matching identity header (two-factor)" and the command directly below it sent only the token, which
answers ``401``. Each line reads fine; only holding them to each other catches it.

Both guards are phrased against a *relationship* rather than against the strings that happened to be
wrong — the archive is never its own manifest entry; every download command carries the header. A
guard written to ``bundle.tar`` would be green over the next spelling of the same mistake.

Two details of the source text drive the parsing, and neither is optional:

* **The JSON in these blocks is not JSON.** It lives inside a double-quoted shell string, so the file
  reads ``\\"name\\":\\"notes.md\\"``, and a size may be written ``NNN``. ``json.loads`` cannot read
  what a reader copies; unescaping and matching by field is what can.
* **A ``curl`` spans several lines.** They are joined by trailing backslashes, so a per-line scan sees
  the URL and the ``-H`` as separate things and concludes the header is missing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_DOCS = REPO_ROOT / "docs"

# A fence opens or closes a code block. Info strings (```bash) ride on the opening fence and are
# ignored: what matters is the boundary, not the language claimed for it.
_FENCE_RE = re.compile(r"^\s*```")

# The local file streamed as the request body, with the shell quoting a reader may have around it.
_STREAMED_RE = re.compile(r"--data-binary\s+@['\"]?([^\s'\"]+)")

# One manifest entry's name, after shell quote-escaping has been undone.
_NAME_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')


@dataclass(frozen=True)
class _Block:
    """One fenced code block: where it starts, and its lines with continuations folded."""

    path: Path
    line: int
    commands: tuple[str, ...]

    @property
    def where(self) -> str:
        return f"{self.path.relative_to(REPO_ROOT)}:{self.line}"

    @property
    def text(self) -> str:
        return "\n".join(self.commands)


def _markdown_files() -> list[Path]:
    """Every document under ``docs/``, in a stable order."""
    return sorted(_DOCS.rglob("*.md"))


def _fold_continuations(lines: list[str]) -> list[str]:
    """Join lines ending in a backslash into one logical command.

    A shell continuation is a typographic convenience; the command is one command. Folding here is
    what lets a single ``in`` test decide whether a request carries a header, rather than a stateful
    scan that has to remember which command it is halfway through.
    """
    folded: list[str] = []
    pending = ""
    for line in lines:
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            pending += stripped[:-1].rstrip() + " "
            continue
        folded.append(pending + stripped.strip() if pending else stripped)
        pending = ""
    if pending:
        folded.append(pending)
    return folded


def _blocks(path: Path) -> list[_Block]:
    """Every fenced code block in one document."""
    blocks: list[_Block] = []
    inside = False
    start = 0
    collected: list[str] = []

    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if _FENCE_RE.match(line):
            if inside:
                blocks.append(_Block(path=path, line=start, commands=tuple(_fold_continuations(collected))))
                collected = []
            else:
                start = lineno
            inside = not inside
            continue
        if inside:
            collected.append(line)

    return blocks


def _unescape(command: str) -> str:
    """Undo the shell's quote-escaping, so the JSON a reader's shell would build is what is read."""
    return command.replace('\\"', '"')


def _normalised(name: str) -> str:
    """A tar entry name as the server sees it — the CLI strips a leading ``./`` before sending."""
    return name[2:] if name.startswith("./") else name


# --- item 1: the manifest lists what is inside the archive, never the archive -------------------


@dataclass(frozen=True)
class _UploadExample:
    """A worked ``POST /uploads`` example: what it declares, and what it then streams."""

    where: str
    manifest_names: tuple[str, ...]
    streamed: tuple[str, ...]


def _upload_examples() -> list[_UploadExample]:
    """Every fenced block that starts a directed upload session and then streams the bytes."""
    examples: list[_UploadExample] = []

    for path in _markdown_files():
        for block in _blocks(path):
            names: list[str] = []
            for command in block.commands:
                if "/uploads" not in command:
                    continue
                body = _unescape(command)
                # Restricted to the region after the `manifest` key: `name` is a common JSON field
                # and the guard must not pick one up from some other object in the same command.
                marker = body.find('"manifest"')
                if marker == -1:
                    continue
                names.extend(_NAME_RE.findall(body[marker:]))

            if not names:
                continue

            examples.append(
                _UploadExample(
                    where=block.where,
                    manifest_names=tuple(names),
                    streamed=tuple(_STREAMED_RE.findall(block.text)),
                )
            )

    return examples


def test_upload_examples_name_a_tar_member_not_the_archive() -> None:
    """No worked example declares the archive it streams as an entry of its own manifest.

    The manifest describes the files *inside* the tar; the archive is the envelope carrying them.
    Declaring the envelope as its own content is accepted by phase 1 and refused by phase 2, which
    is the whole reason it survived in a published reference: the call that contains the mistake
    succeeds.
    """
    examples = _upload_examples()

    # Non-vacuity. A parser that finds no examples finds no broken ones either, and would stay green
    # if `docs/` moved, if the shell quoting changed, or if the example were simply deleted.
    assert examples, (
        "found no worked `POST /uploads` example under docs/ carrying a manifest. Either the "
        "directed-send recipe has gone from the documentation, or this guard has stopped "
        "recognising it — and a guard that recognises nothing passes over anything"
    )

    offenders: list[str] = []
    for example in examples:
        assert example.streamed, (
            f"{example.where}: a `POST /uploads` example that never streams the bytes. The two "
            "phases belong in one block — the manifest is only checkable against what is uploaded"
        )
        declared = {_normalised(name) for name in example.manifest_names}
        for archive in example.streamed:
            if _normalised(archive) in declared:
                offenders.append(
                    f"{example.where}: the manifest declares {archive!r}, which is the archive the "
                    f"same block streams. Manifest entries are the files inside it "
                    f"(declared: {sorted(declared)})"
                )

    assert not offenders, (
        "a worked example declares its own tar archive as a manifest entry. Phase 1 accepts it and "
        "phase 2 answers 400 `manifest_violation` `extra_file` naming the reader's own file, which "
        "sends them looking at their payload instead of at the manifest they copied:\n  " + "\n  ".join(offenders)
    )


# --- item 2: the download command carries both factors ------------------------------------------


def _download_commands() -> list[tuple[str, str]]:
    """Every ``curl`` under ``docs/`` that fetches a referenced file, as (location, command).

    Scanned over the whole document rather than only inside fenced blocks: a command in prose is
    just as copyable. Restricted to ``curl`` on purpose — an envelope showing a ``files[].url`` is
    not a request, and ``cassetta download`` supplies the header itself from the token's
    ``recipient`` claim, so requiring one of either would be wrong rather than strict.
    """
    commands: list[tuple[str, str]] = []
    for path in _markdown_files():
        lines = path.read_text(encoding="utf-8").splitlines()
        for command in _fold_continuations(lines):
            if "curl" in command and "/download/" in command:
                commands.append((str(path.relative_to(REPO_ROOT)), command.strip()))
    return commands


def test_download_examples_carry_the_identity_header() -> None:
    """Every documented download request sends ``X-Sender``.

    The route is two-factor: the token proves the claim and the header declares who is redeeming it.
    Measured, the request answers 401 without the header and 200 with it — so a command missing it
    is not a terse example, it is one that cannot work.
    """
    commands = _download_commands()

    # Non-vacuity: no commands found means nothing was checked.
    assert commands, (
        "found no `curl` for `GET /download/` anywhere under docs/. The reference download recipe "
        "is how a reader fetches a referenced file; a guard that cannot find it checks nothing"
    )

    missing = [f"{where}: {command}" for where, command in commands if "X-Sender" not in command]

    assert not missing, (
        "a documented download request sends the token but not the identity header it is paired "
        "with. `GET /download/...` is two-factor and answers 401 without `X-Sender`:\n  " + "\n  ".join(missing)
    )
