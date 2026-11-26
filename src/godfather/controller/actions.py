"""控制器：结构化行动建议 + 确定性执行层。

控制器只输出结构化建议（设计文档 §3.1）：
    summary / evidence_ids / action / arguments / expected_benefit /
    estimated_cost_status / uncertainties / question_if_blocked

执行层负责：
- schema 校验、权限与作用域校验、预算校验、前置条件校验；
- 只执行白名单动作，**绝不执行模型生成的 shell 命令**；
- 区分"规则更新 / 提示词更新 / 数据更新 / 权重更新"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from ..db import Database
from ..util import canonical_json, new_id, now_iso

ACTION_KINDS = (
    "inspect_failures",
    "propose_rule",
    "request_annotation",
    "generate_examples",
    "build_dataset",
    "run_evaluation",
    "stop",
    "clarify",
)

LAYER_OF_ACTION = {
    "inspect_failures": "none",
    "propose_rule": "policy",
    "request_annotation": "annotation",
    "generate_examples": "data",
    "build_dataset": "data",
    "run_evaluation": "evaluation",
    "stop": "none",
    "clarify": "none",
}


class ActionProposal(BaseModel):
    summary: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    action: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    expected_benefit: str = ""
    estimated_cost_status: str = "unknown"
    uncertainties: list[str] = Field(default_factory=list)
    question_if_blocked: str | None = None


@dataclass
class ValidationOutcome:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    normalized: dict | None = None


def validate_action(
    db: Database,
    project_id: str,
    raw: dict,
    *,
    allowed_kinds: tuple[str, ...] = ACTION_KINDS,
) -> ValidationOutcome:
    errors: list[str] = []
    warnings: list[str] = []

    try:
        proposal = ActionProposal(**(raw or {}))
    except ValidationError as exc:
        return ValidationOutcome(ok=False, errors=[f"结构不符合 action schema：{exc.errors()[0]['msg']}"])

    if proposal.action not in allowed_kinds:
        errors.append(f"不允许的动作类型：{proposal.action}（白名单：{', '.join(allowed_kinds)}）")
    if not proposal.summary.strip():
        warnings.append("建议缺少 summary，用户难以审阅")
    if proposal.action in {"propose_rule", "request_annotation", "generate_examples"} and not proposal.evidence_ids:
        warnings.append("该动作通常应引用实际存在的证据 ID")

    # 证据 ID 必须真实存在（只引用输入中实际存在的证据）
    for evidence_id in proposal.evidence_ids:
        exists = db.query_one(
            "SELECT id FROM validation_reports WHERE id = ?"
            " UNION SELECT id FROM annotations WHERE id = ?"
            " UNION SELECT id FROM training_examples WHERE id = ?"
            " UNION SELECT id FROM documents WHERE id = ?"
            " UNION SELECT id FROM generations WHERE id = ?",
            (evidence_id, evidence_id, evidence_id, evidence_id, evidence_id),
        )
        if not exists:
            errors.append(f"证据 ID 不存在：{evidence_id}")

    if proposal.action == "propose_rule":
        arguments = proposal.arguments
        if not arguments.get("content"):
            errors.append("propose_rule 需要 arguments.content（规则内容）")
        if arguments.get("scope") not in (None, "", "project", "document", "block", "block_kind"):
            errors.append("propose_rule 的 scope 非法")
        try:
            priority = int(arguments.get("priority", 50))
            if not 0 <= priority <= 100:
                errors.append("priority 必须在 0-100 之间")
        except (TypeError, ValueError):
            errors.append("priority 必须是整数")

    if proposal.action == "request_annotation":
        if not proposal.arguments.get("document_id"):
            errors.append("request_annotation 需要 arguments.document_id")

    if proposal.action == "generate_examples":
        if not proposal.arguments.get("document_id"):
            errors.append("generate_examples 需要 arguments.document_id")
        mode = proposal.arguments.get("mode", "pandoc_baseline")
        from ..taskpacks.latex.pipeline import MODES

        valid_modes = set(MODES)
        project_row = db.query_one("SELECT taskpack FROM projects WHERE id = ?", (project_id,))
        if project_row and str(project_row.get("taskpack") or "") != "markdown_to_latex":
            valid_modes |= {"teacher_refine"}
        if mode not in valid_modes:
            errors.append(f"未知生成模式：{mode}")

    if proposal.action == "clarify" and not (proposal.question_if_blocked or proposal.arguments.get("question")):
        errors.append("clarify 需要 question_if_blocked 或 arguments.question")

    normalized = proposal.model_dump()
    normalized["layer"] = LAYER_OF_ACTION.get(proposal.action, "unknown")
    return ValidationOutcome(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        normalized=normalized,
    )


# --------------------------------------------------------------------------
# 确定性分析（不需要模型）
# --------------------------------------------------------------------------
def analyze_project(db: Database, project_id: str) -> dict:
    counts = db.query_one(
        "SELECT "
        " (SELECT COUNT(*) FROM training_examples WHERE project_id = ?) AS examples_total,"
        " (SELECT COUNT(*) FROM training_examples WHERE project_id = ? AND status='accepted') AS examples_accepted,"
        " (SELECT COUNT(*) FROM training_examples WHERE project_id = ? AND status='candidate') AS examples_candidate,"
        " (SELECT COUNT(*) FROM annotations WHERE project_id = ? AND status='open') AS annotations_open,"
        " (SELECT COUNT(*) FROM generations WHERE project_id = ?) AS generations,"
        " (SELECT COUNT(*) FROM documents WHERE project_id = ?) AS documents",
        (project_id,) * 6,
    ) or {}

    failures = db.query_all(
        "SELECT vr.layer, vr.verdict, COUNT(*) AS n FROM validation_reports vr "
        "JOIN generations g ON g.id = vr.generation_id WHERE g.project_id = ? "
        "GROUP BY vr.layer, vr.verdict",
        (project_id,),
    )
    by_layer: dict[str, dict[str, int]] = {}
    for row in failures:
        by_layer.setdefault(row["layer"], {})[row["verdict"]] = row["n"]

    open_questions = db.query_all(
        "SELECT id, question, created_at FROM question_cards WHERE project_id = ? AND status='open'",
        (project_id,),
    )
    jobs = db.query_all(
        "SELECT status, COUNT(*) AS n FROM jobs WHERE project_id = ? GROUP BY status", (project_id,)
    )
    return {
        "counts": counts,
        "validation_by_layer": by_layer,
        "open_questions": open_questions,
        "jobs": {row["status"]: row["n"] for row in jobs},
    }


def deterministic_next_action(db: Database, project_id: str) -> dict:
    """在确定性工具足够时优先提出低成本方案（设计文档 §15.1）。"""
    state = analyze_project(db, project_id)
    counts = state["counts"]
    if counts.get("examples_total", 0) == 0:
        return {
            "summary": "还没有任何训练样本：先导入材料并运行一次基线生成",
            "evidence_ids": [],
            "action": "generate_examples",
            "arguments": {"mode": "pandoc_baseline"},
            "expected_benefit": "建立工具基线，确认材料可编译，再决定是否训练",
            "estimated_cost_status": "free",
            "uncertainties": [],
            "question_if_blocked": None,
        }
    if counts.get("examples_accepted", 0) == 0:
        return {
            "summary": "有候选样本但没有已接纳样本：先做内容层检查与人工抽查",
            "evidence_ids": [],
            "action": "inspect_failures",
            "arguments": {},
            "expected_benefit": "定位失败集中的层次，再决定是改策略还是补数据",
            "estimated_cost_status": "free",
            "uncertainties": [],
            "question_if_blocked": None,
        }
    if counts.get("annotations_open", 0) > 0:
        return {
            "summary": f"有 {counts['annotations_open']} 条待处理批注：先转成修订，再考虑训练",
            "evidence_ids": [],
            "action": "request_annotation",
            "arguments": {},
            "expected_benefit": "把人工注意力集中在最值得改进的样例上",
            "estimated_cost_status": "free",
            "uncertainties": [],
            "question_if_blocked": None,
        }
    return {
        "summary": "样本已就绪：冻结一个数据快照，作为下一轮训练的输入",
        "evidence_ids": [],
        "action": "build_dataset",
        "arguments": {},
        "expected_benefit": "获得可复现的数据版本，后续训练可追溯",
        "estimated_cost_status": "free",
        "uncertainties": [],
        "question_if_blocked": None,
    }


# --------------------------------------------------------------------------
# 执行层
# --------------------------------------------------------------------------
def execute_action(ctx: Any, project_id: str, action: dict) -> dict:
    """执行白名单动作。返回 {status, result, layer}。"""
    kind = action.get("action")
    arguments = action.get("arguments") or {}
    layer = action.get("layer") or LAYER_OF_ACTION.get(kind, "unknown")

    if kind == "inspect_failures":
        return {"status": "succeeded", "layer": "none", "result": analyze_project(ctx.db, project_id)}

    if kind == "propose_rule":
        from ..policy import engine as policy_engine

        policy = ctx.policy_for(project_id)
        pref_id = arguments.get("pref_id") or f"rule-{len(policy.preferences) + 1:03d}"
        draft = policy_engine.Preference(
            pref_id=pref_id,
            content=arguments.get("content", ""),
            scope=arguments.get("scope", "project"),
            scope_value=arguments.get("scope_value", ""),
            conditions=arguments.get("conditions", {}),
            priority=int(arguments.get("priority", 50)),
            confidence=float(arguments.get("confidence", 0.3)),
            status="draft",
            evidence=list(action.get("evidence_ids") or []),
            rule=arguments.get("rule", {}),
        )
        policy.preferences.append(draft)
        result = policy_engine.save_policy(ctx.db, project_id, policy, status="draft", notes="控制器建议的草案（未确认）")
        return {
            "status": "succeeded",
            "layer": layer,
            "result": {"policy": result, "note": "规则已保存为草案，需用户确认后才会生效"},
        }

    if kind == "request_annotation":
        document_id = arguments.get("document_id")
        card_id = new_id("q")
        ctx.db.insert(
            "question_cards",
            {
                "id": card_id,
                "project_id": project_id,
                "job_id": arguments.get("job_id"),
                "question": action.get("question_if_blocked") or arguments.get("question") or "请确认这一批样例是否需要标注",
                "context_json": canonical_json(arguments.get("context", {})),
                "options_json": canonical_json(arguments.get("options", [])),
                "status": "open",
                "answer_json": "",
                "created_at": now_iso(),
                "answered_at": None,
            },
        )
        return {"status": "succeeded", "layer": layer, "result": {"question_card_id": card_id, "document_id": document_id}}

    if kind == "generate_examples":
        document_id = arguments.get("document_id")
        mode = arguments.get("mode", "pandoc_baseline")
        # 文档必须属于本项目：否则可以用 A 项目的会话对 B 的文档发起生成
        if document_id:
            doc_row = ctx.db.query_one(
                "SELECT project_id FROM documents WHERE id = ?", (document_id,)
            )
            if not doc_row or doc_row.get("project_id") != project_id:
                return {
                    "status": "failed",
                    "layer": layer,
                    "result": {"note": "文档不属于当前项目，已拒绝"},
                }
        project_row = ctx.project(project_id) or {}
        task_type = str(project_row.get("taskpack") or "markdown_to_latex")
        taskpacks = getattr(ctx, "taskpacks", None)
        job_type = "latex.generate"
        if taskpacks is not None and taskpacks.has(task_type):
            job_type = taskpacks.get(task_type).default_job_kind or job_type
        provider = arguments.get("provider") or getattr(ctx.settings.providers, "default_teacher_provider", "ollama") or "ollama"
        job = ctx.queue.submit(
            job_type,
            {
                "project_id": project_id,
                "document_id": document_id,
                "mode": mode,
                "provider": provider,
                "model": arguments.get("model", ""),
            },
            project_id=project_id,
            idempotency_key=f"{job_type}:{document_id}:{mode}:{now_iso()[:16]}",
        )
        return {"status": "succeeded", "layer": layer, "result": {"job_id": job["id"], "status": job["status"]}}

    if kind == "build_dataset":
        from ..datasets import build_snapshot

        snapshot = build_snapshot(ctx.db, project_id, seed=arguments.get("seed", "godfather-1"))
        return {
            "status": "succeeded",
            "layer": layer,
            "result": {"snapshot_id": snapshot.id, "version": snapshot.version, "counts": snapshot.counts, "path": snapshot.path},
        }

    if kind == "run_evaluation":
        return {
            "status": "succeeded",
            "layer": layer,
            "result": {"note": "评测入口已预留（M1 实现）；当前仅返回基线统计", "baseline": analyze_project(ctx.db, project_id)},
        }

    if kind == "clarify":
        card_id = new_id("q")
        ctx.db.insert(
            "question_cards",
            {
                "id": card_id,
                "project_id": project_id,
                "job_id": None,
                "question": action.get("question_if_blocked") or arguments.get("question", ""),
                "context_json": canonical_json(arguments),
                "options_json": canonical_json(arguments.get("options", [])),
                "status": "open",
                "answer_json": "",
                "created_at": now_iso(),
                "answered_at": None,
            },
        )
        return {"status": "succeeded", "layer": "none", "result": {"question_card_id": card_id}}

    if kind == "stop":
        return {"status": "succeeded", "layer": "none", "result": {"note": "已停止本轮；未做任何改动"}}

    return {"status": "failed", "layer": layer, "result": {"error": f"未实现的动作：{kind}"}}


def record_proposal(db: Database, project_id: str, action: dict, validation: ValidationOutcome) -> str:
    action_id = new_id("act")
    db.insert(
        "controller_actions",
        {
            "id": action_id,
            "project_id": project_id,
            "kind": action.get("action", ""),
            "summary": action.get("summary", ""),
            "arguments_json": canonical_json(action.get("arguments") or {}),
            "status": "proposed" if validation.ok else "rejected",
            "validation_json": canonical_json({"errors": validation.errors, "warnings": validation.warnings}),
            "result_json": "{}",
            "created_at": now_iso(),
            "executed_at": None,
        },
    )
    return action_id
