import pytest

from backend.graph.policies import ExecutionBudgets


def test_budgets_are_independent_and_bounded():
    budgets = ExecutionBudgets()
    assert (
        budgets.max_tool_iterations,
        budgets.max_repair_attempts,
        budgets.max_answer_revisions,
    ) == (8, 3, 1)
    assert budgets.can_continue_tools(7) and not budgets.can_continue_tools(8)
    assert budgets.can_repair(2) and not budgets.can_repair(3)
    assert budgets.can_revise(0) and not budgets.can_revise(1)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_tool_iterations": 9},
        {"max_repair_attempts": 4},
        {"max_answer_revisions": 2},
    ],
)
def test_budget_limits_are_rejected(kwargs):
    with pytest.raises(ValueError):
        ExecutionBudgets(**kwargs)
