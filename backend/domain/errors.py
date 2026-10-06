from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CanonicalErrorCode = Literal[
    "authentication_error",
    "authorization_error",
    "validation_error",
    "not_found",
    "conflict",
    "rate_limit",
    "timeout",
    "unsupported",
    "internal_error",
]


class SafeError(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: str
    message: str = Field(max_length=500)


class RecoverableFailure(Exception):
    """Explicit transient failure eligible for the Graph/Application repair budget."""


class AppError(Exception):
    """Application failure carrying a canonical code and the mapped HTTP status."""

    def __init__(
        self,
        code: CanonicalErrorCode,
        message: str,
        *,
        status_code: int,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


def authentication_error(message: str = "Authentication is required.") -> AppError:
    return AppError("authentication_error", message, status_code=401)


def authorization_error(message: str = "This resource is not available.") -> AppError:
    return AppError("authorization_error", message, status_code=403)


def not_found(message: str = "Resource was not found.") -> AppError:
    return AppError("not_found", message, status_code=404)


def conflict(message: str = "The request conflicts with the current state.") -> AppError:
    return AppError("conflict", message, status_code=409)


def validation_error(message: str = "Invalid field values.") -> AppError:
    return AppError("validation_error", message, status_code=422)


def unsupported(message: str = "The request is not supported.") -> AppError:
    return AppError("unsupported", message, status_code=400)


def internal_error(message: str = "Request could not complete.") -> AppError:
    return AppError("internal_error", message, status_code=500)
