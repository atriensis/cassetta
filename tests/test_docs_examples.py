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

Nine further guards were added for a second class, found by three independent cold reads of this
repository. Where the first two hold a *command* to what the server does, these hold a *document* to
it: a literal that has gone stale, an invocation printed without the credential it needs, a version
in a sample output, an operator question the documents do not answer. Each is named for what it
protects rather than for the string that happened to be wrong, and each carries a non-vacuity
assertion, because every one of them is a scan that finds nothing when the thing it scans for moves.

Two of the nine reach for the running service rather than restating it. That is the point: the
defect they hold is a document and a service disagreeing, and a test that restates the response is
one more copy to go stale.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
import typer

import cassetta
from cassetta.cli import app as _cli_app

REPO_ROOT = Path(__file__).resolve().parents[1]

_DOCS = REPO_ROOT / "docs"

# ``README.md`` is where a reader starts, and five of the nine findings live in it. It is kept apart
# from ``_markdown_files()`` deliberately: that helper means "the reference documents under docs/",
# and widening it would silently change what the two original guards scan.
_README = REPO_ROOT / "README.md"
_REST_API = _DOCS / "REST_API.md"
_CLIENT_SETUP = _DOCS / "CLIENT_SETUP.md"

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


# --- regions of a document ----------------------------------------------------------------------

# An ATX heading. Matched only outside fenced blocks: a shell comment (`# {"status":"ok"}`) is
# indistinguishable from an H1 by shape, and the quickstart is full of them.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


@dataclass(frozen=True)
class _Region:
    """One heading and the lines directly under it, up to the next heading of *any* level.

    Flat rather than nested on purpose. Three guards below ask "which region says this?", and under
    a nesting definition every answer arrives twice — once for the subsection that says it and once
    for the parent that contains the subsection. Flat regions make "exactly one region answers
    ``docker exec id``" a sentence that means what it says.
    """

    path: Path
    level: int
    heading: str
    line: int
    body: str

    @property
    def where(self) -> str:
        return f"{self.path.relative_to(REPO_ROOT)}:{self.line}"

    @property
    def text(self) -> str:
        return f"{self.heading}\n{self.body}"


def _regions(path: Path) -> list[_Region]:
    """Every heading in one document, with the prose that follows it."""
    lines = path.read_text(encoding="utf-8").splitlines()

    heads: list[tuple[int, int, str]] = []
    inside_fence = False
    for lineno, line in enumerate(lines, start=1):
        if _FENCE_RE.match(line):
            inside_fence = not inside_fence
            continue
        if inside_fence:
            continue
        match = _HEADING_RE.match(line)
        if match:
            heads.append((lineno, len(match.group(1)), match.group(2)))

    regions: list[_Region] = []
    for index, (lineno, level, heading) in enumerate(heads):
        end = heads[index + 1][0] - 1 if index + 1 < len(heads) else len(lines)
        regions.append(_Region(path=path, level=level, heading=heading, line=lineno, body="\n".join(lines[lineno:end])))
    return regions


def _json_samples(text: str) -> list[str]:
    """Every fenced block in ``text`` that parses as a JSON object."""
    samples: list[str] = []
    inside = False
    collected: list[str] = []
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            if inside:
                blob = "\n".join(collected).strip()
                try:
                    parsed = json.loads(blob)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    samples.append(blob)
                collected = []
            inside = not inside
            continue
        if inside:
            collected.append(line)
    return samples


def _fenced_commands(paths: list[Path]) -> list[tuple[str, str]]:
    """Every command a reader can copy out of a fenced block, as (location, command)."""
    commands: list[tuple[str, str]] = []
    for path in paths:
        for block in _blocks(path):
            for command in block.commands:
                commands.append((block.where, command.strip()))
    return commands


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


# --- items 1-2: the section that answers the reader ----------------------------------------------

# The heading has to be the reader's *question*, not our answer — and the question they arrive with
# is about the folder they already have. A heading that merely mentions folders is not it.
_FOLDER_QUESTION_RE = re.compile(r"folder.*\?\s*$", re.IGNORECASE)

_QUICKSTART_HEADING = "Quickstart with Docker"

