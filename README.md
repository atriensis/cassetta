# Cassetta

A lightweight file exchange bus for distributed AI agents. Cassetta runs as a
small API service that lets agents on different hosts exchange files through
either MCP (the primary integration for Claude / Claude Code agents) or a
plain REST API. Storage is pluggable; the default backend is the local
filesystem so you can self-host on a Raspberry Pi, a VPS, or your laptop.

> Cassetta is built with the assistance of AI coding tools. The human author
> is the sole owner of every commit; AI assistance is disclosed here, not in
> git history.

## Quickstart with Docker

You will need Docker and Docker Compose v2.

```bash
cp .env.example .env
# Edit .env and set CASSETTA_SETUP_TOKEN to a long random string.
mkdir -p data data.keys   # bind-mount sources; Docker will not create them for you
docker compose up -d
```

Wait a few seconds for the container to become healthy, then verify:

```bash
curl http://localhost:16001/health
# {"status":"ok"}
```

### Mint your first API key

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-Setup-Token: YOUR_SETUP_TOKEN" \
  http://localhost:16001/setup \
  -d '{"host":"my-laptop","project":"notes"}'
# {"label":"my-laptop:notes","api_key":"cst_...","created_at":"2026-01-01T00:00:00Z"}
```

The key is labelled `host:project`, so one machine can hold a separate key per
project. Save the returned `api_key` — it is shown only once. From now on,
authenticate REST calls with `Authorization: Bearer cst_...`.

### Store and retrieve a file

```bash
API_KEY=cst_...

curl -X PUT \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: text/plain" \
  --data-binary "a note from the quickstart" \
  http://localhost:16001/files/notes/hello.txt

curl -H "Authorization: Bearer $API_KEY" \
  http://localhost:16001/files/notes/hello.txt
# {"mode":"inline","bundle":{...},"files":[{"name":"hello.txt","content":"a note from the quickstart","encoding":"utf8"}]}
```

A read answers with a JSON envelope rather than the raw bytes, because a stored file
is a one-file bundle and the same envelope serves a one-file read and a many-file read
alike. Your content is in `files[0].content`, tagged `utf8` when it decodes as text and
`base64` when it does not. To get the bytes back out:

```bash
curl -fsS -H "Authorization: Bearer $API_KEY" \
  http://localhost:16001/files/notes/hello.txt \
| python3 -c 'import sys,json,base64;d=json.load(sys.stdin)["files"][0];c=d["content"];sys.stdout.buffer.write(base64.b64decode(c) if d["encoding"]=="base64" else c.encode())'
# a note from the quickstart
```

See [docs/REST_API.md](docs/REST_API.md) for the envelope field by field.

### Check that all of the above actually works

`scripts/smoke.sh` performs exactly these steps against a container built from this
repository — it prepares the environment file, starts the stack, waits for the container to
become healthy, mints a key, stores a file and reads it back, decoding the envelope with the
recipe above and asserting the decoded content matches what it wrote. Run it from a clean
clone:

```bash
./scripts/smoke.sh
```

It refuses to run over an existing `.env` or an existing key store, so it will not disturb a
deployment you already have; it takes the stack down again when it finishes, and it removes
both the `.env` and the API key store it created, so the quickstart above still works
afterwards. The one thing it leaves behind is the file it stored under `data/`, which is your
own storage directory and blocks nothing. The same script runs once a week against the default
branch, which is what keeps this quickstart honest: if the documented steps stop working, that
run goes red.

Older copies of the script removed only the `.env`. If you ran one, the key store it left
behind carries `setup_done: true` and the quickstart's first call answers
`409 {"detail":"Setup already completed"}` — which arrives at the first step of the thing you
ran the script to prove. Remove `data.keys/.cassetta-keys.json` and start again.

### Connect an MCP agent

Register Cassetta with your agent's MCP client. For Claude Code, that is the
`claude mcp add` CLI, run from the project directory the agent works in:

```bash
claude mcp add \
  --transport http \
  cassetta \
  "http://localhost:16001/mcp/" \
  --header "Authorization: Bearer cst_..."
