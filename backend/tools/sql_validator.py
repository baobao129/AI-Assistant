from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SQLValidation:
    status: str
    reason: str | None = None


class SQLValidator(Protocol):
    def validate(self, sql: str) -> SQLValidation: ...


class DevelopmentSQLValidator:
    """Phase 1 boundary stub; full AST/schema/complexity policy belongs to Phase 4."""

    def validate(self, sql: str) -> SQLValidation:
        normalized = sql.strip().lower()
        if ";" in normalized.rstrip("; "):
            return SQLValidation("rejected", "multi_statement")
        if not normalized.startswith("select"):
            return SQLValidation("rejected", "read_only_statement_required")
        return SQLValidation("accepted")
