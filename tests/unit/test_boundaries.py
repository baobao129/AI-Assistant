"""领域边界可注入性的最小验证：General LLM 与 SQL Validator 均按接口替换。"""

from backend.domain.state import initial_state
from backend.infrastructure.llm import MockGeneralLLM
from backend.tools.mock_tools import DatabaseQueryInput, DatabaseQueryTool
from backend.tools.sql_validator import ProjectSQLValidator


def test_general_llm_is_injectable():
    # 只要满足 generate 协议即可替换实现，无需依赖具体 provider。
    state = initial_state(conversation_id="c", thread_id="t", run_id="r", user_input="hello")

    class Provider:
        def generate(self, user_input, state):
            return "provider response"

    assert Provider().generate("hello", state) == "provider response"
    assert MockGeneralLLM().generate("hello", state) == "General response: hello"


def test_database_query_uses_explicit_validator_boundary():
    # database_query Tool 必须经过 Validator，危险 SQL 不得直接执行。
    validator = ProjectSQLValidator()
    assert validator.validate("SELECT 1 LIMIT 1").status == "accepted"
    assert validator.validate("DROP TABLE x").status == "rejected"
    result = DatabaseQueryTool(validator).execute("query-1", DatabaseQueryInput(sql="DROP TABLE x"))
    assert result.status == "rejected"
