from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Recording, Task, TaskStatus


def _t12_wav_bytes(n=1024, *, salt="") -> bytes:
    header = (
        b"RIFF"
        + (n + 36).to_bytes(4, "little")
        + b"WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
        + (44100).to_bytes(4, "little")
        + (44100 * 2).to_bytes(4, "little")
        + b"\x02\x00\x10\x00data"
        + (n).to_bytes(4, "little")
    )
    body = bytes(f"t12-{salt}-{os.urandom(8).hex()}", "utf-8") + b"\x00" * max(0, n - 100)
    return header + body[:n]


async def _upload_wav(
    client: AsyncClient,
    *,
    filename: str = "t12_smoke.wav",
    content: Optional[bytes] = None,
    salt: str = "",
) -> dict:
    body = content if content is not None else _t12_wav_bytes(1024, salt=salt)
    r = await client.post(
        "/v1/recordings",
        files={"file": (filename, body, "audio/wav")},
    )
    assert r.status_code == 200, f"upload failed {r.status_code} {r.text}"
    data = r.json()
    return data


async def _poll_task_done(
    client: AsyncClient,
    task_id: str,
    *,
    timeout_s: float = 5.0,
    step: float = 0.1,
) -> dict:
    t0 = asyncio.get_event_loop().time()
    last = {}
    while asyncio.get_event_loop().time() - t0 < timeout_s:
        r = await client.get(f"/v1/tasks/{task_id}")
        assert r.status_code == 200
        last = r.json()
        if last["status"] in {"done", "failed"}:
            return last
        await asyncio.sleep(step)
    pytest.fail(f"task {task_id} never reached terminal: {last}")


@pytest.mark.asyncio
async def test_smoke_health(client: AsyncClient):
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"


@pytest.mark.asyncio
async def test_db_isolation_case_a_inserts_row(
    client: AsyncClient,
    db_session: AsyncSession,
):
    r = await _upload_wav(client, salt="iso-a")
    cnt = (await db_session.execute(select(func.count(Recording.id)))).scalar_one()
    assert cnt == 1
    assert r["status"] == "pending"


@pytest.mark.asyncio
async def test_db_isolation_case_b_starts_empty(
    client: AsyncClient,
    db_session: AsyncSession,
):
    cnt_r = (await db_session.execute(select(func.count(Recording.id)))).scalar_one()
    cnt_t = (await db_session.execute(select(func.count(Task.id)))).scalar_one()
    assert cnt_r == 0 and cnt_t == 0, f"DB not isolated recs={cnt_r} tasks={cnt_t}"


@pytest.mark.asyncio
async def test_uploads_isolation_case_one_creates_file(
    client: AsyncClient,
    test_settings,
):
    content = _t12_wav_bytes(2048, salt="upiso")
    await _upload_wav(client, filename="t12_upiso.wav", content=content)
    upload_dir = Path(test_settings.storage_upload_dir)
    files = list(upload_dir.glob("*.wav"))
    assert len(files) == 1
    assert files[0].stat().st_size == len(content)


@pytest.mark.asyncio
async def test_pipeline_mock_no_real_llm_call_and_goes_done(
    client: AsyncClient,
    db_session: AsyncSession,
    override_dependencies,
):
    upload = await _upload_wav(client, salt="fulldone")
    task_id = upload["task_id"]
    rid = upload["recording_id"]

    final = await _poll_task_done(client, task_id, timeout_s=5.0, step=0.05)
    assert final["status"] == "done", f"task not done: {final}"

    r = await client.get(f"/v1/recordings/{rid}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["last_status"] == "done"
    transcript = detail.get("transcript")
    assert transcript and len(transcript) >= 500
    summary = detail.get("summary") or {}
    assert isinstance(summary, dict)
    expected = override_dependencies["llm_result"]
    assert summary.get("key_points") == expected["key_points"], f"{summary}"

    t_row = (await db_session.execute(select(Task.status).where(Task.id == task_id))).scalar_one()
    r_row = (await db_session.execute(select(Recording.last_status).where(Recording.id == rid))).scalar_one()
    assert t_row == TaskStatus.done
    assert r_row == TaskStatus.done


@pytest.mark.asyncio
async def test_retry_pending_returns_409(
    client: AsyncClient,
):
    upload = await _upload_wav(client, salt="retry409")
    task_id = upload["task_id"]
    r = await client.post(f"/v1/tasks/{task_id}/retry")
    assert r.status_code == 409
    err = r.json()
    assert err.get("error", {}).get("code") == "TASK_NOT_RETRYABLE", f"{r.text}"
