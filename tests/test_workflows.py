"""Guard: the release machinery has the shape it needs in order to work at all.

A workflow is the one kind of code in this repository that cannot be run before it is merged. It
gets exactly one review, from a reader, and then it runs for real. So the properties that a reader
would have to hold in their head are held here instead.

Three of them.

**Every workflow parses.** Cheap, and the only thing between a mis-indented key and a workflow that
silently never runs.

**The release workflow is *called*, not *triggered*.** This is the one that matters, and it is not
about the two workflows here — it is about the next person. The obvious way to run something when a
release is tagged is `on: push: tags: ["v*"]`. It reads correctly, it reviews correctly, and it never
fires: a tag pushed with `GITHUB_TOKEN` does not start other workflow runs, and `workflow_dispatch`
carries no exception. The failure mode is silence, which is why a reviewer is unlikely to catch it
and why the prohibition is written down as a test. Composition through `workflow_call` sidesteps the
rule entirely and needs no extra credential.

**Nothing publishes.** This repository builds a distribution and keeps it as a build artifact,
stopping one step short of publishing on purpose — the package name is unclaimed and the repository
is private. The assertion turns "we have not published yet" from a fact about the present into a
decision someone has to un-make on the record.

Two things about how this is written.

*Triggers are read through one helper.* PyYAML implements YAML **1.1**, in which the bare token `on`
is a boolean — so the trigger block arrives under the key `True`, not `"on"`. A test that looked up
`"on"` would raise on a valid workflow, and, worse, the *negative* assertion here would pass by
finding nothing. Hence the helper, and hence the non-vacuity check that the lookup finds the `push:`
triggers that really do exist.

*Publishing is looked for in parsed `uses:` and `run:` values, not in raw text.* A text scan would
fire on a comment saying the workflow does not publish, which is a sentence worth being able to
write.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

_WORKFLOWS = REPO_ROOT / ".github" / "workflows"

_TAG_WORKFLOW = _WORKFLOWS / "tag.yml"
_RELEASE_WORKFLOW = _WORKFLOWS / "release.yml"

# ci.yml, smoke.yml, tag.yml, release.yml. Non-vacuity: a scan over an empty directory is green.
_KNOWN_WORKFLOW_COUNT = 4

# Substrings that mean a step hands a distribution to an index. Specific rather than a bare
# "publish", because this repository's workflows say in their own comments that they publish
# nothing, and a guard that fired on saying so would make the sentence unwriteable.
_PUBLISHING_MARKERS = (
    "pypi",
    "twine",
    "uv publish",
    "hatch publish",
    "poetry publish",
    "flit publish",
    "setup.py upload",
)


def _workflow_files() -> list[Path]:
    """Every workflow file, in a stable order."""
    assert _WORKFLOWS.is_dir(), f"{_WORKFLOWS.relative_to(REPO_ROOT)} is missing"
    return sorted(_WORKFLOWS.glob("*.yml"))


def _load(path: Path) -> dict[str, Any]:
    """One workflow, parsed."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path.name} does not parse as a YAML mapping"
    return document


def _triggers(document: dict[str, Any]) -> dict[str, Any]:
    """The `on:` block of a workflow, as a mapping of trigger name to its configuration.

    The key is looked up as both `"on"` and `True`, because PyYAML is a YAML 1.1 parser and bare
    `on` is a boolean there. Every workflow in this repository writes it bare, so in practice the
    block always arrives under `True` — the string lookup is kept for the day one is written quoted.

    A trigger list (`on: [push]`) or a bare string (`on: push`) is normalised to a mapping with
    empty configuration, so callers have one shape to reason about.
    """
    for key in ("on", True):
        if key in document:
            block = document[key]
            if isinstance(block, dict):
                return block
            if isinstance(block, list):
                return {name: None for name in block}
            return {block: None}
    raise AssertionError("workflow declares no `on:` block, so nothing can ever start it")


