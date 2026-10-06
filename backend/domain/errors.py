from pydantic import BaseModel, ConfigDict, Field


class SafeError(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: str
    message: str = Field(max_length=500)


class RecoverableFailure(Exception):
    """Explicit transient failure eligible for the Graph/Application repair budget."""
