from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas import TaskOut
from app.services import pipeline as pipeline_service
from app.services.tasks_service import (
    get_task_by_id_or_404,
    retry_task as retry_task_service,
)
from app.utils.logger import get_logger

_logger = get_logger("app.api.v1.tasks")

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get(
    "/{task_id}",
    response_model=TaskOut,
    summary="Get task processing status / retries / error message.",
)
async def get_task_status(
    task_id: str,
    session: AsyncSession = Depends(get_session),
) -> TaskOut:
    """Return TaskOut; status = pending/transcribing/summarizing/done/failed so
    client can distinguish stage inside 'processing' (spec §2.2 ticket 10 A1).
    """
    task = await get_task_by_id_or_404(session, task_id)
    await session.refresh(task)
    return TaskOut.model_validate(task)


@router.post(
    "/{task_id}/retry",
    response_model=TaskOut,
    status_code=status.HTTP_200_OK,
    summary="Manually retry a FAILED task (re-enqueue + reset counters).",
)
async def post_retry_task(
    task_id: str,
    session: AsyncSession = Depends(get_session),
) -> TaskOut:
    """Manual retry semantics (T10 §任务 1 retry_task):

    - **404**: task_id not in DB.
    - **409 Conflict**: task.status != 'failed' (includes concurrent duplicate
      retry requests; row lock + optimistic double-check guarantee exactly 1x 200).
    - **200 OK**: DB updated atomically (tasks pending + recordings.last_status
      pending + total_retry_count++ + counters/error cleared) THEN re-enqueued
      into asyncio.Queue (2-phase: commit first, then enqueue).
    """
    task_obj, _ = await retry_task_service(session, task_id)
    new_total = task_obj.total_retry_count
    await session.commit()
    try:
        await pipeline_service.enqueue_task(task_id)
    except Exception as exc:  # pragma: no cover - queue init 保护
        _logger.exception(
            "[tasks] manual retry commit SUCCESS but enqueue_task FAILED task_id=%s (will NOT auto-recover): %s",
            task_id, exc,
            extra={"task_id": task_id},
        )
        raise
    _logger.warning(
        "[tasks] manual retry SUCCESS task_id=%s total_retry_count now=%d (enqueued)",
        task_id, new_total,
        extra={"task_id": task_id},
    )
    return TaskOut.model_validate(task_obj)
