# -*- coding: utf-8 -*-
import asyncio
import json
import os
import random
import sys
import time
import uuid
from pathlib import Path
import httpx

BASE = "http://127.0.0.1:8000"
ROOT = Path(r"d:\code\xisiyun")
ASSETS = ROOT / "scripts_tmp" / "test_assets"
ASSETS.mkdir(parents=True, exist_ok=True)


def make_wav(n: int = 2048, seed: int = 1) -> bytes:
    import struct
    sz = max(44, n)
    data = bytearray(sz)
    def w(off, s):
        bs = s.encode("ascii")
        for i, b in enumerate(bs):
            data[off + i] = b
    w(0, "RIFF")
    struct.pack_into("<I", data, 4, sz - 8)
    w(8, "WAVE"); w(12, "fmt ")
    struct.pack_into("<I", data, 16, 16)
    struct.pack_into("<H", data, 20, 1)
    struct.pack_into("<H", data, 22, 1)
    struct.pack_into("<I", data, 24, 44100)
    struct.pack_into("<I", data, 28, 88200)
    struct.pack_into("<H", data, 32, 2)
    struct.pack_into("<H", data, 34, 16)
    w(36, "data")
    struct.pack_into("<I", data, 40, sz - 44)
    r = random.Random(seed)
    for i in range(44, sz):
        data[i] = r.randint(0, 255)
    return bytes(data)

results = []

def chk(name, expected, actual, note=""):
    ok = expected == actual
    results.append({"Test": name, "Expected": expected, "Actual": actual, "Pass": ok, "Note": note})
    print(f"  [{ 'OK' if ok else 'NG':2s}] {name}: expected={expected!r} actual={actual!r}  note={note!r}")

def print_div(title):
    print("\n" + "=" * 68)
    print(f"  {title}")
    print("=" * 68)

