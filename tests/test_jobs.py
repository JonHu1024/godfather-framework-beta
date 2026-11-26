"""任务队列：幂等、租约、重试、状态流转。"""
from __future__ import annotations

from godfather.jobs import (
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
)


def test_submit_and_run(ctx, project):
    calls = []

    def handler(job):
        calls.append(job.job_id)
        return {"ok": True}

    ctx.queue.register("demo", handler)
    job = ctx.queue.submit("demo", {"x": 1}, project_id=project)
    assert job["status"] == STATUS_QUEUED

    claimed = ctx.queue._claim("worker-1")
    assert claimed["id"] == job["id"]
    assert claimed["status"] == STATUS_RUNNING
    ctx.queue._run_job(claimed, "worker-1")

    stored = ctx.queue.get(job["id"])
    assert stored["status"] == STATUS_SUCCEEDED
    assert calls == [job["id"]]


def test_idempotency_key_returns_same_job(ctx, project):
    ctx.queue.register("demo", lambda job: {})
    first = ctx.queue.submit("demo", {"a": 1}, idempotency_key="key-1")
    second = ctx.queue.submit("demo", {"a": 2}, idempotency_key="key-1")
    assert first["id"] == second["id"]


def test_failure_retries_then_fails(ctx, project):
    attempts = {"n": 0}

    def handler(job):
        attempts["n"] += 1
        raise RuntimeError("boom")

    ctx.queue.register("bad", handler)
    job = ctx.queue.submit("bad", {}, max_attempts=2)
    for _ in range(2):
        claimed = ctx.queue._claim("worker-1")
        ctx.queue._run_job(claimed, "worker-1")
    stored = ctx.queue.get(job["id"])
    assert stored["status"] == STATUS_FAILED
    assert attempts["n"] == 2
    assert "boom" in stored["error"]


def test_expired_lease_is_reclaimed(ctx, project):
    ctx.queue.register("demo", lambda job: {})
    job = ctx.queue.submit("demo", {})
    claimed = ctx.queue._claim("worker-1")
    # 人为把租约改到过去
    ctx.db.update("jobs", job["id"], {"lease_expires_at": "2000-01-01T00:00:00+00:00"})
    reclaimed = ctx.queue._claim("worker-2")
    assert reclaimed is not None
    assert reclaimed["id"] == job["id"]
    assert reclaimed["attempts"] == claimed["attempts"] + 1


def test_running_job_is_not_claimed_twice(ctx, project):
    ctx.queue.register("demo", lambda job: {})
    ctx.queue.submit("demo", {})
    first = ctx.queue._claim("worker-1")
    second = ctx.queue._claim("worker-2")
    assert first is not None
    assert second is None


def test_cancel_and_resume(ctx, project):
    ctx.queue.register("demo", lambda job: {})
    job = ctx.queue.submit("demo", {})
    assert ctx.queue.cancel(job["id"]) is True
    assert ctx.queue.get(job["id"])["status"] == "cancelled"
    assert ctx.queue.cancel(job["id"]) is False


def test_logs_are_recorded(ctx, project):
    def handler(job):
        job.progress(done=1, total=3, stage="demo", message="进度 1/3")
        return {}

    ctx.queue.register("demo", handler)
    job = ctx.queue.submit("demo", {})
    claimed = ctx.queue._claim("worker-1")
    ctx.queue._run_job(claimed, "worker-1")
    logs = ctx.queue.logs(job["id"])
    assert any("进度 1/3" in entry["message"] for entry in logs)
