"""Settings: zero-env construction, env mapping, normalization, runtime checks."""

from __future__ import annotations

import pytest

from openproject_mcp.config import Settings, check_runtime_config


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


def test_missing_ca_bundle_is_reported() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, url="https://op", api_key="k", ca_bundle="/does/not/exist.pem"
    )
    problems = check_runtime_config(settings, "stdio")
    assert any("CA_BUNDLE" in problem for problem in problems)
