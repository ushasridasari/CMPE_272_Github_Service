"""Application entry point.
Done by Sainath
Builds the ASGI app, installs the request-id middleware, registers the
exception handlers that guarantee a single error envelope, and manages the
lifetimes of the HTTP client and the event store.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import Settings, get_settings
from app.errors import AppError, BadRequestError, NotFoundError
from app.github.cache import ETagCache
from app.github.client import GitHubClient
from app.logging_config import configure_logging, request_id_var
from app.routes import health, issues, webhook
from app.webhooks.store import EventStore

logger = logging.getLogger(__name__)

DESCRIPTION = """
A gateway over the GitHub Issues REST API for a single configured repository.

* Issue create / list / read / update / close and issue comments.
* Signed GitHub webhook intake (`issues`, `issue_comment`, `ping`) with
  idempotent storage and a `/events` inspection endpoint.
* GitHub credentials never leave the server; clients of this API do not need
  a GitHub token.

**On delete:** GitHub's REST API cannot delete an issue, so the "D" of CRUD is
implemented as a state transition to `closed`.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(settings.log_level)

    http_client = httpx.AsyncClient(
        base_url=settings.github_api_base,
        timeout=settings.request_timeout_seconds,
    )
    app.state.github = GitHubClient(settings, client=http_client, cache=ETagCache())
    app.state.store = EventStore(settings.events_db_path)
    logger.info(
        "service_started",
        extra={"repo": settings.repo_path, "port": settings.port},
    )
    try:
        yield
    finally:
        await http_client.aclose()
        app.state.store.close()
        logger.info("service_stopped")


def _error_response(request: Request, error: AppError) -> JSONResponse:
    headers = dict(error.headers)
    headers["X-Request-ID"] = request_id_var.get()
    return JSONResponse(
        status_code=error.status_code, content=error.to_payload(), headers=headers
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory. Tests build their own instance through this."""
    settings = settings or get_settings()

    app = FastAPI(
        title="GitHub Issues Gateway",
        version=health.VERSION,
        description=DESCRIPTION,
        lifespan=lifespan,
        openapi_url="/openapi.json",
        docs_url="/docs",
    )
    app.state.settings = settings

    # -- middleware --------------------------------------------------------
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        incoming = request.headers.get("X-Request-ID")
        rid = incoming or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = rid
            # Logged inside the context so the access line carries the id;
            # resetting first would emit "-" and break correlation.
            logger.info(
                "http_request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "delivery_id": request.headers.get("X-GitHub-Delivery"),
                },
            )
            return response
        finally:
            request_id_var.reset(token)

    # -- exception handlers ------------------------------------------------
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        log = logger.warning if exc.status_code < 500 else logger.error
        log(
            "request_failed",
            extra={"code": exc.code, "status": exc.status_code, "detail": exc.message},
        )
        return _error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Reshape FastAPI's 422 into our documented 400 envelope."""
        details = []
        for err in exc.errors():
            location = [str(part) for part in err.get("loc", []) if part not in ("body",)]
            details.append(
                {"field": ".".join(location) or None, "message": err.get("msg", "invalid value")}
            )
        return _error_response(
            request,
            BadRequestError("Request failed validation.", details=details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        if exc.status_code == 404:
            return _error_response(request, NotFoundError("No route matches this path."))
        return _error_response(
            request,
            AppError(str(exc.detail), status_code=exc.status_code, code="http_error"),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # 5xx is reserved for genuine server faults, and the client is told
        # nothing about the internals beyond a correlation id.
        logger.exception("unhandled_exception", extra={"path": request.url.path})
        return _error_response(
            request,
            AppError("An unexpected internal error occurred.", status_code=500),
        )

    # -- routes ------------------------------------------------------------
    app.include_router(health.router)
    app.include_router(issues.router)
    app.include_router(webhook.router)

    return app


#: Module-level ASGI app, referenced by uvicorn as ``app.main:app``.
app = create_app()


def main() -> None:  # pragma: no cover - process entry point
    import uvicorn

    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        "app.main:app", host="0.0.0.0", port=settings.port, log_config=None, access_log=False
    )


if __name__ == "__main__":  # pragma: no cover
    main()
