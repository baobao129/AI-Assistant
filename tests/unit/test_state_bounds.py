"""Graph State 集合的「有界追加」语义测试。

依据：GRAPH_CONTRACT Graph-006 要求 Tool Result 必须 append 到 ``tool_results``；
STATE_CONTRACT 要求 State 集合必须有界。两者由 ``bounded_add`` reducer 同时满足。
"""

from __future__ import annotations

from typing import get_type_hints

from backend.domain.state import (
    MESSAGES_LIMIT,
    TOOL_RESULTS_LIMIT,
    GraphState,
    bounded_add,
)


def test_bounded_add_appends_and_keeps_the_newest_items():
    reducer = bounded_add(3)
    assert reducer([1, 2], [3]) == [1, 2, 3]
    assert reducer([1, 2, 3], [4, 5]) == [3, 4, 5]
    assert reducer(None, [1]) == [1]
    assert reducer([1], None) == [1]


def test_limits_cover_the_maximum_tool_loop_budget():
    # 上限必须大于单次 Run 的最大工具循环（8），否则计数会在预算耗尽前被截断。
    assert TOOL_RESULTS_LIMIT >= 8
    assert MESSAGES_LIMIT >= TOOL_RESULTS_LIMIT


def test_messages_and_tool_results_declare_reducers_but_tool_calls_does_not():
    hints = get_type_hints(GraphState, include_extras=True)
    assert getattr(hints["messages"], "__metadata__", ())
    assert getattr(hints["tool_results"], "__metadata__", ())
    # tool_calls 保持替换语义：tool_result_normalize 依赖返回 [] 清空当前建议。
    assert not getattr(hints["tool_calls"], "__metadata__", ())
