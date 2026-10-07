"""SQL Validator：LLM 与 read-only 数据库之间的强制边界。

策略来源：``docs/contracts/TOOL_CONTRACT.md`` TOOL-003 与
``docs/contracts/GRAPH_CONTRACT.md`` Graph-005。验证器只读取被提议的语句，
从不执行 SQL，也从不接触 Graph State。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

SQLValidationStatus = Literal["accepted", "rejected"]

DIALECT = "mysql"

# 语句白名单：只有纯读取语句才可能到达数据库驱动。
ALLOWED_STATEMENTS: tuple[type[exp.Expression], ...] = (exp.Select, exp.Union)

# 可能拖垮服务器、读取文件系统或逃逸白名单查询范围的函数。
# 名称按大小写不敏感比较。
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

# 非函数节点，这些节点总是指向写入、DDL 或文件副作用。
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

# 结构化数据查询不得跨数据库读取，也不得读取系统目录。
FORBIDDEN_SCHEMA_PREFIXES: frozenset[str] = frozenset(
    {"information_schema", "mysql", "performance_schema", "sys"}
)


@dataclass(frozen=True)
class SQLValidation:
    """单次验证的结果；``reasons`` 绝不含 SQL 文本或机密。"""

    status: SQLValidationStatus
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reason(self) -> str | None:
        """主拒绝码，为只关心单一原因的调用方保留。"""

        return self.reasons[0] if self.reasons else None

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"


class SQLValidator(Protocol):
    def validate(self, sql: str) -> SQLValidation: ...


class ProjectSQLValidator:
    """针对单条语句的 AST、白名单、作用域、复杂度与 LIMIT 约束。"""

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

        # 先做 AST 解析：只有能解析为单条语句的语法树才继续校验。
        try:
            statements = sqlglot.parse(text, read=DIALECT)
        except ParseError:
            return SQLValidation("rejected", ("unparseable_statement",))
        except (ValueError, RecursionError):
            return SQLValidation("rejected", ("unparseable_statement",))

        # 多语句阻断：任何被分号拼接的额外语句都会直接拒绝，避免绕过白名单。
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

        # 复杂度与 LIMIT 双约束：必须带字面量 LIMIT 且不超过 max_rows，否则拒绝。
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
        """当投影展开所有列时为 True；``COUNT(*)`` 仍然允许。"""

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
    """用 typed settings 构建验证器，且不在模块导入时导入 Settings。"""

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
