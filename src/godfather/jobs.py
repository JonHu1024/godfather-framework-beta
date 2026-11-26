"""任务队列：数据库任务表 + 租约（lease）+ 幂等键 + 有限重试。

状态机：
    queued → running → succeeded
                  ↘ failed（重试耗尽）
                  ↘ waiting_user / paused_budget / paused_resource（可恢复）
    queued/running → cancelled

关键保证：
- 租约到期可被其它 worker 接管；同一时刻不会有两个 worker 执行同一任务。
- 幂等键唯一：相同键重复提交返回既有任务，不会重复产生副作用。
- 成功产物不被重试覆盖：重试产生新的 attempt（attempts 计数 + 日志保留）。
"""
from __future__ import annotations

import json
import os
import socket
import threading
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .db import Database
from .events import emit
from .storage import ArtifactStore
from .util import canonical_json, new_id, now_iso

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_WAITING_USER = "waiting_user"
STATUS_PAUSED_BUDGET = "paused_budget"
STATUS_PAUSED_RESOURCE = "paused_resource"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

ACTIVE_STATUSES = {STATUS_QUEUED, STATUS_RUNNING, STATUS_WAITING_USER, STATUS_PAUSED_BUDGET, STATUS_PAUSED_RESOURCE, STATUS_SUCCEEDED}
TERMINAL_STATUSES = {STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED}
RESUMABLE_STATUSES = {STATUS_WAITING_USER, STATUS_PAUSED_BUDGET, STATUS_PAUSED_RESOURCE}


class JobPaused(Exception):
    """任务因外部条件暂停（资源被占用、预算不足等），不是失败。"""

    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


class PermanentJobError(RuntimeError):
    """确定性失败：重试也不会变好（例如数据为空、预检不通过、TeX 编译失败）。"""

    def __init__(self, message: str = "", *, result: dict[str, Any] | None = None):
        super().__init__(message)
        self.result = result


def _iso_in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).astimezone().isoformat(timespec="seconds")


def _is_expired(value: str | None) -> bool:
    if not value:
        return True
    try:
        return datetime.fromisoformat(value) < datetime.now(timezone.utc)
    except ValueError:
        return True


@dataclass
class JobContext:
    db: Database
    store: ArtifactStore
    settings: Any
    job: dict

    def emit_event(self, **kwargs: Any) -> int:
        kwargs.setdefault("project_id", self.project_id)
        kwargs.setdefault("job_id", self.job_id)
        return emit(self.db, **kwargs)

    @property
    def job_id(self) -> str:
        return self.job["id"]

    @property
    def project_id(self) -> str | None:
        return self.job.get("project_id")

    @property
    def payload(self) -> dict:
        return json.loads(self.job.get("payload_json") or "{}")

    def progress(self, done: int | None = None, total: int | None = None, stage: str = "", message: str = "") -> None:
        values: dict[str, Any] = {}
        if done is not None:
            values["progress_done"] = done
        if total is not None:
            values["progress_total"] = total
        if stage:
            values["stage"] = stage
        values["updated_at"] = now_iso()
        self.db.update("jobs", self.job_id, values)
        if done is not None or message:
            self.emit_event(
                stage=stage or self.job.get("stage") or "",
                done=done,
                total=total,
                message=message,
            )


JobHandler = Callable[[JobContext], dict | None]


