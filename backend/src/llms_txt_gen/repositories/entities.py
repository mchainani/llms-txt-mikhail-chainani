"""Immutable database entities, independent of API request/response models."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class Request:
    id: UUID
    url: str
    created_at: datetime
    timestamp: datetime
    job_id: str | None = None
    cron_expression: str | None = None


@dataclass(frozen=True)
class Run:
    id: UUID
    request_id: UUID
    status: str
    file_id: UUID | None
    created_at: datetime
    timestamp: datetime
    job_id: str | None = None
    num_links: int | None = None


@dataclass(frozen=True)
class File:
    id: UUID
    type: str
    content: str
    created_at: datetime
    timestamp: datetime
