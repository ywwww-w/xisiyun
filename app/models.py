from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)

from app.database import Base


class TaskStatus(str, enum.Enum):
    pending = "pending"
    transcribing = "transcribing"
    summarizing = "summarizing"
    done = "done"
    failed = "failed"


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Recording(Base):
    __tablename__ = "recordings"

    # 对应 spec §4.4.1 recordings 表 11 个字段
    id: Column[str] = Column(
        String(36),
        primary_key=True,
        default=_new_uuid,
        doc="UUID4 recording_id PK (spec §4.4.1 id)",
    )
    original_filename: Column[str] = Column(
        String(255),
        nullable=False,
        doc="用户上传时的原始文件名，仅展示用 (spec §4.4.1 original_filename)",
    )
    file_ext: Column[str] = Column(
        String(10),
        nullable=False,
        doc="扩展名小写，白名单 wav/mp3/m4a/aac (spec §4.4.1 file_ext)",
    )
    file_size_bytes: Column[int] = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        nullable=False,
        doc="文件大小字节数，50MB 上限校验用 (spec §4.4.1 file_size_bytes)",
    )
    file_hash: Column[str] = Column(
        String(64),
        nullable=False,
        doc="上传文件 MD5(预留 SHA256 位长)，P1-4 上传幂等核心 UNIQUE 索引 (spec §4.4.1 file_hash UNIQUE)",
    )
    storage_path: Column[str] = Column(
        String(512),
        nullable=False,
        doc="相对路径 uploads/{uuid}.{ext}，换对象存储不用改代码 (spec §4.4.1 storage_path)",
    )
    transcript: Column[str | None] = Column(
        Text().with_variant(Text, "sqlite"),
        nullable=True,
        doc="Mock 转写结果文本 (spec §4.4.1 transcript)",
    )
    summary_json: Column[dict | list | None] = Column(
        JSON(),
        nullable=True,
        doc="LLM 返回的 {summary,key_points,todos} 三字段 dict，MySQL 原生 JSON (spec §4.4.1 summary_json)",
    )
    last_status: Column[TaskStatus] = Column(
        Enum(
            TaskStatus,
            values_callable=lambda xs: [x.value for x in xs],
        ),
        nullable=False,
        default=TaskStatus.pending,
        doc="冗余存 tasks 最新状态，列表接口不 JOIN 性能好 (spec §4.4.1 last_status)",
    )
    created_at: Column[datetime] = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="分页 ORDER BY 用，加索引 (spec §4.4.1 created_at)",
    )
    updated_at: Column[datetime] = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=_utc_now,
        doc="自动 onupdate (spec §4.4.1 updated_at)",
    )

    __table_args__ = (
        UniqueConstraint("file_hash", name="uk_recordings_file_hash"),
        Index("idx_recordings_last_status", "last_status"),
        Index("idx_recordings_created_at", "created_at"),
    )


class Task(Base):
    __tablename__ = "tasks"

    # 对应 spec §4.4.2 tasks 表 8 个字段（不含额外 idx_tasks_updated_at 也加上，对齐 spec 4.4.2 最后一行）
    id: Column[str] = Column(
        String(36),
        primary_key=True,
        default=_new_uuid,
        doc="UUID4 task_id PK (spec §4.4.2 id)",
    )
    recording_id: Column[str] = Column(
        String(36),
        ForeignKey(
            "recordings.id",
            ondelete="CASCADE",
            name="fk_tasks_recording_id__recordings_id",
        ),
        nullable=False,
        doc="外键级联删除 CASCADE：删 recording 时 task 自动跟着删 (spec §4.4.2 recording_id + R10)",
    )
    status: Column[TaskStatus] = Column(
        Enum(
            TaskStatus,
            values_callable=lambda xs: [x.value for x in xs],
        ),
        nullable=False,
        default=TaskStatus.pending,
        doc="5 态枚举，startup 扫表核心索引 (spec §4.4.2 status)",
    )
    current_stage_retry_count: Column[int] = Column(
        SmallInteger(),
        nullable=False,
        default=0,
        doc="当前阶段自动重试计数，上限 3 (spec §4.4.2 current_stage_retry_count)",
    )
    total_retry_count: Column[int] = Column(
        Integer(),
        nullable=False,
        default=0,
        doc="用户手动 POST retry 累计的总次数，仅统计 (spec §4.4.2 total_retry_count)",
    )
    error_message: Column[str | None] = Column(
        Text(),
        nullable=True,
        doc="失败时的 message 给用户看（堆栈仅打日志文件） (spec §4.4.2 error_message)",
    )
    created_at: Column[datetime] = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="spec §4.4.2 created_at",
    )
    updated_at: Column[datetime] = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=_utc_now,
        doc="spec §4.4.2 updated_at + idx_tasks_updated_at 索引",
    )

    __table_args__ = (
        Index("idx_tasks_status", "status"),
        Index("idx_tasks_recording_id", "recording_id"),
        Index("idx_tasks_updated_at", "updated_at"),
    )
