"""Configuration is a Layer-1 protocol, and it declares only what this library reads.

``AppConfig`` used to be a joint object: a frozen dataclass this repository published, annotated as
the concrete type everywhere in Layer 2, carrying fields that no code here read. Two of them had no
reader at all, and both were documented to an operator of *this* repository as operative settings.
A sentence can be deleted; a shared object keeps carrying shape across the boundary.

So the boundary is written as a type. ``CoreConfig`` declares exactly the attributes ``src/`` reads,
a caller supplies anything that carries them, and the guard in the middle of this file is what stops
a third foreign field arriving the way the first two did.
"""

from __future__ import annotations

import ast
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, get_origin, get_type_hints

from fastapi import FastAPI

from cassetta.app import create_app
from cassetta.config import AppConfig, LimitsConfig
from cassetta.protocols.config import CoreConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

# The identifiers a configuration object is bound to in this tree. Matching the *trailing*
# identifier of the object expression rather than the whole expression is what lets one rule cover
# ``config.x``, ``self._config.x`` and ``app.state.config.x`` without enumerating call shapes.
_CONFIG_OBJECT_NAMES = frozenset({"config", "_config", "cfg"})

_TEST_JWT_KEY = b"test-test-test-test-test-test-test-t"


def _class_var_members() -> set[str]:
    """The protocol members declared as ``ClassVar`` — resolved, not matched as text.

    ``LimitsConfig`` is imported by the protocol module under ``TYPE_CHECKING`` (it is a Layer-2
    concrete type, and a Layer-1 module must not import one at runtime), so it is handed to the
    resolver here. Resolving rather than reading ``__annotations__`` as strings also means a
    protocol whose annotations do not resolve fails this file rather than passing it silently.
    """
    hints = get_type_hints(CoreConfig, localns={"LimitsConfig": LimitsConfig})
    return {name for name, hint in hints.items() if get_origin(hint) is ClassVar}


def _configuration_members() -> set[str]:
    """The protocol's configuration values — every member except the ``ClassVar`` ones.

    ``__protocol_attrs__`` is what ``isinstance`` itself checks against, so this reads the same
    member list the runtime does rather than a second, hand-maintained copy of it. It is also
    indifferent to how a member is spelled: every one of these is a read-only property, which is
    the only shape a frozen implementation can satisfy, and properties carry no annotation to read.

    ``kind`` is excluded by being a ``ClassVar``: it identifies an implementation (ADR 003), it is
    not a configuration value, and requiring a reader for it would make that convention justify
    itself twice.
    """
    return set(CoreConfig.__protocol_attrs__) - _class_var_members()


