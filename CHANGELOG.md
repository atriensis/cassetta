# Changelog

Everything a running deployment or a dependent project would notice, release by release.

The format follows the Keep a Changelog convention; the project follows Semantic Versioning. Each
entry cites the pull request that landed the change. The two oldest releases predate this
repository's pull-request history and cite none.

## [0.26.7] - 2026-09-08

### Fixed

- The worked send example in the REST reference now completes when it is copied. Its manifest named
  the tar archive instead of the file inside it, which phase 1 accepted with a `201` and phase 2 then
  refused with `400 manifest_violation / extra_file` — naming the reader's own file as the extra one,
  so the message pointed at their payload rather than at the manifest they had copied. The manifest
  now names a tar member, as `docs/CLIENT_SETUP.md` always said it must, and the upload call carries
  the `Content-Type: application/x-tar` the client sends. (#29)
- The download command in the same block sends the `X-Sender` identity header. The sentence above it
  promised a "download-token + matching identity header (two-factor)" and the command sent only the
  token, so following the reference produced a `401`. The header was named correctly in three other
  documents and did not appear in the REST reference at all. (#29)
- `path` is documented as what it is: the bundle's name inside the recipient's inbox, used verbatim
  by the recipient in the `peek` and `pick` paths. The reference and `cassetta send --help` both
  spelled it like a local filename, and `--help` is the only reference for that flag anywhere. (#29)
- `CASSETTA_MCP_ALLOWED_HOSTS` is described where it is defined as a list of `Host` header values
  matched literally, port included, rather than as a list of hostnames. A client reaching the service
  on a non-default port sends the port in the header, so an operator who set the bare name got `421`
  on every MCP call while both the variable's description and their own configuration looked right.
  Both spellings have to be listed, which the troubleshooting example already showed and now
  explains. Authentication runs before the host check, so an unauthenticated probe answers `401` and
  never reveals the `421` — stated for anyone debugging one. (#29)
- `GET /keys` and the inbox listing show their response shapes, including that `GET /keys` returns no
  hash and no key material, that the listing's `size` is the content's total rather than the
  archive's, and that a reserved bundle which was never uploaded leaves no entry. (#29)

### Changed

- `scripts/smoke.sh` removes the API key store it created, alongside the `.env` it already removed.
  It had removed only the `.env`, leaving a store carrying `setup_done: true`; the documented
  quickstart's first call then answered `409 Setup already completed` — at the first step of the
  thing the reader had run the script to prove. A store that was already there is still never
  touched, and the pre-flight refusal that makes the removal safe is unchanged. Operator-visible
  consequence: running the script twice in a row previously hit that refusal and now does not. A
  tree poisoned by an older copy is cured by removing `data.keys/.cassetta-keys.json`, which the
  README now says. (#29)

Nothing is published to any index. No behaviour of the running service changes: no route, status
code, configuration variable, MCP tool or structured-log field was added, removed or renamed.

## [0.26.6] - 2026-09-08

### Changed

- The install commands in the documentation now pin the release you are reading about. They had gone
  a version stale without anything reporting it, because a pinned tag one release behind still
  resolves and still installs working code. A test now holds every documented pin to the declared
  version, and `scripts/sync-docs-version.py` updates them all in one idempotent run. (#28)
- The version is declared in one place, `src/cassetta/__init__.py`. The packaging metadata is derived
  from it at build time instead of repeating it, so a bump is one edit rather than two that had to be
  remembered together. An installed copy reports the same version it did before; only the shape of
  the metadata changed. (#28)
- Releases are tagged by the merge rather than by hand. A push to the default branch reads the
  declared version, and if no tag exists for it, creates the annotated tag on the merge commit and
  builds the distribution, checking that what was built carries the version that was tagged. A merge
  that changes no version does nothing. Tagging by hand before a squash merge named a commit that
  never reached the default branch, which is the mistake this removes. (#28)
- `make release-check` reads the version from the package and additionally compares the documented
  install pins against it. It is still shell only, and still runs in a clean clone with no virtual
  environment. (#28)

Nothing is published to any index. No behaviour changes: no route, status code, configuration
variable, MCP tool or structured-log field was added, removed or renamed.

## [0.26.5] - 2026-09-08

### Fixed

- The documents now agree with each other and with the shipped surface. Nine contradictions were
  settled against the implementation rather than between documents: registering the MCP server
  (`claude mcp add` and the required trailing slash, replacing a settings-file recipe the client's
  schema rejects and an address that redirects where MCP clients will not follow), the body for
  minting a key (`host` and `project`, never a pre-joined `label`) and the two credentials that
  endpoint accepts, addressing a recipient by the full `host:project` label, `X-Sender` as the
  identity half of the download route's two-factor check rather than a header the inbox routes read,
  and the passages describing endpoints and policies that are not in this repository. (#26)
- A bare recipient name is now documented as what it is: unvalidated passthrough. A `to` containing a
  colon is verified against the key store and a label nobody holds is refused; a `to` without one is
  accepted even for a recipient that has never existed, and the bundle lands in a namespace the
  intended reader is not listening on, with no error on either side. (#26)
- The single-user posture of inboxes is stated where inboxes are introduced, in the README, the
  endpoint table and the glossary: under the access policy this repository ships, any valid key may
  list and read any label's inbox. It was true before and written down nowhere. (#26)
- Installing the client is now possible from the documentation. No index carries this package, so
  `pip install cassetta` could never work; the two `uv` forms are given instead, pinned to a release
  tag, alongside a statement that nothing publishes it. (#26)
- The front page indexes every document under `docs/`. Three of nine were reachable from it, so the
  configuration reference, the glossary and the architecture decisions could not be found from the
  entry point; and the self-hosting steps name all three variables the server requires, plus the MCP
  host allowlist, instead of one. (#26)
- Version stamps are removed from the REST reference rather than renumbered — the OpenAPI document
  reports the version itself, and a number written into prose is the part that goes stale. Re-checking
  what they vouched for found the endpoint table already accurate and the capabilities sample wrong:
  it predates the `rest_send_init` feature the server advertises, so it is regenerated. (#26)

No behaviour changes: no route, status code, configuration variable, MCP tool or structured-log field
was added, removed or renamed.

## [0.26.4] - 2026-09-07

### Added

- The contributor agreement is now in the repository as `CLA.md` rather than sent on request, and
  `CONTRIBUTING.md` says how it differs from the Apache ICLA it is adapted from — a person rather
  than a foundation as counterparty, no nonprofit-purpose undertaking, a governing-law section, and
  Apache-specific mechanics dropped. The agreement carries a version number: a later version binds
  only the people who sign it.

No behaviour changes.

## [0.26.3] - 2026-09-07

### Added

- The repository now says what shipped, how to contribute and where to report a vulnerability:
  `CHANGELOG.md` covering every release from 0.23.0, `CONTRIBUTING.md`, and `SECURITY.md`. The
  supported-version answer is derived from the licence's per-version two-year clock rather than a
  table that goes stale silently. (#23)
- `make release-check` answers in one command whether the tree is internally consistent as a release:
  the two version sites agree and the changelog has a section for that version. It performs no
  release. (#23)
- Packaging metadata gains `[project.urls]`, so an installed copy carries a link back to the
  repository, its issues and its changelog. (#23)

No behaviour changes: no route, status code, configuration variable, MCP tool or structured-log field
moved.

## [0.26.2] - 2026-09-05

### Fixed

- The disk-pressure probe measures the two directory trees it owns rather than the whole volume they
  sit on, so another process writing to the same disk can no longer turn it red — or green. Test-only:
  the ceiling it asserts, and everything the server does, are unchanged. (#21)

## [0.26.1] - 2026-09-04

### Changed

- The message accompanying a `batch_required` upload rejection no longer says that batch transport is
  unavailable. The server advertises that capability, and hands back a batch URL and a token for it,
  while the text of the refusal denied it. The error code, the HTTP 422 status, the `constraint` /
  `limit` / `observed` fields and the stable `batch_required: ` prefix are all unchanged, so a client
  matching on the code or on the prefix is unaffected — only the prose differs. (#19)
- Development-era identifiers removed from comments, docstrings and documentation throughout, and
  seven documentation headings lose a parenthetical noting when a feature arrived. Those versions
  predate anything a reader of this repository can check out. No behaviour change. (#19)

## [0.26.0] - 2026-09-04

### Removed

- `CASSETTA_TRUSTED_PROXIES` and `CASSETTA_KEY_LABEL_CHARS` are no longer read. Both were parsed and
  validated at startup and then acted on by nothing, so setting either now has no effect and raises
  no error, and one key disappears from the `config_loaded` startup line. The reverse-proxy guidance
  the first one carried survives as prose in `docs/CONFIG.md`. (#17)
- The matching `trusted_proxies` and `key_label_chars` fields leave the public `AppConfig` dataclass.
  **Breaking for code that constructs or reads `AppConfig` directly.** Taken deliberately before the
  first published release rather than softened into a deprecation shim. (#17)

### Changed

- The error the server prints and exits on when `CASSETTA_PUBLIC_BASE_URL` is unset now offers a
  documentation example instead of one specific host name. (#17)
- `CASSETTA_INVITE_TTL_SECONDS` is documented as what it is: a value this repository validates and
  holds but does not act on. (#17)

## [0.25.4] - 2026-09-04

### Security

- The container health probe is declared once, in the image, so a deployment using the shipped
  `docker-compose.yml` gets the privilege drop that previously reached only direct image runs. A
  container-level healthcheck **replaces** the image's rather than merging with it, so the duplicate
  declaration meant the probe ran as root for the life of the container. Same command, same 30s
  interval, 3s timeout, 10s start period and 3 retries — under a different user. (#15)
- Every privilege drop now sets the kernel's `no_new_privs` bit. A service that has just given up
  root has no route back through a setuid binary. (#15)

## [0.25.3] - 2026-09-04

### Fixed

- The container corrects the ownership of its bind-mount points while it still holds root, then hands
  over to its unprivileged account — so the documented quickstart works on Linux at any host uid.
  Previously the first key the server was asked to mint answered `500 Internal Server Error`, because
  a bind mount passes host ownership through unchanged and the image runs as uid 1001. (#13)

### Changed

- Two things an operator will notice. `docker exec` without `--user` now lands as root; pass
  `--user cassetta` for the application account. And `./data` and `./data.keys` become owned by
  uid 1001 after a first start — modes are unchanged, so the documented copy-based backup still
  works, but writing into them by hand now needs elevated privileges. (#13)

## [0.25.2] - 2026-09-04

### Added

- Continuous integration on every pull request and on pushes to the default branch, plus a weekly run
  that builds the image, brings the stack up and walks the quickstart over HTTP. The same walk runs
  against a local clone as `scripts/smoke.sh`. (#11)

### Changed

- `uv run pytest` now deselects the disk-pressure probe by default and reports it as deselected. It
  stays reachable with `uv run pytest -m slow`. (#11)

### Fixed

- The README quickstart claimed that fetching a stored file returns its raw bytes. It returns a JSON
  envelope, and the content arrives in `files[0].content`, tagged `utf8` or `base64`. The quickstart
  now shows the actual envelope and the decode recipe; `docs/REST_API.md` had documented this
  correctly all along. (#11)

## [0.25.1] - 2026-09-03

### Changed

- The `cassetta_broadcast` tool description loses a trailing citation of the project's pre-release
  numbering. No tool is added, removed, renamed or re-signatured — this is simply the only published
  string in the change. (#9)
- Pre-release development citations removed from the published tree, along with nine pointers into a
  directory this repository does not contain. Comments, docstrings and prose only; no behaviour
  change. (#9)

## [0.25.0] - 2026-09-03

### Added

- `docs/CONFIG.md` now documents every `CASSETTA_*` variable the server reads, with its default and
  what it does, grouped by job. Fifteen of them were previously undocumented — including several
  without which the server cannot be started at all. Three are documented as read but inert, because
  describing an effect the code does not produce is worse than admitting there is none. (#7)

### Changed

- The `410 Gone` body for the retired inline-send endpoint changed shape: `reason` no longer carries
  its former literal value, and the `migration_guide` field is gone. `replacement: POST
  /upload/{bundle_path}` is unchanged and is the field that helps anyone holding an old client. **A
  client switching on the old `reason` value, or following the guide pointer, breaks.** (#7)

### Removed

- The upgrade guide, in full. Eleven of its sections addressed an operator upgrading from a version
  nobody outside this project has ever run, and it documented, with tables, configuration this
  repository's source does not read. (#7)

## [0.24.0] - 2026-09-02

### Added

- `configure_logging` and `create_app` accept `extra_log_trees`, so an application embedding this
  server hands in its own logger tree and gets the configured handler. A supplied tree is configured
  but not governed: same handler, same level, and its `propagate` attribute left untouched. (#5)

### Changed

- `configure_logging` no longer configures a hard-coded third logger tree belonging to a distribution
  that cannot be installed from this repository. A caller passing that same name observes identical
  behaviour — the handler attaches, the level is `DEBUG`, and propagation stays `True`. (#5)

## [0.23.1] - 2026-09-02

### Changed

- Re-cut so that the first tag anyone depends on is the swept tree. `0.23.0` points at the initial
  import, before the deployment surface, the tree-wide format pass and the publication sweep landed;
  a consumer pinning it would get none of them.

## [0.23.0] - 2026-09-01

### Added

- First tagged version of Cassetta as a standalone project: a file exchange bus for distributed AI
  agents, running as a small API service that agents reach over MCP or a plain REST API, with a
  local-filesystem storage backend and per-`host:project` API keys.
