"""SQL Validator: the mandatory boundary between the LLM and the read-only database.

Policy source: ``docs/contracts/TOOL_CONTRACT.md`` TOOL-003 and
``docs/contracts/GRAPH_CONTRACT.md`` Graph-005. The validator only ever reads a
proposed statement; it never executes SQL and never touches Graph State.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

SQLValidationStatus = Literal["accepted", "rejected"]

DIALECT = "mysql"

# Statement allowlist: only plain reads may reach the database driver.
ALLOWED_STATEMENTS: tuple[type[exp.Expression], ...] = (exp.Select, exp.Union)

# Functions that can stall the server, read the filesystem, or escape the
# allowlisted query scope. Names are compared case-insensitively.
FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset(
    {
        "sleep",
        "benchmark",
        "load_file",
        "get_lock",
        "release_lock",
        "is_free_lock",
        "is_used_lock",
        "master_pos_wait",
        "sys_exec",
        "sys_eval",
        "uuid_short",
        "last_insert_id",
        "found_rows",
        "row_count",
        "current_user",
        "session_user",
        "system_user",
        "user",
        "version",
        "database",
        "schema",
        "connection_id",
        "inet_aton",
        "inet_ntoa",
    }
)

# Non-function nodes that always indicate a write, DDL, or file side effect.
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Into,
    exp.Command,
    exp.Set,
    exp.Use,
    exp.Lock,
    exp.Grant,
)

# A structured-data query must not read across databases or system catalogs.
FORBIDDEN_SCHEMA_PREFIXES: frozenset[str] = frozenset(
    {"information_schema", "mysql", "performance_schema", "sys"}
)


@dataclass(frozen=True)
class SQLValidation:
    """Result of one validator pass; ``reasons`` never contain SQL text or secrets."""

    status: SQLValidationStatus
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reason(self) -> str | None:
        """Primary rejection code, kept for single-reason callers."""

        return self.reasons[0] if self.reasons else None

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"


class SQLValidator(Protocol):
    def validate(self, sql: str) -> SQLValidation: ...


class ProjectSQLValidator:
    """AST, allowlist, scope, complexity, and limit enforcement for one statement."""

    def __init__(
        self,
        *,
        allowed_tables: tuple[str, ...] = (),
        database: str = "",
        max_sql_length: int = 4000,
        max_rows: int = 100,
        max_joins: int = 3,
        max_subqueries: int = 2,
        max_unions: int = 2,
        max_select_expressions: int = 40,
        max_complexity_score: int = 60,
    ) -> None:
        self.allowed_tables = frozenset(name.casefold() for name in allowed_tables)
        self.database = database.casefold()
        self.max_sql_length = max_sql_length
        self.max_rows = max_rows
        self.max_joins = max_joins
        self.max_subqueries = max_subqueries
        self.max_unions = max_unions
        self.max_select_expressions = max_select_expressions
        self.max_complexity_score = max_complexity_score

    def validate(self, sql: str) -> SQLValidation:
        reasons: list[str] = []
        text = sql.strip()
        if not text:
            return SQLValidation("rejected", ("empty_statement",))
        if len(text) > self.max_sql_length:
            return SQLValidation("rejected", ("statement_too_long",))

        lowered = text.casefold()
        if "outfile" in lowered or "dumpfile" in lowered:
            return SQLValidation("rejected", ("file_write_blocked",))

        try:
            statements = sqlglot.parse(text, read=DIALECT)
        except ParseError:
            return SQLValidation("rejected", ("unparseable_statement",))
        except (ValueError, RecursionError):
            return SQLValidation("rejected", ("unparseable_statement",))

        if len(statements) != 1:
            return SQLValidation("rejected", ("multi_statement_blocked",))
        statement = statements[0]
        if statement is None:
            return SQLValidation("rejected", ("empty_statement",))

        if not isinstance(statement, ALLOWED_STATEMENTS):
            return SQLValidation("rejected", ("read_only_statement_required",))

        for node_type in FORBIDDEN_NODES:
            if statement.find(node_type) is not None:
                reasons.append("write_or_ddl_blocked")
                break

        ctes = {
            cte.alias_or_name.casefold() for cte in statement.find_all(exp.CTE) if cte.alias_or_name
        }
        tables = [
            table
            for table in statement.find_all(exp.Table)
            if table.name and table.name.casefold() not in ctes
        ]
        for table in tables:
            name = table.name.casefold()
            schema = (table.db or "").casefold()
            catalog = (table.catalog or "").casefold()
            if name not in self.allowed_tables:
                reasons.append("table_not_allowed")
            if schema and schema not in {self.database, ""}:
                reasons.append("schema_not_allowed")
            if catalog and catalog not in {self.database, ""}:
                reasons.append("schema_not_allowed")
            if schema in FORBIDDEN_SCHEMA_PREFIXES or name in FORBIDDEN_SCHEMA_PREFIXES:
                reasons.append("schema_not_allowed")

        for node in statement.walk():
            if isinstance(node, exp.Anonymous):
                if str(node.name).casefold() in FORBIDDEN_FUNCTIONS:
                    reasons.append("dangerous_function_blocked")
            elif isinstance(node, exp.Func):
                if node.sql_name().casefold() in FORBIDDEN_FUNCTIONS:
                    reasons.append("dangerous_function_blocked")

        selects = list(statement.find_all(exp.Select))
        if any(self._has_unrestricted_projection(select) for select in selects):
            reasons.append("select_star_blocked")

        joins = len(list(statement.find_all(exp.Join)))
        if joins > self.max_joins:
            reasons.append("too_many_joins")

        subqueries = len(list(statement.find_all(exp.Subquery))) + len(
            list(statement.find_all(exp.CTE))
        )
        if subqueries > self.max_subqueries:
            reasons.append("too_many_subqueries")

        unions = len(list(statement.find_all(exp.Union)))
        if unions > self.max_unions:
            reasons.append("too_many_unions")

        widest = max((len(select.expressions) for select in selects), default=0)
        if widest > self.max_select_expressions:
            reasons.append("too_many_selected_expressions")

        score = self._complexity_score(statement, joins=joins, subqueries=subqueries, unions=unions)
        if score > self.max_complexity_score:
            reasons.append("complexity_limit_exceeded")

        limit = statement.args.get("limit")
        if statement.args.get("offset") is not None:
            reasons.append("offset_not_supported")
        if limit is None:
            reasons.append("limit_required")
        else:
            bound = self._limit_value(limit)
            if bound is None:
                reasons.append("limit_must_be_literal")
            elif bound > self.max_rows:
                reasons.append("limit_exceeds_maximum")

        if reasons:
            return SQLValidation("rejected", tuple(dict.fromkeys(reasons)))
        return SQLValidation("accepted")

    @staticmethod
    def _complexity_score(
        statement: exp.Expression, *, joins: int, subqueries: int, unions: int
    ) -> int:
        nodes = sum(1 for _ in statement.walk())
        aggregates = len(list(statement.find_all(exp.AggFunc)))
        order_by = 1 if statement.args.get("order") is not None else 0
        group_by = 1 if statement.args.get("group") is not None else 0
        return (
            nodes // 5
            + joins * 5
            + subqueries * 8
            + unions * 6
            + aggregates * 2
            + (order_by + group_by) * 2
        )

    @staticmethod
    def _has_unrestricted_projection(select: exp.Select) -> bool:
        """True when a projection expands every column; ``COUNT(*)`` stays allowed."""

        for projection in select.expressions:
            target = projection.this if isinstance(projection, exp.Alias) else projection
            if isinstance(target, (exp.Star, exp.Column)):
                if target.find(exp.Star) is not None:
                    return True
        return False

    @staticmethod
    def _limit_value(limit: exp.Expression) -> int | None:
        expression = limit.args.get("expression") if isinstance(limit, exp.Limit) else limit
        if isinstance(expression, exp.Literal) and not expression.is_string:
            try:
                return int(expression.name)
            except (TypeError, ValueError):
                return None
        return None


def build_validator(settings: object) -> ProjectSQLValidator:
    """Build the validator from typed settings without importing Settings at module import."""

    return ProjectSQLValidator(
        allowed_tables=tuple(getattr(settings, "allowed_table_names", ())),
        database=str(getattr(settings, "mysql_readonly_database", "")),
        max_sql_length=int(getattr(settings, "sql_max_length", 4000)),
        max_rows=int(getattr(settings, "sql_max_rows", 100)),
        max_joins=int(getattr(settings, "sql_max_joins", 3)),
        max_subqueries=int(getattr(settings, "sql_max_subqueries", 2)),
        max_unions=int(getattr(settings, "sql_max_unions", 2)),
        max_select_expressions=int(getattr(settings, "sql_max_select_expressions", 40)),
        max_complexity_score=int(getattr(settings, "sql_max_complexity_score", 60)),
    )
