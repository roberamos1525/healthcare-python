"""Environment-variable configuration with the reference implementation's keys and defaults.

Keys use the ``Section__Key`` form (``Auth__AccessTokenMinutes``, ``ConnectionStrings__Database``,
``Cors__AllowedOrigins__0``) and are matched case-insensitively. A pydantic-settings source flattens them
into the typed settings model, so every value is validated once at startup.
"""

import os
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


def normalize_key(key: str) -> str:
    """Lower-case a key and turn ``__`` into ``:``."""
    return key.replace("__", ":").lower()


class Values:
    """A case-insensitive view of configuration values keyed by ``section:key``."""

    def __init__(self, entries: Mapping[str, str] | None = None) -> None:
        """Create the view from raw key/value pairs."""
        self._values: dict[str, str] = {}
        for key, value in (entries or {}).items():
            self.set(key, value)

    @classmethod
    def from_environment(cls) -> Values:
        """Read every environment variable."""
        return cls(dict(os.environ))

    def set(self, key: str, value: str) -> None:
        """Add or replace a value."""
        self._values[normalize_key(key)] = value

    def get(self, key: str) -> str | None:
        """Return a non-blank value or None."""
        value = self._values.get(normalize_key(key))
        return value if value is not None and value.strip() else None

    def items_with_prefix(self, prefix: str) -> dict[str, str]:
        """Return the entries below a section, keyed by the remainder of their names."""
        start = normalize_key(prefix) + ":"
        return {key[len(start) :]: value for key, value in self._values.items() if key.startswith(start)}

    def text(self, key: str, default: str) -> str:
        """Return a value or a default."""
        return self.get(key) or default

    def as_list(self, key: str) -> list[str] | None:
        """Return an array section (``Key:0``, ``Key:1``) or a comma-separated value; None when absent."""
        indexed = [(int(name), value) for name, value in self.items_with_prefix(key).items() if name.isdigit()]
        if indexed:
            return [value.strip() for _, value in sorted(indexed) if value.strip()]
        value = self.get(key)
        if value is None:
            return None
        return [part.strip() for part in value.split(",") if part.strip()]


class AuthSettings(BaseModel):
    """Authentication and session policy (``Auth`` section)."""

    issuer: str = "healthcare-platform"
    audience: str = "healthcare-api"
    signing_key_pem: str = ""
    signing_key_path: str = ""
    field_encryption_key: str = ""
    access_token_minutes: int = 10
    session_idle_timeout_minutes: int = 60
    session_absolute_lifetime_hours: int = 12
    refresh_reuse_grace_seconds: int = 10
    max_failed_sign_in_attempts: int = 5
    lockout_minutes: int = 15
    email_verification_token_hours: int = 24
    password_reset_token_minutes: int = 60
    mfa_challenge_minutes: int = 5
    invitation_days: int = 7
    password_hash_iterations: int = 600_000
    mfa_issuer_name: str = "HealthCare Platform"


class AppSettings(BaseModel):
    """General settings (``App`` section)."""

    frontend_base_url: str = "http://localhost:5173"
    idempotency_retention_hours: int = 48


class EmailSettings(BaseModel):
    """Email delivery (``Email`` section): ``Smtp``, ``File`` or ``None``."""

    provider: str = "None"
    from_address: str = "no-reply@healthcare.localhost"
    from_name: str = "HealthCare Platform"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_use_start_tls: bool = False
    smtp_username: str = ""
    smtp_password: str = ""
    pickup_directory: str = "mail-pickup"


class MessagingSettings(BaseModel):
    """Event publishing (``Messaging`` section): ``Log`` (default) or ``RabbitMq``."""

    provider: str = "Log"
    rabbit_mq_uri: str = ""
    exchange: str = "healthcare.events"


class WorkerSettings(BaseModel):
    """Background jobs (``Workers`` section)."""

    enabled: bool = True
    outbox_poll_seconds: int = 2
    reminder_poll_seconds: int = 30
    maintenance_interval_minutes: int = 60
    batch_size: int = 50
    outbox_retention_days: int = 14
    expired_credential_retention_days: int = 30


class RateLimitSettings(BaseModel):
    """Request limits (``RateLimiting`` section)."""

    enabled: bool = True
    general_per_minute: int = 600
    auth_per_minute: int = 20


