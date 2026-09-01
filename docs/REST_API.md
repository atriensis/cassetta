# Cassetta REST API reference

The REST API as of core **v0.21.0**.

MCP is Cassetta's primary surface; REST is the plain-HTTP fallback — every operation is usable with a
plain `curl` and a Bearer token (Constitution §IV). The interactive, always-current contract is served
live (see [Interactive docs](#interactive-docs)); this page is the narrative companion.

- **Base URL**: `http://<host>:16001` (default port). Examples below use `BASE="http://localhost:16001"`.
- **Auth header**: `Authorization: Bearer <token>` (examples use `KEY="Bearer cst_…"`).

## Credential model

Three token types front the surface. Present the right one for the call you are making.

| Token | Form | Where it comes from | TTL | What it authorises |
|-------|------|---------------------|-----|--------------------|
| **Agent key** | Bearer, prefix `cst_` | `POST /setup` (first key, via the setup-token) or `POST /keys` (subsequent keys, via an existing agent key) | Long-lived (until rotated or deleted) | Store CRUD (`/files/*`), inbox read/peek/pick (`/inbox/*`), broadcast, key management (`/keys*`), capabilities, agent listing |
| **Upload-token** | Short-lived JWT | Minted by directed send-init phase 1 — MCP `send_init` **or** REST `POST /uploads` (`CASSETTA_UPLOAD_TOKEN_TTL`) | 300 s | A single directed upload — consumed by `POST /upload/{bundle_path}`. A plain `cst_` key on `/upload` is rejected (401); mint this token with `POST /uploads` (presenting your agent key). |
| **Claim / download-token** | Reference envelope returned by a consuming `pick` | `POST /inbox/{agent}/{path}/pick` in reference mode (`CASSETTA_DOWNLOAD_CLAIM_TTL`) | 300 s | A single referenced-file fetch — consumed by `GET /download/{bundle_path}/{name}` together with a matching identity header (two-factor) |

The agent key is the credential for almost everything. The other two are short-lived, single-purpose
tokens that gate the directed-send and reference-download flows.

## Endpoint table

`auth` is the credential required (see above). The table mirrors the live OpenAPI surface.

| Auth | Method | Path | Purpose |
|------|--------|------|---------|
| none | `GET` | `/health` | Liveness probe |
| setup-token | `POST` | `/setup` | Bootstrap the first agent key |
| agent key | `POST` | `/keys` | Create an agent key |
| agent key | `GET` | `/keys` | List agent keys |
| agent key | `POST` | `/keys/{label}/rotate` | Rotate an agent key |
| agent key | `DELETE` | `/keys/{label}` | Delete an agent key |
| agent key | `GET` | `/capabilities` | Discover advertised limits, TTLs, and modes |
| agent key | `GET` | `/agents` | List agents visible to the caller |
| agent key | `POST` | `/broadcast/{path}` | Send a file/bundle to every visible recipient |
| agent key | `PUT` | `/files/{path}` | Store a file (201; overwrite rejected — `DELETE` first) |
| agent key | `GET` | `/files/` | List files (optional `?prefix=`) |
| agent key | `GET` | `/files/{path}/peek` | File metadata (non-consuming) |
| agent key | `GET` | `/files/{path}` | Fetch a file (JSON envelope, see below) |
| agent key | `DELETE` | `/files/{path}` | Delete a file |
| agent key | `GET` | `/inbox/{agent}/` | List inbox bundles |
| agent key | `GET` | `/inbox/{agent}/{path}/peek` | Peek a bundle (non-consuming) |
| agent key | `GET` | `/inbox/{agent}/{path}` | Read a bundle |
| agent key | `POST` | `/inbox/{agent}/{path}/pick` | Consume a bundle → reference envelope |
| agent key | `DELETE` | `/inbox/{agent}/{path}` | Delete a bundle (204) |
| agent key | `POST` | `/uploads` | Start a directed upload session (phase 1) → upload-token |
| upload-token | `POST` | `/upload/{bundle_path}` | Directed upload (phase 2) |
| claim/download-token | `GET` | `/download/{bundle_path}/{name}` | Fetch a referenced file |

> `PUT /inbox/{agent}/{path}` is a removed legacy endpoint and always returns `410 Gone` — use the
> directed upload flow (`POST /upload/{bundle_path}`) instead.

## Workflow A — store-model peer exchange

The store model is a shared key/value space of files. One agent writes; any agent with a key reads.
Field-verified against v0.21.0.

```bash
BASE="http://localhost:16001"
KEY="Bearer cst_…"          # an agent key
F=./report.md

# Send (raw bytes — no manual base64 or manifest; overwrite is rejected, DELETE first)
curl -fsS -X PUT -H "Authorization: $KEY" --data-binary @"$F" "$BASE/files/$(basename "$F")"
# → 201 {"path":"report.md","size":NNN}

# Fetch (content arrives as a JSON envelope {files:[{content,encoding}]} — decode it)
curl -fsS -H "Authorization: $KEY" "$BASE/files/report.md" \
| python3 -c 'import sys,json,base64;d=json.load(sys.stdin)["files"][0];c=d["content"];sys.stdout.buffer.write(base64.b64decode(c) if d["encoding"]=="base64" else c.encode())'

# Auxiliary
curl -fsS -H "Authorization: $KEY" "$BASE/files/report.md/peek"   # metadata, non-consuming
curl -fsS -H "Authorization: $KEY" "$BASE/files/?prefix=report"   # list
curl -fsS -X DELETE -H "Authorization: $KEY" "$BASE/files/report.md"
```

## Workflow B — directed send + reference read

A directed send delivers a bundle to a specific recipient's inbox. It is a **two-phase** flow:
phase 1 (`send_init`) validates the manifest and mints a short-lived **upload-token**; phase 2
(`POST /upload/{bundle_path}`) consumes that token and uploads the bytes.

Phase 1 is available over **both MCP (`send_init`) and REST (`POST /uploads`)**. A plain agent key on
`/upload` still returns `401` — the upload-token is the required credential — but over REST you mint
that token with `POST /uploads` (presenting your agent key), then consume it in phase 2:

```bash
BASE="http://localhost:16001"
ME="alice"                          # your agent label
KEY="Bearer cst_…"                  # an agent key
TO="bob:main"                       # recipient label

# Phase 1 (REST): POST /uploads with your agent key → an upload-token + the upload_url.
curl -fsS -X POST -H "Authorization: $KEY" -H "Content-Type: application/json" \
     -d "{\"to\":\"$TO\",\"path\":\"bundle.tar\",\"manifest\":{\"file_count\":1,\"files\":[{\"name\":\"bundle.tar\",\"size\":NNN}]}}" \
     "$BASE/uploads"
# → 201 {"mode":"batch","upload_url":"…/upload/<bundle_path>","batch_token":"…","bundle_id":"…","expires_at":"…"}

# Phase 2 (REST): consume the batch_token (an upload-token) to upload the bytes to upload_url.
curl -fsS -X POST -H "Authorization: Bearer <batch_token>" \
     --data-binary @bundle.tar "$BASE/upload/<bundle_path>"

# --- The recipient reads in reference mode ---
# List + peek (read-only over REST)
curl -fsS -H "Authorization: $KEY" "$BASE/inbox/$ME/"                     # list
curl -fsS -H "Authorization: $KEY" "$BASE/inbox/$ME/<path>/peek"          # peek, non-consuming

# Pick consumes the bundle and returns a reference envelope (claim/download-token, TTL 300 s)
curl -fsS -X POST -H "Authorization: $KEY" "$BASE/inbox/$ME/<path>/pick"  # → reference envelope

# Fetch the referenced file with the download-token + matching identity header (two-factor)
curl -fsS -H "Authorization: Bearer <download-token>" \
     "$BASE/download/<bundle_path>/<name>"
```

## What REST can / can't do (v0.21.0)

- ✅ **Store model** — full CRUD over `/files/*` with a plain agent key.
- ✅ **Inbox reads** — list / peek / pick over `/inbox/*` with a plain agent key.
- ✅ **Broadcast** — `POST /broadcast/{path}` to every visible recipient.
- ✅ **Directed upload phase 2** — `POST /upload/{bundle_path}` with an upload-token.
- ✅ **Reference download** — `GET /download/...` with a claim/download-token.
- ✅ **Key & capability management** — `/keys*`, `/capabilities`, `/agents`.
- ✅ **Directed send phase 1 over REST** — `POST /uploads` (with an agent key) mints the upload-token,
  running the same manifest validation, recipient/alias resolution, and access-policy checks as MCP
  `send_init`.
- ❌ **Inline single-file send over REST** — use the batch path (`POST /uploads` → `POST /upload/{bundle_path}`);
  inline completion stays an MCP convenience.

## Interactive docs

The generated, always-current OpenAPI contract is served live (unauthenticated):

- **Swagger UI** — `GET /docs`
- **ReDoc** — `GET /redoc`
- **Raw spec** — `GET /openapi.json`

## CLI

The `cassetta` command-line client ships with the package (`pip install cassetta` → `cassetta --help`):

- `cassetta upload` — stream a tar bundle to `POST /upload/{bundle_path}`
- `cassetta download` — fetch every file in a reference envelope via `GET /download/...`
- `cassetta capabilities` — print the server's advertised limits and features
