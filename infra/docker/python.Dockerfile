# syntax=docker/dockerfile:1.7
# Shared multi-stage build for jobpulse-api and jobpulse-worker.
#   docker build -f infra/docker/python.Dockerfile --target api    -t jobpulse-api .
#   docker build -f infra/docker/python.Dockerfile --target worker -t jobpulse-worker .

ARG PYTHON_VERSION=3.14
ARG UV_VERSION=0.12.3

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ---------------------------------------------------------------- build
FROM python:${PYTHON_VERSION}-slim-trixie AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src

# Dependency layer first (cached unless lockfile / manifests change).
COPY pyproject.toml uv.lock ./
COPY packages/python/pyproject.toml packages/python/pyproject.toml
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY apps/worker/pyproject.toml apps/worker/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace

COPY packages packages
COPY apps/api apps/api
COPY apps/worker apps/worker
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------- runtime base
FROM python:${PYTHON_VERSION}-slim-trixie AS runtime
# The app runs from /opt/venv (built by uv); the base image's pip and its vendored
# libraries (urllib3, msgpack, setuptools) are unused attack surface, so remove them.
RUN python -m pip uninstall --yes --quiet pip \
 && rm -rf /root/.cache \
 && groupadd --system --gid 10001 jobpulse \
 && useradd --system --uid 10001 --gid jobpulse --home-dir /app --shell /usr/sbin/nologin jobpulse \
 && mkdir -p /app /data/snapshots && chown -R jobpulse:jobpulse /app /data
COPY --from=build --chown=jobpulse:jobpulse /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOCAL_STORAGE_PATH=/data/snapshots
WORKDIR /app
USER jobpulse

# ---------------------------------------------------------------- api
FROM runtime AS api
COPY --chown=jobpulse:jobpulse alembic.ini ./alembic.ini
COPY --chown=jobpulse:jobpulse migrations ./migrations
COPY --chown=jobpulse:jobpulse seed.yaml ./seed.yaml
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2).status == 200 else 1)"]
CMD ["uvicorn", "jobpulse.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header", "--timeout-graceful-shutdown", "20"]

# ---------------------------------------------------------------- worker
FROM runtime AS worker
EXPOSE 9464
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9464/metrics', timeout=2).status == 200 else 1)"]
CMD ["jobpulse-worker"]
