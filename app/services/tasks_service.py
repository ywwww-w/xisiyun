from __future__ import annotations

from typing import Tuple

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Recording, Task, TaskStatus
from app.utils.errors import ConflictException, NotFoundException
from app.utils.logger import get_logger

_logger = get_logger("app.services.tasks_service")


async def get_task_list_paged(
    session: AsyncSession,
    page: int,
    page_size: int,
) -> Tuple[int, list[Task]]:
    """Paged task list (symmetric with recordings list; frontend detail page polls
    this to find matching task_id by recording_id).

    Tv2-4:默认过滤软删任务(一般跟父recording同步,但防御性过滤)
    ORDER BY updated_at DESC (most recently touched first).
    Returns (total_count, items_on_page).
    """
    total: int = int(await session.scalar(
        select(func.count()).select_from(Task).where(Task.is_deleted == False)
    ) or 0)
    stmt = (
        select(Task)
        .where(Task.is_deleted == False)
        .order_by(Task.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return total, list(rows)


async def get_task_by_id_or_404(session: AsyncSession, task_id: str, *, exclude_deleted: bool = True) -> Task:
    """Return Task row or raise NotFoundException("Task", task_id)."""
    stmt = select(Task).where(Task.id == task_id)
    if exclude_deleted:
        stmt = stmt.where(Task.is_deleted == False)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundException("Task", task_id)
    return row


async def retry_task(
    session: AsyncSession,
    task_id: str,
) -> Tuple[Task, bool]:
    """Manual retry entry (spec P0-6 + R5).

    Steps (strict order):
        1. SELECT ... FOR UPDATE row lock on tasks.id (允许查deleted task,用于409提示restore)
        2. Not found -> NotFound
        3. Optimistic idempotency check: status != failed -> Conflict 409
        4. Tv2-4 防御: task.is_deleted or parent Recording.is_deleted → 409 "请先restore"
        5. UPDATE tasks: status=pending, current_stage_retry_count=0,
           total_retry_count += 1, error_message=None;
           UPDATE recordings.last_status=pending (keep 2-table consistency)
        6. session.commit() first (release lock)
        7. Caller calls pipeline.enqueue_task(task_id) ONLY AFTER commit returns OK.

    Returns (updated_task_ref, True). Caller is responsible for enqueue step
    because enqueue touches in-memory asyncio.Queue, which MUST NOT live in
    a DB transaction (commit may fail but enqueue already happened -> split brain).
    """
    locked = (await session.execute(
        select(Task)
        .where(Task.id == task_id)
        .with_for_update()
    )).scalar_one_or_none()
    if locked is None:
        raise NotFoundException("Task", task_id)

    if locked.status != TaskStatus.failed:
        raise ConflictException(
            message=(
                f"Task '{task_id}' is in status '{locked.status.value}', "
                f"only 'failed' tasks can be retried. concurrent duplicate requests are not allowed."
            ),
            code="TASK_NOT_FAILED",
            details={
                "task_id": task_id,
                "current_status": locked.status.value,
                "expected_status": TaskStatus.failed.value,
            },
        )

    recording_id = locked.recording_id
    # 用户决策 4-2:父recording已软删(archived)→手动retry先409提示恢复
    if locked.is_deleted:
        raise ConflictException(
            message=(
                f"Task '{task_id}' 已随父录音进入回收站(status=archived)，"
                f"请先调 POST /v1/recordings/{recording_id}/restore 恢复后再重试"
            ),
            code="TASK_NOT_RETRYABLE",
            details={
                "task_id": task_id,
                "recording_id": recording_id,
                "restore_hint": f"POST /v1/recordings/{recording_id}/restore",
            },
        )
    parent_deleted = (await session.execute(
        select(Recording.is_deleted).where(Recording.id == recording_id)
    )).scalar_one_or_none()
    if parent_deleted is True:
        raise ConflictException(
            message=(
                f"该录音已进入回收站(status=archived)，请先调 POST /v1/recordings/{recording_id}/restore 恢复后再重试"
            ),
            code="TASK_NOT_RETRYABLE",
            details={
                "task_id": task_id,
                "recording_id": recording_id,
                "restore_hint": f"POST /v1/recordings/{recording_id}/restore",
            },
        )
    new_total = int(locked.total_retry_count or 0) + 1
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            status=TaskStatus.pending,
            current_stage_retry_count=0,
            total_retry_count=new_total,
            error_message=None,
        )
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(Recording)
        .where(Recording.id == recording_id)
        .values(last_status=TaskStatus.pending)
        .execution_options(synchronize_session=False)
    )
    await session.flush()

    locked.status = TaskStatus.pending
    locked.current_stage_retry_count = 0
    locked.total_retry_count = new_total
    locked.error_message = None

    _logger.warning(
        "[tasks_service] manual retry prepared (pending commit) task_id=%s recording_id=%s new_total_retry_count=%d",
        task_id, recording_id, new_total,
        extra={"task_id": task_id},
    )
    return locked, True
