"""HTTP-facing error hierarchy and exception handlers (RFC 9457 problem+json)."""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from jobpulse_core.errors import JobPulseError
from jobpulse_core.errors import ValidationError as DomainValidationError

logger = structlog.get_logger(__name__)

PROBLEM_JSON = "application/problem+json"
MAX_VALIDATION_ERRORS = 20


class ApiError(JobPulseError):
    status_code: int = 500
    code: str = "internal_error"
    # Only errors that opt in expose their context to clients (e.g. which plan limit was hit).
    public_context: bool = False


class AuthenticationError(ApiError):
    status_code = 401
    code = "unauthenticated"


class PermissionDeniedError(ApiError):
    status_code = 403
    code = "forbidden"


class NotFoundError(ApiError):
    status_code = 404
    code = "not_found"


class ConflictError(ApiError):
    status_code = 409
    code = "conflict"


class RateLimitedError(ApiError):
    status_code = 429
    code = "rate_limited"


class ServiceUnavailableError(ApiError):
    status_code = 503
    code = "service_unavailable"


def problem_response(
    status: int,
    code: str,
    detail: str,
    request: Request,
    *,
    extra: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    content: dict[str, object] = {
        "type": f"https://jobpulse.dev/problems/{code}",
        "title": code.replace("_", " ").capitalize(),
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        "request_id": getattr(request.state, "request_id", None),
        **(extra or {}),
    }
    merged_headers = dict(headers or {})
    if status == 401:
        merged_headers.setdefault("WWW-Authenticate", "Bearer")
    return JSONResponse(status_code=status, media_type=PROBLEM_JSON, content=content, headers=merged_headers or None)


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("api.error", code=exc.code, error=exc.message)
        extra = dict(exc.context) if exc.public_context else None
        return problem_response(exc.status_code, exc.code, exc.message, request, extra=extra)

    @app.exception_handler(DomainValidationError)
    async def _domain_validation(request: Request, exc: DomainValidationError) -> JSONResponse:
        return problem_response(422, "validation_error", exc.message, request)

    @app.exception_handler(JobPulseError)
    async def _domain_error(request: Request, exc: JobPulseError) -> JSONResponse:
        logger.error("api.domain_error", error_type=type(exc).__name__, error=exc.message)
        if exc.retryable:
            return problem_response(503, "service_unavailable", "dependency unavailable, retry later", request)
        return problem_response(500, "internal_error", "request failed", request)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": [str(part) for part in err.get("loc", [])], "msg": str(err.get("msg", "invalid"))}
            for err in exc.errors()[:MAX_VALIDATION_ERRORS]
        ]
        return problem_response(422, "validation_error", "request validation failed", request, extra={"errors": errors})

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem_response(exc.status_code, "http_error", str(exc.detail), request)
