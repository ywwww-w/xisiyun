from __future__ import annotations

from typing import Tuple

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Recording, Task, TaskStatus, _utc_now
from app.utils.errors import ConflictException, NotFoundException
from app.utils.logger import get_logger

_logger = get_logger("app.services.recordings_service")


async def get_recording_list_paged(
    session: AsyncSession,
    page: int,
    page_size: int,
) -> Tuple[int, list[Recording]]:
    """Paged recording list (spec §2.2 row 3). ORDER BY created_at DESC.

    Tv2-4:软删行默认exclude (exclude_deleted=True default 对所有查询有效)。
    Returns (total_count, items_on_page).
    """
    total: int = int(await session.scalar(
        select(func.count()).select_from(Recording).where(Recording.is_deleted == False)
    ) or 0)
    stmt = (
        select(Recording)
        .where(Recording.is_deleted == False)
        .order_by(Recording.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return total, list(rows)


async def get_recording_detail_or_404(
    session: AsyncSession,
    recording_id: str,
    *,
    exclude_deleted: bool = True,
) -> Recording:
    stmt = select(Recording).where(Recording.id == recording_id)
    if exclude_deleted:
        stmt = stmt.where(Recording.is_deleted == False)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundException("Recording", recording_id)
    return row


async def soft_delete_recording_cascade(
    session: AsyncSession,
    recording: Recording,
) -> Tuple[str, str]:
    """Logical delete. UPDATE is_deleted=True, set last_status=archived,
    sync child tasks (same). Returns (storage_path, id).

    Caller: after COMMIT, call storage.move_upload_to_trash(storage_path).

    关键: 我们把「软删前的原始 last_status」备份到 tasks.error_message 保留(用 <ARCHIVED_ORIGINAL_STATUS:xxx> 前缀，
    error_message 已在使用中? 不，tasks 正常失败时 error_message 是可读字符串；我们单独用一个私有标记字段的做法更干净：
    两表同时加 archived_original_last_status JSON 列成本高 → 用 tasks 表我们不用的列/直接：
    做法：我们在 recordings.transcript 前面 prepend 注释前缀? NO，破坏 transcript 数据。
    **最终方案（零 schema 修改）：在 recordings.summary_json 我们本来就是 JSON，如果它是 dict，就写入 __archived_previous_last_status 私有键；
    如果 summary_json 为 NULL（还没处理完 pending），插入一个最小 JSON {'__archived_previous_last_status': 'pending'}。
    恢复时 pop 这个私有键 → 保证业务数据/前端输出完全不变。
    """
    rid = recording.id
    storage_path = recording.storage_path or ""
    now = _utc_now()
    prev_last_status_value: str = recording.last_status.value if hasattr(recording.last_status, "value") else str(recording.last_status)
    # 备份prev_status到summary_json
    import json as _json
    merged_sj: dict
    try:
        if recording.summary_json is None:
            merged_sj = {}
        elif isinstance(recording.summary_json, (dict, list)):
            merged_sj = {"__orig_sj": recording.summary_json} if not isinstance(recording.summary_json, dict) else dict(recording.summary_json)
        else:
            # JSON 列偶尔会存 str（老数据），反序列化
            merged_sj = _json.loads(str(recording.summary_json))
            if not isinstance(merged_sj, dict):
                merged_sj = {"__orig_sj": merged_sj}
    except Exception:
        merged_sj = {}
    merged_sj["__archived_previous_last_status"] = prev_last_status_value

    # 1. child tasks (同步软删,避免 tasks 表残留"孤儿"alive记录);顺便备份 prev_task_status 到 task.error_message 前缀 <ARCHIVED_PREV_STATUS:xxx> 方便S7手动UPDATE SQL 之后 restore 能回写
    prev_task_rows = (await session.execute(
        select(Task.id, Task.status).where(Task.recording_id == rid)
    )).all()
    prev_task_status_by_id: dict[str, str] = {str(tid): (st.value if hasattr(st, "value") else str(st)) for tid, st in prev_task_rows}
    for tid in list(prev_task_status_by_id.keys()):
        prev_st = prev_task_status_by_id[tid]
        new_err_prefix = f"<ARCHIVED_PREV_STATUS:{prev_st}>"
        await session.execute(
            update(Task)
            .where(Task.id == tid)
            .values(
                is_deleted=True,
                deleted_at=now,
                status=TaskStatus.archived,
                error_message=new_err_prefix,
            )
            .execution_options(synchronize_session=False)
        )
    # 2. recording本体:置archived终态防止列表还展示pending/done
    await session.execute(
        update(Recording)
        .where(Recording.id == rid)
        .values(
            is_deleted=True,
            deleted_at=now,
            last_status=TaskStatus.archived,
            summary_json=merged_sj,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    try:
        session.expunge(recording)
    except Exception:
        pass
    _logger.warning(
        "[soft_delete] recording_id=%s filename='%s' size=%dB storage_path='%s' prev_last_status=%s",
        rid, recording.original_filename, recording.file_size_bytes, storage_path, prev_last_status_value,
    )
    return storage_path, rid


async def restore_recording_or_409(
    session: AsyncSession,
    recording: Recording,
) -> Recording:
    """Undo logical delete. Idempotent: already-alive recordings simply return.
    Raises 409 only in A-Full TTL mode (trash file missing).

    A-Lite：我们会从备份里取出软删前的 last_status 恢复：
      - recordings.summary_json 里如果有 __archived_previous_last_status 就 pop 它(恢复后的 last_status)，
        并且如果是 dict/summary_json 正常业务键存在的(有 summary/key_points/todos)就保留；
        如果 summary_json 只是我们插入的"最小占位 JSON"就设回 NULL 避免前端展示空 dict。
      - tasks.error_message 前缀 <ARCHIVED_PREV_STATUS:xxx>；剥掉前缀：
        如果剥完后就是空字符串 → task.error_message = NULL（正常非失败态本来就是 NULL）。
    """
    if not recording.is_deleted:
        return recording
    rid = recording.id
    now = _utc_now()

    # === 解析 recording.last_status 备份 ===
    effective_last: TaskStatus = TaskStatus.failed
    restored_sj = recording.summary_json
    import json as _json
    if isinstance(restored_sj, str):
        try:
            restored_sj = _json.loads(restored_sj)
        except Exception:
            restored_sj = None
    if isinstance(restored_sj, dict):
        sj_copy = dict(restored_sj)
        prev = sj_copy.pop("__archived_previous_last_status", None)
        if prev and isinstance(prev, str):
            try:
                effective_last = TaskStatus(prev)
            except Exception:
                # 手动 SQL UPDATE 测试把 tasks.status 改了但没备份? 回落到后面查tasks error_message前缀
                effective_last = TaskStatus.failed
        # 还有业务键? summary/key_points/todos任意一个有 =>保留sj_copy, 否则设NULL恢复原样(pending软删没产生过summary的行)
        business_keys = {"summary", "key_points", "todos"}
        if any(k in sj_copy for k in business_keys):
            restored_sj = sj_copy
        elif "__orig_sj" in sj_copy:
            restored_sj = sj_copy["__orig_sj"]
        else:
            restored_sj = None
    # 子tasks前缀解析, fallback last_status 若上一步还在 fallback failed
    child_rows = (await session.execute(
        select(Task.id, Task.status, Task.error_message).where(Task.recording_id == rid)
    )).all()
    final_last_set = False
    for tid, cst, cerr in child_rows:
        # 回写 tasks.status 前的 prev 状态
        prev_ts = None
        new_err = None
        if isinstance(cerr, str) and cerr.startswith("<ARCHIVED_PREV_STATUS:"):
            left = cerr[len("<ARCHIVED_PREV_STATUS:"):]
            if ">" in left:
                prev_ts = left.split(">", 1)[0].strip() or None
                remain = left.split(">", 1)[1]
                if remain:
                    new_err = remain
        # 回写 UPDATE
        final_status_for_task: TaskStatus
        if prev_ts:
            try:
                final_status_for_task = TaskStatus(prev_ts)
            except Exception:
                final_status_for_task = (
                    cst if (cst and cst != TaskStatus.archived) else TaskStatus.failed
                )
        else:
            # fallback: cst 非archived就保持, else failed
            final_status_for_task = cst if (cst and cst != TaskStatus.archived) else TaskStatus.failed
        # effective_last 优先: 若 summary_json 无备份 就用第一个非archived子task的状态
        if (effective_last == TaskStatus.failed) and (not final_last_set) and final_status_for_task != TaskStatus.archived:
            effective_last = final_status_for_task
            final_last_set = True
        await session.execute(
            update(Task)
            .where(Task.id == tid)
            .values(
                is_deleted=False,
                deleted_at=None,
                status=final_status_for_task,
                error_message=new_err,
            )
            .execution_options(synchronize_session=False)
        )
    # recording UPDATE
    vals: dict = dict(is_deleted=False, deleted_at=None, updated_at=now, last_status=effective_last)
    if restored_sj is not None:
        vals["summary_json"] = restored_sj
    else:
        vals["summary_json"] = None
    await session.execute(
        update(Recording)
        .where(Recording.id == rid)
        .values(**vals)
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    try:
        session.expunge(recording)
    except Exception:
        pass
    _logger.info("[restore] recording_id=%s is_deleted=False last_status => %s summary_json_kept=%s", rid, effective_last.value, restored_sj is not None)
    return recording


# 保留delete_recording_cascade真删接口,暂时不删除,后续人工批量删历史/测试数据用
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
    )
    await session.execute(
        delete(Recording).where(Recording.id == rid)
    )
    # Expunge the deleted ORM instance from session identity map so the instance
    # itself can't be accidentally re-flushed / re-read by downstream code.
    await session.flush()
    try:
        session.expunge(recording)
    except Exception:
        pass
    _logger.warning(
        "[recordings_service] cascade delete prepared (pending commit) recording_id=%s storage_path=%s size=%dB",
        rid, storage_path, recording.file_size_bytes,
    )
    return storage_path, rid
