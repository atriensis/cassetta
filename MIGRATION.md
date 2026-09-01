# Migration Guide: Namespace Isolation (Brief 502)

## What Changed

Storage backends now organize data under three prefixes:
- `data/` — user files (both /files/ and /inbox/)
- `keys/` — API key store
- `locks/` — lease lock files

Previously, files were stored flat in the root, with `__inbox__/` and `__locks__/` prefixes.

## Filesystem Backend Migration

**Prerequisites**: Stop the Cassetta service before migrating.

```bash
STORAGE_ROOT="/opt/cassetta/storage"  # or your configured CASSETTA_STORAGE_PATH

# 1. Create namespace directories
mkdir -p "$STORAGE_ROOT/data" "$STORAGE_ROOT/keys" "$STORAGE_ROOT/locks"

# 2. Move user files into data/
cd "$STORAGE_ROOT"
for item in *; do
  case "$item" in
    data|keys|locks|__locks__|__inbox__) continue ;;
    *) mv "$item" data/ ;;
  esac
done

# 3. Move inbox files
if [ -d "$STORAGE_ROOT/__inbox__" ]; then
  mkdir -p "$STORAGE_ROOT/data/inbox"
  mv "$STORAGE_ROOT/__inbox__/"* "$STORAGE_ROOT/data/inbox/" 2>/dev/null
  rmdir "$STORAGE_ROOT/__inbox__" 2>/dev/null
fi

# 4. Remove old lock files (leases are ephemeral)
rm -rf "$STORAGE_ROOT/__locks__"

# 5. Move keys file into storage root
KEYS_DIR="${STORAGE_ROOT}.keys"
if [ -d "$KEYS_DIR" ]; then
  mv "$KEYS_DIR/.cassetta-keys.json" "$STORAGE_ROOT/keys/"
  rmdir "$KEYS_DIR" 2>/dev/null
fi

# 6. Restart service
```

**Idempotency**: Running these steps again is safe — `mkdir -p` and `mv` are no-ops if already done.

## Azure Blob Backend Migration

Use Azure CLI or Storage Explorer:

1. Copy all non-prefixed blobs to `data/` prefix
2. Copy `__inbox__/*` blobs to `data/inbox/*`
3. Delete `__locks__/*` blobs (ephemeral)
4. Ensure `keys/.cassetta-keys.json` exists in the container
5. Delete old blobs after verification

## New Environment Variables

- `CASSETTA_KEYSTORE_BACKEND` — `file` (default) or `azure_blob`

The `CASSETTA_KEYS_FILE` variable is still supported for custom FileKeyStore paths.

# Brief 503 — Identity strategy + user/team store (v0.2.0)

## What Changed

Identity, access policy, alias resolution, and key-label validation become
pluggable Protocols. Core ships `default` strategies for each that match
the prior implicit single-host self-host behaviour; cloud may register
team-aware alternatives via the same env vars.

## Environment Variables

| Name | Default | Meaning |
|---|---|---|
| `CASSETTA_IDENTITY_PROVIDER` | `default` | Names the identity-provider implementation. `default` keeps the prior API-key → identity mapping. |
| `CASSETTA_ACCESS_POLICY` | `default` | Names the access-policy implementation. `default` allows all (single-host self-host); cloud teams use `team`. |
| `CASSETTA_INVITE_TTL_SECONDS` | `604800` | Invite-token lifetime in seconds (default 7 days). Used by Brief 504's invite flow. |
| `CASSETTA_KEY_LABEL_CHARS` | `a-zA-Z0-9_\-` | Regex character class allowed in API-key labels. |

## Operator-visible Logs

DEBUG-level identity / access-policy decision events accompany every
authenticated request. The exact event names are `identity.resolved`
and `access_policy.evaluated`.

## Operator Action Required

None for self-host. Defaults match the prior implicit behaviour. Set
`CASSETTA_ACCESS_POLICY=team` only on cloud-tier deployments that wire
a `TeamAccessPolicy` (cross-team isolation).

# Brief 504 — RBAC + invite tokens (v0.2.0)

## What Changed

Onboarding moves to invite-token flow. The setup-token gate gains a
header alternative (`X-Setup-Token`). Cloud admin endpoints land but
remain mount-gated by Brief 525's `CASSETTA_ADMIN_ROUTES=enabled`.

## REST API

- **Added** — `POST /onboard` — accept an invite token, mint API key.
- **Added** — `POST /admin/invites` — issue invite token (cloud-axis;
  admin opt-in via Brief 525).
- **Added** — `GET /admin/users` — list users (cloud-axis; admin opt-in
  via Brief 525).
- **Added** — `GET /admin/teams` — list teams (cloud-axis; admin opt-in
  via Brief 525).
- **Added** — `X-Setup-Token` request header — alternative to the
  `?x_setup_token=` query parameter for `/setup` and `/keys`. Both
  forms remain accepted; the header is the recommended form.

## Operator-visible Logs

Invite issuance / consumption events: `invite.issued`,
`invite.consumed`, `invite.consume_failed` (with `reason`).

## Operator Action Required

None for self-host. The four cloud admin endpoints above are gated
behind `CASSETTA_ADMIN_ROUTES=enabled` from Brief 525 onward — see
the Brief 525 section below for current mount semantics and the
`security_model=unrestricted_admin` warning.

# Brief 507 — Observability — log format selection (v0.4.0)

## What Changed

The structured-log emitter gains a JSON output format alongside the
existing human-readable text format. Switching mid-deploy is safe —
no on-disk state — provided the ingest layer is ready for the new
shape.

## Environment Variables

| Name | Default | Meaning |
|---|---|---|
| `CASSETTA_LOG_FORMAT` | `text` | One of `text` or `json`. Invalid values fall back to `text` with a stderr warning. |

## Operator-visible Logs

Every existing structured-log event renders in the chosen format.
Examples for the same `config_loaded` event:

Text:

```
2026-04-13 09:12:34 INFO cassetta config_loaded primary_key_source=env public_base_url=http://localhost:16001
```

JSON:

```
{"ts":"2026-04-13T09:12:34Z","level":"INFO","logger":"cassetta","event":"config_loaded","primary_key_source":"env","public_base_url":"http://localhost:16001"}
```

## Operator Action Required

The ingest layer (Loki / Splunk / Elastic / etc.) MUST handle the
chosen format. Switching is safe in either direction.

# Brief 508 — Discovery, aliases, multicast (v0.4.0)

## What Changed

Inbox target resolution now goes through the `AliasResolver` Protocol.
Direct labels (`alice`, `bob`) resolve verbatim as before; aliases like
`team:alpha` expand to a list of recipient labels at send time. The
resolver is pluggable via `CASSETTA_ALIAS_RESOLVER`.

## Environment Variables

| Name | Default | Meaning |
|---|---|---|
| `CASSETTA_ALIAS_RESOLVER` | `default` | Names the alias-resolver implementation. `default` resolves only direct labels; cloud may register team-aware multicast resolvers. |

## Operator Action Required

None for self-host. Cross-team multicast lands in cloud builds wiring
a team-aware resolver.

# Brief 509 — Multi-file bundles (v0.6.0)

## What Changed

`PUT /files/{path}` becomes the single write surface for both
single-file and multi-file (tar/multipart) payloads. The single-file
wire shape is unchanged; multi-file bundles are new. Path collisions
return HTTP 409 with a structured body identifying the conflicting
bundle.

## REST API

- **Changed** — `PUT /files/{path}` — accepts both single-file and
  multi-file payloads. Single-file behaviour is unchanged on the wire.
- **Changed** — 409 response body shape on path collision:

  ```json
  {"error": "bundle_path_conflict",
   "path": "<colliding bundle path>",
   "bundle_id": "<existing bundle id>"}
  ```

## Operator Action Required

None. Pre-existing single-file uploads continue to work.

# Migration to bundle directory format (Brief 512)

## What Changed

Bundles on disk move from a single tar blob per bundle to a directory per
bundle with a `meta.json` sidecar. The `meta.json` file is written last via
atomic rename — its presence is the sole visibility gate.

**There is no automatic migration.** Bundles written by older versions are
not readable by the new storage layer and must be wiped before upgrade.

## Filesystem Migration

**Prerequisites**: Stop the Cassetta service before migrating.

```bash
# 1. Stop the service
systemctl stop cassetta   # or your equivalent

# 2. Optional backup
tar czf cassetta-data-backup.tgz "$CASSETTA_STORAGE_PATH"

# 3. Wipe the data directory (keys/ and locks/ will be recreated on demand
#    if you also wipe them, but typically keys survive across upgrades;
#    only inbox/ and store/ are affected by the bundle format change).
rm -rf "$CASSETTA_STORAGE_PATH/data"

# 4. Deploy the new version and start the service
systemctl start cassetta
```

The Ansible role at `infra/ansible/roles/cassetta/` can be extended with a
one-shot task gated on `cassetta_wipe_data=true` for operator convenience;
that change is not required for the upgrade to work.

## Azure Blob Migration

