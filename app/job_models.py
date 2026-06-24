from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    FINISHED = "finished"
    ERROR = "error"
    CANCELED = "canceled"


class DownloadType(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"


class JobCreate(BaseModel):
    url: str
    download_type: DownloadType
    codec: str = "auto"
    format: str
    quality: str
    subtitle_langs: list[str] = Field(default_factory=list)


class SubtitleFile(BaseModel):
    filename: str
    size: int | None = None
    download_url: str | None = None


class Job(BaseModel):
    id: str
    url: str
    title: str
    download_type: DownloadType
    codec: str
    format: str
    quality: str
    subtitle_langs: list[str] = Field(default_factory=list)
    status: JobStatus
    message: str | None = None
    percent: float | None = None
    speed: float | None = None
    eta: float | None = None
    filename: str | None = None
    download_url: str | None = None
    size: int | None = None
    error: str | None = None
    subtitle_files: list[SubtitleFile] = Field(default_factory=list)
    cancel_requested_at: datetime | None = Field(default=None, exclude=True)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class AddJobRequest(BaseModel):
    url: str
    download_type: Literal["video", "audio"]
    quality: str
    format: str
    codec: str = "auto"
    subtitle_langs: list[str] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def _valid_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("subtitle_langs")
    @classmethod
    def _valid_subtitle_langs(cls, value: list[str]) -> list[str]:
        for lang in value:
            if not lang or not lang.strip():
                raise ValueError("invalid subtitle language code")
        return value

    @model_validator(mode="after")
    def _normalize_codec(self) -> "AddJobRequest":
        if self.download_type == "audio":
            self.codec = "auto"
        return self

    def to_job_create(self) -> JobCreate:
        return JobCreate(
            url=self.url,
            download_type=DownloadType(self.download_type),
            codec=self.codec,
            format=self.format,
            quality=self.quality,
            subtitle_langs=self.subtitle_langs,
        )


class EnqueueJobResult(BaseModel):
    id: str


class JobList(BaseModel):
    queued: list[Job] = Field(default_factory=list)
    done: list[Job] = Field(default_factory=list)


class StatusResponse(BaseModel):
    status: Literal["ok", "error"] = "ok"
    message: str | None = None


class CookieStatusResponse(BaseModel):
    domains: list[str] = Field(default_factory=list)


class CreateJobResponse(BaseModel):
    status: Literal["ok"] = "ok"
    id: str
