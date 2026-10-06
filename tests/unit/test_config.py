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
    assert "synthetic-test-value" not in repr(settings)


@pytest.mark.parametrize(
    "field, value",
    [("max_tool_iterations", 9), ("max_repair_attempts", 4), ("max_answer_revisions", 2)],
)
def test_limits_cannot_exceed_contract(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
