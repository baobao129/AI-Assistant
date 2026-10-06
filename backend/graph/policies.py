from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionBudgets:
    max_tool_iterations: int = 8
    max_repair_attempts: int = 3
    max_answer_revisions: int = 1

    def __post_init__(self) -> None:
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
