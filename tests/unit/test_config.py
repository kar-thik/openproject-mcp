"""Settings: zero-env construction, env mapping, frozen env surface, runtime checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from openproject_mcp.config import Settings, check_runtime_config

#: Every environment variable the server reads, frozen at the first release.
#: Adding, renaming or removing a name is a compatibility decision — when you
#: make one deliberately, update this set, ``.env.example`` and the docs
#: together.
ENV_SURFACE = frozenset(
    {
        "OPENPROJECT_URL",
        "OPENPROJECT_API_KEY",
        "OPENPROJECT_OAUTH_TOKEN",
        "OPENPROJECT_MCP_ACCEPT_LANGUAGE",
        "OPENPROJECT_MCP_CA_BUNDLE",
        "OPENPROJECT_MCP_READ_ONLY",
        "OPENPROJECT_MCP_ADMIN_TOOLS",
        "OPENPROJECT_MCP_DISABLE",
        "OPENPROJECT_MCP_INSECURE",
        "OPENPROJECT_MCP_DOWNLOAD_DIR",
        "OPENPROJECT_MCP_MAX_DOWNLOAD_MB",
        "OPENPROJECT_MCP_CACHE_TTL",
        "OPENPROJECT_MCP_LOG_LEVEL",
        "OPENPROJECT_MCP_LOG_FORMAT",
        "OPENPROJECT_MCP_LOG_BODIES",
        "OPENPROJECT_MCP_OTEL",
        "OPENPROJECT_MCP_HTTP_HOST",
        "OPENPROJECT_MCP_HTTP_PORT",
        "OPENPROJECT_MCP_AUTH_TOKENS",
        "OPENPROJECT_MCP_CONNECT_TIMEOUT",
        "OPENPROJECT_MCP_READ_TIMEOUT",
        "OPENPROJECT_MCP_WRITE_TIMEOUT",
        "OPENPROJECT_MCP_POOL_TIMEOUT",
        "OPENPROJECT_MCP_MAX_CONNECTIONS",
        "OPENPROJECT_MCP_MAX_RETRIES",
    }
)


def test_settings_construct_with_zero_environment() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.url is None
    assert settings.api_key is None
    assert settings.read_only is False
    assert settings.cache_ttl == 300.0
    assert settings.max_download_mb == 100


def test_every_documented_env_var_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {
        "OPENPROJECT_URL": "https://op.example.com/",
        "OPENPROJECT_API_KEY": "secret",
        "OPENPROJECT_MCP_READ_ONLY": "1",
        "OPENPROJECT_MCP_ADMIN_TOOLS": "true",
        "OPENPROJECT_MCP_DISABLE": "meetings, news",
        "OPENPROJECT_MCP_DOWNLOAD_DIR": "/tmp/downloads",
        "OPENPROJECT_MCP_MAX_DOWNLOAD_MB": "42",
        "OPENPROJECT_MCP_CACHE_TTL": "60",
        "OPENPROJECT_MCP_CA_BUNDLE": "/etc/ssl/private.pem",
        "OPENPROJECT_MCP_INSECURE": "1",
        "OPENPROJECT_MCP_LOG_LEVEL": "debug",
        "OPENPROJECT_MCP_LOG_FORMAT": "JSON",
        "OPENPROJECT_MCP_LOG_BODIES": "1",
        "OPENPROJECT_MCP_OTEL": "1",
        "OPENPROJECT_MCP_ACCEPT_LANGUAGE": "vi",
        "OPENPROJECT_MCP_CONNECT_TIMEOUT": "1.5",
        "OPENPROJECT_MCP_READ_TIMEOUT": "2.5",
        "OPENPROJECT_MCP_WRITE_TIMEOUT": "3.5",
        "OPENPROJECT_MCP_POOL_TIMEOUT": "4.5",
        "OPENPROJECT_MCP_MAX_CONNECTIONS": "7",
        "OPENPROJECT_MCP_MAX_RETRIES": "5",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.url == "https://op.example.com"
    assert settings.api_key is not None and settings.api_key.get_secret_value() == "secret"
    assert settings.read_only and settings.admin_tools and settings.insecure
    assert settings.disabled_groups == {"meetings", "news"}
    assert str(settings.download_dir) == "/tmp/downloads"
    assert settings.max_download_mb == 42
    assert settings.max_download_bytes == 42 * 1024 * 1024
    assert settings.cache_ttl == 60
    assert str(settings.ca_bundle) == "/etc/ssl/private.pem"
    assert settings.log_level == "DEBUG"
    assert settings.log_format == "json"
    assert settings.log_bodies and settings.otel
    assert settings.accept_language == "vi"
    assert settings.connect_timeout == 1.5
    assert settings.read_timeout == 2.5
    assert settings.write_timeout == 3.5
    assert settings.pool_timeout == 4.5
    assert settings.max_connections == 7
    assert settings.max_retries == 5


def test_env_surface_is_frozen() -> None:
    """Every field maps to exactly one documented env var, and nothing else."""
    surface: set[str] = set()
    for name, field in Settings.model_fields.items():
        alias = field.validation_alias
        assert isinstance(alias, str), f"field {name!r} must declare a string validation alias"
        surface.add(alias)
    assert len(surface) == len(Settings.model_fields)
    assert surface == ENV_SURFACE


def test_unprefixed_env_names_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray unprefixed name in the shell must never reach (or crash) a field."""
    monkeypatch.delenv("OPENPROJECT_MCP_READ_TIMEOUT", raising=False)
    monkeypatch.setenv("READ_TIMEOUT", "not-a-duration")
    monkeypatch.setenv("MAX_RETRIES", "not-a-number")
    monkeypatch.setenv("API_KEY", "not-mine")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.read_timeout == 30.0
    assert settings.max_retries == 3
    assert settings.api_key is None


