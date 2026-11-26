"""Markdown→LaTeX 任务包的端到端流程（设计文档 §5.3 / §5.4）。

模式：
- `pandoc_baseline`  纯工具基线：Pandoc 转换全文（不含教师），用于对照。
- `rule_only`        纯规则转换：内置确定性转换器，不调用教师。
- `tool_assisted`    工具辅助：Pandoc 先转，再由教师做语义/样式/局部修复。
- `teacher_direct`   直接生成：由教师按块直接生成 TeX（真正训练转换模型）。

四种模式分别存数据、分别评测：不会把工具完成的收益算到模型头上。
"""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...context import AppContext
from ...datasets import db_path_parent
from ...jobs import JobContext
from ...policy import engine as policy_engine
from ...providers.base import ProviderRequest, strip_thinking
from ...util import canonical_json, new_id, now_iso
from . import compiler as latex_compiler
from . import pandoc_base, validators
from .assembler import assemble_document, build_preamble, escape_latex, render_block
from .mdparse import Block, chunk_blocks, parse_markdown

MODES = ("pandoc_baseline", "rule_only", "tool_assisted", "teacher_direct")


@dataclass
class GenerationOutcome:
    generation_id: str
    tex_artifact_id: str = ""
    pdf_artifact_id: str = ""
    log_artifact_id: str = ""
    validation_reports: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def sanitize_teacher_tex(text: str) -> str:
    """清理教师返回的 LaTeX 片段。

    - 剥离推理模型 <think>...</think> 思考段（BUG-08）；
    - 去掉 Markdown 代码围栏；
    - 去掉行内代码的反引号（教师很容易把 Markdown 习惯带进 LaTeX）；
    - 检查行内数学是否成对，未成对时补齐，避免整篇编译失败。
    """
    if not text:
        return ""
    cleaned, _thoughts = strip_thinking(text)
    cleaned = cleaned.strip()
    cleaned = re.sub(r"^```(?:latex|tex)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    cleaned = cleaned.replace("`", "")
    if cleaned.count("$") % 2 == 1:
        cleaned += "$"
    return cleaned


def _repair_json(text: str) -> str:
    """修复模型常见的非法转义（例如 "\\section" 里的 \\s 不是合法 JSON 转义）。"""
    return re.sub(r"\\(?![\"\\/bfnrtu])", r"\\\\", text)


def parse_json_object(text: str) -> dict:
    """从模型输出中提取 JSON 对象（剥离 <think>、容忍 ```json 包裹与非法转义）。"""
    if not text:
        return {}
    cleaned, _thoughts = strip_thinking(text)
    cleaned = cleaned.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    for candidate in (cleaned,):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        raw = match.group(0)
        for candidate in (raw, _repair_json(raw)):
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue
    return {}


# --------------------------------------------------------------------------
# 1. 解析文档
# --------------------------------------------------------------------------
def parse_markdown_document(
    ctx: AppContext,
    project_id: str,
    *,
    artifact_id: str,
    title: str = "",
    document_id: str | None = None,
    dialect: str = "",
) -> dict:
    """把导入的 Markdown 解析为结构块，落库并记录版本。

    ``dialect`` 来自导入时用户声明的 Markdown 方言：
    以前它只写进数据库、没有任何读取方 ——
    界面上选了方言，解析行为却一字未变。现在它真的影响解析。
    """
    source_text = ctx.store.read_text(artifact_id)
    parsed = parse_markdown(source_text, title=title, dialect=dialect or "pandoc-markdown")

    if not document_id:
        document_id = new_id("doc")
        ctx.db.insert(
            "documents",
            {
                "id": document_id,
                "project_id": project_id,
                "title": parsed.title,
                "source_document_id": None,
                "source_revision": ctx.store.get(artifact_id)["revision"],
                "status": "parsed",
                "policy_version": ctx.policy_for(project_id).version,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            },
        )
    else:
        ctx.db.update(
            "documents",
            document_id,
            {
                "title": parsed.title,
                "source_revision": ctx.store.get(artifact_id)["revision"],
                "updated_at": now_iso(),
            },
        )

    revision = (ctx.db.scalar("SELECT COUNT(*) AS c FROM document_revisions WHERE document_id = ?", (document_id,)) or 0) + 1
    revision_id = new_id("rev")
    ctx.db.insert(
        "document_revisions",
        {
            "id": revision_id,
            "document_id": document_id,
            "revision": revision,
            "source_revision": ctx.store.get(artifact_id)["revision"],
            "blocks_json": canonical_json([b.to_dict() for b in parsed.blocks]),
            "context_json": canonical_json(parsed.context),
            "created_at": now_iso(),
        },
    )
    ctx.db.update(
        "documents",
        document_id,
        {"status": "parsed", "updated_at": now_iso()},
    )
    return {
        "document_id": document_id,
        "revision": revision,
        "revision_id": revision_id,
        "title": parsed.title,
        "block_count": len(parsed.blocks),
        "context": parsed.context,
    }


def load_blocks(ctx: AppContext, document_id: str) -> tuple[list[Block], dict, int]:
    row = ctx.db.query_one(
        "SELECT * FROM document_revisions WHERE document_id = ? ORDER BY revision DESC LIMIT 1",
        (document_id,),
    )
    if not row:
        return [], {}, 0
    raw_blocks = json.loads(row["blocks_json"] or "[]")
    blocks = []
    for b in raw_blocks:
        meta = dict(b.get("meta", {}))
        if b.get("revised_tex"):
            meta["revised_tex"] = b["revised_tex"]
        blocks.append(
            Block(
                block_id=b["block_id"],
                kind=b["kind"],
                text=b.get("text", ""),
                level=b.get("level", 0),
                path=b.get("path", []),
                line_start=b.get("line_start", 0),
                line_end=b.get("line_end", 0),
                meta=meta,
            )
        )
    context = json.loads(row["context_json"] or "{}")
    return blocks, context, row["revision"]


# --------------------------------------------------------------------------
# 2. 生成
# --------------------------------------------------------------------------
IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def extract_asset_references(text: str) -> list[str]:
    """取出 Markdown 中引用的图片路径（保持原样相对路径）。"""
    seen: list[str] = []
    for match in IMAGE_REF_RE.finditer(text or ""):
        path = match.group(1).strip()
        if path and not path.startswith(("http://", "https://", "data:")) and path not in seen:
            seen.append(path)
    return seen


def _rule_convert_chunk(blocks: list[Block]) -> str:
    """用确定性规则把一组源块转成 LaTeX。

    教师失败且没有工具兜底时的**唯一诚实选项**：
    规则转换至少是真正的 LaTeX（可编译、可追溯），
    而原始 Markdown 不是 —— 把它当训练目标等于教模型输出 Markdown。
    """
    from .assembler import render_block

    parts: list[str] = []
    for block in blocks:
        try:
            tex, _warnings, _assets = render_block(block)
        except Exception:  # noqa: BLE001
            tex = ""
        if tex:
            parts.append(tex)
    return "\n".join(parts).strip()


def _template_overrides_dir(settings: Any) -> str | None:
    """模板覆盖目录（存在才用）。

    文档承诺"可在 config/templates/ 下放同名文件覆盖内置模板"，
    以前 load_template 的 overrides_dir 从来没人传 → 那条承诺是空的。
    """
    path = getattr(settings, "templates_dir", None)
    if path is None:
        return None
    try:
        path = Path(path)
    except TypeError:
        return None
    if path.is_dir():
        return str(path)
    return None


def source_text_for_document(ctx: AppContext, document_id: str) -> str:
    """取回文档的原始 Markdown（工具基线应当转换原文，而不是重新拼接块）。"""
    document = ctx.db.query_one("SELECT * FROM documents WHERE id = ?", (document_id,)) or {}
    source_id = document.get("source_document_id")
    if source_id:
        source = ctx.db.query_one("SELECT * FROM source_documents WHERE id = ?", (source_id,))
        if source and source.get("artifact_id"):
            try:
                return ctx.store.read_text(source["artifact_id"])
            except KeyError:
                pass
    # 退化：按块重建（公式补回 $$ 定界符）
    blocks, _context, _revision = load_blocks(ctx, document_id)
    parts = []
    for block in blocks:
        if block.kind == "math":
            parts.append(f"$$\n{block.text}\n$$")
        elif block.kind == "code":
            parts.append(f"```\n{block.text}\n```")
        else:
            parts.append(block.text)
    return "\n\n".join(parts)


def _load_reference_snippets(ctx: AppContext, project_id: str, limit: int = 2) -> list[dict]:
    """读取参考案例（md/tex 对，严禁混入 holdout 样本）供教师参考（BUG-10）。"""
    try:
        rows = ctx.db.query_all(
            "SELECT id, title, md_artifact_id, tex_artifact_id FROM reference_examples "
            "WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
            (project_id, max(1, limit * 3)),
        )
    except Exception:  # noqa: BLE001
        return []
    snippets: list[dict] = []
    for row in rows:
        md = ""
        tex = ""
        if row.get("md_artifact_id"):
            try:
                md = ctx.store.read_text(row["md_artifact_id"]).strip()[:800]
            except Exception:  # noqa: BLE001
                md = ""
        if row.get("tex_artifact_id"):
            try:
                tex = ctx.store.read_text(row["tex_artifact_id"]).strip()[:1200]
            except Exception:  # noqa: BLE001
                tex = ""
        if md or tex:
            snippets.append({"id": row["id"], "title": row.get("title") or "", "markdown": md, "tex": tex})
        if len(snippets) >= limit:
            break
    return snippets


def run_generation(
    ctx: AppContext,
    job: JobContext,
    *,
    project_id: str,
    document_id: str,
    mode: str = "pandoc_baseline",
    provider: str = "",
    model: str = "",
    chunk_chars: int = 4000,
    max_chunks: int = 200,
    template_name: str = "preamble_zh_ctex",
    run_id: str | None = None,
) -> dict:
    if mode not in MODES:
        raise ValueError(f"未知模式：{mode}")
    started = time.time()
    blocks, doc_context, revision = load_blocks(ctx, document_id)
    if not blocks:
        raise ValueError("文档没有结构块，无法生成")

    effective_provider = (
        provider
        or getattr(ctx.settings.providers, "default_teacher_provider", "ollama")
        or "ollama"
    )
    policy = ctx.policy_for(project_id)
    preferences = policy_engine.applicable_preferences(policy, scope="project")
    conflicts = policy_engine.detect_conflicts(preferences)

    # 重试时复用同一个 generation 行：否则每次重试都多出一行，
    # 而且第一次那行永远停在 running（界面里看起来像"还在跑"）。
    existing = ctx.db.query_one(
        "SELECT * FROM generations WHERE job_id = ? ORDER BY created_at ASC LIMIT 1",
        (job.job_id,),
    )
    generation_id = existing["id"] if existing else new_id("gen")
    if existing:
        ctx.db.update(
            "generations",
            generation_id,
            {"status": "running", "updated_at": now_iso()},
        )
    else:
        ctx.db.insert(
            "generations",
            {
                "id": generation_id,
                "project_id": project_id,
                "document_id": document_id,
                "mode": mode,
                "status": "running",
                "provider": effective_provider if mode in {"tool_assisted", "teacher_direct"} else "",
                "model": model,
                "policy_version": policy.version,
                "prompt_version": "prompt-001",
                "job_id": job.job_id,
                "tex_artifact_id": "",
                "pdf_artifact_id": "",
                "stats_json": "{}",
                "created_at": now_iso(),
                "updated_at": now_iso(),
            },
        )

    job.emit_event(
        stage="generate",
        message=f"开始生成（模式 {mode}，{len(blocks)} 个结构块）",
        payload={"mode": mode, "generation_id": generation_id, "policy_version": policy.version},
    )

    units: list[Block]
    teacher_outputs: dict[str, str] = {}
    warnings: list[str] = []
    teacher_stats = {
        "calls": 0,
        "failed": 0,
        "fallback_blocks": 0,
        "tokens": 0,
        # 教师**权重摘要**（digest）：把产物绑定到确切的那份权重上。
        # 以前写的是 "unknown" —— 等于没记录，样本来源无法追溯。
        "model_digest": "",
        "model": "",
        "seed": int(getattr(ctx.settings.providers.ollama, "seed", 42)),
    }

    if mode == "pandoc_baseline":
        source_text = source_text_for_document(ctx, document_id)
        referenced_assets = extract_asset_references(source_text)
        result = pandoc_base.convert_fragment(source_text, ctx.settings)
        if not result.ok:
            warnings.append(f"Pandoc 转换失败：{result.error}；回退到规则转换")
            units, teacher_outputs = blocks, {}
        else:
            body = result.tex
            assembled_tex = wrap_body(body, ctx.settings, template_name=template_name, title=doc_context.get("title", ""))
            units = []
            teacher_outputs = {}
            return _finalize(
                ctx,
                job,
                project_id=project_id,
                document_id=document_id,
                generation_id=generation_id,
                mode=mode,
                assembled_tex=assembled_tex,
                # pandoc 基线也要有行映射：批注定位需要它，
                # 否则这一模式的产物没有定位依据。
                line_map={
                    block.block_id: {
                        "start": block.line_start,
                        "end": block.line_end,
                        "kind": block.kind,
                    }
                    for block in blocks
                },
                blocks=blocks,
                doc_context=doc_context,
                policy=policy,
                preferences=preferences,
                conflicts=conflicts,
                warnings=warnings,
                teacher_stats=teacher_stats,
                started=started,
                run_id=run_id,
                assets=referenced_assets,
            )

    if mode == "rule_only":
        units, teacher_outputs = blocks, {}
    else:
        chunks = chunk_blocks(blocks, max_chars=chunk_chars)
        if len(chunks) > max_chunks:
            warnings.append(
                f"正文过长：只处理了前 {max_chunks} 段（共 {len(chunks)} 段），其余内容未纳入本次生成"
            )
            chunks = chunks[:max_chunks]
        units = []
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            job.emit_event(
                stage="generate",
                done=index,
                total=total,
                message=f"生成块 {index}/{total}（{chunk['chunk_id']}）",
            )
            chunk_blocks_list = chunk["blocks"]
            baseline_tex = ""
            if mode == "tool_assisted":
                md_text = "\n\n".join(b["text"] for b in chunk_blocks_list)
                pandoc_result = pandoc_base.convert_fragment(md_text, ctx.settings)
                if pandoc_result.ok:
                    baseline_tex = pandoc_result.tex
                else:
                    warnings.append(f"{chunk['chunk_id']}: Pandoc 失败（{pandoc_result.error[:120]}）")

            ref_snippets = _load_reference_snippets(ctx, project_id, limit=2)
            active_provider = provider or getattr(ctx.settings.providers, "default_teacher_provider", "ollama") or "ollama"
            prompt = policy_engine.build_teacher_prompt(
                policy,
                block={
                    "chunk_id": chunk["chunk_id"],
                    "block_ids": chunk["block_ids"],
                    "kinds": [b["kind"] for b in chunk_blocks_list],
                    "markdown": "\n\n".join(b["text"] for b in chunk_blocks_list),
                    "heading_path": chunk["context"]["heading_path"],
                },
                context={
                    "title": doc_context.get("title", ""),
                    "outline": doc_context.get("heading_outline", [])[:40],
                    "references": doc_context.get("references", []),
                    "citations": doc_context.get("citations", []),
                    "labels": doc_context.get("labels", []),
                    "neighbors": chunk["context"],
                },
                preferences=preferences,
                reference_snippets=ref_snippets,
                validation_errors=[],
                extra_instructions=(
                    "Pandoc 已给出初稿，请修正语义环境、样式与明显错误；不要凭空添加内容。"
                    if mode == "tool_assisted"
                    else "请直接输出该块的 LaTeX。"
                ),
            )
            result = ctx.teacher.call(
                project_id,
                active_provider,
                ProviderRequest(
                    prompt=prompt,
                    json_mode=True,
                    temperature=0.1,
                    max_tokens=3000,
                    # 固定种子 → 教师产出可复现
                    seed=int(getattr(ctx.settings.providers.ollama, "seed", 42)),
                ),
                model=model,
                policy_version=policy.version,
                prompt_version="prompt-001",
                run_id=run_id,
            )
            if result.error_kind == "budget":
                # 预算不足是硬约束：任务进入可恢复的 paused_budget，
                # 不能悄悄降级成规则转换（那会让用户以为"跑完了"）。
                from ...jobs import STATUS_PAUSED_BUDGET, JobPaused

                raise JobPaused(
                    STATUS_PAUSED_BUDGET,
                    f"预算不足，教师调用未执行：{result.error}",
                )
            teacher_stats["calls"] += 1
            teacher_stats["tokens"] += result.usage.input_tokens + result.usage.output_tokens
            teacher_stats["model"] = result.model or teacher_stats.get("model") or model
            digest = str((result.raw or {}).get("model_digest") or "")
            if digest:
                teacher_stats["model_digest"] = digest
            chunk_tex = ""
            if result.ok:
                payload = parse_json_object(result.content)
                chunk_tex = sanitize_teacher_tex(str(payload.get("tex") or ""))
                for warning in payload.get("warnings") or []:
                    warnings.append(f"{chunk['chunk_id']}: 教师警告：{warning}")
                teacher_questions = [str(q).strip() for q in (payload.get("questions") or []) if str(q).strip()]
                for question in teacher_questions:
                    warnings.append(f"{chunk['chunk_id']}: 教师提问：{question}")
                # 策略要求"教师提问必须先由人回答"时，任务进入可恢复的 waiting_user：
                # 问题会进收件箱，用户回答后再恢复任务。
                if teacher_questions and _wait_on_teacher_questions(policy):
                    for question in teacher_questions:
                        ctx.db.insert(
                            "question_cards",
                            {
                                "id": new_id("q"),
                                "project_id": project_id,
                                "job_id": job.job_id,
                                "question": question,
                                "context_json": canonical_json(
                                    {"block_id": chunk["chunk_id"], "source": "teacher"}
                                ),
                                "options_json": canonical_json([]),
                                "status": "open",
                                "answer_json": "",
                                "created_at": now_iso(),
                                "answered_at": None,
                            },
                        )
                    from ...jobs import STATUS_WAITING_USER, JobPaused

                    raise JobPaused(
                        STATUS_WAITING_USER,
                        "教师提出了需要确认的问题（已进「问题收件箱」），回答后可以恢复任务",
                    )
            else:
                teacher_stats["failed"] += 1
                warnings.append(f"{chunk['chunk_id']}: 教师调用失败（{result.error}）")

            if not chunk_tex and baseline_tex:
                chunk_tex = baseline_tex
                teacher_stats["fallback_blocks"] += 1
                warnings.append(f"{chunk['chunk_id']}: 使用 Pandoc 结果兜底")
            elif not chunk_tex:
                # 教师没产出、也没有 Pandoc 兜底：
                # **不能**把原始 Markdown 当作 LaTeX 目标 ——
                # 那样等于教模型"输出 Markdown"，直接污染 SFT 数据。
                # 这里改用规则转换（确定性、可复现），并计入兜底，
                # 让 label_origin 如实反映"这条不是教师产物"。
                chunk_tex = _rule_convert_chunk(chunk_blocks_list)
                if chunk_tex:
                    teacher_stats["fallback_blocks"] += 1
                    warnings.append(f"{chunk['chunk_id']}: 教师未产出，已用规则转换兜底")

            unit = Block(
                block_id=chunk["chunk_id"],
                kind="teacher_chunk",
                text=chunk_tex or "\n\n".join(b["text"] for b in chunk_blocks_list),
                path=chunk["context"]["heading_path"],
                meta={"block_ids": chunk["block_ids"]},
            )
            units.append(unit)
            teacher_outputs[chunk["chunk_id"]] = chunk_tex

    assembled = assemble_document(
        units,
        settings=ctx.settings,
        title=doc_context.get("title", ""),
        teacher_outputs=teacher_outputs,
        template_name=template_name,
        overrides_dir=_template_overrides_dir(ctx.settings),
    )
    warnings.extend(assembled.warnings)

    # 兜底：教师输出编译失败时，用规则转换的结果重编一次
    fallback = assemble_document(
        blocks,
        settings=ctx.settings,
        title=doc_context.get("title", ""),
        template_name=template_name,
        overrides_dir=_template_overrides_dir(ctx.settings),
    )

    return _finalize(
        ctx,
        job,
        project_id=project_id,
        document_id=document_id,
        generation_id=generation_id,
        mode=mode,
        assembled_tex=assembled.tex,
        line_map=assembled.line_map,
        blocks=blocks,
        doc_context=doc_context,
        policy=policy,
        preferences=preferences,
        conflicts=conflicts,
        warnings=warnings,
        teacher_stats=teacher_stats,
        started=started,
        run_id=run_id,
        assets=assembled.assets or fallback.assets,
        fallback_tex=fallback.tex,
        # 回退产物是**规则版**文档，它的行映射与教师版完全不同
        # （连 block_id 都不一样：教师版是 chunk-xxx，规则版是 block-xxxx）。
        # 存教师版的 line_map 等于给出错误的批注定位依据。
        fallback_line_map=fallback.line_map,
    )


def wrap_body(
    body: str,
    settings: Any,
    *,
    template_name: str = "preamble_zh_ctex",
    title: str = "",
) -> str:
    preamble = build_preamble(
        settings, template_name=template_name, overrides_dir=_template_overrides_dir(settings)
    )
    title_block = f"\\begin{{document}}\n\\begin{{center}}\n{{\\LARGE\\bfseries {escape_latex(title)}}}\\par\n\\end{{center}}\n" if title else "\\begin{document}\n"
    return f"{preamble}\n{title_block}\n{body}\n\\end{{document}}\n"


def _finalize(
    ctx: AppContext,
    job: JobContext,
    *,
    project_id: str,
    document_id: str,
    generation_id: str,
    mode: str,
    assembled_tex: str,
    line_map: dict,
    fallback_line_map: dict | None = None,
    blocks: list[Block],
    doc_context: dict,
    policy: Any,
    preferences: list,
    conflicts: list,
    warnings: list[str],
    teacher_stats: dict,
    started: float,
    run_id: str | None = None,
    assets: list[str] | None = None,
    label_origin: str | None = None,
    annotation_ids: list[str] | None = None,
    example_extra: dict[str, Any] | None = None,
    fallback_tex: str | None = None,
) -> dict:
    tex_artifact = ctx.store.put_bytes(
        project_id,
        "tex",
        assembled_tex.encode("utf-8"),
        media_type="application/x-tex",
        document_id=document_id,
        meta={"mode": mode, "generation_id": generation_id, "line_map": line_map},
    )

    # 资源：把项目里已有的图片资产按“引用路径”组织，保证 LaTeX 能按相对路径找到
    asset_payloads: dict[str, bytes] = {}
    available_assets: set[str] = set()
    referenced = list(assets or [])
    for artifact in ctx.store.list_by_project(project_id, "asset"):
        meta = json.loads(artifact.get("meta_json") or "{}")
        basename = meta.get("filename") or Path(artifact["path"]).name
        available_assets.add(basename)
        payload = Path(artifact["path"]).read_bytes()
        matched = False
        for reference in referenced:
            if Path(reference).name == basename:
                asset_payloads[reference] = payload
                matched = True
        if not matched and not referenced:
            asset_payloads[basename] = payload

    job.emit_event(stage="compile", message="开始隔离编译")
    compile_result = latex_compiler.compile_tex(
        assembled_tex,
        settings=ctx.settings,
        work_root=ctx.settings.work_dir,
        job_id=generation_id,
        assets=asset_payloads,
    )
    fallback_used = False
    if not compile_result.ok and fallback_tex and fallback_tex != assembled_tex:
        # 教师输出导致编译失败：回退到规则转换重新编译一次（有限次局部修复的上限）
        fallback_used = True
        warnings.append("教师输出无法编译，已回退到规则转换结果")
        job.emit_event(
            stage="compile",
            message="编译失败，回退到规则转换后重新编译",
            level="warning",
        )
        compile_result = latex_compiler.compile_tex(
            fallback_tex,
            settings=ctx.settings,
            work_root=ctx.settings.work_dir,
            job_id=f"{generation_id}-fallback",
            assets=asset_payloads,
        )
        if compile_result.ok:
            assembled_tex = fallback_tex
            # 回退文档的行映射要换成**规则版**的：
            # 教师版的 line_map 描述的是教师版文档的行号与 block_id
            # （chunk-xxx），套到回退产物（block-xxxx）上全是错的。
            if fallback_line_map:
                line_map = fallback_line_map
            tex_artifact = ctx.store.put_bytes(
                project_id,
                "tex",
                assembled_tex.encode("utf-8"),
                media_type="application/x-tex",
                document_id=document_id,
                meta={
                    "mode": mode,
                    "generation_id": generation_id,
                    "fallback": "rules",
                    # 行映射必须一起落库：回退产物是**新的 digest**，
                    # 不会与教师版产物合并 meta → 不带 line_map 的话，
                    # 这次生成的批注定位依据就消失了。
                    "line_map": line_map,
                },
            )
    compile_info = compile_result.to_dict()
    compile_info["fallback_to_rules"] = fallback_used
    job.emit_event(
        stage="compile",
        message=(
            f"编译{'成功' if compile_result.ok else '失败'}（{compile_result.duration_ms} ms）"
            + (f"：{compile_result.error}" if compile_result.error else "")
        ),
        payload=compile_info,
        level="info" if compile_result.ok else "warning",
    )

    log_artifact = ctx.store.put_bytes(
        project_id,
        "log",
        (compile_result.log or "").encode("utf-8"),
        media_type="text/plain",
        document_id=document_id,
        meta={"generation_id": generation_id, "exit_code": compile_result.exit_code},
    )

    source_text = "\n\n".join(b.text for b in blocks)
    reports = validators.run_all(
        compile_result=compile_info,
        blocks=blocks,
        tex_text=assembled_tex,
        source_text=source_text,
        assets=assets or [],
        available_assets=available_assets,
        policy_preferences=[p.to_dict() for p in preferences],
    )
    # H10: PDF 先写临时文件，四层校验通过后才入库 ——
    # 否则即使内容层判 fail，产物也已经可以通过产物接口下载。
    report_ids = []
    for report in reports:
        report_id = new_id("val")
        ctx.db.insert(
            "validation_reports",
            {
                "id": report_id,
                "project_id": project_id,
                "generation_id": generation_id,
                "layer": report["layer"],
                "verdict": report["verdict"],
                "details_json": canonical_json(report),
                "created_at": now_iso(),
            },
        )
        report_ids.append(report_id)

    verdict = _overall_verdict(reports, policy)

    pdf_artifact_id = ""
    if compile_result.ok and compile_result.pdf_path:
        pdf_meta = {
            "generation_id": generation_id,
            "engine": compile_result.engine,
            "binary": compile_result.binary,
            "duration_ms": compile_result.duration_ms,
            "exit_code": compile_result.exit_code,
            "source_tex_artifact_id": tex_artifact["id"],
        }
        if verdict == "fail":
            quarantine_dir = db_path_parent(ctx.db) / "quarantine"
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            quarantined = quarantine_dir / f"{generation_id}.pdf"
            shutil.copyfile(compile_result.pdf_path, quarantined)
            pdf_meta["quarantined"] = True
            pdf_meta["quarantine_path"] = str(quarantined)
        else:
            pdf_artifact = ctx.store.put_file(
                project_id,
                "pdf",
                compile_result.pdf_path,
                media_type="application/pdf",
                document_id=document_id,
                meta=pdf_meta,
            )
            pdf_artifact_id = pdf_artifact["id"]

    stats = {
        "warnings": warnings,
        "teacher": teacher_stats,
        "conflicts": conflicts,
        "compile": compile_info,
        "validation": [{"layer": r["layer"], "verdict": r["verdict"], "summary": r["summary"]} for r in reports],
        "verdict": verdict,
        "fallback_to_rules": fallback_used,
        "duration_ms": int((time.time() - started) * 1000),
    }
    ctx.db.update(
        "generations",
        generation_id,
        {
            "status": "succeeded" if verdict in {"pass", "warn"} else "failed",
            "tex_artifact_id": tex_artifact["id"],
            "pdf_artifact_id": pdf_artifact_id,
            "stats_json": canonical_json(stats),
            "updated_at": now_iso(),
        },
    )

    if verdict in {"pass", "warn"}:
        _record_training_example(
            ctx,
            project_id=project_id,
            document_id=document_id,
            generation_id=generation_id,
            blocks=blocks,
            teacher_stats=teacher_stats,
            mode=mode,
            policy_version=policy.version,
            fallback_used=bool(fallback_used),
            label_origin=label_origin,
            annotation_ids=annotation_ids,
            example_extra=example_extra,
        )

    job.emit_event(
        stage="validate",
        message=f"四层检查完成：{verdict}",
        payload={"verdict": verdict, "reports": stats["validation"]},
        artifact_ids=[tex_artifact["id"]] + ([pdf_artifact_id] if pdf_artifact_id else []),
    )

    return {
        "generation_id": generation_id,
        "tex_artifact_id": tex_artifact["id"],
        "pdf_artifact_id": pdf_artifact_id,
        "log_artifact_id": log_artifact["id"],
        "verdict": verdict,
        "validation_report_ids": report_ids,
        "stats": stats,
    }


def _wait_on_teacher_questions(policy: Any) -> bool:
    """策略是否要求"教师提问先由人回答"。

    默认否（教师提问只作为警告记录，生成继续）；
    打开后任务会进入可恢复的 waiting_user。
    """
    config = getattr(policy, "validator_config", None) or {}
    return bool(config.get("wait_on_teacher_questions", False))


def _resolve_label_origin(teacher_stats: dict, fallback_used: bool) -> str:
    """决定样本的标签来源。

    只有教师**真的产出并被采用**的内容才算 teacher_generated；
    教师失败、或内容回退到规则/工具转换时，必须如实标成规则来源，
    否则会把工具完成的收益算到模型头上（直接污染 SFT 数据）。
    """
    calls = int(teacher_stats.get("calls", 0) or 0)
    failed = int(teacher_stats.get("failed", 0) or 0)
    fallback_blocks = int(teacher_stats.get("fallback_blocks", 0) or 0)
    if calls == 0:
        return "rule_synthesized"
    if failed >= calls or fallback_used:
        return "rule_synthesized"
    if fallback_blocks:
        # 有块回退到了规则/Pandoc 兜底 → 内容是"教师 + 工具"的混合，
        # 只能算教师生成（不是规则纯生成），但**绝不能**标成
        # human_reviewed_teacher：生成流程里没有任何人工复核，
        # 标成"人工复核过"就是把工具产物冒充成人审过的。
        return "teacher_generated"
    return "teacher_generated"


def _overall_verdict(reports: list[dict], policy: Any) -> str:
    config = policy.validator_config or {}
    raw_accept = config.get("accept_verdict", ["pass"])
    # 配置里可能写成字符串 "pass"：set("pass") == {'p','a','s'}，会让判定永远不成立
    accept = set(raw_accept) if isinstance(raw_accept, (list, tuple, set)) else {str(raw_accept)}
    raw_fail = config.get("fail_on_layers", ["engineering", "content"])
    fail_on = set(raw_fail) if isinstance(raw_fail, (list, tuple, set)) else {str(raw_fail)}
    verdicts = {r["layer"]: r["verdict"] for r in reports}
    for layer in fail_on:
        if verdicts.get(layer) == "fail":
            return "fail"
    if all(verdicts.get(layer) in accept for layer in verdicts):
        return "pass"
    return "warn"


# --------------------------------------------------------------------------
# 3. 局部修订（批注 → 修订 → 验证 → SFT 候选）
# --------------------------------------------------------------------------
def run_revision(
    ctx: AppContext,
    job: JobContext,
    *,
    project_id: str,
    document_id: str,
    annotation_id: str,
    block_id: str = "",
    block_ids: list[str] | None = None,
    instruction: str,
    provider: str = "",
    model: str = "",
    template_name: str = "preamble_zh_ctex",
) -> dict:
    """按用户意见修改单个或多个源块，然后重新编译与验证（支持 BUG-03 路由与 BUG-08 多选块修订）。"""
    started = time.time()
    blocks, doc_context, doc_revision = load_blocks(ctx, document_id)
    active_provider = (
        provider
        or getattr(ctx.settings.providers, "default_teacher_provider", "ollama")
        or "ollama"
    )

    annotation = ctx.db.query_one("SELECT * FROM annotations WHERE id = ?", (annotation_id,))
    if not annotation:
        raise ValueError(f"批注不存在：{annotation_id}")
    if annotation["document_id"] != document_id:
        raise ValueError("批注与文档不匹配")
    if annotation.get("document_revision"):
        if int(annotation["document_revision"]) != int(doc_revision):
            raise ValueError(
                f"批注绑定的是文档版本 {annotation['document_revision']}，"
                f"当前文档版本是 {doc_revision}；请在当前 PDF 上重新定位后再修订"
            )
    latest_generation = ctx.db.query_one(
        "SELECT * FROM generations WHERE document_id = ? "
        "AND (tex_artifact_id != '' OR pdf_artifact_id != '') "
        "ORDER BY created_at DESC LIMIT 1",
        (document_id,),
    )
    if latest_generation:
        current_artifacts = {
            str(latest_generation.get("tex_artifact_id") or ""),
            str(latest_generation.get("pdf_artifact_id") or ""),
        }
        current_artifacts.discard("")
        if current_artifacts and str(annotation["artifact_id"]) not in current_artifacts:
            raise ValueError(
                "批注绑定的是更早的产物版本：文档已重新解析，源块编号可能已经变化，"
                "请在当前 PDF 上重新定位后再修订"
            )

    requested_ids: list[str] = []
    for cand in list(block_ids or []) + ([block_id] if block_id else []):
        if cand and cand not in requested_ids:
            requested_ids.append(cand)
    if not requested_ids:
        raise ValueError("批注未指定任何源块，请先确认对应源块")

    blocks_by_id = {b.block_id: b for b in blocks}
    missing_ids = [bid for bid in requested_ids if bid not in blocks_by_id]
    if missing_ids:
        raise ValueError(f"找不到源块：{', '.join(missing_ids)}")

    # 按文档原生顺序排列目标块，避免跨块上下文颠倒
    target_blocks = [b for b in blocks if b.block_id in set(requested_ids)]
    ordered_ids = [b.block_id for b in target_blocks]
    primary_block = target_blocks[0]

    policy = ctx.policy_for(project_id)
    preferences = policy_engine.applicable_preferences(
        policy, scope="block", scope_value=primary_block.block_id, block_kind=primary_block.kind
    )
    current_parts = []
    for tb in target_blocks:
        rendered, _w, _a = render_block(tb)
        current_parts.append({"block_id": tb.block_id, "kind": tb.kind, "markdown": tb.text, "current_tex": rendered})

    if len(target_blocks) == 1:
        block_payload: dict[str, Any] = current_parts[0]
        extra = (
            f"用户对这一处的意见是：{instruction}\n"
            "只修改这一处有证据的问题；不要改动其它内容；不要凭空补充事实；"
            "若意见的量化参数（如最小字号）未给出，请使用项目默认值并在 warnings 中说明假设。"
        )
    else:
        block_payload = {
            "block_ids": ordered_ids,
            "blocks": current_parts,
            "markdown": "\n\n".join(b.text for b in target_blocks),
            "current_tex": "\n".join(p["current_tex"] for p in current_parts),
        }
        extra = (
            f"用户选中了连续或相关的 {len(target_blocks)} 个源块（{', '.join(ordered_ids)}），意见是：{instruction}\n"
            "请以 JSON 返回每个源块修订后的 TeX，格式 {\"blocks\": {\"block-xxxx\": \"...\", ...}, \"warnings\": [...]}；"
            "若需要将多个块合并为一个环境，也可返回 {\"tex\": \"完整 LaTeX 片段\"}。"
            "只修改这几处有证据的问题；不要改动其它内容。"
        )

    prompt = policy_engine.build_teacher_prompt(
        policy,
        block=block_payload,
        context={
            "title": doc_context.get("title", ""),
            "heading_path": primary_block.path,
            "outline": doc_context.get("heading_outline", [])[:30],
        },
        preferences=preferences,
        reference_snippets=_load_reference_snippets(ctx, project_id, limit=2),
        validation_errors=[],
        extra_instructions=extra,
    )

    result = ctx.teacher.call(
        project_id,
        active_provider,
        ProviderRequest(
            prompt=prompt,
            json_mode=True,
            temperature=0.1,
            max_tokens=2500,
            seed=int(getattr(ctx.settings.providers.ollama, "seed", 42)),
        ),
        model=model,
        policy_version=policy.version,
        prompt_version="prompt-001",
    )
    if result.error_kind == "budget":
        from ...jobs import STATUS_PAUSED_BUDGET, JobPaused

        raise JobPaused(STATUS_PAUSED_BUDGET, f"预算不足，修订调用未执行：{result.error}")

    block_updates: dict[str, str] = {}
    new_tex = ""
    warnings: list[str] = []
    if result.ok:
        payload = parse_json_object(result.content)
        for warning in payload.get("warnings") or []:
            warnings.append(f"教师警告：{warning}")
        raw_blocks_map = payload.get("blocks")
        if isinstance(raw_blocks_map, dict) and raw_blocks_map:
            for bid in ordered_ids:
                if bid in raw_blocks_map and str(raw_blocks_map[bid]).strip():
                    cleaned = sanitize_teacher_tex(str(raw_blocks_map[bid]))
                    if cleaned:
                        block_updates[bid] = cleaned
        raw_tex = sanitize_teacher_tex(str(payload.get("tex") or ""))
        if not block_updates and raw_tex:
            if len(ordered_ids) == 1:
                block_updates[ordered_ids[0]] = raw_tex
            else:
                block_updates[ordered_ids[0]] = raw_tex
                for extra_id in ordered_ids[1:]:
                    block_updates[extra_id] = "% (已合并到上方协同修订块)"
        new_tex = block_updates.get(ordered_ids[0], "") or raw_tex
    else:
        warnings.append(f"教师调用失败：{result.error}")
    if not block_updates:
        raise RuntimeError(f"修订未产生可用的 TeX：{result.error or '教师返回为空'}")

    # 修订也要按 job_id 复用 generation 行：
    # 否则重试会多建一行（界面像"还在跑"），并多花一次教师费用。
    existing_generation = ctx.db.query_one(
        "SELECT * FROM generations WHERE job_id = ?", (job.job_id,)
    )
    actual_model = result.model or model
    if existing_generation:
        generation_id = existing_generation["id"]
        ctx.db.update(
            "generations",
            generation_id,
            {
                "status": "running",
                "provider": active_provider,
                "model": actual_model,
                "updated_at": now_iso(),
                "tex_artifact_id": "",
                "pdf_artifact_id": "",
            },
        )
    else:
        generation_id = new_id("gen")
        ctx.db.insert(
            "generations",
            {
                "id": generation_id,
                "project_id": project_id,
                "document_id": document_id,
                "mode": "revision",
                "status": "running",
                "provider": active_provider,
                "model": actual_model,
                "policy_version": policy.version,
                "prompt_version": "prompt-001",
                "job_id": job.job_id,
                "tex_artifact_id": "",
                "pdf_artifact_id": "",
                "stats_json": "{}",
                "created_at": now_iso(),
                "updated_at": now_iso(),
            },
        )
    job.emit_event(
        stage="revision",
        message=f"按批注修订源块 {', '.join(ordered_ids)}",
        payload={"annotation_id": annotation_id, "block_id": ordered_ids[0], "block_ids": ordered_ids},
    )

    assembled = assemble_document(
        blocks,
        settings=ctx.settings,
        title=doc_context.get("title", ""),
        teacher_outputs=block_updates,
        template_name=template_name,
        overrides_dir=_template_overrides_dir(ctx.settings),
    )
    teacher_stats = {
        "calls": 1,
        "failed": 0,
        "fallback_blocks": 0,
        "tokens": result.usage.input_tokens + result.usage.output_tokens,
        "model": actual_model,
        "model_digest": str((result.raw or {}).get("model_digest") or ""),
        "seed": (result.raw or {}).get("seed"),
    }
    outcome = _finalize(
        ctx,
        job,
        project_id=project_id,
        document_id=document_id,
        generation_id=generation_id,
        mode="revision",
        assembled_tex=assembled.tex,
        line_map=assembled.line_map,
        blocks=blocks,
        doc_context=doc_context,
        policy=policy,
        preferences=preferences,
        conflicts=policy_engine.detect_conflicts(preferences),
        warnings=warnings,
        teacher_stats=teacher_stats,
        started=started,
        assets=assembled.assets,
        label_origin="human_reviewed_teacher",
        annotation_ids=[annotation_id],
    )

    # H9: 只有修订真的通过验证，批注才能标成"已接受"；
    # 编译失败/内容层判 fail 时，批注必须留在未解决状态，
    # 否则用户看到"已接受"但其实什么都没修好。
    revision_verdict = str(outcome.get("verdict") or "")
    if revision_verdict == "fail":
        ctx.db.update(
            "annotations",
            annotation_id,
            {"status": "open", "updated_at": now_iso()},
        )
        job.emit_event(
            stage="revision",
            message=f"修订未通过验证（{revision_verdict}），批注保持未解决状态",
            level="warning",
        )
    else:
        ctx.db.update(
            "annotations",
            annotation_id,
            {"status": "accepted", "updated_at": now_iso()},
        )
        # 写回文档版本：否则这次修订只存在于产物里，
        # 下一次生成还会按原文渲染，训练样本也拿不到修订内容。
        try:
            revision_no = write_back_revision(
                ctx,
                document_id=document_id,
                blocks=blocks,
                block_updates=block_updates,
                context={**doc_context, "source_revision": doc_context.get("source_revision", "")},
            )
            job.emit_event(
                stage="revision",
                message=f"修订已写回文档版本 {revision_no}",
                payload={"document_revision": revision_no, "block_id": ordered_ids[0], "block_ids": ordered_ids},
            )
        except Exception as exc:  # noqa: BLE001
            job.emit_event(
                stage="revision",
                message=f"修订写回文档版本失败：{exc}",
                level="warning",
            )
    return {
        **outcome,
        "annotation_id": annotation_id,
        "block_id": ordered_ids[0],
        "block_ids": ordered_ids,
        "new_tex": new_tex,
        "block_updates": block_updates,
        "annotation_status": "open" if revision_verdict == "fail" else "accepted",
    }


def write_back_revision(
    ctx: AppContext,
    *,
    document_id: str,
    blocks: list[Block],
    block_id: str = "",
    new_tex: str = "",
    block_updates: dict[str, str] | None = None,
    context: dict | None = None,
) -> int:
    """把修订后的 TeX（单块或多块）写回文档版本。

    没有这一步，修订只停留在产物里：下一次生成仍然按原文渲染，
    训练样本里也拿不到修订后的内容。写回后产生一个新的 document_revision
    （旧版本保留，便于追溯）。
    """
    updates = dict(block_updates or {})
    if block_id and new_tex and block_id not in updates:
        updates[block_id] = new_tex
    if not updates:
        raise ValueError("缺少要写回的修订块内容")

    revised_blocks: list[Block] = []
    changed = 0
    for block in blocks:
        if block.block_id in updates:
            meta = dict(block.meta or {})
            meta["revised_tex"] = updates[block.block_id]
            revised_blocks.append(
                Block(
                    block_id=block.block_id,
                    kind=block.kind,
                    text=block.text,
                    level=block.level,
                    path=block.path,
                    line_start=block.line_start,
                    line_end=block.line_end,
                    meta=meta,
                )
            )
            changed += 1
        else:
            revised_blocks.append(block)
    if changed == 0:
        raise ValueError(f"找不到要写回的源块：{', '.join(updates.keys())}")

    revision = (
        ctx.db.scalar("SELECT COUNT(*) AS c FROM document_revisions WHERE document_id = ?", (document_id,))
        or 0
    ) + 1
    ctx.db.insert(
        "document_revisions",
        {
            "id": new_id("docrev"),
            "document_id": document_id,
            "revision": revision,
            "source_revision": str((context or {}).get("source_revision") or ""),
            "blocks_json": canonical_json([b.to_dict() for b in revised_blocks]),
            "context_json": canonical_json(context or {}),
            "created_at": now_iso(),
        },
    )
    ctx.db.update("documents", document_id, {"status": "revised", "updated_at": now_iso()})
    return revision


def _record_training_example(
    ctx: AppContext,
    *,
    project_id: str,
    document_id: str,
    generation_id: str,
    blocks: list[Block],
    teacher_stats: dict,
    mode: str,
    policy_version: str,
    fallback_used: bool = False,
    label_origin: str | None = None,
    annotation_ids: list[str] | None = None,
    example_extra: dict[str, Any] | None = None,
) -> None:
    """把一次通过的生成登记为 SFT 候选（按设计文档 §4 的样例结构）。"""
    document = ctx.db.query_one("SELECT * FROM documents WHERE id = ?", (document_id,)) or {}
    generation = ctx.db.query_one("SELECT * FROM generations WHERE id = ?", (generation_id,)) or {}
    tex_artifact_id = generation.get("tex_artifact_id") or ""
    if not tex_artifact_id:
        return
    tex_text = ctx.store.read_text(tex_artifact_id)
    # 训练输入用**原始 Markdown**（保留 $$ 定界符与代码围栏）：
    # 块拼接会把公式定界符、代码围栏都丢掉 ——
    # 模型学到的输入格式与用户实际持有的 Markdown 不一致。
    try:
        source_markdown = source_text_for_document(ctx, document_id)
    except Exception:  # noqa: BLE001
        source_markdown = "\n\n".join(b.text for b in blocks)
    # 同一个 generation 只登记一条样本：重试会再次走到这里，
    # 否则数据集里会出现内容完全相同的重复样本。
    source_group_id = document.get("source_document_id") or document_id
    duplicate = ctx.db.query_one(
        "SELECT id FROM training_examples WHERE source_group_id = ? AND source_revision = ? "
        "AND target_payload_json = ?",
        (source_group_id, document.get("source_revision", ""), canonical_json({"tex": tex_text})),
    )
    if duplicate:
        return
    example_id = new_id("ex")
    ctx.db.insert(
        "training_examples",
        {
            "id": example_id,
            "project_id": project_id,
            "task_type": "markdown_to_latex",
            "source_group_id": document.get("source_document_id") or document_id,
            "source_revision": document.get("source_revision", ""),
            "input_payload_json": canonical_json(
                {
                    "mode": mode,
                    # 用**原始 Markdown**（含 $$ 定界符与代码围栏）：
                    # 块拼接会把它们丢掉 → 训练输入与用户实际持有的 Markdown
                    # 不是一个东西，推理时分布不一致。
                    "markdown": source_markdown,
                    "blocks": [b.to_dict() for b in blocks],
                    "context": {"title": document.get("title", "")},
                }
            ),
            "target_payload_json": canonical_json({"tex": tex_text}),
            "teacher_json": canonical_json(
                {
                    "provider": generation.get("provider") or "rule_or_tool",
                    "model": generation.get("model") or "",
                    # 真实权重摘要（拿不到才写 "unknown"）
                    "revision": teacher_stats.get("model_digest") or "unknown",
                    "mode": mode,
                    "seed": teacher_stats.get("seed"),
                    "calls": teacher_stats.get("calls", 0),
                    "failed": teacher_stats.get("failed", 0),
                    "fallback_blocks": teacher_stats.get("fallback_blocks", 0),
                    "fallback_to_rules": bool(fallback_used),
                }
            ),
            "policy_version": policy_version,
            "prompt_version": generation.get("prompt_version", ""),
            "reference_ids_json": "[]",
            "annotation_ids_json": canonical_json(annotation_ids or []),
            "label_origin": label_origin or _resolve_label_origin(teacher_stats, fallback_used),
            "validation_report_id": None,
            "status": "candidate",
            "split": "train",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        },
    )
    if example_extra:
        ctx.db.update("training_examples", example_id, example_extra)
