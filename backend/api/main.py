"""FastAPI application factory: health, authentication, Conversation, Run, SSE, Approval,
Document, Ingestion, and Structured Data APIs (``docs/contracts/API_CONTRACT.md``).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from backend.api.dependencies import Container, RunDispatcher, build_container
from backend.api.routers import approvals, auth, conversations, data, runs
from backend.api.schemas import ErrorResponse, HealthResponse
from backend.application.health import HealthProbe
from backend.domain.errors import AppError
from backend.infrastructure.config import Settings
from backend.infrastructure.health import DependencyHealthProbe
from backend.infrastructure.logging import configure_logging

APP_ERROR_STATUS_CODES = {
    400: "validation_error",
    401: "authentication_error",
    403: "authorization_error",
    404: "not_found",
    409: "conflict",
    422: "validation_error",
    429: "rate_limit",
    500: "internal_error",
    503: "internal_error",
}


def create_app(
    settings: Settings | None = None,
    probe: HealthProbe | None = None,
    container: Container | None = None,
) -> FastAPI:
    config = settings or Settings()
    logger = configure_logging(config.log_level)
    health_probe = probe or DependencyHealthProbe(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active = container or build_container(config)
        app.state.container = active
        app.state.dispatcher = RunDispatcher(container=active)
        maintenance = getattr(active, "maintenance", None)
        if maintenance is not None:
            maintenance.start()
        logger.info("application_started")
        try:
            yield
        finally:
            active.close()
            logger.info("application_stopped")

    app = FastAPI(title="Enterprise Multi-Source AI Agent", lifespan=lifespan)

    def error_response(request: Request, status: int, code: str, message: str) -> JSONResponse:
        request_id = getattr(request.state, "request_id", uuid4().hex)
        body = ErrorResponse.model_validate(
            {
                "error": {
                    "code": code,
                    "message": message,
                    "request_id": request_id,
                    "details": {},
                }
            }
        )
        return JSONResponse(
            status_code=status,
            content=body.model_dump(),
            headers={"X-Request-ID": request_id},
        )

    @app.middleware("http")
    async def correlate(request: Request, call_next):
        proposed = request.headers.get("X-Request-ID", "")
        request.state.request_id = (
            proposed if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", proposed) else uuid4().hex
        )
        try:
            response = await call_next(request)
        except AppError as error:
            response = error_response(request, error.status_code, error.code, error.message)
        except Exception:
            logger.error("request_failed", extra={"request_id": request.state.request_id})
            response = error_response(request, 500, "internal_error", "Request could not complete.")
        response.headers["X-Request-ID"] = request.state.request_id
        logger.info(
            "request_completed",
            extra={"request_id": request.state.request_id, "status_code": response.status_code},
        )
        return response

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError) -> JSONResponse:
        return error_response(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(request, 422, "validation_error", "Invalid field values.")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code = APP_ERROR_STATUS_CODES.get(exc.status_code, "internal_error")
        message = "Request is unavailable." if exc.status_code == 404 else "Request was rejected."
        return error_response(request, exc.status_code, code, message)

    @app.get("/api/v1/health/live")
    def live() -> dict[str, str]:
        return {"status": "healthy"}

    @app.get("/api/v1/health", response_model=HealthResponse)
    def health() -> JSONResponse:
        services = {"backend": "healthy", **health_probe.check()}
        ready = all(value == "healthy" for value in services.values())
        result = HealthResponse(status="healthy" if ready else "degraded", services=services)
        return JSONResponse(status_code=200 if ready else 503, content=result.model_dump())

    api = "/api/v1"
    app.include_router(auth.router, prefix=api)
    app.include_router(conversations.router, prefix=api)
    app.include_router(runs.router, prefix=api)
    app.include_router(approvals.router, prefix=api)
    app.include_router(data.router, prefix=api)
    return app


app = create_app()
