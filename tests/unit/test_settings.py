"""Settings load from the shipped .env.example and fail fast on bad config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from jobpulse.core.config import Settings
from jobpulse.core.redis import RedisConfigError, require_redis_for_api
from jobpulse.main import create_app
from tests.auth_helpers import JWKS_JSON, PRIVATE_JWK

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("DATABASE_URL", "API_JWT_JWKS", "ENVIRONMENT", "LOG_JSON", "OWNER_GITHUB_IDS"):
        monkeypatch.delenv(key, raising=False)


def test_env_example_loads_with_blank_optionals() -> None:
    settings = Settings(_env_file=ENV_EXAMPLE, api_jwt_jwks=JWKS_JSON)  # type: ignore[call-arg]
    assert settings.temporal_api_key is None
    assert settings.openai_api_key is None
    assert settings.r2_bucket is None
    assert settings.intelligence_config().enabled is False


DB = "postgresql+psycopg://u:p@h/db"


@pytest.mark.parametrize(
    ("jwks", "message"),
    [
        ("not json", "not valid JSON"),
        ('{"keys": []}', "non-empty"),
        (json.dumps({"keys": [PRIVATE_JWK]}), "PUBLIC keys only"),
        (json.dumps({"keys": [{"kty": "oct", "k": "c2VjcmV0", "kid": "x"}]}), "Ed25519"),
        (json.dumps({"keys": [{k: v for k, v in json.loads(JWKS_JSON)["keys"][0].items() if k != "kid"}]}), "kid"),
    ],
)
def test_rejects_unsafe_or_malformed_jwks(jwks: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(database_url=DB, api_jwt_jwks=jwks)  # type: ignore[arg-type]


def test_owner_ids_parse_from_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWNER_GITHUB_IDS", "583231, 42")
    settings = Settings(database_url=DB, api_jwt_jwks=JWKS_JSON)  # type: ignore[arg-type]
    assert settings.owner_github_ids == [583231, 42]


def test_owner_ids_must_be_numeric(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWNER_GITHUB_IDS", "octocat")
    with pytest.raises(ValidationError, match="owner_github_ids"):
        Settings(database_url=DB, api_jwt_jwks=JWKS_JSON)  # type: ignore[arg-type]


def test_rejects_wrong_driver() -> None:
    with pytest.raises(ValidationError, match=r"postgresql\+psycopg"):
        Settings(database_url="postgresql://u:p@h/db", api_jwt_jwks=JWKS_JSON)  # type: ignore[arg-type]


PRODUCTION_BASE: dict[str, object] = {
    "database_url": "postgresql+psycopg://u:p@h/db",
    "api_jwt_jwks": JWKS_JSON,
    "environment": "production",
    "redis_url": "rediss://default:pw@cache.example:6380/0",
    "storage_backend": "r2",
    "r2_account_id": "a",
    "r2_access_key_id": "k",
    "r2_secret_access_key": "s",
    "r2_bucket": "b",
}


def test_production_baseline_is_valid() -> None:
    settings = Settings(_env_file=None, **PRODUCTION_BASE)  # type: ignore[arg-type,call-arg]
    assert settings.cors_allowed_origins == []  # deny-by-default
    require_redis_for_api(settings)


def test_redis_is_required_by_the_api_but_not_the_worker() -> None:
    worker_settings = Settings(_env_file=None, **{**PRODUCTION_BASE, "redis_url": None})  # type: ignore[arg-type,call-arg]
    with pytest.raises(RedisConfigError, match="REDIS_URL is required"):
        require_redis_for_api(worker_settings)
    with pytest.raises(RedisConfigError):
        create_app(worker_settings)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"demo_mode": True}, "DEMO_MODE must be off"),
        ({"cors_allowed_origins": "*"}, "wildcard CORS"),
        ({"storage_backend": "local"}, "STORAGE_BACKEND=r2"),
        ({"r2_bucket": None}, "R2_BUCKET"),
        ({"redis_url": "http://cache.example"}, "redis:// or rediss://"),
        ({"cors_allowed_origins": "https://app.example/path"}, "exact origins"),
    ],
)
def test_production_rules(override: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None, **{**PRODUCTION_BASE, **override})  # type: ignore[arg-type,call-arg]


def test_temporal_api_key_requires_tls() -> None:
    with pytest.raises(ValidationError, match="TEMPORAL_TLS"):
        Settings(
            database_url="postgresql+psycopg://u:p@h/db",  # type: ignore[arg-type]
            api_jwt_jwks=JWKS_JSON,  # type: ignore[arg-type]
            temporal_api_key="key",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("url", "override", "expected"),
    [
        ("postgresql+psycopg://u:p@ep-cool-123.eu-central-1.aws.neon.tech/db", None, True),
        ("postgresql+psycopg://u:p@ep-cool-123-pooler.eu-central-1.aws.neon.tech/db", None, False),
        ("postgresql+psycopg://u:p@ep-cool-123-pooler.eu-central-1.aws.neon.tech/db", True, True),
        ("postgresql+psycopg://u:p@localhost/db", False, False),
    ],
)
def test_prepared_statements_auto_disabled_for_pgbouncer(url: str, override: bool | None, expected: bool) -> None:
    settings = Settings(database_url=url, api_jwt_jwks=JWKS_JSON, db_prepared_statements=override)  # type: ignore[arg-type]
    assert settings.use_prepared_statements is expected
