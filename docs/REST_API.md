# Cassetta REST API reference

MCP is Cassetta's primary surface; REST is the plain-HTTP fallback — every operation is usable with a
plain `curl` and a Bearer token (Constitution §IV). The generated OpenAPI document your server serves
is the always-current contract and reports its own version (see [Interactive docs](#interactive-docs));
this page is the narrative companion and carries no version number of its own, so that it cannot
disagree with the server you are pointing at.

- **Base URL**: `http://<host>:16001` (default port). Examples below use `BASE="http://localhost:16001"`.
- **Auth header**: `Authorization: Bearer <token>` (examples use `KEY="Bearer cst_…"`).

## Credential model

Three token types front the surface. Present the right one for the call you are making.

| Token | Form | Where it comes from | TTL | What it authorises |
|-------|------|---------------------|-----|--------------------|
| **Agent key** | Bearer, prefix `cst_` | `POST /setup` (first key, via the setup-token) or `POST /keys` (subsequent keys, via the setup-token **or** an existing agent key) | Long-lived (until rotated or deleted) | Store CRUD (`/files/*`), inbox read/peek/pick (`/inbox/*`), broadcast, key management (`/keys*`), capabilities, agent listing |
| **Upload-token** | Short-lived JWT | Minted by directed send-init phase 1 — MCP `send_init` **or** REST `POST /uploads` (`CASSETTA_UPLOAD_TOKEN_TTL`) | 300 s | A single directed upload — consumed by `POST /upload/{bundle_path}`. A plain `cst_` key on `/upload` is rejected (401); mint this token with `POST /uploads` (presenting your agent key). |
| **Claim / download-token** | Reference envelope returned by a consuming `pick` | `POST /inbox/{agent}/{path}/pick` in reference mode (`CASSETTA_DOWNLOAD_CLAIM_TTL`) | 300 s | A single referenced-file fetch — consumed by `GET /download/{bundle_path}/{name}` together with a matching identity header (two-factor) |

The agent key is the credential for almost everything. The other two are short-lived, single-purpose
tokens that gate the directed-send and reference-download flows.

### Minting a key

Both `POST /setup` and `POST /keys` take the same body — `host` and `project`, never a pre-joined
`label`:

```bash
curl -fsS -X POST -H "Content-Type: application/json" \
     -H "X-Setup-Token: $SETUP_TOKEN" \
     "$BASE/keys" -d '{"host":"work-laptop","project":"notes"}'
# → 201 {"label":"work-laptop:notes","api_key":"cst_…","created_at":"…"}
```

`label` comes back in the response, derived as `host:project`; sending it as an input field is
rejected `422` naming `host` and `project` as the missing fields. `POST /keys` accepts the setup
token shown above **or** an `Authorization: Bearer` agent key — an agent that already holds a key
does not need the setup token again. `POST /setup` is the bootstrap case and takes only the setup
token, because on a server with no keys yet there is no agent key to present; it answers `409` once a
key exists.

## Endpoint table

`auth` is the credential required (see above). The table mirrors the live OpenAPI surface.

| Auth | Method | Path | Purpose |
|------|--------|------|---------|
| none | `GET` | `/health` | Liveness probe |
| setup-token | `POST` | `/setup` | Bootstrap the first agent key (body `{"host","project"}`) |
| setup-token or agent key | `POST` | `/keys` | Create an agent key (body `{"host","project"}`) |
| setup-token or agent key | `GET` | `/keys` | List agent keys |
| setup-token or agent key | `POST` | `/keys/{label}/rotate` | Rotate an agent key |
| setup-token or agent key | `DELETE` | `/keys/{label}` | Delete an agent key |
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

Two things the table does not show.

**The `{label}` segment carries a colon.** Every label is `host:project`, and both routes that take
one accept it unencoded: `DELETE /keys/work-laptop:notes` and `POST /keys/work-laptop:notes/rotate`
answer `200`.

**`{agent}` is not checked against the caller.** Under the access policy this repository ships, any
valid agent key may list, peek, read and pick any label's inbox — the same single-user posture the
store has (see [Workflow A](#workflow-a--store-model-peer-exchange)). Inbox addressing separates
recipients; it does not isolate them. A deployment shared between people who should not read each
other's mail needs more than one Cassetta.

## Two listing responses

The generated OpenAPI document is the contract for every shape on this page; these two are reproduced
here because they are the ones a reader reaches for while writing a client, and because each has a
field whose meaning is not obvious from its name.

### `GET /keys`

```json
{"keys": [{"label": "work-laptop:notes",
           "key_prefix": "cst_ab12",
           "created_at": "2026-09-08T09:14:21Z",
           "is_active": true}]}
```

**No hash and no key material comes back** — not a truncated key, not a digest of one. `key_prefix`
is the leading characters, enough to tell two keys apart in a log line and not enough to present as
a credential. A key's secret is returned exactly once, by the call that mints it.

### `GET /inbox/{agent}/`

```json
{"agent": "bob:main",
 "files": [{"path": "handoff-A",
            "size": 150000,
            "created_at": "2026-09-08T09:20:05Z",
            "sender": "alice:main",
            "remaining_ttl": null,
            "file_count": 1,
            "bundle_id": "0191a3d0-1b3c-7e29-8012-a2b3c4d5e6f7",
            "schema_version": 1,
            "files": [{"name": "notes.md", "size": 150000, "mime": "text/markdown"}]}]}
```

- `path` is the bundle's name — the `path` the sender passed to `POST /uploads` — and it is what you
  put in the `peek`, `pick` and `DELETE` paths.
- `size` is the **content's** total, the sum of `files[].size`. It is not the size of the archive
  that carried it, which is larger and which the recipient never sees.
- `remaining_ttl` is seconds, or `null` when the bundle does not expire.
- **A reserved bundle that was never uploaded leaves no entry.** The listing shows delivered
  bundles, not sessions: a `POST /uploads` whose phase 2 never happened is invisible here, so an
  empty listing does not mean a send was refused.

## Workflow A — store-model peer exchange

The store model is a shared key/value space of files. One agent writes; any agent with a key reads.

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
that token with `POST /uploads` (presenting your agent key), then consume it in phase 2.

**Address the recipient by the full `host:project` label.** A `to` containing a colon is looked up in
the key store, so a label nobody holds is refused `404 unknown_recipient` and you find out
immediately. A `to` without one is taken as a raw inbox name and is not checked against anything: it
is accepted `201` even when no such recipient has ever existed, and the bundle lands in a namespace
the intended reader is not listening on. Nothing reports an error on either side.

**`path` is the bundle's name in the recipient's inbox, not a local filename.** It is what the
recipient sees as `path` in their inbox listing and what they put in the `peek` and `pick` paths, so
it is worth choosing as a name rather than copying from whatever the archive on your disk is called.
The server percent-encodes it into the `upload_url` it returns — `path: "handoff-A"` sent to
`bob:main` comes back as `…/upload/inbox%2Fbob%3Amain%2Fhandoff-A`.

**The manifest lists what is *inside* the archive.** One entry per file the tar contains, weighed
before you send. The archive itself is never one of its own entries — and getting that wrong is
expensive, because phase 1 has no bytes to check the manifest against and answers `201` to anything
well-formed. Phase 2 is where every tar entry is checked against it, so declaring the archive gets
you `400 {"error":"manifest_violation","reason":"extra_file"}` naming the file you actually packed,
one call after the call that accepted the mistake.

```bash
BASE="http://localhost:16001"
ME="alice:main"                     # your own agent label
KEY="Bearer cst_…"                  # an agent key
TO="bob:main"                       # recipient label — the full host:project, always

# Phase 1 (REST): POST /uploads with your agent key → an upload-token + the upload_url.
# path names the bundle in the recipient's inbox; the manifest names the file the archive holds.
curl -fsS -X POST -H "Authorization: $KEY" -H "Content-Type: application/json" \
     -d "{\"to\":\"$TO\",\"path\":\"handoff-A\",\"manifest\":{\"file_count\":1,\"files\":[{\"name\":\"notes.md\",\"size\":NNN}]}}" \
     "$BASE/uploads"
# → 201 {"mode":"batch","upload_url":"…/upload/<bundle_path>","batch_token":"…","bundle_id":"…","expires_at":"…"}

# Phase 2 (REST): consume the batch_token (an upload-token) to upload the bytes to upload_url.
# The local archive holds notes.md — the entry the manifest above declares.
curl -fsS -X POST -H "Authorization: Bearer <batch_token>" \
     -H "Content-Type: application/x-tar" \
     --data-binary @bundle.tar "$BASE/upload/<bundle_path>"

# --- The recipient reads in reference mode ---
# List + peek (read-only over REST). <path> is the path sent in phase 1 — here, handoff-A.
curl -fsS -H "Authorization: $KEY" "$BASE/inbox/$ME/"                     # list
curl -fsS -H "Authorization: $KEY" "$BASE/inbox/$ME/<path>/peek"          # peek, non-consuming

# Pick consumes the bundle and returns a reference envelope (claim/download-token, TTL 300 s)
curl -fsS -X POST -H "Authorization: $KEY" "$BASE/inbox/$ME/<path>/pick"  # → reference envelope

# Fetch the referenced file with the download-token + matching identity header (two-factor).
# Without X-Sender the request is 401; it must match the token's recipient claim.
curl -fsS -H "Authorization: Bearer <download-token>" -H "X-Sender: $ME" \
     "$BASE/download/<bundle_path>/<name>"
```

## What REST can / can't do

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

The `cassetta` command-line client ships in this repository, declared at `pyproject.toml`
`[project.scripts]`. It is what an agent shells out to when a payload is too large to pass through
MCP.

### Installing it

**There is no package on PyPI or any other index**, so there is nothing to `pip install`. Install
from the repository with `uv`, pinned to a release tag:

```bash
# Persistent — for a machine that will use the client repeatedly.
uv tool install git+https://github.com/atriensis/cassetta.git@v0.27.0

# One-off — runs the command and leaves nothing installed.
uvx --from git+https://github.com/atriensis/cassetta.git@v0.27.0 cassetta --help
```

Pin the tag rather than tracking a branch. A client that silently follows the default branch changes
under you between one run and the next, which turns every report into a question about which revision
was in play.

### The commands

- `cassetta upload` — stream a tar bundle to `POST /upload/{bundle_path}`
- `cassetta download` — fetch every file in a reference envelope via `GET /download/...`
- `cassetta send` — both phases at once: mint the upload session, then stream the tar
- `cassetta capabilities` — print the server's advertised limits and features

`cassetta --help` lists them, and each takes `--help` of its own. See
[CLIENT_SETUP.md](CLIENT_SETUP.md) for the full reference — flags, exit codes and failure states.
