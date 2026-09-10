# Client Setup — Connecting an Agent to Cassetta

This is the **human-facing** guide for connecting an MCP-capable agent
(Claude Code, Copilot CLI, OpenCode, Gemini CLI, ...) to a running
Cassetta server.

If you'd rather have your AI agent set this up for you end-to-end, paste
[`AGENT_SETUP.md`](AGENT_SETUP.md) into the agent chat instead — it
contains the same steps written as direct instructions for the agent.

## Prerequisites

- A reachable Cassetta server. You should know its base URL, e.g.
  `http://cassetta.example.com:16001` or `http://localhost:16001`.
- The Cassetta **setup token** (the value of `CASSETTA_SETUP_TOKEN` on the
  server). Used to mint API keys.
- `curl` and the `claude` CLI (for Claude Code; other clients have their
  own MCP registration commands).

## Step 1 — Verify the server

```bash
CASSETTA_URL='http://your-cassetta-host:16001'
curl -fsS "$CASSETTA_URL/health"
# expected: {"status":"ok","dev_mode":false}
```

If this fails, the hostname does not resolve from this machine, the port
is blocked, or the container is not running. Fix that before continuing.

`dev_mode` describes the server, not you. `true` means it was started with
an empty setup token and authenticates nothing at all — see
[REST_API.md](REST_API.md#what-get-health-answers).

## Step 2 — Mint an API key for this agent

Each (machine, project) pair should have its **own** API key. The label
identifies the agent in audit logs and is what you revoke / rotate later.
You do not choose the label directly: you send a **machine** and a
**project**, and the server joins them as `machine:project`.

Examples of the result: `home-laptop:assistant`,
`work-laptop:financial-toolbox`, `pi:scratch`,
`ci-runner:integration-tests`.

```bash
SETUP_TOKEN='<the cassetta setup token>'
HOST='this-machine'
PROJECT='this-project'

curl -fsS -X POST \
  -H "X-Setup-Token: $SETUP_TOKEN" \
  -H "Content-Type: application/json" \
  "$CASSETTA_URL/keys" \
  -d "{\"host\":\"$HOST\",\"project\":\"$PROJECT\"}"
```

**Send `host` and `project`, not a pre-joined `label`.** A body of
`{"label": "..."}` is rejected with HTTP 422 naming `host` and `project`
as the missing fields — the label is a derived value the server computes
and returns, never an input.

The response includes `"label": "this-machine:this-project"` and
`"api_key": "cst_..."`. **Save the key now** — it is shown only once.
There is no way to read it back later; if you lose it, rotate with
`POST /keys/{label}/rotate`. The colon goes into that URL unencoded:
`POST /keys/this-machine:this-project/rotate`, and likewise
`DELETE /keys/this-machine:this-project`.

`POST /keys` accepts the `X-Setup-Token` header shown above **or** an
`Authorization: Bearer cst_...` header holding an existing agent key.
Either mints a new key; use whichever credential you already have.

If the label already exists, you get HTTP 409 — pick another machine or
project name.

For a **brand-new server with no keys yet**, use `POST /setup` instead of
`POST /keys`: same body, same response shape. It takes only the setup
token — there is no agent key to present yet — and returns HTTP 409 once
any key exists, which is what makes it a one-time bootstrap.

## Step 3 — Register the MCP server

### For Claude Code

```bash
API_KEY='cst_...'   # from step 2
claude mcp add \
  --transport http \
  cassetta \
  "$CASSETTA_URL/mcp/" \
  --header "Authorization: Bearer $API_KEY"
```

Two crucial details:

- **The trailing slash on `/mcp/` is required.** Without it the server
  responds 307 to `/mcp/`, and the MCP client does not follow the
  redirect — the connection fails silently.
- **Default scope is `local`**, which is exactly what you want. Despite
  the file name, `~/.claude.json` is sectioned by project path — the
  `local` scope writes the entry under the current project directory
  only, and it loads only when Claude Code starts in that directory.
  So `local` means "this project on this machine", not "all projects on
  this machine". See "Why not `settings.json`?" below for the
  alternatives and why this is the correct one.

### For other MCP clients

The shape is always: HTTP transport, base URL ending in `/mcp/`,
`Authorization: Bearer cst_...` header. Configure per your client's
docs (Copilot CLI, OpenCode, Gemini CLI, ...).

## Step 4 — Verify and restart

```bash
claude mcp list | grep cassetta
# expected: cassetta: <url>/mcp/ (HTTP) - ✓ Connected
```

Then **close and reopen** the Claude Code session — MCP servers are
loaded only at session start. After restart, the cassetta tools should
be available: `cassetta_put`, `cassetta_get`, `cassetta_delete`,
`cassetta_list`, `cassetta_send_init`, `cassetta_send_inline`,
`cassetta_inbox`, `cassetta_pick`, `cassetta_peek`,
`cassetta_capabilities`, `cassetta_agents`, `cassetta_broadcast`.

`cassetta_peek` returns the full metadata of a store or inbox bundle
(sender, created\_at, per-file manifest) without consuming or mutating
the bundle — useful for previewing a handoff before deciding to pick
it.

`cassetta_capabilities` returns the server's advertised limits, TTLs,
supported transport modes, and features. Agents SHOULD call it once
per session and cache the result; see "Handshake: discovering server
capabilities" below for the full pattern.

> **Before exposing this server beyond `localhost`**, replace the
> development-only `CASSETTA_JWT_KEY` shipped in `.env.example` with a
> freshly-generated key:
>
> ```bash
> openssl rand -base64 32 > /tmp/k && export CASSETTA_JWT_KEY=$(cat /tmp/k)
> ```
>
> Or switch to `CASSETTA_JWT_KEY_FILE=<path>` for managed-secret setups.
> The shipped placeholder decodes to a fixed English literal — anyone
> who clones the repo can mint a JWT against an unmodified `.env`-shipped
> server.

Upload rejections now surface in a structured wire format: REST
responses use HTTP 413 with `{"error": "cap_exceeded", ...}` for hard
caps and HTTP 422 with `{"error": "batch_required", ...}` when a
payload exceeds `max_inline_size` (the inline-transport threshold).
MCP tool errors use the stable `cap_exceeded: ` / `batch_required: `
prefix. See `docs/CONFIG.md` for the `CASSETTA_PER_*` /
`CASSETTA_MAX_INLINE_SIZE` env-var reference.

## Handshake: discovering server capabilities

The server advertises its upload caps, TTLs, and supported modes at a
single endpoint. Agents SHOULD call it once per session, cache the
result, and use it to fail fast on oversized payloads before a
`cassetta_send_init` round-trip.

Two surfaces return the **same** document:

- MCP tool `cassetta_capabilities` (returns a JSON string).
- REST `GET /capabilities` (returns a JSON object).

CLI introspection for operators:

*The sample below is one server answering with the built-in defaults.
Yours will report its own version, and different numbers if the operator
tightened limits or TTLs — the point of asking is that you do not have to
guess. The `features` list in particular grows between releases.*

**`--api-key` is not optional.** `GET /capabilities` answers `401`
without a credential, and unlike `cassetta send` this command reads **no**
environment variable — `--url` and `--api-key` are the only inputs it has,
so exporting `CASSETTA_API_KEY` does nothing for it.

```
$ cassetta capabilities --url http://localhost:16001 --api-key "$API_KEY"
Server version: 0.30.0
Schema version: 1
Supported modes: inline, batch, reference
Features: peek, batch_upload, reference_download, rest_send_init
Limits:
  per_file_max: unlimited
  per_bundle_total_max: unlimited
  per_bundle_file_count_max: 25
  max_inline_size: 102400
TTLs (seconds):
  upload_token_ttl: 300
  download_claim_ttl: 300
  passive_gc_min_age: 3600
  passive_gc_interval: 600
```

`null` in any `limits` field means *no limit on this axis*.
`schema_version` is an integer; the server bumps it only on
non-additive wire changes, so agents can pin on major-version parity
and ignore additive future fields.

### Cache-and-precheck pattern (client-side)

The same shared helper the server uses is public:

```python
import httpx
from cassetta.limits import check_manifest_against_limits

# Once per session: fetch + cache caps.
caps = httpx.get(f"{base_url}/capabilities",
                 headers={"Authorization": f"Bearer {api_key}"}).json()
limits = caps["limits"]

# Build a manifest for the intended upload.
manifest = {
    "files": [{"name": "release-notes.md", "size": 420}],
    "file_count": 1,
}

# Pre-check locally — no network call.
decision = check_manifest_against_limits(manifest, limits)
if "error" in decision:
    raise RuntimeError(
        f"upload blocked by cached caps: "
        f"{decision['constraint']} "
        f"(limit {decision['limit']}, observed {decision['observed']})"
    )

# decision is {"mode": "inline"|"batch", "reason": None}
# Proceed to cassetta_send_init with the chosen mode.
```

Server-side enforcement remains authoritative — `cassetta_send_init`
and `POST /upload/...` still run the same check and will reject any
payload the cached caps happen to have missed (e.g. the operator
tightened caps mid-session). Refresh the cache on reconnect, or after
a rejection whose `constraint` does not match what the cached caps
predicted.

## Installing the `cassetta` CLI

**No package index carries Cassetta.** There is no `pip install cassetta`
to run — the client is declared at `pyproject.toml` `[project.scripts]`
and installed from the repository with `uv`, pinned to a release tag:

```bash
# Persistent — for a machine that will use the client repeatedly.
uv tool install git+https://github.com/atriensis/cassetta.git@v0.30.0

# One-off — runs the command and leaves nothing installed.
uvx --from git+https://github.com/atriensis/cassetta.git@v0.30.0 cassetta --help
```

Either way you get a `cassetta` executable with four subcommands:
`upload`, `download`, `send` and `capabilities`. Pin the tag rather than
tracking a branch: a client that quietly follows the default branch
changes under you between runs, which makes every problem report a
question about which revision was installed.

## Using `cassetta upload`

The legacy `cassetta_send` MCP tool and the
`PUT /inbox/{agent}/{path}` REST endpoint are **retired**; they are
replaced by a two-phase upload flow. For bundles larger than
`CASSETTA_MAX_INLINE_SIZE` (default 100 KiB) the agent must shell out
to the `cassetta upload` CLI, which streams a tar archive to
`POST /upload/{bundle_path}` using the JWT credential from
`cassetta_send_init`.

### Canonical example

From the working directory where the local paths match the manifest
names the agent declared to `cassetta_send_init`:

```bash
cassetta upload \
  --url  "http://localhost:16001/upload/inbox%2Falice%3Amain%2Frelease-notes.md" \
  --token "eyJhbGciOiJIUzI1Ni..." \
  ./release-notes.md
```

Success prints `bundle_id=<uuid>` to stdout and exits 0:

```
bundle_id=0191a3d0-1b3c-7e29-8012-a2b3c4d5e6f7
```

Directories are accepted and tarred recursively at the requested
arcname:

```bash
cassetta upload --url ... --token ... src/ data/big.bin README.md
```

### Verbatim args (with normalisation)

The CLI treats each positional argument as **both** the local path to
read and the tar entry name. The agent computes manifest names
up-front (at `send_init`) and passes the same paths to `cassetta
upload` — the server then validates each tar entry against the
signed manifest.

The CLI applies a small normalisation pass to match server-side
`validate_path`:

- **Leading `./` is stripped** — `./src/main.py` becomes `src/main.py`.
  The manifest entry the server signed has no `./` prefix; the CLI
  normalises so the two line up.
- **Trailing slashes are stripped** — `data/` becomes `data`. (Tar
  would otherwise encode a directory entry, which the server rejects
  as a non-regular entry.)
- **Absolute paths are rejected** — `/etc/passwd` fails at CLI parse
  time with a clear `BadParameter` error; this mirrors the server
  rule that bundle entries are relative.
- **`..` segments and out-of-charset characters are rejected** via
  the same `validate_path` helper the server runs at `send_init`.

If your manifest declared `src/main.py`, pass `src/main.py` (or
`./src/main.py` — both normalise to the same arcname). Do **not**
`chdir` between the `send_init` call and `cassetta upload`: the
relative paths must still resolve to the files you weighed in the
manifest.

### `--no-compress`

By default `cassetta upload` gzip-compresses the tar stream and sends
`Content-Encoding: gzip`. For payloads that are already compressed
(e.g. `.mp4`, `.zip`, `.pdf`, encrypted archives) gzip adds CPU cost
with no size saving. Pass `--no-compress` to send an uncompressed
`application/x-tar` body:

```bash
cassetta upload --url ... --token ... --no-compress data/archive.mp4
```

The server negotiates automatically from the `Content-Encoding`
header, so the flag is a pure client-side optimisation.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Upload succeeded (`bundle_id=...` on stdout). |
| 1 | Transport / I/O / bad argument (network error, unreadable file, invalid path). |
| 2 | Server returned 4xx (body echoed to stderr — token expired, manifest violation, bundle path conflict, ...). |
| 3 | Server returned 5xx (body echoed to stderr). |

## Using `cassetta send`

`cassetta upload` is phase 2 on its own: it needs an `upload_url` and a
token that something else already minted. `cassetta send` does both phases
in one call — it opens the upload session with your agent key
(`POST /uploads`) and then streams the tar to the URL that comes back. It
is the command a *person* types; the split pair is what an agent drives.

| Parameter | Meaning |
|---|---|
| `files...` | Positional, one or more. Each is both the local path to read and the entry name inside the bundle, with the same normalisation `cassetta upload` applies: leading `./` and trailing slashes are stripped, absolute paths and `..` segments are rejected before any request. |
| `--to` | Recipient label — the full `host:project`, e.g. `alice:main`. A value with no colon is taken as a raw inbox name and is not checked against the key store, so it is accepted even when nobody is listening there. |
| `--path` | The bundle's name in the recipient's inbox, e.g. `handoff-A`. Not a local filename — it is what the recipient sees in their inbox listing and puts in the `peek` and `pick` paths. |
| `--url` | Server base URL. Falls back to `CASSETTA_URL`. |
| `--api-key` | Your agent key. Falls back to `CASSETTA_API_KEY`. |

```bash
export CASSETTA_URL='http://localhost:16001'
export CASSETTA_API_KEY='cst_...'

cassetta send --to alice:main --path handoff-A ./release-notes.md
# bundle_id=0191a3d0-1b3c-7e29-8012-a2b3c4d5e6f7
```

The manifest is computed from the files you name, so the one expensive
mistake in the hand-rolled REST recipe — declaring the archive as an entry
of its own manifest, which phase 1 accepts and phase 2 refuses — cannot be
made here.

Missing configuration or a malformed path fails before any network call,
exiting 2. Otherwise the exit codes are `cassetta upload`'s: 2 for a 4xx,
3 for a 5xx, 1 for a transport failure.

## Using `cassetta download`

The read side is symmetric with the two-phase write flow:
small bundles still come back inline, but bundles over
`CASSETTA_MAX_INLINE_SIZE` now return a **reference envelope** instead
of inlining the bytes. The envelope names per-file URLs and a single
download JWT; it is the CLI's job to fetch them.

### Canonical example

```bash
# 1. Agent gets the envelope from cassetta_pick / cassetta_get. Save it
#    as a file or pipe it straight in on stdin.
cassetta download --manifest-json manifest.json --out ./drop

# or, piping:
echo "$ENVELOPE_JSON" | cassetta download --manifest-json - --out ./drop
```

On success, files land under `./drop/` with the manifest names
preserved (including any nested paths — `./drop/src/lib.py`). The CLI
prints nothing on stdout and exits 0.

### What the CLI sends

For each file, one HTTP GET to the envelope's `files[i].url` with:

- `Authorization: Bearer <download_token>` — the envelope's JWT,
  reused verbatim on every file request.
- `X-Sender: <recipient>` — read out of the JWT's `recipient` claim
  (no signature verification client-side; the server verifies). It is
  the identity half of the two-factor check on `GET /download/...`: the
  token proves the claim, the header declares who is redeeming it, and a
  mismatch is a 403 `identity_mismatch`. This header belongs to the
  download route only — `/inbox/*` and `/files/*` do not read it, and
  authenticate with the agent key alone.

Streaming is used throughout — each response body is `iter_bytes`-pulled
straight to disk without buffering the full file in RAM.

### Manifest input shapes

- `--manifest-json manifest.json` — read the envelope from a file.
- `--manifest-json -` — read from stdin. Handy for piping from the
  tool-call result.

The CLI validates that `envelope["mode"] == "reference"` — passing an
inline-mode envelope exits 2 with `manifest mode is 'inline', not
'reference'; nothing to stream` on stderr (the bytes are already in
the response you piped in; you don't need the CLI for that).

### Exit codes

| Code | Meaning |
|---|---|
| 0 | All files downloaded successfully (directory `--out/<name>` contains every file). |
| 1 | Network / transport error (timeout, DNS, TLS, etc. — exception message on stderr). |
| 2 | Manifest is malformed, mode is not `reference`, `download_token` missing, OR server returned 4xx (body echoed to stderr). Examples: `expired` / `invalid_token` on an expired JWT, `identity_mismatch` on a forbidden identity, `not_found` if the bundle was reaped. |

### Expected `--out` state on failure

Downloads are sequential. If file N fails, files 0..N-1 are already
written to disk; files N..last are **not** created. Re-running with a
fresh envelope (obtain a new one via `cassetta_pick` / `cassetta_get`)
will re-fetch from scratch — the CLI does not resume partial
transfers. For inbox bundles, the original claim survives until TTL
expiry; if the expiry has passed, the bundle reappears in the inbox
and a fresh `pick` will re-mint a credential.

### When NOT to use this CLI

If the envelope you have is `{"mode": "inline", ...}`, the bytes are
already in the JSON — decode `files[i].content` (UTF-8 or base64 per
`encoding`) and write them yourself. The CLI only handles reference
mode; it is not a general-purpose envelope consumer.

## Why not `settings.json`?

A Cassetta MCP connection is **identity**, not project metadata: it
represents *this agent on this machine in this project* — its own API
key, its own inbox, its own audit trail. So:

| Mechanism | Where it lives | Use for cassetta? |
|-----------|---------------|-------------------|
| `claude mcp add` (default `local` scope) | `~/.claude.json`, in this project's section | ✅ recommended |
| `claude mcp add --scope user` | `~/.claude.json`, in the global (cross-project) section | only if you really want one key for every project on this machine |
| `claude mcp add --scope project` | `<project>/.mcp.json`, committed to git | ❌ key would end up in the repo |
| Hand-edited `.claude/settings.json` | rejected by schema | ❌ does not work |

Claude Code's `settings.json` schema does **not** accept an `mcpServers`
field — trying to add it produces a validation error. Always use the
`claude mcp add` CLI.

Three reasons not to commit the connection:

1. **Each (machine, project) needs its own key.** If two machines share
   one key, audit logs cannot tell them apart and revocation affects both.
2. **API keys are credentials.** They should never be in git, even in a
   private repo.
3. **Different agents may need different inboxes.** Your work laptop and
   home laptop are separate Cassetta identities.

## Troubleshooting

**`claude mcp list` shows `✗ Failed to connect`**
- `curl "$CASSETTA_URL/health"` — server reachable?
- URL ends with `/mcp/` (trailing slash)? Re-run `claude mcp add` if not.
- API key correct (no whitespace, full `cst_` prefix)? Verify with
  `claude mcp get cassetta`.

**HTTP 421 "Invalid Host header"**

The MCP endpoint has DNS-rebinding protection — by default it only
accepts loopback hosts. The server operator must add the `Host` header
value your client sends to `CASSETTA_MCP_ALLOWED_HOSTS` and restart,
e.g.:

```
CASSETTA_MCP_ALLOWED_HOSTS=cassetta.example.com,cassetta.example.com:16001
```

**Two entries, and both are needed.** The list is matched against the
`Host` header literally — nothing resolves a name or strips a port — and
the port is part of that header whenever it is not the scheme's default.
So a client pointed at `https://cassetta.example.com` sends the first
spelling and a client pointed at `http://cassetta.example.com:16001`
sends the second, and listing only one of them leaves the other on `421`
with everything looking correctly configured at both ends. An unlisted
value is still refused, so listing both costs nothing.

This is a server-side fix.

Note that **authentication runs before the host check**: an
unauthenticated request answers `401` and never reveals the `421`. A
bare `curl` against `/mcp/` therefore cannot tell you whether the
allowlist is your problem — send the API key when you probe.

**HTTP 401**
- Wrong / missing / revoked API key, or wrong / missing setup token.
- `Authorization` header must be exactly `Bearer cst_...`.

**HTTP 409 from `POST /keys`**
- Label already exists. Pick a different label or rotate the existing
  key with `POST /keys/{label}/rotate`.

**Connection refused / timeout**
- Server not running, port blocked, or unreachable network.
- For remote hosts on a different network you may need an SSH tunnel
  (`ssh -L 16001:localhost:16001 your-host`) or a VPN.

**Tools appear but calls fail**
- Check server logs: `docker compose logs cassetta`.
- Read the error in the MCP response — it usually says exactly what is
  wrong (storage permissions, expired key, etc.).


## Broadcast a bundle to every visible recipient

`POST /broadcast/<path>` delivers a bundle to every recipient your access
policy considers visible. **In this repository that means every agent
holding an active key**: the access policy shipped here allows every
action and the visibility set is the whole key store. There is no
grouping to configure, and no way to broadcast to a subset — if you need
one, send to each label individually.

The `<path>` segment is the **logical destination address** — the alias
resolver expands it into per-target inbox addresses. Path segments with
embedded slashes (`team-alpha/morning-update.md`) are supported via
FastAPI's `:path` converter.

```bash
curl -X POST "http://localhost:16001/broadcast/team-alpha/morning-update.md" \
  -H "Authorization: Bearer $KEY" \
  --data-binary @morning-update.md
```

The response distinguishes per-target outcomes:

```json
{
  "path": "team-alpha/morning-update.md",
  "delivered_to": ["bob:main", "carol:main"],
  "denied":  [{"target": "dan:main",  "reason": "access_denied"}],
  "failed":  [{"target": "eve:main",  "error":  "resolver_error: ..."}],
  "total_delivered": 2,
  "total_denied":    1,
  "total_failed":    1
}
```

Targets are named by key label throughout — the recipients are taken from
the key store, and the sender's own label is excluded.

> **Not in this repository.** The commercial distribution adds a
> team-scoped access policy that filters recipients outside your team out
> of the visibility set entirely — they appear in none of
> `delivered_to`, `denied` or `failed`, so a broadcast cannot be used to
> enumerate labels you are not permitted to see. Nothing here implements
> that, because there are no teams here: see **Core / Cloud** in
> [GLOSSARY.md](GLOSSARY.md). Mentioned so that a `delivered_to` list
> which looks short on one deployment and complete on another is not a
> surprise.

## Admin endpoints — not in this repository

`/admin/users`, `/admin/teams` and `/admin/invites` **do not exist here**.
This server mounts no `/admin/*` route at all: any such request is a 404,
the OpenAPI document contains nothing under that prefix, and there is no
environment variable that turns them on — [CONFIG.md](CONFIG.md)
enumerates every variable this server reads, and a test keeps that list
honest in both directions.

They belong to the commercial distribution described under **Core /
Cloud** in [GLOSSARY.md](GLOSSARY.md), which builds on this one. This
section exists so that a reader who has met those endpoints elsewhere —
in the other distribution's documentation, or in an agent's memory of a
different deployment — gets an answer here rather than concluding they
have misconfigured something.

Everything else in this document describes routes this repository serves.