```

Two details decide whether this works:

- **The trailing slash on `/mcp/` is required.** Without it the server answers
  `307` redirecting to `/mcp/`, and MCP clients do not follow the redirect — the
  connection fails silently rather than reporting an error.
- **Do not hand-write the server into `.claude/settings.json` or
  `.claude/settings.local.json`.** Claude Code's settings schema has no field for
  MCP servers and rejects the attempt. `claude mcp add` writes to
  `~/.claude.json`, sectioned by project path, which is why its default `local`
  scope means "this project on this machine".

Restart the session — MCP servers load only at start — and your agent gains the
twelve `cassetta_*` tools, from storing and listing files to sending a bundle to
another agent's inbox and picking one up. Among them is the non-destructive
`cassetta_peek` (and the matching `GET /.../peek` REST endpoints), which returns
bundle metadata without consuming the bundle.
[docs/CLIENT_SETUP.md](docs/CLIENT_SETUP.md) lists every tool and what it does;
[docs/REST_API.md](docs/REST_API.md) covers the REST equivalents.

**Why not committed config?** Each (machine, project) should have its own API key
for clean audit trails and per-machine revocation — so the registration is
per-machine state, not something to check into a repository where the key would
travel with it. [docs/CLIENT_SETUP.md](docs/CLIENT_SETUP.md) has the full
rationale, the key labelling conventions, and troubleshooting.

**Want the agent to configure itself?** Send your agent the contents of
[docs/AGENT_SETUP.md](docs/AGENT_SETUP.md) — it will ask you for the URL and the
setup token, mint its own key, and run `claude mcp add` for you.

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

## Configuration

All configuration is read from environment variables. `.env.example` is a
copyable starting point and [docs/CONFIG.md](docs/CONFIG.md) is the full
reference. The table below is the short list — the variables most deployments
touch, not all of them:

| Variable | Default | Description |
|----------|---------|-------------|
| `CASSETTA_SETUP_TOKEN` | *(required)* | Token for the one-time `/setup` call. Set to `""` for dev mode (no auth). |
| `CASSETTA_PORT` | `16001` | Host port to expose the service on. |
| `CASSETTA_STORAGE_PATH` | `/data` | Storage path inside the container (do not change unless you know what you are doing). |
| `CASSETTA_KEYS_FILE` | *(sibling of storage)* | Path to the API key store. Defaults to `<storage>.keys/.cassetta-keys.json` so the keys file lives outside the storage directory and never appears in `/files/` listings. |
| `CASSETTA_DEFAULT_TTL` | `0` | Default file TTL in seconds. `0` disables expiration. |
| `CASSETTA_JWT_KEY` | *(required)* | Base64-encoded HS256 signing key, ≥32 bytes decoded. The shipped `.env.example` carries a fixed dev-only placeholder so quickstart works clone-and-run; replace before any deployment beyond `localhost`. Use `CASSETTA_JWT_KEY_FILE` for managed-secret setups. |
| `CASSETTA_PUBLIC_BASE_URL` | *(required)* | Absolute base URL used to compose download / upload URLs in batch responses. |
| `CASSETTA_MCP_ALLOWED_HOSTS` | *(empty)* | Comma-separated allowlist of `Host` header values for the MCP endpoint (DNS-rebinding protection). Matched literally, port included, so a deployment on a non-default port needs both spellings: `cassetta.example.com,cassetta.example.com:16001`. Empty means loopback-only. |

To change a value, edit `.env` and run `docker compose up -d` again — no
image rebuild is needed.

## Self-Hosting Guide

Cassetta is designed to be self-hosted on any small Linux machine you control:
a Raspberry Pi at home, a $5 VPS, an old laptop, or a corporate sandbox.

### Server preparation

1. Install Docker Engine and Docker Compose v2 from your distro packages or
   from <https://docs.docker.com/engine/install/>.
2. Clone or copy this repository to the server.
3. Create `.env` from `.env.example` and set the three variables the server
   requires — it refuses to start without them, naming the one it missed:
   - `CASSETTA_SETUP_TOKEN` — a long random string. (The empty string is also
     accepted and selects dev mode, which disables authentication entirely.
     Local experiments only.)
   - `CASSETTA_PUBLIC_BASE_URL` — the address agents can actually reach, which
     is what upload and download URLs are composed from. Not the bind address,
     when the two differ.
   - `CASSETTA_JWT_KEY` — a freshly generated signing key, or
     `CASSETTA_JWT_KEY_FILE` pointing at one. Do not ship the placeholder in
     `.env.example`; see the warning above.
4. If agents will reach MCP from another machine, add to
   `CASSETTA_MCP_ALLOWED_HOSTS` the `Host` header values those clients will send.
   It is empty by default, which means loopback only, and a request from elsewhere
   is rejected with `421 Invalid Host header`. The list is matched literally and
   the port is part of the header, so a client reaching you on `:16001` sends
   `name:16001` and needs that spelling listed — usually alongside the bare name.
5. Run `docker compose up -d`.

Everything else has a working default. [docs/CONFIG.md](docs/CONFIG.md) is the
full reference — every variable the server reads, its default, and its effect —
and a test keeps it in step with the source in both directions.

### Persistence and backups

Stored files live under `./data` on the host (bind-mounted to `/data`
inside the container). The API key store lives under `./data.keys` on the
host (bind-mounted to `/data.keys`) — kept outside the storage directory so
that key files never appear in `/files/` listings or get served as user
data. Back both directories up regularly. To migrate to a new host, stop
the service, copy `data/` and `data.keys/` across, and start the service
again on the new host — no extra steps required.

### Mixed-content storage (advanced)

Cassetta tolerates plain (non-bundle) files in the storage backend. You
can drop a regular file into `./data/` by hand, or share the storage
directory with another tool, and Cassetta will list and serve those files.
Plain files use the filesystem's mtime as their `created_at` and have no
sender attribution. The `/inbox/` namespace does **not** accept plain
files (sender attribution is required): a bundle appears in an inbox
listing only when it arrived through the two-phase send flow or a
broadcast, both of which record who sent it. There is no single-request
write to an inbox — the legacy `PUT /inbox/{agent}/{path}` was retired
and now answers `410 Gone` naming its replacement.

Inboxes separate recipients; they do not isolate them. Under the access
policy shipped here, any valid API key may list and read any label's
inbox, exactly as it may read any file in the store. That is the right
answer for one person self-hosting, and the wrong one for a machine
shared between people who should not read each other's mail.

### Networking and TLS

Cassetta itself terminates plain HTTP on the host port you configure. For
public exposure put it behind a reverse proxy that handles TLS, for example
[Caddy](https://caddyserver.com/) or
[Traefik](https://doc.traefik.io/traefik/). Cassetta deliberately does **not**
ship its own TLS termination — that is your reverse proxy's job and gives you
flexibility around certificates and renewal.

If you do not need remote access, bind the host port to loopback so the service
is unreachable from the rest of the network. The address to bind is part of the
port mapping, so edit `docker-compose.yml` — it carries the substitution to make
as a comment on the line above:

```yaml
ports:
  - "127.0.0.1:${CASSETTA_PORT:-16001}:16001"
