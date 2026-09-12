from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Tuple

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Recording, Task, TaskStatus


def _fake_wav(n: int = 1024, *, seed: str = "") -> bytes:
    header = (
        b"RIFF"
        + (n + 36).to_bytes(4, "little")
        + b"WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
        + (44100).to_bytes(4, "little")
        + (44100 * 2).to_bytes(4, "little")
        + b"\x02\x00\x10\x00data"
        + (n).to_bytes(4, "little")
    )
    suffix = bytes(
        f"t13-{seed}-{os.urandom(16).hex()}", "utf-8", errors="ignore"
    )
    body = suffix + b"\x00" * max(0, n - len(suffix))
    return header + body[:n]


async def _upload(
    client: AsyncClient,
    *,
    filename: str,
    content: bytes,
    wait_before_return: float | None = None,
) -> dict:
    r = await client.post(
        "/v1/recordings",
        files={"file": (filename, content, "audio/wav")},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    data = r.json()
    for k in ("recording_id", "task_id", "status"):
        assert k in data, f"missing key {k}: {data}"
    if wait_before_return:
        await asyncio.sleep(wait_before_return)
    return data


async def _poll_task_status(
    client: AsyncClient,
    task_id: str,
    *,
    target_status: str = "done",
    timeout_s: float = 5.0,
    step: float = 0.05,
) -> dict:
    t0 = asyncio.get_event_loop().time()
    last: dict = {}
    while asyncio.get_event_loop().time() - t0 < timeout_s:
        r = await client.get(f"/v1/tasks/{task_id}")
        assert r.status_code == 200, (r.status_code, r.text)
        last = r.json()
        if last["status"] in {"done", "failed"}:
            break
        await asyncio.sleep(step)
    assert last.get("status") == target_status, (
        f"task {task_id} expected {target_status}, got {last}"
    )
    return last


async def _force_failed_in_db(
    db_session: AsyncSession,
    task_id: str,
) -> None:
    await db_session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            status=TaskStatus.failed,
            current_stage_retry_count=0,
            error_message="t13 manual force failed",
        )
    )
    t_row = (
        await db_session.execute(select(Task).where(Task.id == task_id))
    ).scalar_one()
    await db_session.execute(
        update(Recording)
        .where(Recording.id == t_row.recording_id)
        .values(last_status=TaskStatus.failed)
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_upload_success_returns_pending_creates_db_and_file(
    client: AsyncClient,
    db_session: AsyncSession,
    test_settings,
):
    content = _fake_wav(1024, seed="upload-success")
    upload = await _upload(
        client, filename="hello.wav", content=content, wait_before_return=None
    )

    assert upload["status"] == "pending"

    await db_session.commit()
    await db_session.close()
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as fresh:
        rec_cnt = (
            await fresh.execute(select(func.count(Recording.id)))
        ).scalar_one()
        task_cnt = (
            await fresh.execute(select(func.count(Task.id)))
        ).scalar_one()
        assert rec_cnt == 1 and task_cnt == 1, (rec_cnt, task_cnt)

        t_status = (
            await fresh.execute(
                select(Task.status).where(Task.id == upload["task_id"])
            )
        ).scalar_one()
        assert t_status in {TaskStatus.pending, TaskStatus.transcribing, TaskStatus.summarizing, TaskStatus.done}, t_status

        upload_dir = Path(test_settings.storage_upload_dir).resolve()
        wavs = list(upload_dir.glob("*.wav"))
        assert len(wavs) >= 1, f"expected wav file in {upload_dir}, got {wavs}"
        sizes = sorted([p.stat().st_size for p in wavs])
        assert len(content) in sizes, (sizes, len(content))


@pytest.mark.asyncio
async def test_upload_idempotent_by_md5_same_content_different_names(
    client: AsyncClient,
    db_session: AsyncSession,
    test_settings,
):
    content = os.urandom(2048)
    r1 = await _upload(client, filename="a.wav", content=content)
    r2 = await _upload(client, filename="b.wav", content=content)

    assert r1["recording_id"] == r2["recording_id"]

    await db_session.commit()
    await db_session.close()
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as fresh:
        rec_cnt = (
            await fresh.execute(select(func.count(Recording.id)))
        ).scalar_one()
        assert rec_cnt == 1

        upload_dir = Path(test_settings.storage_upload_dir).resolve()
        files = [p for p in upload_dir.iterdir() if p.is_file()]
        assert len(files) == 1


@pytest.mark.asyncio
async def test_pipeline_full_success_mock_path(
    client: AsyncClient,
    db_session: AsyncSession,
):
    upload = await _upload(
        client,
        filename="pipeline-full.wav",
        content=_fake_wav(2048, seed="full-success"),
    )
    rid = upload["recording_id"]
    tid = upload["task_id"]

    await _poll_task_status(client, tid, target_status="done", timeout_s=5.0)

    r_detail = await client.get(f"/v1/recordings/{rid}")
    assert r_detail.status_code == 200
    detail = r_detail.json()
    assert detail["last_status"] == "done"
    transcript = detail.get("transcript")
    assert transcript and len(transcript) >= 500, f"transcript len invalid {detail}"
    summary = detail.get("summary") or {}
    assert isinstance(summary, dict)
    assert summary.get("key_points") == ["Point 1", "Point 2"], f"{summary}"

    task_status = (
        await db_session.execute(
            select(Task.status).where(Task.id == tid)
        )
    ).scalar_one()
    rec_status = (
        await db_session.execute(
            select(Recording.last_status).where(Recording.id == rid)
        )
    ).scalar_one()
    assert (task_status, rec_status) in {
        (TaskStatus.done, TaskStatus.done),
        (TaskStatus.done, TaskStatus.failed),
        (TaskStatus.failed, TaskStatus.done),
        (TaskStatus.failed, TaskStatus.failed),
    }, (task_status, rec_status)
    assert task_status == rec_status, (task_status, rec_status)


@pytest.mark.asyncio
async def test_retry_409_on_non_failed_task(
    client: AsyncClient,
    db_session: AsyncSession,
):
    upload = await _upload(
        client,
        filename="retry-nonfailed.wav",
        content=_fake_wav(1024, seed="retry-flow"),
    )
    tid = upload["task_id"]
    rid = upload["recording_id"]

    r1 = await client.post(f"/v1/tasks/{tid}/retry")
    assert r1.status_code == 409
    assert r1.json()["error"]["code"] == "TASK_NOT_FAILED"

    await db_session.execute(
        update(Task)
        .where(Task.id == tid)
        .values(
            status=TaskStatus.done,
            current_stage_retry_count=0,
            error_message=None,
        )
    )
    await db_session.execute(
        update(Recording)
        .where(Recording.id == rid)
        .values(last_status=TaskStatus.done)
    )
    await db_session.commit()

    r2 = await client.post(f"/v1/tasks/{tid}/retry")
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "TASK_NOT_FAILED"

    await _force_failed_in_db(db_session, tid)
    total_before = (
        await db_session.execute(
            select(Task.total_retry_count).where(Task.id == tid)
        )
    ).scalar_one()

    r3 = await client.post(f"/v1/tasks/{tid}/retry")
    assert r3.status_code == 200, (r3.status_code, r3.text)
    payload3 = r3.json()
    assert payload3["status"] == "pending"

    total_after = (
        await db_session.execute(
            select(Task.total_retry_count).where(Task.id == tid)
        )
    ).scalar_one()
    assert total_after == total_before + 1

    r4 = await client.post(f"/v1/tasks/{tid}/retry")
    assert r4.status_code == 409
    assert r4.json()["error"]["code"] == "TASK_NOT_FAILED"


@pytest.mark.asyncio
async def test_delete_cascades_everything_db_file(
    client: AsyncClient,
    db_session: AsyncSession,
    test_settings,
):
    upload_dir = Path(test_settings.storage_upload_dir).resolve()

    upload_info = await _upload(
        client,
        filename="delete-me.wav",
        content=_fake_wav(1024, seed="del1"),
        wait_before_return=0.05,
    )
    rid1 = upload_info["recording_id"]
    tid1 = upload_info["task_id"]

    try:
        await _poll_task_status(
            client,
            tid1,
            target_status="done",
            timeout_s=5.0,
            step=0.05,
        )
    except AssertionError:
        try:
            await _poll_task_status(
                client,
                tid1,
                target_status="failed",
                timeout_s=1.0,
                step=0.05,
            )
        except Exception:
            pass

    d1 = await client.delete(f"/v1/recordings/{rid1}")
    assert d1.status_code == 204, (d1.status_code, d1.text)
    assert d1.text == "" or d1.content == b""

    g1 = await client.get(f"/v1/recordings/{rid1}")
    assert g1.status_code == 404
    assert g1.json()["error"]["code"] == "NOT_FOUND"

    await db_session.commit()
    await db_session.close()
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as fresh:
        rec_cnt = (
            await fresh.execute(select(func.count(Recording.id)))
        ).scalar_one()
        task_cnt = (
            await fresh.execute(select(func.count(Task.id)))
        ).scalar_one()
        assert rec_cnt == 0 and task_cnt == 0, (rec_cnt, task_cnt)

    upload_files = [p for p in upload_dir.iterdir() if p.is_file()]
    assert upload_files == []

    # ensure DB session state after delete is invalid => close so next GET uses fresh AsyncSessionLocal:
    await asyncio.sleep(0.05)

    d_again = await client.delete(f"/v1/recordings/{rid1}")
    assert d_again.status_code == 404
    assert d_again.json()["error"]["code"] == "NOT_FOUND"

    rid2 = (
        await _upload(
            client,
            filename="pre-delete.wav",
            content=_fake_wav(2048, seed="del2"),
        )
    )["recording_id"]
    shutil.rmtree(upload_dir, ignore_errors=True)
    upload_dir.mkdir(parents=True, exist_ok=True)

    d_missing_ok = await client.delete(f"/v1/recordings/{rid2}")
    assert d_missing_ok.status_code == 204, (
        d_missing_ok.status_code,
        d_missing_ok.text,
    )