Azure Blob bundle support ships in a follow-up brief. On the current
release, Azure Blob `open_bundle_write` and related bundle operations raise
`NotImplementedError`. Deployments using the Azure backend for inbox/store
traffic should delay the upgrade until the cloud bundle track lands.

## What clients see

REST and MCP response bodies for bundle listings now include per-bundle
`files[]` entries with `name`, `size`, `mime`, plus a `bundle_id`. The
multi-file bundle download response carries a `bundle: { ... }` envelope
with the full manifest. `PUT /files/{path}` on a colliding path returns
HTTP 409 with a structured body (`bundle_path_conflict`). Single-file
downloads and single-file send semantics are unchanged on the wire.

# Migration to LimitsPolicy (Brief 513)

## What changed

`CASSETTA_MAX_FILE_SIZE` is retired. Upload size rejection is now owned
by the pluggable `LimitsPolicy` Protocol, with a new `CoreLimitsPolicy`
reference implementation that reads configuration from a richer set of
env vars.

See `docs/CONFIG.md` for the complete reference.

## Env-var mapping

| Old | New | Notes |
|---|---|---|
| `CASSETTA_MAX_FILE_SIZE` | `CASSETTA_PER_FILE_MAX` | Old var is ignored; a stderr warning is printed when it is set so operators notice the move. |
| — | `CASSETTA_PER_BUNDLE_TOTAL_MAX` | Total bytes per bundle. Unset by default. |
| — | `CASSETTA_PER_BUNDLE_FILE_COUNT_MAX` | Default 25. Empty string unsets the cap. |
| — | `CASSETTA_MAX_INLINE_SIZE` | Default 100 KB. Payloads above this size will require the batch transport (brief 514); meanwhile they are rejected with HTTP 422 / `batch_required`. |
| — | `CASSETTA_UPLOAD_TOKEN_TTL` / `CASSETTA_DOWNLOAD_CLAIM_TTL` / `CASSETTA_PASSIVE_GC_MIN_AGE` / `CASSETTA_PASSIVE_GC_INTERVAL` | Plumbed but not consumed by brief 513; landing seam for briefs 514/515. |

## Wire-format change

Oversized payloads now surface as structured JSON on REST
(`{"error": "cap_exceeded"|"batch_required", "constraint": ..., "limit": ..., "observed": ...}`
with HTTP 413 or 422) and as `ValueError` messages whose prefix is
`cap_exceeded:` / `batch_required:` on MCP. Any client that matched
the previous `"File too large"` substring must be updated.

# Brief 514 — Upload flow

## What changed

The inline-only `cassetta_send(content=…)` MCP tool and its matching
REST send endpoint (`PUT /inbox/{agent}/{path}`) are **replaced** by a
two-step policy-gated flow:

1. **`cassetta_send_init(to, manifest)`** — manifest-only MCP call. The
   server validates each name (shape + reserved + duplicate + prefix
   collision), consults `LimitsPolicy.evaluate_upload` (brief 513), and
   returns a signed **upload credential** (HS256 JWT) plus a mode.
2. Either **`cassetta_send_inline(token, files)`** (small bundles) or
   **`POST /upload/{bundle_path}`** (streaming tar, typically driven by
   the bundled `cassetta upload` CLI) to actually move bytes.

The JWT carries the full manifest, the destination `bundle_path`, the
audit `bundle_id`, and validity bounds. The server is stateless with
respect to credentials — no session store, no active revocation ledger.

## Old → new mapping

| Old | New |
|---|---|
| MCP `cassetta_send(path, to, content=…)` | MCP `cassetta_send_init` → `cassetta_send_inline` |
| MCP `cassetta_send(path, to, files=…)` (bundle) | MCP `cassetta_send_init` → `cassetta_send_inline` (inline branch) or the CLI (`cassetta upload`) for batch |
| REST `PUT /inbox/{agent}/{path}` | MCP `cassetta_send_init` → REST `POST /upload/{bundle_path}` |

Callers that still hit the legacy REST path receive
`HTTP 410 Gone` with a structured body pointing at the replacement:

```json
{"error": "gone", "reason": "replaced_by_514",
 "replacement": "POST /upload/{bundle_path}",
 "migration_guide": "MIGRATION.md#brief-514"}
```

## New environment variables

| Name | Required | Meaning |
|---|---|---|
| `CASSETTA_JWT_KEY` | yes (or `_FILE`) | Primary HS256 signing key, base64-encoded, ≥32 bytes decoded. |
| `CASSETTA_JWT_KEY_FILE` | yes (or value) | Path to a file containing the primary key. Wins over `CASSETTA_JWT_KEY` if both set. |
| `CASSETTA_JWT_KEY_SECONDARY` | no | Verify-only secondary key for key rotation. |
| `CASSETTA_JWT_KEY_SECONDARY_FILE` | no | Path variant of the secondary key. |
| `CASSETTA_PUBLIC_BASE_URL` | **yes** | Absolute base URL (scheme mandatory) used to compose `upload_url` in batch responses. Trailing slash optional. |

### Generating a key

```bash
mkdir -p ~/.config/cassetta
openssl rand -base64 32 > ~/.config/cassetta/jwt.key
chmod 600 ~/.config/cassetta/jwt.key
export CASSETTA_JWT_KEY_FILE=$HOME/.config/cassetta/jwt.key

# dev
export CASSETTA_PUBLIC_BASE_URL=http://localhost:16001
# Pi / home-lab
# export CASSETTA_PUBLIC_BASE_URL=https://dev1.cassetta.ai
```

### Rotation (no downtime)

```bash
# Promote a new primary, keep old as verify-only secondary
openssl rand -base64 32 > ~/.config/cassetta/jwt.new.key
chmod 600 ~/.config/cassetta/jwt.new.key
export CASSETTA_JWT_KEY_SECONDARY_FILE=$HOME/.config/cassetta/jwt.key
export CASSETTA_JWT_KEY_FILE=$HOME/.config/cassetta/jwt.new.key
# restart; wait `upload_token_ttl` seconds (default 5 min)
unset CASSETTA_JWT_KEY_SECONDARY_FILE
rm ~/.config/cassetta/jwt.key
# restart
```

## CLI (new)

`cassetta` is now a console script (`[project.scripts]`). The upload
subcommand streams a tar archive to `/upload/{bundle_path}` using the
credential returned by `send_init`:

```bash
cassetta upload \
  --url  "https://dev1.cassetta.ai/upload/inbox%2Falice%2Fproject-drop.tgz" \
  --token "eyJhbGciOiJIUzI1Ni..." \
  src/main.py data/big.bin README.md
```

`--no-compress` sends `application/x-tar` without gzip. Positional
arguments become tar entry names verbatim after `validate_path`-equivalent
normalisation; they MUST match the manifest names declared to
`send_init`.

## Observability

New structured log events:
- `config_loaded` (startup) — records `primary_key_source`, `secondary_key` presence, `public_base_url`.
- `upload_init` — one per `send_init` call (success path).
- `upload_stream_start` / `upload_stream_complete` — per upload.
- `upload_manifest_violation` / `upload_rollback` — per failure.
- `gc_scheduled` (startup) / `gc_reaped` (per deletion).

Secrets (keys) are never logged, only sources.

# Brief 515 — Download flow

## What changed

Every read surface — MCP `cassetta_pick` / `cassetta_get` and REST
`GET /files/{path}`, `GET /inbox/{agent}/{path}`, and the `pick` REST
endpoint — becomes **policy-driven**. Small bundles (under
`max_inline_size`) return an **inline envelope**; large bundles return a
**reference envelope** with per-file URLs and a single download JWT.
File bytes never cross the MCP / LLM context window for over-threshold
bundles.

**Breaking** — the legacy single-file raw-body response shape is retired
symmetrically with Brief 514's write-surface retirement. Every read
surface now returns `application/json`; there is no raw-body / raw-string
fork.

### Response shape (both modes)

Inline:

```json
{
  "mode": "inline",
  "bundle": { /* full meta.json */ },
  "files": [
    {"name": "notes.md", "content": "# hello\n", "encoding": "utf8"},
    {"name": "logo.png", "content": "iVBORw0K...", "encoding": "base64"}
  ]
}
```

Reference:

```json
{
  "mode": "reference",
  "bundle": { /* full meta.json */ },
  "files": [
    {"name": "notes.md", "size": 120000, "mime": "text/markdown",
     "url": "https://host/download/inbox%2Falice%2Fdrop/notes.md"}
  ],
  "download_token": "eyJhbGciOi…",
  "expires_at": "2026-04-20T22:45:00Z"
}
```

Single-file bundles go through the same branching — they become a
1-entry `files` array in whichever mode the policy selects.

### New endpoint

`GET /download/{bundle_path}/{name}` — streams one file per request.

- **`Authorization: Bearer <download_token>`** — validates signature,
  `exp`, `nbf`, `bundle_path` claim (matches URL), `name` in the
  credential's `file_names` list.
- **Identity header** — the existing API-key / sender-header plumbing
  used by `/inbox/*` and `/files/*`. The resolved identity MUST equal
  the JWT's `recipient` claim (two-factor auth).
