from __future__ import annotations

import hashlib
from pathlib import Path
from typing import AsyncGenerator, Tuple

from fastapi import UploadFile

from app.config import Settings
from app.utils.errors import PayloadTooLargeException, UnsupportedMediaTypeException
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