class Settings(BaseSettings):
    """The complete configuration."""

    model_config = SettingsConfigDict(extra="ignore")

    environment: str = "Production"
    http_port: int = 8080
    database: str = ""
    migrations: str = ""
    redis: str = ""
    auth: AuthSettings = Field(default_factory=AuthSettings)
    app: AppSettings = Field(default_factory=AppSettings)
    email: EmailSettings = Field(default_factory=EmailSettings)
    messaging: MessagingSettings = Field(default_factory=MessagingSettings)
    workers: WorkerSettings = Field(default_factory=WorkerSettings)
    rate_limiting: RateLimitSettings = Field(default_factory=RateLimitSettings)
    cors_allowed_origins: list[str] = Field(default_factory=list)
    forwarded_headers: bool = False
    migrate_on_startup: bool = False
    log_level: str = "Information"
    otlp_endpoint: str = ""
    shutdown_timeout_seconds: int = 30
    workers_per_host: int = 1

    @property
    def allows_temporary_keys(self) -> bool:
        """Return whether keys may be generated at startup (development and testing only)."""
        return self.environment.lower() in ("development", "testing")

    @property
    def is_development(self) -> bool:
        """Return whether this is the development environment."""
        return self.environment.lower() == "development"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Read only the explicit values passed to the constructor (see ``load_settings``)."""
        return (init_settings,)


_SECTION_FIELDS: dict[str, tuple[str, type[BaseModel]]] = {
    "auth": ("Auth", AuthSettings),
    "app": ("App", AppSettings),
    "email": ("Email", EmailSettings),
    "messaging": ("Messaging", MessagingSettings),
    "workers": ("Workers", WorkerSettings),
    "rate_limiting": ("RateLimiting", RateLimitSettings),
}


def _section(values: Values, section: str, model: type[BaseModel]) -> dict[str, Any]:
    """Collect the values of one section, matching field names case-insensitively without underscores."""
    entries = {
        name.replace("_", ""): value
        for name, value in values.items_with_prefix(section).items()
        if ":" not in name and value.strip()
    }
    result: dict[str, Any] = {}
    for field in model.model_fields:
        value = entries.get(field.replace("_", ""))
        if value is not None:
            result[field] = value.strip() if field != "signing_key_pem" else value
    return result


def _http_port(values: Values) -> int | None:
    """Read the listening port from ASPNETCORE_HTTP_PORTS, HTTP_PORTS, PORT or ASPNETCORE_URLS."""
    for key in ("ASPNETCORE_HTTP_PORTS", "HTTP_PORTS", "PORT"):
        value = values.get(key)
        if value:
            first = value.replace(",", ";").split(";")[0].strip()
            if first.isdigit():
                return int(first)
    urls = values.get("ASPNETCORE_URLS")
    if urls:
        first = urls.split(";")[0].rstrip("/")
        port = first.rsplit(":", 1)[-1]
        if port.isdigit():
            return int(port)
    return None


def load_settings(values: Values | None = None) -> Settings:
    """Build the validated settings from the environment (or from the given values)."""
    values = values or Values.from_environment()
    environment = values.text(
        "ASPNETCORE_ENVIRONMENT", values.text("HEALTHCARE_ENVIRONMENT", values.text("DOTNET_ENVIRONMENT", "Production"))
    )
    development = environment.lower() == "development"
    data: dict[str, Any] = {"environment": environment}
    for field, (section, model) in _SECTION_FIELDS.items():
        data[field] = _section(values, section, model)
    if development and "provider" not in data["email"]:
        data["email"]["provider"] = "File"
    origins = values.as_list("Cors:AllowedOrigins")
    data["cors_allowed_origins"] = (
        origins if origins is not None else (["http://localhost:5173"] if development else [])
    )
    port = _http_port(values)
    if port is not None:
        data["http_port"] = port
    simple = {
        "database": "ConnectionStrings:Database",
        "migrations": "ConnectionStrings:Migrations",
        "redis": "ConnectionStrings:Redis",
        "forwarded_headers": "ForwardedHeaders:Enabled",
        "migrate_on_startup": "Database:MigrateOnStartup",
        "otlp_endpoint": "OTEL_EXPORTER_OTLP_ENDPOINT",
        "shutdown_timeout_seconds": "HostOptions:ShutdownTimeoutSeconds",
        "workers_per_host": "HEALTHCARE_WORKERS",
    }
    for field, key in simple.items():
        value = values.get(key)
        if value is not None:
            data[field] = value.strip()
    data["log_level"] = values.text(
        "Serilog:MinimumLevel:Default", values.text("Logging:LogLevel:Default", "Information")
    )
    return Settings(**data)