def test_prefixed_timeout_env_names_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENPROJECT_MCP_READ_TIMEOUT", "12.5")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.read_timeout == 12.5


def test_dotenv_file_also_ignores_unprefixed_names(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("READ_TIMEOUT=not-a-duration\nOPENPROJECT_MCP_READ_TIMEOUT=12.5\n")
    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert settings.read_timeout == 12.5


def test_dotenv_unprefixed_names_are_ignored_even_without_the_alias(tmp_path: Path) -> None:
    """A bare field name alone in ``.env`` must not populate (or crash) a field.

    Without the alias present, the dotenv source classifies these keys as
    *extra* data, which would otherwise reach validation and be mapped onto
    the fields by ``populate_by_name=True``.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("READ_TIMEOUT=not-a-duration\nREAD_ONLY=1\nAPI_KEY=stray\n")
    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert settings.read_timeout == 30.0
    assert settings.read_only is False
    assert settings.api_key is None


def test_secrets_do_not_leak_through_repr() -> None:
    settings = Settings(_env_file=None, url="https://op", api_key="super-secret")  # type: ignore[call-arg]
    assert "super-secret" not in repr(settings)
    assert "super-secret" not in str(settings.api_key)
    assert settings.credential_secret == "super-secret"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://op.example.com", "https://op.example.com"),
        ("https://op.example.com/", "https://op.example.com"),
        ("https://op.example.com/api/v3", "https://op.example.com"),
        ("https://op.example.com/api/v3/", "https://op.example.com"),
        ("  https://op.example.com  ", "https://op.example.com"),
        ("", None),
    ],
)
def test_url_normalization(raw: str, expected: str | None) -> None:
    assert Settings(_env_file=None, url=raw).url == expected  # type: ignore[call-arg]


def test_api_base_url_appends_api_v3() -> None:
    settings = Settings(_env_file=None, url="https://op.example.com")  # type: ignore[call-arg]
    assert settings.api_base_url == "https://op.example.com/api/v3"


def test_runtime_check_reports_missing_url_and_key() -> None:
    problems = check_runtime_config(Settings(_env_file=None), "stdio")  # type: ignore[call-arg]
    assert any("OPENPROJECT_URL" in problem for problem in problems)
    assert any("OPENPROJECT_API_KEY" in problem for problem in problems)


def test_runtime_check_passes_with_url_and_key() -> None:
    settings = Settings(_env_file=None, url="https://op", api_key="k")  # type: ignore[call-arg]
    assert check_runtime_config(settings, "stdio") == []


def test_runtime_check_accepts_oauth_token_instead_of_api_key() -> None:
    settings = Settings(_env_file=None, url="https://op", oauth_token="t")  # type: ignore[call-arg]
    assert check_runtime_config(settings, "stdio") == []


def test_runtime_check_rejects_a_scheme_less_url() -> None:
    settings = Settings(_env_file=None, url="op.example.com", api_key="k")  # type: ignore[call-arg]
    problems = check_runtime_config(settings, "stdio")
    assert any("http://" in problem for problem in problems)


def test_http_transport_refuses_to_start_without_auth() -> None:
    settings = Settings(_env_file=None, url="https://op", api_key="k")  # type: ignore[call-arg]
    problems = check_runtime_config(settings, "http")
    assert any("OPENPROJECT_MCP_AUTH_TOKENS" in problem for problem in problems)


def test_http_transport_allows_explicit_insecure_mode() -> None:
    settings = Settings(_env_file=None, url="https://op", api_key="k", insecure=True)  # type: ignore[call-arg]
    assert check_runtime_config(settings, "http") == []


def test_http_transport_starts_with_auth_tokens() -> None:
    settings = Settings(_env_file=None, url="https://op", api_key="k", auth_tokens="tok")  # type: ignore[call-arg]
    assert check_runtime_config(settings, "http") == []


@pytest.mark.parametrize("raw", [",", " , ", ",,", "  "])
def test_http_transport_refuses_auth_tokens_that_parse_empty(raw: str) -> None:
    """``AUTH_TOKENS=","`` is a configuration mistake, not an open endpoint."""
    settings = Settings(_env_file=None, url="https://op", api_key="k", auth_tokens=raw)  # type: ignore[call-arg]
    problems = check_runtime_config(settings, "http")
    assert any("contains no tokens" in problem for problem in problems)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("alpha", ("alpha",)),
        ("alpha,beta", ("alpha", "beta")),
        ("alpha, beta", ("alpha", "beta")),
        ("  alpha  ,  beta  ", ("alpha", "beta")),
        ("alpha,beta,", ("alpha", "beta")),
        (",alpha,,beta,", ("alpha", "beta")),
        (",", ()),
        ("", ()),
        ("   ", ()),
    ],
)
def test_bearer_token_parsing(raw: str, expected: tuple[str, ...]) -> None:
    settings = Settings(_env_file=None, auth_tokens=raw)  # type: ignore[call-arg]
    assert settings.bearer_tokens == expected


def test_bearer_tokens_default_to_empty_when_unset() -> None:
    assert Settings(_env_file=None).bearer_tokens == ()  # type: ignore[call-arg]


def test_auth_tokens_do_not_leak_through_repr() -> None:
    settings = Settings(_env_file=None, auth_tokens="hush-alpha,hush-beta")  # type: ignore[call-arg]
    assert "hush-alpha" not in repr(settings)
    assert "hush-alpha" not in str(settings.auth_tokens)
    assert settings.bearer_tokens == ("hush-alpha", "hush-beta")


def test_missing_ca_bundle_is_reported() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, url="https://op", api_key="k", ca_bundle="/does/not/exist.pem"
    )
    problems = check_runtime_config(settings, "stdio")
    assert any("CA_BUNDLE" in problem for problem in problems)
