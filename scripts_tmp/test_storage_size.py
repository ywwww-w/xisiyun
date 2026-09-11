"""临时验收脚本：storage.save_uploaded_file 两大硬边界（扩展名白名单 415、大小上限 413 立即删部分文件）+ 边界值刚好 50MB-1 通过。"""
from __future__ import annotations

import asyncio
import io
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import UploadFile
from starlette.datastructures import Headers

from app.config import Settings
from app.services import storage as S
from app.utils.errors import PayloadTooLargeException, UnsupportedMediaTypeException

_settings = Settings()
_UPLOADS = S.ensure_upload_dir()


def _cleanup(filename: str) -> None:
    """Clean up files possibly leaked by broken implementations."""
    p = _UPLOADS / filename
    if p.exists():
        p.unlink(missing_ok=True)


def _fake_upload(payload: bytes, filename: str) -> UploadFile:
    headers = Headers({"content-type": "application/octet-stream"})
    return UploadFile(filename=filename, file=io.BytesIO(payload), headers=headers)


async def _ext_case_415() -> bool:
    """扩展名白名单（EXE/sh/空扩展名/dotless 全部返回 415，且 uploads 目录里一条垃圾残留都没有）"""
    case_cases = [
        ("EXE", "rid-001.exe"),
        ("sh", "rid-002.sh"),
        ("", "rid-003-emptyext"),  # ext = ''
        ("bat", "rid-004.bat"),
        (".MP4", "rid-005.mp4"),  # starts with dot not in list
    ]
    for ext, _ in case_cases:
        uf = _fake_upload(b"small payload " * 10, "f")
        filename_on_disk = f"t4_ext_{ext or 'empty'}.{ext.lstrip('.')}" if ext else f"t4_ext_empty.empty"
        rid = filename_on_disk.replace(".", "_").replace("__", "_")
        # Actually recording_id: use rid so that we know what file to look for leaks
        try:
            await S.save_uploaded_file(uf, recording_id=rid, ext=ext)
        except UnsupportedMediaTypeException as e:
            # Spec §4.5 T4-2: 415 must NOT even open a file / write anything
            print(f"  [{rid:<30s}] ext={ext or '<empty>'!r:8s} got 415 {e.code} {e.http_status}  details={e.details}")
        else:
            print(f"  [FAIL EXT {rid}] expected 415, got success = BUG in storage")
            return False
        # leak check — listing uploads for any rid file
        leaked = [p for p in _UPLOADS.iterdir() if rid in p.name and p.is_file()]
        if leaked:
            print(f"  [LEAK EXT {rid}]: {leaked!r} — must not create a partial file for 415")
            for p in leaked:
                p.unlink(missing_ok=True)
            return False
    return True


