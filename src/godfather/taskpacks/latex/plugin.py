"""Cordis 插件：Markdown → LaTeX 专精任务包。"""
from __future__ import annotations

from ...cordis import CordisContext, PluginMeta, TaskPackSpec
from ...training.datasets import SYSTEM_PROMPT as LATEX_SYSTEM_PROMPT
from .pipeline import MODES, parse_markdown_document, run_generation, run_revision

PLUGIN_META = PluginMeta(
    name="taskpack-markdown-to-latex",
    display_name="Markdown → LaTeX 编译精调包",
    version="0.2.0",
    description="Markdown 分块解析、Pandoc 基线、教师/规则合成、XeLaTeX 沙箱编译、四层校验与 PDF 批注协同修订。",
    kind="taskpack",
    inject=("app", "taskpacks", "queue"),
    source="builtin",
    hot_reloadable=True,
)

TASKPACK_MODES = [
    {
        "id": "pandoc_baseline",
        "label": "Pandoc 工具基线",
        "description": "纯确定性工具转换 + XeLaTeX 编译验证，不调用大模型，建立基线对照。",
        "uses_teacher": False,
    },
    {
        "id": "rule_only",
        "label": "内置规则转换",
        "description": "纯内置 Python 规则转换，不依赖 Pandoc 或大模型，最快跑通全链路。",
        "uses_teacher": False,
    },
    {
        "id": "tool_assisted",
        "label": "Pandoc + 教师协同精修",
        "description": "先由 Pandoc 生成结构初稿，再由本地或远程教师模型按偏好优化。",
        "uses_teacher": True,
    },
    {
        "id": "teacher_direct",
        "label": "教师模型端到端直出（SFT 首选）",
        "description": "由教师模型按语义块直接合成 LaTeX，产出最贴近学生模型输入分布的样本。",
        "uses_teacher": True,
    },
]


def _ingest_latex_material(
    ctx,
    *,
    project_id: str,
    artifact_id: str,
    title: str = "",
    document_id: str | None = None,
    dialect: str = "",
) -> dict:
    return parse_markdown_document(
        ctx,
        project_id,
        artifact_id=artifact_id,
        title=title,
        document_id=document_id,
        dialect=dialect,
    )


def _format_latex_training_example(example: dict) -> tuple[str, str, str]:
    from ...training.datasets import _example_payload

    return _example_payload(example)


def _score_latex_prediction(response: str, reference: str, example: dict) -> dict:
    from ...training.evaluate import score_response

    return score_response(response, reference, example)


def apply(ctx: CordisContext) -> None:
    """将 Markdown→LaTeX 任务包与任务处理器挂载到 Cordis 内核。"""
    app_ctx = ctx.inject("app")
    taskpacks = ctx.inject("taskpacks")
    queue = ctx.inject("queue")

    spec = TaskPackSpec(
        id="markdown_to_latex",
        name="Markdown → LaTeX",
        description="面向中文/科技 Markdown 转 LaTeX 的完整数据生产、沙箱编译、PDF 批注与微调闭环。",
        version=PLUGIN_META.version,
        input_formats=(".md", ".markdown", ".txt", ".png", ".jpg", ".jpeg", ".pdf", ".svg"),
        modes=TASKPACK_MODES,
        system_prompt=LATEX_SYSTEM_PROMPT,
        stop_sequences=["\\end{document}"],
        default_job_kind="latex.generate",
        capabilities={
            "has_pdf_preview": True,
            "has_visual_annotation": True,
            "supports_multi_block_revision": True,
            "supports_rule_baseline": True,
            "supported_modes": list(MODES),
            "evaluation_metrics": [
                {"key": "non_empty_rate", "label": "非空率"},
                {"key": "has_latex_rate", "label": "LaTeX 结构率"},
                {"key": "mean_block_coverage", "label": "块覆盖率"},
                {"key": "mean_similarity", "label": "文本/结构相似度"},
                {"key": "cjk_rate", "label": "中文保留率"},
            ],
        },
        ingest_material=_ingest_latex_material,
        run_generation=run_generation,
        run_revision=run_revision,
        format_training_example=_format_latex_training_example,
        score_prediction=_score_latex_prediction,
    )
    ctx.effect(taskpacks.register(spec))

    def _handle_parse(job):
        payload = job.payload
        return parse_markdown_document(
            app_ctx,
            payload["project_id"],
            artifact_id=payload["artifact_id"],
            title=payload.get("title", ""),
            document_id=payload.get("document_id"),
            dialect=payload.get("dialect", ""),
        )

    def _handle_generate(job):
        payload = job.payload
        try:
            return run_generation(
                app_ctx,
                job,
                project_id=payload["project_id"],
                document_id=payload["document_id"],
                mode=payload.get("mode", "pandoc_baseline"),
                provider=payload.get("provider", ""),
                model=payload.get("model", ""),
                chunk_chars=int(payload.get("chunk_chars", 4000)),
                template_name=payload.get("template_name", "preamble_zh_ctex"),
            )
        except Exception:
            try:
                from ...util import now_iso

                app_ctx.db.execute(
                    "UPDATE generations SET status = 'failed', updated_at = ? "
                    "WHERE job_id = ? AND status = 'running'",
                    (now_iso(), job.job_id),
                )
            except Exception:
                pass
            raise

    def _handle_revise(job):
        payload = job.payload
        return run_revision(
            app_ctx,
            job,
            project_id=payload["project_id"],
            document_id=payload["document_id"],
            annotation_id=payload["annotation_id"],
            block_id=payload.get("block_id", ""),
            block_ids=payload.get("block_ids"),
            instruction=payload["instruction"],
            provider=payload.get("provider", ""),
            model=payload.get("model", ""),
            template_name=payload.get("template_name", "preamble_zh_ctex"),
        )

    ctx.effect(queue.register_disposable("latex.parse", _handle_parse))
    ctx.effect(queue.register_disposable("latex.generate", _handle_generate))
    ctx.effect(queue.register_disposable("latex.revise", _handle_revise))
    ctx.emit("taskpack:registered", taskpack_id=spec.id, meta=PLUGIN_META.to_dict())
