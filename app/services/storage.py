from __future__ import annotations

import datetime as _dt
import glob
import hashlib
import shutil
from pathlib import Path
from typing import AsyncGenerator, Tuple

from fastapi import UploadFile

from app.config import Settings
from app.utils.errors import ConflictException, PayloadTooLargeException, UnsupportedMediaTypeException
from app.utils.logger import get_logger

_settings = Settings()
_logger = get_logger("app.services.storage")

_ALLOWED_EXTENSIONS = {"wav", "mp3", "m4a", "aac"}
_DEFAULT_CHUNK_SIZE = 1024 * 1024


def _max_bytes_allowed() -> int:
    return int(_settings.max_upload_size_mb) * 1024 * 1024


def _normalized_extension(ext: str) -> str:
    return (ext or "").strip().lstrip(".").lower()


def ensure_upload_dir() -> Path:
    """Ensure STORAGE_UPLOAD_DIR exists (mkdir parents, idempotent)."""
    path = Path(_settings.storage_upload_dir).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


async def compute_file_md5_streaming(
    file_obj: UploadFile,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
) -> Tuple[str, int]:
    """Stream-read `file_obj` -> (md5_hex_lower, total_bytes).

    - Does **not** read the full file into memory.
    - After reading, the internal cursor is rewound to byte 0 so that the
      same UploadFile can be immediately passed to `save_uploaded_file`
      without being exhausted.
    - This function is size agnostic; `save_uploaded_file` enforces the
      50MB cap (enforced twice: once here during md5, once again during
      save, as a dual safety net per spec §4.5 R6).
    """
    md5 = hashlib.md5()
    total = 0
    size_limit = _max_bytes_allowed() + 1  # allow 1 byte overrun so save() can raise w/ correct message
    while True:
        chunk = await file_obj.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > size_limit:
            # Stop wasting cycles: we know it's oversized. Caller will also
            # hit the same limit during save, but fail fast here for UX.
            break
        md5.update(chunk)
    # Rewind so subsequent save_uploaded_file can stream again
    try:
        await file_obj.seek(0)
    except Exception as exc:  # pragma: no cover - defensive for unusual file-likes
        _logger.warning(
            "[storage] Could not seek(0) on file object after MD5: %s",
            exc,
        )
    return md5.hexdigest().lower(), total


