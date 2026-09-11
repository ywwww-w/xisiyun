"""Ticket<11> Recordings API 验收脚本（httpx.ASGITransport 内存端到端，不占端口）.

T11 验收标准 A1-A5:
A1. 分页：25 条上传 → page1 page_size10 total=25 items=10; page3 page10 items=5; page_size101 → 422; created_at 严格倒序; 每条 last_status 5 态合法
A2. 详情字段控制：
    A2a. pending → transcript=None, summary=None
    A2b. done    → transcript len>=500, summary 三键齐全 + key_points>=1
    A2c. done 但 summary_json invalid (key_points=[]) → transcript 仍然有值, summary is None, 日志有 WARNING "summary_json invalid even status done"
A3. DELETE 级联：done recording → DELETE 204; 再 GET detail → 404; SELECT tasks WHERE recording_id=? → 0 行; 磁盘文件已不存在
A4. DELETE 边界：磁盘文件先手动删除 → 再 DELETE API → HTTP 204 不报错
A5. 404 场景：GET / DELETE 不存在 recording_id → HTTP 404 error.code=NOT_FOUND 结构统一
"""
from __future__ import annotations

import asyncio
import io
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
LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "app.log")

_VALID_STATUSES = {"pending", "transcribing", "summarizing", "done", "failed"}


async def _clean_t11() -> None:
    sql = (
        "SET FOREIGN_KEY_CHECKS=0; "
        "DELETE FROM tasks WHERE recording_id IN (SELECT id FROM recordings WHERE original_filename LIKE 't11_%'); "
        "DELETE FROM recordings WHERE original_filename LIKE 't11_%'; "
        "SET FOREIGN_KEY_CHECKS=1;"
    )
    try:
        subprocess.run([MYSQL_CLI, "-uroot", "-proot", "xisiyun_asr", "-e", sql], check=False, capture_output=True)
    except Exception:
        pass


def _t11_bytes(n: int = 1024, *, salt: str = "") -> bytes:
    """Return a deterministic 1KB-ish WAV-ish blob with unique salt so MD5 differs per file (avoid upload idempotent)."""
    header = b"RIFF" + (n + 36).to_bytes(4, "little") + b"WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
    body = bytes(f"t11-{salt}-{uuid.uuid4()}", "utf-8") + b"\x00" * max(0, n - 100)
    return header + body[:n]


async def _wait_until_task_done(client: httpx.AsyncClient, task_id: str, *, timeout: float = 20.0) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        r = await client.get(f"/v1/tasks/{task_id}")
        if r.status_code == 200 and r.json().get("status") == "done":
            return True
        await asyncio.sleep(0.03)
    return False


async def _upload_wav(client: httpx.AsyncClient, *, filename: str, n: int = 1024, salt: str = "") -> tuple[int, dict[str, Any]]:
    files = {"file": (filename, io.BytesIO(_t11_bytes(n, salt=salt)), "audio/wav")}
    r = await client.post("/v1/recordings", files=files)
    return r.status_code, r.json() if r.content else {}


