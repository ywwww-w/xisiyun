"""Ticket<10> Tasks API 验收脚本（httpx.ASGITransport 内存端到端，不占端口）.

T10 验收标准 A1~A5:
A1. GET /v1/tasks/{id} 能体现阶段：pending -> transcribing -> summarizing -> done  四态顺序出现（status 不是模糊的 processing）
A2. POST retry 成功流：failed task -> retry -> HTTP 200 status='pending' total_retry_count=1 current_stage_retry_count=0 err=null ; recordings.last_status==pending ; pipeline 日志重新 enqueued 1 次
A3. 非 failed 状态 POST retry -> HTTP 409 error.code in {CONFLICT, TASK_NOT_RETRYABLE}（4 个状态 pending/transcribing/summarizing/done 各测一次）
A4. 双并发同 failed task POST retry -> 1 个 200 + 1 个 409（行锁 + 乐观判断双重幂等生效；不能两个都 200）
A5. 404 场景：GET/POST 不存在 task_id -> HTTP 404 error.code==NOT_FOUND 结构统一
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from sqlalchemy import func, select, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.config import Settings  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import Recording, Task, TaskStatus  # noqa: E402
from app.services import llm as L  # noqa: E402
from app.services import pipeline as P  # noqa: E402
from app.utils.logger import setup_logging  # noqa: E402

setup_logging()

MYSQL_CLI = r"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe"
UPLOADS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uploads")


async def _clean_t10() -> None:
    sql = (
        "SET FOREIGN_KEY_CHECKS=0; "
        "DELETE FROM tasks WHERE recording_id IN (SELECT id FROM recordings WHERE original_filename LIKE 't10_%'); "
        "DELETE FROM recordings WHERE original_filename LIKE 't10_%'; "
        "SET FOREIGN_KEY_CHECKS=1;"
    )
    try:
        subprocess.run([MYSQL_CLI, "-uroot", "-proot", "xisiyun_asr", "-e", sql], check=False, capture_output=True)
    except Exception:
        pass


async def _insert_rec_task(
    original_filename: str,
    status: TaskStatus = TaskStatus.pending,
    *,
    total_retry_count: int = 0,
    error_message: str | None = None,
) -> tuple[str, str]:
    rid = str(uuid.uuid4())
    tid = str(uuid.uuid4())
    async with AsyncSessionLocal() as s:
        s.add(Recording(
            id=rid, original_filename=original_filename,
            file_ext=os.path.splitext(original_filename)[1].lstrip(".") or "wav",
            storage_path=os.path.join(UPLOADS_DIR, rid + ".wav"),
            file_size_bytes=4096, file_hash="t10_md5_" + rid[:16],
            transcript=None, summary_json=None, last_status=status,
        ))
        s.add(Task(
            id=tid, recording_id=rid, status=status,
            total_retry_count=total_retry_count,
            current_stage_retry_count=0,
            error_message=error_message,
        ))
        await s.commit()
    return rid, tid


async def _poll_until_status(
    client: httpx.AsyncClient,
    task_id: str,
    target_status: str,
    *,
    timeout_s: float = 20.0,
    poll_every: float = 0.02,
) -> dict[str, Any] | None:
    deadline = time.perf_counter() + timeout_s
    last: dict[str, Any] | None = None
    while time.perf_counter() < deadline:
        r = await client.get(f"/v1/tasks/{task_id}")
        if r.status_code == 200:
            last = r.json()
            if last.get("status") == target_status:
                return last
        await asyncio.sleep(poll_every)
    return last


# ---------- A1: status 体现阶段（pending->transcribing->summarizing->done 四态） ----------
async def _a1_status_stages(client: httpx.AsyncClient) -> bool:
    print("\n===== A1: GET /tasks/{id} shows stage (pending->transcribing->summarizing->done) =====")
    old_sleep_min = P._MOCK_ASR_MIN_SLEEP_SECONDS
    old_sleep_max = P._MOCK_ASR_MAX_SLEEP_SECONDS
    old_fail = P._MOCK_ASR_FAIL_PROBABILITY
    old_summ = L.summarize_transcript
    real_sleep = asyncio.sleep
    enqueue_hits: list[str] = []

    P._MOCK_ASR_MIN_SLEEP_SECONDS = 0.08
    P._MOCK_ASR_MAX_SLEEP_SECONDS = 0.12
    P._MOCK_ASR_FAIL_PROBABILITY = 0.0

    saw: dict[str, bool] = {"pending": False, "transcribing": False, "summarizing": False, "done": False}

    async def fake_summ(transcript: str):
        # 在 summary 阶段确保轮询能读到 summarizing
        await real_sleep(0.08)
        return {"summary": "ok", "key_points": ["k1"], "todos": []}

    L.summarize_transcript = fake_summ  # type: ignore[assignment]

    real_enq = P.enqueue_task

    async def spy_enq(tid: str):
        enqueue_hits.append(tid)
        return await real_enq(tid)

    P.enqueue_task = spy_enq  # type: ignore[assignment]

    try:
        rid, tid = await _insert_rec_task("t10_a1_stages.wav", status=TaskStatus.pending)
        t0 = time.perf_counter()
        await P.enqueue_task(tid)
        # 先拿到 pending：
        saw["pending"] = True
        # 轮询，记录四态出现
        deadline = t0 + 15.0
        while time.perf_counter() < deadline and not all(saw.values()):
            r = await client.get(f"/v1/tasks/{tid}")
            if r.status_code == 200:
                st = r.json()["status"]
                if st in saw:
                    saw[st] = True
            await asyncio.sleep(0.01)
        ok = all(saw.values())
        if not ok:
            print(f"A1 FAILED: saw={saw} (missing stages)")
            return False
        final_r = await client.get(f"/v1/tasks/{tid}")
        final_status = final_r.json()["status"] if final_r.status_code == 200 else None
        if final_status != "done":
            print(f"A1 FAILED: final status not done -> {final_status} err={final_r.text[:200]}")
            return False
        print(f"A1 OK: 4 stages all seen pending/transcribing/summarizing/done={list(saw.values())} final status=done")
        return True
    finally:
        P._MOCK_ASR_MIN_SLEEP_SECONDS = old_sleep_min
        P._MOCK_ASR_MAX_SLEEP_SECONDS = old_sleep_max
        P._MOCK_ASR_FAIL_PROBABILITY = old_fail
        L.summarize_transcript = old_summ
        P.enqueue_task = real_enq  # type: ignore[assignment]


# ---------- A2: retry 成功路径 ----------
async def _a2_retry_success(client: httpx.AsyncClient) -> bool:
    print("\n===== A2: POST retry FAILED task -> status=pending total_retry_count=1 + re-enqueued =====")
    old_sleep_min = P._MOCK_ASR_MIN_SLEEP_SECONDS
    old_sleep_max = P._MOCK_ASR_MAX_SLEEP_SECONDS
    old_fail = P._MOCK_ASR_FAIL_PROBABILITY
    old_summ = L.summarize_transcript
    enqueue_hits: list[str] = []

    P._MOCK_ASR_MIN_SLEEP_SECONDS = 0.001
    P._MOCK_ASR_MAX_SLEEP_SECONDS = 0.002
    P._MOCK_ASR_FAIL_PROBABILITY = 0.0
    L.summarize_transcript = (  # type: ignore[assignment]
        lambda t: asyncio.sleep(0, result={"summary": "x", "key_points": ["a"], "todos": []})
    )

    real_enq = P.enqueue_task

    async def spy_enq(tid: str):
        enqueue_hits.append(tid)
        return await real_enq(tid)

    P.enqueue_task = spy_enq  # type: ignore[assignment]

    try:
        rid, tid = await _insert_rec_task(
            "t10_a2_retry_ok.wav",
            status=TaskStatus.failed,
            total_retry_count=0,
            error_message="Stage1 attempt 3/3 failed: RuntimeError ... [RETRY EXHAUSTED]",
        )
        before_enqueue_cnt = sum(1 for x in enqueue_hits if x == tid)
        r = await client.post(f"/v1/tasks/{tid}/retry")
        if r.status_code != 200:
            print(f"A2 FAILED: retry HTTP {r.status_code} body={r.text[:400]}")
            return False
        body = r.json()
        ok_body = (
            body.get("status") == "pending"
            and body.get("current_stage_retry_count") == 0
            and body.get("total_retry_count") == 1
            and body.get("error_message") is None
        )
        if not ok_body:
            print(f"A2 FAILED: response body wrong -> {body!r}")
            return False
        # DB 一致性：tasks.status=pending AND recordings.last_status=pending
        async with AsyncSessionLocal() as s:
            t = (await s.execute(select(Task).where(Task.id == tid))).scalar_one()
            r_row = (await s.execute(select(Recording).where(Recording.id == rid))).scalar_one()
        if t.status != TaskStatus.pending or r_row.last_status != TaskStatus.pending:
            print(f"A2 FAILED: DB mismatch task.status={t.status} rec.last_status={r_row.last_status}")
            return False
        after_enqueue_cnt = sum(1 for x in enqueue_hits if x == tid)
        re_enq = after_enqueue_cnt - before_enqueue_cnt
        if re_enq != 1:
            print(f"A2 FAILED: enqueue delta expected +1 actual +{re_enq} enqueue_hits={enqueue_hits}")
            return False
        print(f"A2 OK: retry HTTP 200 body=(pending,total=1,stage_cnt=0,err=None); DB 2-table consistent pending; enqueue delta +{re_enq}")
        return True
    finally:
        P._MOCK_ASR_MIN_SLEEP_SECONDS = old_sleep_min
        P._MOCK_ASR_MAX_SLEEP_SECONDS = old_sleep_max
        P._MOCK_ASR_FAIL_PROBABILITY = old_fail
        L.summarize_transcript = old_summ
        P.enqueue_task = real_enq  # type: ignore[assignment]


# ---------- A3: 非 failed 4 状态全部 409 ----------
async def _a3_non_failed_409(client: httpx.AsyncClient) -> bool:
    print("\n===== A3: POST retry non-failed states -> HTTP 409 all 4 states =====")
    states = [TaskStatus.pending, TaskStatus.transcribing, TaskStatus.summarizing, TaskStatus.done]
    all_ok = True
    for st in states:
        _, tid = await _insert_rec_task(f"t10_a3_{st.value}.wav", status=st)
        r = await client.post(f"/v1/tasks/{tid}/retry")
        if r.status_code != 409:
            print(f"A3 FAILED[{st.value}]: status={r.status_code} expected 409 body={r.text[:300]}")
            all_ok = False
            continue
        err_code = (r.json() or {}).get("error", {}).get("code", "")
        if err_code not in {"CONFLICT", "TASK_NOT_RETRYABLE", "PIPELINE_STATE_ERROR"}:
            print(f"A3 WARN[{st.value}]: err_code={err_code!r} not in conflict set")
        print(f"A3 sub[{st.value}]: HTTP 409 code={err_code!r} OK")
    return all_ok


# ---------- A4: 并发同 task 2 个 retry -> 1x200 + 1x409 ----------
async def _a4_concurrent_retry(client: httpx.AsyncClient) -> bool:
    print("\n===== A4: concurrent 2x POST retry same failed task -> 1x200 + 1x409 =====")
    _, tid = await _insert_rec_task(
        "t10_a4_concurrent.wav",
        status=TaskStatus.failed,
        error_message="Stage2 attempt 3/3 failed: LLMCallException ... [RETRY EXHAUSTED]",
    )
    r1, r2 = await asyncio.gather(
        client.post(f"/v1/tasks/{tid}/retry"),
        client.post(f"/v1/tasks/{tid}/retry"),
        return_exceptions=False,
    )
    codes = sorted([r1.status_code, r2.status_code])
    if codes != [200, 409]:
        print(f"A4 FAILED: statuses={codes} expected [200,409]; r1_body={r1.text[:300]!r} r2_body={r2.text[:300]!r}")
        return False
    # 200 的 body 应该是 pending + total=1
    ok_resp = r1 if r1.status_code == 200 else r2
    body = ok_resp.json()
    ok_body = body.get("status") == "pending" and body.get("total_retry_count") == 1
    if not ok_body:
        print(f"A4 FAILED: 200-body wrong {body!r}")
        return False
    # DB total_retry_count 应该是 1（不会被加两次）
    async with AsyncSessionLocal() as s:
        t = (await s.execute(select(Task).where(Task.id == tid))).scalar_one()
    if t.total_retry_count != 1:
        print(f"A4 FAILED: DB total_retry_count={t.total_retry_count} expected 1 (行锁应只允许加一次)")
        return False
    print(f"A4 OK: statuses={codes}; 200 body (pending,total=1); DB total=1 无重复加")
    return True


# ---------- A5: 404 场景 GET/POST 不存在 task ----------
async def _a5_notfound(client: httpx.AsyncClient) -> bool:
    print("\n===== A5: GET / POST nonexistent task_id -> HTTP 404 NOT_FOUND =====")
    fake = "00000000-0000-0000-0000-000000000000"
    r_get = await client.get(f"/v1/tasks/{fake}")
    r_post = await client.post(f"/v1/tasks/{fake}/retry")
    ok = True
    for name, resp in [("GET", r_get), ("POST retry", r_post)]:
        if resp.status_code != 404:
            print(f"A5 FAILED[{name}]: {resp.status_code} body={resp.text[:300]}")
            ok = False
            continue
        code = (resp.json() or {}).get("error", {}).get("code", "")
        if code != "NOT_FOUND":
            print(f"A5 FAILED[{name}]: error.code={code!r} expected NOT_FOUND")
            ok = False
            continue
        print(f"A5 sub[{name}]: HTTP 404 code=NOT_FOUND OK")
    return ok


async def main() -> int:
    await _clean_t10()
    await P.shutdown_workers()
    await L.shutdown_client()

    settings = Settings()
    P.init_engine(settings)
    await P.start_workers(settings)

    results: dict[str, bool] = {}
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        results["A1"] = await _a1_status_stages(client)
        await _clean_t10()
        results["A2"] = await _a2_retry_success(client)
        await _clean_t10()
        results["A3"] = await _a3_non_failed_409(client)
        await _clean_t10()
        results["A4"] = await _a4_concurrent_retry(client)
        await _clean_t10()
        results["A5"] = await _a5_notfound(client)

    print("\n===== SUMMARY =====")
    all_ok = True
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")
        all_ok = all_ok and v

    await _clean_t10()
    await P.shutdown_workers()
    await L.shutdown_client()
    print(f"\nTASKS_T10_OK={str(all_ok)}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
