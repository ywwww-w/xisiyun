"""临时验收脚本：delete_file_if_exists = 幂等 + never raise FileNotFound/IsDir"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import storage as S

_UPLOADS = S.ensure_upload_dir()


def _touch(name: str, content: bytes = b"x") -> pathlib.Path:
    p = _UPLOADS / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def main() -> bool:
    # 0. create dummy file
    dummy = _touch("t4_delete_realfile.m4a", b"hello-storage-delete\n" * 9)
    assert dummy.exists()

    # 1. delete existing — should succeed, no raise
    try:
        S.delete_file_if_exists(dummy)
    except Exception as exc:  # pragma: no cover
        print(f"[FAIL 1] delete existing raised {type(exc).__name__}: {exc}")
        return False
    if dummy.exists():
        print(f"[FAIL 1.1] after delete_if_exists existing file {dummy} still on disk")
        return False
    print("  [1] delete existing file: OK (file gone, no exc)")

    # 2. delete 50× same (non-existent) path = never raise FileNotFoundError
    for i in range(50):
        try:
            S.delete_file_if_exists(dummy)
        except FileNotFoundError as exc:
            print(f"[FAIL 2.1] iteration {i} raised FileNotFoundError: {exc} — should be swallowed")
            return False
        except Exception as exc:  # pragma: no cover
            print(f"[FAIL 2.2] iteration {i} raised unexpected {type(exc).__name__}: {exc}")
            return False
    print("  [2] delete 50× same non-existent path: OK (no raise, idempotent)")

    # 3. accept any of: relative filename (e.g. 'abc.wav') — should resolve to uploads/abc.wav (gone since no such) = no raise
    rel_names = ["t4_absent_file.wav", "sub/nested/not_there.mp3"]
    for rel in rel_names:
        try:
            S.delete_file_if_exists(rel)
        except Exception as exc:  # pragma: no cover
            print(f"[FAIL 3] rel={rel!r} raised {type(exc).__name__}: {exc}")
            return False
    print("  [3] accept relative filenames (resolve to STORAGE_UPLOAD_DIR): OK (no raise)")

    # 4. absolute path to non-existent (outside uploads): no raise, no crash
    abs_nonexist = ROOT / "uploads" / "definitely_not_here_t4_delete_check.aac"
    try:
        S.delete_file_if_exists(abs_nonexist)
    except Exception as exc:  # pragma: no cover
        print(f"[FAIL 4] abs nonexistent {abs_nonexist} raised {type(exc).__name__}: {exc}")
        return False
    print("  [4] abs path to non-existent: OK (no raise)")

    # 5. path str type accepted (both Path and str)
    s_str = _touch("t4_delete_by_str.wav", b"a")
    try:
        S.delete_file_if_exists(str(s_str))
    except Exception as exc:  # pragma: no cover
        print(f"[FAIL 5] str path raised {type(exc).__name__}: {exc}")
        return False
    if s_str.exists():
        print(f"[FAIL 5.1] by str path didn't actually delete {s_str}")
        return False
    print("  [5] accept `str` type path (not only Path): OK")

    # Final: no t4_delete_* leaks
    leaks = [p for p in _UPLOADS.rglob("t4_delete_*") if p.is_file()]
    if leaks:
        print(f"[FAIL final] leftover t4_delete_* files: {leaks}")
        for p in leaks:
            p.unlink(missing_ok=True)
        return False

    print("\nSTORAGE_DELETE_IDEMPOTENT_OK=True")
    return True


if __name__ == "__main__":
    ok = main()
    assert ok, "delete idempotent test FAILED"
