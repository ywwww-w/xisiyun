from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Task, TaskStatus
from app.utils.logger import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from asyncio import Task as _ATask

_logger = get_logger("app.services.pipeline")

_NUM_WORKERS = 5

_queue: asyncio.Queue[str] | None = None
_sem: asyncio.Semaphore | None = None
_worker_tasks: list["_ATask[None]"] = []
_settings_snapshot: Settings | None = None
_placeholder_sleep_seconds: float = 0.1  # small default; tests may monkey-patch for concurrency measurement


# ---------------------------------------------------------------------------
# Initialization / lifecycle control
# ---------------------------------------------------------------------------
def init_engine(settings: Settings) -> None:
    """Create queue + semaphore; idempotent (safe to call twice).

    Queue size = max_concurrent * 100 (bounded, so unlimited enqueues will
    apply backpressure instead of eating memory — spec T7-验收5).
    """
    global _queue, _sem, _settings_snapshot
    if _queue is None:
        _queue = asyncio.Queue(maxsize=max(1, settings.max_concurrent_tasks * 100))
    if _sem is None:
        _sem = asyncio.Semaphore(settings.max_concurrent_tasks)
    _settings_snapshot = settings
    _logger.info(
        "[pipeline] engine initialized max_concurrent=%d queue_maxsize=%d workers=%d",
        settings.max_concurrent_tasks,
        _queue.maxsize if _queue else 0,
        _NUM_WORKERS,
    )


async def start_workers(settings: Settings) -> None:
    """Start N worker coroutines (N = _NUM_WORKERS = 5,经验值 > Semaphore 3).

    Safe to call twice (subsequent calls are no-ops; workers are already up).
    """
    global _worker_tasks
    init_engine(settings)
    assert _queue is not None  # init_engine guarantees this
    if _worker_tasks:
        _logger.info("[pipeline] start_workers called again: workers already running (%d)", len(_worker_tasks))
        return
    for i in range(_NUM_WORKERS):
        task = asyncio.create_task(
            _worker_loop(i),
            name=f"pipeline-worker-{i}",
        )
        _worker_tasks.append(task)
    _logger.info(
        "[pipeline] started %d pipeline workers (max_concurrent=%d)",
        len(_worker_tasks),
        settings.max_concurrent_tasks,
    )


async def shutdown_workers() -> None:
    """Cancel worker tasks, wait for gather with return_exceptions=True.

    Contract: zero 'Task was destroyed but it is pending!' warnings after
    shutdown (T7 验收 4).
    """
    global _worker_tasks, _queue, _sem, _settings_snapshot
    if not _worker_tasks:
        _logger.info("[pipeline] shutdown_workers called: no workers running, skip")
        _queue = None
        _sem = None
        _settings_snapshot = None
        return
    _logger.info("[pipeline] cancelling %d workers ...", len(_worker_tasks))
    for t in _worker_tasks:
        if not t.done():
            t.cancel()
    results = await asyncio.gather(*_worker_tasks, return_exceptions=True)
    cancelled = sum(1 for r in results if isinstance(r, asyncio.CancelledError))
    other_errs = [r for r in results if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError)]
    _logger.info(
        "[pipeline] workers joined cancelled=%d unexpected_errors=%d",
        cancelled,
        len(other_errs),
    )
    for err in other_errs:  # pragma: no cover - defensive
        _logger.exception("[pipeline] worker unexpected error during join", exc_info=err)
    _worker_tasks.clear()
    _queue = None
    _sem = None
    _settings_snapshot = None
    _logger.info("[pipeline] workers shutdown complete")


