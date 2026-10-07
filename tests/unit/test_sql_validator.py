"""SQL Validator 策略测试。

下面这些长语句被刻意保留在同一物理行内，以便校验的文本与用例断言完全一致；
E501 豁免被限定在本模块范围内。
"""

# ruff: noqa: E501

from backend.tools.sql_validator import ProjectSQLValidator

VALIDATOR = ProjectSQLValidator(
    allowed_tables=("sales_orders", "procurement_orders", "products"),
    database="enterprise_agent",
)

ACCEPTED = [
    "SELECT order_id, amount FROM sales_orders LIMIT 10",
    "SELECT p.name, SUM(o.amount) FROM sales_orders o JOIN products p ON o.product_id = p.product_id GROUP BY p.name LIMIT 25",
    "SELECT order_id FROM sales_orders WHERE amount > 100 ORDER BY amount DESC LIMIT 5",
    "SELECT COUNT(*) FROM procurement_orders LIMIT 1",
    "WITH recent AS (SELECT order_id FROM sales_orders LIMIT 10) SELECT order_id FROM recent LIMIT 10",
    "SELECT order_id FROM enterprise_agent.sales_orders LIMIT 3",
]

# 键为待校验 SQL，值为期望的拒绝原因码之一。
REJECTED = {
    "": "empty_statement",
    "SELECT FROM WHERE": "unparseable_statement",
    "SELECT 1; SELECT 2": "multi_statement_blocked",
    "SELECT 1; DROP TABLE sales_orders": "multi_statement_blocked",
    "DROP TABLE sales_orders": "read_only_statement_required",
    "DELETE FROM sales_orders WHERE order_id = 1": "read_only_statement_required",
    "UPDATE sales_orders SET amount = 0": "read_only_statement_required",
    "INSERT INTO sales_orders (order_id) VALUES (1)": "read_only_statement_required",
    "CREATE TABLE t (a INT)": "read_only_statement_required",
    "SELECT order_id INTO OUTFILE '/tmp/x' FROM sales_orders": "file_write_blocked",
    "SELECT SLEEP(10) FROM sales_orders LIMIT 1": "dangerous_function_blocked",
    "SELECT BENCHMARK(1000000, MD5('x')) FROM sales_orders LIMIT 1": "dangerous_function_blocked",
    "SELECT LOAD_FILE('/etc/passwd') FROM sales_orders LIMIT 1": "dangerous_function_blocked",
    "SELECT * FROM sales_orders LIMIT 5": "select_star_blocked",
    "SELECT o.* FROM sales_orders o LIMIT 5": "select_star_blocked",
    "SELECT order_id FROM users LIMIT 5": "table_not_allowed",
    "SELECT order_id FROM other_db.sales_orders LIMIT 5": "schema_not_allowed",
    "SELECT order_id FROM information_schema.tables LIMIT 5": "schema_not_allowed",
    "SELECT order_id FROM sales_orders": "limit_required",
    "SELECT order_id FROM sales_orders LIMIT 100000": "limit_exceeds_maximum",
    "SELECT order_id FROM sales_orders LIMIT 10 OFFSET 5": "offset_not_supported",
    "SELECT o.order_id FROM sales_orders o JOIN products p ON o.product_id = p.product_id JOIN procurement_orders q ON q.product_id = p.product_id JOIN products p2 ON p2.product_id = p.product_id JOIN sales_orders o2 ON o2.order_id = o.order_id LIMIT 5": "too_many_joins",
    "SELECT (SELECT COUNT(*) FROM sales_orders) AS a, (SELECT COUNT(*) FROM products) AS b, (SELECT COUNT(*) FROM procurement_orders) AS c LIMIT 1": "too_many_subqueries",
    "SELECT a.order_id FROM sales_orders a LIMIT 1 UNION SELECT b.order_id FROM sales_orders b LIMIT 1 UNION SELECT c.order_id FROM sales_orders c LIMIT 1 UNION SELECT d.order_id FROM sales_orders d LIMIT 1": "too_many_unions",
    "x" * 5000: "statement_too_long",
}


def test_accepted_statements():
    for sql in ACCEPTED:
        result = VALIDATOR.validate(sql)
        assert result.status == "accepted", (sql, result.reasons)


def test_rejected_statements_report_expected_reason():
    for sql, reason in REJECTED.items():
        result = VALIDATOR.validate(sql)
        assert result.status == "rejected", (sql, result.reasons)
        assert reason in result.reasons, (sql, reason, result.reasons)


def test_wide_select_projection_is_bounded():
    columns = ", ".join(f"col_{index}" for index in range(50))
    result = VALIDATOR.validate(f"SELECT {columns} FROM sales_orders LIMIT 5")
    assert result.status == "rejected"
    assert "too_many_selected_expressions" in result.reasons


def test_complexity_score_rejects_deeply_nested_aggregation():
    sql = (
        "SELECT order_id FROM sales_orders WHERE amount > (SELECT AVG(amount) FROM sales_orders "
        "WHERE amount > (SELECT MIN(amount) FROM sales_orders WHERE amount < "
        "(SELECT MAX(amount) FROM sales_orders LIMIT 1) LIMIT 1) LIMIT 1) "
        "ORDER BY amount DESC LIMIT 10"
    )
    result = VALIDATOR.validate(sql)
    assert result.status == "rejected"
    assert {"too_many_subqueries", "complexity_limit_exceeded"} & set(result.reasons)


def test_validator_never_returns_sql_text_in_reasons():
    # 拒绝原因中不得回显原始 SQL 片段，避免泄漏表名等敏感信息。
    result = VALIDATOR.validate("SELECT * FROM users LIMIT 5")
    assert "users" not in " ".join(result.reasons)


def test_validator_reports_every_independent_violation():
    # 多个独立违规必须全部上报，而不是遇到第一个就短路返回。
    result = VALIDATOR.validate("SELECT * FROM users LIMIT 100000")
    assert result.status == "rejected"
    assert {"select_star_blocked", "table_not_allowed", "limit_exceeds_maximum"} <= set(
        result.reasons
    )
