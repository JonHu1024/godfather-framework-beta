"""进度事件：写入 events 表，SSE 从表读取（跨进程安全）。

事件格式（与设计文档第 10 节一致）：
    {run_id, stage, done, total, timestamp, artifact_ids, cost, message}
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Database
from .util import canonical_json, now_iso

STAGE_INGEST = "ingest"
STAGE_PARSE = "parse"
STAGE_GENERATE = "generate"
STAGE_COMPILE = "compile"
STAGE_VALIDATE = "validate"
STAGE_ANNOTATE = "annotate"
STAGE_REVISION = "revision"
STAGE_DATASET = "dataset"
STAGE_TRAIN = "train"
STAGE_EVAL = "eval"
STAGE_DEPLOY = "deploy"


# 事件表保留策略：条数 + 时间双条件，防止长期运行把 SQLite 撑大
EVENT_RETENTION = 20000
EVENT_RETENTION_DAYS = 30


def _retention_threshold(days: int) -> str:
    """保留期阈值（本地时区的 ISO 字符串，与 ts 的格式一致，字典序即可比较）。"""
    return (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).astimezone().isoformat(timespec="seconds")


def _prune_events(db: Database) -> None:
    """裁剪历史事件：超过条数上限或超过保留天数的都清掉。"""
    try:
        total = db.scalar("SELECT COUNT(*) FROM events") or 0
        if total > EVENT_RETENTION:
            cutoff = db.scalar(
                "SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?", (EVENT_RETENTION - 1,)
            )
            if cutoff:
                db.execute("DELETE FROM events WHERE id <= ?", (cutoff,))
        db.execute("DELETE FROM events WHERE ts < ?", (_retention_threshold(EVENT_RETENTION_DAYS),))
    except Exception:
        # 裁剪失败不能影响事件写入
        return


def emit(
    db: Database,
    *,
    project_id: str | None = None,
    run_id: str | None = None,
    job_id: str | None = None,
    stage: str = "",
    done: int | None = None,
    total: int | None = None,
    message: str = "",
    artifact_ids: list[str] | None = None,
    cost: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    level: str = "info",
) -> int:
    """写入一条事件，返回事件 id（SSE 游标）。"""
    ts = now_iso()
    _prune_events(db)
    cur = db.execute(
        "INSERT INTO events(project_id, run_id, job_id, stage, done, total, ts, artifact_ids_json,"
        " cost_json, message, payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            project_id,
            run_id,
            job_id,
            stage,
            done,
            total,
            ts,
            canonical_json(artifact_ids or []),
            canonical_json(cost or {}),
            message,
            canonical_json(payload or {}),
        ),
    )
    event_id = int(cur.lastrowid or 0)
    if job_id:
        db.execute(
            "INSERT INTO job_logs(job_id, ts, level, message, data_json) VALUES(?,?,?,?,?)",
            (job_id, ts, level, message, canonical_json(payload or {})),
        )
        _prune_job_logs(db, job_id)
    return event_id


# 每个任务保留的日志条数上限（训练会逐行写日志，不限制的话会涨得很快）
JOB_LOG_RETENTION = 2000
JOB_LOG_RETENTION_DAYS = 30


def _prune_job_logs(db: Database, job_id: str) -> None:
    """裁剪单个任务的旧日志，只保留最近 JOB_LOG_RETENTION 条。"""
    try:
        # 按时间清理要无条件执行：
        # 放在"条数没超就 return"的后面的话，日志不满上限时过期日志永远清不掉。
        db.execute(
            "DELETE FROM job_logs WHERE job_id = ? AND ts < ?",
            (job_id, _retention_threshold(JOB_LOG_RETENTION_DAYS)),
        )
        total = db.scalar("SELECT COUNT(*) FROM job_logs WHERE job_id = ?", (job_id,)) or 0
        if total <= JOB_LOG_RETENTION:
            return
        cutoff = db.scalar(
            "SELECT id FROM job_logs WHERE job_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?",
            (job_id, JOB_LOG_RETENTION - 1),
        )
        if cutoff:
            db.execute("DELETE FROM job_logs WHERE job_id = ? AND id <= ?", (job_id, cutoff))
        threshold = (
            datetime.now(timezone.utc) - timedelta(days=JOB_LOG_RETENTION_DAYS)
        ).astimezone().isoformat(timespec="seconds")
        db.execute("DELETE FROM job_logs WHERE job_id = ? AND ts < ?", (job_id, threshold))
    except Exception:
        return


def read_events(
    db: Database,
    *,
    after_id: int = 0,
    project_id: str | None = None,
    limit: int = 200,
) -> list[dict]:
    if project_id:
        rows = db.query_all(
            "SELECT * FROM events WHERE id > ? AND project_id = ? ORDER BY id ASC LIMIT ?",
            (after_id, project_id, limit),
        )
    elif project_id is None:
        # 不带 project_id：只订阅全局事件（project_id IS NULL）
        rows = db.query_all(
            "SELECT * FROM events WHERE id > ? AND project_id IS NULL ORDER BY id ASC LIMIT ?",
            (after_id, limit),
        )
    else:
        # 显式要求跨项目
        rows = db.query_all(
            "SELECT * FROM events WHERE id > ? ORDER BY id ASC LIMIT ?", (after_id, limit)
        )
    return [_normalize(row) for row in rows]


def _normalize(row: dict) -> dict:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "run_id": row["run_id"],
        "job_id": row["job_id"],
        "stage": row["stage"],
        "done": row["done"],
        "total": row["total"],
        "timestamp": row["ts"],
        "artifact_ids": json.loads(row["artifact_ids_json"] or "[]"),
        "cost": json.loads(row["cost_json"] or "{}"),
        "message": row["message"],
        "payload": json.loads(row["payload_json"] or "{}"),
    }


def format_sse(event: dict) -> str:
    """序列化为 SSE 帧。"""
    data = json.dumps(event, ensure_ascii=False)
    return f"id: {event['id']}\nevent: progress\ndata: {data}\n\n"
