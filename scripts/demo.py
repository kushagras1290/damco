"""One-command local demo:  uv run python scripts/demo.py

1. Creates ``.env`` from ``.env.example`` if needed and fills in any *missing* secrets
   (AUTH_SECRET, the Ed25519 web->API key pair, webhook secret). Existing values are
   never overwritten and secrets are never printed.
2. Turns on DEMO_MODE (a fictional job board that releases new postings every minute).
3. Builds and starts the Docker Compose stack and waits until the API and web are ready.
4. Seeds the profile, real public job boards and the demo board.
5. Opens the dashboard, where the "Live activity" feed updates in real time.

Options: --no-build (reuse images), --no-open (don't launch a browser), --timeout SECONDS.
Stop with:  docker compose down
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
LOCALHOST = "localhost"
API_PORT = 8000
WEB_PORT = 3000
DASHBOARD_URL = f"http://{LOCALHOST}:{WEB_PORT}/dashboard"
SEED_FILES = ("/app/seed.yaml", "/app/seed.demo.yaml")
DEFAULT_TIMEOUT_SECONDS = 900
PROBE_TIMEOUT_SECONDS = 3
POLL_INTERVAL_SECONDS = 3
SECRET_BYTES = 48


class DemoError(Exception):
    """A setup step failed; the message says what to do next."""


def step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


# ------------------------------------------------------------------ .env handling


def read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            values[key.strip()] = value.strip()
    return lines, values


def write_env(path: Path, lines: list[str], updates: dict[str, str]) -> None:
    """Replace ``KEY=`` lines in place (append unknown keys), atomically."""
    pending = dict(updates)
    output: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key is not None and key in pending:
            output.append(f"{key}={pending.pop(key)}")
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in pending.items())
    temp = path.with_suffix(".tmp")
    temp.write_text("\n".join(output) + "\n", encoding="utf-8")
    if os.name == "posix":
        temp.chmod(0o600)
    temp.replace(path)


def generate_key_pair() -> tuple[str, str]:
    from jobpulse.keys import generate  # noqa: PLC0415 - needs the uv workspace environment

    private_jwk, jwks = generate(f"{datetime.now(tz=UTC):%Y%m%d}-{secrets.token_hex(3)}")
    compact = (",", ":")
    return json.dumps(private_jwk, separators=compact), json.dumps(jwks, separators=compact)


def prepare_env() -> list[str]:
    """Fill missing secrets and enable demo mode. Returns the names of keys that changed."""
    if not ENV_FILE.exists():
        shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
        print("created .env from .env.example")
    lines, values = read_env(ENV_FILE)
    if values.get("ENVIRONMENT") == "production":
        msg = ".env has ENVIRONMENT=production; the demo needs a local environment"
        raise DemoError(msg)
    updates: dict[str, str] = {}

    has_private = bool(values.get("API_JWT_PRIVATE_JWK"))
    has_public = bool(values.get("API_JWT_JWKS"))
    if has_private != has_public:
        msg = "only one of API_JWT_PRIVATE_JWK / API_JWT_JWKS is set; set both (make keys) or clear both"
        raise DemoError(msg)
    if not has_private:
        updates["API_JWT_PRIVATE_JWK"], updates["API_JWT_JWKS"] = generate_key_pair()
    for name in ("AUTH_SECRET", "WEBHOOK_SIGNING_SECRET"):
        if not values.get(name):
            updates[name] = secrets.token_urlsafe(SECRET_BYTES)
    if values.get("DEMO_MODE", "").lower() != "true":
        updates["DEMO_MODE"] = "true"

    if updates:
        write_env(ENV_FILE, lines, updates)
    return sorted(updates)


# ------------------------------------------------------------------ docker


def docker_compose(*args: str, timeout: float) -> None:
    docker = shutil.which("docker")
    if docker is None:
        msg = "Docker is not installed or not on PATH"
        raise DemoError(msg)
    command = [docker, "compose", *args]
    try:
        result = subprocess.run(command, cwd=ROOT, timeout=timeout, check=False)  # noqa: S603 - fixed argv
    except subprocess.TimeoutExpired as exc:
        msg = f"`docker compose {' '.join(args)}` timed out after {timeout:.0f}s"
        raise DemoError(msg) from exc
    if result.returncode != 0:
        msg = f"`docker compose {' '.join(args)}` failed (exit {result.returncode})"
        raise DemoError(msg)


def http_ok(port: int, path: str) -> bool:
    """Plain HTTP GET against a fixed local port (no URL parsing, no other schemes)."""
    connection = http.client.HTTPConnection(LOCALHOST, port, timeout=PROBE_TIMEOUT_SECONDS)
    try:
        connection.request("GET", path)
        status = connection.getresponse().status
    except OSError, http.client.HTTPException:
        return False
    finally:
        connection.close()
    return 200 <= status < 400


def wait_until(name: str, ready: Callable[[], bool], deadline: float) -> None:
    while time.monotonic() < deadline:
        if ready():
            print(f"{name} is ready")
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    msg = f"{name} did not become ready in time; inspect with: docker compose logs {name.lower()}"
    raise DemoError(msg)


# ------------------------------------------------------------------ main


def run(args: argparse.Namespace) -> None:
    deadline = time.monotonic() + args.timeout

    step("Preparing .env")
    changed = prepare_env()
    print(f"updated: {', '.join(changed)} (values not shown)" if changed else ".env already complete")

    step("Starting the stack (Postgres, Redis, Temporal, API, worker, web)")
    up_args = ["up", "-d", *(["--build"] if args.build else [])]
    docker_compose(*up_args, timeout=max(1.0, deadline - time.monotonic()))

    step("Waiting for services")
    wait_until("API", lambda: http_ok(API_PORT, "/health/ready"), deadline)
    wait_until("Web", lambda: http_ok(WEB_PORT, "/"), deadline)

    step("Seeding profile, public job boards and the live demo board")
    for seed in SEED_FILES:
        docker_compose("exec", "-T", "api", "python", "-m", "jobpulse.seed", seed, timeout=120)

    step("Ready")
    print(f"Dashboard     {DASHBOARD_URL}   (watch 'Live activity')")
    print("API docs      http://localhost:8000/docs")
    print("Temporal UI   http://localhost:8233")
    print("New demo postings appear about once a minute. Sign in with GitHub as an owner")
    print("(OWNER_GITHUB_IDS in .env) to add sources or track applications. Stop: docker compose down")
    if args.open:
        webbrowser.open(DASHBOARD_URL)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-build", dest="build", action="store_false", help="reuse existing images")
    parser.add_argument("--no-open", dest="open", action="store_false", help="don't open a browser")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="overall timeout in seconds")
    try:
        run(parser.parse_args())
    except DemoError as exc:
        print(f"\ndemo setup failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
