"""``cassetta capabilities`` subcommand (Brief 516).

One-shot ``GET {url}/capabilities`` that pretty-prints the server's
advertised capabilities document: version, supported modes, limits,
TTLs, and features. Null caps render as ``unlimited``.

Exit codes (see ``specs/516-advertise-limits-handshake/contracts/capabilities-cli.md``):
- 0: happy path (2xx response, valid JSON)
- 1: network failure (connect refused, DNS, TLS, timeout)
- 2: HTTP error (non-2xx) or malformed JSON
"""

from __future__ import annotations

import json
import sys
from typing import Any

import httpx
import typer

from cassetta.cli.download import _run_async


def _render(doc: dict[str, Any]) -> None:
    """Print the capabilities document in a stable, human-readable layout."""
    print(f"Server version: {doc.get('server_version', '?')}")
    print(f"Schema version: {doc.get('schema_version', '?')}")

    modes = doc.get("supported_modes") or []
    print(f"Supported modes: {', '.join(str(m) for m in modes)}")

    features = doc.get("features") or []
    print(f"Features: {', '.join(str(f) for f in features)}")

    limits = doc.get("limits") or {}
    print("Limits:")
    for key in (
        "per_file_max",
        "per_bundle_total_max",
        "per_bundle_file_count_max",
        "max_inline_size",
    ):
        value = limits.get(key)
        rendered = "unlimited" if value is None else str(value)
        print(f"  {key}: {rendered}")

    ttls = doc.get("ttls") or {}
    print("TTLs (seconds):")
    for key in (
        "upload_token_ttl",
        "download_claim_ttl",
        "passive_gc_min_age",
        "passive_gc_interval",
    ):
        value = ttls.get(key)
        rendered = "n/a" if value is None else str(value)
        print(f"  {key}: {rendered}")


async def _fetch(url: str, api_key: str | None) -> dict[str, Any]:
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{url.rstrip('/')}/capabilities", headers=headers)
        if resp.status_code >= 400:
            body = resp.text.strip()
            hint = (
                " (authentication)"
                if resp.status_code in (401, 403)
                else ""
            )
            print(
                f"capabilities request failed{hint}: {resp.status_code} {body}",
                file=sys.stderr,
            )
            raise typer.Exit(code=2)
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            print(f"response is not valid JSON: {exc}", file=sys.stderr)
            raise typer.Exit(code=2) from exc
        if not isinstance(data, dict):
            print("response is not a JSON object", file=sys.stderr)
            raise typer.Exit(code=2)
        return data


def capabilities(
    url: str = typer.Option(
        ..., "--url", help="Server base URL (e.g. http://localhost:16001).",
    ),
    api_key: str | None = typer.Option(
        None, "--api-key", help="Optional bearer token for the request.",
    ),
) -> None:
    """Fetch and pretty-print the server's advertised capabilities."""
    try:
        doc = _run_async(_fetch(url, api_key))
    except typer.Exit:
        raise
    except httpx.RequestError as exc:
        print(f"network failure: {exc}", file=sys.stderr)
        raise typer.Exit(code=1) from exc

    _render(doc)