async def main():
    # 0 Health
    print_div("0. Health Check")
    async with httpx.AsyncClient(timeout=30) as c:
        h = await c.get(f"{BASE}/health")
        chk("0.Health 200", 200, h.status_code)
        try:
            s = h.json().get("status")
        except Exception:
            s = None
        chk("0.Health body.status == ok", "ok", s)

        # 1 Upload valid
        print_div("1. Upload valid wav")
        wav = make_wav(2048, 42)
        files = {"file": ("acc1.wav", wav, "audio/wav")}
        r = await c.post(f"{BASE}/v1/recordings", files=files, timeout=60)
        rid = tid = None
        chk("1.Upload 200", 200, r.status_code)
        try:
            b = r.json()
            rid = b.get("recording_id"); tid = b.get("task_id")
            chk("1.status=pending", "pending", b.get("status"))
            chk("1.recording_id len >=8", True, bool(rid) and len(str(rid)) >= 8)
            chk("1.task_id present", True, bool(tid))
        except Exception as e:
            chk("1.body parse JSON", False, True, str(e)[:80])

        # 2 Idempotent
        print_div("2. Idempotent 2nd upload same bytes (rename)")
        files2 = {"file": ("renamed_2nd.wav", wav, "audio/wav")}
        r2 = await c.post(f"{BASE}/v1/recordings", files=files2, timeout=60)
        chk("2. 2nd upload 200", 200, r2.status_code)
        try:
            rid2 = r2.json().get("recording_id")
            chk("2. same recording_id as 1st", rid, rid2)
        except Exception as e:
            chk("2. body parse", False, True, str(e)[:60])

        # 3 txt 415
        print_div("3. Upload txt -> 415")
        txt = b"not audio data at all hello world"
        files3 = {"file": ("wrong.txt", txt, "text/plain")}
        r3 = await c.post(f"{BASE}/v1/recordings", files=files3, timeout=60)
        chk("3. txt => 415", 415, r3.status_code)
        try:
            code = r3.json()["error"]["code"]
        except Exception:
            code = None
        chk("3. code=UNSUPPORTED_MEDIA_TYPE", "UNSUPPORTED_MEDIA_TYPE", code)

        # 4 51MB mock via direct python check of 413 logic: hit endpoint with 51MB fake file
        print_div("4. Upload 51MB fake.mp3 -> 413 PAYLOAD_TOO_LARGE")
        # Build a wav of 51MB (mock, fake bytes after valid header, extension mp3)
        small_header = make_wav(44, 7)
        # Append junk up to 51MB exactly (stream generator to avoid memory spike if possible --
        # here 51MB is fine for modern machines)
        big = small_header + b"\x00" * (51 * 1024 * 1024 - 44)
        files4 = {"file": ("51mb_fake.mp3", big, "audio/mpeg")}
        t0 = time.time()
        r4 = await c.post(f"{BASE}/v1/recordings", files=files4, timeout=120)
        print(f"    (51MB req completed in {time.time()-t0:.2f}s status={r4.status_code})")
        chk("4. 51MB => 413 (not 200)", 413, r4.status_code)
        try:
            code4 = r4.json()["error"]["code"]
        except Exception:
            code4 = None
        chk("4. code=PAYLOAD_TOO_LARGE", "PAYLOAD_TOO_LARGE", code4)
        del big

        # 5 GET task
        print_div("5. GET /v1/tasks/{task_id} -> initial status")
        await asyncio.sleep(0.6)
        r5 = await c.get(f"{BASE}/v1/tasks/{tid}", timeout=20)
        chk("5.GET task status 200", 200, r5.status_code)
        try:
            st = r5.json().get("status")
            chk("5.status in 5 enum", True, st in {"pending","transcribing","summarizing","done","failed"})
        except Exception as e:
            chk("5.json parse", False, True, str(e)[:60])

        # 6 Poll final status (90s max)
        print_div("6. Poll final status <=90s")
        final = None
        for i in range(0, 91, 2):
            g = await c.get(f"{BASE}/v1/tasks/{tid}", timeout=20)
            try:
                st = g.json().get("status")
            except Exception:
                st = None
            print(f"    poll t={i:3d}s status={st}")
            if st in {"done", "failed"}:
                final = st
                break
            await asyncio.sleep(2)
        chk("6. Final in {done,failed} within 90s", True, final is not None)
        chk("6. Final status expected done", "done", final)

        # 7 GET recording detail: transcript + summary 3 keys
        print_div("7. GET recording detail done fields")
        d7 = await c.get(f"{BASE}/v1/recordings/{rid}", timeout=20)
        chk("7.detail 200", 200, d7.status_code)
        try:
            b7 = d7.json()
            chk("7.last_status matches final", final, b7.get("last_status"))
            tr = b7.get("transcript") or ""
            chk("7.transcript>=500 chars", True, len(tr) >= 500)
            su = b7.get("summary")
            chk("7.summary object present", True, isinstance(su, dict) and len(su) > 0)
            if isinstance(su, dict):
                chk("7.summary: summary is str", True, isinstance(su.get("summary"), str) and len(su["summary"]) > 0)
                kp = su.get("key_points")
                chk("7.summary: key_points is list[str] len>=1", True, isinstance(kp, list) and len(kp) >= 1 and all(isinstance(x,str) for x in kp))
                td = su.get("todos")
                chk("7.summary: todos is list[str]", True, isinstance(td, list) and all(isinstance(x,str) for x in td))
        except Exception as e:
            chk("7.detail json parse", False, True, str(e)[:100])

        # 8 Manual SQL fail -> retry 200 -> retry again 409 (FOR UPDATE lock)
        print_div("8. SQL failed => retry 200 pending => retry #2 409 TASK_NOT_FAILED")
        import subprocess
        MYSQL = r"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe"
        if Path(MYSQL).exists():
            cmd = [MYSQL, "-u", "root", "-proot", "-D", "xisiyun_asr", "-e",
                   f"UPDATE tasks SET status='failed' WHERE id='{tid}'; UPDATE recordings SET last_status='failed' WHERE id='{rid}';"]
            subprocess.run(cmd, capture_output=True, timeout=30)
            await asyncio.sleep(0.4)
            ra = await c.post(f"{BASE}/v1/tasks/{tid}/retry", timeout=20)
            chk("8.first retry 200", 200, ra.status_code)
            try:
                chk("8.retry returns status pending", "pending", ra.json().get("status"))
            except Exception: pass
            rb = await c.post(f"{BASE}/v1/tasks/{tid}/retry", timeout=20)
            chk("8.second retry (already pending) 409", 409, rb.status_code)
            try:
                chk("8.retry 409 code TASK_NOT_FAILED", "TASK_NOT_FAILED", rb.json()["error"]["code"])
            except Exception as e:
                chk("8.retry 409 code parse", False, True, str(e)[:60])
        else:
            for nm in ["mysql.exe exists at known path", "retry first 200", "retry status=pending", "retry 2nd 409", "retry 2nd code=TASK_NOT_FAILED"]:
                results.append({"Test":f"8.{nm} SKIP","Expected":"MySQL.exe","Actual":"missing","Pass":False,"Note":"MySQL CLI not found at default path; skipped automated SQL injection"})
            print("    MySQL client missing, SKIP 8.*")

        # 9 DELETE recording => 204; GET => 404; include_deleted=1 200 is_deleted=True
        print_div("9. Soft DELETE + 204 + GET default 404 + include_deleted=1 200 + 再DELETE 404 + 幂等POST restore")
        r9d = await c.delete(f"{BASE}/v1/recordings/{rid}", timeout=20)
        chk("9.DELETE returns 204", 204, r9d.status_code)
        r9g = await c.get(f"{BASE}/v1/recordings/{rid}", timeout=20)
        chk("9.GET detail after DELETE default=>404", 404, r9g.status_code)
        try:
            chk("9.404 code=NOT_FOUND", "NOT_FOUND", r9g.json()["error"]["code"])
        except Exception as e:
            chk("9.404 code parse", False, True, str(e)[:60])
        # S1:?include_deleted=1 => 200 + is_deleted=True last_status=archived (Tv2 DoD3)
        r9s = await c.get(f"{BASE}/v1/recordings/{rid}", params={"include_deleted":"1"}, timeout=20)
        chk("S1.GET detail ?include_deleted=1 200", 200, r9s.status_code)
        try:
            b9 = r9s.json()
            chk("S1.include_deleted=1 is_deleted=True", True, b9.get("is_deleted") is True)
            chk("S1.last_status=archived", "archived", b9.get("last_status"))
            chk("S1.deleted_at not null", True, b9.get("deleted_at") is not None)
        except Exception as e:
            chk("S1.json parse S1", False, True, str(e)[:80])
        # S2:再DELETE已删的 =>404 (exclude_deleted=True生效)
        r9del2 = await c.delete(f"{BASE}/v1/recordings/{rid}", timeout=20)
        chk("S2.再次对已删行DELETE =>404", 404, r9del2.status_code)
        # S3:POST restore 200 is_deleted=False deleted_at=None summary仍然保留
        r9rst = await c.post(f"{BASE}/v1/recordings/{rid}/restore", timeout=30)
        chk("S3.POST restore =>200", 200, r9rst.status_code)
        try:
            brs = r9rst.json()
            chk("S3.restore后is_deleted=False", False, brs.get("is_deleted"))
            chk("S3.restore后deleted_at=None", None, brs.get("deleted_at"))
            # 恢复后的summary不应丢失(软删只改标记不碰业务列)
            su9 = brs.get("summary") or {}
            chk("S3.restore后summary字段保留(非空)", True, bool(su9) and isinstance(su9.get("summary"),str) and len(su9.get("summary",""))>0)
        except Exception as e:
            chk("S3.restore parse 3 fields", False, True, str(e)[:80])
        # S4:幂等restore(再一次POST restore)=>200不报错
        r9rst2 = await c.post(f"{BASE}/v1/recordings/{rid}/restore", timeout=30)
        chk("S4.二次幂等restore仍200(不409)", 200, r9rst2.status_code)
        # S5:GET列表(restore后先有这个rid)→DELETE→再GET列表确实不包含已删行
        r9l0 = await c.get(f"{BASE}/v1/recordings", params={"page":1,"page_size":50}, timeout=20)
        ids_alive_before = set()
        if r9l0.status_code == 200:
            try:
                ids_alive_before = {str(x.get("id")) for x in (r9l0.json().get("items") or []) if x.get("id")}
            except Exception: pass
        chk("S5a.DELETE前列表包含rid", True, rid in ids_alive_before)
        # 执行第二次软删(restore后现在是alive)用于后续测试
        await c.delete(f"{BASE}/v1/recordings/{rid}", timeout=20)
        r9l1 = await c.get(f"{BASE}/v1/recordings", params={"page":1,"page_size":50}, timeout=20)
        ids_alive_after = set()
        if r9l1.status_code == 200:
            try:
                ids_alive_after = {str(x.get("id")) for x in (r9l1.json().get("items") or []) if x.get("id")}
            except Exception: pass
        chk("S5b.软删后列表不包含rid", False, rid in ids_alive_after)
        # S6:同文件第三次上传(MD5不变但rid1已软删+函数UNIQUE让出槽)=> recording_id != rid (3-B)
        files6 = {"file": ("third_time_same_bytes.wav", wav, "audio/wav")}
        r6 = await c.post(f"{BASE}/v1/recordings", files=files6, timeout=60)
        chk("S6.删后同MD5新上传200", 200, r6.status_code)
        try:
            rid6 = r6.json().get("recording_id")
            chk("S6.删后同MD5新recording_id != old rid (3-B口径)", True, bool(rid6) and str(rid6) != str(rid))
        except Exception as e:
            chk("S6.3-B new id parse", False, True, str(e)[:60])
        # S7:SQL置tasks.status=failed + recording.is_deleted=True(archived) → POST retry 409 message含"先恢复/restore"字样 (4-2口径)
        import subprocess
        MYSQL = r"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe"
        if Path(MYSQL).exists():
            # 对S6新开的那条(alive)先真DELETE避免干扰,然后用老rid那条archived
            if 'rid6' in dir() and rid6:
                subprocess.run([MYSQL, "-uroot", "-proot", "-D", "xisiyun_asr", "-e",
                                f"DELETE FROM tasks WHERE recording_id='{rid6}'; DELETE FROM recordings WHERE id='{rid6}'; "],
                               capture_output=True, timeout=30)
                # 也删掉uploads/rid6对应文件
                import glob as _g; [Path(p).unlink(missing_ok=True) for p in _g.glob(str(ROOT/"uploads"/f"{rid6}.*"))]
            # S7前置:把archived那条的tasks.status设为failed(否则retry直接TASK_NOT_FAILED不是目标分支)
            subprocess.run([MYSQL, "-uroot", "-proot", "-D", "xisiyun_asr", "-e",
                            f"UPDATE tasks SET status='failed' WHERE recording_id='{rid}' LIMIT 1;"],
                           capture_output=True, timeout=30)
            await asyncio.sleep(0.5)
            # tasks/那一行 id先查出来
            tids_in_rid = subprocess.run([MYSQL, "-uroot", "-proot", "-D", "xisiyun_asr", "-N", "-B", "-e",
                                          f"SELECT id FROM tasks WHERE recording_id='{rid}' LIMIT 1;"],
                                         capture_output=True, timeout=30, text=True)
            tid_archived = (tids_in_rid.stdout or "").strip()
            if tid_archived:
                rs7 = await c.post(f"{BASE}/v1/tasks/{tid_archived}/retry", timeout=20)
                chk("S7.已删recording下的task retry =>409", 409, rs7.status_code)
                try:
                    body7 = rs7.json()
                    err_msg = str((body7.get("error") or {}).get("message") or "")
                    chk("S7.409 message含'先恢复'/'restore'关键字", True,
                        ("恢复" in err_msg) or ("restore" in err_msg.lower()))
                    chk("S7.code=TASK_NOT_RETRYABLE", "TASK_NOT_RETRYABLE", (body7.get("error") or {}).get("code"))
                except Exception as e:
                    chk("S7.409 error parse", False, True, str(e)[:100])
            else:
                chk("S7.archived tid found for retry 409", True, False, "no task row under archived rid(检查FK cascade?)")
        # S8:再restore rid一次 → GET list又能看到;结束
        rs8 = await c.post(f"{BASE}/v1/recordings/{rid}/restore", timeout=30)
        chk("S8.final restore =>200", 200, rs8.status_code)
        r9l2 = await c.get(f"{BASE}/v1/recordings", params={"page":1,"page_size":50}, timeout=20)
        ids_end = set()
        if r9l2.status_code == 200:
            try:
                ids_end = {str(x.get("id")) for x in (r9l2.json().get("items") or []) if x.get("id")}
            except Exception: pass
        chk("S8.restore后列表又含rid", True, rid in ids_end)

        # 10 EXTRA: GET /v1/tasks list paged 200 (previously missing 404)
        print_div("EXTRA. GET /v1/tasks?page=1&page_size=5 200 (prev bug: 404)")
        rx = await c.get(f"{BASE}/v1/tasks", params={"page":1,"page_size":5}, timeout=20)
        chk("X.tasks list 200 (previously 404 triggered upload error modal)", 200, rx.status_code)
        try:
            js = rx.json()
            chk("X.tasks list paged schema has total/page/page_size/items", True,
                all(k in js for k in ("total","page","page_size","items")))
        except Exception as e:
            chk("X.tasks list schema parse", False, True, str(e)[:60])

    print()
    print("=" * 72)
    print("  FINAL TEST SUMMARY (spec.md §5.1 L1 manual smoke)")
    print("=" * 72)
    header = f"{'#':4}{'Test':80}{'Expected':25}{'Actual':35}{'Pass':6}{'Note':40}"
    print(header)
    print("-" * min(190, len(header)*2))
    for i, r in enumerate(results, 1):
        print(f"{i:>3}. {str(r['Test'])[:78]:<80}{str(r['Expected'])[:23]:<25}{str(r['Actual'])[:33]:<35}{'PASS' if r['Pass'] else 'FAIL':<6}{str(r.get('Note',''))[:38]:<40}")
    passed = sum(1 for r in results if r["Pass"])
    total = len(results)
    print()
    print(f"RESULT: {passed}/{total} PASSED")
    # dump JSON file for later
    out = ROOT / "scripts_tmp" / "acceptance_result.json"
    out.write_text(json.dumps({"passed":passed,"total":total,"items":results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"raw json saved: {out}")
    sys.exit(0 if passed == total else 1)

if __name__ == "__main__":
    asyncio.run(main())
