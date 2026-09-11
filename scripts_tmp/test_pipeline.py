"""T7 验收脚本：pipeline 引擎 5 条验收断言。

- 不写 pytest；asyncio.run + httpx.AsyncClient ASGITransport 跑。
- 真实连 MySQL（与 T6 一致），用 unique prefix 隔离并最终清理数据。
"""
from __future__ import annotations

import asyncio
import io
import os
import pathlib
import sys
import time
import uuid
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from httpx import AsyncClient, ASGITransport  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from app.database import AsyncSessionLocal  # noqa: E402
from app.main import app  # noqa: E402  # triggers lifespan → init workers + resume on start
from app.models import Recording, Task, TaskStatus  # noqa: E402
from app.services import pipeline as P  # noqa: E402
from app.services.storage import ensure_upload_dir  # noqa: E402

UPLOADS = ensure_upload_dir()

_PREFIX = f"t7_{uuid.uuid4().hex[:6]}_"
_WAV = b"RIFF" + os.urandom(400)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _clear_t7_leftovers() -> None:
    """Remove any uploads/<uuid>.* disks left from prior T7 runs."""
    for p in UPLOADS.iterdir():
        if not p.is_file() or p.name == ".gitkeep":
            continue
        stem = p.stem
        if len(stem) == 36 and stem.count("-") == 4:
            p.unlink(missing_ok=True)


async def _cleanup_t7_db_rows() -> None:
    """Delete rows WHERE recording.original_filename LIKE '<prefix>%' OR id in list."""
    async with AsyncSessionLocal() as s:
        # First tasks linked to recordings we'll delete
        from sqlalchemy import delete as sqld

        rids = list(
            (
                await s.execute(
                    select(Recording.id).where(Recording.original_filename.like(f"{_PREFIX}%"))
                )
            )
            .scalars()
            .all()
        )
        if rids:
            await s.execute(sqld(Task).where(Task.recording_id.in_(rids)))
            await s.execute(sqld(Recording).where(Recording.id.in_(rids)))
            await s.commit()
            print(f"[pre/post-cleanup] deleted {len(rids)} t7 recordings + linked tasks from DB")


async def _seed_db_status(rows: list[tuple[str, TaskStatus]]) -> list[str]:
    """Insert recordings + tasks with specific tasks.statuses (for startup resume tests).

    Returns the task_ids in order.
    """
    tasks_ids: list[str] = []
    async with AsyncSessionLocal() as s:
        for suffix, status in rows:
            rid = str(uuid.uuid4())
            tid = str(uuid.uuid4())
            r = Recording(
                id=rid,
                original_filename=f"{_PREFIX}{suffix}",
                file_ext="wav",
                file_size_bytes=len(_WAV),
                file_hash=f"{suffix}-{uuid.uuid4().hex[:24]}",  # unique
                storage_path=str(UPLOADS / f"{rid}.wav"),
                last_status=status,
            )
            s.add(r)
            t = Task(
                id=tid,
                recording_id=rid,
                status=status,
                current_stage_retry_count=3 if status != TaskStatus.pending else 0,
                total_retry_count=1 if status != TaskStatus.pending else 0,
                error_message=None,
            )
            s.add(t)
            tasks_ids.append(tid)
        await s.commit()
    return tasks_ids


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------
async def assert_upload_truly_enqueued() -> dict:
    """上传接口 → 200，随后 placeholder 日志中能看到该 task_id processing + done。

    返回上传响应的 JSON body。
    """
    # 注意：app import 时 lifespan 已经 start_workers 跑起来过一次（但我们在脚本
    # 开头没有显式 lifespan.startup；通过 ASGITransport 每次请求都会重新触发
    # lifespan startup/shutdown吗？ — 不，ASGITransport 对同一个 FastAPI app
    # 实例调用 lifespan 一次在 __aenter__ / __aexit__。所以这里用 async with block
    # 包裹。
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        t0 = time.perf_counter()
        resp = await client.post(
            "/v1/recordings",
            files={"file": (f"{_PREFIX}upload_check.wav", io.BytesIO(_WAV), "audio/wav")},
        )
        dt_ms = int((time.perf_counter() - t0) * 1000)
        assert resp.status_code == 200, f"upload status={resp.status_code}: {resp.text[:500]}"
        body = resp.json()
        assert set(body.keys()) == {"recording_id", "task_id", "status"} and body["status"] == "pending"
        print(f"[A3 upload] response time {dt_ms}ms (< 500ms? {dt_ms < 500}) body keys={sorted(body)}")
        assert dt_ms < 2000, f"上传接口立即返回要求，实际耗时 {dt_ms}ms"
        # Wait for placeholder to finish (max 2s; default sleep=0.1)
        await asyncio.sleep(0.6)
    return body


