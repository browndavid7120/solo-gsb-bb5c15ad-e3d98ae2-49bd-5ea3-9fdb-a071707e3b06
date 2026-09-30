from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class RegisterIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)


class SealIn(BaseModel):
    body: str = Field(min_length=1, max_length=1_000_000)
    witnesses: list[str] = Field(min_length=3, max_length=3)


class OpenRequestIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    deadline: datetime  # 必须带时区，例如 2026-10-01T12:00:00Z

    @field_validator("deadline")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("deadline 必须带时区信息，例如 2026-10-01T12:00:00Z")
        return v


class ApproveIn(BaseModel):
    digest: str = Field(min_length=1, max_length=128)


class RevokeIn(BaseModel):
    reason: str = Field(default="", max_length=2000)
