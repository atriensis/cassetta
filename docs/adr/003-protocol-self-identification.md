# ADR 003 — Protocol Self-Identification via `kind: ClassVar[str]`

**Status**: Accepted — Brief 519, shipped **v0.11.0**

## Problem

To log *which* claim backend was active, `app.py` did two things that violate the
three-layer architecture ([ADR 001](001-three-layer-architecture.md)):

1. `isinstance(claim_store, FilesystemClaimStorage)` — a concrete Layer 3 class
   imported into Layer 2.
2. `type(x).__name__.removesuffix("ClaimStorage").lower()` — Layer 2 reverse-
   engineering a backend's identity from Layer 3 class-naming conventions.

Brief 518 had removed the `isinstance` but kept the string-mangling as a
scope-preserving compromise, leaving a `TODO` pointing at this decision. Layer 2
should not have to know how Layer 3 names or types itself.

## Decision

Every Layer 1 Protocol self-identifies through a `kind: ClassVar[str]` member, and
each implementation sets it: `"filesystem"` / `"core"` for the reference backends,
`"azure"` / `"cloud"` for the cloud ones. Layer 2 reads `claim_store.kind` —
no `isinstance`, no string-mangling, no Layer 3 knowledge. The member was added to
**all nine** Layer 1 Protocols for symmetry: one self-identifying Protocol among
eight that don't would be worse than a uniform convention.

## Consequences

- **Clean Layer-2 logging.** The lifespan emits the active backend by reading
  `backends.claim_store.kind` (see `src/cassetta/app.py`).
- **Observable change**: Azure now logs `claim_storage_backend=azure` (previously
  `=blob`). Operators grepping that field must update expectations — recorded in
  `MIGRATION.md`.
- `@runtime_checkable` + `ClassVar[str]` forced three `cloud/extensions/*` classes
  (`CloudAliasResolver`, `CloudIdentityProvider`, `TeamAccessPolicy`) to carry
  `kind = "cloud"` so they satisfy `isinstance` at runtime — three classes outside
  the original twelve-impl inventory.
- Two implementation gotchas: the impl side needs an explicit
  `kind: ClassVar[str] = "..."` (a bare assignment reads as an instance variable to
  mypy); and the three cloud classes above were easy to miss.

Verified in the tree today: `kind: ClassVar[str]` is declared on the nine Layer 1
Protocols under `src/cassetta/protocols/`, and `BlobClaimStorage.kind ==
"azure"` (`cloud/src/cassetta_cloud/backends/azure/claim_storage.py`).

## Links

- PR: <https://github.com/ximera239/cassetta/pull/40>
- Issue: <https://github.com/ximera239/cassetta/issues/37>
- Background: §VII Three-Layer Architecture — [ADR 001](001-three-layer-architecture.md); analyst archive
  `cassetta-protocol-self-identification.md` (not in this repo).
