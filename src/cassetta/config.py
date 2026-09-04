import base64
import os
import re
import sys
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_MIN_HS256_KEY_BYTES = 32

# slowapi-compatible rate string ("<int>/<unit>"). Accepts
# the short forms ("sec", "min", "hour") and normalises them to the
# canonical long forms ("second", "minute", "hour"). slowapi's
# ``limits`` parser accepts both, but normalising up-front keeps the
# ``config_loaded`` event readable for operators.
_RATE_LIMIT_PATTERN = re.compile(
    r"^([1-9]\d*)/(sec|second|min|minute|hour|hourly)$",
    re.IGNORECASE,
)
_RATE_UNIT_NORMALISE = {
    "sec": "second",
    "second": "second",
    "min": "minute",
    "minute": "minute",
    "hour": "hour",
    "hourly": "hour",
}


@dataclass(frozen=True)
class LimitsConfig:
    """Configuration for :class:`CoreLimitsPolicy`.

    See ``docs/CONFIG.md`` for the env-var mapping and the
    semantic meaning of each field. Loaded via :func:`load_limits_config`.
    """

    per_file_max: int | None = None
    per_bundle_total_max: int | None = None
    per_bundle_file_count_max: int | None = 25
    max_inline_size: int | None = 100 * 1024
    upload_token_ttl: int = 300
    download_claim_ttl: int = 300
    passive_gc_min_age: int = 3600
    passive_gc_interval: int = 600


_CAP_ENV_VARS: tuple[tuple[str, str], ...] = (
    ("CASSETTA_PER_FILE_MAX", "per_file_max"),
    ("CASSETTA_PER_BUNDLE_TOTAL_MAX", "per_bundle_total_max"),
    ("CASSETTA_PER_BUNDLE_FILE_COUNT_MAX", "per_bundle_file_count_max"),
    ("CASSETTA_MAX_INLINE_SIZE", "max_inline_size"),
)

_TTL_ENV_VARS: tuple[tuple[str, str], ...] = (
    ("CASSETTA_UPLOAD_TOKEN_TTL", "upload_token_ttl"),
    ("CASSETTA_DOWNLOAD_CLAIM_TTL", "download_claim_ttl"),
    ("CASSETTA_PASSIVE_GC_MIN_AGE", "passive_gc_min_age"),
    ("CASSETTA_PASSIVE_GC_INTERVAL", "passive_gc_interval"),
)


