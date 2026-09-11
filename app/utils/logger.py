from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


_LOG_FORMAT = (
    "[%(asctime)s] %(levelname)-8s [%(name)s:%(lineno)d]"
    " [task_id=%(task_id)s] %(message)s"
)
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class _TaskIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "task_id"):
            record.task_id = "-"
        return True


_initialized = False


def _ensure_dirs() -> None:
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)


def _build_console_handler() -> logging.StreamHandler:
    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
    handler.setFormatter(formatter)
    handler.addFilter(_TaskIdFilter())
    return handler


def _build_file_handler() -> RotatingFileHandler:
    log_path = Path("logs") / "app.log"
    handler = RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
    handler.setFormatter(formatter)
    handler.addFilter(_TaskIdFilter())
    return handler


def setup_logging() -> None:
    global _initialized
    if _initialized:
        return

    _ensure_dirs()

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    for existing in list(root_logger.handlers):
        root_logger.removeHandler(existing)

    root_logger.addHandler(_build_console_handler())
    root_logger.addHandler(_build_file_handler())
    root_logger.propagate = False

    _initialized = True


def get_logger(name: str) -> logging.Logger:
    if not _initialized:
        setup_logging()
    return logging.getLogger(name)
