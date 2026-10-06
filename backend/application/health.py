from typing import Protocol


class HealthProbe(Protocol):
    def check(self) -> dict[str, str]: ...
