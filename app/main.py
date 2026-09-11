from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI

from app.api.v1 import v1_router
from app.config import Settings
from app.database import AsyncSessionLocal
from app.services import llm as llm_service
from app.services import pipeline as pipeline_service
from app.services.storage import ensure_upload_dir
from app.utils.errors import register_exception_handlers
from app.utils.logger import get_logger, setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    logger = get_logger("app.main")
    settings = Settings()
    logger.info(
        "Starting %s v%s",
        app.title,
        app.version,
        extra={"task_id": "bootstrap"},
    )
    # --- Startup ---
    ensure_upload_dir()
    llm_service.init_client(settings)
    await pipeline_service.start_workers(settings)
    async with AsyncSessionLocal() as session:
        await pipeline_service.resume_pending_tasks_on_startup(session)

    yield

    # --- Shutdown ---
    logger.info(
        "Shutting down %s",
        app.title,
        extra={"task_id": "shutdown"},
    )
    await pipeline_service.shutdown_workers()
    await llm_service.shutdown_client()


app = FastAPI(
    title="Xisiyun ASR Service",
    version="0.1.0",
    lifespan=lifespan,
)

register_exception_handlers(app)

app.include_router(v1_router)


@app.get("/health", tags=["meta"])
async def health_check() -> dict:
    logger = get_logger("app.health")
    logger.info("Health check called", extra={"task_id": "health"})
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": app.title,
    }