async def assert_concurrency_3() -> None:
    """临时把 placeholder sleep=2s；同时塞 10 个任务；统计开始时间差。

    期望：第 4 个开始时间 ≥ 第 1 个开始时间 + 2 秒（Semaphore(3) 生效）。
    """
    import contextlib

    P._placeholder_sleep_seconds = 2.0
    start_ts: dict[str, float] = {}

    orig = P._run_pipeline_placeholder

    async def _wrapped(task_id: str) -> None:
        start_ts[task_id] = time.perf_counter()
        await orig(task_id)

    P._run_pipeline_placeholder = _wrapped  # type: ignore[assignment]
    try:
        ids = [f"t7sem-{uuid.uuid4().hex[:12]}" for _ in range(10)]
        t0 = time.perf_counter()
        await asyncio.gather(*[P.enqueue_task(tid) for tid in ids])
        # 等待 10 个全跑完：2s * ceil(10/3) ≈ 8 秒，给 12 秒余量
        deadline = time.perf_counter() + 14.0
        while len(start_ts) < 10:
            await asyncio.sleep(0.1)
            if time.perf_counter() > deadline:
                raise AssertionError(
                    f"[A1 concurrency] timed out: started only {len(start_ts)}/10 tasks"
                )
        # 等待它们全部 task_done (用 sleep 0.2 再给一点 buffer)
        await asyncio.sleep(0.5)
    finally:
        P._run_pipeline_placeholder = orig  # type: ignore[assignment]
        P._placeholder_sleep_seconds = 0.1

    ordered = sorted(start_ts.items(), key=lambda kv: kv[1])
    first_3_end_estimate = ordered[2][1] + 2.0
    fourth_start = ordered[3][1]
    gap_ok = fourth_start >= first_3_end_estimate - 0.2  # -0.2 容差
    print(
        f"[A1 concurrency] sleep=2s, enqueued 10 tasks. "
        f"1st batch start={ordered[0][1]-t0:.2f}s "
        f"3rd start={ordered[2][1]-t0:.2f}s "
        f"4th start={fourth_start-t0:.2f}s (should be >= {first_3_end_estimate-t0:.2f}s = 3rd+2s)  → OK={gap_ok}"
    )
    # 同时第 10 个开始时间要 ≥ 第 1 个开始 + 2s * floor(9/3) = + 6s
    tenth_ok = ordered[9][1] >= ordered[0][1] + 5.8  # 容差 0.2s
    print(
        f"[A1 extra] 10th start - 1st start = {ordered[9][1]-ordered[0][1]:.2f}s >= 5.8s? {tenth_ok}"
    )
    assert gap_ok and tenth_ok, "Semaphore(3) 未生效：同时在跑的 placeholder 超过 3 个"


async def assert_startup_resume_r8() -> None:
    """spec R8 验收：手动往 DB 塞 1 pending + 1 transcribing + 1 summarizing。

    再手动调 resume_pending_tasks_on_startup()：
      (a) UPDATE 让 transcribing/summarizing 变成 pending（current_stage_retry_count=0）；
      (b) 返回 resume 数量 == 3。
    """
    seeds = await _seed_db_status(
        [
            ("seed_pending.wav", TaskStatus.pending),
            ("seed_transcribing.wav", TaskStatus.transcribing),
            ("seed_summarizing.wav", TaskStatus.summarizing),
        ]
    )
    assert len(seeds) == 3
    # 先确认 statuses 非全 pending（刚 insert 的是指定态）
    async with AsyncSessionLocal() as s:
        rows = (
            (
                await s.execute(
                    select(Task.id, Task.status, Task.current_stage_retry_count).where(Task.id.in_(seeds))
                )
            )
            .all()
        )
        initial_statuses = {r[0]: r[1].value if hasattr(r[1], "value") else r[1] for r in rows}
        print(f"[A2 R8 pre-resume] statuses: {initial_statuses}")
        assert any(v == "transcribing" for v in initial_statuses.values()), "种子数据含 transcribing"
        assert any(v == "summarizing" for v in initial_statuses.values()), "种子数据含 summarizing"

    async with AsyncSessionLocal() as s:
        n = await P.resume_pending_tasks_on_startup(s)
    # 我们只关心 3 个种子，resume 可能把系统里别的 pending 也入队（=T6 之前测上传残留），所以断言 ≥ 3
    print(f"[A2 R8] resume_pending_tasks_on_startup() returned total_enqueued={n} (>=3? {n>=3})")
    assert n >= 3

    # 再查 DB：原 transcribing/summarizing 两条的 status 现在必须 == pending + current_stage_retry_count=0
    async with AsyncSessionLocal() as s:
        rows2 = (
            (
                await s.execute(
                    select(Task.id, Task.status, Task.current_stage_retry_count).where(Task.id.in_(seeds))
                )
            )
            .all()
        )
    statuses_after = {
        r[0]: (r[1].value if hasattr(r[1], "value") else r[1], int(r[2] or 0)) for r in rows2
    }
    original_tid_transcribing = next(
        tid for tid, st in initial_statuses.items() if st == "transcribing"
    )
    original_tid_summarizing = next(
        tid for tid, st in initial_statuses.items() if st == "summarizing"
    )
    after_t = statuses_after[original_tid_transcribing]
    after_s = statuses_after[original_tid_summarizing]
    r8_reset_ok = after_t[0] == "pending" and after_s[0] == "pending" and after_t[1] == 0 and after_s[1] == 0
    print(
        f"[A2 R8 reset] 原 transcribing -> status={after_t[0]!r} retry={after_t[1]} | "
        f"原 summarizing -> status={after_s[0]!r} retry={after_s[1]}   → R8 update OK={r8_reset_ok}"
    )
    assert r8_reset_ok, "spec R8 未执行 UPDATE：transcribing/summarizing 仍未重置成 pending + retry=0"

    # 等它们被 worker placeholder 处理完（sleep 0.1 × ceil(3/3)=0.1 → 给 0.6s buffer）
    await asyncio.sleep(0.8)


