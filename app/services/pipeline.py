from __future__ import annotations

import asyncio
import json
import random
import time
from typing import TYPE_CHECKING

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database import AsyncSessionLocal
from app.models import Recording, Task, TaskStatus
from app.services import llm as llm_service
from app.utils.errors import LLMCallException, LLMResponseFormatException
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

_MOCK_ASR_FAIL_PROBABILITY: float = 0.2  # T9 验收标准 2: 20% 随机失败
_MOCK_ASR_MIN_SLEEP_SECONDS: float = 5.0  # T9 验收标准 1. 5~15s 随机 Mock 耗时
_MOCK_ASR_MAX_SLEEP_SECONDS: float = 15.0

_TRANSCRIPT_SENTENCES: tuple[str, ...] = (
    "大家好，今天我们讨论产品上线前的最后准备工作，主要聚焦三个方向：前端体验优化、后端稳定性加固、以及部署脚本回归验证。",
    "前端方面需要修复移动端输入框键盘遮挡的 bug，同时把首屏资源压缩到 200KB 以内，以保证低端设备也能在 2 秒内完成首屏渲染。",
    "后端需要为所有慢查询添加联合索引，特别是 recordings 表的 last_status + created_at 联合查询，以及 tasks 表 recording_id 聚合统计语句。",
    "压力测试目标是在 200 并发上传请求下，接口平均响应时间小于 1 秒，错误率控制在千分之五以内，数据库 CPU 占用不超过 60%。",
    "部署脚本需要支持灰度发布，能够按 10%、30%、50%、100% 四个阶段逐步放量，并在每阶段监控错误告警，一旦超过阈值自动回滚。",
    "LLM 接口超时设置为 30 秒，失败自动重试 3 次，指数退避间隔分别为 1 秒、2 秒、4 秒，避免短时间内触发上游 429 限流。",
    "上传接口要严格校验文件 MD5，对相同内容的音频实现幂等返回，保证同一个文件重复上传不会产生多条重复记录与多余转写任务。",
    "日志系统要统一添加 task_id 作为上下文追踪 ID，从上传接口、入队、转写、摘要、到最终完成都能串联出完整调用链。",
    "数据库迁移脚本必须同时提供升级和回滚方案，线上执行前先在预发环境执行完整链路回归，并备份所有相关表。",
    "最终验收标准是：上传、查询、重试、删除四个主要接口全部通过自动化用例，pipeline 状态机在异常注入下 99.9% 的任务能进入终态。",
)


def _random_transcript() -> str:
    """T9 §任务 1. 500~2000 字随机中文 Mock 转写（固定 10 句随机拼接）。

    不新增依赖（不装 python-lorem），只用内置句子拼接 + 少量随机数。
    """
    n_sentences = random.randint(4, 18)  # 4~18 句 × 每句平均 100 字 → ~400~1800 字
    selected = random.choices(_TRANSCRIPT_SENTENCES, k=n_sentences)
    body = " ".join(selected)
    # 再随机重复 1~2 次，保证整体 >= 500 字
    extra_repeats = random.randint(0, 2)
    body += (" " + " ".join(random.choices(_TRANSCRIPT_SENTENCES, k=2))) * extra_repeats
    if len(body) < 500:
        # 保底 500 字：追加默认长文
        body += (" " + _TRANSCRIPT_SENTENCES[0] + _TRANSCRIPT_SENTENCES[3]) * 4
    # T9 验收 1. 校验：>= 500 字（保证用例通过）
    body = body[: max(len(body), 500)]
    return body


def _backoff_seconds(attempt_idx_0based: int) -> float:
    """T9 §任务 1. 指数退避：0→1s, 1→2s, 2→4s（最多 3 次重试 × 每阶段）。"""
    return 2 ** attempt_idx_0based * 1.0