def _step_commands(node: Any) -> list[str]:
    """Every `uses:` and `run:` value anywhere in a parsed workflow.

    Walked recursively rather than reached through `jobs → steps`, because a reusable-workflow call
    is a `uses:` at *job* level, not inside a step, and a guard that only knew about steps would not
    see it.
    """
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("uses", "run") and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_step_commands(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_step_commands(item))
    return found


def test_every_workflow_parses() -> None:
    """Every `*.yml` under `.github/workflows/` parses, and declares something that starts it."""
    files = _workflow_files()
    assert len(files) >= _KNOWN_WORKFLOW_COUNT, (
        f"found {len(files)} workflow file(s), expected at least {_KNOWN_WORKFLOW_COUNT} — either "
        "workflows have been removed or this guard is reading the wrong directory"
    )

    for path in files:
        document = _load(path)
        assert _triggers(document), f"{path.name} declares an empty `on:` block"


def test_release_workflow_is_called_not_triggered() -> None:
    """The release workflow is reached by a call, and no workflow waits for a tag push.

    The last clause is the point. The first two describe what this repository does today; the third
    describes what it must never do, and it is the one that will still be earning its place long
    after anyone remembers why.
    """
    assert _RELEASE_WORKFLOW.is_file(), f"{_RELEASE_WORKFLOW.name} is missing"
    assert _TAG_WORKFLOW.is_file(), f"{_TAG_WORKFLOW.name} is missing"

    release_triggers = _triggers(_load(_RELEASE_WORKFLOW))
    assert "workflow_call" in release_triggers, (
        f"{_RELEASE_WORKFLOW.name} does not declare `on: workflow_call`, so it cannot be called by "
        f"{_TAG_WORKFLOW.name}. Found: {sorted(str(name) for name in release_triggers)}"
    )

    expected_call = f"./.github/workflows/{_RELEASE_WORKFLOW.name}"
    calls = _step_commands(_load(_TAG_WORKFLOW))
    assert expected_call in calls, (
        f"{_TAG_WORKFLOW.name} does not reach {_RELEASE_WORKFLOW.name} through a "
        f"`uses: {expected_call}` job. A tag pushed with GITHUB_TOKEN starts no workflow runs, so a "
        "release that is not called here does not happen at all"
    )

    # The prohibition, and the non-vacuity that makes it mean something. `push:` triggers do exist
    # here, so a lookup that found none would be broken rather than reassuring — which is exactly
    # what the YAML-1.1 `on`-is-`True` trap produces.
    push_triggers = 0
    offenders = []
    for path in _workflow_files():
        triggers = _triggers(_load(path))
        if "push" not in triggers:
            continue
        push_triggers += 1
        configuration = triggers["push"]
        if isinstance(configuration, dict) and "tags" in configuration:
            offenders.append(f"{path.name}: push.tags = {configuration['tags']!r}")

    assert push_triggers >= 1, (
        "no workflow was found to declare a `push:` trigger, but several do — the trigger lookup is "
        "broken, and the assertion below is passing over anything"
    )

    assert not offenders, (
        "a workflow waits for a tag push. It will never run: a tag pushed with GITHUB_TOKEN does "
        "not start other workflow runs, and the workflow looks correct while never firing. Reach "
        "the release workflow with `uses:` instead:\n  " + "\n  ".join(offenders)
    )


def test_no_workflow_publishes() -> None:
    """No workflow hands a distribution to a package index.

    This repository builds one and keeps it as a build artifact. Publishing is a separate decision
    with a name to claim and a credential to configure, and it should arrive as an edit somebody
    made on purpose — visible in a diff, not discovered in a run.
    """
    offenders = []
    for path in _workflow_files():
        for command in _step_commands(_load(path)):
            lowered = command.lower()
            for marker in _PUBLISHING_MARKERS:
                if marker in lowered:
                    offenders.append(f"{path.name}: {marker!r} in {command.strip().splitlines()[0][:80]!r}")

    assert not offenders, (
        "a workflow publishes this package. That is a deliberate step this repository has not taken "
        "— the name is unclaimed and nothing carries the package — so if it is being taken now, "
        "this guard is the thing to change first:\n  " + "\n  ".join(offenders)
    )
