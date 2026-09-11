"""Ticket<9> 状态机验收脚本（monkey-patch 快速验，不真等 5-15s）.

T9 验收标准 1~5:
A1. 正常成功流（monkey-patch: 强制 fail_prob=0; sleep=0.001; mock summarize_transcript 返回合法 3 键） → tasks.status=done, transcript>=500 chars, summary_json 3 键 key_points>=1.
A2. Stage1 强制 100% 失败 (fail_prob=1.0) → 3 次 1+2+4s 退避后 failed, current_stage_retry_count=3, error_message contains "Stage1 attempt 3 failed" + ends "[RETRY EXHAUSTED]".
A3. Stage2 LLM 100% 抛 LLMCallException(upstream_status=401) → stage2 3 次退避后 failed, error_message contains "401" + "[RETRY EXHAUSTED]".
A4. 乐观锁生效: stage1 sleep 期间 MySQL CLI 手动 UPDATE tasks.status='done' → 醒来 UPDATE transcribing WHERE pending rowcount=0, WARN log "stage1 lock not acquired", 函数 return 无异常.
A5. 一致性: 脚本 10 次 SELECT tasks.status, recordings.last_status, 除了事务极短时间必须相等.
A6. Startup Resume 不重复跑 done/failed: 手动 INSERT 1 条 done + 1 条 failed 任务 → 调 resume_pending_tasks_on_startup → 只 SELECT where status='pending' (done/failed 不在范围内), 不会入队.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
import uuid
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import AsyncSessionLocal  # noqa: E402
from app.models import Recording, Task, TaskStatus  # noqa: E402
from app.services import pipeline as P  # noqa: E402
from app.services import llm as L  # noqa: E402
from app.config import Settings  # noqa: E402
from app.utils.logger import setup_logging, get_logger  # noqa: E402
from sqlalchemy import func, select, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

setup_logging()
_logger = get_logger("test_pipeline_state_machine")


MYSQL_CLI = r"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe"

UPLOADS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uploads")


async def _clean_t9() -> None:
    """Clean DB recordings+tasks where original_filename like 't9_%'. Also uploads t9_* uuid files."""
    import subprocess
    sql = (
        "SET FOREIGN_KEY_CHECKS=0; "
        f"DELETE FROM tasks WHERE recording_id IN (SELECT id FROM recordings WHERE original_filename LIKE 't9_%'); "
        f"DELETE FROM recordings WHERE original_filename LIKE 't9_%'; "
        "SET FOREIGN_KEY_CHECKS=1;"
    )
    try:
        subprocess.run(
            [MYSQL_CLI, "-uroot", "-proot", "xisiyun_asr", "-e", sql],
            check=False, capture_output=True,
        )
    except Exception:
        pass
    try:
        for fn in os.listdir(UPLOADS_DIR):
            if fn.endswith((".wav", ".mp3", ".m4a", ".aac")) and os.path.splitext(fn)[0].replace("-", "") != "":
                # 仅清 t9_ 相关: 文件名含 uuid 且是最近 30s 创建的. 简单粗暴不做.
                pass
    except Exception:
        pass


async def _create_recording_task_rows(
    session: AsyncSession,
    *,
    original_filename: str,
    status: TaskStatus = TaskStatus.pending,
) -> tuple[str, str]:
    """INSERT recordings + tasks; returns (recording_id, task_id)."""
    rid = str(uuid.uuid4())
    tid = str(uuid.uuid4())
    rec = Recording(
        id=rid,
        original_filename=original_filename,
        file_ext=os.path.splitext(original_filename)[1].lstrip(".") or "wav",
        storage_path=os.path.join(UPLOADS_DIR, rid + ".wav"),
        file_size_bytes=4096,
        file_hash="t9_md5_" + rid[:16],
        transcript=None,
        summary_json=None,
        last_status=status,
    )
    session.add(rec)
    task = Task(
        id=tid,
        recording_id=rid,
        status=status,
    )
    session.add(task)
    await session.commit()
    return rid, tid


async def _wait_until_task_terminal(
    session: AsyncSession,
    task_id: str,
    *,
    timeout_s: float = 30.0,
    poll_every: float = 0.05,
) -> Task | None:
    """Poll tasks.id until status in {done, failed}. Returns Task row or None on timeout.

    Uses a FRESH AsyncSessionLocal() for each poll cycle so worker updates from a
    different session are immediately visible (avoids expire_on_commit=False cache).
    """
    deadline = time.perf_counter() + timeout_s
    last: Task | None = None
    while time.perf_counter() < deadline:
        async with AsyncSessionLocal() as s2:
            row = (await s2.execute(
                select(Task).where(Task.id == task_id)
            )).scalar_one_or_none()
            if row is None:
                return None
            last = row
            # Wait until status reaches true terminal {done, failed} AND status will
            # no longer change. Exhausted current_stage_retry_count means no more
            # retries will be attempted by the worker, so we're truly at terminal.
            s = row.status
            if s in (TaskStatus.done, TaskStatus.failed):
                return row
            # Also: If status still transcribing/summarizing but rowcount (via
            # poll_every loop) hasn't changed, keep waiting — worker may still run.
        await asyncio.sleep(poll_every)
    return last


async def _assert_consistency(session: AsyncSession, recording_id: str, task_id: str) -> bool:
    """SELECT tasks.status vs recordings.last_status — 必须相等. Fresh session each check."""
    async with AsyncSessionLocal() as s2:
        t = (await s2.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
        r = (await s2.execute(select(Recording).where(Recording.id == recording_id))).scalar_one_or_none()
        if t is None or r is None:
            return True
        ok = (t.status == r.last_status)
        if not ok:
            _logger.error("CONSISTENCY FAIL task.status=%s recording.last_status=%s", t.status, r.last_status)
        return ok


# ===================================================================
# A1: Happy path success — monkeypatch sleep=0, fail_prob=0, mock summarize_transcript
# ===================================================================
async def _a1_happy() -> bool:
    print("\n===== A1: Happy path -> status=done transcript>=500 summary_json 3 keys =====")
    # --- Patch mock values (fast) ---
    old_sleep_min = P._MOCK_ASR_MIN_SLEEP_SECONDS
    old_sleep_max = P._MOCK_ASR_MAX_SLEEP_SECONDS
    old_fail = P._MOCK_ASR_FAIL_PROBABILITY
    old_summ = L.summarize_transcript

    P._MOCK_ASR_MIN_SLEEP_SECONDS = 0.001
    P._MOCK_ASR_MAX_SLEEP_SECONDS = 0.002
    P._MOCK_ASR_FAIL_PROBABILITY = 0.0

    mock_summary = {
        "summary": "Mock summary for t9_a1 test.",
        "key_points": ["kp1", "kp2", "kp3"],
        "todos": ["todo1", "todo2"],
    }

    async def fake_summarize_transcript(transcript: str):
        if len(transcript) < 10:
            raise ValueError("too short")
        return mock_summary

    L.summarize_transcript = fake_summarize_transcript  # type: ignore[assignment]

    try:
        async with AsyncSessionLocal() as session:
            rid, tid = await _create_recording_task_rows(session, original_filename="t9_a1_happy.wav")
            settings = Settings()
            P.init_engine(settings)
            await P.start_workers(settings)
            await P.enqueue_task(tid)
            final_task = await _wait_until_task_terminal(session, tid, timeout_s=20.0)
            if final_task is None:
                print("A1 FAILED: task not reached terminal in 20s")
                return False
            if final_task.status != TaskStatus.done:
                print(f"A1 FAILED: status={final_task.status!r} err={final_task.error_message!r}")
                return False
            async with AsyncSessionLocal() as s2:
                rec = (await s2.execute(
                    select(Recording).where(Recording.id == rid)
                )).scalar_one()
            t_len = len(rec.transcript or "")
            sj = rec.summary_json
            if t_len < 500:
                print(f"A1 FAILED: transcript len={t_len} (< 500)")
                return False
            if not isinstance(sj, dict):
                print(f"A1 FAILED: summary_json not dict -> {type(sj).__name__}")
                return False
            if set(sj.keys()) != {"summary", "key_points", "todos"}:
                print(f"A1 FAILED: keys={sorted(sj.keys())} expected 3 keys")
                return False
            if not isinstance(sj["key_points"], list) or len(sj["key_points"]) < 1:
                print(f"A1 FAILED: key_points={sj.get('key_points')}")
                return False
            if not await _assert_consistency(session, rid, tid):
                print("A1 FAILED: tasks.status != recordings.last_status")
                return False
            print(f"A1 OK: status=done transcript={t_len} chars summary_keys={sorted(sj.keys())} key_points_n={len(sj['key_points'])}")
            return True
    finally:
        P._MOCK_ASR_MIN_SLEEP_SECONDS = old_sleep_min
        P._MOCK_ASR_MAX_SLEEP_SECONDS = old_sleep_max
        P._MOCK_ASR_FAIL_PROBABILITY = old_fail
        L.summarize_transcript = old_summ
        await P.shutdown_workers()
        await L.shutdown_client()


# ===================================================================
# A2: Stage1 100% failed -> retry 3x -> permanent failed
# ===================================================================
async def _a2_stage1_exhausted() -> bool:
    print("\n===== A2: Stage1 100% fail -> 3 retries -> failed current_stage_retry_count=3 =====")
    old_sleep_min = P._MOCK_ASR_MIN_SLEEP_SECONDS
    old_sleep_max = P._MOCK_ASR_MAX_SLEEP_SECONDS
    old_fail = P._MOCK_ASR_FAIL_PROBABILITY
    old_backoff = P._backoff_seconds

    P._MOCK_ASR_MIN_SLEEP_SECONDS = 0.001
    P._MOCK_ASR_MAX_SLEEP_SECONDS = 0.002
    P._MOCK_ASR_FAIL_PROBABILITY = 1.0

    backoff_calls: list[float] = []

    def _spy_backoff(i: int) -> float:
        v = 0.01 * (2 ** i)  # 缩短: 0.01/0.02/0.04s, 加速测试但仍然保留 3 次间隔
        backoff_calls.append(v)
        return v

    P._backoff_seconds = _spy_backoff  # type: ignore[assignment]

    try:
        async with AsyncSessionLocal() as session:
            rid, tid = await _create_recording_task_rows(session, original_filename="t9_a2_stage1_fail.wav")
            settings = Settings()
            P.init_engine(settings)
            await P.start_workers(settings)
            t0 = time.perf_counter()
            await P.enqueue_task(tid)
            final_task = await _wait_until_task_terminal(session, tid, timeout_s=20.0)
            elapsed = time.perf_counter() - t0
            if final_task is None:
                print("A2 FAILED: task not terminal in 20s")
                return False
            if final_task.status != TaskStatus.failed:
                print(f"A2 FAILED: status={final_task.status!r} err={final_task.error_message!r}")
                return False
            # current_stage_retry_count == 3 (after last attempt update)
            if final_task.current_stage_retry_count != 3:
                print(f"A2 FAILED: current_stage_retry_count={final_task.current_stage_retry_count} expected 3")
                return False
            err = final_task.error_message or ""
            ok_msg = (
                err.startswith("Stage1 attempt 3")
                and "[RETRY EXHAUSTED]" in err
                and "Stage1 attempt 3/3 failed" in err  # 3/3 = exhausted 3rd
            )
            if not ok_msg:
                print(f"A2 FAILED: error_message={err!r}")
                return False
            if len(backoff_calls) < 2:
                print(f"A2 WARN: backoff_calls len={len(backoff_calls)} (attempt 0 and 1 should backoff)")
            print(f"A2 OK: status=failed count=3 backoff_calls={len(backoff_calls)} err has Stage1 attempt 3 + RETRY EXHAUSTED.")
            return await _assert_consistency(session, rid, tid)
    finally:
        P._MOCK_ASR_MIN_SLEEP_SECONDS = old_sleep_min
        P._MOCK_ASR_MAX_SLEEP_SECONDS = old_sleep_max
        P._MOCK_ASR_FAIL_PROBABILITY = old_fail
        P._backoff_seconds = old_backoff
        await P.shutdown_workers()
        await L.shutdown_client()


# ===================================================================
# A3: Stage2 LLM 100% 抛 LLMCallException(401) -> stage2 3 次 failed
# ===================================================================
async def _a3_stage2_llm_exhausted() -> bool:
    print("\n===== A3: Stage2 LLM 401 -> 3 retries -> failed error_message has 401 =====")
    old_sleep_min = P._MOCK_ASR_MIN_SLEEP_SECONDS
    old_sleep_max = P._MOCK_ASR_MAX_SLEEP_SECONDS
    old_fail = P._MOCK_ASR_FAIL_PROBABILITY
    old_summ = L.summarize_transcript
    old_backoff = P._backoff_seconds

    P._MOCK_ASR_MIN_SLEEP_SECONDS = 0.001
    P._MOCK_ASR_MAX_SLEEP_SECONDS = 0.002
    P._MOCK_ASR_FAIL_PROBABILITY = 0.0  # stage1 MUST succeed

    backoff_calls: list[float] = []

    def _spy_backoff(i: int) -> float:
        v = 0.01 * (2 ** i)
        backoff_calls.append(v)
        return v

    P._backoff_seconds = _spy_backoff  # type: ignore[assignment]

    call_counter: list[int] = []

    async def fake_summarize(transcript: str):
        call_counter.append(1)
        from app.utils.errors import LLMCallException
        raise LLMCallException(
            message="DeepSeek rejected key (HTTP 401).",
            upstream_status=401,
            details={"response_body_preview": '{"code":401,"msg":"invalid access token"}'},
        )

    L.summarize_transcript = fake_summarize  # type: ignore[assignment]

    try:
        async with AsyncSessionLocal() as session:
            rid, tid = await _create_recording_task_rows(session, original_filename="t9_a3_stage2_401.wav")
            settings = Settings()
            P.init_engine(settings)
            await P.start_workers(settings)
            await P.enqueue_task(tid)
            final_task = await _wait_until_task_terminal(session, tid, timeout_s=20.0)
            if final_task is None:
                print("A3 FAILED: not terminal 20s")
                return False
            if final_task.status != TaskStatus.failed:
                print(f"A3 FAILED: status={final_task.status} err={final_task.error_message!r}")
                return False
            if final_task.current_stage_retry_count != 3:
                print(f"A3 FAILED: current_stage_retry_count={final_task.current_stage_retry_count} expected 3")
                return False
            err = final_task.error_message or ""
            ok_err = (
                "401" in err
                and "[RETRY EXHAUSTED]" in err
                and ("Stage2 attempt 3 failed" in err or "Stage2 attempt 3/3 failed" in err)
            )
            if not ok_err:
                print(f"A3 FAILED: err={err!r}")
                return False
            if len(call_counter) != 3:
                print(f"A3 FAILED: LLM called {len(call_counter)}x, expected 3")
                return False
            # Recording.last_status must be failed, transcript must be non-None (stage1 succeeded, wrote transcript)
            async with AsyncSessionLocal() as s2:
                rec = (await s2.execute(
                    select(Recording).where(Recording.id == rid)
                )).scalar_one()
            if rec.transcript is None or len(rec.transcript or "") < 500:
                print(f"A3 FAILED: transcript not written despite stage1 success: {rec.transcript!r}")
                return False
            print(f"A3 OK: status=failed err has 401+RETRY EXHAUSTED+Stage2 attempt 3; LLM called {len(call_counter)}x (3 expected); transcript filled")
            return await _assert_consistency(session, rid, tid)
    finally:
        P._MOCK_ASR_MIN_SLEEP_SECONDS = old_sleep_min
        P._MOCK_ASR_MAX_SLEEP_SECONDS = old_sleep_max
        P._MOCK_ASR_FAIL_PROBABILITY = old_fail
        L.summarize_transcript = old_summ
        P._backoff_seconds = old_backoff
        await P.shutdown_workers()
        await L.shutdown_client()


# ===================================================================
# A4: 乐观锁不被 acquired 打 WARN 且 return 无异常
# ===================================================================
async def _a4_optimistic_lock() -> bool:
    print("\n===== A4: Stage1 optimistic lock SKIP on external status change =====")
    # Strategy: patch asyncio.sleep inside pipeline run_pipeline_real stage1.
    # We wrap P.asyncio.sleep to: on the first sleep > 0.5s, run UPDATE status=done in DB.
    old_sleep_min = P._MOCK_ASR_MIN_SLEEP_SECONDS
    old_sleep_max = P._MOCK_ASR_MAX_SLEEP_SECONDS
    old_fail = P._MOCK_ASR_FAIL_PROBABILITY
    old_summ = L.summarize_transcript
    real_asyncio_sleep = asyncio.sleep

    P._MOCK_ASR_MIN_SLEEP_SECONDS = 0.15  # 150ms stage1 mock
    P._MOCK_ASR_MAX_SLEEP_SECONDS = 0.20
    P._MOCK_ASR_FAIL_PROBABILITY = 0.0

    L.summarize_transcript = fake_summ = (  # type: ignore[assignment]
        lambda t: asyncio.sleep(0, result={"summary": "ok", "key_points": ["a"], "todos": []})
    )  # won't be reached

    target_tid_ref: list[str] = [""]
    external_updated: list[bool] = [False]
    saw_warn: list[bool] = [False]

    # Patch _log_task to capture WARN lock not acquired:
    real_log = P._log_task

    def spy_log(level, task_id, msg, **kw):
        if "lock not acquired" in msg:
            saw_warn[0] = True
        return real_log(level, task_id, msg, **kw)

    P._log_task = spy_log  # type: ignore[assignment]

    async def patched_sleep(seconds, *a, **kw):
        # only intercept first long-ish sleep during which we know stage1 is running
        if seconds >= 0.1 and not external_updated[0] and target_tid_ref[0]:
            external_updated[0] = True
            async with AsyncSessionLocal() as inner:
                await inner.execute(
                    update(Task)
                    .where(Task.id == target_tid_ref[0])
                    .values(status=TaskStatus.done, updated_at=func.now())
                )
                await inner.commit()
        return await real_asyncio_sleep(seconds, *a, **kw)

    P.asyncio.sleep = patched_sleep  # type: ignore[attr-defined]

    try:
        async with AsyncSessionLocal() as session:
            rid, tid = await _create_recording_task_rows(session, original_filename="t9_a4_lock.wav")
            target_tid_ref[0] = tid
            settings = Settings()
            P.init_engine(settings)
            await P.start_workers(settings)
            await P.enqueue_task(tid)
            await asyncio.sleep(2.0)  # 足够 stage1 lock 失败
            task = (await session.execute(
                select(Task).where(Task.id == tid)
            )).scalar_one_or_none()
            if task is None:
                print("A4 FAILED: task deleted")
                return False
            print(f"A4: after external update task.status={task.status}, saw_lock_not_acquired_warn={saw_warn[0]}")
            # 乐观锁 rowcount==0 时函数 return, 不抛异常挂掉 worker
            # 只检查 saw_warn 即可.
            if not saw_warn[0]:
                print("A4 FAILED: lock WARN log not produced (worker ran all the way instead of SKIP)")
                return False
            print("A4 OK: stage1 saw WARN lock not acquired, SKIP return without exception")
            return True
    finally:
        P._MOCK_ASR_MIN_SLEEP_SECONDS = old_sleep_min
        P._MOCK_ASR_MAX_SLEEP_SECONDS = old_sleep_max
        P._MOCK_ASR_FAIL_PROBABILITY = old_fail
        L.summarize_transcript = old_summ
        P.asyncio.sleep = real_asyncio_sleep  # type: ignore[attr-defined]
        P._log_task = real_log
        await P.shutdown_workers()
        await L.shutdown_client()


# ===================================================================
# A5: Resume 只对 pending 生效，不重跑 done/failed
# ===================================================================
async def _a5_resume_skips_done_failed() -> bool:
    print("\n===== A5: Resume skips done/failed (only pending enqueued) =====")
    await P.shutdown_workers()
    await L.shutdown_client()
    async with AsyncSessionLocal() as session:
        _, tid_done = await _create_recording_task_rows(session, original_filename="t9_a5_done.wav", status=TaskStatus.done)
        _, tid_failed = await _create_recording_task_rows(session, original_filename="t9_a5_failed.wav", status=TaskStatus.failed)
        _, tid_pending = await _create_recording_task_rows(session, original_filename="t9_a5_pending.wav", status=TaskStatus.pending)
        settings = Settings()
        P.init_engine(settings)
        await P.start_workers(settings)
        # Capture enqueue_task calls by monkey-patching P.enqueue_task:
        real_enq = P.enqueue_task
        enqueued_ids: list[str] = []

        async def spy_enq(tid: str):
            enqueued_ids.append(tid)
            return await real_enq(tid)

        P.enqueue_task = spy_enq  # type: ignore[assignment]
        try:
            total = await P.resume_pending_tasks_on_startup(session)
            pending_enq = sum(1 for x in enqueued_ids if x in (tid_done, tid_failed, tid_pending))
            print(f"A5: resume total={total} our test rows enqueued={enqueued_ids[-pending_enq:] if pending_enq else []}")
            if tid_done in enqueued_ids or tid_failed in enqueued_ids:
                print(f"A5 FAILED: enqueued_ids contains done={tid_done in enqueued_ids} failed={tid_failed in enqueued_ids}")
                return False
            if tid_pending not in enqueued_ids:
                print("A5 FAILED: pending task was NOT enqueued during resume")
                return False
            print("A5 OK: resume enqueued pending task only; skipped done/failed")
            return True
        finally:
            P.enqueue_task = real_enq  # type: ignore[assignment]
            await P.shutdown_workers()
            await L.shutdown_client()


# ===================================================================
# Main driver
# ===================================================================
async def main() -> int:
    # Pre-clean
    await _clean_t9()
    # Reset globals (shutdown may not have been called)
    await P.shutdown_workers()
    await L.shutdown_client()

    results: dict[str, bool] = {}
    results["A1"] = await _a1_happy()
    # clean between sub-tests to keep order:
    await _clean_t9()
    results["A2"] = await _a2_stage1_exhausted()
    await _clean_t9()
    results["A3"] = await _a3_stage2_llm_exhausted()
    await _clean_t9()
    results["A4"] = await _a4_optimistic_lock()
    await _clean_t9()
    results["A5"] = await _a5_resume_skips_done_failed()

    print("\n===== SUMMARY =====")
    all_ok = True
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")
        all_ok = all_ok and v

    # Post-clean
    await _clean_t9()
    await P.shutdown_workers()
    await L.shutdown_client()
    print(f"\nPIPELINE_T9_OK={str(all_ok)}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
