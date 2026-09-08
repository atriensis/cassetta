# Glossary

Project vocabulary, terms only. Alphabetical. For *why* the architecture is shaped
this way, see [`docs/adr/`](adr/) — [ADR 001](adr/001-three-layer-architecture.md)
carries the three-layer rules the rest of the vocabulary assumes.

- **§VII** — Constitution Principle VII, the three-layer architecture (Layer 1
  abstractions / Layer 2 product logic / Layer 3 vendor backends). See
  [ADR 001](adr/001-three-layer-architecture.md).
- **Alias** — a friendly recipient name resolved to a concrete inbox/agent by an
  `AliasResolver` (Layer 1). Enables addressing without raw identifiers.
- **AST lock** — a regression test that parses the abstract syntax tree of
  `src/cassetta/` to forbid §VII violations (concrete-class type annotations
  or vendor imports leaking into Layer 2).
- **Backend axis** — the parameter the claim-storage tests are run over. In this
  repository it has exactly one value, `filesystem`. The parametrisation is a
  seam: a distribution that adds a vendor backend adds a value to the axis rather
  than forking the suite.
- **BackendConfig** — the frozen dataclass composing the nine backend
  implementations passed to `create_app`. See
  [ADR 002](adr/002-backendconfig-public-api.md).
- **Bundle** — a set of files transferred atomically as one unit, stored per-file
  with a `meta.json` manifest.
- **Claim** — download-claim state: a recorded, JWT-backed reservation of a bundle
  for reference-mode download, persisted via the `ClaimStorage` Protocol.
- **Core / Cloud** — the open-core split. Core (this repository) = open-source
  single-user self-host; Cloud = commercial multi-tenant extensions, a separate
  distribution that depends on this one. The dependency only ever points that way,
  so nothing described as Cloud is present here: no accounts, no teams, no
  team-scoped visibility, no `/admin/*` routes. Where the documentation mentions
  one of those, it says so at that point and means "not in this repository".
- **Dev mode** — relaxed local-development posture, enabled by setting
  `CASSETTA_SETUP_TOKEN` to an empty string (and using a dev-only JWT key). Not for
  production.
- **Inbox** — a per-recipient mailbox a sender addresses; the recipient later picks
  from it. Addressed by the recipient's full `host:project` key label. It separates
  recipients but does not isolate them: under the access policy this repository
  ships, any valid key may read any label's inbox.
- **Multicast** — addressing a single send to multiple recipients at once.
- **Pick** — a recipient retrieving a bundle from its inbox (`cassetta_pick` /
  inbox `pick` action).
- **Reference mode** — download via a reference envelope + claim, rather than
  inline transfer; the downloader resolves the claim to fetch bytes.
- **Send** — placing a bundle into one or more recipient inboxes.
- **Setup token** — the secret (`CASSETTA_SETUP_TOKEN`) that bootstraps client
  onboarding; an empty value selects dev mode.
- **X-Sender** — the identity header on `GET /download/{bundle_path}/{name}`, and
  on that route only. It is the second factor of a two-factor check: the download
  JWT proves the claim, this header declares who is redeeming it, and a value that
  does not match the token's `recipient` is refused. Not an identity header for the
  API at large — `/inbox/*` and `/files/*` do not read it.