class JobQueue:
    def __init__(self, db: Database, settings: Any):
        self.db = db
        self.settings = settings
        self.handlers: dict[str, JobHandler] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ---- 注册 ----
    def register(self, job_type: str, handler: JobHandler) -> None:
        self.handlers[job_type] = handler

    def register_disposable(self, job_type: str, handler: JobHandler):
        """注册任务处理器并返回清理句柄（供 Cordis EffectScope 自动回收）。"""
        self.handlers[job_type] = handler

        def _unregister() -> None:
            if self.handlers.get(job_type) is handler:
                self.handlers.pop(job_type, None)

        return _unregister

    def unregister(self, job_type: str) -> None:
        self.handlers.pop(job_type, None)

    # ---- 提交 ----
    def submit(
        self,
        job_type: str,
        payload: dict,
        *,
        project_id: str | None = None,
        idempotency_key: str | None = None,
        priority: int = 100,
        max_attempts: int | None = None,
    ) -> dict:
        if idempotency_key:
            existing = self.db.query_one(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
            )
            if existing:
                # 只有"还在进行中或已成功"的任务才复用：
                # 已失败/已取消的任务必须允许重新提交，否则用户永远无法重跑
                if existing["status"] in ACTIVE_STATUSES:
                    # 标记这是"复用"：前端要能区分"新建任务"与"命中已有任务"
                    reused = dict(existing)
                    reused["reused"] = True
                    return reused
                # 失败的旧任务占用着幂等键：清掉它，让新任务能建起来
                self.db.execute(
                    "UPDATE jobs SET idempotency_key = NULL WHERE id = ?", (existing["id"],)
                )
        max_attempts = max_attempts or getattr(self.settings.worker, "max_attempts", 3)
        job = {
            "id": new_id("job"),
            "project_id": project_id,
            "type": job_type,
            "payload_json": canonical_json(payload),
            "status": STATUS_QUEUED,
            "priority": priority,
            "attempts": 0,
            "max_attempts": max_attempts,
            "lease_owner": "",
            "lease_expires_at": None,
            "idempotency_key": idempotency_key,
            "progress_done": 0,
            "progress_total": 0,
            "stage": "",
            "error": "",
            "result_json": "{}",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "started_at": None,
            "finished_at": None,
        }
        try:
            self.db.insert("jobs", job)
        except Exception as exc:  # 并发下唯一键冲突
            if idempotency_key:
                existing = self.db.query_one(
                    "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
                )
                if existing:
                    # 命中已有任务：标记 reused，前端要能区分"新建"与"复用"
                    reused = dict(existing)
                    reused["reused"] = True
                    return reused
            raise exc
        emit(
            self.db,
            project_id=project_id,
            job_id=job["id"],
            stage="queued",
            message=f"任务已入队：{job_type}",
            payload={"type": job_type},
        )
        return job

    def cancel(self, job_id: str) -> bool:
        job = self.db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not job or job["status"] in TERMINAL_STATUSES:
            return False
        self.db.update(
            "jobs",
            job_id,
            {
                "status": STATUS_CANCELLED,
                "updated_at": now_iso(),
                "finished_at": now_iso(),
                "lease_owner": "",
                "lease_expires_at": None,
            },
        )
        emit(self.db, project_id=job["project_id"], job_id=job_id, stage="cancelled", message="任务已取消")
        return True

    def resume(self, job_id: str) -> bool:
        """恢复一个暂停的任务。尝试次数要重置，否则恢复后可能一次都跑不了。"""
        job = self.db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not job or job["status"] not in RESUMABLE_STATUSES:
            return False
        self.db.update(
            "jobs",
            job_id,
            {
                "status": STATUS_QUEUED,
                "updated_at": now_iso(),
                "lease_owner": "",
                "lease_expires_at": None,
                # 恢复后重新获得完整的重试次数
                "attempts": 0,
                "error": "",
            },
        )
        emit(self.db, project_id=job["project_id"], job_id=job_id, stage="queued", message="任务已恢复排队")
        return True

    def get(self, job_id: str) -> dict | None:
        return self.db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))

    def list_jobs(self, project_id: str | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
        clauses, params = [], []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self.db.query_all(f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ?", params)

    def logs(self, job_id: str, limit: int = 500, tail: bool = True) -> list[dict]:
        """任务日志。默认返回**最后** limit 条（长任务失败时，最早的日志没有诊断价值）。"""
        if tail:
            rows = self.db.query_all(
                "SELECT * FROM job_logs WHERE job_id = ? ORDER BY id DESC LIMIT ?", (job_id, limit)
            )
            rows.reverse()
        else:
            rows = self.db.query_all(
                "SELECT * FROM job_logs WHERE job_id = ? ORDER BY id ASC LIMIT ?", (job_id, limit)
            )
        for row in rows:
            row["data"] = json.loads(row.pop("data_json") or "{}")
        return rows

    # ---- 领取 ----
    def _claim(self, owner: str) -> dict | None:
        lease_seconds = getattr(self.settings.worker, "lease_seconds", 120)
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY priority ASC, created_at ASC LIMIT 1",
                (STATUS_QUEUED,),
            ).fetchone()
            if row is None:
                # 接管租约过期的 running 任务
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY priority ASC, created_at ASC",
                    (STATUS_RUNNING,),
                ).fetchall()
                expired = [dict(r) for r in rows if _is_expired(r["lease_expires_at"])]
                if not expired:
                    return None
                candidate = expired[0]
                conn.execute(
                    "UPDATE jobs SET status=?, lease_owner=?, lease_expires_at=?, attempts=attempts+1,"
                    " updated_at=?, error=? WHERE id=?",
                    (
                        STATUS_RUNNING,
                        owner,
                        _iso_in(lease_seconds),
                        now_iso(),
                        "租约超时，已被接管",
                        candidate["id"],
                    ),
                )
                candidate = dict(candidate)
                candidate["status"] = STATUS_RUNNING
                candidate["attempts"] += 1
                candidate["lease_owner"] = owner
                candidate["lease_expires_at"] = _iso_in(lease_seconds)
                candidate["error"] = "租约超时，已被接管"
                return candidate

            job = dict(row)
            conn.execute(
                "UPDATE jobs SET status=?, lease_owner=?, lease_expires_at=?, attempts=attempts+1,"
                " started_at=COALESCE(started_at, ?), updated_at=? WHERE id=?",
                (STATUS_RUNNING, owner, _iso_in(lease_seconds), now_iso(), now_iso(), job["id"]),
            )
            job["status"] = STATUS_RUNNING
            job["attempts"] += 1
            job["lease_owner"] = owner
            job["lease_expires_at"] = _iso_in(lease_seconds)
            return job

    def _heartbeat_loop(self, job_id: str, owner: str, stop_event: threading.Event) -> None:
        """定期续租；若任务已被取消或租约被他人接管则停止。"""
        lease_seconds = getattr(self.settings.worker, "lease_seconds", 120)
        interval = max(2.0, min(30.0, lease_seconds / 3.0))
        while not stop_event.wait(interval):
            try:
                row = self.db.query_one("SELECT status, lease_owner FROM jobs WHERE id = ?", (job_id,))
            except Exception as exc:  # noqa: BLE001
                # 续租失败不能中断心跳，但要留下痕迹
                import logging

                logging.getLogger("godfather.jobs").debug("续租查询失败（job=%s）：%s", job_id, exc)
                continue
            if row is None:
                return
            if row["status"] in (STATUS_CANCELLED, STATUS_SUCCEEDED, STATUS_FAILED):
                return
            if row["lease_owner"] and row["lease_owner"] != owner:
                # 租约已被别人接管，放弃续租
                return
            self._renew(job_id, owner)

    def _renew(self, job_id: str, owner: str) -> None:
        lease_seconds = getattr(self.settings.worker, "lease_seconds", 120)
        # 条件更新：只续自己的租约。
        # 无条件续租的话，任务被别人接管后旧 worker 还会把租约续回去，
        # 两个 worker 会同时执行同一个任务。
        self.db.execute(
            "UPDATE jobs SET lease_expires_at = ?, updated_at = ? WHERE id = ? AND lease_owner = ?",
            (_iso_in(lease_seconds), now_iso(), job_id, owner),
        )

    # ---- 执行 ----
    def _run_job(self, job: dict, owner: str) -> None:
        handler = self.handlers.get(job["type"])
        if handler is None:
            self.db.update(
                "jobs",
                job["id"],
                {
                    "status": STATUS_FAILED,
                    "error": f"未注册的任务类型: {job['type']}",
                    "finished_at": now_iso(),
                    "updated_at": now_iso(),
                },
            )
            emit(self.db, project_id=job["project_id"], job_id=job["id"], stage="failed", message=f"未注册的任务类型: {job['type']}")
            return
        ctx = JobContext(db=self.db, store=self.settings.artifact_store, settings=self.settings, job=job)
        # 租约心跳：长任务（训练可能几十分钟）持续续租，
        # 否则租约过期后会被其它 worker 当成"崩溃的任务"接管，导致同一个任务被执行两次。
        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(job["id"], owner, stop_heartbeat),
            daemon=True,
            name=f"lease-{job['id'][:8]}",
        )
        heartbeat.start()
        try:
            result = handler(ctx) or {}
            # 条件更新：如果任务在执行期间被取消（状态已不是 running），
            # 就不要用"成功"覆盖掉"已取消"（取消状态必须真实反映给用户）。
            cur = self.db.conn.execute(
                "UPDATE jobs SET status = ?, result_json = ?, finished_at = ?, updated_at = ?, "
                "lease_owner = '', lease_expires_at = NULL, error = '' "
                "WHERE id = ? AND status = ? AND (lease_owner = ? OR lease_owner = '')",
                (
                    STATUS_SUCCEEDED,
                    canonical_json(result),
                    now_iso(),
                    now_iso(),
                    job["id"],
                    STATUS_RUNNING,
                    owner,
                ),
            )
            if cur.rowcount == 0:
                current = self.db.query_one("SELECT status FROM jobs WHERE id = ?", (job["id"],))
                emit(
                    self.db,
                    project_id=job["project_id"],
                    job_id=job["id"],
                    stage="cancelled",
                    message=f"任务在执行期间被取消，忽略本次结果（当前状态：{current['status'] if current else '未知'}）",
                    level="warning",
                )
                return
            emit(
                self.db,
                project_id=job["project_id"],
                job_id=job["id"],
                stage="succeeded",
                message="任务完成",
                payload=result if isinstance(result, dict) else {},
            )
        except JobPaused as paused:
            # 条件更新：任务若在执行期间被取消，就不要再改成暂停态
            cur = self.db.execute(
                "UPDATE jobs SET status = ?, error = ?, updated_at = ?, "
                "lease_owner = '', lease_expires_at = NULL "
                "WHERE id = ? AND status = ? AND (lease_owner = ? OR lease_owner = '')",
                (
                    paused.status,
                    paused.reason,
                    now_iso(),
                    job["id"],
                    STATUS_RUNNING,
                    owner,
                ),
            )
            if cur.rowcount == 0:
                current = self.db.query_one("SELECT status FROM jobs WHERE id = ?", (job["id"],))
                emit(
                    self.db,
                    project_id=job["project_id"],
                    job_id=job["id"],
                    stage="cancelled",
                    message=f"任务在执行期间被取消，忽略暂停结果（当前状态：{current['status'] if current else '未知'}）",
                    level="warning",
                )
                return
            emit(
                self.db,
                project_id=job["project_id"],
                job_id=job["id"],
                stage=paused.status,
                message=f"任务暂停：{paused.reason}",
                level="warning",
            )
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            attempts = job.get("attempts", 1)
            max_attempts = job.get("max_attempts", 3)
            trace = traceback.format_exc()
            permanent = isinstance(exc, PermanentJobError)
            if permanent:
                max_attempts = 0
            if attempts < max_attempts:
                # 条件更新：被取消的任务不能变回 queued（否则会复活再跑一遍）
                cur = self.db.execute(
                    "UPDATE jobs SET status = ?, error = ?, updated_at = ?, "
                    "lease_owner = '', lease_expires_at = NULL, result_json = ? "
                    "WHERE id = ? AND status = ? AND (lease_owner = ? OR lease_owner = '')",
                    (
                        STATUS_QUEUED,
                        detail,
                        now_iso(),
                        canonical_json({"traceback": trace}),
                        job["id"],
                        STATUS_RUNNING,
                        owner,
                    ),
                )
                if cur.rowcount == 0:
                    current = self.db.query_one("SELECT status FROM jobs WHERE id = ?", (job["id"],))
                    emit(
                        self.db,
                        project_id=job["project_id"],
                        job_id=job["id"],
                        stage="cancelled",
                        message=f"任务在执行期间被取消，不再重试（当前状态：{current['status'] if current else '未知'}）",
                        level="warning",
                    )
                    return
                emit(
                    self.db,
                    project_id=job["project_id"],
                    job_id=job["id"],
                    stage="retrying",
                    message=f"失败，将重试（{attempts}/{max_attempts}）：{detail}",
                    level="warning",
                )
            else:
                structured_result = (
                    exc.result
                    if isinstance(getattr(exc, "result", None), dict)
                    else {"traceback": trace}
                )
                cur = self.db.execute(
                    "UPDATE jobs SET status = ?, error = ?, result_json = ?, finished_at = ?, "
                    "updated_at = ?, lease_owner = '', lease_expires_at = NULL "
                    "WHERE id = ? AND status = ? AND (lease_owner = ? OR lease_owner = '')",
                    (
                        STATUS_FAILED,
                        detail,
                        canonical_json(structured_result),
                        now_iso(),
                        now_iso(),
                        job["id"],
                        STATUS_RUNNING,
                        owner,
                    ),
                )
                if cur.rowcount == 0:
                    current = self.db.query_one("SELECT status FROM jobs WHERE id = ?", (job["id"],))
                    emit(
                        self.db,
                        project_id=job["project_id"],
                        job_id=job["id"],
                        stage="cancelled",
                        message=f"任务在执行期间被取消，忽略失败结果（当前状态：{current['status'] if current else '未知'}）",
                        level="warning",
                    )
                    return
                emit(
                    self.db,
                    project_id=job["project_id"],
                    job_id=job["id"],
                    stage="failed",
                    message=f"任务失败：{detail}" if permanent else f"失败（已达最大重试次数）：{detail}",
                    level="error",
                    payload={**structured_result, "traceback": trace} if isinstance(structured_result, dict) else {"traceback": trace},
                )
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=2)

    # ---- worker 循环 ----
    def worker_loop(self, owner: str | None = None, stop_event: threading.Event | None = None) -> None:
        owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        stop = stop_event or self._stop
        poll = float(getattr(self.settings.worker, "poll_interval_seconds", 0.5))
        while not stop.is_set():
            try:
                job = self._claim(owner)
            except Exception:
                job = None
            if job is None:
                stop.wait(poll)
                continue
            try:
                self._run_job(job, owner)
            except Exception:
                stop.wait(poll)

    def start_embedded(self, count: int = 1) -> None:
        for index in range(count):
            thread = threading.Thread(
                target=self.worker_loop,
                kwargs={"owner": f"embedded-{index}-{os.getpid()}"},
                daemon=True,
                name=f"godfather-worker-{index}",
            )
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5)