async def save_uploaded_file(
    upload_file: UploadFile,
    recording_id: str,
    ext: str,
) -> Tuple[Path, int]:
    """Stream-save `upload_file` to `uploads/<recording_id>.<ext>`.

    Returns (absolute storage path, total written bytes).

    Double safety per spec §4.5 R6 and tickets.md T4 task 2:
        1. Extension whitelist (wav / mp3 / m4a / aac, case-insensitive) —
           raise UnsupportedMediaTypeException; NO partial file written.
        2. Size cap of `MAX_UPLOAD_SIZE_MB` during streaming write; if
           exceeded, delete the partial file IMMEDIATELY (no garbage left)
           and raise PayloadTooLargeException.

    Args:
        upload_file: FastAPI UploadFile from a multipart/form-data request.
        recording_id: UUID string, used as the basename (no extension).
        ext: File extension (may include leading dot; will be normalized).

    Raises:
        UnsupportedMediaTypeException: ext not in whitelist.
        PayloadTooLargeException: file is larger than settings.max_upload_size_mb.
    """
    ext_norm = _normalized_extension(ext)
    if ext_norm not in _ALLOWED_EXTENSIONS:
        raise UnsupportedMediaTypeException(
            actual_ext=ext_norm or "<empty>",
            allowed=sorted(_ALLOWED_EXTENSIONS),
        )

    upload_dir = ensure_upload_dir()
    target_path = (upload_dir / f"{recording_id}.{ext_norm}").resolve()

    max_bytes = _max_bytes_allowed()
    total_bytes = 0
    try:
        with target_path.open("ab") as out:
            while True:
                chunk = await upload_file.read(_DEFAULT_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise PayloadTooLargeException(
                        max_mb=int(_settings.max_upload_size_mb),
                        actual_bytes=total_bytes,
                    )
                out.write(chunk)
    except PayloadTooLargeException:
        # Clean up partial file regardless of failure point — zero garbage.
        try:
            target_path.unlink(missing_ok=True)
        except OSError as exc:
            _logger.warning(
                "[storage] oversized file cleanup unlink failed: %s (ok, will retry)",
                exc,
            )
        raise
    except Exception:
        # Any other disk / IO error: try best-effort unlink, re-raise caller.
        try:
            target_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    _logger.info(
        "[storage] saved %s ext=%s size=%d bytes (limit_mb=%d)",
        recording_id,
        ext_norm,
        total_bytes,
        int(_settings.max_upload_size_mb),
    )
    return target_path, total_bytes


def delete_file_if_exists(storage_path_or_relative: str | Path) -> None:
    """Delete a file from storage if present; NEVER raise FileNotFound.

    Accepts both an absolute path or a relative path (e.g. the string
    `uploads/a.wav` or just `a.wav` stored in `recordings.storage_path`).
    Relative paths are resolved against `STORAGE_UPLOAD_DIR`.

    Any other OSError (e.g. permission denied) is logged as a WARNING but
    swallowed — spec §4.5 DELETE boundary: don't fail the whole DELETE
    endpoint just because a local file was already deleted manually.
    """
    p = Path(storage_path_or_relative)
    if not p.is_absolute():
        p = ensure_upload_dir() / p

    if not p.exists():
        return

    try:
        p.unlink()
    except FileNotFoundError:
        pass  # nothing to do
    except OSError as exc:
        _logger.warning(
            "[storage] delete_file_if_exists unlink failed for %s: %s (will not raise)",
            p,
            exc,
        )


def _resolve_uploads_path(storage_path_or_relative: str | Path) -> Path:
    """Resolve recording.storage_path → absolute under ensure_upload_dir()
    (recordings表通常存相对或绝对都兼容)
    """
    p = Path(storage_path_or_relative)
    if not p.is_absolute():
        return (ensure_upload_dir() / p).resolve()
    return p.resolve()


def _today_trash_dir() -> Path:
    uploads_root = ensure_upload_dir().parent
    today = _dt.date.today().strftime("%Y%m%d")
    d = (uploads_root / ".trash" / today).resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


async def move_upload_to_trash(storage_path_or_relative: str | Path) -> Path:
    """Move uploads/uuid.ext → uploads/.trash/<YYYYMMDD>/uuid.ext

    Priority same-filesystem rename (atomic) → fallback shutil.move → worst
    case copy2 + unlink (跨盘EXDEV). 目标存在则overwrite(幂等).

    Returns absolute trash_path for recover_token INFO log.
    Missing original file -> return empty, no error (软删DB commit已做完, 文件先丢了属best-effort)
    """
    src = _resolve_uploads_path(storage_path_or_relative)
    if not src.exists():
        _logger.warning(
            "[storage.move_trash] src missing best-effort skip: %s", str(src),
        )
        return src
    trash_dir = _today_trash_dir()
    dst = (trash_dir / src.name).resolve()
    try:
        # 1st try: atomic rename (同盘符/同mount)
        if dst.exists():
            dst.unlink(missing_ok=True)
        src.rename(dst)
    except OSError:
        try:
            # 2nd fallback: shutil.move (自带跨盘/覆盖兼容逻辑)
            shutil.move(str(src), str(dst))
        except OSError:
            # 3rd worst case copy2 + unlink (Windows跨盘符极端)
            shutil.copy2(src, dst)
            try:
                src.unlink(missing_ok=True)
            except Exception as exc2:
                _logger.warning(
                    "[storage.move_trash] cross-drive copy2 OK but unlink src fail (leave duplicate best-effort): %s",
                    exc2,
                )
    _logger.info(
        "[storage.move_trash] moved\n  src=%s\n  dst=%s\n  RECOVER_TOKEN=%s",
        str(src), str(dst), str(dst),
    )
    return dst


async def move_trash_back_to_upload(storage_path_or_relative: str | Path) -> Path:
    """Reverse of move_upload_to_trash. Globs .trash/**/uuid.ext first match.

    Returns uploads absolute path. A-Full档TTL已清找不到 => raise 409 RESOURCE_TRASH_EXPIRED
    目标uploads目录若已存在同名(极端恢复两次) -> 直接success idempotent。
    """
    upload_path_abs = _resolve_uploads_path(storage_path_or_relative)
    basename = upload_path_abs.name  # e.g. 550e8400-e29b-41d4-a716-446655440000.wav
    uploads_root = ensure_upload_dir().parent
    pattern = str((uploads_root / ".trash" / "**" / basename).resolve())
    matched = glob.glob(pattern, recursive=True)
    if not matched:
        # 没有trash记录→如果uploads里已经有文件(可能delete时trash失败),也认为"恢复成功"
        if upload_path_abs.exists():
            return upload_path_abs
        _logger.error(
            "[storage.restore] trash file missing basename=%s pattern=%s",
            basename, pattern,
        )
        raise ConflictException(
            code="RESOURCE_TRASH_EXPIRED",
            message="文件已超过30天保留期物理清理,无法恢复",
            details={"basename": basename},
        )
    trash_abs = Path(matched[0]).resolve()
    try:
        uploads_parent = upload_path_abs.parent
        uploads_parent.mkdir(parents=True, exist_ok=True)
        if upload_path_abs.exists():
            upload_path_abs.unlink(missing_ok=True)
        trash_abs.rename(upload_path_abs)
    except OSError:
        try:
            shutil.move(str(trash_abs), str(upload_path_abs))
        except OSError:
            shutil.copy2(trash_abs, upload_path_abs)
            try:
                trash_abs.unlink(missing_ok=True)
            except Exception as exc2:
                _logger.warning("[storage.restore] copy2 OK unlink fail (duplicate left): %s", exc2)
    _logger.info("[storage.restore] basename=%s back to %s", basename, str(upload_path_abs))
    return upload_path_abs