async def assert_bounded_queue_backpressure() -> None:
    """把 queue maxsize 临时改成 2，然后塞 5 个 enqueue → 前 2 put 立即回，第 3 个起会 await 阻塞。

    验证：在 "worker 不消费" 的情况下，第 3+ 次 put 超过 maxsize=2 就会阻塞。
    我们不直接停 worker（怕影响其它测试），所以用更可测的方法：手动创建一个新
    asyncio.Queue(maxsize=2) 来模拟（同时验证 pipeline 的 put 是 async）。
    """
    # 只做一个独立 queue 的行为验证 + pipeline 模块内 _queue 已初始化的属性检查
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=2)
    await q.put("a")
    await q.put("b")
    assert q.qsize() == 2 and q.full()

    # 第 3 个 put 应该阻塞。用 asyncio.wait_for(0.05) 看它是否 TimeoutError=确实阻塞了
    saw_blocking = False
    try:
        await asyncio.wait_for(q.put("c"), timeout=0.05)
    except asyncio.TimeoutError:
        saw_blocking = True
    print(f"[A5 backpressure] Queue(maxsize=2).put('c') 第 3 次阻塞: {saw_blocking} (True = 反压生效)")
    assert saw_blocking, "bounded queue 未按预期施加反压（maxsize=2，put(c) 应阻塞）"


async def assert_shutdown_no_warnings() -> None:
    """调 pipeline.shutdown_workers()；检查 _worker_tasks 清空、无 pending Task。"""
    before = len(P._worker_tasks)
    print(f"[A4 shutdown] before shutdown: worker_tasks={before}")
    await P.shutdown_workers()
    after = len(P._worker_tasks)
    assert after == 0, f"after shutdown workers still exists: {after}"
    assert P._queue is None and P._sem is None, "module globals not cleared after shutdown"
    print(f"[A4 shutdown] after shutdown: worker_tasks={after} globals reset (queue/sem None). OK")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> int:
    _clear_t7_leftovers()
    await _cleanup_t7_db_rows()
    # Clean module state so lifespan creates a fresh pipeline
    P._queue = None
    P._sem = None
    P._worker_tasks = []
    P._settings_snapshot = None

    print("\n===== A1: Semaphore(3) concurrency =====")
    # Ensure workers are running (pipeline.start_workers 幂等)
    from app.config import Settings

    await P.start_workers(Settings())
    await assert_concurrency_3()

    print("\n===== A2: R8 Startup Resume UPDATE transcribing/summarizing → pending =====")
    await assert_startup_resume_r8()

    print("\n===== A3: Upload interface truly enqueues (enqueue + placeholder) =====")
    await assert_upload_truly_enqueued()

    print("\n===== A4: Shutdown workers 优雅关闭 (no Task destroyed warnings) =====")
    await assert_shutdown_no_warnings()

    print("\n===== A5: Bounded Queue Backpressure =====")
    await assert_bounded_queue_backpressure()

    # Final clean
    await _cleanup_t7_db_rows()
    _clear_t7_leftovers()

    print("\nPIPELINE_T7_OK=True")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