def _trailing_identifier(node: ast.expr) -> str | None:
    """The last identifier of an object expression: ``self._config`` -> ``_config``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _attributes_read_off_configuration(paths: list[Path]) -> set[str]:
    """Every attribute name read off a configuration object in ``paths``.

    An ``ast.Attribute`` is a *read*. A dataclass field and a protocol member are both
    ``ast.AnnAssign``, so neither the declaration in ``config.py`` nor the one in
    ``protocols/config.py`` can contribute here — which is the property that makes the guard below
    a guard rather than a restatement of itself.
    """
    names: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and _trailing_identifier(node.value) in _CONFIG_OBJECT_NAMES:
                names.add(node.attr)
    return names


def _source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


@dataclass(frozen=True)
class _ForeignConfig:
    """A configuration object that is not ``AppConfig`` and does not inherit from it.

    This is the caller the protocol exists for, written small enough to read: the sixteen members
    ``CoreConfig`` declares, and nothing else.
    """

    kind: ClassVar[str] = "test"

    setup_token: str
    dev_mode: bool
    storage_path: str
    keys_file: str
    default_ttl: int
    allowed_path_chars: str
    mcp_allowed_hosts: tuple[str, ...]
    jwt_primary_key: bytes
    jwt_secondary_key: bytes | None
    jwt_primary_key_source: str
    jwt_key_overlap_ttl: int
    public_base_url: str
    log_format: str
    rate_limit_broadcast: str
    broadcast_max_targets: int
    limits: LimitsConfig = field(default_factory=LimitsConfig)


def _app_config(tmp_path: Path) -> AppConfig:
    """An ``AppConfig`` of the shape ``load_config()`` produces, without touching the environment."""
    return AppConfig(
        setup_token="",
        dev_mode=True,
        storage_path=str(tmp_path / "data"),
        keys_file=str(tmp_path / "data.keys" / ".cassetta-keys.json"),
        default_ttl=0,
        allowed_path_chars=r"a-zA-Z0-9\-_./",
        mcp_allowed_hosts=(),
        jwt_primary_key=_TEST_JWT_KEY,
        public_base_url="http://localhost:16001",
    )


def test_app_config_satisfies_the_protocol(tmp_path: Path) -> None:
    """``AppConfig`` is accepted where ``CoreConfig`` is required — structurally and in use.

    The first assertion is a real sweep rather than a restatement of an annotation: ``CoreConfig``
    is ``@runtime_checkable``, so ``isinstance`` checks that every declared member is actually
    present on the object. It goes red the moment ``AppConfig`` loses one.

    The second is the requirement executed. A frozen dataclass that is *not* ``AppConfig`` drives
    ``create_app`` to a working application — which is precisely what could not happen before this
    protocol existed, because ``create_app`` and ``build_core_defaults`` both named the concrete
    type. Structural conformance is what mypy checks; this is what it buys.
    """
    assert isinstance(_app_config(tmp_path), CoreConfig)

    foreign = _ForeignConfig(
        setup_token="",
        dev_mode=True,
        storage_path=str(tmp_path / "foreign"),
        keys_file=str(tmp_path / "foreign.keys" / ".cassetta-keys.json"),
        default_ttl=0,
        allowed_path_chars=r"a-zA-Z0-9\-_./",
        mcp_allowed_hosts=(),
        jwt_primary_key=_TEST_JWT_KEY,
        jwt_secondary_key=None,
        jwt_primary_key_source="env",
        jwt_key_overlap_ttl=600,
        public_base_url="http://localhost:16001",
        log_format="text",
        rate_limit_broadcast="10/minute",
        broadcast_max_targets=1000,
    )
    # Non-vacuity: an `_ForeignConfig` that inherited from `AppConfig` would prove nothing.
    assert not isinstance(foreign, AppConfig)
    assert isinstance(foreign, CoreConfig)

    app = create_app(foreign)

    assert isinstance(app, FastAPI)
    assert app.state.config is foreign


def test_the_protocol_declares_only_what_core_reads() -> None:
    """Every attribute ``CoreConfig`` declares is read somewhere under ``src/``.

    This is the mechanical guard, and the reason the protocol is worth its size. Two fields reached
    the published configuration surface with no reader anywhere, were parsed and range-checked at
    startup, and were documented as operative settings; nothing said so.

    **When this fails, it is one of two things**, and the test cannot tell them apart:

    * the attribute is dead — it belongs in neither the protocol nor the dataclass; or
    * its last reader was removed — the protocol should follow it out, and so should the variable
      that feeds it.

    Either way the answer is to remove the member, not to add a reader for its own sake.

    It reads the **source**, not the dataclass: a field can be declared, defaulted, parsed and
    validated without anything ever consulting it, which is exactly how the two got here.
    """
    sources = _source_files()
    # Non-vacuity: a mistyped root would otherwise make this guard silently green forever.
    assert len(sources) >= 30, f"parsed only {len(sources)} files — the source root is wrong"

    read = _attributes_read_off_configuration(sources)
    assert len(read) >= 20, f"collected only {len(read)} attribute names — the collector is wrong"

    declared = _configuration_members()
    assert declared, "CoreConfig declares no configuration members — the resolver is wrong"

    unread = sorted(declared - read)

    assert not unread, (
        f"CoreConfig declares {len(unread)} attribute(s) that nothing under src/ reads. Either the "
        "attribute is dead and should leave the protocol and AppConfig with it, or its last reader "
        "was removed and the protocol should follow it out — a member with no reader is a knob this "
        "repository promises and does not turn:\n  " + "\n  ".join(unread)
    )


def test_declaring_an_attribute_does_not_count_as_reading_it() -> None:
    """The two files that *declare* the configuration cannot vouch for their own members.

    A dataclass field and a protocol member are both ``ast.AnnAssign``; only a read is an
    ``ast.Attribute``. That is what keeps the guard above from being satisfied by the very
    declaration it is checking, and it is asserted here so a later change to the collector cannot
    quietly lose the property.
    """
    declaration_sites = [SRC / "cassetta" / "config.py", SRC / "cassetta" / "protocols" / "config.py"]
    for path in declaration_sites:
        assert path.is_file(), f"declaration site missing: {path}"

    self_vouched = _attributes_read_off_configuration(declaration_sites) & _configuration_members()

    assert not self_vouched, (
        "the configuration's own declaration sites are being counted as readers of it, so the "
        f"guard above would pass over a dead field: {sorted(self_vouched)}"
    )


def test_the_protocol_declares_kind() -> None:
    """The tenth Layer-1 protocol follows the ADR 003 convention, and ``AppConfig`` implements it.

    ADR 003 put ``kind: ClassVar[str]`` on all nine Layer-1 protocols and argued that one
    non-conforming protocol among them would cost more than the convention does. The same reasoning
    applies to the tenth.

    The second half is the ADR's own implementation gotcha, asserted rather than commented: the
    member has to be a class variable. A bare ``kind = "core"`` on a dataclass reads as a field to
    both ``dataclasses`` and mypy, which would put it in every constructor call in the tree.
    """
    assert "kind" in CoreConfig.__annotations__, "CoreConfig does not declare `kind` in its class body."
    assert "kind" in _class_var_members(), "CoreConfig.kind is not a ClassVar"

    assert AppConfig.kind == "core"
    assert "kind" not in {f.name for f in dataclasses.fields(AppConfig)}, (
        "AppConfig.kind is a dataclass field rather than a class variable — it would then be a "
        "required argument at every construction site."
    )