async def _size_413_and_boundary() -> bool:
    """大小 52428800+1 bytes → 413 立即删部分文件（uploads 不能有残留）；52428800-1 byte → 正常成功 写全 bytes"""
    max_mb = int(_settings.max_upload_size_mb)  # 50
    max_bytes = max_mb * 1024 * 1024  # 52_428_800

    # ---- Case A: 50MB + 1 byte → 413 + 0 partial residual ----
    over_bytes = max_bytes + 1
    chunk = b"0123456789ABCDEF"
    repeats_over, rem_over = divmod(over_bytes, len(chunk))
    buf_over = chunk * repeats_over + chunk[:rem_over]
    assert len(buf_over) == over_bytes, f"over={len(buf_over)} want {over_bytes}"
    uf_over = _fake_upload(buf_over, "fat.wav")
    rid_over = "t4_size_A_over50mb"
    try:
        path, written = await S.save_uploaded_file(uf_over, recording_id=rid_over, ext="wav")
    except PayloadTooLargeException as e:
        print(f"  [{rid_over}] len={len(buf_over):,d}  413 ok  max_mb={e.details and e.details.get('max_mb')}  actual_mb={e.details and e.details.get('actual_mb')}")
    else:
        print(f"  [FAIL SIZE A] expected 413 for {len(buf_over):,d} bytes, got file={path} written={written:,d}")
        S.delete_file_if_exists(path)
        return False
    finally:
        # Safety cleanup: even buggy impl won't leak
        S.delete_file_if_exists(f"{rid_over}.wav")
    leaked = [p for p in _UPLOADS.iterdir() if rid_over in p.name and p.is_file()]
    if leaked:
        print(f"  [LEAK SIZE A] uploads/{leaked[0].name} must not exist after 413 (was not deleted inline)")
        for p in leaked:
            p.unlink()
        return False

    # ---- Case B: 50MB - 1 byte → 成功、写全 bytes, storage_path matches ----
    under_bytes = max_bytes - 1
    repeats_under, rem_under = divmod(under_bytes, len(chunk))
    buf_under = chunk * repeats_under + chunk[:rem_under]
    assert len(buf_under) == under_bytes, f"under={len(buf_under)} want {under_bytes}"
    uf_under = _fake_upload(buf_under, "thin.MP3")
    rid_under = "t4_size_B_under50mb"
    try:
        path, written = await S.save_uploaded_file(uf_under, recording_id=rid_under, ext=".MP3")
    except Exception as exc:
        print(f"  [FAIL SIZE B] expected save success for {len(buf_under):,d} bytes, got exc={type(exc).__name__}: {exc}")
        return False
    # path must match recordings.storage_path expected shape: <upload_dir>/<rid>.mp3 (normalized lowercase, no leading dot)
    want_name = f"{rid_under}.mp3"
    if path.name != want_name or path.parent.resolve() != _UPLOADS.resolve():
        print(f"  [FAIL SIZE B path] got {path} want parent={_UPLOADS} name={want_name}")
        S.delete_file_if_exists(path)
        return False
    if written != under_bytes:
        print(f"  [FAIL SIZE B written] got {written:,d} bytes written != {under_bytes:,d}")
        S.delete_file_if_exists(path)
        return False
    actual_bytes_on_disk = path.stat().st_size
    if actual_bytes_on_disk != under_bytes:
        print(f"  [FAIL SIZE B disk] got disk size {actual_bytes_on_disk:,d} != {under_bytes:,d}")
        S.delete_file_if_exists(path)
        return False
    # file sha1-equivalent: content identical
    with path.open("rb") as f:
        content = f.read()
    if content != buf_under:
        print(f"  [FAIL SIZE B content mismatch] prefix not equal")
        S.delete_file_if_exists(path)
        return False
    print(f"  [{rid_under}] len={len(buf_under):,d}  saved={path.name}  written={written:,d} disk={actual_bytes_on_disk:,d}   OK (50MB - 1 byte, boundary below max)")

    # ---- Case C: 刚好 50MB 边界 = 不报错（= max 精确值也允许）----
    exact_bytes = max_bytes
    repeats_ex, rem_ex = divmod(exact_bytes, len(chunk))
    buf_exact = chunk * repeats_ex + chunk[:rem_ex]
    assert len(buf_exact) == exact_bytes
    uf_exact = _fake_upload(buf_exact, "exact.AAC")
    rid_exact = "t4_size_C_exact50mb"
    try:
        path_ex, written_ex = await S.save_uploaded_file(uf_exact, recording_id=rid_exact, ext="aac")
    except Exception as exc:
        print(f"  [FAIL SIZE C exact] expected save success for exact {max_mb}MB, got exc={type(exc).__name__}: {exc}")
        return False
    if written_ex != exact_bytes:
        print(f"  [FAIL SIZE C written exact] got {written_ex:,d} != {exact_bytes:,d}")
        S.delete_file_if_exists(path_ex)
        return False
    print(f"  [{rid_exact}] len={len(buf_exact):,d}  saved={path_ex.name}  written={written_ex:,d}  OK (== exact 50MB allowed)")
    S.delete_file_if_exists(path_ex)
    S.delete_file_if_exists(path)  # clean case B too
    return True


async def main() -> bool:
    # Pre-clean uploads/ of any t4_* files (previous runs may have left garbage)
    for p in _UPLOADS.glob("t4_*"):
        if p.is_file():
            p.unlink(missing_ok=True)
    print("[T4-2a] ext whitelist → 415 with ZERO residual files:")
    a = await _ext_case_415()
    print()
    print("[T4-2b] size cap → 413 + inline delete + exact boundary:")
    b = await _size_413_and_boundary()
    print()
    ok = a and b
    assert ok, "storage boundary tests FAILED"
    print("STORAGE_SIZE_BOUNDARY_OK=True")
    return True


if __name__ == "__main__":
    asyncio.run(main())
