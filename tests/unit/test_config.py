"""Settings 的独立预算上限与密钥脱敏行为测试。"""

import pytest
from pydantic import ValidationError

from backend.infrastructure.config import Settings


def test_independent_limits_and_secret_redaction():
    settings = Settings(_env_file=None, mysql_password="synthetic-test-value")
    assert (
        settings.max_tool_iterations,
        settings.max_repair_attempts,
        settings.max_answer_revisions,
    ) == (8, 3, 1)
    # 口令属于机密，禁止出现在 repr/日志中。
    assert "synthetic-test-value" not in repr(settings)


@pytest.mark.parametrize(
    "field, value",
    [("max_tool_iterations", 9), ("max_repair_attempts", 4), ("max_answer_revisions", 2)],
)
def test_limits_cannot_exceed_contract(field, value):
    # 三个预算分别越界都应触发校验错误，证明约束相互独立。
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