- Successful fetches append `name` to the claim's `files_fetched` set
  (inbox only). When the set covers every manifest entry, the bundle
  and claim are both deleted. Store bundles are never deleted by the
  download path.

Status surface: `200` stream, `401` any JWT-class failure, `403`
identity mismatch, `404` bundle / file missing. No 410. Body shape on
errors is flat `{"error": ..., "reason": ...}` (not wrapped in `detail`).

### Claim sidecar (inbox only)

Reference-mode `pick` writes a claim sidecar before returning the
credential (FR-011a ordering invariant — credential must not escape
until the sidecar is durably on disk). The sidecar hides the bundle
from `cassetta_inbox` listings for the duration of the TTL (default 15
min, `CASSETTA_DOWNLOAD_CLAIM_TTL`). Claim records survive server
restart.

The reaper (extended from Brief 514) sweeps expired claims: complete +
inbox → delete bundle + claim; incomplete → drop claim only (bundle
reappears in listings); bundle missing → drop claim only.

Store `cassetta_get` writes **no** sidecar — all claim machinery is
inbox-only.

## Old → new mapping

| Old | New |
|---|---|
| MCP `cassetta_pick(path)` returns raw string (single-file) or JSON bundle (multi-file) | MCP `cassetta_pick(path)` returns a **unified JSON envelope** — `mode: "inline"` with `files[]` for small bundles, `mode: "reference"` with URLs for large bundles |
| MCP `cassetta_get(path)` — same dual return shape | Same unified JSON envelope |
| REST `GET /files/{path}` returns `Response(bytes, media_type=mime)` for single-file, JSON for multi-file | Unified JSON envelope for every bundle, both modes |
| REST `GET /inbox/{agent}/{path}` — same dual return shape | Same unified JSON envelope |
| REST DELETE-on-read pick — same dual return shape | Same unified JSON envelope |
| (no equivalent) | REST `GET /download/{bundle_path}/{name}` — per-file streaming endpoint, auth'd by the envelope's `download_token` |

Callers that parsed `response.content` bytes or the unwrapped raw string
need to switch to:

```python
envelope = response.json()
if envelope["mode"] == "inline":
    for f in envelope["files"]:
        data = f["content"].encode() if f["encoding"] == "utf8" \
               else base64.b64decode(f["content"])
else:
    # reference mode — stream each file[i]["url"] with the token
    for f in envelope["files"]:
        resp = httpx.get(f["url"],
            headers={"Authorization": f"Bearer {envelope['download_token']}",
                     "X-Sender": identity_label})
```

Or — simpler — shell out to the bundled CLI (below).

## CLI (new subcommand)

`cassetta download` reads a reference envelope from stdin (or a file)
and streams each file to `--out/<name>`:

```bash
# Pipe the tool response straight in.
jq -c . manifest.json | cassetta download --manifest-json - --out ./drop

# Or from a file.
cassetta download --manifest-json manifest.json --out ./drop
```

The CLI:
- Decodes the JWT's `recipient` claim (without verification — server
  verifies) and sends it on `X-Sender`.
- One Authorization-header-bearer GET per file, streaming the response
  body to disk.
- Exit 0 on full success; exit 2 on manifest / mode problems or HTTP
  4xx; exit 1 on network failures. Server error bodies are echoed to
  stderr.

`--manifest-json` accepts `-` for stdin.

## New environment variables

| Name | Default | Meaning |
|---|---|---|
| `CASSETTA_DOWNLOAD_TTL` | `900` (15 min) | Download-JWT lifetime in seconds. Also the claim-sidecar TTL window for inbox claims. |

No other new env vars — the download endpoint reuses
`CASSETTA_JWT_KEY` / `CASSETTA_JWT_KEY_FILE` (and the secondary) from
Brief 514.

## Observability

New structured log events (all on the `cassetta` logger, per FR-026):

- `download_mode_decision` — every read surface, after policy eval.
- `download_claim_issued` — inbox reference-mode pick, at sidecar write.
- `download_file_fetched` — one per successful file stream.
- `download_claim_completed` — all files fetched, bundle + claim deleted.
- `download_claim_expired` — reaper swept an expired claim.
- `download_jwt_validation_failed` — 401-class failures.
- `download_identity_mismatch` — 403-class failures.

# Brief 516 — Advertise limits handshake (v0.9.0)

## What Changed

The server advertises its upload caps, TTLs, supported transport
modes, and feature flags on a single endpoint. Agents can call once
per session, cache the result, and fail fast on oversized payloads
before a `cassetta_send_init` round-trip.

## REST API

- **Added** — `GET /capabilities` — returns the capabilities document
  (limits, TTLs, supported modes, features). Auth follows the
  standard bearer-token rules; the document is the same for every
  authenticated identity.

## MCP Tools

- **Added** — `cassetta_capabilities` — returns the same document,
  JSON-stringified, for MCP clients.

## CLI

- **Added** — `cassetta capabilities --url <base>` — pretty-prints the
  capabilities document for operators.

## Operator-visible Logs

- DEBUG `capabilities.queried` — emitted once per call, with a
  `via=mcp|rest|cli` discriminator.

## Operator Action Required

None. `schema_version` is an integer; the server bumps it only on
non-additive wire changes, so agents can pin on major-version parity
and ignore additive future fields.

# Migration Guide: ClaimStore → ClaimStorage (Brief 517)

## What Changed

The Brief-515 concrete class ``ClaimStore`` is retired in favour of a
Protocol-based split:

- **Layer 1** (new) — ``cassetta.protocols.claim_storage`` —
  ``ClaimStorage`` Protocol + ``BundleClaimedError`` exception.
- **Layer 3 core** (new path) —
  ``cassetta.backends.filesystem.claim_storage.FilesystemClaimStorage`` —
  behaviour-identical to the old ``ClaimStore``, same on-disk layout.
- **Layer 3 cloud** (new) —
  ``cassetta_cloud.backends.azure.claim_storage.BlobClaimStorage`` —
  new Azure-Blob-backed impl used automatically when
  ``CASSETTA_STORAGE_BACKEND=azure_blob``.

All Protocol methods are ``async def``. Layer 2 call sites
(``app.py``, ``downloads.py``, ``gc.py``, ``mcp_server.py``,
``routes/``) prepend ``await`` to every ``claim_store.*`` call and
annotate the Protocol type.

``cassetta.claims`` still exposes ``ClaimRecord``,
``BundleClaimedError``, ``ClaimStorage`` and ``FilesystemClaimStorage``
(re-exported) so existing ``from cassetta.claims import ...``
statements keep working once ``ClaimStore`` is renamed to
``FilesystemClaimStorage``.

## Filesystem deployments

**No action required.** The on-disk layout is unchanged:

- ``<storage_path>/.claims/{jti}.json`` — sidecar bodies (same JSON
  schema, ``schema_version=1``).
- ``<storage_path>/.claims/.<sha256(bundle_path)[:16]>.lock`` — lock
  dotfiles.

Existing claims created under Brief 515 are readable by
``FilesystemClaimStorage`` without any migration step.

Startup log emits ``claim_storage_backend=filesystem`` at INFO.

## Azure multi-pod deployments

Redeploy v0.x.y+1. With ``CASSETTA_STORAGE_BACKEND=azure_blob`` set
(same env var as the bundle backend), ``bootstrap_cloud_app`` now
wires ``BlobClaimStorage`` against a dedicated ``claims`` container
alongside the existing ``cassetta-data`` and ``cassetta-keys``
containers. The container is auto-created lazily on first ``issue()``.

Schedule the deployment during a quiet window — in-flight filesystem
claims sitting on pod-local ``.claims/`` disks are lost when pods are
replaced. Affected recipients simply re-pick after the existing TTL
window expires.

Startup log emits ``claim_storage_backend=azure_blob`` at INFO from
``cassetta_cloud.app_setup``.

## Shared BlobServiceClient

A single ``BlobServiceClient`` (with uniform retry kwargs — FR-025) is
now shared across all three Azure backends:

- ``AzureBlobBackend`` — bundle storage
- ``BlobKeyStore`` — API keys
- ``BlobClaimStorage`` — download claims

Constructor signatures changed: ``AzureBlobBackend(svc, container_name=...)``
and ``BlobKeyStore(svc, container_name=...)`` now take a
pre-constructed ``BlobServiceClient`` instead of credentials. Callers
outside ``bootstrap_cloud_app`` must update accordingly.

## New environment variables

None. ``CASSETTA_STORAGE_BACKEND`` (from Brief 501) continues to be
the single switch — ``azure_blob`` now implies Azure-backed claim
storage in addition to Azure-backed bundles and keys.

# Brief 518 — Core defaults factory (v0.11.0)

## What Changed

`build_core_defaults(config)` becomes the single Layer 3 factory that
Layer 2 ever names for the core (filesystem) axis. The cloud bootstrap
path retains its own `_build_cloud_backends(config)` helper. The
factory wires every backend by reading the active `AppConfig` — no
`if backend == "vendor"` branches remain in Layer 2.

## Operator-visible Logs

