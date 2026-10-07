"""scripts/demo.py: .env preparation must fill gaps without ever overwriting real values."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "demo.py"


@pytest.fixture
def demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("jobpulse_demo_script", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(module, "ENV_EXAMPLE", tmp_path / ".env.example")
    return module


def env_values(module: ModuleType) -> dict[str, str]:
    _, values = module.read_env(module.ENV_FILE)
    return dict(values)


def test_creates_env_and_fills_missing_secrets(demo: ModuleType) -> None:
    demo.ENV_EXAMPLE.write_text(
        "# comment kept\nENVIRONMENT=local\nAUTH_SECRET=\nAPI_JWT_PRIVATE_JWK=\nAPI_JWT_JWKS=\nDEMO_MODE=false\n",
        encoding="utf-8",
    )
    changed = demo.prepare_env()
    values = env_values(demo)
    assert changed == ["API_JWT_JWKS", "API_JWT_PRIVATE_JWK", "AUTH_SECRET", "DEMO_MODE", "WEBHOOK_SIGNING_SECRET"]
    assert len(values["AUTH_SECRET"]) >= 32
    private, public = json.loads(values["API_JWT_PRIVATE_JWK"]), json.loads(values["API_JWT_JWKS"])
    assert private["kid"] == public["keys"][0]["kid"]
    assert "d" in private
    assert "d" not in public["keys"][0]
    assert values["DEMO_MODE"] == "true"
    assert demo.ENV_FILE.read_text(encoding="utf-8").startswith("# comment kept\n")


def test_never_overwrites_existing_values(demo: ModuleType) -> None:
    demo.ENV_FILE.write_text(
        "AUTH_SECRET=keep-me-keep-me-keep-me-keep-me-keep\n"
        'API_JWT_PRIVATE_JWK={"kid":"mine"}\nAPI_JWT_JWKS={"keys":[]}\n'
        "WEBHOOK_SIGNING_SECRET=whsec-mine\nDEMO_MODE=true\nAUTH_GITHUB_SECRET=gh-secret\n",
        encoding="utf-8",
    )
    before = env_values(demo)
    assert demo.prepare_env() == []
    assert env_values(demo) == before


def test_refuses_half_configured_key_pair(demo: ModuleType) -> None:
    demo.ENV_FILE.write_text('API_JWT_PRIVATE_JWK={"kid":"x"}\nAPI_JWT_JWKS=\n', encoding="utf-8")
    with pytest.raises(demo.DemoError, match="only one of"):
        demo.prepare_env()


def test_refuses_production_env(demo: ModuleType) -> None:
    demo.ENV_FILE.write_text("ENVIRONMENT=production\n", encoding="utf-8")
    with pytest.raises(demo.DemoError, match="production"):
        demo.prepare_env()
    assert "DEMO_MODE" not in env_values(demo)  # nothing written on refusal
