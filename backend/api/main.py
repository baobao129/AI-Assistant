import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from backend.api.schemas import HealthResponse
from backend.application.health import HealthProbe
from backend.infrastructure.config import Settings
from backend.infrastructure.health import DependencyHealthProbe
from backend.infrastructure.logging import configure_logging


def create_app(settings: Settings | None = None, probe: HealthProbe | None = None) -> FastAPI:
    config = settings or Settings()
    logger = configure_logging(config.log_level)
    health_probe = probe or DependencyHealthProbe(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("application_started")
        yield
        logger.info("application_stopped")

    app = FastAPI(title="Enterprise Multi-Source AI Agent", lifespan=lifespan)

    def error_response(request: Request, status: int, code: str, message: str) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "request_id": request.state.request_id,
                    "details": {},
                }
            },
            headers={"X-Request-ID": request.state.request_id},
        )

    @app.middleware("http")
    async def correlate(request: Request, call_next):
        proposed = request.headers.get("X-Request-ID", "")
        request.state.request_id = (
            proposed if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", proposed) else uuid4().hex
        )
        try:
            response = await call_next(request)
        except Exception:
            logger.error("request_failed", extra={"request_id": request.state.request_id})
            response = error_response(request, 500, "internal_error", "Request could not complete.")
        response.headers["X-Request-ID"] = request.state.request_id
        logger.info(
            "request_completed",
            extra={"request_id": request.state.request_id, "status_code": response.status_code},
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(request, 422, "validation_error", "Invalid field values.")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "validation_error"
        return error_response(request, exc.status_code, code, "Request is unavailable.")

    @app.get("/api/v1/health/live")
    def live() -> dict[str, str]:
        return {"status": "healthy"}

    @app.get("/api/v1/health", response_model=HealthResponse)
    def health() -> JSONResponse:
        services = {"backend": "healthy", **health_probe.check()}
        ready = all(value == "healthy" for value in services.values())
        result = HealthResponse(status="healthy" if ready else "degraded", services=services)
        return JSONResponse(status_code=200 if ready else 503, content=result.model_dump())

    return app


app = create_app()