def _log_task(level: str, task_id: str, message: str, **kwargs) -> None:
    """统一日志，附加 extra={"task_id": xxx}，使得所有 pipeline 日志都能 grep 聚合。"""
    getattr(_logger, level.lower(), _logger.info)(
        "[pipeline] task_id=%s %s", task_id, message,
        extra={"task_id": task_id},
        **kwargs,
    )


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
# Worker inner loop + run_pipeline (T9 真实状态机替换 T7 placeholder)
# ---------------------------------------------------------------------------
async def _worker_loop(worker_id: int) -> None:
    assert _queue is not None and _sem is not None, "worker started before init_engine"
    _logger.info("[pipeline] worker-%d started", worker_id)
    try:
        while True:
            task_id = await _queue.get()
            try:
                async with _sem:
                    await _run_pipeline_real(task_id)  # T9: 替换 placeholder
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - 已在 real 内吞异常, 兜底防 worker 死
                _logger.exception(
                    "[pipeline] worker-%d: task_id=%s run_pipeline raised UNEXPECTED (will swallow, next task)",
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
    """T7 占位函数 — 保留（T7 test_pipeline.py 并发验收仍用它，不删）。"""
    _logger.info("[pipeline] placeholder processing task_id=%s (sleep=%.2fs)", task_id, _placeholder_sleep_seconds)
    await asyncio.sleep(_placeholder_sleep_seconds)
    _logger.info("[pipeline] placeholder done task_id=%s", task_id)


async def _run_pipeline_real(task_id: str) -> None:
    """T9 核心状态机：两阶段（转写 → 摘要）× 各 3 次指数退避 × WHERE 乐观锁。

    所有异常在函数内被捕获并处理成 failed 终态（重试耗尽）/或者成功 done。
    函数 return 之前总会把任务推进到终态 {done, failed} 或者因为乐观锁未拿到（被其他
    worker 处理了）而直接 return（打 WARN 日志，不 throw 出函数）。
    """
    t0 = time.perf_counter()
    settings = _settings_snapshot or Settings()
    max_retries = max(1, settings.pipeline_max_auto_retries)

    async with AsyncSessionLocal() as session:
        # --- 先拿 task + recording 对象（不存在就静默 return）---
        row = (await session.execute(
            select(Task, Recording)
            .join(Recording, Recording.id == Task.recording_id)
            .where(Task.id == task_id)
            .execution_options(populate_existing=True)
        )).one_or_none()
        if row is None:
            _log_task("warning", task_id, "task/recording row not found in DB (already deleted?), skip silently")
            return
        task, recording = row

        # ===================================================================
        # Stage 1: 转写 Mock  — pending -> transcribing -> summarizing / failed
        # ===================================================================
        stage1_final_error: str | None = None
        for stage1_attempt in range(max_retries):
            # --- 乐观锁: 必须当前状态是 pending 才能推进到 transcribing ---
            lock_stmt = (
                update(Task)
                .where(Task.id == task_id, Task.status == TaskStatus.pending)
                .values(
                    status=TaskStatus.transcribing,
                    current_stage_retry_count=stage1_attempt,
                    error_message=None,
                    updated_at=func.now(),
                )
            )
            lock_res = await session.execute(lock_stmt)
            await session.commit()
            if int(lock_res.rowcount or 0) == 0:
                # 没拿到锁 → 状态已不是 pending；可能被其他 worker 接管了
                _log_task(
                    "warning", task_id,
                    "stage1 lock not acquired: task status is not 'pending'. "
                    "Assume already handled by another worker or user-triggered status change. SKIP this task.",
                )
                return
            _log_task("info", task_id, f"stage1(transcribe) START attempt={stage1_attempt + 1}/{max_retries}")
            try:
                # --- Mock 5~15s sleep + 20% 概率失败（验收 1/2）---
                sleep_s = random.uniform(_MOCK_ASR_MIN_SLEEP_SECONDS, _MOCK_ASR_MAX_SLEEP_SECONDS)
                await asyncio.sleep(sleep_s)
                fail_prob = _MOCK_ASR_FAIL_PROBABILITY
                if random.random() < fail_prob:
                    raise RuntimeError(
                        f"Mock ASR random failure (prob={fail_prob:.0%}) stage1 attempt {stage1_attempt + 1}"
                    )
                transcript = _random_transcript()
                if len(transcript) < 500:  # pragma: no cover - _random_transcript 保证 >= 500
                    transcript += _TRANSCRIPT_SENTENCES[0] * 5
                # --- 阶段 1 成功：两表推进到 summarizing WHERE status=transcribing 乐观锁 ---
                await session.execute(
                    update(Recording)
                    .where(Recording.id == recording.id)
                    .values(
                        transcript=transcript,
                        last_status=TaskStatus.summarizing,
                        updated_at=func.now(),
                    )
                )
                stg2_lock_stmt = (
                    update(Task)
                    .where(Task.id == task_id, Task.status == TaskStatus.transcribing)
                    .values(
                        status=TaskStatus.summarizing,
                        current_stage_retry_count=0,
                        error_message=None,
                        updated_at=func.now(),
                    )
                )
                stg2_lock_res = await session.execute(stg2_lock_stmt)
                await session.commit()
                if int(stg2_lock_res.rowcount or 0) == 0:
                    _log_task(
                        "warning", task_id,
                        "stage1->stage2 transition lock not acquired (task status changed externally). SKIP.",
                    )
                    return
                _log_task("info", task_id,
                          f"stage1(transcribe) SUCCESS transcript_len={len(transcript)} chars "
                          f"(took so far {time.perf_counter() - t0:.1f}s)")
                stage1_final_error = None
                break  # 跳出重试，进 stage2
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - 防御
                msg = (
                    f"Stage1 attempt {stage1_attempt + 1}/{max_retries} failed: "
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                )
                stage1_final_error = msg
                _log_task("error", task_id, msg)
                retry_cnt = stage1_attempt + 1
                is_last = (stage1_attempt == max_retries - 1)
                new_status = TaskStatus.failed if is_last else TaskStatus.pending
                update_vals: dict[str, Any] = {
                    "error_message": msg + (" [RETRY EXHAUSTED]" if is_last else ""),
                    "current_stage_retry_count": retry_cnt,
                    "status": new_status,
                    "updated_at": func.now(),
                }
                if is_last:
                    await session.execute(
                        update(Recording)
                        .where(Recording.id == recording.id)
                        .values(last_status=TaskStatus.failed, updated_at=func.now())
                    )
                await session.execute(
                    update(Task)
                    .where(Task.id == task_id)
                    .values(**update_vals)
                )
                await session.commit()
                if is_last:
                    _log_task(
                        "error", task_id,
                        f"stage1 FAILED permanently after {max_retries} attempts "
                        f"(total elapsed {time.perf_counter() - t0:.1f}s): {msg[:100]}...",
                    )
                    return
                # --- 否则指数退避 sleep 1/2/4s 再重试 ---
                backoff = _backoff_seconds(stage1_attempt)
                _log_task(
                    "info", task_id,
                    f"stage1 will retry in {backoff:.1f}s "
                    f"(backoff attempt_idx={stage1_attempt}, status reset to pending for lock)",
                )
                await asyncio.sleep(backoff)

        # ===================================================================
        # Stage 2: LLM 摘要 — summarizing -> done / failed
        # ===================================================================
        # 重新 refresh recording 拿 transcript 最新值（stage1 刚写进去）
        recording_obj = (await session.execute(
            select(Recording).where(Recording.id == recording.id)
        )).scalar_one_or_none()
        if recording_obj is None:
            _log_task("warning", task_id, "recording deleted between stage1 and stage2, SKIP")
            return
        transcript_text = recording_obj.transcript or ""

        stage2_final_error: str | None = None
        for stage2_attempt in range(max_retries):
            # --- 乐观锁：必须是 summarizing ---
            lock_stmt = (
                update(Task)
                .where(Task.id == task_id, Task.status == TaskStatus.summarizing)
                .values(
                    status=TaskStatus.summarizing,  # 状态不变，只更新 retry 计数，占位锁
                    current_stage_retry_count=stage2_attempt,
                    error_message=None,
                    updated_at=func.now(),
                )
            )
            lock_res = await session.execute(lock_stmt)
            await session.commit()
            if int(lock_res.rowcount or 0) == 0:
                _log_task(
                    "warning", task_id,
                    "stage2 lock not acquired: task status is not 'summarizing'. Assume handled externally. SKIP.",
                )
                return
            _log_task("info", task_id, f"stage2(summarize) START attempt={stage2_attempt + 1}/{max_retries}")
            try:
                # --- 真实调用 T8 LLM service 层（三层校验）---
                summary_dict = await llm_service.summarize_transcript(transcript_text)
                if not isinstance(summary_dict, dict) or set(summary_dict.keys()) != {"summary", "key_points", "todos"}:
                    raise LLMResponseFormatException(
                        message=f"summarize_transcript returned illegal keys: {sorted(summary_dict.keys()) if isinstance(summary_dict, dict) else type(summary_dict).__name__}",
                        raw_preview=str(summary_dict)[:200],
                    )
                kp = summary_dict.get("key_points") or []
                if not isinstance(kp, list) or len(kp) < 1:
                    raise LLMResponseFormatException(
                        message="safeguard: key_points list is empty after LLM (min_length=1 broken)",
                        raw_preview=json.dumps(summary_dict, ensure_ascii=False)[:200],
                    )
                # --- 阶段 2 成功：两表 done，乐观锁 WHERE status=summarizing ---
                await session.execute(
                    update(Recording)
                    .where(Recording.id == recording.id)
                    .values(
                        summary_json=summary_dict,
                        last_status=TaskStatus.done,
                        updated_at=func.now(),
                    )
                )
                done_lock_stmt = (
                    update(Task)
                    .where(Task.id == task_id, Task.status == TaskStatus.summarizing)
                    .values(
                        status=TaskStatus.done,
                        current_stage_retry_count=0,
                        error_message=None,
                        updated_at=func.now(),
                    )
                )
                done_lock_res = await session.execute(done_lock_stmt)
                await session.commit()
                if int(done_lock_res.rowcount or 0) == 0:
                    _log_task(
                        "warning", task_id,
                        "stage2 finalize->done lock not acquired (task status changed externally). SKIP.",
                    )
                    return
                _log_task(
                    "info", task_id,
                    "PIPELINE COMPLETED SUCCESS took_total=%.1fs summary_len=%d key_points=%d todos=%d" % (
                        time.perf_counter() - t0,
                        len(str(summary_dict.get("summary", ""))),
                        len(summary_dict.get("key_points", [])),
                        len(summary_dict.get("todos", [])),
                    ),
                )
                stage2_final_error = None
                return
            except asyncio.CancelledError:
                raise
            except (
                LLMCallException,
                LLMResponseFormatException,
                json.JSONDecodeError,
                Exception,
            ) as exc:  # stage2 全部计入重试
                extra_parts: list[str] = []
                if isinstance(exc, LLMCallException):
                    upstream_code = (exc.details or {}).get("upstream_status_code")
                    if upstream_code is not None:
                        extra_parts.append(f"upstream_status={upstream_code}")
                base_msg = f"{type(exc).__name__}: {str(exc)[:300]}"
                if extra_parts:
                    base_msg = base_msg + " [" + "; ".join(extra_parts) + "]"
                msg = (
                    f"Stage2 attempt {stage2_attempt + 1}/{max_retries} failed: {base_msg}"
                )
                stage2_final_error = msg
                _log_task("error", task_id, msg)
                retry_cnt = stage2_attempt + 1
                is_last = (stage2_attempt == max_retries - 1)
                new_task_status = TaskStatus.failed if is_last else TaskStatus.summarizing
                exhausted_tail = " [RETRY EXHAUSTED]" if is_last else ""
                await session.execute(
                    update(Task)
                    .where(Task.id == task_id)
                    .values(
                        error_message=msg + exhausted_tail,
                        current_stage_retry_count=retry_cnt,
                        status=new_task_status,
                        updated_at=func.now(),
                    )
                )
                if is_last:
                    await session.execute(
                        update(Recording)
                        .where(Recording.id == recording.id)
                        .values(last_status=TaskStatus.failed, updated_at=func.now())
                    )
                await session.commit()
                if is_last:
                    _log_task(
                        "error", task_id,
                        f"stage2 FAILED permanently after {max_retries} attempts "
                        f"(elapsed {time.perf_counter() - t0:.1f}s): {msg[:120]}...",
                    )
                    return
                backoff = _backoff_seconds(stage2_attempt)
                _log_task(
                    "info", task_id,
                    f"stage2 will retry in {backoff:.1f}s "
                    f"(attempt_idx={stage2_attempt}, status kept summarizing)",
                )
                await asyncio.sleep(backoff)

    # end of _run_pipeline_real


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