# -------------------- A1: 分页正确性（total/page/page_size/sort/page_size>100→422） --------------------
async def _a1_paging(client: httpx.AsyncClient) -> bool:
    print("\n===== A1: Paging: 25 uploads -> page1 size10 total25; page3 size10 count5; page_size101 -> 422; DESC strict; last_status legal =====")
    # 先清空 + 上传 25 个不同 MD5 的 t11 小 wav
    ok_all = True
    ids: list[str] = []
    for i in range(25):
        code, body = await _upload_wav(client, filename=f"t11_a1_{i:02d}.wav", salt=f"a1-{i}-{uuid.uuid4()}")
        if code != 200:
            print(f"A1 FAIL upload #{i}: status={code} body={str(body)[:300]}")
            ok_all = False
            break
        ids.append(body["recording_id"])
    if not ok_all:
        return False
    await asyncio.sleep(0.05)
    r1 = await client.get("/v1/recordings", params={"page": 1, "page_size": 10})
    r3 = await client.get("/v1/recordings", params={"page": 3, "page_size": 10})
    r_invalid = await client.get("/v1/recordings", params={"page": 1, "page_size": 101})
    if r1.status_code != 200:
        print(f"A1 FAIL page1 status={r1.status_code} body={r1.text[:400]}")
        return False
    b1 = r1.json()
    ok_b1 = b1["total"] == 25 and b1["page"] == 1 and b1["page_size"] == 10 and len(b1["items"]) == 10
    if not ok_b1:
        print(f"A1 FAIL page1 meta: {b1!r} expected total=25 page=1 size=10 items.len=10")
        return False
    b3 = r3.json()
    ok_b3 = b3["total"] == 25 and b3["page"] == 3 and len(b3["items"]) == 5
    if not ok_b3:
        print(f"A1 FAIL page3 meta: {b3!r} expected items len=5")
        return False
    # 严格递减：page1 items 内部 created_at desc strict (same second OK，但同一 session 插入顺序就是 created_at 顺序)
    def _ts(x: dict) -> str: return x["created_at"]
    desc_ok = all(_ts(b1["items"][i]) >= _ts(b1["items"][i+1]) for i in range(9))
    if not desc_ok:
        print(f"A1 FAIL DESC: page1 created_at order = {[_ts(x) for x in b1['items']]}")
        return False
    # 每个 item.last_status in 5 态
    last_statuses_ok = all(x.get("last_status") in _VALID_STATUSES for x in b1["items"])
    if not last_statuses_ok:
        bad = [x.get("last_status") for x in b1["items"] if x.get("last_status") not in _VALID_STATUSES]
        print(f"A1 FAIL last_status illegal: {bad!r}")
        return False
    # page_size 101 → 422
    if r_invalid.status_code != 422:
        print(f"A1 FAIL page_size=101 status={r_invalid.status_code} expected 422 body={r_invalid.text[:300]}")
        return False
    print(f"A1 OK: total=25; page1 items=10 DESC; page3 items=5; page_size101→422; last_status all in 5-state legal set")
    return True


