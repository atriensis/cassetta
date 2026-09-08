"""Guard: ``scripts/smoke.sh`` leaves the tree as it found it.

The script walks the README quickstart against a real container. It had removed the ``.env`` it wrote
and nothing else — so the API key store it caused to be created survived, carrying ``setup_done:
true``, and the documented quickstart's very first call then answered ``409 Setup already completed``.
The order that produces that is the README's own: the reader is invited to prove the quickstart before
trusting it, and the failure arrives at the first step of the thing they were proving.

The script already diagnosed this precisely, for itself. Its pre-flight refuses to start when a store
survives and explains that ``POST /setup`` is one-time and the flag is persisted. It knew the store
was durable and poisoned a fresh setup; it neither removed it nor warned that the quickstart was
poisoned too.

**This is a text guard, and the distinction is load-bearing.** Running the script needs Docker, and
this suite must not. What is asserted here is that the recipe says the right thing — that
``cleanup()`` names the store, under a guard, in the shape its ``.env`` removal already uses. That
the file is actually gone after a run is the operator's observation, not this file's claim.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_SCRIPT = REPO_ROOT / "scripts" / "smoke.sh"

# `if [ "$flag" = true ]; then` — the shape both existing ownership guards are written in.
_IF_FLAG_RE = re.compile(r'^\s*if\s+\[\s+"\$(?P<flag>\w+)"\s+=\s+true\s+\]\s*;\s*then\b')
_IF_RE = re.compile(r"^\s*if\b")
_FI_RE = re.compile(r"^\s*fi\b")
_REMOVAL_RE = re.compile(r"^\s*rm\b")

# The pre-flight's existence test on the key store, and the `fi` that closes it.
_IF_STORE_EXISTS_RE = re.compile(r"^\s*if\s+\[\s+-e\s+\"?\$\{?KEY_STORE\}?\"?\s+\]\s*;\s*then\b")


def _script() -> str:
    assert _SCRIPT.is_file(), f"{_SCRIPT} is missing — it is the recipe this guard reads"
    return _SCRIPT.read_text(encoding="utf-8")


def _function_body(script: str, name: str) -> list[str]:
    """The lines of a shell function, between ``name() {`` and the ``}`` that closes it at column 0."""
    opening = re.compile(rf"^{re.escape(name)}\(\)\s*\{{")
    lines = script.splitlines()
    for index, line in enumerate(lines):
        if opening.match(line):
            body = []
            for candidate in lines[index + 1 :]:
                if candidate.startswith("}"):
                    return body
                body.append(candidate)
            raise AssertionError(f"{name}() is never closed by a `}}` at column zero")
    raise AssertionError(f"scripts/smoke.sh declares no {name}() function")


def _guard_of_removal(body: list[str], subject: str) -> str | None:
    """The flag guarding the removal of ``subject``, walking ``if``/``fi`` nesting.

    Returns the flag name when the innermost open condition at the removal is a boolean-flag test,
    ``None`` when the removal is unguarded or guarded by something else. Raises when there is no
    removal of that subject at all, so a missing recipe and an unguarded one report differently.
    """
    conditions: list[str | None] = []
    for line in body:
        if _IF_RE.match(line):
            flag = _IF_FLAG_RE.match(line)
            conditions.append(flag.group("flag") if flag else None)
            continue
        if _FI_RE.match(line):
            if conditions:
                conditions.pop()
            continue
        if _REMOVAL_RE.match(line) and subject in line:
            return conditions[-1] if conditions else None
    raise AssertionError(f"cleanup() removes nothing matching {subject!r}")


def _preflight_refusal(script: str) -> list[str]:
    """The body of the pre-flight's ``if [ -e "$KEY_STORE" ]`` block."""
    lines = script.splitlines()
    for index, line in enumerate(lines):
        if _IF_STORE_EXISTS_RE.match(line):
            block = []
            for candidate in lines[index + 1 :]:
                if _FI_RE.match(candidate):
                    return block
                block.append(candidate)
    return []


def test_cleanup_removes_the_key_store_it_created() -> None:
    """``cleanup()`` removes the key store this run created — and only that one.

    Four assertions, and the last is what keeps the first three honest: the cheapest way to satisfy
    a removal requirement while making the script worse is to delete the pre-flight refusal and let
    ``cleanup()`` remove whatever it finds.
    """
    script = _script()
    body = _function_body(script, "cleanup")

    # 1. cleanup() names the store — by the variable the script already declares once, not by a
    #    second spelling of the same path that would have to be kept in step with it.
    assert any("KEY_STORE" in line for line in body), (
        "cleanup() never names KEY_STORE, so the key store this run caused to be created survives "
        "the run. It carries `setup_done: true`, and the documented quickstart's first call then "
        "answers 409 Setup already completed"
    )

    # 2. The `.env` removal is guarded. Checked first because it is the shape the key-store removal
    #    is required to match — and because it doubles as this walker's non-vacuity check: a parser
    #    that understood nothing would find no unguarded removal either.
    env_guard = _guard_of_removal(body, ".env")
    assert env_guard is not None, (
        "cleanup() removes the .env without testing a boolean ownership flag first. That guard is "
        "the shape this test measures the key-store removal against, so it cannot be dropped"
    )

    # 3. The key-store removal is guarded the same way.
    store_guard = _guard_of_removal(body, "KEY_STORE")
    assert store_guard is not None, (
        "cleanup() removes the key store unconditionally. A run that refused at pre-flight, or one "
        f"whose stack never started, would delete a store it did not create — guard it as the .env "
        f'removal is guarded (`if [ "${env_guard}" = true ]; then …`)'
    )

    # 4. The flag is a real flag: declared false, set true somewhere. One that is declared and never
    #    set makes the removal dead; one that is set and never declared makes cleanup() fail under
    #    `set -u` on every path that exits before it is assigned.
    assert re.search(rf"^{store_guard}=false$", script, re.MULTILINE), (
        f"{store_guard} guards the key-store removal but is never initialised to false at the top "
        "of the script, so cleanup() reads an unset variable under `set -u` on early-exit paths"
    )
    assert re.search(rf"^\s*{store_guard}=true$", script, re.MULTILINE), (
        f"{store_guard} guards the key-store removal and is never set to true, so the removal is "
        "dead code and the store still survives every run"
    )

    # 5. The pre-flight refusal is still there. It is what makes a guarded removal safe to write:
    #    the flag can only mean "this run created it" because the script has already refused to
    #    start over one it did not.
    refusal = _preflight_refusal(script)
    assert refusal, (
        "the pre-flight no longer refuses to start when a previous run's key store survives. That "
        "refusal is the reason the removal above is safe — without it, `cleanup()` deletes a store "
        "the script may have found rather than created"
    )
    assert any("die" in line for line in refusal), (
        "the pre-flight notices a surviving key store and does not refuse to run. Continuing over "
        "one means minting a key against a store that already carries setup_done"
    )
    assert any("409" in line for line in refusal), (
        "the pre-flight refusal no longer says what a surviving key store causes. The 409 from "
        "POST /setup is the whole diagnosis, and it is what a reader hitting this needs to read"
    )
