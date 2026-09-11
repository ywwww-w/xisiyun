"""T5 验收：schemas.py 5 条断言。不写 pytest，临时脚本保留。"""
from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic import ValidationError


def main() -> int:
    # ---- 验收 1 import 零错（脚本头部已经 import，跑到这里代表通过）----
    from app.schemas import (
        ErrorResponse,
        LLMSummaryResponse,
        PagedResponse,
        PaginationQueryParams,
        RecordingDetailOut,
        SummaryOut,
        TaskOut,
        UploadResponse,
        TaskStatus,
        RecordingListItem,
    )
    print("[1/5] schemas import OK (UploadResponse, TaskOut, RecordingDetailOut, PagedResponse, SummaryOut, LLMSummaryResponse, ErrorResponse 全存在)")

    # ---- 验收 2 UploadResponse 三字段严格 PDF：recording_id/task_id/status ----
    ur = UploadResponse(recording_id="a", task_id="b", status="pending")
    ur_dumped = ur.model_dump()
    want_ur = {"recording_id": "a", "task_id": "b", "status": "pending"}
    assert ur_dumped == want_ur, (
        f"UploadResponse dump mismatch:\n"
        f"  got : {sorted(ur_dumped.items())}\n"
        f"  want: {sorted(want_ur.items())}"
    )
    # 额外强校验：字段数量必须 = 3（不能莫名多出来）
    assert len(UploadResponse.model_fields) == 3, f"UploadResponse fields={len(UploadResponse.model_fields)} expected=3 (PDF exactly 3 fields)"
    print(f"[2/5] UploadResponse fields={sorted(UploadResponse.model_fields.keys())} dump={ur_dumped} OK (strict 3 fields snake_case matches PDF)")

    # ---- 验收 3 PaginationQueryParams 越界抛 ValidationError ----
    # (a) page=0 → fail ge=1
    try:
        _ = PaginationQueryParams(page=0, page_size=20)
    except ValidationError as e:
        errs = e.errors()
        locs = [err.get("loc", tuple()) for err in errs]
        assert any("page" in l for l in locs), f"PaginationQueryParams(page=0) 报错 location 不含 page: {errs}"
        print(f"[3/5a] PaginationQueryParams(page=0) → ValidationError OK: {errs[0]['type']} at {locs}")
    else:
        raise AssertionError("[3/5a FAIL] PaginationQueryParams(page=0) must raise, but didn't (ge=1 missing)")

    # (b) page_size=10000 → fail le=100
    try:
        _ = PaginationQueryParams(page=1, page_size=10000)
    except ValidationError as e:
        errs = e.errors()
        locs = [err.get("loc", tuple()) for err in errs]
        assert any("page_size" in l for l in locs), f"PaginationQueryParams(page_size=10000) 报错 location 不含 page_size: {errs}"
        print(f"[3/5b] PaginationQueryParams(page_size=10000) → ValidationError OK: {errs[0]['type']} at {locs}")
    else:
        raise AssertionError("[3/5b FAIL] PaginationQueryParams(page_size=10000) must raise (le=100) but didn't")

    # ---- 验收 4 LLMSummaryResponse key_points=[] → ValidationError（min_length=1）----
    try:
        _ = LLMSummaryResponse(summary="x", key_points=[], todos=[])
    except ValidationError as e:
        errs = e.errors()
        locs = [err.get("loc", tuple()) for err in errs]
        assert any("key_points" in l for l in locs), f"LLMSummaryResponse(key_points=[]) 报错 location 不含 key_points: {errs}"
        print(f"[4/5] LLMSummaryResponse(key_points=[]) → ValidationError OK: {errs[0]['type']} at {locs}（spec R7 三层校验之 min_length=1）")
    else:
        raise AssertionError("[4/5 FAIL] LLMSummaryResponse(key_points=[]) must raise (min_length=1) but didn't")

    # 正向补测：key_points=["a"] 通过
    good = LLMSummaryResponse(summary="x", key_points=["a"], todos=[])
    assert good.key_points == ["a"]
    print(f"     LLMSummaryResponse 正向 key_points=['a']: OK (dump={good.model_dump()})")

    # ---- 验收 5 PagedResponse[T] 泛型正常：实例化 PagedResponse[RecordingListItem] 不报错且 items 校验 ----
    now = datetime.now(timezone.utc)
    item = RecordingListItem(
        id="r1",
        original_filename="a.wav",
        file_ext="wav",
        file_size_bytes=1024,
        last_status="pending",
        created_at=now,
        updated_at=now,
    )
    paged: PagedResponse[RecordingListItem] = PagedResponse[RecordingListItem](
        total=1, page=1, page_size=20, items=[item]
    )
    p_dumped = paged.model_dump()
    # items 数量、类型一致
    assert isinstance(p_dumped["items"], list) and len(p_dumped["items"]) == 1, f"PagedResponse.items 不对: {p_dumped}"
    assert p_dumped["items"][0]["id"] == "r1", f"PagedResponse item field lost: {p_dumped}"
    # PagedResponse.page_size 字段 le 校验：传 999 也应报错（= 证明 PagedResponse 自己也带 le，不是只依赖 Params）
    try:
        _ = PagedResponse(total=0, page=1, page_size=999, items=[])
    except ValidationError:
        print(f"[5/5a] PagedResponse(page_size=999) → ValidationError OK (PagedResponse schema 自带 Field le=100 生效)")
    else:
        raise AssertionError("[5/5a FAIL] PagedResponse page_size 999 应该也被 Field(le=100) 拦住，但没拦")

    print(f"[5/5b] PagedResponse[RecordingListItem] generic OK: total={paged.total}, page={paged.page}, page_size={paged.page_size}, items[0].id={paged.items[0].id}（泛型验证通过）")

    # 额外：TaskOut 字段数 = 8（对应 ORM Task 8 字段）
    to_fields = sorted(TaskOut.model_fields.keys())
    assert len(to_fields) == 8, f"TaskOut 字段数={len(to_fields)} 期望 8，got {to_fields}"
    print(f"[extra] TaskOut 8 fields: {to_fields} (align ORM 8 cols: OK)")

    print("\nSCHEMAS_T5_OK=True")
    return 0


if __name__ == "__main__":
    sys.exit(main())