# -------------------- A2: 详情字段控制 (pending / done+ok / done+invalid summary_json) --------------------
async def _a2_detail_fields(client: httpx.AsyncClient) -> bool:
    print("\n===== A2: Detail field control: pending(Nones) + done(valid transcript+summary) + done(invalid summary WARNING log transcript still shown) =====")
    all_ok = True
    # --- A2a: pending ---
    code, body = await _upload_wav(client, filename="t11_a2a_pending.wav", salt="a2a-"+str(uuid.uuid4()))
    if code != 200:
        print(f"A2a FAIL upload {code} {body}")
        return False
    rid_pending = body["recording_id"]
    r = await client.get(f"/v1/recordings/{rid_pending}")
    b = r.json()
    ok_a2a = r.status_code == 200 and b.get("transcript") in (None, "") and b.get("summary") is None
    if not ok_a2a:
        print(f"A2a FAIL pending detail: status={r.status_code} trans={b.get('transcript')!r} summary={b.get('summary')!r}")
        all_ok = False
    else:
        print("A2a OK: status=pending detail transcript=None summary=None")
    # --- A2b: done valid (reuse existing worker; wait until task done) ---
    old_sleep_min = P._MOCK_ASR_MIN_SLEEP_SECONDS
    old_sleep_max = P._MOCK_ASR_MAX_SLEEP_SECONDS
    old_fail = P._MOCK_ASR_FAIL_PROBABILITY
    old_summ = L.summarize_transcript
    P._MOCK_ASR_MIN_SLEEP_SECONDS = 0.001
    P._MOCK_ASR_MAX_SLEEP_SECONDS = 0.002
    P._MOCK_ASR_FAIL_PROBABILITY = 0.0
    L.summarize_transcript = (lambda t: asyncio.sleep(0, result={"summary":"hi","key_points":["a","b"],"todos":["x"]}))  # type: ignore[assignment]
    try:
        code, body = await _upload_wav(client, filename="t11_a2b_done_valid.wav", salt="a2b-"+str(uuid.uuid4()))
        if code != 200:
            all_ok = False
            print(f"A2b FAIL upload {code} {body}")
        else:
            task_id = body["task_id"]
            ok_done = await _wait_until_task_done(client, task_id, timeout=20.0)
            if not ok_done:
                all_ok = False
                print("A2b FAIL: task not done in 20s")
            else:
                r = await client.get(f"/v1/recordings/{body['recording_id']}")
                b = r.json()
                trans = b.get("transcript") or ""
                summ = b.get("summary") or {}
                ok_b = (
                    r.status_code == 200
                    and len(trans) >= 500
                    and isinstance(summ, dict)
                    and set(summ.keys()) == {"summary","key_points","todos"}
                    and len(summ.get("key_points") or []) >= 1
                )
                if not ok_b:
                    print(f"A2b FAIL detail: status={r.status_code} trans_len={len(trans)} summary_keys={sorted(summ.keys()) if isinstance(summ,dict) else type(summ).__name__} body_sample={str(b)[:300]}")
                    all_ok = False
                else:
                    print(f"A2b OK: status=done detail transcript={len(trans)} chars summary keys={sorted(summ.keys())} key_points={len(summ['key_points'])}")
    finally:
        P._MOCK_ASR_MIN_SLEEP_SECONDS = old_sleep_min
        P._MOCK_ASR_MAX_SLEEP_SECONDS = old_sleep_max
        P._MOCK_ASR_FAIL_PROBABILITY = old_fail
        L.summarize_transcript = old_summ
    # --- A2c: done invalid summary_json (key_points empty) WARNING log ---
    # 手工 DB 改 recording.last_status=done summary_json={"summary":"x","key_points":[],"todos":["y"]}
    rid_c = str(uuid.uuid4())
    tid_c = str(uuid.uuid4())
    async with AsyncSessionLocal() as s:
        s.add(Recording(
            id=rid_c, original_filename="t11_a2c_invalid_summary.wav",
            file_ext="wav", storage_path=os.path.join(UPLOADS_DIR, rid_c+".wav"),
            file_size_bytes=1024, file_hash="t11_md5_c_"+rid_c[:16],
            transcript="x" * 600,
            summary_json={"summary":"x","key_points":[],"todos":["y"]},
            last_status=TaskStatus.done,
        ))
        s.add(Task(id=tid_c, recording_id=rid_c, status=TaskStatus.done))
        await s.commit()
    # 读日志位置: 记录当前文件长度，GET 后再读新内容
    try:
        log_start = os.path.getsize(LOG_PATH) if os.path.exists(LOG_PATH) else 0
    except OSError:
        log_start = 0
    r = await client.get(f"/v1/recordings/{rid_c}")
    b = r.json()
    try:
        log_new = ""
        if os.path.exists(LOG_PATH):
            with open(LOG_PATH, "rb") as fh:
                fh.seek(log_start)
                log_new = fh.read().decode("utf-8", errors="ignore")
    except Exception:
        log_new = ""
    saw_warn = "summary_json invalid even status=done" in log_new
    trans_ok = b.get("transcript") is not None and len(b.get("transcript") or "") >= 500
    summ_null = b.get("summary") in (None, [], {})
    ok_c = r.status_code == 200 and saw_warn and trans_ok and summ_null
    if not ok_c:
        print(f"A2c FAIL (status={r.status_code} saw_warn={saw_warn} trans_ok={trans_ok} summ_null={summ_null}) log_new_contains: {('summary_json invalid even status' in log_new)}, body_sample={str(b)[:300]}")
        all_ok = False
    else:
        print("A2c OK: done invalid summary → WARNING log present, transcript still shown len>=500, summary null")
    return all_ok


# -------------------- A3: DELETE 级联 --------------------
async def _a3_delete_cascade(client: httpx.AsyncClient) -> bool:
    print("\n===== A3: DELETE cascade: DB tasks 0 rows / recording 404 / uploads file missing =====")
    code, body = await _upload_wav(client, filename="t11_a3_del.wav", salt="a3-"+str(uuid.uuid4()))
    if code != 200:
        print(f"A3 FAIL upload {code} {body}")
        return False
    rid = body["recording_id"]
    # 先查 storage_path（我们自己 INSERT 的是 recordings.storage_path = 实际路径，T6 写了绝对，这里直接 DB 查）
    async with AsyncSessionLocal() as s:
        rec = (await s.execute(select(Recording).where(Recording.id == rid))).scalar_one_or_none()
        storage_path = rec.storage_path if rec is not None else ""
    if not storage_path:
        print("A3 FAIL: storage_path empty in DB")
        return False
    if not os.path.exists(storage_path):
        # 没上传文件？理论上不可能
        print(f"A3 WARN: {storage_path} not exist before delete (will continue, spec wants it gone anyway)")
    r_del = await client.delete(f"/v1/recordings/{rid}")
    if r_del.status_code != 204:
        print(f"A3 FAIL DELETE status={r_del.status_code} expected 204 body={r_del.text[:300]}")
        return False
    # 再 GET detail → 404
    r_get = await client.get(f"/v1/recordings/{rid}")
    if r_get.status_code != 404:
        print(f"A3 FAIL GET after DELETE status={r_get.status_code} expected 404")
        return False
    # SELECT tasks WHERE recording_id = rid → 0 行
    async with AsyncSessionLocal() as s:
        cnt = int(await s.scalar(select(func.count()).select_from(Task).where(Task.recording_id == rid)) or 0)
    if cnt != 0:
        print(f"A3 FAIL tasks remaining count={cnt} expected 0")
        return False
    # 磁盘文件不存在
    if os.path.exists(storage_path):
        print(f"A3 FAIL physical file still exists: {storage_path}")
        return False
    print(f"A3 OK: DELETE→204; re-GET→404; tasks rows=0; disk file gone ({storage_path})")
    return True


