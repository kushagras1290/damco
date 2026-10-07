"""Production entrypoint: ``python -m jobpulse.serve``.

Validates configuration *before* binding (fail fast), honours the platform-provided
``PORT`` (Render injects one), and makes proxy-header trust explicit configuration
instead of a hardcoded ``--forwarded-allow-ips *``.
"""

from __future__ import annotations

import sys

import structlog
import uvicorn
from pydantic import ValidationError

from jobpulse.core.config import get_settings
from jobpulse.core.logging import configure_logging

GRACEFUL_SHUTDOWN_SECONDS = 20
KEEP_ALIVE_SECONDS = 75  # above typical load-balancer idle timeouts (60 s)

logger = structlog.get_logger(__name__)


def main() -> None:
    try:
        settings = get_settings()
    except ValidationError as exc:
        sys.stderr.write(f"invalid configuration:\n{exc}\n")
        sys.exit(2)
    configure_logging(level=settings.log_level, json_logs=settings.log_json)
    logger.info("api.serve", port=settings.port, proxy_headers=settings.trusted_proxy_count > 0)
    uvicorn.run(
        "jobpulse.main:create_app",
        factory=True,
        host=settings.bind_host,
        port=settings.port,
        proxy_headers=settings.trusted_proxy_count > 0,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        server_header=False,
        date_header=True,
        access_log=False,  # RequestContextMiddleware emits structured access logs
        timeout_keep_alive=KEEP_ALIVE_SECONDS,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        log_config=None,
    )


if __name__ == "__main__":
    main()