# The one way this section can do harm. Inboxes separate recipients and do not isolate them — the
# README says so itself, twenty lines further down, and a cold reader quoted that line as a reason to
# stop reading. A claim the reader disproves inside the same document costs more than it buys.
# Matched as stems so that "isolation"/"isolated", "private"/"privacy", "secure"/"security" are all
# one rule.
_FORBIDDEN_STEMS = ("isolat", "privat", "secure", "confidential")


def _folder_question() -> _Region | None:
    """The README region whose heading asks the reader's own question, if it exists."""
    for region in _regions(_README):
        if _FOLDER_QUESTION_RE.search(region.heading):
            return region
    return None


def test_the_readme_says_what_the_project_is_for_before_it_says_how_to_run_it() -> None:
    """The answer to "why not a shared folder" comes before the first ``docker compose up``.

    Three independent cold reads put the same item first: the documents never say what this is for.
    Ordering is the whole finding — an explanation placed after the quickstart is an explanation the
    reader who needed it has already left without.
    """
    quickstart = [region for region in _regions(_README) if region.heading.strip() == _QUICKSTART_HEADING]

    # Non-vacuity: with no quickstart there is no ordering to be wrong about, and this guard would
    # pass over a README that had lost the section it exists to position something against.
    assert len(quickstart) == 1, (
        f"expected exactly one `{_QUICKSTART_HEADING}` heading in README.md, found {len(quickstart)}. "
        "This guard positions the answer *before* the quickstart; without the quickstart it checks nothing"
    )

    region = _folder_question()
    assert region is not None, (
        "README.md carries no section whose heading asks the reader's question about a shared "
        "folder. A reader arrives wanting to know why their synced directory is not enough, and "
        "today the document answers that nowhere — it describes the mechanism and then how to run it"
    )

    assert region.line < quickstart[0].line, (
        f"README.md:{region.line} answers the reader's question, but README.md:{quickstart[0].line} "
        "asks them to run a container first. A reader gives this ten minutes; the reason to want it "
        "has to arrive before the instructions for having it"
    )


def test_the_opening_section_claims_no_isolation() -> None:
    """That section claims nothing this repository disproves further down.

    Under the access policy shipped here any valid key may read any label's inbox. An opening that
    hints otherwise is not a small overstatement — it is the specific sentence a cold reader found,
    checked, and closed the tab over.
    """
    region = _folder_question()
    assert region is not None, (
        "README.md carries no section whose heading asks the reader's question about a shared "
        "folder — so there is nothing here to hold to its one prohibition"
    )

    lowered = region.text.lower()
    offenders = [stem for stem in _FORBIDDEN_STEMS if stem in lowered]

    assert not offenders, (
        f"{region.where}: the section answering the reader claims {offenders}. Inboxes separate "
        "recipients; they do not isolate them, and README.md says so itself further down. The four "
        "reasons this section gives are about addressing, consumption and reach — none of them "
        "needs a claim about who can read what"
    )


# --- items 3-4: what the first ten minutes contradict --------------------------------------------

# A response printed as a shell comment beneath the command that produced it — the shape the
# quickstart uses, and the shape that goes stale without anything reporting it.
_JSON_COMMENT_RE = re.compile(r"^\s*#\s*(\{.*\})\s*$")


def _readme_health_literals() -> list[tuple[int, str]]:
    """Every JSON literal README.md prints as the answer to a ``/health`` call."""
    found: list[tuple[int, str]] = []
    for block in _blocks(_README):
        if not any("curl" in command and "/health" in command for command in block.commands):
            continue
        for command in block.commands:
            match = _JSON_COMMENT_RE.match(command)
            if match:
                found.append((block.line, match.group(1)))
    return found


