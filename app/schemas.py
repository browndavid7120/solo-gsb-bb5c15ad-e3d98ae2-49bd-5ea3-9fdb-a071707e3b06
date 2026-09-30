from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class RegisterIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)


class SealIn(BaseModel):
    body: str = Field(min_length=1, max_length=1_000_000)
    witnesses: list[str] = Field(min_length=3, max_length=3)


def _must_be_aware(name: str, example: str):
    def _validate(v: datetime) -> datetime:
        if v is None:
            return v
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError(f"{name} 必须带时区信息，例如 {example}")
        return v

    return _validate


class OpenRequestIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    deadline: datetime  # 必须带时区，例如 2026-10-01T12:00:00Z
    # 可选：最早开封时刻。不填则沿用“两名不同见证人同意即开封”；
    # 填写后两票在该时刻前集齐只进入 scheduled（待开封），到点才开封。
    earliest_open_at: datetime | None = Field(default=None)

    @field_validator("deadline")
    @classmethod
    def _deadline_aware(cls, v: datetime) -> datetime:
        return _must_be_aware("deadline", "2026-10-01T12:00:00Z")(v)

    @field_validator("earliest_open_at")
    @classmethod
    def _earliest_aware(cls, v: datetime | None) -> datetime | None:
        return _must_be_aware("earliest_open_at", "2026-10-01T12:00:00Z")(v)


class ApproveIn(BaseModel):
    digest: str = Field(min_length=1, max_length=128)


class RevokeIn(BaseModel):
    reason: str = Field(default="", max_length=2000)