- **Added** — INFO boot-log event: `claim_storage_backend=filesystem`
  on the core (filesystem) axis, or `claim_storage_backend=azure_blob`
  on the cloud (Azure) axis. Operators MAY assert on this line for
  deployment-axis verification (`grep claim_storage_backend=` on
  startup logs).

## Operator Action Required

None. The boot-log line is informational. Note that Brief 519 renames
the cloud value from `azure_blob` to `azure` — see the next section.

# Brief 519 — Protocol self-identification (v0.11.0)

## What Changed

Every Layer 1 Protocol gains a `kind` class-level constant that
identifies the implementation (`core`, `azure`, `filesystem`, ...).
The `claim_storage_backend` boot-log value migrates from the prior
implementation-name string to the Protocol's `kind`, so the cloud
value renames from `blob` (or `azure_blob`) to `azure`.

## Operator-visible Logs

- **Renamed** — `claim_storage_backend=blob` → `claim_storage_backend=azure`
  on the cloud axis. The filesystem axis keeps `claim_storage_backend=filesystem`.
- **Added** — `kind` field on Layer-1 Protocol implementations
  (visible in capability-log decoration and structured-log emission).

## Operator Action Required

Ingest layers that grep for `claim_storage_backend=blob` (or
`claim_storage_backend=azure_blob`) MUST switch to
`claim_storage_backend=azure`. The filesystem axis is unchanged.

# Migration Guide: BackendConfig public API (Brief 520, v0.12.0)

## What Changed

`create_app` and `mcp_server.configure` collapse their nine per-backend
kwargs into a single `backends: BackendConfig` parameter.

- Old: `create_app(config, backend=..., key_store=..., identity_provider=..., access_policy=..., alias_resolver=..., limits_policy=..., metrics_provider=..., reference_transport=..., claim_store=...)`
- New: `create_app(config, backends=BackendConfig(...))`

Same collapse for `mcp_server.configure`:
- Old: `configure(backend, config, access_policy=..., alias_resolver=..., key_store=..., metrics=..., limits_policy=..., reference_transport=..., claim_store=...)`
- New: `configure(config, backends)`

`BackendConfig` is a frozen dataclass with the same nine fields. Partial
overrides use `dataclasses.replace(build_core_defaults(config), <field>=...)`.

The lifespan now stashes the full bundle at `app.state.backends`; the nine
individual `app.state.backend`, `app.state.key_store`, etc. attributes are
gone. Every Layer 2 consumer reads `app.state.backends.<field>` instead.

## Why

Constitution §VII (three-layer architecture). With nine Protocols, the
9-kwarg constructor was a growing drift magnet — new backends required
touching Layer 2. The BackendConfig bundle replaces nine code paths with
one, and `build_core_defaults(config)` is the single Layer 3 factory that
Layer 2 ever names.

## Caller migration

**Typical Layer 2 consumer (core, dev, self-host)** — no change. If you
call `create_app()` with no kwargs, the lifespan now builds its bundle
via `build_core_defaults(config)` internally; behaviour is identical.

**Callers that previously injected a single override** (most tests):
```python
# Before
app = create_app(config, limits_policy=custom)

# After
from dataclasses import replace
from cassetta.defaults.factory import build_core_defaults

bundle = replace(build_core_defaults(config), limits_policy=custom)
app = create_app(config, backends=bundle)
```

When overriding `key_store`, also override `alias_resolver` with a
`DefaultAliasResolver(key_store=new_key_store)` — the two share state
(see `cassetta.defaults.factory.build_core_defaults` docstring).

**Cloud bootstrap** (`cassetta_cloud.app_setup.bootstrap_cloud_app`) now
builds its BackendConfig via the private `_build_cloud_backends(config)`
helper and calls `create_app(config, backends=...)`. Existing callers of
`bootstrap_cloud_app()` need no change.

## Reading backends at request time

Inside a FastAPI route:
```python
from fastapi import Request

def my_route(request: Request):
    key_store = request.app.state.backends.key_store
    policy = request.app.state.backends.access_policy
```

Or via dependency injection:
```python
from cassetta.dependencies import get_backends

def my_route(backends: BackendConfig = Depends(get_backends)):
    ...
```

## New environment variables

None.

# Migration Guide: AzureBlobBackend bundle operations (Brief 522, v0.13.0)

## What Changed

`AzureBlobBackend` ships full bundle-operation support
(`open_bundle_write`, `read_bundle_meta`, `open_bundle_file_read`,
`list_bundles`, `delete_bundle`). The five methods previously raised
`NotImplementedError("Azure bundle operations land in follow-up
brief")` placeholders introduced by Brief 512.

Bundle blobs live under a dedicated `bundles/` prefix in the Azure
container, disjoint from the existing `data/` (flat-file) and `locks/`
(lease) prefixes:

```
<container>/
├── data/<key>                                    # flat-file blobs
├── locks/<key>                                   # lease blobs
└── bundles/<bundle_path>/
    ├── meta.json                                 # manifest blob
    └── <filename>                                # one blob per entry
```

Atomic visibility: the manifest blob is written last; readers see
either the complete bundle or nothing. `list_bundles(include_orphans=
True)` surfaces in-progress writes for cleanup tooling.

Concurrency-safe `delete_bundle` uses an ETag-conditional manifest
delete; on a 412 (concurrent re-upload completed mid-delete) it raises
`BundleDeleteRaceError` and leaves the new bundle intact. Phase-2 file
blob deletion is best-effort with WARNING-level logging on per-blob
failures.

## Operator migration

For operators running an Azure-axis deployment that disabled or worked
around the previous `NotImplementedError` stubs (e.g., capacity
shaping, custom retry handling, etc.):

1. Upgrade to v0.13.0 (or any later version that includes Brief 522).
2. Re-enable bundle traffic for the Azure-axis pods.
3. Re-run your integration suite — the 14 azurite-axis cloud
   integration parametrisations of scenarios 09, 10, 11, 13, 14, 15,
   16, 18, 19 are now green.

No on-disk migration is needed. The change is purely behavioural —
bundles already in flight on Azure transition cleanly: any blobs left
behind under `bundles/<path>/` from prior workarounds will surface as
orphans via `list_bundles(include_orphans=True)` and can be swept
via `delete_bundle(<path>)` (orphan path: skips phase-1, deletes any
remaining file blobs best-effort).

## New error type

`cassetta_cloud.backends.azure.storage.BundleDeleteRaceError(
bundle_path)` is raised when `delete_bundle` detects an ETag mismatch
on the manifest blob (concurrent re-upload completed mid-delete).
Layer-2 callers performing retention sweeps or batch deletes on the
Azure axis may catch it for retry semantics:

```python
from cassetta_cloud.backends.azure.storage import BundleDeleteRaceError

try:
    await backend.delete_bundle(bundle_path)
except BundleDeleteRaceError:
    # Concurrent re-upload won — the new bundle remains intact.
    # Retry policy is the caller's call.
    pass
```

The error is cloud-only (Layer 3) — `FilesystemBackend` cannot raise
it because `shutil.rmtree` has no analogous primitive. If a future
backend (S3 with object versioning, GCS) introduces a similar race,
the type may be promoted to Layer 1.

## Constructor signature change

`AzureBlobBackend.__init__` now accepts an optional
`sync_blob_service_client: SyncBlobServiceClient | None = None`
keyword:

```python
from azure.storage.blob import BlobServiceClient as SyncBlobServiceClient
from azure.storage.blob.aio import BlobServiceClient
from cassetta_cloud.backends.azure.storage import AzureBlobBackend

backend = AzureBlobBackend(
    blob_service_client=async_svc,
    container_name="cassetta-data",
    sync_blob_service_client=sync_svc,  # Brief 522: required for list_bundles
)
```

`list_bundles` is a synchronous Protocol method (`StorageBackend.
list_bundles`) — Cassetta runs it through a sync `BlobServiceClient`
to avoid async-loop gymnastics from inside the running async loop.
`bootstrap_cloud_app` constructs both clients automatically; only
direct constructions need the new kwarg.

A backend constructed without a sync client raises a clear
`RuntimeError` from `list_bundles`; flat-file ops and the four async
bundle methods continue to work without it.

## Protocol decoration

`StorageBackend` is now `@runtime_checkable`. `isinstance(backend,
StorageBackend)` returns `True` for any backend that structurally
satisfies the Protocol. The conformance test
(`cloud/tests/test_storage_backend_conformance.py`) parametrizes over
`[filesystem, azurite]` and asserts `isinstance` on each.

## New environment variables

None.

---

# Migration Guide: Persistent identity stores + asymmetric cache (Brief 523, v0.14.0)

## What Changed

Cloud-tier identity stores graduate from in-memory to blob-backed when
Azure credentials are configured:

- `MemoryUserStore` → `BlobUserStore`
- `MemoryTeamStore` → `BlobTeamStore`
- `MemoryInviteStore` → `BlobInviteStore`

`bootstrap_cloud_app()` dispatches automatically: blob stores when
Azure config is present (env-var-based detection), memory stores
otherwise. Filesystem deployments are unaffected — memory stores
remain the default there.

This closes the multi-pod K8s viability gap from Brief 506: under the
prior implementation, every pod held its own in-process stores and
any pod restart wiped all state (users, teams, memberships, invites).

