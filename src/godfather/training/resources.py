"""稀缺资源调度（GPU）。

设计要求（§9.1）：
- 同一 GPU 上推理与训练默认串行调度；
- 避免 Ollama 常驻教师占满显存；资源不足时暂停或卸载可恢复的模型。

实现：
- `resource_locks` 表做互斥（谁拿到锁谁训练）；
- 训练开始前检查 Ollama 已加载的模型，显存被占满时暂停而不是硬挤。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..db import Database
from ..util import now_iso

GPU_RESOURCE = "gpu"


@dataclass
class ResourceState:
    resource: str
    locked: bool
    owner: str = ""
    acquired_at: str = ""
    ollama_models: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "resource": self.resource,
            "locked": self.locked,
            "owner": self.owner,
            "acquired_at": self.acquired_at,
            "ollama_models": self.ollama_models,
        }


def acquire(db: Database, resource: str, owner: str, *, project_id: str | None = None, ttl_seconds: int = 7200) -> bool:
    """尝试获取资源锁；已被占用则返回 False。"""
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM resource_locks WHERE resource = ?", (resource,)).fetchone()
        now = now_iso()
        if row is None:
            conn.execute(
                "INSERT INTO resource_locks(id, resource, owner, project_id, acquired_at, expires_at, note) "
                "VALUES(?,?,?,?,?,?,?)",
                (resource, resource, owner, project_id, now, _expires(ttl_seconds), "训练占用"),
            )
            return True
        expires = row["expires_at"]
        if expires and _expired(expires):
            conn.execute(
                "UPDATE resource_locks SET owner=?, project_id=?, acquired_at=?, expires_at=?, note=? WHERE resource=?",
                (owner, project_id, now, _expires(ttl_seconds), "训练占用（接管过期锁）", resource),
            )
            return True
        return False


def renew(db: Database, resource: str, owner: str, *, ttl_seconds: int = 7200) -> bool:
    """续租：长任务（训练可能几小时）必须持续延长锁的有效期。

    没有续租的话，TTL 到期后另一个训练会"接管过期锁"，
    两个训练就会同时占用同一块 GPU。
    """
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE resource_locks SET expires_at=? WHERE resource=? AND owner=?",
            (_expires(ttl_seconds), resource, owner),
        )
        return cur.rowcount > 0


class ResourceHeartbeat:
    """资源锁续租心跳（上下文管理器）。"""

    def __init__(self, db: Database, resource: str, owner: str, *, ttl_seconds: int = 7200, interval: float = 60.0):
        self.db = db
        self.resource = resource
        self.owner = owner
        self.ttl_seconds = ttl_seconds
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> ResourceHeartbeat:
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"res-{self.resource}")
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                if not renew(self.db, self.resource, self.owner, ttl_seconds=self.ttl_seconds):
                    return
            except Exception:
                continue

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


def release(db: Database, resource: str, owner: str | None = None) -> None:
    if owner:
        db.execute("DELETE FROM resource_locks WHERE resource = ? AND owner = ?", (resource, owner))
    else:
        db.execute("DELETE FROM resource_locks WHERE resource = ?", (resource,))


def _expires(ttl_seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).astimezone().isoformat(timespec="seconds")


def _expired(value: str) -> bool:
    from datetime import datetime, timezone

    try:
        return datetime.fromisoformat(value) < datetime.now(timezone.utc)
    except ValueError:
        return True


# Ollama 探测结果的短暂缓存：第一次连接 Ollama 可能要两三秒，
# 每次打开页面都同步等一次会让界面卡住。
_OLLAMA_CACHE: dict[str, tuple[float, list[dict]]] = {}
_OLLAMA_CACHE_TTL = 5.0


def ollama_loaded_models(base_url: str = "http://127.0.0.1:11434", timeout: float = 3.0) -> list[dict]:
    """查询 Ollama 当前常驻的模型（占显存的大头）。"""
    cached = _OLLAMA_CACHE.get(base_url)
    if cached and (time.time() - cached[0]) < _OLLAMA_CACHE_TTL:
        return cached[1]
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/api/ps", timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        _OLLAMA_CACHE[base_url] = (time.time(), [])
        return []
    models = []
    for item in payload.get("models") or []:
        models.append(
            {
                "name": item.get("name") or item.get("model"),
                "size_vram": item.get("size_vram") or item.get("size") or 0,
                "expires_at": item.get("expires_at"),
            }
        )
    _OLLAMA_CACHE[base_url] = (time.time(), models)
    return models


def gpu_state(db: Database, base_url: str = "http://127.0.0.1:11434") -> dict[str, Any]:
    row = db.query_one("SELECT * FROM resource_locks WHERE resource = ?", (GPU_RESOURCE,))
    models = ollama_loaded_models(base_url)
    vram_bytes = sum(int(m.get("size_vram") or 0) for m in models)
    return {
        "resource": GPU_RESOURCE,
        "locked": bool(row),
        "owner": row["owner"] if row else "",
        "acquired_at": row["acquired_at"] if row else "",
        "ollama_models": models,
        "ollama_vram_bytes": vram_bytes,
        "hint": (
            "Ollama 正占用显存，训练前建议先卸载模型（ollama stop <model>）"
            if models
            else "Ollama 当前没有常驻模型"
        ),
    }


def wait_for_resource(
    db: Database,
    resource: str,
    owner: str,
    *,
    project_id: str | None = None,
    timeout_seconds: int = 1800,
    poll_seconds: float = 5.0,
    on_wait: Any = None,
    should_cancel: Any = None,
) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if acquire(db, resource, owner, project_id=project_id):
            return True
        if on_wait:
            # 回调**零参**：runner/export 传的都是零参 lambda。
            # 之前 evaluate 传的是 lambda waited: → 真有 GPU 锁被占用时
            # 评测任务直接 TypeError 崩掉（正好命中"评测也要占 GPU 锁"的目标场景）。
            on_wait()
        # 等待期间要能被取消打断：
        # otherwise 用户点了取消，训练还要在这里傻等到超时（默认 15 分钟）。
        if should_cancel and should_cancel():
            return False
        time.sleep(poll_seconds)
    return False
