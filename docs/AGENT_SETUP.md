# Agent Setup Instructions — Cassetta MCP

> **For humans:** Paste this entire file (or a link to it) into your AI
> coding assistant chat. The agent will ask you for the connection
> details, mint an API key, and register the MCP server. Have your
> Cassetta server URL and your setup token retrieval method ready.

---

You (the agent) are being asked to connect this workspace to a
**Cassetta** MCP server — a self-hosted file exchange bus for
distributed AI agents. Follow these steps in order. Do not improvise:
if any required value is missing, ask the human for it explicitly.

## Step 0 — Gather inputs from the human

Ask the human for these three things:

1. **Cassetta base URL** — e.g. `http://cassetta.example.com:16001`,
   `http://192.168.1.10:16001`, `http://localhost:16001`. Without
   trailing slash, without `/mcp`.
2. **How to read the Cassetta setup token** — the human stores
   `CASSETTA_SETUP_TOKEN` somewhere (env var, password manager CLI like
   `op read 'op://VAULT/ITEM/FIELD'`, K8s secret, plain file, ...). Use
   the **exact** retrieval method they tell you. Do not invent vault
   paths or environment variable names.
3. **A machine name and a project name for this agent** — the two halves
   of its label, which the server joins as `<machine>:<project>`, for
   example `work-laptop:assistant` or `home-laptop:financial-toolbox`.
   Each (machine, project) pair must have its own unique label. If the
   human is unsure, suggest one based on the hostname and the current
   project directory name. You send the two halves separately, never the
   joined string — see Step 2.

## Step 1 — Verify the server is reachable

```bash
CASSETTA_URL='<from step 0>'
curl -fsS "$CASSETTA_URL/health"
```

You must see `{"status":"ok"}`. If you don't:
- The hostname does not resolve from this machine (different network,
  no VPN, no Tailscale).
- The port is blocked.
- The container is not running.

**Stop and report this to the human. Do not proceed.**

## Step 2 — Mint an API key for this agent

Read the setup token using the method the human provided in Step 0,
then POST to `/keys`:

```bash
# Example using 1Password CLI — replace with the human's actual method
SETUP_TOKEN="$(op read 'op://VAULT/ITEM/FIELD')"

HOST='<machine name from step 0>'
PROJECT='<project name from step 0>'

curl -fsS -X POST \
  -H "X-Setup-Token: $SETUP_TOKEN" \
  -H "Content-Type: application/json" \
  "$CASSETTA_URL/keys" \
  -d "{\"host\":\"$HOST\",\"project\":\"$PROJECT\"}"
```

**The body is `host` and `project`, two separate fields.** The server
joins them and returns the result as `label`; sending a pre-joined
`{"label": "..."}` is rejected with HTTP 422 naming `host` and `project`
as missing.

The response is JSON containing `"label": "host:project"` and
`"api_key": "cst_..."`. **Capture the key immediately** — Cassetta shows
it only once. If you lose it you must rotate the key.

`POST /keys` accepts the `X-Setup-Token` header shown above **or** an
`Authorization: Bearer cst_...` agent key. If this workspace already
holds a working key, use it and do not ask the human for the setup token
a second time.

If you get HTTP 409, the label already exists. Ask the human whether to
pick a new one or rotate the existing key with
`POST /keys/{label}/rotate` — the colon in the label goes into the URL
as-is, e.g. `POST /keys/work-laptop:assistant/rotate`.

If you get HTTP 401, the credential is wrong. Re-check Step 0.

If this is a brand-new server with no keys yet, the human should tell
you so — in that case use `POST /setup` instead of `POST /keys`. Same
body, same response shape; it takes only the setup token, because there
is no agent key to present yet, and it answers 409 once any key exists.

## Step 3 — Register the MCP server

For Claude Code, use `claude mcp add` (do **not** edit `settings.json`
or `settings.local.json` — the schema does not accept `mcpServers` and
the change will be rejected):

```bash
API_KEY='cst_...'   # from Step 2 response

claude mcp add \
  --transport http \
  cassetta \
  "$CASSETTA_URL/mcp/" \
  --header "Authorization: Bearer $API_KEY"
```

Two requirements you must follow exactly:

- **The trailing slash on `/mcp/` is mandatory.** Without it the server
  responds 307 to `/mcp/` and the Claude Code MCP client does not follow
  the redirect — the connection fails silently.
