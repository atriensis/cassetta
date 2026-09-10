"""Guard: the release machinery has the shape it needs in order to work at all.

A workflow is the one kind of code in this repository that cannot be run before it is merged. It
gets exactly one review, from a reader, and then it runs for real. So the properties that a reader
would have to hold in their head are held here instead.

Six of them.

**Every workflow parses.** Cheap, and the only thing between a mis-indented key and a workflow that
silently never runs.

**The release workflow is *called*, not *triggered*.** This is the one that matters, and it is not
about the two workflows here — it is about the next person. The obvious way to run something when a
release is tagged is `on: push: tags: ["v*"]`. It reads correctly, it reviews correctly, and it never
fires: a tag pushed with `GITHUB_TOKEN` does not start other workflow runs, and `workflow_dispatch`
carries no exception. The failure mode is silence, which is why a reviewer is unlikely to catch it
and why the prohibition is written down as a test. Composition through `workflow_call` sidesteps the
rule entirely and needs no extra credential.

**One job publishes, and it publishes what the release built.** The distribution reaches an index
from exactly one place: a job in ``tag.yml`` that runs after ``release.yml`` has built the
distribution and checked it against the tag, and that hands over those bytes rather than building
its own. Not from ``release.yml`` itself, although that is where the artifact is made. It is reached
only by a call, a called workflow can narrow the token its caller grants but never widen it, and the
index documents a reusable workflow as unsupported for trusted publishing. So the workflow registered
with the index is ``tag.yml``.

**A person decides.** The publishing job is bound to a deployment environment whose required reviewer
is the gate, and the gate is outside this repository on purpose. The job carries no condition of its
own: a condition that evaluates false skips the job and the run goes green.

**Nothing is stored.** The job authenticates by exchanging a short-lived OIDC token. It asks for that
permission itself, the workflow around it does not grant it, and it holds no password and reads no
secret.

**The tag lock is released before anything waits on a person.** ``tag.yml`` serializes the one
decision that must not race, whether a tag is missing, with a concurrency group. At workflow level
that group would stay held for as long as a reviewer takes. GitHub keeps one pending run per group and
cancels it when the next one arrives, so a merge landing during the wait could lose its tag run. On
the ``tag`` job the group is held for seconds.

Two things about how this is written.

*Triggers are read through one helper.* PyYAML implements YAML **1.1**, in which the bare token `on`
is a boolean — so the trigger block arrives under the key `True`, not `"on"`. A test that looked up
`"on"` would raise on a valid workflow, and, worse, the *negative* assertion here would pass by
finding nothing. Hence the helper, and hence the non-vacuity check that the lookup finds the `push:`
triggers that really do exist.

*Publishing is looked for in parsed `uses:` and `run:` values, not in raw text.* A text scan would
fire on a comment explaining why ``release.yml`` does not publish, which is a sentence worth being
able to write.
"""

from __future__ import annotations

import re
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
# "publish", because this repository's workflows talk about publishing in their own comments and
# step names, and a guard that fired on saying so would make the sentence unwriteable.
_PUBLISHING_MARKERS = (
    "pypi",
    "twine",
    "uv publish",
    "hatch publish",
    "poetry publish",
    "flit publish",
    "setup.py upload",
)

# The job in tag.yml that decides whether a tag is missing, and the lock it holds while it decides.
_TAG_JOB = "tag"
_TAG_LOCK = {"group": "tag-on-merge", "cancel-in-progress": False}

# The deployment environment the publishing job waits on. Not a label: its required reviewer is the
# gate, and the index checks this name in the token's claim. Renaming it here without renaming the
# registration on the index fails the first approved publish, after the approval.
_PUBLISHING_ENVIRONMENT = "pypi"

# Where the publish action reads distributions from when it is given no `packages-dir`.
_PUBLISH_ACTION_DEFAULT_DIR = "dist"

# Mapping keys that carry a stored credential into a job. `password` is the publish action's input
# for an API token; a key named like a token is the same thing by another name. Read outside the
# job's `permissions:`, where `id-token` is the permission this job is meant to have.
_CREDENTIAL_KEY_FRAGMENTS = ("password", "token")

# `${{ inputs.version }}` in a called workflow, so a value it names can be resolved through the
# caller's `with:`.
_INPUT_REFERENCE = re.compile(r"\$\{\{\s*inputs\.([\w-]+)\s*\}\}")


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