# ---------------------------------------------------------------------------
# Public enqueue API (used from T6 router_recordings.py 末尾 & resume function)
# ---------------------------------------------------------------------------
async def enqueue_task(task_id: str) -> None:
    """Put a task_id into the pipeline queue (blocks if queue full = backpressure).

    Raises RuntimeError if engine was not initialized (defensive).
    """
    if _queue is None:
        raise RuntimeError("[pipeline] enqueue_task called before init_engine/start_workers")
    qsize_before = _queue.qsize()
    await _queue.put(task_id)  # bounded maxsize; if full blocks = backpressure
    _logger.info(
        "[pipeline] enqueued task_id=%s queue_size_before=%d (now %d)",
        task_id,
        qsize_before,
        _queue.qsize(),
    )


# ---------------------------------------------------------------------------
# Worker inner loop + placeholder run_pipeline (T9 替换真实状态机)
# ---------------------------------------------------------------------------
async def _worker_loop(worker_id: int) -> None:
    assert _queue is not None and _sem is not None, "worker started before init_engine"
    _logger.info("[pipeline] worker-%d started", worker_id)
    try:
        while True:
            task_id = await _queue.get()
            try:
                async with _sem:
                    await _run_pipeline_placeholder(task_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - placeholder 几乎不会抛
                _logger.exception(
                    "[pipeline] worker-%d: task_id=%s placeholder raised (will swallow, next task)",
                    worker_id,
                    task_id,
                    exc_info=exc,
                )
            finally:
                _queue.task_done()
    except asyncio.CancelledError:
        _logger.info("[pipeline] worker-%d cancelled (expected during shutdown)", worker_id)
        raise
    finally:
        _logger.info("[pipeline] worker-%d exiting", worker_id)


async def _run_pipeline_placeholder(task_id: str) -> None:
    """T7 占位：不碰 DB/LLM/转写。只 sleep + 打日志（T9 替换）。

    sleep duration 可由外部通过修改 `_placeholder_sleep_seconds` 调整（测试
    并发控制验收时会临时改成 2.0 秒）。
    """
    _logger.info("[pipeline] placeholder processing task_id=%s (sleep=%.2fs)", task_id, _placeholder_sleep_seconds)
    await asyncio.sleep(_placeholder_sleep_seconds)
    _logger.info("[pipeline] placeholder done task_id=%s", task_id)


# ---------------------------------------------------------------------------
# Startup Resume (spec R8, 加分项 P1-2): UPDATE + 入队
# ---------------------------------------------------------------------------
async def resume_pending_tasks_on_startup(session: AsyncSession) -> int:
    """Perform R8 on process startup:
        1. UPDATE tasks SET status=pending, current_stage_retry_count=0
           WHERE status IN ('transcribing','summarizing')   →  commit
        2. SELECT id FROM tasks WHERE status='pending' ORDER BY created_at ASC
        3. for each id: await enqueue_task(id)

    Returns total enqueued count. Logs warning with: total + (reset_count from step 1)
    → 形如："[pipeline] resumed N tasks from DB (X reset from in-progress state)"
    """
    if _queue is None:
        raise RuntimeError("[pipeline] resume called before init_engine; call start_workers(settings) first")

    # Step 1: reset in-progress (spec R8 — otherwise阶段乐观锁WHERE不命中永远卡死)
    reset_stmt = (
        update(Task)
        .where(Task.status.in_([TaskStatus.transcribing, TaskStatus.summarizing]))
        .values(status=TaskStatus.pending, current_stage_retry_count=0)
        .execution_options(synchronize_session=False)
    )
    reset_result = await session.execute(reset_stmt)
    reset_count: int = int(reset_result.rowcount or 0)
    await session.commit()

    # Step 2: SELECT pending tasks 老的先入队
    select_stmt = (
        select(Task.id)
        .where(Task.status == TaskStatus.pending)
        .order_by(Task.created_at.asc())
    )
    rows = (await session.execute(select_stmt)).all()
    pending_ids: list[str] = [r[0] for r in rows]

    # Step 3: enqueue one by one
    for tid in pending_ids:
        await enqueue_task(tid)

    total = len(pending_ids)
    _logger.warning(
        "[pipeline] resumed %d tasks from DB (%d reset from in-progress state transcribing/summarizing)",
        total,
        reset_count,
    )
    return total