- **Default scope is `local`**, which is exactly what you want.
  Despite the name, `~/.claude.json` is a single file shared between all
  projects on this machine, but it is **sectioned by project path** —
  the `local` scope writes the cassetta entry under the current project
  directory only, and the entry loads only when Claude Code is started
  in that directory. So `local` means "this project on this machine",
  not "all projects on this machine". Do **not** pass `--scope project`
  (would write to `<project>/.mcp.json`, which is committed to git) or
  `--scope user` (would put the entry under a global section that
  applies to every project).

For other MCP clients (Copilot CLI, OpenCode, Gemini CLI), the shape is
the same: HTTP transport, base URL ending in `/mcp/`,
`Authorization: Bearer cst_...` header. Configure per their docs.

## Step 4 — Verify

```bash
claude mcp list | grep cassetta
```

Expected:

```
cassetta: <CASSETTA_URL>/mcp/ (HTTP) - ✓ Connected
```

If you see `✗ Failed to connect`:
- `curl "$CASSETTA_URL/health"` — still reachable?
- URL has trailing slash on `/mcp/`?
- API key correct? `claude mcp get cassetta` to inspect the registered
  configuration.
- HTTP 421 "Invalid Host header"? The server's
  `CASSETTA_MCP_ALLOWED_HOSTS` does not include the hostname you used.
  Tell the human — this is a server-side fix.

## Step 5 — Tell the human to restart Claude Code

MCP servers are loaded only at session start. After `claude mcp add`,
the human must **close and reopen the Claude Code session** for the new
server's tools to appear. Tell them this explicitly.

After restart, these tools should be available:

- cassetta_capabilities — discover server limits, TTLs, supported modes (CALL FIRST AT SESSION START)
- cassetta_put — store a file
- cassetta_get — retrieve a file (returns an inline or reference envelope)
- cassetta_delete — delete a file
- cassetta_list — list files (with optional prefix)
- cassetta_send_init — two-phase send, step 1 (declare manifest, receive credential + mode)
- cassetta_send_inline — two-phase send, step 2 for inline mode (deliver bytes in-band)
- cassetta_inbox — list own inbox
- cassetta_pick — atomically consume from own inbox (same envelope shapes as get)
- cassetta_peek — read bundle metadata without consuming
- cassetta_agents — enumerate visible agents per access policy
- cassetta_broadcast — deliver a bundle to every visible recipient

**Before any other tool call, run `cassetta_capabilities`.** It returns
the server's advertised limits, TTLs, supported transport modes, and
features. Cache the result for the session — the document is small
and the values do not change without an operator action. Refresh on
reconnect, or after any rejection whose `constraint` does not match
what the cached capabilities predicted. The same document is reachable
via REST `GET /capabilities` if you need a curl-style fallback.

The legacy `cassetta_send` tool and the `PUT /inbox/{agent}/{path}`
REST endpoint are **retired**. Hitting the REST path now returns
HTTP 410 with a migration pointer. Use the two-phase flow below
instead.

## Sending to another agent

Every send is two phases: `cassetta_send_init` first, then either
`cassetta_send_inline` (small payloads) or the `cassetta upload` CLI
(large payloads). The server picks the mode based on the declared
total size and the configured `CASSETTA_MAX_INLINE_SIZE` threshold
(default 100 KiB).

### Inline path — small bundle, single MCP chain

Call `cassetta_send_init` with the recipient address and a manifest
listing each file you will send.

**`to` is always the recipient's full `host:project` label.** This is the
one mistake here that fails silently, so it is worth the paragraph. A `to`
containing a colon is looked up in the server's key store: if no active
key holds that label you get HTTP 404 `unknown_recipient` and you know at
once. A `to` without a colon is taken as a raw inbox name and checked
against nothing — `"to": "alice"` is accepted with a 201 even if no such
recipient has ever existed, and the bundle lands in `inbox/alice/`, a
different namespace from `inbox/alice:main/`. The recipient's
`cassetta_inbox` lists the label namespace, sees nothing, and reports no
error; neither does the send. Ask the human for the recipient's exact
label rather than guessing the short form.

```json
{
  "to": "alice:main",
  "manifest": {
    "file_count": 2,
    "files": [
      {"name": "src/main.py", "size": 27},
      {"name": "README.md",   "size": 13}
    ]
  }
}
```

If the total size is at or below `max_inline_size`, the server
returns:

```json
{
  "bundle_id": "0191a3c7-e8a0-7b5e-aee1-3b1c1d2f1a2b",
  "mode": "inline",
  "inline_token": "eyJhbGciOiJIUzI1Ni...",
  "expires_at": "2026-04-19T20:05:00Z"
}
```

