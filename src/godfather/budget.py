"""预算与调用审计。

规则（设计文档 §7.3）：
- 默认关闭付费调用；项目设置总预算、单次上限、调用数、重试数和时间上限后才运行。
- 已定价 API：调用前按"最大可能费用"预留额度，完成后结算实际费用。
- 未知用量的 Agent/人工模式：显示"未知"，用调用数与时长硬限，不展示虚构费用。
- 缓存键包含输入、教师、提示词、策略、模板与验证器版本。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .db import Database
from .providers.base import COST_FREE, COST_KNOWN, COST_UNKNOWN
from .util import canonical_json, new_id, now_iso, stable_hash


class BudgetExceeded(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


@dataclass
class BudgetState:
    total_cents: float
    spent_cents: float
    reserved_cents: float
    calls_used: int
    max_calls: int
    max_seconds: int
    paid_enabled: bool
    unknown_usage_calls: int = 0
    used_seconds: float = 0.0

    @property
    def available_cents(self) -> float:
        return max(0.0, self.total_cents - self.spent_cents - self.reserved_cents)

    def to_dict(self) -> dict:
        return {
            "total_cents": self.total_cents,
            "spent_cents": self.spent_cents,
            "reserved_cents": self.reserved_cents,
            "available_cents": self.available_cents,
            "calls_used": self.calls_used,
            "max_calls": self.max_calls,
            "max_seconds": self.max_seconds,
            "paid_calls_enabled": self.paid_enabled,
            "unknown_usage_calls": self.unknown_usage_calls,
            "used_seconds": round(self.used_seconds, 1),
        }


def cache_key(
    *,
    provider: str,
    model: str,
    prompt: str,
    system: str | None = None,
    policy_version: str = "",
    prompt_version: str = "",
    template_version: str = "",
    validator_version: str = "",
    project_id: str = "",
    model_digest: str = "",
    extra: dict | None = None,
) -> str:
    """缓存键：任一相关版本变化都会使旧结果失效。

    必须包含 project_id：否则同一个提示词在项目 B 里会命中项目 A 的缓存，
    把 A 的私有内容带进 B 的产物与训练样本。

    必须包含 model_digest：同一个模型 tag 被重新 pull 之后权重就变了
    （DeepInfer 把它叫"固定模型修订"）。只记 tag 的话，
    换了权重还会命中旧缓存 —— 训练样本里混着另一份权重生成的答案，
    而审计时完全看不出来。
    """
    return stable_hash(
        {
            "provider": provider,
            "model": model,
            "model_digest": model_digest,
            "prompt": prompt,
            "system": system or "",
            "policy_version": policy_version,
            "prompt_version": prompt_version,
            "template_version": template_version,
            "validator_version": validator_version,
            "project_id": project_id,
            "extra": extra or {},
        }
    )


def get_state(db: Database, project_id: str, settings: Any) -> BudgetState:
    budget_cfg = settings.budget
    project = db.query_one("SELECT settings_json FROM projects WHERE id = ?", (project_id,)) or {}
    import json

    project_settings = json.loads(project.get("settings_json") or "{}")
    budget_overrides = project_settings.get("budget", {}) if isinstance(project_settings, dict) else {}

    def pick(name: str, default: Any) -> Any:
        return budget_overrides.get(name, getattr(budget_cfg, name, default))

    totals = db.query_one(
        # spent 只统计已结算的调用；pending 行是"预留"，不能同时算进 spent 和 reserved
        "SELECT COALESCE(SUM(CASE WHEN status != 'pending' THEN cost_cents ELSE 0 END),0) AS spent, "
        "COUNT(*) AS calls, "
        "COALESCE(SUM(CASE WHEN cost_status='unknown' THEN 1 ELSE 0 END),0) AS unknown_calls, "
        "COALESCE(SUM(duration_ms),0) AS duration_ms "
        "FROM provider_calls WHERE project_id = ?",
        (project_id,),
    ) or {}
    reserved = db.scalar(
        "SELECT COALESCE(SUM(cost_cents),0) FROM provider_calls WHERE project_id = ? AND status = 'pending'",
        (project_id,),
    ) or 0.0
    return BudgetState(
        total_cents=float(pick("total_cents", 0)),
        spent_cents=float(totals.get("spent") or 0),
        reserved_cents=float(reserved),
        calls_used=int(totals.get("calls") or 0),
        max_calls=int(pick("max_calls", 200)),
        max_seconds=int(pick("max_seconds", 3600)),
        paid_enabled=bool(pick("paid_calls_enabled", False)),
        unknown_usage_calls=int(totals.get("unknown_calls") or 0),
        used_seconds=float(totals.get("duration_ms") or 0) / 1000.0,
    )


def check_and_reserve(
    db: Database,
    project_id: str,
    settings: Any,
    *,
    estimated_cents: float,
    cost_status: str,
) -> dict:
    """调用前检查并预留额度。返回调用审计记录 id。"""
    per_call = float(getattr(settings.budget, "per_call_cents", 0))

    # 检查与预留必须在同一个写事务里：否则并发调用会同时通过检查，
    # 预留总额可以远超预算上限（实测 4×60 分能全部通过，额度超买 140%）。
    with db.transaction():
        state = get_state(db, project_id, settings)
        _check_budget(state, settings, estimated_cents=estimated_cents, cost_status=cost_status, per_call=per_call)
        call = {
            "id": new_id("call"),
            "project_id": project_id,
            "provider": "",
            "model": "",
            "request_id": "",
            "cache_key": "",
            "status": "pending",
            "cost_status": cost_status,
            "cost_cents": float(estimated_cents) if cost_status in {COST_KNOWN, COST_FREE} else 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "duration_ms": 0,
            "request_json": "{}",
            "response_json": "{}",
            "error": "",
            "created_at": now_iso(),
        }
        db.insert("provider_calls", call)
    return call


def _check_budget(
    state: BudgetState,
    settings: Any,
    *,
    estimated_cents: float,
    cost_status: str,
    per_call: float,
) -> None:
    """预算判定（不写库）。"""
    # 时长硬限对所有价格类型都生效（max_seconds 以前只统计不执行）
    if state.max_seconds and state.used_seconds >= state.max_seconds:
        raise BudgetExceeded(
            "已达到累计时长上限",
            {"used_seconds": round(state.used_seconds, 1), "max_seconds": state.max_seconds},
        )

    if cost_status == COST_UNKNOWN:
        # 未知价格：不虚构费用，用调用数与时长硬限
        if state.calls_used >= state.max_calls:
            raise BudgetExceeded(
                "已达到调用次数上限",
                {"calls_used": state.calls_used, "max_calls": state.max_calls},
            )
        return
    if cost_status == COST_FREE:
        estimated_cents = 0.0
    else:
        if not state.paid_enabled:
            raise BudgetExceeded("付费调用未启用（默认关闭）", {"provider_cost": estimated_cents})
        if per_call and estimated_cents > per_call:
            raise BudgetExceeded("超过单次调用上限", {"estimated": estimated_cents, "per_call_cents": per_call})
        if state.total_cents and state.available_cents < estimated_cents:
            raise BudgetExceeded(
                "预算不足",
                {
                    "available_cents": state.available_cents,
                    "estimated_cents": estimated_cents,
                    "total_cents": state.total_cents,
                },
            )


def settle(
    db: Database,
    call: dict,
    *,
    status: str,
    actual_cents: float | None = None,
    cost_status: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    duration_ms: int = 0,
    error: str = "",
    response: dict | None = None,
) -> None:
    values: dict[str, Any] = {
        "status": status,
        "cost_cents": float(actual_cents or 0.0),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_ms": duration_ms,
        "error": error,
        "response_json": canonical_json(response or {}),
    }
    if cost_status:
        values["cost_status"] = cost_status
    db.update("provider_calls", call["id"], values)


def calls_of(db: Database, project_id: str, limit: int = 100) -> list[dict]:
    return db.query_all(
        "SELECT * FROM provider_calls WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
        (project_id, limit),
    )
