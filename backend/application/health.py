from typing import Protocol


class HealthProbe(Protocol):
    """健康检查探针协议：返回组件名到其状态的映射。"""

    def check(self) -> dict[str, str]: ...