Call `cassetta_send_inline` with the token and the file bytes —
either UTF-8 text or base64-encoded binary:

```json
{
  "token": "eyJhbGciOiJIUzI1Ni...",
  "files": [
    {"name": "src/main.py", "content": "print('hello cassetta')\n", "encoding": "utf8"},
    {"name": "README.md",   "content": "# hello\n",                 "encoding": "utf8"}
  ]
}
```

Response on success: `{"bundle_id": "...", "ok": true}`. The
recipient picks it up with `cassetta_pick` exactly like before.

### Batch path — large bundle via `cassetta upload` CLI

When the total size exceeds `max_inline_size`, `cassetta_send_init`
returns the batch branch instead:

```json
{
  "bundle_id": "0191a3d0-1b3c-7e29-8012-a2b3c4d5e6f7",
  "mode": "batch",
  "upload_url": "http://localhost:16001/upload/inbox%2Falice%3Amain%2Fproject-drop.tgz",
  "batch_token": "eyJhbGciOiJIUzI1Ni...",
  "expires_at": "2026-04-19T20:10:00Z"
}
```

You cannot feed the bytes back through MCP in this mode. Shell out to
the `cassetta upload` CLI.

**If `cassetta` is not on `$PATH`, install it first.** There is no
package on PyPI or any other index — the client lives in the Cassetta
repository and is installed from it with `uv`, pinned to a release tag:

```bash
uv tool install git+https://github.com/atriensis/cassetta.git@v0.28.3
```

Or run it without installing anything:

```bash
uvx --from git+https://github.com/atriensis/cassetta.git@v0.28.3 \
  cassetta upload --url ... --token ... <files>
```

Tell the human which of the two you used. Do not invent a `pip install`
line: nothing publishes this package, so one would fail. With the client
available, the upload is:

```bash
cassetta upload \
  --url  "http://localhost:16001/upload/inbox%2Falice%3Amain%2Fproject-drop.tgz" \
  --token "eyJhbGciOiJIUzI1Ni..." \
  src/main.py data/big.bin README.md
```

The CLI streams a tar archive to `POST /upload/{bundle_path}`,
verifying each entry against the manifest. On success it prints
`bundle_id=<uuid>` and exits 0.

**Positional args are verbatim (with small normalisation).** The
local paths you pass must match the manifest names exactly; the CLI
strips leading `./` and trailing `/` to paper over cosmetic
differences, and rejects absolute paths or `..` segments. Do not
`chdir` between `cassetta_send_init` and `cassetta upload` — the
manifest entries were signed against the working-directory-relative
names you declared.

Pass `--no-compress` for already-compressed payloads (e.g. `.mp4`,
`.zip`): the CLI will send `application/x-tar` without gzip.

See `docs/CLIENT_SETUP.md` for the full CLI reference
(exit codes, rotation-safe credentials, etc.) and
`docs/REST_API.md` § "Workflow B" for a hands-on walkthrough.

### Things you MUST NOT do

- **Don't** start the upload before `cassetta_send_init` completes.
  Every upload is under a freshly-issued credential.
- **Don't** reuse a `batch_token` across two `cassetta upload`
  invocations. The bundle path is occupied by the first successful
  commit; a second run will fail with `bundle_path_conflict`.
- **Don't** try an `inline_token` against the `POST /upload/...`
  endpoint, or a `batch_token` against `cassetta_send_inline`. The
  mode is pinned in the signed claims and the server rejects
  crosstalk with `unauthenticated: wrong_mode`.

## Receiving from another agent

`cassetta_pick` (inbox) and `cassetta_get` (store) return one of two
unified envelope shapes depending on the bundle's total size and the
server's policy. **Branch on `envelope["mode"]`** — do not assume a
shape.

### Inline mode — small bundle, bytes embedded

```json
{
  "mode": "inline",
  "bundle": { /* full meta.json — bundle_id, files[], total_size, ... */ },
  "files": [
    {"name": "notes.md", "content": "# hello\n",      "encoding": "utf8"},
    {"name": "logo.png", "content": "iVBORw0K...",    "encoding": "base64"}
  ]
}
```

Single-file bundles come through as a 1-entry `files` array — there is
no raw-string fork any more (the retirement described above, symmetric
on the read side).

To consume:

```python
envelope = json.loads(tool_response["content"][0]["text"])
assert envelope["mode"] == "inline"
for f in envelope["files"]:
    if f["encoding"] == "utf8":
        data = f["content"].encode("utf-8")
    else:
        data = base64.b64decode(f["content"])
    Path(f["name"]).write_bytes(data)
```

