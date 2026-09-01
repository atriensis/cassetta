import pytest

_TEST_JWT_KEY_B64 = "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"


def _seed_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the bare-minimum env that ``load_config`` requires to boot.

    Brief 539: also clears ``CASSETTA_DEFAULT_TTL`` and the cap vars so a value
    leaked by another test (historically ``test_negative_ttl_raises`` leaked
    ``DEFAULT_TTL=-1``) cannot bleed into this load and trip a SystemExit or a
    wrong default. Every test below seeds via this helper, then sets only the
    var it exercises — and ``monkeypatch`` restores all of them at teardown, so
    this file neither free-rides on nor leaks any ``CASSETTA_*`` value.
    """
    monkeypatch.setenv("CASSETTA_SETUP_TOKEN", "")
    monkeypatch.setenv("CASSETTA_JWT_KEY", _TEST_JWT_KEY_B64)
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", "http://localhost:16001")
    for var in (
        "CASSETTA_JWT_KEY_FILE", "CASSETTA_JWT_KEY_SECONDARY",
        "CASSETTA_JWT_KEY_SECONDARY_FILE", "CASSETTA_KEYS_FILE",
        "CASSETTA_DEFAULT_TTL", "CASSETTA_STORAGE_PATH",
        "CASSETTA_MAX_FILE_SIZE", "CASSETTA_PER_FILE_MAX",
        "CASSETTA_PER_BUNDLE_TOTAL_MAX", "CASSETTA_PER_BUNDLE_FILE_COUNT_MAX",
        "CASSETTA_MAX_INLINE_SIZE", "CASSETTA_MCP_ALLOWED_HOSTS",
    ):
        monkeypatch.delenv(var, raising=False)


class TestConfigValidation:
    def test_missing_setup_token_raises(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_required_env(monkeypatch)
        monkeypatch.delenv("CASSETTA_SETUP_TOKEN", raising=False)
        from cassetta.config import load_config

        with pytest.raises(SystemExit):
            load_config()

    def test_empty_setup_token_enables_dev_mode(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_required_env(monkeypatch)
        monkeypatch.setenv("CASSETTA_STORAGE_PATH", "/tmp/test")
        from cassetta.config import load_config

        config = load_config()
        assert config.dev_mode is True

    def test_nonempty_setup_token_disables_dev_mode(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_required_env(monkeypatch)
        monkeypatch.setenv("CASSETTA_SETUP_TOKEN", "my-token")
        monkeypatch.setenv("CASSETTA_STORAGE_PATH", "/tmp/test")
        from cassetta.config import load_config

        config = load_config()
        assert config.dev_mode is False
        assert config.setup_token == "my-token"

    def test_negative_ttl_raises(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_required_env(monkeypatch)
        monkeypatch.setenv("CASSETTA_DEFAULT_TTL", "-1")
        from cassetta.config import load_config

        with pytest.raises(SystemExit):
            load_config()

    def test_default_values(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_required_env(monkeypatch)
        from cassetta.config import load_config

        config = load_config()
        assert config.storage_path == "./data"
        assert config.default_ttl == 0
        # LimitsConfig defaults live in docs/CONFIG.md (brief 513).
        assert config.limits.per_file_max is None
        assert config.limits.max_inline_size == 102400

    def test_custom_values(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_required_env(monkeypatch)
        monkeypatch.setenv("CASSETTA_SETUP_TOKEN", "tok")
        monkeypatch.setenv("CASSETTA_STORAGE_PATH", "/custom/path")
        monkeypatch.setenv("CASSETTA_DEFAULT_TTL", "3600")
        monkeypatch.setenv("CASSETTA_PER_FILE_MAX", "5242880")
        from cassetta.config import load_config

        config = load_config()
        assert config.storage_path == "/custom/path"
        assert config.default_ttl == 3600
        assert config.limits.per_file_max == 5242880

    def test_mcp_allowed_hosts_default_empty(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_required_env(monkeypatch)
        from cassetta.config import load_config

        config = load_config()
        assert config.mcp_allowed_hosts == ()

    def test_mcp_allowed_hosts_parses_csv(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_required_env(monkeypatch)
        monkeypatch.setenv(
            "CASSETTA_MCP_ALLOWED_HOSTS", "pi.host, pi.host:16001 ,foo.example",
        )
        from cassetta.config import load_config

        config = load_config()
        assert config.mcp_allowed_hosts == ("pi.host", "pi.host:16001", "foo.example")

    def test_keys_file_defaults_outside_storage(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_required_env(monkeypatch)
        monkeypatch.setenv("CASSETTA_STORAGE_PATH", "/var/lib/cassetta/data")
        from cassetta.config import load_config

        config = load_config()
        # Keys file is in a sibling directory, never inside storage_path
        assert config.keys_file == "/var/lib/cassetta/data.keys/.cassetta-keys.json"
        assert not config.keys_file.startswith(config.storage_path + "/")

    def test_keys_file_explicit_override(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_required_env(monkeypatch)
        monkeypatch.setenv("CASSETTA_STORAGE_PATH", "/var/lib/cassetta/data")
        monkeypatch.setenv("CASSETTA_KEYS_FILE", "/etc/cassetta/keys.json")
        from cassetta.config import load_config

        config = load_config()
        assert config.keys_file == "/etc/cassetta/keys.json"


class TestBrief531EnvVars:
    """Brief 531 — operational-resilience env-var parsing."""

    def test_defaults_applied_when_unset(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_required_env(monkeypatch)
        for var in (
            "CASSETTA_RATE_LIMIT_ONBOARD",
            "CASSETTA_RATE_LIMIT_BROADCAST",
            "CASSETTA_BROADCAST_MAX_TARGETS",
            "CASSETTA_TRUSTED_PROXIES",
            "CASSETTA_JWT_KEY_OVERLAP_TTL",
        ):
            monkeypatch.delenv(var, raising=False)
        from cassetta.config import load_config

        config = load_config()
        assert config.rate_limit_onboard == "5/minute"
        assert config.rate_limit_broadcast == "10/minute"
        assert config.broadcast_max_targets == 1000
        assert config.trusted_proxies == ""
        assert config.jwt_key_overlap_ttl == 600

    def test_valid_values_plumbed_through(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_required_env(monkeypatch)
        monkeypatch.setenv("CASSETTA_RATE_LIMIT_ONBOARD", "20/hour")
        monkeypatch.setenv("CASSETTA_RATE_LIMIT_BROADCAST", "100/minute")
        monkeypatch.setenv("CASSETTA_BROADCAST_MAX_TARGETS", "50")
        monkeypatch.setenv(
            "CASSETTA_TRUSTED_PROXIES", "10.0.0.0/8,192.168.0.0/16",
        )
        monkeypatch.setenv("CASSETTA_JWT_KEY_OVERLAP_TTL", "1200")
        from cassetta.config import load_config

        config = load_config()
        assert config.rate_limit_onboard == "20/hour"
        assert config.rate_limit_broadcast == "100/minute"
        assert config.broadcast_max_targets == 50
        assert config.trusted_proxies == "10.0.0.0/8,192.168.0.0/16"
        assert config.jwt_key_overlap_ttl == 1200

    def test_rate_limit_short_units_normalised(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_required_env(monkeypatch)
        monkeypatch.setenv("CASSETTA_RATE_LIMIT_ONBOARD", "5/min")
        monkeypatch.setenv("CASSETTA_RATE_LIMIT_BROADCAST", "10/sec")
        from cassetta.config import load_config

        config = load_config()
        assert config.rate_limit_onboard == "5/minute"
        assert config.rate_limit_broadcast == "10/second"

    def test_malformed_rate_limit_onboard_exits(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_required_env(monkeypatch)
        monkeypatch.setenv("CASSETTA_RATE_LIMIT_ONBOARD", "not-a-rate")
        from cassetta.config import load_config

        with pytest.raises(SystemExit):
            load_config()

    def test_malformed_rate_limit_broadcast_exits(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_required_env(monkeypatch)
        monkeypatch.setenv("CASSETTA_RATE_LIMIT_BROADCAST", "abc/minute")
        from cassetta.config import load_config

        with pytest.raises(SystemExit):
            load_config()

    def test_broadcast_max_targets_zero_exits(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_required_env(monkeypatch)
        monkeypatch.setenv("CASSETTA_BROADCAST_MAX_TARGETS", "0")
        from cassetta.config import load_config

        with pytest.raises(SystemExit):
            load_config()

    def test_broadcast_max_targets_negative_exits(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_required_env(monkeypatch)
        monkeypatch.setenv("CASSETTA_BROADCAST_MAX_TARGETS", "-5")
        from cassetta.config import load_config

        with pytest.raises(SystemExit):
            load_config()

    def test_jwt_key_overlap_ttl_non_integer_exits(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_required_env(monkeypatch)
        monkeypatch.setenv("CASSETTA_JWT_KEY_OVERLAP_TTL", "abc")
        from cassetta.config import load_config

        with pytest.raises(SystemExit):
            load_config()

    def test_jwt_key_overlap_ttl_zero_exits(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_required_env(monkeypatch)
        monkeypatch.setenv("CASSETTA_JWT_KEY_OVERLAP_TTL", "0")
        from cassetta.config import load_config

        with pytest.raises(SystemExit):
            load_config()