# -------------------- A4: DELETE 边界：磁盘文件先被删，接口仍然 204 --------------------
async def _a4_delete_missing_file(client: httpx.AsyncClient) -> bool:
    print("\n===== A4: DELETE boundary: file pre-deleted -> still HTTP 204 =====")
    code, body = await _upload_wav(client, filename="t11_a4_missing.wav", salt="a4-"+str(uuid.uuid4()))
    if code != 200:
        print(f"A4 FAIL upload {code} {body}")
        return False
    rid = body["recording_id"]
    async with AsyncSessionLocal() as s:
        rec = (await s.execute(select(Recording).where(Recording.id == rid))).scalar_one()
        storage_path = rec.storage_path
    if os.path.exists(storage_path):
        os.unlink(storage_path)
    if os.path.exists(storage_path):
        print(f"A4 FAIL: failed to remove file {storage_path} before test")
        return False
    r_del = await client.delete(f"/v1/recordings/{rid}")
    if r_del.status_code != 204:
        print(f"A4 FAIL DELETE after file missing status={r_del.status_code} expected 204 body={r_del.text[:400]}")
        return False
    print(f"A4 OK: missing file DELETE→204 no error (no 500)")
    return True


# -------------------- A5: 404 GET/DELETE --------------------
async def _a5_notfound(client: httpx.AsyncClient) -> bool:
    print("\n===== A5: 404 GET + DELETE nonexistent recording_id =====")
    fake = "00000000-0000-0000-0000-0000000000ff"
    r_g = await client.get(f"/v1/recordings/{fake}")
    r_d = await client.delete(f"/v1/recordings/{fake}")
    ok = True
    for name, resp in [("GET", r_g), ("DELETE", r_d)]:
        if resp.status_code != 404:
            print(f"A5 FAIL[{name}]: status={resp.status_code} body={resp.text[:300]}")
            ok = False
            continue
        code = (resp.json() or {}).get("error", {}).get("code", "")
        if code != "NOT_FOUND":
            print(f"A5 FAIL[{name}]: error.code={code!r} expected NOT_FOUND")
            ok = False
            continue
        print(f"A5 sub[{name}]: HTTP 404 NOT_FOUND OK")
    return ok


async def main() -> int:
    await _clean_t11()
    await P.shutdown_workers()
    await L.shutdown_client()

    settings = Settings()
    P.init_engine(settings)
    await P.start_workers(settings)
    # 注意：resume 会把前面 clean 之前残留的 pending 入队，但 clean_t11 只清 t11_ 前缀
    # 其他前缀的残留任务（比如 T6/T9 临时）可能入队，但不影响本脚本断言 t11_ 前缀数据，所以没关系。

    transport = httpx.ASGITransport(app=fastapi_app)
    results: dict[str, bool] = {}
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=30.0) as client:
        results["A1"] = await _a1_paging(client)
        await _clean_t11()
        results["A2"] = await _a2_detail_fields(client)
        await _clean_t11()
        results["A3"] = await _a3_delete_cascade(client)
        await _clean_t11()
        results["A4"] = await _a4_delete_missing_file(client)
        await _clean_t11()
        results["A5"] = await _a5_notfound(client)

    print("\n===== SUMMARY =====")
    all_ok = True
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")
        all_ok = all_ok and v

    await _clean_t11()
    await P.shutdown_workers()
    await L.shutdown_client()
    print(f"\nRECORDINGS_T11_OK={str(all_ok)}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
