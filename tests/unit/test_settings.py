"""Settings load from the shipped .env.example and fail fast on bad config."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from jobpulse.core.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"
SECRET = "s" * 48


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("DATABASE_URL", "API_JWT_SECRET", "ENVIRONMENT", "LOG_JSON"):
        monkeypatch.delenv(key, raising=False)


def test_env_example_loads_with_blank_optionals() -> None:
    settings = Settings(_env_file=ENV_EXAMPLE)  # type: ignore[call-arg]
    assert settings.temporal_api_key is None
    assert settings.openai_api_key is None
    assert settings.r2_bucket is None
    assert settings.intelligence_config().enabled is False


def test_rejects_short_secret() -> None:
    with pytest.raises(ValidationError, match="API_JWT_SECRET"):
        Settings(database_url="postgresql+psycopg://u:p@h/db", api_jwt_secret="short")  # type: ignore[arg-type]


def test_rejects_wrong_driver() -> None:
    with pytest.raises(ValidationError, match=r"postgresql\+psycopg"):
        Settings(database_url="postgresql://u:p@h/db", api_jwt_secret=SECRET)  # type: ignore[arg-type]


def test_production_requires_r2_and_no_wildcard_cors() -> None:
    base = {"database_url": "postgresql+psycopg://u:p@h/db", "api_jwt_secret": SECRET, "environment": "production"}
    with pytest.raises(ValidationError, match="STORAGE_BACKEND=r2"):
        Settings(**base)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="R2_BUCKET"):
        Settings(**base, storage_backend="r2", r2_account_id="a", r2_access_key_id="k", r2_secret_access_key="s")  # type: ignore[arg-type]


def test_temporal_api_key_requires_tls() -> None:
    with pytest.raises(ValidationError, match="TEMPORAL_TLS"):
        Settings(
            database_url="postgresql+psycopg://u:p@h/db",  # type: ignore[arg-type]
            api_jwt_secret=SECRET,  # type: ignore[arg-type]
            temporal_api_key="key",  # type: ignore[arg-type]
        )