def _jobs(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The `jobs:` block of a workflow, as a mapping of job id to its configuration."""
    jobs = document.get("jobs")
    assert isinstance(jobs, dict) and jobs, "workflow declares no jobs"
    return {str(job_id): job for job_id, job in jobs.items() if isinstance(job, dict)}


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    """The steps of one job. A job that calls a reusable workflow has none."""
    return [step for step in job.get("steps") or [] if isinstance(step, dict)]


def _needs(job: dict[str, Any]) -> list[str]:
    """The jobs one job waits for. `needs:` may be a single id or a list."""
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else [str(job_id) for job_id in needs]


def _publishing_commands(node: Any) -> list[str]:
    """The `uses:` and `run:` values under ``node`` that hand a distribution to an index."""
    return [
        command for command in _step_commands(node) if any(marker in command.lower() for marker in _PUBLISHING_MARKERS)
    ]


def _publishing_jobs() -> list[tuple[Path, str, dict[str, Any]]]:
    """Every job, in every workflow, that hands a distribution to an index.

    Non-vacuity is asserted here rather than by each caller: every guard that reads this list is
    about the job that publishes, and there is one, so an empty list means the markers stopped
    recognising it.
    """
    found = [
        (path, job_id, job)
        for path in _workflow_files()
        for job_id, job in _jobs(_load(path)).items()
        if _publishing_commands(job)
    ]
    assert found, (
        f"no job in any workflow hands a distribution to an index, but {_TAG_WORKFLOW.name} has one. "
        "The publishing markers no longer recognise it, and every guard reading this list is passing "
        "over nothing"
    )
    return found


def _grants_id_token(permissions: Any) -> bool:
    """Whether a `permissions:` value lets a job mint an OIDC token.

    The mapping form grants it by name; the shorthand `write-all` grants it with everything else.
    """
    if isinstance(permissions, dict):
        return permissions.get("id-token") == "write"
    return permissions == "write-all"


def _entries(node: Any, trail: str = "") -> list[tuple[str, str, Any]]:
    """Every mapping entry and list item under ``node``, as (where, key, value).

    A list item has an empty key: it can still carry a value worth reading.
    """
    found: list[tuple[str, str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            where = f"{trail}.{key}" if trail else str(key)
            found.append((where, str(key), value))
            found.extend(_entries(value, where))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            where = f"{trail}[{index}]"
            found.append((where, "", item))
            found.extend(_entries(item, where))
    return found


def _concurrency_group(value: Any) -> str | None:
    """The group a `concurrency:` value names, lower-cased — GitHub compares group names that way."""
    group = value.get("group") if isinstance(value, dict) else value
    return str(group).lower() if group else None


def _artifact_the_release_uploads(caller: dict[str, Any]) -> str:
    """The name ``release.yml`` uploads its build under, as it reads in the run that calls it.

    The upload names the artifact through the called workflow's ``inputs``, and the calling job
    supplies them in ``with:``. Resolving one through the other gives the name a download in the
    calling run has to ask for.
    """
    names = [
        (step.get("with") or {}).get("name")
        for job in _jobs(_load(_RELEASE_WORKFLOW)).values()
        for step in _steps(job)
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert len(names) == 1 and isinstance(names[0], str), (
        f"expected {_RELEASE_WORKFLOW.name} to upload exactly one named artifact, the distribution it built "
        f"and checked. Found: {names!r}"
    )

    passed = caller.get("with") or {}
    unresolved = sorted(set(_INPUT_REFERENCE.findall(names[0])) - set(passed))
    assert not unresolved, (
        f"{_RELEASE_WORKFLOW.name} names its artifact through input(s) the calling job does not pass: {unresolved}"
    )
    return _INPUT_REFERENCE.sub(lambda match: str(passed[match.group(1)]), names[0])


def _normalised_dir(path: Any) -> str:
    """A directory as a workflow writes it, without the spellings that name the same place."""
    text = str(path).strip().rstrip("/")
    return text[2:] if text.startswith("./") else text or "."


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


def test_only_the_tag_workflow_hands_a_distribution_to_an_index() -> None:
    """One job publishes: the one in ``tag.yml`` that runs after the release is built, handing over what was built.

    This replaces a guard that said no workflow publishes. It was right until the repository decided
    otherwise, and it asked for that decision to arrive as an edit somebody made on purpose. This is
    the edit, so the guard now names the one place publishing happens. A second place is a diff
    somebody has to explain, rather than a job somebody notices on the index.

    The place is ``tag.yml`` and not ``release.yml``, although that is where the artifact is made.
    ``release.yml`` is reached only by a call, and a called workflow can narrow the token its caller
    grants but never widen it. The index, separately, documents a reusable workflow as unsupported for
    trusted publishing. A publishing step there would either be refused its token or run in the
    configuration the index disclaims.

    "What was built" is three facts, because each can break alone and each would surface only after
    a person approved the run. The job needs the release. It checks nothing out, so there is no tree
    to build a second distribution from. And it downloads the artifact ``release.yml`` uploads, by that
    artifact's name, into the directory the publish action reads.
    """
    publishing = _publishing_jobs()

    elsewhere = sorted(
        f"{path.name} → {job_id}: {command.strip().splitlines()[0][:80]!r}"
        for path, job_id, job in publishing
        if path != _TAG_WORKFLOW
        for command in _publishing_commands(job)
    )
    assert not elsewhere, (
        f"a workflow other than {_TAG_WORKFLOW.name} hands a distribution to an index. Publishing happens "
        f"in one place, the job in {_TAG_WORKFLOW.name} that runs after the release is built and checked; "
        "anywhere else it publishes something nobody checked against the tag:\n  " + "\n  ".join(elsewhere)
    )

    in_tag = sorted(job_id for path, job_id, _ in publishing if path == _TAG_WORKFLOW)
    assert len(in_tag) == 1, (
        f"expected exactly one job in {_TAG_WORKFLOW.name} to publish, found {in_tag}. Two publishing jobs "
        "hand the same release to the index twice, and the second upload of a version is refused"
    )
    job = _jobs(_load(_TAG_WORKFLOW))[in_tag[0]]

    release_call = f"./.github/workflows/{_RELEASE_WORKFLOW.name}"
    callers = [
        job_id for job_id, candidate in _jobs(_load(_TAG_WORKFLOW)).items() if candidate.get("uses") == release_call
    ]
    assert len(callers) == 1, f"expected one job in {_TAG_WORKFLOW.name} to call {release_call}, found {callers}"
    caller = callers[0]

    assert caller in _needs(job), (
        f"{_TAG_WORKFLOW.name} → {in_tag[0]} does not need `{caller}`, the job that builds the release and "
        f"checks it against the tag. Without that edge it can start before there is anything to publish. "
        f"Found: needs = {_needs(job)}"
    )

    checkouts = [command for command in _step_commands(job) if command.startswith("actions/checkout@")]
    assert not checkouts, (
        f"{_TAG_WORKFLOW.name} → {in_tag[0]} checks out the repository. It publishes what `{caller}` built; "
        f"with a tree in front of it, a second build is one line away:\n  " + "\n  ".join(checkouts)
    )

    uploaded = _artifact_the_release_uploads(_jobs(_load(_TAG_WORKFLOW))[caller])
    downloads = [step for step in _steps(job) if str(step.get("uses", "")).startswith("actions/download-artifact@")]
    assert len(downloads) == 1, (
        f"{_TAG_WORKFLOW.name} → {in_tag[0]} must fetch the distribution with exactly one "
        f"actions/download-artifact step. Found {len(downloads)}"
    )
    fetched = downloads[0].get("with") or {}
    assert fetched.get("name") == uploaded, (
        f"{_TAG_WORKFLOW.name} → {in_tag[0]} downloads {fetched.get('name')!r}, but {_RELEASE_WORKFLOW.name} "
        f"uploads its checked build as {uploaded!r} in this run. A name that does not match fails after "
        "the approval, not before it"
    )

    publish_steps = [step for step in _steps(job) if _publishing_commands(step)]
    assert len(publish_steps) == 1, f"expected one publishing step in {in_tag[0]}, found {len(publish_steps)}"
    reads_from = (publish_steps[0].get("with") or {}).get("packages-dir", _PUBLISH_ACTION_DEFAULT_DIR)
    assert _normalised_dir(fetched.get("path", ".")) == _normalised_dir(reads_from), (
        f"{_TAG_WORKFLOW.name} → {in_tag[0]} downloads the distribution into {fetched.get('path', '.')!r}, "
        f"but the publish step reads {reads_from!r}. It would find nothing to upload, after the approval"
    )


def test_the_publishing_job_is_scoped_to_an_environment() -> None:
    """Every job that publishes waits on the ``pypi`` deployment environment, and on no condition of its own.

    Whether a release reaches the index is decided outside this repository, on the run, by the
    environment's required reviewer. The environment's name is also what the index checks in the
    token's claim, so binding the job to it and registering it with the index are one fact.

    The job carries no ``if:``, and that is deliberate. A condition that evaluates false *skips* the
    job and the run goes green, and a release that silently did not happen is the failure
    :func:`test_release_workflow_is_called_not_triggered` exists to name. A job waiting on an
    environment is loud. On a merge that carries no new version the job is skipped anyway, through
    ``needs``, because the release is. That is the quiet no-op ``tag.yml`` relies on, and it needs no
    condition to say so.
    """
    unbound = []
    conditional = []
    for path, job_id, job in _publishing_jobs():
        environment = job.get("environment")
        name = environment.get("name") if isinstance(environment, dict) else environment
        if name != _PUBLISHING_ENVIRONMENT:
            unbound.append(f"{path.name} → {job_id}: environment = {environment!r}")
        if "if" in job:
            conditional.append(f"{path.name} → {job_id}: if: {job['if']!r}")

    assert not unbound, (
        f"a job that publishes is not bound to the `{_PUBLISHING_ENVIRONMENT}` environment. That "
        "environment's reviewer is the only gate between a merge and the index, and its name is what the "
        "index checks in the token:\n  " + "\n  ".join(unbound)
    )
    assert not conditional, (
        "a job that publishes carries a condition. A false condition skips the job and paints the run "
        "green; the gate is the environment's reviewer, which waits where everyone can see it:\n  "
        + "\n  ".join(conditional)
    )


def test_the_publishing_job_carries_no_long_lived_credential() -> None:
    """Every job that publishes authenticates by an OIDC exchange, and holds nothing that outlives the run.

    Three things. The job asks for ``id-token: write`` itself. Without it the exchange cannot happen,
    and the obvious repair is a stored token. The workflow around it does not grant the permission: at
    workflow level it reaches every job that declares no permissions of its own, and the index
    recommends the job level for that reason. And nothing in the job is a stored credential: no
    ``password:`` input, no key named like a token, no ``secrets.`` reference.

    A stored credential in this job is worse than a style problem. It works without anyone's
    approval, and the approval is what this job exists to wait for.
    """
    offenders = []
    for path, job_id, job in _publishing_jobs():
        if _grants_id_token(_load(path).get("permissions")):
            offenders.append(f"{path.name}: `id-token: write` granted at workflow level")
        if not _grants_id_token(job.get("permissions")):
            offenders.append(f"{path.name} → {job_id}: does not ask for `id-token: write` at job level")

        held = {key: value for key, value in job.items() if key != "permissions"}
        for where, key, value in _entries(held):
            if any(fragment in key.lower() for fragment in _CREDENTIAL_KEY_FRAGMENTS):
                offenders.append(f"{path.name} → {job_id}.{where}: a key named like a credential")
            if isinstance(value, str) and "secrets." in value.lower():
                offenders.append(f"{path.name} → {job_id}.{where}: reads a secret, {value.strip()!r}")

    assert not offenders, (
        "a job that publishes carries a long-lived credential, or cannot do without one. It must "
        "authenticate by the OIDC exchange the publish action performs, with the permission on the job "
        "and nothing stored:\n  " + "\n  ".join(offenders)
    )


def test_the_tag_lock_is_not_held_while_a_publish_waits() -> None:
    """``tag.yml``'s concurrency group sits on the ``tag`` job, not on the workflow and not on a job that waits.

    The group exists so two merges landing seconds apart cannot both decide a tag is missing. At
    workflow level it is held for the whole run, and the run now includes a job that waits for a
    person, where a build takes minutes. GitHub keeps at most one pending run per group and cancels it
    when another arrives, so a merge landing during the wait could lose its tag run to the merge after
    it. That is the failure ``tag.yml``'s own comment says the group exists to prevent.

    On the ``tag`` job the group still serializes the one decision that must not race, and it is
    released before anything waits on a person. The value is held exactly: the move changes where the
    lock is, not what it is.
    """
    document = _load(_TAG_WORKFLOW)

    assert "concurrency" not in document, (
        f"{_TAG_WORKFLOW.name} declares `concurrency:` at workflow level again. The run includes a job that "
        "waits for a reviewer, so the group would be held for the whole wait, and a merge landing during it "
        f"could lose its tag run. Put it on the `{_TAG_JOB}` job. Found: {document['concurrency']!r}"
    )

    jobs = _jobs(document)
    assert _TAG_JOB in jobs, f"{_TAG_WORKFLOW.name} has no `{_TAG_JOB}` job. Found: {sorted(jobs)}"
    assert jobs[_TAG_JOB].get("concurrency") == _TAG_LOCK, (
        f"{_TAG_WORKFLOW.name} → {_TAG_JOB} does not carry the tag lock {_TAG_LOCK!r}, so two merges landing "
        f"together can both decide a tag is missing. Found: {jobs[_TAG_JOB].get('concurrency')!r}"
    )

    holders = sorted(
        job_id
        for job_id, job in jobs.items()
        if job_id != _TAG_JOB and _concurrency_group(job.get("concurrency")) == _concurrency_group(_TAG_LOCK)
    )
    assert not holders, (
        f"another job in {_TAG_WORKFLOW.name} holds the `{_TAG_LOCK['group']}` group as well: {holders}. On a "
        "job that waits for a reviewer it is held for the whole wait, which is what moving it off the "
        "workflow was for"
    )