```

`CASSETTA_PORT` itself stays what it is: the published host port, read by Compose
alone and never by the server.

### Multi-arch support

The provided Dockerfile builds for both `linux/amd64` (most VPSes, x86 home
servers) and `linux/arm64` (Raspberry Pi 4/5, Apple Silicon). Use:

```bash
docker buildx build --platform linux/arm64,linux/amd64 .
```

(Add `--load` for a single-arch local image, or `--push` to push to a
registry.)

### Updating

Pull the latest sources, then:

```bash
docker compose up -d --build
```

Existing data in `./data` is preserved. The container will be recreated and
the new image will be used.

## Operations

| Task | Command |
|------|---------|
| Start | `docker compose up -d` |
| Stop | `docker compose down` |
| Logs | `docker compose logs -f` |
| Health | `curl http://localhost:16001/health` |
| Restart | `docker compose restart` |
| Rebuild | `docker compose up -d --build` |

## Documentation

Everything under `docs/`, and what each file answers:

- [docs/CONFIG.md](docs/CONFIG.md) — every environment variable the server reads,
  its default and its effect. The one to open when something will not start.
- [docs/CLIENT_SETUP.md](docs/CLIENT_SETUP.md) — connecting an MCP agent by hand:
  minting a key, registering the server, the `cassetta` CLI, troubleshooting.
- [docs/AGENT_SETUP.md](docs/AGENT_SETUP.md) — the same steps written as
  instructions to paste into an agent, plus how an agent sends and receives.
- [docs/REST_API.md](docs/REST_API.md) — the REST surface: credentials, every
  endpoint, two worked workflows.
- [docs/GLOSSARY.md](docs/GLOSSARY.md) — the project's vocabulary, and what is
  and is not part of this repository.
- [docs/LICENSE_FAQ.md](docs/LICENSE_FAQ.md) — what the licence lets you do, in
  plain terms.
- [docs/adr/](docs/adr/) — the architecture decisions and why they were taken.

## Changes, contributions, security

- [CHANGELOG.md](CHANGELOG.md) — what changed in each released version, and whether it affects you.
- [CONTRIBUTING.md](CONTRIBUTING.md) — how a change gets in, and the agreement it needs first.
- [SECURITY.md](SECURITY.md) — where to report a vulnerability, and which versions are covered.

## License

Cassetta is licensed under the [Functional Source License, Version 1.1,
ALv2 Future License](LICENSE) (FSL-1.1-ALv2).

You can use, modify, and self-host Cassetta freely. The only restriction
is competitive use — you may not offer it as a managed service that
competes with Cassetta.

The license automatically converts to [Apache License 2.0](http://www.apache.org/licenses/LICENSE-2.0)
two years after each version's release date.
