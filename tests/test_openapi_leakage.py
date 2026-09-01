"""Guard: the public REST OpenAPI surface must not leak internals.

Sibling lock to the §VII layer-separation AST test and the MCP description-hygiene
guard (``integration/test_mcp_description_hygiene.py``). FastAPI surfaces each route
handler's docstring as the operation ``summary``/``description`` in the generated
OpenAPI, and Pydantic surfaces model docstrings + ``Field(description=...)`` as
component-schema descriptions. This test keeps internal change-ticket numbers,
requirement IDs, and internal class/helper names out of every *published* description.

It runs against the **generated** OpenAPI (``GET /openapi.json``), not a source grep,
so non-surfacing module docstrings and ``# comments`` are correctly ignored and routes
added later are covered automatically.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest

# Internal markers that must never appear in caller-facing description text.
# Mirrors the Brief 541 MCP guard's intent for the REST surface: internal
# Layer-1/2 protocol & policy class names and private route helpers.
_FORBIDDEN_SUBSTRINGS = [
    "Brief ",
    "LimitsPolicy",
    "AccessPolicy",
    "AliasResolver",
    "KeyStoreProtocol",
    "MetricsProvider",
    "StorageBackend",
    "check_manifest_against_limits",
    "_get_backend",
    "_sender_from_identity",
    "_get_config",
]
_FR_RE = re.compile(r"\bFR-\d")

# OpenAPI operation objects live under these HTTP-verb keys; other keys in a
# path item (e.g. "parameters") are not operations.
_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "patch", "options", "head", "trace"})


def _iter_described_texts(schema: dict) -> Iterator[tuple[str, str]]:
    """Yield ``(location, text)`` for every caller-facing description surface.

    Covers path-operation ``summary``/``description`` plus component-schema and
    schema-property ``description``. Schema *names* are intentionally excluded —
    they are legitimate public identifiers, not description prose.
    """
    for path, item in (schema.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(op, dict):
                continue
            for field in ("summary", "description"):
                text = op.get(field)
                if isinstance(text, str) and text:
                    yield (f"paths.{path}.{method}.{field}", text)

    schemas = ((schema.get("components") or {}).get("schemas")) or {}
    for name, model in schemas.items():
        if not isinstance(model, dict):
            continue
        desc = model.get("description")
        if isinstance(desc, str) and desc:
            yield (f"components.schemas.{name}.description", desc)
        for prop, pschema in (model.get("properties") or {}).items():
            if not isinstance(pschema, dict):
                continue
            pdesc = pschema.get("description")
            if isinstance(pdesc, str) and pdesc:
                yield (
                    f"components.schemas.{name}.properties.{prop}.description",
                    pdesc,
                )


def _violations(location: str, text: str) -> list[str]:
    found = [bad for bad in _FORBIDDEN_SUBSTRINGS if bad in text]
    if _FR_RE.search(text):
        found.append("FR-<id>")
    return [f"{location} leaks {bad!r}: {text!r}" for bad in found]


@pytest.mark.asyncio
async def test_openapi_descriptions_have_no_internal_leakage(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200, resp.text
    schema = resp.json()

    described = list(_iter_described_texts(schema))
    assert described, "no OpenAPI descriptions found — schema generation likely broken"

    # Collect every violation so a single run reports the complete leak set.
    offenders: list[str] = []
    for location, text in described:
        offenders.extend(_violations(location, text))
    assert not offenders, "Generated OpenAPI leaks internals:\n" + "\n".join(offenders)


@pytest.mark.asyncio
async def test_known_endpoints_are_caller_facing(
    client: httpx.AsyncClient,
) -> None:
    """The two formerly-leaky operations now read as caller guidance, not brief refs."""
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200, resp.text
    paths = resp.json()["paths"]

    broadcast = paths["/broadcast/{path}"]["post"]
    legacy_put = paths["/inbox/{agent}/{path}"]["put"]
    for label, op in (("broadcast", broadcast), ("legacy inbox PUT", legacy_put)):
        blob = f"{op.get('summary', '')} {op.get('description', '')}".strip()
        assert blob, f"{label} operation has no summary/description"
        assert "Brief " not in blob, f"{label} still leaks a brief number: {blob!r}"
