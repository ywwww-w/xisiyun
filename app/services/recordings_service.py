from __future__ import annotations

from typing import Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Recording, Task
from app.utils.errors import NotFoundException
from app.utils.logger import get_logger

_logger = get_logger("app.services.recordings_service")


async def get_recording_list_paged(
    session: AsyncSession,
    page: int,
    page_size: int,
) -> Tuple[int, list[Recording]]:
    """Paged recording list (spec §2.2 row 3). ORDER BY created_at DESC.

    Returns (total_count, items_on_page).
    """
    total: int = int(await session.scalar(
        select(func.count()).select_from(Recording)
    ) or 0)
    stmt = (
        select(Recording)
        .order_by(Recording.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return total, list(rows)


async def get_recording_detail_or_404(
    session: AsyncSession,
    recording_id: str,
) -> Recording:
    row = (await session.execute(
        select(Recording).where(Recording.id == recording_id)
    )).scalar_one_or_none()
    if row is None:
        raise NotFoundException("Recording", recording_id)
    return row


async def delete_recording_cascade(
    session: AsyncSession,
    recording: Recording,
) -> Tuple[str, str]:
    """Delete tasks + recording row in DB (事务内) and return (storage_path, id).

    Caller should commit the transaction, then call storage.delete_file_if_exists
    on the returned storage_path (spec §4.5 DELETE order: DB first, file second,
    so rollback never leaves a dangling deleted-file without DB row).
    """
    rid = recording.id
    storage_path = recording.storage_path or ""
    # Double safe: ORM FK is ON DELETE CASCADE, so delete(Recording) would drop
    # tasks automatically. Delete tasks explicitly anyway for deterministic order.
    await session.execute(
        delete(Task).where(Task.recording_id == rid)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        delete(Recording).where(Recording.id == rid)
        .execution_options(synchronize_session=False)
    )
    _logger.warning(
        "[recordings_service] cascade delete prepared (pending commit) recording_id=%s storage_path=%s size=%dB",
        rid, storage_path, recording.file_size_bytes,
    )
    return storage_path, rid
