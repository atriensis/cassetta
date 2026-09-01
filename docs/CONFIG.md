# Cassetta configuration reference

All Cassetta configuration is read from environment variables at
startup. This document enumerates each variable, its default, and its
effect.

Sections:

- [Storage](#storage)
- [Authentication](#authentication)
- [Discovery & aliases](#discovery--aliases)
- [Limits policy](#limits-policy)
- [Upload flow (brief 514)](#upload-flow-brief-514)
- [Logging & observability](#logging--observability)
- [MCP](#mcp)

> This file is populated alongside brief 513. Sections not directly
> touched by 513/514 are tracked elsewhere for now (see `core/README.md`)
> and will be folded in as briefs refresh them.

## Upload flow (brief 514)

Brief 514 introduced a two-phase upload flow (`cassetta_send_init` →
`cassetta_send_inline` / `POST /upload/{bundle_path}`) backed by HS256
JWT credentials. Three environment variables below are required for
the server to boot; two more are optional and enable verify-only key
rotation.

### JWT signing keys

The server signs each upload credential with the **primary** key. Each
credential is accepted for verification if its signature matches
either the primary or, if configured, the **secondary** key. The
secondary key is verify-only and never used to sign.

| Env var | Required | Meaning |
|---|---|---|
| `CASSETTA_JWT_KEY` | primary required if `..._FILE` unset | Raw base64-encoded primary signing key value. |
| `CASSETTA_JWT_KEY_FILE` | primary required if `...` unset | Path to a file whose contents are the base64-encoded primary key. `_FILE` wins if both are set. |
| `CASSETTA_JWT_KEY_SECONDARY` | no | Raw base64-encoded verify-only secondary key (for rotation). |
| `CASSETTA_JWT_KEY_SECONDARY_FILE` | no | Path variant of `CASSETTA_JWT_KEY_SECONDARY`. |

**Minimum key length is 32 bytes (decoded).** The server validates
this at startup and aborts with a clear error if the key is shorter;
HS256 rejects shorter keys anyway (pyjwt's
`enforce_minimum_key_length` constraint).

Generate a key with OpenSSL:

```bash
mkdir -p ~/.config/cassetta
openssl rand -base64 32 > ~/.config/cassetta/jwt.key
chmod 600 ~/.config/cassetta/jwt.key

export CASSETTA_JWT_KEY_FILE=$HOME/.config/cassetta/jwt.key
```

#### Key rotation procedure

The credential TTL is short (default 5 minutes, `CASSETTA_UPLOAD_TOKEN_TTL`),
so rotation is zero-downtime as long as both keys are accepted during
the overlap window:

1. Generate the new key (call it **B**). Keep the current key (**A**)
   reachable on the host.
2. Deploy with **primary = B** and **secondary = A**:

   ```bash
   export CASSETTA_JWT_KEY_SECONDARY_FILE=$HOME/.config/cassetta/jwt.key       # old A
   export CASSETTA_JWT_KEY_FILE=$HOME/.config/cassetta/jwt.new.key            # new B
   # restart cassetta — new inits sign with B; in-flight A-signed tokens still verify.
   ```
3. Wait at least `CASSETTA_UPLOAD_TOKEN_TTL` seconds (default 300 s)
   so every credential signed by A has expired.
4. Drop the secondary:

   ```bash
   unset CASSETTA_JWT_KEY_SECONDARY_FILE
   rm ~/.config/cassetta/jwt.key
   # restart cassetta — only B is accepted now.
   ```

On the wire, a rotation in progress is detected by the `config_loaded`
startup log line (`secondary_key: "configured"` vs `null`). Failed
verifications surface as `jwt_validation_failed` structured log
entries.

### Public base URL

The batch-mode `upload_url` returned by `cassetta_send_init` is
composed from this variable, so it must be set to the absolute URL
that agents can reach (not the internal bind address, if those
differ).

| Env var | Required | Meaning |
|---|---|---|
| `CASSETTA_PUBLIC_BASE_URL` | **yes, unconditionally** | Absolute base URL (scheme mandatory). Trailing slash optional. |

The server validates the shape at startup: `scheme` must be `http` or
`https` and `netloc` must be non-empty. Examples:

```bash
# Local dev
export CASSETTA_PUBLIC_BASE_URL=http://localhost:16001

# Pi / home-lab production
export CASSETTA_PUBLIC_BASE_URL=https://cassetta.home.arpa
```

If unset or malformed, the server aborts boot with:

```
ERROR: CASSETTA_PUBLIC_BASE_URL is required (http://... or https://...).
       Example (Pi):    CASSETTA_PUBLIC_BASE_URL=https://cassetta.home.arpa
       Example (local): CASSETTA_PUBLIC_BASE_URL=http://localhost:16001
```

### TTL (overlaps with limits policy)

`CASSETTA_UPLOAD_TOKEN_TTL` (default 300 s, documented in the limits
section below) controls how long a credential stays valid. It is also
the minimum wait during rotation step 3 above.


## Limits policy

Introduced by brief 513. Environment variables are parsed by
`load_limits_config()` in `core/src/cassetta/config.py` into a
`LimitsConfig` dataclass that parameterises `CoreLimitsPolicy` (the
default `LimitsPolicy` implementation).

See `specs/513-peek-and-limits-policy/contracts/core-limits-policy.md`
for the semantic contract.

### Cap fields

| Env var | Default | Purpose |
|---|---|---|
| `CASSETTA_PER_FILE_MAX` | unset | Maximum bytes for any single file inside a bundle. Empty string → unset (no cap). |
| `CASSETTA_PER_BUNDLE_TOTAL_MAX` | unset | Maximum sum of file sizes in a bundle. Empty string → unset. |
| `CASSETTA_PER_BUNDLE_FILE_COUNT_MAX` | `25` | Maximum number of files per bundle. Empty string → unset (unlimited). |
| `CASSETTA_MAX_INLINE_SIZE` | `102400` (100 KiB) | Payloads whose total size is `<= max_inline_size` are accepted inline; larger payloads require the batch transport (brief 514). Empty string → unset → always inline. |

For caps, the **"empty string = unset"** idiom lets operators
explicitly clear an unset cap via configuration management tools that
cannot distinguish absent variables from empty ones. A negative value
fails fast at startup (`SystemExit(1)`).

### TTL fields

All TTL values are integers measured in seconds; all must be `> 0`.

| Env var | Default | Purpose |
|---|---|---|
| `CASSETTA_UPLOAD_TOKEN_TTL` | `300` | Lifespan of an upload token once issued. (Consumed by brief 514.) |
| `CASSETTA_DOWNLOAD_CLAIM_TTL` | `300` | Lifespan of a download claim. (Consumed by brief 515.) |
| `CASSETTA_PASSIVE_GC_MIN_AGE` | `3600` | Minimum age before the passive GC sweep considers an orphan. |
| `CASSETTA_PASSIVE_GC_INTERVAL` | `600` | Interval between passive GC sweeps. |

Brief 513 plumbs these fields through to `CoreLimitsPolicy.ttls()` so
downstream briefs can read them without re-parsing env vars; the
active TTL cleanup loop continues to key off `CASSETTA_DEFAULT_TTL`
in this release.

### Deprecated

| Env var | Handling |
|---|---|
| `CASSETTA_MAX_FILE_SIZE` | **Retired in brief 513.** The value is ignored; setting it triggers a one-line stderr warning at startup. Migrate to `CASSETTA_PER_FILE_MAX`. |

### Access action verb

`LimitsPolicy` is distinct from `AccessPolicy`, but brief 513 also
introduces a new `"peek"` access action verb used by the
`cassetta_peek` MCP tool and the two `/peek` REST endpoints. The
default access policy allows it unconditionally. See
`specs/504-rbac-invites/data-model.md` §"Resource → action matrix"
for the shape of the action taxonomy in custom policies.
