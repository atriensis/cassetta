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
  --data-binary "Hello, Cassetta!" \
  http://localhost:16001/files/notes/hello.txt

curl -H "Authorization: Bearer $API_KEY" \
  http://localhost:16001/files/notes/hello.txt
# Hello, Cassetta!
```

### Connect an MCP agent

Add Cassetta to your agent's MCP server configuration. For Claude Code, the
recommended location is `<your-project>/.claude/settings.local.json`:

```json
{
  "mcpServers": {
    "cassetta": {
      "url": "http://localhost:16001/mcp",
      "headers": {
        "Authorization": "Bearer cst_..."
      }
    }
  }
}
```

Your agent can now call `cassetta_put`, `cassetta_get`, `cassetta_list`,
`cassetta_send`, `cassetta_inbox`, `cassetta_pick`, and `cassetta_peek`.
The non-destructive `cassetta_peek` tool (and the matching
`GET /.../peek` REST endpoints) returns bundle metadata without
consuming the bundle; see `specs/513-peek-and-limits-policy/` for
details.

**Why `settings.local.json` and not committed config?** Each (machine, project)
should have its own API key for clean audit trails and per-machine revocation.
See [docs/CLIENT_SETUP.md](docs/CLIENT_SETUP.md) for the full rationale, key
labelling conventions, and troubleshooting.

**Want the agent to configure itself?** Send your agent the contents of
[docs/AGENT_SETUP.md](docs/AGENT_SETUP.md) — it will ask you for the URL and
API key, then create or update `settings.local.json` for you.

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

All configuration is read from environment variables. The `.env.example` file
documents every supported variable with its default. The most important ones:

| Variable | Default | Description |
|----------|---------|-------------|
| `CASSETTA_SETUP_TOKEN` | *(required)* | Token for the one-time `/setup` call. Set to `""` for dev mode (no auth). |
| `CASSETTA_PORT` | `16001` | Host port to expose the service on. |
| `CASSETTA_STORAGE_PATH` | `/data` | Storage path inside the container (do not change unless you know what you are doing). |
| `CASSETTA_KEYS_FILE` | *(sibling of storage)* | Path to the API key store. Defaults to `<storage>.keys/.cassetta-keys.json` so the keys file lives outside the storage directory and never appears in `/files/` listings. |
| `CASSETTA_DEFAULT_TTL` | `0` | Default file TTL in seconds. `0` disables expiration. |
| `CASSETTA_JWT_KEY` | *(required)* | Base64-encoded HS256 signing key, ≥32 bytes decoded. The shipped `.env.example` carries a fixed dev-only placeholder so quickstart works clone-and-run; replace before any deployment beyond `localhost`. Use `CASSETTA_JWT_KEY_FILE` for managed-secret setups. |
| `CASSETTA_PUBLIC_BASE_URL` | *(required)* | Absolute base URL used to compose download / upload URLs in batch responses. |
| `CASSETTA_MCP_ALLOWED_HOSTS` | *(empty)* | Comma-separated Host header allowlist for the MCP endpoint (DNS-rebinding protection). Empty means localhost-only. Set to your external hostname(s) when accessing MCP from another machine. |

To change a value, edit `.env` and run `docker compose up -d` again — no
image rebuild is needed.

## Self-Hosting Guide

Cassetta is designed to be self-hosted on any small Linux machine you control:
a Raspberry Pi at home, a $5 VPS, an old laptop, or a corporate sandbox.

For Kubernetes deployments, see the [Helm chart](../deploy/helm/cassetta/README.md)
in `deploy/helm/cassetta/`.

### Server preparation

1. Install Docker Engine and Docker Compose v2 from your distro packages or
   from <https://docs.docker.com/engine/install/>.
2. Clone or copy this repository to the server.
3. Create `.env` from `.env.example` and set a strong `CASSETTA_SETUP_TOKEN`.
4. Run `docker compose up -d`.

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
files (sender attribution is required) — only files written via
`PUT /inbox/` or `cassetta_send` appear in inbox listings.

### Networking and TLS

Cassetta itself terminates plain HTTP on the host port you configure. For
public exposure put it behind a reverse proxy that handles TLS, for example
[Caddy](https://caddyserver.com/) or
[Traefik](https://doc.traefik.io/traefik/). Cassetta deliberately does **not**
ship its own TLS termination — that is your reverse proxy's job and gives you
flexibility around certificates and renewal.

If you do not need remote access, bind the host port to `127.0.0.1` only by
setting `CASSETTA_PORT=127.0.0.1:16001` is not supported directly; instead
edit `docker-compose.yml` to use `"127.0.0.1:${CASSETTA_PORT:-16001}:16001"`.

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

## License

Cassetta is licensed under the [Functional Source License, Version 1.1,
ALv2 Future License](LICENSE) (FSL-1.1-ALv2).

You can use, modify, and self-host Cassetta freely. The only restriction
is competitive use — you may not offer it as a managed service that
competes with Cassetta.

The license automatically converts to [Apache License 2.0](http://www.apache.org/licenses/LICENSE-2.0)
two years after each version's release date.
