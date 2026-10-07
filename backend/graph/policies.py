from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionBudgets:
    """Graph 的独立执行预算。

    三个计数器各有独立所有者与额度，互不消耗：``max_tool_iterations`` 统计 SQL
    Agent 的 Tool Call 次数，``max_repair_attempts`` 统计同一问题在一 Run 内的修复
    次数，``max_answer_revisions`` 统计单个答案候选的修订次数。
    """

    max_tool_iterations: int = 8
    max_repair_attempts: int = 3
    max_answer_revisions: int = 1

    def __post_init__(self) -> None:
        # 构造时即校验上下界，避免任何无限循环或超预算配置进入运行态。
        if not 1 <= self.max_tool_iterations <= 8:
            raise ValueError("max_tool_iterations must be between 1 and 8")
        if not 0 <= self.max_repair_attempts <= 3:
            raise ValueError("max_repair_attempts must be between 0 and 3")
        if not 0 <= self.max_answer_revisions <= 1:
            raise ValueError("max_answer_revisions must be between 0 and 1")

    def can_continue_tools(self, iteration_count: int) -> bool:
        return iteration_count < self.max_tool_iterations

    def can_repair(self, attempt_count: int) -> bool:
        return attempt_count < self.max_repair_attempts

    def can_revise(self, revision_count: int) -> bool:
        return revision_count < self.max_answer_revisions