## Asymmetric label-owner cache (security-relevant)

`app.state.label_owner_index` (in-process cache for label → user_id
resolution, introduced in Brief 521) is now **asymmetric by axis**:

- **Filesystem axis**: cache initialised and populated as before. It
  IS the source of truth on this axis (no blob lookup exists).
- **Azure axis**: cache is **NOT initialised**. Lookups dispatch
  directly to `BlobKeyStore.get_key_owner_by_label()`. Single source
  of truth.

Why: under multi-pod deployment with per-pod cache, a `revoke + re-
issue same label` sequence on pod A could leave pod B with stale
attribution (pod B's cache returns the prior owner's user_id even
after the new owner's key is the only valid one in the blob). The
prior owner could thus receive requests authenticated against the
new owner's key — a silent identity-attribution defect.

The asymmetric design closes this class by removing the cache from
the axis where it cannot be reliably invalidated. Performance impact:
one extra blob read per team-tier resolution; acceptable at Tier-2
scale, optionally optimised later via `If-None-Match`-conditional
GET (cache stays absent; HTTP-level 304 short-circuits unchanged
reads).

## New invite reaper background task

`bootstrap_cloud_app()` on the Azure axis now spawns a fire-and-
forget `_invite_reaper_loop` task in the FastAPI lifespan. The
reaper:

- runs every `passive_gc_interval` seconds (default 600,
  `CASSETTA_PASSIVE_GC_INTERVAL` env override — pre-existing knob,
  shared with the bundle/claim reapers);
- acquires a 15-second blob lease on `invites/.reaper.lock` to
  guarantee single-writer semantics across pods (other pods skip the
  cycle when the lease is held);
- enumerates `invites/*.json`, filters by per-record `expires_at`,
  deletes expired entries via ETag-conditional delete;
- treats `ResourceNotFoundError` on delete as idempotent success
  (logged at DEBUG; benign — concurrent consume / revoke / parallel
  reaper sweep all produce the same result).

Operator visibility: INFO logs `invite_reaper.sweep_start`,
`invite_reaper.sweep_complete: scanned=N, deleted=M, duration_ms=...`,
`invite_reaper.lease_acquire_failed` (expected when another pod
holds the lease).

## Operator migration

**No on-disk migration needed.** Tier-2 trial deployments don't have
existing in-blob identity data — the prior `Memory*Store` impls
weren't persisted anywhere. After upgrading to v0.14.0:

1. Existing users/teams/invites in-memory on a running pod are lost
   on restart (they always were under `MemoryUserStore` semantics).
2. New onboard / team-create / invite-create operations write to
   blob and survive pod restart + cross-pod read.
3. The `label_owner_index` dict no longer exists on the Azure-axis
   `app.state`. Any custom test or extension that introspected
   `app.state.label_owner_index` must use
   `BlobKeyStore.get_key_owner_by_label(label)` directly when
   running on the Azure axis. (Filesystem-axis access remains
   unchanged.)

## Concrete behavioural changes by store

### `BlobUserStore`

- Per-user blobs at `users/<user_id>.json` with ETag concurrency on
  updates (Pattern 1, [[storage-patterns-adr]]).
- `set_alias` mutates a shared `users/.alias-index.json` blob with
  ETag-retry; alias uniqueness enforced atomically.

### `BlobTeamStore`

- Per-team blobs at `teams/<team_id>.json`.
- Memberships at `memberships/<team_id>/<user_id>.json` (single-
  primary, suffix-filter for the reverse direction; Pattern 7
  Option B). At Tier-2 scale (≤2,500 membership blobs)
  `get_user_teams(user_id)` is one round-trip; Tier-3 graduation
  to aggregate-blob design is a future brief.

### `BlobInviteStore`

- Per-invite blobs at `invites/<invite_id>.json`.
- `consume_invite` is single-winner via ETag-conditional delete; a
  losing concurrent consumer sees `None` and surfaces the standard
  "invite already consumed" error to the caller.
- Reaper-driven cleanup per the section above; lazy-filter on
  read-paths is still in place as the correctness backstop.

## New error / log surface

- `invite_reaper.lease_acquire_failed` — INFO-level, expected when
  another pod holds the reaper lease. Not an error.
- `etag_conflict: store=<name>, retry=<N>` — INFO-level retry telemetry
  on contended single-blob writes (alias index, manifest, etc.).
- `etag_retries_exhausted: store=<name>, key=<...>` — WARNING when
  the retry budget is consumed; raise to caller.
- `corrupt_blob: store=<name>, key=<...>` — WARNING on JSON decode
  failure inside any of the three new stores.

No structured-log (`struct_log`) wiring yet; the cloud-side
unification across all stores is tracked under the observability
follow-up brief.

## New environment variables

None. The reaper cadence reuses the existing
`CASSETTA_PASSIVE_GC_INTERVAL` (default 600s).

## Constructor signatures

The three new stores accept the same kwargs as their `BlobKeyStore`
sibling:

```python
from azure.storage.blob.aio import BlobServiceClient
from cassetta_cloud.backends.azure import (
    BlobUserStore, BlobTeamStore, BlobInviteStore,
)

user_store = BlobUserStore(
    blob_service_client=async_svc,
    container_name="cassetta-data",
)
team_store = BlobTeamStore(blob_service_client=async_svc, container_name="cassetta-data")
invite_store = BlobInviteStore(blob_service_client=async_svc, container_name="cassetta-data")
```

`bootstrap_cloud_app` constructs all three automatically when Azure
config is present; only direct constructions need explicit wiring.




# Brief 525 — Broadcast / agents access policy + admin opt-in (v0.15.0)

Two changes ship together in the release that bundles Brief 525.

## BREAKING — REST `/broadcast` migrates from query-string to path-style

Pre-525:

```bash
curl -X POST "http://localhost:16001/broadcast?path=hello.md" --data-binary @hello.md
```

Post-525:

```bash
curl -X POST "http://localhost:16001/broadcast/hello.md" --data-binary @hello.md
```

The path segment supports embedded slashes via FastAPI's `:path` converter
(`/broadcast/team-alpha/morning-update.md`); the body / multipart contract is
unchanged. The legacy URL form returns 404 (or 307 redirect to a no-segment
404) — there is no compatibility shim. MCP clients are unaffected (the
`cassetta_broadcast` tool's `path` argument was already a positional
parameter).

The response shape is additive: existing fields (`delivered_to`, `failed`,
`total_delivered`, `total_failed`) are preserved; new fields `denied: list[{"target", "reason"}]`
and `total_denied: int` carry per-target policy denials.

## NEW — `CASSETTA_ADMIN_ROUTES=enabled` required to mount cloud admin endpoints

Cloud deployments that use `/admin/users`, `/admin/teams`, or `/admin/invites`
MUST set `CASSETTA_ADMIN_ROUTES=enabled` in the environment. Unset (or any
value other than `enabled`, case-insensitive) leaves the admin endpoints
absent — calls return 404, OpenAPI does not list them.

```bash
export CASSETTA_ADMIN_ROUTES=enabled
uv run uvicorn cassetta_cloud.app:app
# Boot log: "admin_routes.mounted policy_kind=cloud security_model=policy_gated routers=[...]"
```

**WARNING for multi-user deployments**: setting `CASSETTA_ADMIN_ROUTES=enabled`
while running with the default allow-all access policy (`DefaultAccessPolicy`,
`kind=core`) makes the admin surface unrestricted. The boot log calls this
out as `security_model=unrestricted_admin`. Use the cloud `TeamAccessPolicy`
(operator-only on admin namespaces) for any deployment that exposes admin
endpoints to non-trusted callers.

# Brief 527 — Onboarding quickstart refresh (v0.15.1)

## What Changed

The documented self-host quickstart now boots the server end-to-end
from a literal `cp .env.example .env && make run` with no
further file edits. The `Makefile:run` target switched to
`uvicorn --env-file .env --factory cassetta.app:create_app`.
`MIGRATION.md` is now exhaustive between v0.2.0 and v0.15.0 (eight
backfilled sections — Briefs 503, 504, 507, 508, 509, 516, 518, 519).
`CLIENT_SETUP.md` and `AGENT_SETUP.md` enumerate the full live MCP
tool surface (12 tools). Constitution Principle VIII (NON-NEGOTIABLE)
requires a `MIGRATION.md` section for any future release shipping
operator-visible changes in four mechanically-checkable classes (env
vars, REST routes, MCP tools, structured-log fields).

## Environment Variables

- **Added** — `CASSETTA_JWT_KEY` placeholder shipped in
  `.env.example`. Decodes to a fixed obviously-fake English string
  (`dev-only-quickstart-key-DO-NOT-USE-IN-PRODUCTION`). DEV ONLY —
  operators MUST regenerate before any deployment beyond `localhost`.
  The placeholder is unmissable in `.env.example` (3-line DEV ONLY
  warning above the value).
- **Removed** — retired `CASSETTA_MAX_FILE_SIZE` removed from
  `.env.example` (already retired in Brief 513; now also gone from
  the docs).

## Operator Action Required

Before any deployment beyond `localhost`, regenerate
`CASSETTA_JWT_KEY`:

```bash
openssl rand -base64 32 > /tmp/k && export CASSETTA_JWT_KEY=$(cat /tmp/k)
```

Or switch to `CASSETTA_JWT_KEY_FILE=<path>` for managed-secret setups.
The `README.md` Quickstart block carries the same warning
immediately after the "Connect an MCP agent" section.

---

# Brief 529 — Authentication observability (v0.16.0)

## What Changed

Authentication 401/403 paths now emit structured observability so
operators can detect brute-force attempts, compromised keys, and
misconfigured clients without packet capture or log parsing. Three
operator-visible signals were added; one existing event was renamed.

## Added

- **`auth.failure` structured-log event family** — emitted at every
  401/403 produced by an authentication path. Fields:
  - `source` ∈ `{rest, mcp, download}` — naming the surface.
  - `reason` ∈ `{missing_bearer, invalid_key, invalid_setup_token,
    jwt_invalid, jwt_expired, jwt_aud_mismatch}` — naming the failure
    mode (the last value is reserved-future; no current path emits).
  - `identity_hint` — first ≤12 chars of the supplied bearer or `null`.
  - Level: WARNING.
- **`cassetta.auth.failures{source, reason}` metric counter** —
  monotonic counter, incremented one-to-one with `auth.failure`
  events. Tags match the event's `source` and `reason` fields.
- **`dev_mode: bool` field in `GET /health` response body** — always
  present on both 200 and 503 responses. Symmetric shape — dashboards
  can panel the field directly without absence-detection.
- **`dev_mode_enabled` structured-log event** — emitted at lifespan
  startup at WARNING level when `app.state.dev_mode` is true (i.e.,
  `CASSETTA_SETUP_TOKEN` is empty). Greppable on its own — NOT folded
  into `config_loaded`.
- **`dev_mode: bool` field on `config_loaded` structured-log event** —
  always emitted at startup, value matches `app.state.dev_mode`.
- **`configure_logging` now attaches the same handler stack to BOTH
  `cassetta` and `cassetta_cloud` logger trees** — records emitted on
  any `cassetta.auth.*` or `cassetta_cloud.*` logger now render through
  the configured formatter on the same handler. Both top-level loggers
  have `propagate=False` to prevent root-double-emission.

## Renamed

- `download_jwt_validation_failed` structured-log event →
  `auth.failure` with `source=download`. The reason vocabulary changes
  from TokenError class names (e.g., `TokenExpired`,
  `TokenInvalidSignature`) to the new enum (`jwt_expired`,
  `jwt_invalid`). The HTTP response body is unchanged.

## Operator Action Required

- Update monitoring dashboards: panel `/health.dev_mode` for prod
  deployments and alert on `dev_mode=true`.
- Update log searches: replace `event=download_jwt_validation_failed`
  with `event=auth.failure source=download`.
- Add a dashboard panel for `cassetta.auth.failures{source, reason}` —
  this is the foundation signal for brief 531 rate-limiting.
- Add an alert for `event=dev_mode_enabled` at WARNING level on prod
  deployments (matches the no-token misconfiguration class).

## Environment Variables

None added or removed. All behaviour is driven by existing config
(`CASSETTA_LOG_FORMAT`, the configured `MetricsProvider`,
`app.state.dev_mode`).

# Brief 531 — Operational resilience: rate limits and JWT hot-reload (v0.17.0)

## What Changed

Three independent operational-resilience controls landed:

1. **Per-IP rate limits** on the cloud `/onboard` route, REST `/broadcast`,
   and the MCP `cassetta_broadcast` tool. Both broadcast surfaces share
   one `route=broadcast` budget, so a single attacker cannot evade the
   cap by mixing transports.
2. **Per-file 100 MiB ceiling** on uploads. `LimitsPolicy.per_file_max=None`
   no longer means "unlimited" — it now defaults to 100 MiB. The check
   runs against `tarinfo.size` BEFORE the entry's bytes are read, closing
   the OOM vector that an explicit oversized tar entry could exploit.
3. **JWT primary-key hot-reload** via SIGHUP. The just-demoted key remains
   valid for a configurable overlap window (default 600s) so in-flight
   download / upload tokens issued under the old key keep working. Lazy
   drop on first verify past the window. Multi-worker deployments
   gracefully announce themselves as not-supported (no SIGHUP handler
   installed; `jwt.hot_reload_disabled` event at startup).

## Added

- **Five new env vars** (see `.env.example` for full operator guide):
  - `CASSETTA_RATE_LIMIT_ONBOARD` — default `5/minute`. Format
    `<int>/<unit>` where unit ∈ `{sec, min, hour, second, minute, hourly}`.
  - `CASSETTA_RATE_LIMIT_BROADCAST` — default `10/minute`. Shared budget
    across REST `/broadcast` AND MCP `cassetta_broadcast`.
  - `CASSETTA_BROADCAST_MAX_TARGETS` — default `1000`. Fan-out cap;
    broadcasts above this size return 429 before any storage write.
  - `CASSETTA_TRUSTED_PROXIES` — default empty. Comma-separated CIDR list
    plumbed through to uvicorn `--forwarded-allow-ips`. Tier-by-tier
    operator guide in `.env.example`.
  - `CASSETTA_JWT_KEY_OVERLAP_TTL` — default `600` (seconds). Time the
    just-demoted JWT key remains valid after a hot rotation (SIGHUP).
    MUST be ≥ `max(CASSETTA_DOWNLOAD_CLAIM_TTL,
    CASSETTA_UPLOAD_TOKEN_TTL)`; boot-time WARNING
    `config_validation_warning` if violated.
- **One new structured-log event family**:
  - `jwt.key_rotated` (INFO) — emitted on a successful SIGHUP-driven
    rotation. Fields: `rotated_at` (ISO8601 UTC), `previous_kid`,
    `new_kid` (16-char SHA-256 hex prefix — never the key bytes),
    `overlap_until` (ISO8601 UTC).
  - `jwt.key_rotation_failed` (ERROR) — emitted when SIGHUP fires but the
    rotation cannot complete. Fields: `reason` ∈ `{missing_file,
    permission_denied, invalid_key_material, read_error}`, `path`. Slots
    are NOT mutated; the existing primary continues to verify.
  - `jwt.hot_reload_disabled` (INFO) — emitted once at startup when
    multi-worker deployment is detected (any of `WEB_CONCURRENCY`,
    `UVICORN_WORKERS`, `GUNICORN_WORKERS` ≠ 1) or when the platform's
    asyncio loop cannot register a signal handler (Windows / non-asyncio
    test contexts). `reason` ∈ `{multi_worker, signal_handler_unavailable}`.
  - `config_validation_warning` (WARNING) — emitted at startup when
    `jwt_key_overlap_ttl` is shorter than the largest issued-token TTL
    (download_claim_ttl, upload_token_ttl). Fields: `field`, `value`,
    `min_required`, `reason=overlap_shorter_than_token_ttl`.
- **One new metric counter**:
  - `cassetta.rate_limit.hits{route, reason}` — monotonic counter. Tags:
    `route` ∈ `{onboard, broadcast}`, `reason` ∈ `{rate, fanout_cap}`.
    Advances exactly once per HTTP-429 / structured-rate-limit
    `ValueError`.
- **`config_loaded` startup event extended** with `rate_limit_onboard`,
  `rate_limit_broadcast`, `broadcast_max_targets`, `trusted_proxies`,
  `jwt_key_overlap_ttl` fields. Existing fields unchanged.
- **`app.state.limiter`** — shared `slowapi.Limiter` instance, reachable
  by the cloud overlay's `/onboard` route via the standard `cloud → core`
  one-way import.
- **`app.state.jwt_keys`** — mutable `JWTKeySlots` holder populated at
  `create_app` time. The verify path now reads
  `app.state.jwt_keys.primary` and `_resolve_secondary(slots)` instead
  of the boot-time `AppConfig.jwt_*` fields, so rotations take effect on
  the next request without a restart.

## Behavior Change

- **`LimitsPolicy.per_file_max=None`** now defaults to **100 MiB**
  (`104857600` bytes), not unlimited. Operators who deliberately want a
  higher per-file ceiling must set the policy explicitly via
  `CASSETTA_PER_FILE_MAX=<bytes>` (or the equivalent code-level
  configuration). The 100 MiB constant is exposed as
  `cassetta.defaults.default_limits.DEFAULT_PER_FILE_MAX`.

## Operator Action Required

- **Add monitoring panels** for `cassetta.rate_limit.hits{route, reason}`.
  Distinguish `reason=rate` (someone hammering an authenticated route)
  from `reason=fanout_cap` (someone trying to spray a too-large
  broadcast). Both signal abuse but have different tuning levers.
- **Tune `CASSETTA_RATE_LIMIT_*`** to your traffic baseline. Defaults
  (5/minute on onboard, 10/minute on broadcast) are conservative. Run a
  one-week baseline before tightening.
- **Set `CASSETTA_TRUSTED_PROXIES`** to the CIDR range of your ingress.
  Without it, uvicorn ignores `X-Forwarded-For` and rate limits by the
  ingress IP — turning the per-IP budget into a global one. The empty
  default is correct for the directly-exposed Tier-0 deployment;
  everything else needs an explicit value.
- **Add a SIGHUP-based rotation procedure** to your secrets-rotation
  runbook. Step-by-step:
  1. Write the new base64-encoded HS256 key (≥32 bytes raw) to the path
     in `CASSETTA_JWT_KEY_FILE` (atomic replace recommended — write to
     `*.tmp` then `mv` so cassetta never reads a half-written file).
  2. `kill -HUP <cassetta-pid>` (use `pgrep cassetta` or systemd's
     `systemctl reload cassetta` if your unit declares `ExecReload`).
  3. Watch for the `jwt.key_rotated` log event with non-empty
     `previous_kid`, `new_kid`. Tokens issued under the old primary
     remain valid until `overlap_until`.
  4. Past `overlap_until`, any in-flight token signed by the old key
     starts returning HTTP 401 `auth.failure source=download
     reason=jwt_invalid` (per Brief 529's vocabulary). This is expected.
- **Decide your overlap window** before the first rotation. Default 600s
  is a balance between "operator wants to flip keys quickly after a
  suspected leak" and "in-flight download/upload tokens issued seconds
  before SIGHUP need to keep working". If your `download_claim_ttl` /
  `upload_token_ttl` are higher than the default 300s, raise
  `CASSETTA_JWT_KEY_OVERLAP_TTL` to cover them — otherwise you'll see
  `config_validation_warning` at startup and clients will see spurious
  401s mid-transfer.
- **Multi-worker deployments are explicitly NOT supported** for hot
  reload. If you run `uvicorn --workers N` (or behind gunicorn with
  `WEB_CONCURRENCY=N`), the `jwt.hot_reload_disabled` event fires once
  at startup with `reason=multi_worker`, and SIGHUP becomes a no-op. The
  rotation procedure for multi-worker deployments stays
  rolling-restart-driven — same as before this brief.

## Compatibility

- **No data migration**. All state is in-process; restart resets it.
- **No protocol changes**. The capabilities document and existing API
  surfaces are unchanged. New rejections (HTTP 429, manifest violation
  413) use existing envelope shapes.
- **Backwards compatibility for unauthenticated `cassetta_broadcast`
  callers**: clients that legitimately exceeded the now-default 10/minute
  budget will start receiving structured rate-limit `ValueError`
  messages. Tune `CASSETTA_RATE_LIMIT_BROADCAST` upward before the brief
  rolls out if your real-traffic baseline is higher.
- **Backwards compatibility for upload clients shipping >100 MiB single
  files**: they will start receiving HTTP 413 `cap_exceeded` with the
  `LimitsRejection` envelope. Set `CASSETTA_PER_FILE_MAX` explicitly to
  the bytes value you require before rolling out.

---

# Brief 533 — Observability completeness — metric parity and cloud struct_log (v0.18.0)

## What Changed

Three closely-coupled additive improvements:

1. **Hot-path domain metrics** — every uninstrumented operation in core
   now emits a `cassetta.<area>.<event>` counter, plus a one-time
   `cassetta.active_keys` gauge re-assertion on key rotation:

   | Operation | Counter (selected tags) |
   |---|---|
   | `GET /inbox/{a}/`, `GET /inbox/{a}/{p}`, `POST /inbox/{a}/{p}/pick`, `DELETE /inbox/{a}/{p}` | `cassetta.inbox.operations{action}` |
   | `POST /upload/{p}` | `cassetta.upload.operations{result}`, `cassetta.upload.bytes`, `cassetta.upload.manifest_violations{reason}` |
   | `GET /download/{token}` | `cassetta.download.operations{result}`, `cassetta.download.bytes` |
   | MCP `_enforce` denial, any `_enforce` denial | `cassetta.policy.decisions{result=denied, policy_kind}` |
   | `GET /capabilities`, MCP `cassetta_capabilities` | `cassetta.capabilities.queries{via}` |
   | Key rotation gauge re-assertion | `cassetta.active_keys` (gauge) |

2. **Cloud uniform JSON log stream** — onboard, invite, team-policy,
   identity-resolve, and Azure backend error sites converted from plain
   `logger.*` calls to `struct_log` so every line parses as JSON under
   `CASSETTA_LOG_FORMAT=json`. The duplicate
   `claim_storage_backend=azure_blob` emission at the cloud bootstrap
   was deleted; the canonical line now lives in
   `src/cassetta/app.py` and is itself a struct_log record.

3. **Auth logger isolation** — `configure_logging(...)` now attaches an
   explicit handler to `cassetta.auth` and sets `propagate=False` on
   that sub-tree (matching what brief 529 set up for the parent
   `cassetta` logger). Idempotent on re-invocation.

A new helper, `safe_emit(...)` in `cassetta.structured_log`, funnels
paired struct_log + counter emission through TWO INDEPENDENT
best-effort try/except handlers. Every counter call in
`src/cassetta/` now routes through `safe_emit` (one explicit
exception: `auth/observability.py:emit_auth_failure` keeps its
brief-529 fallback string verbatim for regression-lock reasons).

## Additive `policy_kind` field/tag (FR-007a / FR-022)

`policy.denied` log records and `cassetta.policy.decisions` counter
increments gain an additive `policy_kind` dimension. **Existing
log-aggregator queries and metric aggregations that ignore the new
dimension continue to work unchanged.**

| Source policy | `policy.denied` event field `policy_kind` | `cassetta.policy.decisions` counter tag `policy_kind` |
|---|---|---|
| `CoreAccessPolicy` (`kind=core`) | `core` | `core` |
| `TeamAccessPolicy` (`kind=cloud`) | `cloud` | `team` |

The asymmetry is intentional: the log-event field identifies the
deployment family (`cloud`); the counter tag identifies the
decision-engine kind (`team`-based vs. flat `core`).

**Operator action**: none required. Drill-down filtering is now
available via `event=policy.denied AND policy_kind=cloud`.

## Upload counter `{result}`-only shape (FR-002)

`cassetta.upload.operations` is tagged ONLY by `result` (one of `ok`,
`rejected`, `error`). A future brief may add a `mode` tag
(inline/streaming/etc.) when those modes are first-class — until then,
aggregations on `result` are stable.

## Download counter closed `{ok, not_found, identity_mismatch}` enum (FR-005)

`cassetta.download.operations` is tagged by `result`. The value
vocabulary is closed in this brief; `cassetta.auth.failures` (from
brief 529) remains the canonical counter for download-side JWT failures
— the two are disjoint by design.

**Stolen-credential detection**: an unexpected spike in
`cassetta.download.operations{result=identity_mismatch}` is the
appropriate alert for token-replay attempts where the JWT validates
but the bearer's identity does not match the recipient encoded in the
claim. Alert on a per-recipient threshold; alongside the brief-529
`cassetta.auth.failures{source=download}` counter this gives full
coverage of download-side authentication anomalies.

## Operator action

No on-disk migration. No new environment variables. No new
third-party dependencies. The Brief 533 changes are additive at the
observability layer; existing dashboards and alerts continue to work.

If you operate `CASSETTA_LOG_FORMAT=json`, you can now write
operator-aggregator queries like:

```text
event=policy.denied AND policy_kind=cloud
event=onboard.failure
event=invite.created
event=identity.resolve AND result=user_not_found
event=azure.lease.acquire_failed
event=claim_storage_backend AND detail.kind=azure
```

And query the new domain counters by their counter name + tags
(e.g., `cassetta.inbox.operations{action=read}`,
`cassetta.upload.operations{result=ok}`,
`cassetta.download.operations{result=identity_mismatch}`).

# Brief 535 — Techdebt cleanup wave (v0.19.0)

> Version filled at release time. Wave bundles six independent hygiene
> fixes; reverting any one fix does not affect the others.

## What Changed

Six narrowly-scoped fixes:

1. **Removed environment variables** (Fix 1) — `CASSETTA_IDENTITY_PROVIDER`,
   `CASSETTA_ACCESS_POLICY`, `CASSETTA_ALIAS_RESOLVER` are dropped from
   `AppConfig` and `load_config`. They had no production reader since
   landing in Briefs 503 / 504 / 508; setting them was already a no-op.

2. **OpenAPI version surface correction** (Fix 2) — `/openapi.json`
   reports the real package version (`cassetta.__version__`) instead of
   the hardcoded `"0.1.0"` baked into the FastAPI constructor.

3. **Policy-derived capabilities features** (Fix 3) — the `features`
   list in `/capabilities` is now produced by
   `LimitsPolicy.advertise_features(ctx) -> list[str]` (new Protocol
   method). `CoreLimitsPolicy` returns the full canonical tuple
   unchanged; cloud introduces a `CloudLimitsPolicy` wrapper that
   subtracts features denied by the active `AccessPolicy` (today: `peek`
   for non-owner identities under Team-tier).

4. **Cloud bootstrap fails fast on missing Azure SDK** (Fix 4) — when
   Azure credentials are present but the `azure-storage-blob` SDK fails
   to import, `_build_cloud_backends` now exits with code 1 after
   emitting an ERROR-level structured log event
   `cloud.bootstrap.azure_sdk_missing` (fields: `missing_module`,
   `creds_source`, `hint`). The previous warn-and-fallback path silently
   landed every write in container-ephemeral filesystem.

5. **`KeyStoreProtocol.create_key` unified signature** (Fix 5) — accepts
   `user_id: str | None = None` (keyword-only) on every backend. The
   route handler stops catching `TypeError` as a backend-signature
   fallback; a new AST regression test forbids reintroduction.

6. **Dead pydantic models pruned + live shapes wired** (Fix 6) — nine
   unused models removed from `src/cassetta/models.py`. Four real
   wire shapes wired with `response_model=` so the OpenAPI document
   reflects them: `InboxListResponse`, `PeekResponse`,
   `LimitsRejectionBody`, `BundlePathConflict`.

## Operator-Visible Changes

### Removed Environment Variables (Fix 1)

| Variable | Status | Action |
|---|---|---|
| `CASSETTA_IDENTITY_PROVIDER` | Removed | Silently ignored. Remove from deployment manifests at next convenience. |
| `CASSETTA_ACCESS_POLICY` | Removed | Same as above. Cloud team enforcement is wired automatically by `bootstrap_cloud_app`. |
| `CASSETTA_ALIAS_RESOLVER` | Removed | Same as above. |

No deprecation warning is emitted — the variables were no-ops before
removal; logging a warning would be louder than the silent removal.

### Cloud Bootstrap Behavior Change (Fix 4)

| Condition | Pre-535 | Post-535 |
|---|---|---|
| Azure creds present, SDK importable | Cloud bundle built | Unchanged |
| No Azure creds | Filesystem fallback | Unchanged |
| Azure creds present, SDK import fails | Filesystem fallback (silent data loss on pod restart) | `SystemExit(1)` + ERROR-level `cloud.bootstrap.azure_sdk_missing` event |

**Operator action**: Verify the container image includes
`azure-storage-blob` before deploying cloud pods. Any tooling that
caught `SystemExit` (none should — `bootstrap_cloud_app` is normally
called only once at process start) must accept the new failure mode.

### New Structured-Log Event (Fix 4)

- **Event**: `cloud.bootstrap.azure_sdk_missing`
- **Severity**: ERROR
- **Frequency**: at most once per pod start (process exits immediately after)
- **Fields**:
  - `missing_module: str` — failing import's module name (from
    `ImportError.name`), defaults to `"azure-storage-blob"`.
  - `creds_source: str` — `"connection_string"` | `"account_key"` |
    `"none"`.
  - `hint: str` — actionable remediation string.

### OpenAPI Version Surface (Fix 2)

| Surface | Pre-535 | Post-535 |
|---|---|---|
| `/openapi.json` → `info.version` | `"0.1.0"` | `cassetta.__version__` |
| `/capabilities` → `server_version` | `cassetta.__version__` | Unchanged |

Any tooling that pinned against `info.version == "0.1.0"` must update.

### Wire Format

Unchanged on every route. Fix 6's `response_model=` declarations are
OpenAPI-metadata-only — payload shapes (`InboxListResponse`,
`PeekResponse`, `LimitsRejectionBody`, `BundlePathConflict`) are
byte-identical to the previous dict literals. The
`InboxFileInfo.schema_version` field added to the pydantic model
matches what `_listing_entry` already emitted at the dict layer.

## Migration Steps

None for typical operators. If your deployment scripts export any of
the three removed `CASSETTA_*` variables, drop them at convenience —
they have no effect either way. If your cloud image ever shipped
without `azure-storage-blob`, rebuild before upgrading.

# Brief 541 — MCP send surface: typed schemas, honest encoding error, serverInfo version (v0.21.0)

> Version filled at release time (`make release`).

## What Changed

Three client-visible cleanups to the MCP send surface. **No behaviour change for
callers that already send well-formed payloads** — only the advertised schema, the
tool descriptions, and one error-reason string change.

1. **Typed send schemas.** `cassetta_send_init` and `cassetta_send_inline` now
   advertise a typed `inputSchema` instead of an open object. `send_init.manifest`
   is `{files: [{name, size, mime?}], file_count?}` (a `SendManifest` model) where
   `size` is the **decoded** byte count; `send_inline.files` is
   `[{name, content, encoding}]` (a `SendInlineFile` model) where `encoding` is a
   required enum of `"base64"` / `"utf8"`. Field names, `required`, and the enum are
   now discoverable from `tools/list`. Unknown extra fields are still ignored
   (Pydantic default), so existing clients sending the same shapes are unaffected.

2. **Honest inline encoding error.** On the MCP inline path, a missing/invalid
   `encoding` — and content that cannot be decoded under it — is reported as
   `manifest_violation: reason=missing_or_bad_encoding` (previously the misleading
   `reason=wrong_size`). A genuine size mismatch (encoding valid, declared `size` ≠
   decoded length) still returns `reason=wrong_size`. The REST batch/tar upload path
   (`POST /upload/...`) carries no per-file encoding and is unchanged — its
   `wrong_size` remains a true size check.

3. **serverInfo version.** The MCP `initialize` handshake now advertises
   `serverInfo.version == cassetta.__version__` instead of the underlying `mcp` SDK
   version.

## Operator-Visible Changes

No `CASSETTA_*` env var, REST route, MCP tool name, or structured-log field changes.

## Migration Steps

Clients that **pattern-match the literal `wrong_size` reason** to detect a
missing-encoding inline send must update: that case is now either rejected at
schema-validation time (naming the `encoding` field) or returned as
`missing_or_bad_encoding`. Clients that send correctly-encoded inline payloads, or
that read `serverInfo.version`, need no changes.

# Brief 542 — REST surface hygiene (v0.22.0)

> Version filled at release time (`make release`).

## What Changed

Description-only hygiene on the public REST surface — **no runtime behaviour change**: no endpoint
added, removed, or renamed, and no request/response shape change. Two effects:

1. **Leak-free OpenAPI descriptions.** Internal change-ticket numbers and internal class names no longer
   appear in the generated OpenAPI. `POST /broadcast/{path}`, `GET /agents`, and the removed
   `PUT /inbox/{agent}/{path}` now carry caller-facing descriptions. A standing guard
   (`tests/test_openapi_leakage.py`) keeps every path and component-schema description free of
   internal markers, covering routes added later automatically.

2. **New REST reference.** `docs/REST_API.md` documents the REST surface as of this release
   (credential model, endpoint table, curl workflows, what REST can/can't do, live `/docs` pointer). The
   root README now links it alongside the `cassetta` CLI and the live interactive docs.

## Operator-Visible Changes

No `CASSETTA_*` env var, REST route, MCP tool name, or structured-log field changes. The **generated
OpenAPI output text changes** (operation descriptions only) — clients that snapshot or diff the spec will
see reworded descriptions, but no schema or shape change.

## Migration Steps

None — description and documentation only. Clients that pin exact OpenAPI description strings (rare)
should refresh their snapshot.

# Brief 543 — REST upload-init (v0.22.0)

> Version filled at release time (`make release`).

## What Changed

Directed-send **phase 1 is now available over plain HTTP** — previously it could only be minted via the
MCP `send_init` tool, so a non-MCP client could not start a directed send. **Additive and backward
compatible**: no existing route, request/response shape, or MCP tool changes.

1. **New `POST /uploads` route.** Authenticated with the caller's `cst_` agent key (the same identity
   dependency every other REST route uses), it takes a typed body (`{to, path, manifest}`) and returns an
   upload session (`{mode, bundle_id, upload_url, batch_token, expires_at}`). The client then streams the
   tar to the unchanged `POST /upload/{bundle_path}` carrying `batch_token`. REST send-init is **batch-only**
   for any size; inline completion stays an MCP convenience.

2. **Shared phase-1 helper.** The manifest validation → recipient/alias resolution → access-policy check →
   capacity-policy evaluation → upload-token mint now lives in one transport-agnostic helper called by both
   the MCP tool and the REST route, so the two surfaces behave identically. A REST caller gains no
   capability an MCP caller lacks; rejections reuse the existing taxonomy (413 capacity / 422 malformed
   manifest or invalid path / 404 unknown recipient / 403 recipient-visibility denial).

3. **Discoverability.** Typed request/response models make the generated OpenAPI self-documenting (the
   `POST /uploads` body references the `SendManifest` schema, not an open object). `GET /capabilities` and
   `cassetta_capabilities` now advertise the `rest_send_init` feature.

## Operator-Visible Changes

One new REST route (`POST /uploads`) and one new `features` entry (`rest_send_init`) in the capabilities
document. No `CASSETTA_*` env var, MCP tool name, or structured-log field changes — the shared helper emits
the same `upload_init` / `policy.denied` events the MCP path always did.

## Migration Steps

None required — the route is additive. Non-MCP clients that previously had to drive phase 1 over MCP can
switch to `POST /uploads`. Clients that pin the exact `/capabilities` `features` list (rare) should refresh
their snapshot to include `rest_send_init`.
