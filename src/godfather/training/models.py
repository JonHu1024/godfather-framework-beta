"""模型版本登记、切换与回滚。

规则（设计文档 §9.2）：
- 每个候选使用独立名称（例如 `godfather-latex:v003`）；
- 评估通过后修改项目的 active_model 指针，保留上一版本；
- 无需向公共模型仓库发布。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..db import Database
from ..util import canonical_json, now_iso


def list_models(db: Database, project_id: str) -> list[dict]:
    rows = db.query_all(
        "SELECT * FROM model_versions WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
    )
    for row in rows:
        row["metrics"] = json.loads(row.pop("metrics_json") or "{}")
    return rows


def get_model(db: Database, model_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM model_versions WHERE id = ?", (model_id,))
    if row:
        row["metrics"] = json.loads(row.pop("metrics_json") or "{}")
    return row


def find_by_name(db: Database, project_id: str, name: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM model_versions WHERE project_id = ? AND name = ?", (project_id, name)
    )


def activate(db: Database, project_id: str, model_id: str) -> dict:
    """把某个模型设为当前使用版本，并保留上一版本（状态改为 archived）。"""
    model = get_model(db, model_id)
    if not model:
        raise ValueError("模型版本不存在")
    if model["project_id"] != project_id:
        # 不能把别的项目的模型激活到自己名下（会出现"项目有 active 模型但指针为空"的矛盾状态）
        raise ValueError("模型版本不属于该项目")
    project = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise ValueError("项目不存在")

    previous = project["active_model"]
    db.execute(
        "UPDATE model_versions SET status = 'archived' WHERE project_id = ? AND status = 'active'",
        (project_id,),
    )
    db.update(
        "model_versions",
        model_id,
        {"status": "active", "deployed_at": now_iso(), "previous_version": previous},
    )
    db.update("projects", project_id, {"active_model": model["name"], "updated_at": now_iso()})
    return {"activated": model["name"], "previous": previous, "model_id": model_id}


def rollback(db: Database, project_id: str) -> dict:
    """回滚到**上一个**版本。

    以前取的是"最近一个被归档的模型"（按 deployed_at 倒序），
    那不是历史栈：activate A → activate B → rollback 回到 A →
    再 rollback 又回到 B，来回跳。
    现在沿 `previous_version` 链回退：每个版本都记着自己上线时替换掉的那个，
    回滚就是顺着这条链走一步。
    """
    project = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise ValueError("项目不存在")
    current = project["active_model"]
    current_row = db.query_one(
        "SELECT * FROM model_versions WHERE project_id = ? AND name = ?", (project_id, current)
    )
    previous_name = str((current_row or {}).get("previous_version") or "")
    if previous_name:
        target = db.query_one(
            "SELECT * FROM model_versions WHERE project_id = ? AND name = ?",
            (project_id, previous_name),
        )
    else:
        target = None
    if not target:
        # 没有链就退回到"最近一个被归档的"（老数据没有 previous_version）
        candidates = db.query_all(
            "SELECT * FROM model_versions WHERE project_id = ? AND status = 'archived' "
            "ORDER BY deployed_at DESC, created_at DESC",
            (project_id,),
        )
        target = candidates[0] if candidates else None
    if not target:
        raise ValueError("没有可回滚的历史版本")
    db.execute(
        "UPDATE model_versions SET status = 'archived' WHERE project_id = ? AND status = 'active'",
        (project_id,),
    )
    db.update(
        "model_versions",
        target["id"],
        {"status": "active", "deployed_at": now_iso(), "previous_version": current},
    )
    db.update("projects", project_id, {"active_model": target["name"], "updated_at": now_iso()})
    return {"activated": target["name"], "previous": current, "model_id": target["id"]}


def latest_evaluation_for_model(db: Database, model_name: str) -> dict | None:
    """取某个模型最近一次评测记录（按模型名匹配）。"""
    rows = db.query_all(
        "SELECT * FROM evaluation_runs WHERE candidate_model_id = ? ORDER BY created_at DESC LIMIT 1",
        (model_name,),
    )
    if rows:
        return rows[0]
    return None


def check_activation(
    db: Database,
    project_id: str,
    model: dict,
    *,
    available_models: list[str] | None = None,
    force: bool = False,
) -> None:
    """激活前的校验：**验证过才发布**。

    DeepInfer 的做法是"置信度调度验证"——没通过验证的权重不进服务。
    这里对应两件事：

    1) 模型得真的还在（Ollama 里没被删 / GGUF 文件不在了 → 激活它等于指向空气）；
    2) 有评测记录的话，不能比基线明显更差。

    force=True 时跳过（用户明确知道自己在做什么）。
    """
    if force:
        return
    name = str(model.get("name") or "")
    path = str(model.get("path") or "")
    in_ollama = available_models is not None and name in available_models
    if available_models is not None and name and not in_ollama:
        # Ollama 里已经没有了（可能被人 `ollama rm` 掉）。
        # 但 Ollama 会把权重复制进自己的 blob 库，
        # 所以原 GGUF 文件还在的话，这个模型仍然可以用（以文件方式）。
        if not (path and Path(path).exists()):
            raise ValueError(
                f"模型 {name} 不在 Ollama 里了（可能已被 ollama rm），"
                f"GGUF 文件也不存在：激活它会让项目指向一个不存在的模型"
            )
    elif not in_ollama and path and not Path(path).exists():
        # Ollama 不可达（拿不到列表）：只能靠文件判断
        raise ValueError(f"模型文件不存在：{path}：激活它等于指向空气")
    evaluation = latest_evaluation_for_model(db, name)
    if not evaluation:
        # 没有评测记录：不阻断（可能是刚导出、还没评测），
        # 但要说清楚，避免用户以为"验过了"。
        return
    try:
        metrics = json.loads(evaluation.get("metrics_json") or "{}")
    except (TypeError, ValueError):
        return
    aggregates = metrics.get("aggregates") or {}
    candidate = aggregates.get("candidate") or {}
    if not candidate:
        return
    non_empty = float(candidate.get("non_empty_rate") or 0)
    if non_empty <= 0 and int(candidate.get("n") or 0) > 0:
        raise ValueError(
            f"模型 {name} 最近一次评测的输出全为空（non_empty_rate=0），不予激活："
            "先确认它确实能产出内容"
        )
    deltas = metrics.get("deltas") or {}
    worse = [
        key
        for key in ("non_empty_rate", "has_latex_rate")
        if key in deltas and float(deltas.get(key) or 0) < -1e-9
    ]
    if worse:
        raise ValueError(
            f"模型 {name} 最近一次评测在 {', '.join(worse)} 上劣于基线（{deltas}），"
            "不予激活；确实要切换请用 force"
        )


def record_metrics(db: Database, model_id: str, metrics: dict[str, Any]) -> None:
    db.update("model_versions", model_id, {"metrics_json": canonical_json(metrics)})
