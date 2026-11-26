"""请求的项目归属解析。

项目级访问控制要回答两个问题：
1. 这个请求要动哪个项目的资源？
2. 调用方的会话有没有解锁那个项目？

第 1 个问题只能从请求本身推：路径里的 project_id、查询参数 project_id，
或者路径里的对象 id（产物、任务、训练运行……）反查所属项目。

**关键**：查询参数与对象反查的结果**都要算进去**，而且必须全部一致。
只认其中一个会留下绕过：按 id 取对象的端点（`/api/artifacts/{id}` 等）
如果只认查询参数，那么带上 `?project_id=<自己的项目>` 就能读别人的产物。
"""
from __future__ import annotations

import re
from typing import Any

# 路径里带 project_id 的形式：/api/projects/{project_id}/...
_PROJECT_PATH_RE = re.compile(r"^/api/projects/([^/]+)")

# 路径里带对象 id 的形式：前缀 -> 查哪个表拿 project_id
_OBJECT_PATHS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^/api/artifacts/([^/]+)"), "artifacts"),
    (re.compile(r"^/api/documents/([^/]+)"), "documents"),
    (re.compile(r"^/api/generations/([^/]+)"), "generations"),
    (re.compile(r"^/api/jobs/([^/]+)"), "jobs"),
    (re.compile(r"^/api/annotations/([^/]+)"), "annotations"),
    (re.compile(r"^/api/examples/([^/]+)"), "training_examples"),
    (re.compile(r"^/api/questions/([^/]+)"), "question_cards"),
    (re.compile(r"^/api/training/runs/([^/]+)"), "training_runs"),
    (re.compile(r"^/api/training/models/([^/]+)"), "model_versions"),
)


def resolve_target_projects(
    ctx: Any, path: str, query: dict[str, Any] | None = None
) -> set[str]:
    """返回这个请求**可能**涉及的项目 id 集合。

    集合里可能有多个值（查询参数说 A、对象其实属于 B）—— 这是自相矛盾的请求，
    调用方必须要求"全部都与会话解锁的项目一致"才能放行。
    """
    candidates: set[str] = set()

    # 1) 路径里的 project_id
    match = _PROJECT_PATH_RE.match(path)
    if match:
        candidates.add(match.group(1))

    # 2) 查询参数里的 project_id（训练、事件接口用它）
    if query:
        for key, value in query.items():
            if key == "project_id" and value:
                candidates.add(str(value))

    # 3) 路径里的对象 id：反查所属项目
    for pattern, table in _OBJECT_PATHS:
        found = pattern.match(path)
        if not found:
            continue
        object_id = found.group(1)
        try:
            row = ctx.db.query_one(
                f"SELECT project_id FROM {table} WHERE id = ?", (object_id,)
            )
        except Exception:  # noqa: BLE001
            # 查不到归属（DB 抖动/锁库）→ fail-closed：
            # 空串候选会让任何会话的项目 id 都不匹配，从而拒绝。
            candidates.add("")
            break
        if row:
            owner = (row.get("project_id") or "").strip()
            # 对象存在但没有归属（project_id 可空）：fail-closed，
            # 用空串当候选 —— 任何会话的项目 id 都不等于空串，会被拒。
            candidates.add(owner)
        break

    return candidates


def resolve_target_project(ctx: Any, path: str, query: dict[str, Any] | None = None) -> str:
    """返回请求要访问的项目 id；无法确定时返回空串。

    多候选时返回第一个（仅用于展示/日志）；**鉴权必须用**
    :func:`resolve_target_projects` 的完整集合。
    """
    candidates = resolve_target_projects(ctx, path, query)
    return sorted(candidates)[0] if candidates else ""
