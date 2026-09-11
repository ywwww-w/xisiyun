from __future__ import annotations

import asyncio
import glob as _glob
import os
import random
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base, get_session
from app.models import Recording, Task, TaskStatus


TESTS_ROOT = Path(__file__).parent.resolve()
PROJECT_ROOT = TESTS_ROOT.parent


@pytest.fixture(scope="session")
def event_loop_policy():
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(scope="function")
def test_settings(tmp_path_factory, request) -> Settings:
    upload_dir = tmp_path_factory.mktemp("uploads")
    settings = Settings(
        mysql_host="127.0.0.1",
        mysql_port=3306,
        mysql_user="root",
        mysql_password=None,
        mysql_database="xisiyun_asr_test",
        deepseek_api_key="sk-test-dummy",
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-chat",
        deepseek_timeout_seconds=3,
        max_concurrent_tasks=2,
        max_upload_size_mb=50,
        storage_upload_dir=upload_dir,
        pipeline_max_auto_retries=1,
    )
    return settings


_SQLITE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="function")
async def test_engine(test_settings: Settings):
    engine = create_async_engine(
        _SQLITE_URL,
        echo=False,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture(scope="function")
async def db_session(test_engine) -> AsyncSession:
    TestingSessionLocal = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    async with TestingSessionLocal() as session:
        yield session
        try:
            await session.rollback()
        except Exception:
            pass
        await session.close()


@pytest.fixture(scope="function")
def clean_uploads(test_settings: Settings):
    upload_dir: Path = Path(test_settings.storage_upload_dir)
    shutil.rmtree(upload_dir, ignore_errors=True)
    upload_dir.mkdir(parents=True, exist_ok=True)
    yield upload_dir
    shutil.rmtree(upload_dir, ignore_errors=True)


@pytest.fixture(scope="function")
def override_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    test_settings: Settings,
    db_session: AsyncSession,
    test_engine,
    clean_uploads,
):
    from app import database as _db_module
    from app.api.v1 import router_recordings as _router_rec_module
    from app.services import llm as _llm_module
    from app.services import pipeline as _pipeline_module
    from app.services import storage as _storage_module
    from app import main as _main_module

    TestingSessionLocal = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    upload_dir: Path = Path(test_settings.storage_upload_dir).resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("app.config.Settings", lambda: test_settings)
    monkeypatch.setattr(_db_module, "_settings", test_settings)
    monkeypatch.setattr(_storage_module, "_settings", test_settings)
    monkeypatch.setattr(_router_rec_module, "_settings", test_settings)
    monkeypatch.setattr(
        _router_rec_module,
        "_MAX_BYTES",
        int(test_settings.max_upload_size_mb) * 1024 * 1024,
    )
    monkeypatch.setattr(_router_rec_module, "_UPLOADS", upload_dir)

    monkeypatch.setattr(_db_module, "engine", test_engine)
    monkeypatch.setattr(_db_module, "AsyncSessionLocal", TestingSessionLocal)
    monkeypatch.setattr(_pipeline_module, "AsyncSessionLocal", TestingSessionLocal)
    monkeypatch.setattr(_main_module, "AsyncSessionLocal", TestingSessionLocal)

    async def _override_get_session():
        s = TestingSessionLocal()
        try:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise
        finally:
            try:
                await s.close()
            except Exception:
                pass

    from app.main import app as _app

    _app.dependency_overrides[get_session] = _override_get_session

    # Mock 1: skip pipeline sleep; no ASR random failure
    monkeypatch.setattr(_pipeline_module.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(_pipeline_module.random, "random", lambda: 0.0)
    monkeypatch.setattr(random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(random, "random", lambda: 0.0)
    monkeypatch.setattr(_pipeline_module, "_MOCK_ASR_MIN_SLEEP_SECONDS", 0.0)
    monkeypatch.setattr(_pipeline_module, "_MOCK_ASR_MAX_SLEEP_SECONDS", 0.0)
    monkeypatch.setattr(_pipeline_module, "_MOCK_ASR_FAIL_PROBABILITY", 0.0)
    monkeypatch.setattr(_pipeline_module, "_placeholder_sleep_seconds", 0.0)

    # Mock 2: LLM summarize_transcript returns mock dict (no real HTTP)
    fake_llm_result = {
        "summary": "Mock summary",
        "key_points": ["Point 1", "Point 2"],
        "todos": ["Todo 1"],
    }
    monkeypatch.setattr(
        _llm_module,
        "summarize_transcript",
        AsyncMock(return_value=fake_llm_result),
    )

    yield {
        "settings": test_settings,
        "llm_result": fake_llm_result,
    }

    _app.dependency_overrides.pop(get_session, None)


@pytest.fixture(scope="function")
async def client(override_dependencies, clean_uploads, test_settings: Settings):
    from app.main import app as _app, lifespan as _lifespan

    async with _lifespan(_app):
        transport = ASGITransport(app=_app)
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as ac:
            yield ac
