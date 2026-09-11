"""临时验收脚本：storage.compute_file_md5_streaming 的流式 MD5 正确性 + 读指针回到 0 可再次完整读取内容。"""
from __future__ import annotations

import asyncio
import hashlib
import io
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import UploadFile
from starlette.datastructures import Headers

from app.services.storage import compute_file_md5_streaming


def _fake_upload(payload: bytes, filename: str) -> UploadFile:
    headers = Headers({"content-type": "application/octet-stream"})
    return UploadFile(filename=filename, file=io.BytesIO(payload), headers=headers)


async def main() -> None:
    sizes_mb = [0, 1, 4, 10, 25]
    chunk_size = 64 * 1024  # deliberately small, force multi-chunk streaming paths

    all_ok = True
    for mb in sizes_mb:
        n = mb * 1024 * 1024 + (311 if mb else 0)  # non-aligned sizes
        payload = (
            (b"hello-storage-world" * (n // 20 + 1))[:n]
            if n > 0
            else b""
        )
        uf = _fake_upload(payload, f"sample_{mb}mb.wav")
        md5_stream, total_stream = await compute_file_md5_streaming(uf, chunk_size=chunk_size)
        md5_truth = hashlib.md5(payload).hexdigest().lower()
        same = md5_stream == md5_truth and total_stream == len(payload)
        print(
            f"[md5 {mb:>2d}MB] size={n:<10d} streamed_total={total_stream:<10d} "
            f"md5_match={md5_stream == md5_truth} ok={same}"
        )
        if not same:
            all_ok = False
            break

        # rewind assertion: after streaming MD5 compute, we can re-read entire content from file
        reread = await uf.read()
        if reread != payload:
            all_ok = False
            print(f"[rewind FAIL {mb}MB]: reread len={len(reread)} != payload {len(payload)}")
            break
        print(f"[rewind {mb:>2d}MB] len={len(reread)} == payload")

        # also: uppercase extension should give same md5 (ext case insensitive — tested later by save_uploaded_file)
        uf2 = _fake_upload(payload, f"sample.{mb}.WAV")
        md5_up, _ = await compute_file_md5_streaming(uf2, chunk_size=chunk_size)
        if md5_up != md5_truth:
            all_ok = False
            print("[upper ext FAIL] md5 should be ext-agnostic but wasn't")
            break
        print(f"[ext-case {mb:>2d}MB] case insensitive OK")

    assert all_ok, "MD5 streaming test failed"
    print("\nSTORAGE_MD5_OK=True")


if __name__ == "__main__":
    asyncio.run(main())
