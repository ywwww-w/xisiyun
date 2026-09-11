from fastapi import APIRouter

from app.api.v1.router_recordings import router as recordings_router
from app.api.v1.router_tasks import router as tasks_router

v1_router = APIRouter(prefix="/v1")

v1_router.include_router(recordings_router)
v1_router.include_router(tasks_router)

__all__ = ["v1_router"]
