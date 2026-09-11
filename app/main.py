from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Query
from pydantic import Field

from app.utils.errors import (
    BadRequestException,
    ConflictException,
    LLMCallException,
    LLMResponseFormatException,
    NotFoundException,
    PayloadTooLargeException,
    PipelineStateException,
    UnsupportedMediaTypeException,
    register_exception_handlers,
)
from app.utils.logger import get_logger, setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    logger = get_logger("app.main")
    logger.info(
        "Starting %s v%s",
        app.title,
        app.version,
        extra={"task_id": "bootstrap"},
    )
    yield
    logger.info(
        "Shutting down %s",
        app.title,
        extra={"task_id": "shutdown"},
    )


app = FastAPI(
    title="Xisiyun ASR Service",
    version="0.1.0",
    lifespan=lifespan,
)

register_exception_handlers(app)


@app.get("/health", tags=["meta"])
async def health_check() -> dict:
    logger = get_logger("app.health")
    logger.info("Health check called", extra={"task_id": "health"})
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": app.title,
    }


# ========== Ticket 2 验收专用临时路由（验收完成后在下个 Ticket 删除） ==========


@app.get("/_t2_test/errors/notfound", tags=["debug"])
async def _debug_notfound() -> None:
    raise NotFoundException("Recording", "123")


@app.get("/_t2_test/errors/conflict", tags=["debug"])
async def _debug_conflict() -> None:
    raise PipelineStateException(
        task_id="t-456",
        current_status="done",
        expected_status="failed",
    )


@app.get("/_t2_test/errors/validation", tags=["debug"])
async def _debug_validation(
    page: int = Query(ge=1, default=1),
    page_size: int = Query(ge=1, le=100, default=20),
) -> dict:
    if page < 0:
        raise BadRequestException("page must be positive")
    return {"page": page, "page_size": page_size}


@app.get("/_t2_test/errors/crash", tags=["debug"])
async def _debug_crash() -> None:
    _ = 1 / 0


@app.get("/_t2_test/errors/415", tags=["debug"])
async def _debug_415() -> None:
    raise UnsupportedMediaTypeException(allowed=["wav", "mp3", "m4a", "aac"])


@app.get("/_t2_test/errors/413", tags=["debug"])
async def _debug_413() -> None:
    raise PayloadTooLargeException(max_mb=50, actual_bytes=51 * 1024 * 1024)


@app.get("/_t2_test/errors/502_call", tags=["debug"])
async def _debug_502_llm() -> None:
    raise LLMCallException("Upstream refused connection", upstream_status=502)


@app.get("/_t2_test/errors/502_format", tags=["debug"])
async def _debug_502_format() -> None:
    raise LLMResponseFormatException(
        "LLM did not output valid JSON",
        raw_preview="Here is a summary: not json at all",
    )


@app.get("/_t2_test/conflict_generic", tags=["debug"])
async def _debug_conflict_generic() -> None:
    raise ConflictException(
        "Task already retried",
        code="TASK_NOT_RETRYABLE",
    )
