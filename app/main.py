from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI

from app.utils.errors import register_exception_handlers
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
