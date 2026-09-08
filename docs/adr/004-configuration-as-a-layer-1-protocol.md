# ADR 004 — Configuration as a Layer-1 Protocol

**Status**: Accepted — shipped **v0.27.0**

## Problem

[ADR 002](002-backendconfig-public-api.md) settled how a caller puts its own *implementations* into
this library's composition: declare typed slots, let the caller inject, no string registry. It left
the other half of the same question unanswered. `create_app(config, backends=…)` took the caller's
`BackendConfig` and, in the same call, insisted on this library's own `AppConfig` — the concrete
frozen dataclass, named in every Layer-2 signature that touched a setting.

So configuration stayed a joint object across the boundary, and a joint object accumulates. Two of
`AppConfig`'s eighteen fields had **no reader anywhere** under `src/`: `rate_limit_onboard` and
`invite_ttl_seconds`. Both were parsed, defaulted and range-checked at startup — a malformed value
stopped the server — and both were documented in `docs/CONFIG.md` as operative settings of *this*
repository. Neither governed anything here. The measurement that settles the first: nine `POST
/keys` inside one minute all answered `201`, against a documented five-a-minute budget.

That is not a documentation defect. A sentence can be deleted; a shared type carries shape across
the boundary and keeps carrying it, and nothing in the tree could tell the difference between a
setting this library reads and one it merely holds.

The same shape appeared once more, in the rate-limit handler: it chose which counter a 429 belonged
to by testing `request.url.path.startswith("/onboard")` — a route this library does not serve,
recognised by a hardcoded prefix in its own exception handler.

## Decision

**Configuration is a Layer-1 protocol.** `CoreConfig`, in `src/cassetta/protocols/config.py`,
declares the sixteen settings this library reads and nothing else. Every Layer-2 annotation names
it. `AppConfig` keeps its name, its export and its role as `load_config()`'s return type, and
becomes the reference implementation rather than the only possible one.

Three consequences of the declaration were deliberate:

- **Members are read-only properties.** Nothing here writes to a configuration object, and a
  settable protocol member demands a settable implementation — which would exclude `AppConfig`
  itself, a frozen dataclass, and every immutable settings type a caller is likely to bring.
- **`LimitsConfig` is named as the concrete type it is**, imported under `TYPE_CHECKING`. Layer 1
  does not import Layer 2 at runtime. Whether that type deserves the same treatment is a later
  question, not one this decision answers by accident.
- **`kind: ClassVar[str]` follows [ADR 003](003-protocol-self-identification.md).** The argument
  there — that one non-conforming protocol among the others costs more than the convention does —
  applies to the tenth as it did to the nine.

**The two fields left**, along with their environment variables, their parsers and their rows in
`docs/CONFIG.md` and `.env.example`. They were not replaced by a note saying they exist elsewhere:
`test_config_reference_names_no_private_half_var` states in its own docstring that a reference,
even phrased as an absence, is the leak rather than the courtesy.

**The rate-limit route became the caller's to name.** `check_rate_limit_imperative` takes a
required `route=` keyword and records it on the request; the handler reads it back and falls back to
`broadcast` when nothing was recorded — which is what a decorator-raised rejection has always been
counted as. The `Literal["onboard", "broadcast"]` alphabet is unchanged: the seam is typed rather
than sniffed.

## Consequences

- **A caller can bring its own settings object.** Anything carrying the sixteen members drives
  `create_app`; this library never asks what class it is.
- **A dead field is now a failing test rather than a documented promise.**
  `test_the_protocol_declares_only_what_core_reads` parses `src/` and requires a reader for every
  member the protocol declares. It reads the source rather than the dataclass, because a field can
  be declared, defaulted, parsed and validated without anything ever consulting it — which is
  exactly how the two got here.
- **Two configuration variables are gone**, and with them the startup validation they carried:
  setting either now does nothing at all, where a malformed value previously stopped the server.
  The `config_loaded` boot event no longer carries `rate_limit_onboard`.
- **Breaking for a caller that pinned the old signature.** `create_app`'s `config` parameter is
  retyped and `check_rate_limit_imperative` gains a required argument, which is why this is a MINOR
  release: ADR 002 calls `create_app` the public composition API.
- **Both `# type: ignore[arg-type]` in `src/cassetta/app.py` became unnecessary.** One was the
  untyped route string; the other was the `DownloadError` registration, now made through the
  `@app.exception_handler` decorator like every other handler in that file.
- Cost: thirty-seven annotations across twelve files moved in one pass. mypy was the reviewer, and
  the sites that genuinely need the concrete type — `load_config()`'s return, and the construction
  of `AppConfig` itself — were deliberately left alone. A protocol used everywhere including where
  it cannot work would be worse than the honest mixture.

Implemented at `src/cassetta/protocols/config.py` (`CoreConfig`), `src/cassetta/config.py`
(`AppConfig`), `src/cassetta/app.py` (`create_app`, the 429 handler) and
`src/cassetta/rate_limit/limiter.py` (route attribution).

## Links

- Background: §VII Three-Layer Architecture — [ADR 001](001-three-layer-architecture.md); the
  composition half of the same question — [ADR 002](002-backendconfig-public-api.md); the
  self-identification convention — [ADR 003](003-protocol-self-identification.md).
- The guards that hold the decision: `tests/test_config_protocol.py`,
  `tests/test_rate_limit_route_attribution.py`, `tests/test_public_surface.py`.
