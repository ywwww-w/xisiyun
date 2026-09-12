from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class TaskStatus(str, Enum):
    pending = "pending"
    transcribing = "transcribing"
    summarizing = "summarizing"
    done = "done"
    failed = "failed"
    archived = "archived"


class BaseSchema(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        use_enum_values=True,
    )


class ErrorBody(BaseSchema):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(BaseSchema):
    error: ErrorBody


class UploadResponse(BaseSchema):
    recording_id: str
    task_id: str
    status: TaskStatus


class TaskOut(BaseSchema):
    id: str
    recording_id: str
    status: TaskStatus
    current_stage_retry_count: int = Field(ge=0)
    total_retry_count: int = Field(ge=0)
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    is_deleted: bool = False
    deleted_at: datetime | None = None


class SummaryOut(BaseSchema):
    summary: str
    key_points: list[str]
    todos: list[str]


class RecordingListItem(BaseSchema):
    id: str
    original_filename: str
    file_ext: str
    file_size_bytes: int = Field(ge=0)
    last_status: TaskStatus
    created_at: datetime
    updated_at: datetime
    is_deleted: bool = False
    deleted_at: datetime | None = None


class RecordingDetailOut(RecordingListItem):
    transcript: str | None = None
    summary: SummaryOut | None = None


class PaginationQueryParams(BaseSchema):
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)


class PagedResponse(BaseSchema, Generic[T]):
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    items: list[T]


class LLMSummaryResponse(BaseSchema):
    summary: str
    key_points: list[str] = Field(min_length=1)
    todos: list[str]
