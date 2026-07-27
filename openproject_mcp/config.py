"""Settings for the OpenProject MCP server (SPEC §14 config surface).

Every knob is an environment variable. Nothing here reads the network or
raises at import time: ``Settings()`` is constructable with zero environment
so that ``--help``, tests and ``build_server()`` all work on a bare machine.
Runtime requirements (a URL, a credential, HTTP-transport auth) are checked
explicitly by :func:`check_runtime_config`, which returns readable problems
instead of tracebacks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogFormat = Literal["text", "json"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
TransportName = Literal["stdio", "http"]

DEFAULT_CACHE_TTL = 300.0
PROBE_CACHE_TTL = 3600.0


class Settings(BaseSettings):
    """Environment-driven configuration.

    Field names are snake_case; the environment variable for each field is
    given by its validation alias (SPEC §14). Unknown environment variables
    are ignored.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- connection -----------------------------------------------------
    url: str | None = Field(default=None, validation_alias="OPENPROJECT_URL")
    api_key: SecretStr | None = Field(default=None, validation_alias="OPENPROJECT_API_KEY")
    oauth_token: SecretStr | None = Field(default=None, validation_alias="OPENPROJECT_OAUTH_TOKEN")
    accept_language: str | None = Field(
        default=None, validation_alias="OPENPROJECT_MCP_ACCEPT_LANGUAGE"
    )
    ca_bundle: Path | None = Field(default=None, validation_alias="OPENPROJECT_MCP_CA_BUNDLE")

    # --- deployment blast radius ---------------------------------------
    read_only: bool = Field(default=False, validation_alias="OPENPROJECT_MCP_READ_ONLY")
    admin_tools: bool = Field(default=False, validation_alias="OPENPROJECT_MCP_ADMIN_TOOLS")
    disable: str = Field(default="", validation_alias="OPENPROJECT_MCP_DISABLE")
    insecure: bool = Field(default=False, validation_alias="OPENPROJECT_MCP_INSECURE")

    # --- files ----------------------------------------------------------
    download_dir: Path | None = Field(default=None, validation_alias="OPENPROJECT_MCP_DOWNLOAD_DIR")
    max_download_mb: int = Field(
        default=100, ge=1, validation_alias="OPENPROJECT_MCP_MAX_DOWNLOAD_MB"
    )

    # --- caching --------------------------------------------------------
    cache_ttl: float = Field(
        default=DEFAULT_CACHE_TTL, ge=0, validation_alias="OPENPROJECT_MCP_CACHE_TTL"
    )

    # --- observability --------------------------------------------------
    log_level: LogLevel = Field(default="INFO", validation_alias="OPENPROJECT_MCP_LOG_LEVEL")
    log_format: LogFormat = Field(default="text", validation_alias="OPENPROJECT_MCP_LOG_FORMAT")
    log_bodies: bool = Field(default=False, validation_alias="OPENPROJECT_MCP_LOG_BODIES")
    otel: bool = Field(default=False, validation_alias="OPENPROJECT_MCP_OTEL")

    # --- HTTP transport (streamable HTTP; see SPEC §3.3 / §11) ----------
    http_host: str = Field(default="127.0.0.1", validation_alias="OPENPROJECT_MCP_HTTP_HOST")
    http_port: int = Field(default=8000, validation_alias="OPENPROJECT_MCP_HTTP_PORT")
    auth_tokens: SecretStr | None = Field(
        default=None, validation_alias="OPENPROJECT_MCP_AUTH_TOKENS"
    )

    # --- timeouts (SPEC §4.1) -------------------------------------------
    connect_timeout: float = Field(default=10.0, gt=0)
    read_timeout: float = Field(default=30.0, gt=0)
    write_timeout: float = Field(default=60.0, gt=0)
    pool_timeout: float = Field(default=5.0, gt=0)
    max_connections: int = Field(default=10, gt=0)
    max_retries: int = Field(default=3, ge=1)

    @field_validator("url", mode="after")
    @classmethod
    def _normalize_url(cls, value: str | None) -> str | None:
        """Accept the instance root with or without a trailing ``/api/v3``."""
        if value is None:
            return None
        cleaned = value.strip().rstrip("/")
        if not cleaned:
            return None
        for suffix in ("/api/v3", "/api/v3/"):
            if cleaned.endswith(suffix):
                cleaned = cleaned[: -len(suffix)].rstrip("/")
        return cleaned

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("log_format", mode="before")
    @classmethod
    def _lower_log_format(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    @property
    def api_base_url(self) -> str:
        """Base URL for every API call, e.g. ``https://op.example.com/api/v3``."""
        if not self.url:
            raise ValueError("OPENPROJECT_URL is not configured")
        return f"{self.url}/api/v3"

    @property
    def disabled_groups(self) -> frozenset[str]:
        """Group tags dropped at startup (``OPENPROJECT_MCP_DISABLE=meetings,news``)."""
        return frozenset(part.strip() for part in self.disable.split(",") if part.strip())

    @property
    def has_credential(self) -> bool:
        return bool(self.api_key or self.oauth_token)

    @property
    def credential_secret(self) -> str | None:
        """The raw credential, used only for cache scoping and auth construction."""
        if self.api_key is not None:
            return self.api_key.get_secret_value()
        if self.oauth_token is not None:
            return self.oauth_token.get_secret_value()
        return None

    @property
    def max_download_bytes(self) -> int:
        return self.max_download_mb * 1024 * 1024


def check_runtime_config(settings: Settings, transport: TransportName) -> list[str]:
    """Return human-readable configuration problems, empty when good to run.

    Kept separate from validation so the server object stays constructable
    with zero environment (SPEC §3.4) while the CLI can still refuse to start
    with a clean message instead of a traceback.
    """
    problems: list[str] = []
    if not settings.url:
        problems.append(
            "OPENPROJECT_URL is not set — point it at your instance root, "
            "e.g. https://openproject.example.com"
        )
    elif not settings.url.startswith(("http://", "https://")):
        problems.append(
            f"OPENPROJECT_URL must start with http:// or https:// (got {settings.url!r})"
        )
    if not settings.has_credential:
        problems.append(
            "OPENPROJECT_API_KEY is not set — create an API key under "
            "'My account → Access tokens' in OpenProject "
            "(or set OPENPROJECT_OAUTH_TOKEN for bearer auth)"
        )
    if settings.ca_bundle is not None and not settings.ca_bundle.exists():
        problems.append(f"OPENPROJECT_MCP_CA_BUNDLE does not exist: {settings.ca_bundle}")
    if transport == "http" and not settings.insecure and settings.auth_tokens is None:
        problems.append(
            "HTTP transport requires authentication: set OPENPROJECT_MCP_AUTH_TOKENS "
            "(comma-separated bearer tokens) or, for local development only, "
            "OPENPROJECT_MCP_INSECURE=1"
        )
    return problems
