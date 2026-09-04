# Cassetta configuration reference

All Cassetta configuration is read from environment variables at startup.
This document enumerates every variable the server reads, its default,
and its effect.

Three variables are required; everything else has a built-in default, so
a variable you never set behaves as documented here. `.env.example` in
the repository root is a copyable starting point with the same values.

Sections:

- [Required](#required)
- [Upload flow](#upload-flow)
- [Storage](#storage)
- [Identity](#identity)
- [Limits policy](#limits-policy)
- [Rate limiting and fan-out](#rate-limiting-and-fan-out)
- [MCP and logging](#mcp-and-logging)
- [Command-line client](#command-line-client)

## Required

The server exits at startup, with a message naming the variable, if any
of these is missing.

| Env var | Meaning |
|---|---|
| `CASSETTA_SETUP_TOKEN` | Token that the one-time `POST /setup` call must present to mint the first API key. Set it to a long random string. Setting it to the **empty string** is accepted and enables dev mode — no authentication at all — which is for local experiments only. |
| `CASSETTA_PUBLIC_BASE_URL` | Absolute base URL that agents can reach. It composes the upload and download URLs returned in batch responses, so it must be the externally reachable address, not the internal bind address when those differ. |
| `CASSETTA_JWT_KEY` | Base64-encoded HS256 signing key for upload and download credentials. Required unless `CASSETTA_JWT_KEY_FILE` is set instead. |
| `CASSETTA_JWT_KEY_FILE` | Path to a file whose contents are the base64-encoded signing key. Wins over `CASSETTA_JWT_KEY` when both are set — the managed-secret arrangement. |

`CASSETTA_PUBLIC_BASE_URL` is validated for shape: the scheme must be
`http` or `https` and the host must be non-empty. Trailing slashes are
stripped. A missing or malformed value aborts the boot rather than
producing unreachable URLs at runtime later:

```bash
export CASSETTA_PUBLIC_BASE_URL=http://localhost:16001
```

## Upload flow

Sending a bundle is a two-phase flow (`cassetta_send_init` →
`cassetta_send_inline` or `POST /upload/{bundle_path}`) backed by HS256
JWT credentials. The primary key above signs them; the variables below
are optional and exist for zero-downtime key rotation.

| Env var | Default | Meaning |
|---|---|---|
| `CASSETTA_JWT_KEY_SECONDARY` | unset | Base64-encoded verify-only second key. Accepted for verification, never used to sign. |
| `CASSETTA_JWT_KEY_SECONDARY_FILE` | unset | Path form of `CASSETTA_JWT_KEY_SECONDARY`. Wins when both are set. |
| `CASSETTA_JWT_KEY_OVERLAP_TTL` | `600` | Seconds a just-demoted key stays valid after a `SIGHUP` rotation, so credentials issued moments before the rotation keep verifying. Must be greater than zero. |

**Minimum key length is 32 bytes (decoded).** The server validates this
at startup and aborts with a clear error if the key is shorter; HS256
rejects shorter keys anyway.

Generate a key with OpenSSL:

```bash
mkdir -p ~/.config/cassetta
openssl rand -base64 32 > ~/.config/cassetta/jwt.key
chmod 600 ~/.config/cassetta/jwt.key

export CASSETTA_JWT_KEY_FILE=$HOME/.config/cassetta/jwt.key
```

Set `CASSETTA_JWT_KEY_OVERLAP_TTL` to at least
`max(CASSETTA_DOWNLOAD_CLAIM_TTL, CASSETTA_UPLOAD_TOKEN_TTL)`. Below
that, the server still starts but logs a `config_validation_warning` at
boot, and clients can see spurious `401`s mid-transfer after a rotation.

### Key rotation procedure

Credential lifetimes are short (5 minutes by default,
`CASSETTA_UPLOAD_TOKEN_TTL`), so rotation is zero-downtime as long as
both keys are accepted during the overlap window:

1. Generate the new key (call it **B**). Keep the current key (**A**)
   reachable on the host.
2. Deploy with **primary = B** and **secondary = A**:

   ```bash
   export CASSETTA_JWT_KEY_SECONDARY_FILE=$HOME/.config/cassetta/jwt.key   # old A
   export CASSETTA_JWT_KEY_FILE=$HOME/.config/cassetta/jwt.new.key         # new B
   # restart cassetta — new credentials sign with B; in-flight A-signed ones still verify.
   ```

3. Wait at least `CASSETTA_UPLOAD_TOKEN_TTL` seconds (300 by default) so
   every credential signed by A has expired.
4. Drop the secondary and restart. Only B is accepted from then on.

A rotation in progress is visible on the wire: the `config_loaded`
startup line reports `secondary_key` as configured or null. Failed
verifications surface as `jwt_validation_failed` log entries.

## Storage

| Env var | Default | Meaning |
|---|---|---|
| `CASSETTA_STORAGE_PATH` | `./data` | Root of the storage tree. Inside the container image this is `/data`, which is the mount point `docker-compose.yml` uses. |
| `CASSETTA_KEYS_FILE` | `<storage path>.keys/.cassetta-keys.json` | Where the API-key store lives. The default is a **sibling** of the storage root, not a child, so key material never appears in a `/files/` listing. Set this for a custom layout. |
| `CASSETTA_DEFAULT_TTL` | `0` | Default file lifetime in seconds. `0` disables expiry: nothing is ever reported expired and the cleanup sweep does no work. Must not be negative. |
| `CASSETTA_ALLOWED_PATH_CHARS` | `a-zA-Z0-9\-_./` | Regular-expression character class a stored path may use. Checked on every route and tool that accepts a path, in addition to the unconditional rejection of absolute paths and `..` segments. |

## Identity

| Env var | Default | Meaning |
|---|---|---|
| `CASSETTA_INVITE_TTL_SECONDS` | `604800` (7 days) | The lifetime this library validates and carries for invite tokens. Parsed and range-checked at startup — a non-integer or non-positive value stops the server — and then held for whatever invite implementation is attached to it. This is an extension point: no invite implementation ships here, so a value set here is validated and held rather than used. |

## Limits policy

Parsed into a `LimitsConfig` that parameterises the default limits
policy. A negative cap, or a TTL that is not a positive integer, exits
at startup with status 1.

### Cap fields

| Env var | Default | Meaning |
|---|---|---|
| `CASSETTA_PER_FILE_MAX` | unset | Maximum bytes for any single file inside a bundle. |
| `CASSETTA_PER_BUNDLE_TOTAL_MAX` | unset | Maximum sum of file sizes in a bundle. |
| `CASSETTA_PER_BUNDLE_FILE_COUNT_MAX` | `25` | Maximum number of files per bundle. |
| `CASSETTA_MAX_INLINE_SIZE` | `102400` (100 KiB) | Payloads whose total size is at or below this are accepted inline; larger ones require the batch upload flow. |

For all four, the **empty string means "unset"** — no cap, unlimited
count, always inline. That is deliberate: configuration-management tools
that cannot distinguish an absent variable from an empty one can still
clear a cap explicitly.

### TTL fields

All are integer seconds and all must be greater than zero.

| Env var | Default | Meaning |
|---|---|---|
| `CASSETTA_UPLOAD_TOKEN_TTL` | `300` | Lifespan of an upload credential once issued. |
| `CASSETTA_DOWNLOAD_CLAIM_TTL` | `300` | Lifespan of a download claim. |
| `CASSETTA_PASSIVE_GC_MIN_AGE` | `3600` | Minimum age before the passive sweep treats a bundle as an orphan. |
| `CASSETTA_PASSIVE_GC_INTERVAL` | `600` | Interval between passive sweeps. |

File expiry is separate from these and keys off `CASSETTA_DEFAULT_TTL`
in the [Storage](#storage) section.

The limits policy is distinct from the access policy, which decides
whether an identity may read, write, or peek at a bundle. There is no
environment variable for it: the built-in policy allows every action,
which is the right answer for single-user self-hosting.

### Retired

| Env var | Handling |
|---|---|
| `CASSETTA_MAX_FILE_SIZE` | **Retired.** The value is ignored; setting it prints one warning to stderr at startup. Use `CASSETTA_PER_FILE_MAX`. |

## Rate limiting and fan-out

| Env var | Default | Meaning |
|---|---|---|
| `CASSETTA_RATE_LIMIT_ONBOARD` | `5/minute` | Budget for the onboarding endpoint. Format is `<int>/<unit>`, where unit is one of `sec`, `second`, `min`, `minute`, `hour`, `hourly`; short forms are normalised to long ones. A malformed value exits at startup. |
| `CASSETTA_RATE_LIMIT_BROADCAST` | `10/minute` | Budget **shared** across the REST `/broadcast` route and the `cassetta_broadcast` tool — one bucket, not one each. Same format. |
| `CASSETTA_BROADCAST_MAX_TARGETS` | `1000` | Fan-out cap. A broadcast addressed to more recipients than this is rejected before any storage write. Must be greater than zero. |

**Behind a reverse proxy, tell the ASGI server about it.** The budgets above are
per-client, and the client is whichever address the ASGI server attributes the
request to. Give uvicorn your proxy's address list as `--forwarded-allow-ips`;
without it uvicorn ignores `X-Forwarded-For` and attributes every request to the
proxy, which collapses the per-client budgets into a single global one. Cassetta
itself needs no configuration for this — the setting belongs to the server you run
it under, and matters only when something sits in front of it.

## MCP and logging

| Env var | Default | Meaning |
|---|---|---|
| `CASSETTA_MCP_ALLOWED_HOSTS` | empty | Comma-separated allowlist of `Host` header values accepted at the MCP endpoint — DNS-rebinding protection. Empty means localhost only; set your external hostnames to reach MCP from another machine. |
| `CASSETTA_LOG_FORMAT` | `text` | `text` or `json`. Any other value falls back to `text` with a warning on stderr rather than failing the boot. Every structured-log event renders in the chosen format. |

## Command-line client

`cassetta send` resolves its configuration from `--url` and `--api-key`
first, then from these. Missing either one, with no flag, fails before
any network call.

| Env var | Default | Meaning |
|---|---|---|
| `CASSETTA_URL` | unset | Server base URL, used when `--url` is absent. |
| `CASSETTA_API_KEY` | unset | Bearer API key, used when `--api-key` is absent. |
