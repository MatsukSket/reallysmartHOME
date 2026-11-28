from pydantic import BaseModel, ConfigDict, Field


class ManualAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    reply: str = Field(min_length=1, max_length=1600)