### Reference mode — large bundle, URLs + download token

```json
{
  "mode": "reference",
  "bundle": { /* full meta.json */ },
  "files": [
    {"name": "notes.md",  "size": 120000, "mime": "text/markdown",
     "url": "http://localhost:16001/download/inbox%2Falice%3Amain%2Fdrop/notes.md"},
    {"name": "logo.png",  "size": 450000, "mime": "image/png",
     "url": "http://localhost:16001/download/inbox%2Falice%3Amain%2Fdrop/logo.png"}
  ],
  "download_token": "eyJhbGciOi…",
  "expires_at": "2026-04-20T22:45:00Z"
}
```

Bytes **never** appear in an MCP response in this mode. The
`download_token` is a single JWT that authorizes every listed URL, and
`GET /download/...` additionally requires an `X-Sender: <your label>`
header matching the token's `recipient` claim (two-factor — JWT +
identity). That header belongs to the download route alone: the
`/inbox/*` and `/files/*` routes do not read it, and sending it there
changes nothing.

You should **not** fetch these URLs yourself inside the tool-call loop —
that pulls bytes back into the LLM context, which is the whole problem
reference mode was designed to solve. **Shell out to the CLI:**

```bash
# Pipe the envelope straight through jq — or save to a file first.
echo "$TOOL_RESPONSE" | jq -c . | cassetta download --manifest-json - --out ./drop
```

The CLI streams each URL to `./drop/<file-name>` with the download JWT
in `Authorization` and the JWT's `recipient` claim echoed on
`X-Sender` (so you do not have to re-supply your own identity on the
command line). Exit 0 = all files on disk.

See `docs/CLIENT_SETUP.md` for the full CLI reference.

### Claim semantics (inbox only)

A reference-mode `cassetta_pick` **claims** the bundle: as soon as the
envelope is returned, the bundle disappears from `cassetta_inbox`
listings. The claim resolves one of two ways:

- **All files fetched via the URLs** — bundle is deleted, claim is
  cleared. Standard "pick consumed" semantics.
- **You do nothing (crash, network drop, forgot to shell out)** — the
  claim expires after `download_claim_ttl` (default 300 seconds) and the
  bundle reappears in listings for a retry.

`cassetta_get` (store) is **not** claim-backed — the store path is
persistent and parallel `cassetta_get` calls on the same path issue
independent credentials.

### Download-endpoint errors

If the CLI exits non-zero, the server body is on stderr as flat JSON
`{"error": ..., "reason": ...}`:

- **401 `unauthenticated`** — JWT problem (bad signature, expired,
  `bundle_path` mismatch, `name` not in the credential's `file_names`).
- **403 `forbidden` reason `identity_mismatch`** — credential validates
  but `X-Sender` (your agent's identity) does not match the JWT's
  `recipient` claim. Usually means you passed someone else's envelope.
- **404 `not_found`** — bundle or file was deleted (already consumed,
  reaped after TTL, or never existed).

## Upload rejection wire format

Oversized payloads surface as structured errors rather than free-form
messages:

- **REST**: HTTP 413 `{"error": "cap_exceeded", "constraint": "...", "limit": N, "observed": N}` for hard caps (`per_file_max`, `per_bundle_total_max`, `per_bundle_file_count_max`) and HTTP 422 `{"error": "batch_required", "constraint": "max_inline_size", ...}` when a payload fits all hard caps but exceeds `max_inline_size` (the inline-transport threshold).
- **MCP**: `ValueError` whose message starts with the stable prefix
  `cap_exceeded: ` or `batch_required: `; the suffix is a short
  human-readable phrase naming the failed constraint.

Programmatic clients should discriminate on the prefix / `error`
field, not the exact suffix wording.

## Things you MUST NOT do

- **Do not** invent the location of the setup token. Always use the
  exact retrieval method the human gave you in Step 0.
- **Do not** edit `.claude/settings.json` or `.claude/settings.local.json`
  to add an `mcpServers` field. The schema rejects it. Use `claude mcp add`.
- **Do not** drop the trailing slash on `/mcp/`. The connection will
  fail silently.
- **Do not** use `--scope project` or `--scope user` for the
  `claude mcp add` command. The default `local` scope is correct.
- **Do not** commit the API key anywhere — to a file, to a memory note,
  to a comment. It lives in `~/.claude.json` outside the repo and that
  is where it stays.
- **Do not** reuse a label across machines or projects. Mint a fresh
  key with a unique label.
