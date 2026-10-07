"""Application settings, loaded from environment variables and validated at startup.

Missing required variables crash the process immediately (fail fast).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, PostgresDsn, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from jobpulse_core.ingestion.http import HttpClientConfig
from jobpulse_core.intelligence import IntelligenceConfig
from jobpulse_core.sources.ats import ATS_HOST_ALLOWLIST

# Must match the vector(N) columns created by the initial migration.
EMBEDDING_DIMENSIONS = 1536
MIN_SECRET_LENGTH = 32
RESEND_HOST = "api.resend.com"

Environment = Literal["local", "test", "staging", "production"]


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # `FOO=` in .env means "unset", not "empty secret".
        env_ignore_empty=True,
    )

    environment: Environment = "local"
    service_name: str = "jobpulse-api"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True

    # --- database ---------------------------------------------------------------
    database_url: PostgresDsn
    db_pool_size: Annotated[int, Field(ge=1, le=100)] = 10
    db_max_overflow: Annotated[int, Field(ge=0, le=100)] = 10
    db_pool_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 10.0
    db_statement_timeout_ms: Annotated[int, Field(ge=100, le=600_000)] = 15_000

    # --- temporal ---------------------------------------------------------------
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_api_key: SecretStr | None = None
    temporal_tls: bool = False
    temporal_task_queue: str = "jobpulse"
    temporal_connect_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 10.0

    # --- auth -------------------------------------------------------------------
    api_jwt_secret: SecretStr
    api_jwt_audience: str = "jobpulse-api"
    api_jwt_issuer: str = "jobpulse-web"
    public_demo_enabled: bool = True

    # --- http hardening ---------------------------------------------------------
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:3000"])
    max_request_bytes: Annotated[int, Field(ge=1024, le=10 * 1024 * 1024)] = 256 * 1024
    rate_limit_per_minute: Annotated[int, Field(ge=1, le=100_000)] = 120
    trusted_proxy_count: Annotated[int, Field(ge=0, le=5)] = 1

    # --- outbound fetching ------------------------------------------------------
    outbound_enforce_allowlist: bool = True
    outbound_allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    outbound_connect_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 5.0
    outbound_read_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 20.0
    outbound_total_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 30.0
    outbound_max_response_bytes: Annotated[int, Field(ge=1024, le=50 * 1024 * 1024)] = 10 * 1024 * 1024
    outbound_respect_robots_txt: bool = True

    # --- AI -------------------------------------------------------------------
    openai_api_key: SecretStr | None = None
    openai_classification_model: str = "gpt-5-mini"
    openai_reasoning_model: str | None = "gpt-5"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 45.0
    openai_escalation_threshold: Annotated[float, Field(ge=0, le=1)] = 0.6
    openai_input_price_per_million: Annotated[float, Field(ge=0)] = 0.0
    openai_output_price_per_million: Annotated[float, Field(ge=0)] = 0.0
    openai_embedding_price_per_million: Annotated[float, Field(ge=0)] = 0.0
    embedding_dimensions: int = EMBEDDING_DIMENSIONS

    # --- storage ----------------------------------------------------------------
    storage_backend: Literal["local", "r2"] = "local"
    local_storage_path: Path = Path("./.data/snapshots")
    r2_account_id: str | None = None
    r2_access_key_id: SecretStr | None = None
    r2_secret_access_key: SecretStr | None = None
    r2_bucket: str | None = None
    r2_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 20.0

    # --- notifications ----------------------------------------------------------
    resend_api_key: SecretStr | None = None
    notification_from_email: str | None = None
    webhook_signing_secret: SecretStr | None = None
    dashboard_base_url: str = "http://localhost:3000"

    # --- observability ----------------------------------------------------------
    sentry_dsn: SecretStr | None = None
    sentry_traces_sample_rate: Annotated[float, Field(ge=0, le=1)] = 0.0
    otel_exporter_otlp_endpoint: str | None = None
    metrics_enabled: bool = True

    _split_origins = field_validator("cors_allowed_origins", "outbound_allowed_hosts", mode="before")(
        _split_csv,
    )

    @field_validator("database_url")
    @classmethod
    def _psycopg_driver(cls, value: PostgresDsn) -> PostgresDsn:
        if value.scheme != "postgresql+psycopg":
            msg = "DATABASE_URL must use the 'postgresql+psycopg://' scheme"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _cross_field(self) -> Self:
        if len(self.api_jwt_secret.get_secret_value()) < MIN_SECRET_LENGTH:
            msg = f"API_JWT_SECRET must be at least {MIN_SECRET_LENGTH} characters"
            raise ValueError(msg)
        if self.embedding_dimensions != EMBEDDING_DIMENSIONS:
            msg = f"EMBEDDING_DIMENSIONS must equal {EMBEDDING_DIMENSIONS} (schema vector size)"
            raise ValueError(msg)
        if self.storage_backend == "r2":
            missing = [
                name
                for name in ("r2_account_id", "r2_access_key_id", "r2_secret_access_key", "r2_bucket")
                if getattr(self, name) in (None, "")
            ]
            if missing:
                msg = f"STORAGE_BACKEND=r2 requires: {', '.join(n.upper() for n in missing)}"
                raise ValueError(msg)
        if self.environment == "production":
            if "*" in self.cors_allowed_origins:
                msg = "wildcard CORS origin is not allowed in production"
                raise ValueError(msg)
            if self.storage_backend != "r2":
                msg = "production requires STORAGE_BACKEND=r2"
                raise ValueError(msg)
        if self.temporal_api_key is not None and not self.temporal_tls:
            msg = "TEMPORAL_API_KEY requires TEMPORAL_TLS=true"
            raise ValueError(msg)
        return self

    # --- derived configs -------------------------------------------------------

    @property
    def sqlalchemy_url(self) -> str:
        return str(self.database_url)

    def http_client_config(
        self, *, extra_hosts: list[str] | None = None, robots: bool | None = None
    ) -> HttpClientConfig:
        hosts: tuple[str, ...] = ()
        if self.outbound_enforce_allowlist:
            hosts = tuple(
                dict.fromkeys([*ATS_HOST_ALLOWLIST, RESEND_HOST, *self.outbound_allowed_hosts, *(extra_hosts or [])]),
            )
        return HttpClientConfig(
            connect_timeout_seconds=self.outbound_connect_timeout_seconds,
            read_timeout_seconds=self.outbound_read_timeout_seconds,
            total_timeout_seconds=self.outbound_total_timeout_seconds,
            max_response_bytes=self.outbound_max_response_bytes,
            allowed_host_suffixes=hosts,
            respect_robots_txt=self.outbound_respect_robots_txt if robots is None else robots,
        )

    def intelligence_config(self) -> IntelligenceConfig:
        return IntelligenceConfig(
            api_key=self.openai_api_key.get_secret_value() if self.openai_api_key else None,
            classification_model=self.openai_classification_model,
            reasoning_model=self.openai_reasoning_model or None,
            embedding_model=self.openai_embedding_model,
            embedding_dimensions=self.embedding_dimensions,
            timeout_seconds=self.openai_timeout_seconds,
            escalation_threshold=self.openai_escalation_threshold,
            input_price_per_million=self.openai_input_price_per_million,
            output_price_per_million=self.openai_output_price_per_million,
            embedding_price_per_million=self.openai_embedding_price_per_million,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton. Raises pydantic ValidationError on bad config."""
    return Settings()  # type: ignore[call-arg]  # populated from environment
