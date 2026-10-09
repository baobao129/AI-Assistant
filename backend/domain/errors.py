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
    """显式的瞬时失败，可进入 Graph/Application 的 repair 预算。"""


class ExecutionOwnershipLost(Exception):
    """执行者已失去执行所有权（CR-RUNTIME-20261009-01 / ADR-035）。

    它不是对外的 API 错误：它是「该执行者不得继续写入」的内部信号。
    提交点（事件、等待态、终态、promote、清理）在所有权校验失败时抛出它，
    调用方必须放弃写入，而不是把它转换成用户可见的失败。
    """

    def __init__(self, subject_id: str) -> None:
        super().__init__(f"Execution ownership was lost: {subject_id}")
        self.subject_id = subject_id


class AppError(Exception):
    """应用层失败，携带规范化的错误码与映射后的 HTTP 状态码。"""

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