async def test_the_readme_health_example_matches_the_running_service(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """The quickstart's first command that prints anything prints what the service actually returns.

    ``/health`` answers ``{"status":"ok","dev_mode":false}``; the README claimed ``{"status":"ok"}``.
    The literal was right once and the service moved, which is the failure mode a document cannot
    detect about itself.

    Booted through ``auth_client`` — with a setup token — because that is the posture the quickstart
    puts a reader in (``README.md`` tells them to set one). The dev-mode fixture would compare the
    document against a stack nobody following it is running.

    Parsed objects are compared, never strings: whitespace and key order are not the subject, and a
    missing field is.
    """
    literals = _readme_health_literals()

    # Non-vacuity: no literal found means the block moved, or the comment shape changed, and this
    # guard would sit green over the next stale copy of the same thing.
    assert len(literals) == 1, (
        f"expected exactly one printed `/health` response in README.md, found {len(literals)}. "
        "The quickstart's verification step shows the command and its answer as a shell comment; "
        "a guard that cannot find that answer is not checking it"
    )

    line, literal = literals[0]
    client, _ = auth_client

    response = await client.get("/health")
    assert response.status_code == 200, f"GET /health answered {response.status_code} on a healthy stack"

    assert json.loads(literal) == response.json(), (
        f"README.md:{line} prints {literal}, the service answers "
        f"{json.dumps(response.json(), separators=(',', ':'))}. This is the first command in the "
        "quickstart that prints anything, so it is the first thing a reader can catch us being "
        "wrong about"
    )


# What the documented ``/health`` section has to say, and why each phrase is load-bearing. Held as
# phrases rather than as a paraphrase check because the point is that these four things are *stated*
# — an operator cannot act on a shape nobody wrote down.
_HEALTH_DOC_PHRASES = (
    ("dev_mode", "the field's name, so a reader can grep for it and a client can read it"),
    ("no credential", "that this is the one route that takes none — which is why monitoring can poll it"),
    ("no authentication", "what `true` means: the deployment authenticates nothing at all"),
    ("alert", "the operator-facing consequence — this is the signal that catches an empty setup token"),
)


async def test_the_health_response_shape_is_documented(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """``docs/REST_API.md`` documents what ``/health`` returns, including ``dev_mode``.

    The field is published to anyone who can reach the port and appears in no document. It is not a
    hole — dev mode is "NO AUTHENTICATION AT ALL" by ``.env.example``'s own words, and any single
    probe discloses the same state — but it is the one signal an operator's monitoring can alert on
    to catch a deployment that shipped with an empty ``CASSETTA_SETUP_TOKEN``. Undocumented, that use
    of it does not exist.

    The sample is held to the live response by **key set**, not by value: a documented ``dev_mode``
    is an example of some deployment, and pinning its value would make the document wrong for half
    its readers. Which fields exist is the part that must not drift.
    """
    regions = [region for region in _regions(_REST_API) if "/health" in region.heading]
    assert len(regions) == 1, (
        f"expected exactly one section of docs/REST_API.md documenting `/health`, found {len(regions)}. "
        "The endpoint table lists the route; a route that publishes a field needs a section that says "
        "what comes back"
    )
    region = regions[0]

    samples = _json_samples(region.body)
    assert len(samples) == 1, (
        f"{region.where}: expected exactly one JSON sample in the `/health` section, found {len(samples)}. "
        "The sample is what this guard holds against the running service"
    )

    client, _ = auth_client
    live = (await client.get("/health")).json()

    assert set(json.loads(samples[0])) == set(live), (
        f"{region.where}: the documented `/health` response names {sorted(json.loads(samples[0]))}, "
        f"the service returns {sorted(live)}"
    )

    lowered = region.text.lower()
    missing = [f"{phrase!r} — {why}" for phrase, why in _HEALTH_DOC_PHRASES if phrase not in lowered]

    assert not missing, (
        f"{region.where}: the `/health` section does not say:\n  "
        + "\n  ".join(missing)
        + "\nA field an operator is meant to alert on has to arrive with the reason to"
    )


# --- items 5-6: the printed example, and the version beside it -----------------------------------


def _capabilities_invocations() -> list[tuple[str, str]]:
    """Every ``cassetta capabilities`` command a reader can copy, as (location, command).

    Fenced blocks only. ``docs/REST_API.md`` lists the four subcommands by name in a bullet list, and
    a name in prose is a reference rather than an invocation — requiring a credential on it would be
    nonsense. A fence is what a reader copies out of, which is the population this guard is about.
    (The trade-off is real: an invocation written into prose escapes. Nothing in these documents does
    that today, and the alternative — classifying prose by shape — is the kind of cleverness that
    goes wrong quietly.)
    """
    return [
        (where, command)
        for where, command in _fenced_commands([*_markdown_files(), _README])
        if "cassetta capabilities" in command
    ]


def test_every_documented_capabilities_invocation_carries_a_key() -> None:
    """Every printed ``cassetta capabilities`` sends a credential, because the endpoint demands one.

    ``GET /capabilities`` answers ``401`` without a key. Unlike ``cassetta send``, this command reads
    **no** environment variable — ``--url`` and ``--api-key`` are the only parameters it declares —
    so a reader cannot rescue a keyless example by exporting anything. The example simply fails.

    Phrased over every invocation rather than over the one that was wrong, so the next one somebody
    adds is covered on the day it is added.
    """
    invocations = _capabilities_invocations()

    # Non-vacuity: nothing found means nothing checked.
    assert invocations, (
        "found no `cassetta capabilities` invocation in any fenced block under docs/ or in "
        "README.md. The handshake section is where an operator is shown how to ask a server what it "
        "supports; a guard that cannot find it checks nothing"
    )

    keyless = [f"{where}: {command}" for where, command in invocations if "--api-key" not in command]

    assert not keyless, (
        "a documented `cassetta capabilities` invocation sends no credential. `GET /capabilities` "
        "answers 401 without one, and this command reads no environment variable, so the example "
        "cannot be made to work by the reader:\n  " + "\n  ".join(keyless)
    )


# A version printed inside a sample output. Not an install pin — `scripts/sync-docs-version.py` and
# `make release-check` both match `cassetta.git@vX.Y.Z` and neither can see this shape, which is how
# one line sat two releases behind while three separate checks reported the documents current.
_SERVER_VERSION_RE = re.compile(r"Server version:\s*(\S+)")


def test_no_sample_output_names_a_version_other_than_the_current_one() -> None:
    """No sample output under ``docs/`` prints a ``Server version:`` that is not this release.

    Kept to that one shape deliberately. ``CHANGELOG.md``'s version literals are records of what
    shipped and the install pins have their own guard (``tests/test_docs_version_pins.py``); widening
    this one would either duplicate that guard or start rewriting a history.
    """
    found: list[tuple[str, str]] = []
    for path in _markdown_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = _SERVER_VERSION_RE.search(line)
            if match:
                found.append((f"{path.relative_to(REPO_ROOT)}:{lineno}", match.group(1)))

    # Non-vacuity: a sample output that has gone from the documents cannot be stale, and this guard
    # would report that as agreement.
    assert found, (
        "found no `Server version:` line under docs/. It is printed by `cassetta capabilities`, and "
        "the sample of that output is the one place a version literal hides from every other check"
    )

    declared = cassetta.__version__
    stale = [f"{where}: prints {version}, the release is {declared}" for where, version in found if version != declared]

    assert not stale, (
        "a sample output names a release this repository is no longer on. Nothing else looks at this "
        "shape — `scripts/sync-docs-version.py` rewrites install pins only — so it goes stale "
        "silently and stays that way:\n  " + "\n  ".join(stale)
    )


# --- item 7: the command a person types ----------------------------------------------------------


def _send_parameters() -> list[tuple[str, tuple[str, ...]]]:
    """Every parameter ``cassetta send`` declares, as (name, spellings).

    Read off the Click object rather than the source. A list transcribed from ``send.py`` is a second
    copy of the interface and goes stale the same way the documentation does; asking the command
    means a flag added later turns this red on the day it is added, which is the point of the guard.
    """
    group = typer.main.get_command(_cli_app)
    commands = getattr(group, "commands", None)
    assert commands is not None, "the `cassetta` CLI is no longer a command group — this guard reads its subcommands"

    send = commands["send"]
    return [(str(param.name), tuple(param.opts) + tuple(param.secondary_opts)) for param in send.params]


def test_every_option_of_cassetta_send_is_documented() -> None:
    """The ``cassetta send`` reference names every parameter the command actually has.

    ``send`` is the one command a person types, and it had no reference: half a sentence in
    ``docs/REST_API.md`` and a note in ``docs/CONFIG.md`` about where its URL and key come from. No
    flag list, no example.

    Matched with a word boundary because ``--to`` is a prefix of ``--token``, which appears in the
    ``cassetta upload`` reference immediately above — a plain substring test would call ``--to``
    documented by the neighbour's example.
    """
    regions = [region for region in _regions(_CLIENT_SETUP) if "cassetta send" in region.heading]
    assert len(regions) == 1, (
        f"expected exactly one section of docs/CLIENT_SETUP.md documenting `cassetta send`, found "
        f"{len(regions)}. It sits beside `Using cassetta upload` so a reader meets both legs of the "
        "two-phase flow together"
    )
    region = regions[0]

    parameters = _send_parameters()
    assert parameters, "`cassetta send` declares no parameters — this guard would then hold nothing"

    undocumented = [
        f"{name} ({'/'.join(spellings)})"
        for name, spellings in parameters
        if not all(re.search(rf"{re.escape(spelling)}\b", region.text) for spelling in spellings)
    ]

    assert not undocumented, (
        f"{region.where}: `cassetta send` declares parameters its reference does not mention: "
        + ", ".join(undocumented)
        + ". The reference is read off the command, so a flag added later is undocumented until "
        "somebody writes it down"
    )


# --- items 8-9: the two operator notes -----------------------------------------------------------


def test_the_backup_instructions_name_the_macos_bind_mount_case() -> None:
    """The backup section says who owns the bind mounts, on both platforms it is run on.

    On Linux the container chowns ``./data`` and ``./data.keys`` to uid 1001 during its one
    privileged moment, so a copy still reads them but writing back by hand needs elevation. On Docker
    Desktop for macOS the host side keeps the host user's ownership and no ``sudo`` is involved. The
    instructions said neither, and "no extra steps required" is true only on one of them.
    """
    regions = [region for region in _regions(_README) if "backup" in region.heading.lower()]
    assert len(regions) == 1, (
        f"expected exactly one README.md section about backups, found {len(regions)}. It is where an "
        "operator is told to copy the data directories across, so it is where ownership belongs"
    )
    region = regions[0]
    lowered = region.text.lower()

    missing = [
        f"{token!r} — {why}"
        for token, why in (
            ("1001", "the Linux case: the uid the container hands the directories to on first start"),
            ("macos", "the other platform this is run on, where that does not happen"),
            ("sudo", "what the reader is deciding about when they read this"),
        )
        if token not in lowered
    ]
    assert not missing, (
        f"{region.where}: the backup instructions do not name:\n  "
        + "\n  ".join(missing)
        + "\nAn operator copying these directories meets one of two ownership stories and the "
        "section has to say which"
    )

    assert re.search(r"no\s+`?sudo", lowered), (
        f"{region.where}: the backup section names macOS and `sudo` but never says that no `sudo` is "
        "needed there. Naming both without joining them leaves the reader assuming the Linux case"
    )


def test_the_container_docs_answer_docker_exec_id() -> None:
    """The documents answer the first command an auditor runs.

    ``docker exec <container> id`` says ``uid=0(root)``, which reads as contradicting this project's
    claim that the service runs unprivileged. It does not: the image carries no ``USER`` line so the
    entrypoint can correct bind-mount ownership and then drop for good, and ``docker top`` shows
    uvicorn as 1001. That is written in ``Dockerfile``'s comments, where nobody auditing a running
    container is looking.
    """
    regions = [region for region in _regions(_README) if "docker exec" in region.body]
    assert len(regions) == 1, (
        f"expected exactly one README.md section answering `docker exec`, found {len(regions)}. One "
        "answer, in one place — an auditor who finds two has to work out which is current"
    )
    region = regions[0]
    lowered = region.text.lower()

    missing = [
        f"{token!r} — {why}"
        for token, why in (
            ("root", "what the command actually answers, said before it is explained away"),
            ("1001", "who the service process is, which is the claim being defended"),
            ("docker top", "how to check it, rather than being asked to take our word"),
        )
        if token not in lowered
    ]

    assert not missing, (
        f"{region.where}: the section answering `docker exec` does not name:\n  "
        + "\n  ".join(missing)
        + "\nAn auditor reaches for `docker exec id` first; the answer it gives needs the sentence "
        "that makes it not a contradiction"
    )

    # Asserted as a relationship rather than as the word `USER`, which is too common in prose to
    # mean anything on its own: the section has to say the image declares *no* such line, because
    # that absence is the entire reason `exec` lands as root.
    assert re.search(r"no\s+`?user`?\s+line", lowered), (
        f"{region.where}: the section names root and the service uid but never says the image "
        "carries no `USER` line. Without that, `docker exec` landing as root reads as a defect "
        "rather than as the cost of correcting bind-mount ownership at start-up"
    )
