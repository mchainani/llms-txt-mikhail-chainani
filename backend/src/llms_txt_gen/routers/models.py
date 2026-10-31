"""HTTP request and response schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, HttpUrl, field_validator

from llms_txt_gen.cron import next_cron_time
from llms_txt_gen.urls import normalize_url


class FetchPageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    cron_expression: str | None = None

    @field_validator("cron_expression")
    @classmethod
    def validate_cron_expression(cls, expression: str | None) -> str | None:
        if expression is not None:
            try:
                next_cron_time(expression)
            except ValueError as error:
                raise ValueError("Invalid cron expression or no future occurrence.") from error
        return expression

    @field_validator("url")
    @classmethod
    def normalize_requested_url(cls, url: HttpUrl) -> HttpUrl:
        return HttpUrl(normalize_url(str(url)))


class RunCreated(BaseModel):
    id: UUID


class RequestInfo(BaseModel):
    id: UUID
    url: str
    created_at: datetime
    timestamp: datetime
    job_id: str | None
    cron_expression: str | None


class FileInfo(BaseModel):
    id: UUID
    type: str
    content: str
    created_at: datetime
    timestamp: datetime


class RunInfo(BaseModel):
    id: UUID
    request_id: UUID
    status: str
    file_id: UUID | None
    created_at: datetime
    timestamp: datetime
    job_id: str | None
    num_links: int | None