def _parse_cap(env_var: str, raw: str) -> int | None:
    if raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        print(
            f"ERROR: {env_var} must be an integer, got '{raw}'",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    if value < 0:
        print(
            f"ERROR: {env_var} must be >= 0 (or empty to unset), got {value}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return value


def _parse_ttl(env_var: str, raw: str, default: int) -> int:
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        print(
            f"ERROR: {env_var} must be an integer, got '{raw}'",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    if value <= 0:
        print(
            f"ERROR: {env_var} must be > 0, got {value}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return value


def load_limits_config() -> LimitsConfig:
    """Load :class:`LimitsConfig` from environment variables.

    Exits with status 1 if any variable parses to a negative/invalid
    integer or (for TTLs) to a non-positive value. Emits a deprecation
    warning to stderr if the retired ``CASSETTA_MAX_FILE_SIZE`` is set.
    """
    if "CASSETTA_MAX_FILE_SIZE" in os.environ:
        print(
            "WARNING: CASSETTA_MAX_FILE_SIZE is deprecated; use CASSETTA_PER_FILE_MAX. The stale value is ignored.",
            file=sys.stderr,
        )

    defaults = LimitsConfig()
    kwargs: dict[str, int | None] = {}
    for env_var, field_name in _CAP_ENV_VARS:
        if env_var in os.environ:
            kwargs[field_name] = _parse_cap(env_var, os.environ[env_var])
    for env_var, field_name in _TTL_ENV_VARS:
        if env_var in os.environ:
            kwargs[field_name] = _parse_ttl(
                env_var,
                os.environ[env_var],
                getattr(defaults, field_name),
            )
    return LimitsConfig(**kwargs)  # type: ignore[arg-type]


@dataclass(frozen=True)
class AppConfig:
    setup_token: str
    dev_mode: bool
    storage_path: str
    keys_file: str
    default_ttl: int
    allowed_path_chars: str
    mcp_allowed_hosts: tuple[str, ...]
    jwt_primary_key: bytes = b""
    public_base_url: str = ""
    jwt_secondary_key: bytes | None = None
    jwt_primary_key_source: str = "env"
    invite_ttl_seconds: int = 604800
    log_format: str = "text"
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    # Operational-resilience env vars.
    rate_limit_onboard: str = "5/minute"
    rate_limit_broadcast: str = "10/minute"
    broadcast_max_targets: int = 1000
    jwt_key_overlap_ttl: int = 600


def _parse_rate_limit(env_var: str, raw: str) -> str:
    """Validate a slowapi rate string and normalise short units to long forms."""
    match = _RATE_LIMIT_PATTERN.match(raw.strip())
    if match is None:
        print(
            f"ERROR: {env_var} must match '<int>/<unit>' where unit ∈ sec/second/min/minute/hour/hourly, got '{raw}'",
            file=sys.stderr,
        )
        raise SystemExit(1)
    count, unit = match.group(1), match.group(2).lower()
    return f"{count}/{_RATE_UNIT_NORMALISE[unit]}"


def _parse_positive_int(env_var: str, raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        print(
            f"ERROR: {env_var} must be an integer, got '{raw}'",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    if value <= 0:
        print(
            f"ERROR: {env_var} must be > 0, got {value}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return value


def _decode_jwt_key(raw: str, var_name: str) -> bytes:
    """Base64-decode a JWT key string; exit on malformed input or short key."""
    stripped = raw.strip()
    try:
        decoded = base64.b64decode(stripped, validate=True)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        print(
            f"ERROR: {var_name} must be base64-encoded bytes, got '{stripped[:12]}...': {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    if len(decoded) < _MIN_HS256_KEY_BYTES:
        print(
            f"ERROR: {var_name} decodes to {len(decoded)} bytes; "
            f"HS256 requires >= {_MIN_HS256_KEY_BYTES} bytes of key material.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return decoded


def _load_jwt_key_pair(
    *,
    value_var: str,
    file_var: str,
    required: bool,
) -> tuple[bytes | None, str | None]:
    """Load a JWT key from (``value_var``, ``file_var``) — file wins over value.

    Returns the decoded key bytes and a source label (``"file"`` or ``"env"``).
    On missing (when ``required``) or malformed, exits with a clear message.
    """
    file_path = os.environ.get(file_var, "").strip()
    if file_path:
        try:
            raw = open(file_path).read().strip()
        except OSError as exc:
            print(
                f"ERROR: {file_var}={file_path!r} could not be read: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        if not raw:
            print(
                f"ERROR: {file_var}={file_path!r} is empty.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return _decode_jwt_key(raw, file_var), "file"
    env_value = os.environ.get(value_var, "").strip()
    if env_value:
        return _decode_jwt_key(env_value, value_var), "env"
    if required:
        print(
            f"ERROR: No primary signing key configured. "
            f"Set {value_var} (raw base64 value) or {file_var} (path to key file).",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return None, None


def _load_public_base_url(var_name: str) -> str:
    raw = os.environ.get(var_name, "").strip()
    if not raw:
        print(
            f"ERROR: {var_name} is required (http://... or https://...).\n"
            f"       Example (remote): {var_name}=https://cassetta.example.com\n"
            f"       Example (local): {var_name}=http://localhost:16001",
            file=sys.stderr,
        )
        raise SystemExit(1)
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        print(
            f"ERROR: {var_name} must include an http:// or https:// scheme and a non-empty host, got {raw!r}.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    # Normalise: strip trailing slash (single or many) before composing upload_url.
    return raw.rstrip("/")


def load_config() -> AppConfig:
    """Load configuration from environment variables.

    Exits if CASSETTA_SETUP_TOKEN is not set or CASSETTA_DEFAULT_TTL is negative.
    """
    setup_token_raw = os.environ.get("CASSETTA_SETUP_TOKEN")
    if setup_token_raw is None:
        print(
            "ERROR: CASSETTA_SETUP_TOKEN is not set. Set it to a secret value, or to an empty string for dev mode.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    storage_path = os.environ.get("CASSETTA_STORAGE_PATH", "./data")
    # Keys file lives OUTSIDE storage_path by default so list() never sees it.
    # Default location: sibling directory "<storage>.keys/.cassetta-keys.json".
    # Operators can override via CASSETTA_KEYS_FILE for non-default layouts.
    keys_file = os.environ.get(
        "CASSETTA_KEYS_FILE",
        os.path.join(storage_path.rstrip("/") + ".keys", ".cassetta-keys.json"),
    )

    ttl_str = os.environ.get("CASSETTA_DEFAULT_TTL", "0")
    try:
        default_ttl = int(ttl_str)
    except ValueError:
        print(
            f"ERROR: CASSETTA_DEFAULT_TTL must be an integer, got '{ttl_str}'",
            file=sys.stderr,
        )
        raise SystemExit(1) from None

    if default_ttl < 0:
        print(
            f"ERROR: CASSETTA_DEFAULT_TTL must be >= 0, got {default_ttl}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    limits = load_limits_config()

    allowed_path_chars = os.environ.get(
        "CASSETTA_ALLOWED_PATH_CHARS",
        r"a-zA-Z0-9\-_./",
    )

    # MCP DNS-rebinding protection: comma-separated list of allowed Host
    # header values. Empty (default) means localhost only — set this when
    # the server is reachable from non-loopback hostnames.
    mcp_hosts_raw = os.environ.get("CASSETTA_MCP_ALLOWED_HOSTS", "")
    mcp_allowed_hosts = tuple(h.strip() for h in mcp_hosts_raw.split(",") if h.strip())

    log_format_raw = os.environ.get("CASSETTA_LOG_FORMAT", "text").lower()
    if log_format_raw not in ("text", "json"):
        print(
            f"WARNING: CASSETTA_LOG_FORMAT must be 'text' or 'json', got '{log_format_raw}' — falling back to 'text'",
            file=sys.stderr,
        )
        log_format_raw = "text"

    invite_ttl_str = os.environ.get("CASSETTA_INVITE_TTL_SECONDS", "604800")
    try:
        invite_ttl_seconds = int(invite_ttl_str)
    except ValueError:
        print(
            f"ERROR: CASSETTA_INVITE_TTL_SECONDS must be an integer, got '{invite_ttl_str}'",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    if invite_ttl_seconds <= 0:
        print(
            f"ERROR: CASSETTA_INVITE_TTL_SECONDS must be > 0, got {invite_ttl_seconds}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    primary_key, primary_source = _load_jwt_key_pair(
        value_var="CASSETTA_JWT_KEY",
        file_var="CASSETTA_JWT_KEY_FILE",
        required=True,
    )
    secondary_key, _ = _load_jwt_key_pair(
        value_var="CASSETTA_JWT_KEY_SECONDARY",
        file_var="CASSETTA_JWT_KEY_SECONDARY_FILE",
        required=False,
    )
    public_base_url = _load_public_base_url("CASSETTA_PUBLIC_BASE_URL")

    assert primary_key is not None  # required=True guarantees non-None
    assert primary_source is not None

    # Operational-resilience env vars.
    rate_limit_onboard = _parse_rate_limit(
        "CASSETTA_RATE_LIMIT_ONBOARD",
        os.environ.get("CASSETTA_RATE_LIMIT_ONBOARD", "5/minute"),
    )
    rate_limit_broadcast = _parse_rate_limit(
        "CASSETTA_RATE_LIMIT_BROADCAST",
        os.environ.get("CASSETTA_RATE_LIMIT_BROADCAST", "10/minute"),
    )
    broadcast_max_targets = _parse_positive_int(
        "CASSETTA_BROADCAST_MAX_TARGETS",
        os.environ.get("CASSETTA_BROADCAST_MAX_TARGETS", "1000"),
    )
    jwt_key_overlap_ttl = _parse_positive_int(
        "CASSETTA_JWT_KEY_OVERLAP_TTL",
        os.environ.get("CASSETTA_JWT_KEY_OVERLAP_TTL", "600"),
    )

    return AppConfig(
        setup_token=setup_token_raw,
        dev_mode=setup_token_raw == "",
        storage_path=storage_path,
        keys_file=keys_file,
        invite_ttl_seconds=invite_ttl_seconds,
        default_ttl=default_ttl,
        allowed_path_chars=allowed_path_chars,
        mcp_allowed_hosts=mcp_allowed_hosts,
        jwt_primary_key=primary_key,
        jwt_secondary_key=secondary_key,
        jwt_primary_key_source=primary_source,
        public_base_url=public_base_url,
        log_format=log_format_raw,
        limits=limits,
        rate_limit_onboard=rate_limit_onboard,
        rate_limit_broadcast=rate_limit_broadcast,
        broadcast_max_targets=broadcast_max_targets,
        jwt_key_overlap_ttl=jwt_key_overlap_ttl,
    )
