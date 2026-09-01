# Glossary

Project vocabulary, terms only. Alphabetical. For *why* the architecture is shaped
this way, see [`docs/adr/`](adr/); for the rules, the
[constitution](../.specify/memory/constitution.md).

- **§VII** — Constitution Principle VII, the three-layer architecture (Layer 1
  abstractions / Layer 2 product logic / Layer 3 vendor backends). See
  [ADR 001](adr/001-three-layer-architecture.md).
- **Alias** — a friendly recipient name resolved to a concrete inbox/agent by an
  `AliasResolver` (Layer 1). Enables addressing without raw identifiers.
- **AST lock** — a regression test that parses the abstract syntax tree of
  `src/cassetta/` to forbid §VII violations (concrete-class type annotations
  or vendor imports leaking into Layer 2).
- **Azurite axis** — the test axis that runs against Azurite (the Azure Blob
  emulator, via Docker) to exercise cloud backends; complements the filesystem
  axis. Skipped on Docker-less workstations.
- **BackendConfig** — the frozen dataclass composing the nine backend
  implementations passed to `create_app`. See
  [ADR 002](adr/002-backendconfig-public-api.md).
- **Brief** — a numbered, immutable unit of work authored by the analyst stating
  intent and constraints. Lives in `inbox/`, then `processed/`.
- **Bundle** — a set of files transferred atomically as one unit, stored per-file
  with a `meta.json` manifest.
- **Claim** — download-claim state: a recorded, JWT-backed reservation of a bundle
  for reference-mode download, persisted via the `ClaimStorage` Protocol.
- **Core / Cloud** — the open-core split. Core (this repository) = open-source
  single-user self-host; `cloud/` = commercial multi-tenant extensions. Core never
  imports `cloud/`.
- **Dev mode** — relaxed local-development posture, enabled by setting
  `CASSETTA_SETUP_TOKEN` to an empty string (and using a dev-only JWT key). Not for
  production.
- **Inbox** — a per-recipient mailbox a sender addresses; the recipient later picks
  from it.
- **Multicast** — addressing a single send to multiple recipients at once.
- **Pick** — a recipient retrieving a bundle from its inbox (`cassetta_pick` /
  inbox `pick` action).
- **Reference mode** — download via a reference envelope + claim, rather than
  inline transfer; the downloader resolves the claim to fetch bytes.
- **Send** — placing a bundle into one or more recipient inboxes.
- **Setup token** — the secret (`CASSETTA_SETUP_TOKEN`) that bootstraps client
  onboarding; an empty value selects dev mode.
