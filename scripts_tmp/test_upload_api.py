"""T6 临时验收脚本：FastAPI TestClient 走 POST /v1/recordings。

不启用 pytest 框架；用 httpx.AsyncClient(TestClient)+ asyncio.run 跑完所有断言。
"""
from __future__ import annotations

import asyncio
import io
import os
import pathlib
import sys
import uuid
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 提前 patch 路径：让 FastAPI TestClient / httpx ASGITransport 成功 import
try:
    from httpx import AsyncClient, ASGITransport  # noqa: F401
except Exception as exc:  # pragma: no cover
    raise AssertionError(f"[T6 环境缺失] pip install httpx failed: {exc}")

from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import AsyncSessionLocal  # noqa: E402
from app.models import Recording, Task  # noqa: E402
from app.services.storage import ensure_upload_dir  # noqa: E402

UPLOADS = ensure_upload_dir()
_WAV_CONTENT_SMALL = b"RIFF" + os.urandom(200)
_WAV_CONTENT_MID = b"RIFF" + os.urandom(4 * 1024 * 1024)  # 4MB-ish, real full MD5 check


def _uploads_files_starting_with(prefix: str) -> list[Path]:
    return [p for p in UPLOADS.iterdir() if p.is_file() and p.name.startswith(prefix)]


async def _cleanup_db_prefix(prefix: str) -> None:
    """Clean recordings+tasks rows where original_filename starts with prefix.

    (Idempotent: tests run against real local MySQL, clean data after assertions.)
    """
    async with AsyncSessionLocal() as s:
        # ORM delete
        from sqlalchemy import delete, select

        t_ids = (await s.execute(
            select(Task.id).join(Recording, Recording.id == Task.recording_id).where(Recording.original_filename.like(f"{prefix}%"))
        )).scalars().all()
        for tid in t_ids:
            await s.execute(delete(Task).where(Task.id == tid))
        r_ids = (await s.execute(
            select(Recording.id).where(Recording.original_filename.like(f"{prefix}%"))
        )).scalars().all()
        for rid in r_ids:
            # delete recordings CASCADE to tasks already — but also delete orphan disk files
            for disk in UPLOADS.glob(f"{rid}.*"):
                disk.unlink(missing_ok=True)
            await s.execute(delete(Recording).where(Recording.id == rid))
        await s.commit()


def _s200_ok(resp, *, label: str) -> dict:
    assert resp.status_code == 200, f"[{label}] 期望 200 得到 {resp.status_code}: {resp.text[:500]}"
    body = resp.json()
    assert isinstance(body, dict) and set(body.keys()) == {"recording_id", "task_id", "status"}, (
        f"[{label}] UploadResponse shape miss: {body}"
    )
    assert body["status"] == "pending", f"[{label}] status 必须是 pending (not {body['status']})"
    assert isinstance(body["recording_id"], str) and len(body["recording_id"]) == 36, body
    assert isinstance(body["task_id"], str) and len(body["task_id"]) == 36, body
    return body


