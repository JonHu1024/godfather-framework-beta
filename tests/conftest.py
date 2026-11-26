"""pytest 公共夹具：隔离的临时数据目录与 AppContext。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from godfather.context import AppContext
from godfather.settings import load_settings


@pytest.fixture()
def settings(tmp_path: Path):
    cfg = load_settings()
    cfg.storage.data_dir = str(tmp_path / "data")
    cfg.worker.mode = "external"  # 测试中不自动起 worker
    cfg.worker.poll_interval_seconds = 0.05
    cfg.worker.lease_seconds = 30
    cfg.providers.ollama.enabled = True
    cfg.providers.ollama.base_url = "http://127.0.0.1:59999"  # 不可达，用于测试失败路径
    cfg.providers.manual.enabled = True
    cfg.budget.paid_calls_enabled = False
    cfg.budget.max_calls = 5
    cfg.ensure_dirs()
    return cfg


@pytest.fixture()
def ctx(settings) -> AppContext:
    context = AppContext.create(settings)
    yield context
    context.db.close()


@pytest.fixture()
def client(settings):
    from fastapi.testclient import TestClient

    from godfather.app import create_app

    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def drain_jobs(context, limit: int = 20) -> int:
    """同步执行队列中所有可执行任务（测试里替代 worker 进程）。"""
    from godfather.app import register_job_handlers

    register_job_handlers(context)
    processed = 0
    for _ in range(limit):
        job = context.queue._claim("test-worker")
        if job is None:
            break
        context.queue._run_job(job, "test-worker")
        processed += 1
    return processed


@pytest.fixture()
def project(ctx):
    project_id = ctx.db.insert(
        "projects",
        {
            "id": "prj-test",
            "name": "测试项目",
            "taskpack": "markdown_to_latex",
            "description": "",
            "active_model": "",
            "policy_version": "",
            "settings_json": "{}",
            "status": "created",
            "created_at": "2026-11-06T00:00:00+08:00",
            "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )["id"]
    return project_id
