from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Tuple

from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database import get_session
from app.models import Recording, Task, TaskStatus
from app.schemas import UploadResponse
from app.services.pipeline import enqueue_task
from app.services.storage import (
    compute_file_md5_streaming,
    delete_file_if_exists,
    save_uploaded_file,
)
from app.utils.errors import BadRequestException
from app.utils.logger import get_logger

_settings = Settings()
_logger = get_logger("app.api.v1.recordings")

router = APIRouter(prefix="/recordings", tags=["recordings"])

_ALLOWED_EXT = {"wav", "mp3", "m4a", "aac"}
_MAX_BYTES = int(_settings.max_upload_size_mb) * 1024 * 1024
_UPLOADS = Path(_settings.storage_upload_dir).resolve()
_UPLOADS.mkdir(parents=True, exist_ok=True)


def _safe_extract_ext(filename: str | None) -> str:
    if not filename:
        return ""
    _, ext = os.path.splitext(filename)
    return (ext or "").lstrip(".").lower()


@router.post(
    "",
    status_code=status.HTTP_200_OK,
    response_model=UploadResponse,
)
async def create_recording(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> UploadResponse:
    # Step 1: file field must have a payload (FastAPI File(...) already enforces non-None,
    # but double guard against empty filename edge case).
    if not getattr(file, "filename", None):
        raise BadRequestException(
            message="Missing form field: file",
            details={"required": {"file": "UploadFile (multipart/form-data)"}},
        )

    # Step 2: extension whitelist (spec §4.5 upload 顺序：先扩展名)
    ext = _safe_extract_ext(file.filename)
    if ext not in _ALLOWED_EXT:
        raise BadRequestException.unsupported_media_type(
            actual_ext=ext or "<empty>",
            allowed=sorted(_ALLOWED_EXT),
        )

    # Step 3: stream MD5 (size fail-fast + seek(0) rewind, reuse for save_uploaded_file)
    md5_hex, total_bytes = await compute_file_md5_streaming(file)
    if total_bytes > _MAX_BYTES:
        raise BadRequestException.payload_too_large(
            max_mb=int(_settings.max_upload_size_mb),
            actual_bytes=total_bytes,
        )

    # Step 4: DB power — query by uk_recordings_file_hash (idempotency, spec §4.5 上传幂等)
    existing: Tuple[str, str] | None = await _find_existing_recording(session, md5_hex)
    if existing is not None:
        existing_recording_id, existing_task_id = existing
        _logger.info(
            "[upload] md5=%s idempotent hit (existing_recording_id=%s task_id=%s) — skip write disk+DB",
            md5_hex,
            existing_recording_id,
            existing_task_id,
            extra={"task_id": f"upload:{md5_hex[:8]}"},
        )
        return UploadResponse(
            recording_id=existing_recording_id,
            task_id=existing_task_id,
            status=TaskStatus.pending.value,
        )

    # Step 5: write to disk first (before DB transaction — spec §4.5 顺序；失败无 DB 写入)
    recording_id = str(uuid.uuid4())
    try:
        storage_path, written_bytes = await save_uploaded_file(
            file,
            recording_id=recording_id,
            ext=ext,
        )
    except Exception:
        # any disk error (including PayloadTooLargeException already raised inside
        # save_uploaded_file's inline unlink — in case of IOError, we already have no
        # partial file on disk by save_uploaded_file contract — re-raise caller.
        raise

    # Step 6: DB transaction — INSERT recordings + tasks together
    task_id: str | None = None
    try:
        recording = Recording(
            id=recording_id,
            original_filename=file.filename or "",
            file_ext=ext,
            file_size_bytes=written_bytes,
            file_hash=md5_hex,
            storage_path=str(storage_path),
            last_status=TaskStatus.pending,
        )
        session.add(recording)
        task = Task(
            recording_id=recording_id,
            status=TaskStatus.pending,
            current_stage_retry_count=0,
            total_retry_count=0,
            error_message=None,
        )
        session.add(task)
        await session.flush()
        task_id = task.id
        await session.commit()
    except IntegrityError as exc:
        # Race: another concurrent request with the same MD5 already INSERT committed
        # between Step 4's SELECT and our COMMIT. Treat as idempotent success:
        #   (a) rollback this txn
        #   (b) delete the file we just wrote (we have same-hash different recording_id
        #       file on disk from another request — clean up our orphan)
        #   (c) read the winner's (recording_id, task_id) and return it (exactly same
        #       contract as Step 4, 200 OK, same payload).
        #
        # We detect IntegrityError specifically on uk_recordings_file_hash (the only
        # UniqueConstraint on recordings) via the DB driver's error message.
        await session.rollback()
        _logger.warning(
            "[upload] IntegrityError md5=%s recording_id=%s — treating as idempotent. "
            "Cleanup orphan storage_path=%s",
            md5_hex,
            recording_id,
            storage_path,
            exc_info=exc,
            extra={"task_id": f"upload:race:{md5_hex[:8]}"},
        )
        delete_file_if_exists(storage_path)
        winner = await _find_existing_recording(session, md5_hex)
        if winner is None:  # pragma: no cover - extremely unlikely
            raise
        winner_recording_id, winner_task_id = winner
        return UploadResponse(
            recording_id=winner_recording_id,
            task_id=winner_task_id,
            status=TaskStatus.pending.value,
        )
    except Exception:
        # Any other DB error: ROLLBACK + delete disk file (no garbage left).
        await session.rollback()
        delete_file_if_exists(storage_path)
        raise

    # Step 7: enqueue task into pipeline queue — worker (from T7 pipeline service) picks it up
    #   Semaphore(3) controls concurrency; run_pipeline is a placeholder (T9 替换真实状态机)。
    assert task_id is not None
    await enqueue_task(task_id)
    _logger.info(
        "[upload] inserted new recording_id=%s task_id=%s md5=%s bytes=%d (queued)",
        recording_id,
        task_id,
        md5_hex,
        written_bytes,
        extra={"task_id": f"upload:new:{md5_hex[:8]}"},
    )

    return UploadResponse(
        recording_id=recording_id,
        task_id=task_id,
        status=TaskStatus.pending.value,
    )


async def _find_existing_recording(
    session: AsyncSession,
    file_hash: str,
) -> Tuple[str, str] | None:
    """Return (recording_id, task_id) if an existing recording matches `file_hash`.

    Uses uk_recordings_file_hash (UNIQUE) → exactly 0 or 1 row.
    """
    stmt = (
        select(Recording.id, Task.id)
        .select_from(Recording)
        .join(Task, Task.recording_id == Recording.id)
        .where(Recording.file_hash == file_hash)
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    return row[0], row[1]
