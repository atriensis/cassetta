# ADR 002 — `BackendConfig` as the Public Composition API

**Status**: Accepted — Brief 520, shipped **v0.12.0**

## Problem

`create_app()` had grown to take nine `Optional` keyword arguments — `backend=`,
`claim_store=`, `key_store=`, `identity_provider=`, `access_policy=`, and more.
Constructing a partially-overridden app (say, Azure storage but a filesystem
claim store) meant juggling ad-hoc parameter combinations, and every caller had to
know the whole list. Worse, the §VII "fall back to a local default if this kwarg
is `None`" construct had been copied beyond `app.py` into `mcp_server.py`,
`dependencies.py`, and `routes/{inbox,files,keys}.py` — the same drift the
three-layer architecture ([ADR 001](001-three-layer-architecture.md)) exists to
prevent, now smeared across Layer 2.

## Decision

Introduce a single `@dataclass(frozen=True) BackendConfig` holding all nine backend
implementations. `create_app(config, backends: BackendConfig | None = None)` takes
**one** optional argument; when it is `None`, `build_core_defaults(config)` produces
the all-filesystem default. The lifespan resolves the config once and stashes it on
`app.state.backends`; every Layer 2 consumer reads it through a single
`get_backends()` FastAPI dependency.

Crucially, **vendor selection lives outside the dataclass**. Cloud constructs a
`BackendConfig` of Azure implementations and passes it as `backends=…`; there is no
`if backend == "vendor"` branch anywhere. AWS/GCP/mixed deployments are just
different `BackendConfig(...)` literals.

## Consequences

- **Compile-time typing**: mypy sees each field as a concrete Protocol type — no
  string registry, no `Optional` soup. One kwarg replaces nine.
- **§VII-clean**: the local-default fallback exists in exactly one place
  (`build_core_defaults`); Layer 2 never reconstructs it. The AST regression lock
  was extended from `app.py` to all of `src/cassetta/` (except
  `defaults/factory.py` and `backends/`).
- **Expressible deployments**: Docker, K8s, and self-host are all just
  `BackendConfig` literals.
- Cost: this was bundled into a larger cleanup wave rather than a minimal patch —
  but doing so eliminated a second pass over the same files.

Implemented at `src/cassetta/defaults/factory.py` (`BackendConfig`,
`build_core_defaults`) and `src/cassetta/app.py` (`create_app`).

## Links

- PR: <https://github.com/ximera239/cassetta/pull/41>
- Issue: <https://github.com/ximera239/cassetta/issues/38>
- Constitution: [§VII Three-Layer Architecture](../../.specify/memory/constitution.md)
- Background: [ADR 001](001-three-layer-architecture.md); analyst archive
  `cassetta-backendconfig-public-api.md` (not in this repo).