async def main() -> int:
    # ---- Pre-clean ----
    prefix = f"t6_{uuid.uuid4().hex[:6]}_"
    await _cleanup_db_prefix(prefix)  # in case of prior runs
    # remove any disk leftovers from prior aborted tests
    for p in UPLOADS.glob("t6_*"):
        if p.is_file():
            p.unlink(missing_ok=True)

    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # ---------------------------------------------------------------------
        # 1. Basic successful upload, small payload.
        # ---------------------------------------------------------------------
        small_name = f"{prefix}demo.wav"
        r1 = await client.post(
            "/v1/recordings",
            files={"file": (small_name, io.BytesIO(_WAV_CONTENT_SMALL), "audio/wav")},
        )
        b1 = _s200_ok(r1, label="1.small-upload")
        rid1 = b1["recording_id"]
        tid1 = b1["task_id"]
        # disk file must exist: uploads/<rid1>.wav
        disk_file = UPLOADS / f"{rid1}.wav"
        assert disk_file.exists(), f"[FAIL 1b] 上传成功但磁盘上 {disk_file} 不存在"
        assert disk_file.stat().st_size == len(_WAV_CONTENT_SMALL), (
            f"[FAIL 1c] 磁盘字节 {disk_file.stat().st_size} != 内容 {len(_WAV_CONTENT_SMALL)}"
        )
        print(f"[1] 上传成功 recording_id={rid1} task_id={tid1} 磁盘={disk_file.name}({disk_file.stat().st_size} bytes) OK")

        # ---------------------------------------------------------------------
        # 2. MD5 idempotency — re-upload SAME BYTES (any filename) returns same recording_id/task_id,
        #    and NO NEW FILE WRITTEN in uploads (single-file count per rid1 prefix).
        # ---------------------------------------------------------------------
        same_bytes = bytes(_WAV_CONTENT_SMALL)
        r2 = await client.post(
            "/v1/recordings",
            files={"file": (f"{prefix}renamed_copy_of_1.mp3", io.BytesIO(same_bytes), "audio/mp3")},
        )
        b2 = _s200_ok(r2, label="2.idempotent-reupload")
        assert b2["recording_id"] == rid1 and b2["task_id"] == tid1, (
            f"[FAIL 2b] 二次上传同 md5 没返回同 rid/tid: first({rid1},{tid1}) second({b2})"
        )
        # Ensure the second call didn't drop a new orphan disk file named like <other uuid>.mp3
        extras = [p for p in UPLOADS.iterdir() if p.is_file() and p.suffix.lower() in {".wav", ".mp3", ".m4a", ".aac"}
                  and p.name != f"{rid1}.wav"]
        extras = [p for p in extras if any(part.isalnum() and len(part) >= 8 for part in p.stem.split("-"))]
        # Heuristic: only care about orphan files named like a uuid — we won't create them.
        # Simpler: count uuid-shaped files.
        def _uuid_shaped(name: str) -> bool:
            stem = Path(name).stem
            return len(stem) == 36 and stem.count("-") == 4
        new_uuid_files = [p for p in UPLOADS.iterdir() if p.is_file() and _uuid_shaped(p.name) and p.name != f"{rid1}.wav"]
        assert not new_uuid_files, f"[FAIL 2c] 幂等调用仍然写了新磁盘文件: {new_uuid_files}"
        print(f"[2] 同内容重命名上传幂等 OK: rid/tid 与第一次完全一致, 新增 disk 文件 0 个")

        # ---------------------------------------------------------------------
        # 3. 415 UnsupportedMedia — ext exe / sh / none.
        # ---------------------------------------------------------------------
        bad_exts = [f"{prefix}bad.exe", f"{prefix}bad.sh", f"{prefix}bad_noext"]
        for bad_name in bad_exts:
            r = await client.post(
                "/v1/recordings",
                files={"file": (bad_name, io.BytesIO(b"not audio"), "application/octet-stream")},
            )
            assert r.status_code == 415, f"[FAIL 3] {bad_name} 期望 415 得 {r.status_code}: {r.text}"
            j = r.json()
            assert j["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE", (f"[{bad_name}] 415 code 错: {j}")
            print(f"[3] filename={bad_name} → 415 UNSUPPORTED_MEDIA_TYPE, details.actual_extension={j['error'].get('details', {}).get('actual_extension')!r}")
        # No t6_*.exe/sh/... residual on disk
        leaked = [p for p in UPLOADS.iterdir() if p.is_file() and p.name.startswith(prefix)
                  and "bad" in p.name.lower()]
        assert not leaked, f"[FAIL 3b] 415 分支仍然在磁盘上创建了垃圾: {leaked}"
        print(f"[3b] 415 分支磁盘零垃圾: OK")

        # ---------------------------------------------------------------------
        # 4. 413 PayloadTooLarge.
        # ---------------------------------------------------------------------
        max_mb = int(50)  # matches T4 default MAX_UPLOAD_SIZE_MB = 50 (in app/config default)
        over_bytes = max_mb * 1024 * 1024 + 1
        over_payload = (b"0123456789ABCDEF" * (over_bytes // 16)) + b"0"[: over_bytes % 16]
        assert len(over_payload) == over_bytes, f"precond: over_payload len {len(over_payload)} want {over_bytes}"
        r4 = await client.post(
            "/v1/recordings",
            files={"file": (f"{prefix}fat_over_50mb.wav", io.BytesIO(over_payload), "audio/wav")},
            timeout=300.0,
        )
        assert r4.status_code == 413, f"[FAIL 4] 50MB+1 字节期望 413，得 {r4.status_code}: {r4.text[:400]}"
        j4 = r4.json()
        assert j4["error"]["code"] == "PAYLOAD_TOO_LARGE", f"[FAIL 4b] 413 错误码错: {j4}"
        print(f"[4] 52,428,801 bytes (> 50MB) → 413 PAYLOAD_TOO_LARGE OK. resp={j4['error']['message']}")
        # No t6_*fat* file leaked
        leaked4 = [p for p in UPLOADS.iterdir() if p.is_file() and "fat_over_50mb" in p.name]
        # Also: any UUID-shaped file in uploads whose size > max bytes MUST NOT exist.
        big_leak = [p for p in UPLOADS.iterdir() if p.is_file() and _uuid_shaped(p.name)
                   and p.stat().st_size > 50 * 1024 * 1024]
        assert not leaked4 and not big_leak, f"[FAIL 4c] 413 磁盘残留: leaked4={leaked4} big={big_leak}"
        print("[4c] 413 分支磁盘零残留 OK")

        # ---------------------------------------------------------------------
        # 5. 400 missing file field (FastAPI default RequestValidationError — T2 handler → 422).
        #    T6 spec says: `file` 字段缺失 → 返回我们自定义的错误响应 JSON（code=BAD_REQUEST
        #    或 request_validation_errors 都行，只要 JSON error schema 有 error.code/message/details）
        #    我们用默认 FastAPI File(...) → 它走 RequestValidationError，T2 handler 会返回 422。
        # ---------------------------------------------------------------------
        r5 = await client.post("/v1/recordings", data={"not_file": "x"})
        assert r5.status_code == 422, f"[5] 缺 file 字段期望 422, got {r5.status_code}: {r5.text}"
        j5 = r5.json()
        # T2 register_exception_handlers: RequestValidationError wraps into error.code=BAD_REQUEST
        # (we rely on T2 handler here; if not, we assert there is an 'error' key at minimum.)
        assert "error" in j5 and "code" in j5["error"], (
            f"[FAIL 5] 缺 file 字段的响应没有 error.code: {j5}"
        )
        print(f"[5] 缺 file 字段 → {r5.status_code} error.code={j5['error']['code']!r} (T2 handler 统一 JSON 响应 OK)")

    # ---- Post test: CLEAN UP TEST DATA (real DB + uploads) ----
    await _cleanup_db_prefix(prefix)
    # remove leftover file we created (rid1.wav)
    disk_file.unlink(missing_ok=True)
    # ensure clean
    post_leftover = [p for p in UPLOADS.iterdir() if p.is_file() and (p.name.startswith(prefix) or (len(p.stem) == 36 and p.name != ".gitkeep"))]
    # Leave .gitkeep alone; warn about unexpected uuids from unrelated runs (but don't fail test).
    if any(p.name != ".gitkeep" for p in post_leftover):
        print(f"[post-cleanup] uploads 剩余疑似测试残留: {[p.name for p in post_leftover if p.name != '.gitkeep']} (未删除，避免误删真实数据；请手动核对)")
    else:
        print("[post-cleanup] uploads 目录干净: OK")

    print("\nUPLOAD_API_T6_OK=True")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
