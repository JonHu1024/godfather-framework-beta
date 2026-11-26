"""Cordis 插件：通用指令/对话微调任务包（General SFT）。"""
from __future__ import annotations

import difflib
import json

from ...cordis import CordisContext, PluginMeta, TaskPackSpec
from ...providers.base import strip_thinking
from .pipeline import DEFAULT_SFT_SYSTEM_PROMPT, parse_sft_document, run_sft_generation

PLUGIN_META = PluginMeta(
    name="taskpack-general-sft",
    display_name="通用指令微调任务包（General SFT）",
    version="0.2.0",
    description="支持 JSONL / JSON / CSV / Markdown 语料导入、教师合成/润色、相似度与结构化 JSON 评测，演示 Cordis 热插拔扩展架构。",
    kind="taskpack",
    inject=("app", "taskpacks", "queue"),
    source="builtin",
    hot_reloadable=True,
)

GENERAL_SFT_MODES = [
    {
        "id": "rule_only",
        "label": "直接入库已标语料",
        "description": "直接导入 JSONL / CSV 中自带的监督答案作为候选样本，不调用大模型。",
        "uses_teacher": False,
    },
    {
        "id": "tool_assisted",
        "label": "教师检查并润色",
        "description": "结合项目偏好对现有回答进行纠错与润色。",
        "uses_teacher": True,
    },
    {
        "id": "teacher_direct",
        "label": "教师合成回答",
        "description": "根据输入指令与策略规则，由教师模型直接生成训练目标。",
        "uses_teacher": True,
    },
]


def _format_sft_example(example: dict) -> tuple[str, str, str]:
    inp = example.get("input_payload") or {}
    tgt = example.get("target_payload") or {}
    system = str(inp.get("system") or DEFAULT_SFT_SYSTEM_PROMPT)
    user = str(inp.get("prompt") or inp.get("markdown") or "")
    assistant = str(tgt.get("text") or tgt.get("output") or tgt.get("tex") or "")
    return system, user, assistant


def _score_sft_prediction(response: str, reference: str, example: dict) -> dict:
    cleaned, _ = strip_thinking(response or "")
    ref = str(reference or "").strip()
    non_empty = bool(cleaned.strip())
    similarity = 0.0
    if non_empty and ref:
        similarity = difflib.SequenceMatcher(None, cleaned.strip(), ref).ratio()
    exact_match = non_empty and ref != "" and cleaned.strip() == ref
    json_valid = True
    if ref.startswith("{") or ref.startswith("["):
        try:
            json.loads(cleaned)
            json_valid = True
        except Exception:  # noqa: BLE001
            json_valid = False
    return {
        "non_empty": non_empty,
        "has_latex": json_valid,
        "block_coverage": 1.0 if non_empty else 0.0,
        "similarity": round(similarity, 4),
        "cjk_preserved": True,
        "exact_match": exact_match,
        "json_valid": json_valid,
        "output_chars": len(cleaned),
    }


def apply(ctx: CordisContext) -> None:
    app_ctx = ctx.inject("app")
    taskpacks = ctx.inject("taskpacks")
    queue = ctx.inject("queue")

    spec = TaskPackSpec(
        id="general_sft",
        name="通用指令微调（General SFT）",
        description="适合问答、摘要、代码生成、结构化抽取等任意文本 SFT 微调需求。",
        version=PLUGIN_META.version,
        input_formats=(".jsonl", ".json", ".csv", ".tsv", ".md", ".txt"),
        modes=GENERAL_SFT_MODES,
        system_prompt=DEFAULT_SFT_SYSTEM_PROMPT,
        stop_sequences=[],
        default_job_kind="sft.generate",
        capabilities={
            "has_pdf_preview": False,
            "has_visual_annotation": False,
            "supports_multi_block_revision": False,
            "supports_rule_baseline": True,
            "supported_modes": [m["id"] for m in GENERAL_SFT_MODES],
            "evaluation_metrics": [
                {"key": "non_empty_rate", "label": "非空率"},
                {"key": "has_latex_rate", "label": "格式合规率"},
                {"key": "mean_similarity", "label": "文本相似度"},
            ],
        },
        ingest_material=lambda a_ctx, **kw: parse_sft_document(a_ctx, **kw),
        run_generation=run_sft_generation,
        format_training_example=_format_sft_example,
        score_prediction=_score_sft_prediction,
    )
    ctx.effect(taskpacks.register(spec))

    ctx.effect(
        queue.register_disposable(
            "sft.parse",
            lambda job: parse_sft_document(
                app_ctx,
                job.payload["project_id"],
                artifact_id=job.payload["artifact_id"],
                title=job.payload.get("title", ""),
                document_id=job.payload.get("document_id"),
                dialect=job.payload.get("dialect", ""),
            ),
        )
    )
    ctx.effect(
        queue.register_disposable(
            "sft.generate",
            lambda job: run_sft_generation(
                app_ctx,
                job,
                project_id=job.payload["project_id"],
                document_id=job.payload["document_id"],
                mode=job.payload.get("mode", "teacher_direct"),
                provider=job.payload.get("provider", ""),
                model=job.payload.get("model", ""),
            ),
        )
    )
    ctx.emit("taskpack:registered", taskpack_id=spec.id, meta=PLUGIN_META.to_dict())
