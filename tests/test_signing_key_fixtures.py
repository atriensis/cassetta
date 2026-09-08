"""Guard: a test configuration that can sign carries a signing key.

``AppConfig.jwt_primary_key`` defaults to ``b""``, and a running server can never hold that value.
``load_config()`` calls ``_load_jwt_key_pair(required=True)``, which exits 1 when neither
``CASSETTA_JWT_KEY`` nor ``CASSETTA_JWT_KEY_FILE`` is set — so the empty key is a state production
cannot reach, and a test that builds one is exercising something that does not exist.

Six fixtures built exactly that. Each called ``load_config()`` — which has a key — and then rebuilt
``AppConfig`` field by field from it, and the hand-copied list omitted ``jwt_primary_key``. Five of
them passed anyway, because their code paths never signed a token; the sixth,
``tests/test_send_with_policy.py``, went red the moment PyJWT 2.13 added
``InvalidKeyError("HMAC key must not be empty.")``. **The newer PyJWT was enforcing an invariant this
repository already had.**

So the defect is not the version floor, and it is not the dataclass default. It is the hand-copied
field list, which silently loses whatever is added to the dataclass after the copy was written. This
test is the general form: whatever fields a test config names, the one that decides whether it can
sign has to be among them.

**Where a config derives from ``load_config()``, the fix is ``dataclasses.replace(config, …)``**
rather than a longer list. ``replace`` carries every field forward by construction, so it cannot lose
a field added later — and a site written that way stops being an ``AppConfig`` call and leaves this
rule's scope on its own. That is the intended outcome rather than a way around the check: the reason
the rule exists no longer applies there.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent

# The field that decides whether a configuration can sign a token.
_REQUIRED_KEYWORD = "jwt_primary_key"

# The dataclass, by the name it is called under. An `ast.Attribute` callee (`config.AppConfig(...)`)
# is out of scope: no site in this tree writes one, and a rule written for no code is a rule nobody
# maintains. Add the case when a site appears.
_TARGET = "AppConfig"

# The number of compliant call sites at the time this guard was written. Below it, the walk has
# stopped finding what it is meant to check — a moved layout, a changed callee, a swallowed parse —
# and a permanently green check is worse than no check.
_MINIMUM_CALL_SITES = 3


def _app_config_calls() -> list[tuple[Path, ast.Call]]:
    """Every ``AppConfig(...)`` written under ``tests/``, with the file that writes it."""
    found: list[tuple[Path, ast.Call]] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == _TARGET:
                found.append((path, node))
    return found


def test_every_test_config_that_can_sign_has_a_key() -> None:
    """No ``AppConfig(...)`` under ``tests/`` leaves the signing key to its default."""
    calls = _app_config_calls()

    assert len(calls) >= _MINIMUM_CALL_SITES, (
        f"found {len(calls)} {_TARGET}(...) call(s) under {TESTS_ROOT}, expected at least "
        f"{_MINIMUM_CALL_SITES}. This guard has stopped finding what it checks — the callee, the "
        "layout or the way configs are built has changed — and a check that inspects nothing passes "
        "over everything."
    )

    offenders = [
        f"  {path.relative_to(TESTS_ROOT.parent)}:{call.lineno}"
        for path, call in calls
        # `kw.arg is None` is a `**` unpacking: a dict assembled somewhere else is precisely the
        # hand-copied field list this guard exists to stop, so it does not satisfy the rule.
        if _REQUIRED_KEYWORD not in {kw.arg for kw in call.keywords if kw.arg is not None}
    ]

    assert not offenders, (
        f"{len(offenders)} {_TARGET}(...) call(s) do not pass `{_REQUIRED_KEYWORD}`, so they fall "
        f'back to the dataclass default `b""` — a state a running server cannot hold, because '
        "`load_config()` calls `_load_jwt_key_pair(required=True)` and exits 1 without a key. PyJWT "
        '>= 2.13 rejects it outright with InvalidKeyError("HMAC key must not be empty.").\n'
        + "\n".join(offenders)
        + f"\n\nWhere the config derives from `load_config()`, prefer "
        f"`dataclasses.replace(config, ...)` over re-listing the fields: a hand-copied list loses "
        f"whatever is added to the dataclass after it was written, which is the defect itself. "
        f"Otherwise pass `{_REQUIRED_KEYWORD}=` explicitly."
    )
