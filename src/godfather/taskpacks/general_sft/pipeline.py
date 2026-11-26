"""通用指令微调任务包流水线：解析 JSONL/CSV/Markdown 样本、教师合成/润色、协同修订与登记。"""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

from ...context import AppContext
from ...jobs import STATUS_PAUSED_BUDGET, JobContext, JobPaused
from ...providers.base import ProviderRequest, strip_thinking
from ...util import canonical_json, new_id, now_iso

DEFAULT_SFT_SYSTEM_PROMPT = (
    "你是一个专业且严谨的本地微调助手。请严格遵循用户指令与项目约束规则作答，"
    "输出清晰准确的结果，不要编造输入中不存在的事实。"
)


def parse_sft_source_items(raw_text: str, filename: str = "") -> list[dict[str, Any]]:
    """把 JSONL / JSON / CSV / Markdown 或纯文本解析为统一条目的输入/参考输出列表。"""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    text = (raw_text or "").replace("\r\n", "\n").strip()
    if not text:
        return []

    items: list[dict[str, Any]] = []
    # 1. JSON / JSONL
    if ext in {".jsonl", ".json"} or text.startswith("{") or text.startswith("["):
        loaded: list[dict[str, Any]] = []
        if ext == ".json" or text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    loaded = [x for x in parsed if isinstance(x, dict)]
                elif isinstance(parsed, dict):
                    loaded = [parsed]
            except json.JSONDecodeError:
                loaded = []
        if not loaded:
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        loaded.append(obj)
                except json.JSONDecodeError:
                    continue
        for idx, obj in enumerate(loaded, start=1):
            norm = _normalize_sft_record(obj, idx)
            if norm:
                items.append(norm)
        if items:
            return items

    # 2. CSV / TSV
    if ext in {".csv", ".tsv"}:
        dialect_delim = "\t" if ext == ".tsv" else ","
        reader = csv.DictReader(io.StringIO(text), delimiter=dialect_delim)
        for idx, row in enumerate(reader, start=1):
            norm = _normalize_sft_record({str(k).strip(): v for k, v in row.items() if k}, idx)
            if norm:
                items.append(norm)
        if items:
            return items

    # 3. Markdown / 文本按分段或标题切分成条
    chunks = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
    for idx, chunk in enumerate(chunks, start=1):
        prompt = chunk
        answer = ""
        qa_match = re.search(r"(?:^|\n)(?:###?\s*回答|A:|Output:|Answer:)\s*\n?([\s\S]+)$", chunk, re.IGNORECASE)
        if qa_match:
            prompt = chunk[: qa_match.start()].strip()
            answer = qa_match.group(1).strip()
        items.append(
            {
                "block_id": f"block-{idx:04d}",
                "kind": "sft_item",
                "text": prompt,
                "path": [filename or "corpus"],
                "line_start": idx,
                "line_end": idx,
                "meta": {"target_hint": answer, "system": ""},
            }
        )
    return items


def _normalize_sft_record(obj: dict[str, Any], idx: int) -> dict[str, Any] | None:
    system = ""
    prompt = ""
    target = ""

    messages = obj.get("messages")
    if isinstance(messages, list) and messages:
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "")
            content = str(msg.get("content") or "")
            if role == "system" and not system:
                system = content
            elif role == "user":
                prompt = f"{prompt}\n\n{content}".strip() if prompt else content
            elif role == "assistant":
                target = f"{target}\n\n{content}".strip() if target else content
    else:
        inst = str(obj.get("instruction") or obj.get("prompt") or obj.get("input") or obj.get("question") or "").strip()
        extra_in = str(obj.get("input") or "").strip() if obj.get("instruction") else ""
        prompt = f"{inst}\n\n{extra_in}".strip() if extra_in and extra_in != inst else inst
        target = str(
            obj.get("output")
            or obj.get("response")
            or obj.get("answer")
            or obj.get("completion")
            or obj.get("target")
            or ""
        ).strip()
        system = str(obj.get("system") or "").strip()

    if not prompt:
        return None
    return {
        "block_id": str(obj.get("id") or f"block-{idx:04d}"),
        "kind": "sft_item",
        "text": prompt,
        "path": [str(obj.get("category") or obj.get("topic") or "dataset")],
        "line_start": idx,
        "line_end": idx,
        "meta": {"target_hint": target, "system": system},
    }


def parse_sft_document(
    ctx: AppContext,
    project_id: str,
    *,
    artifact_id: str,
    title: str = "",
    document_id: str | None = None,
    dialect: str = "",
) -> dict[str, Any]:
    artifact = ctx.store.get(artifact_id)
    if not artifact:
        raise ValueError(f"找不到素材产物：{artifact_id}")
    raw_text = ctx.store.read_text(artifact_id)
    meta = json.loads(artifact.get("meta_json") or "{}")
    filename = str(meta.get("filename") or title or "dataset.jsonl")
    items = parse_sft_source_items(raw_text, filename=filename)

    if document_id is None:
        source_id = new_id("src")
        ctx.db.insert(
            "source_documents",
            {
                "id": source_id,
                "project_id": project_id,
                "filename": filename,
                "media_type": artifact["media_type"],
                "sha256": artifact["revision"],
                "revision": 1,
                "artifact_id": artifact["id"],
                "size_bytes": artifact["size_bytes"],
                "dialect": dialect or "jsonl",
                "status": "parsed",
                "meta_json": "{}",
                "created_at": now_iso(),
            },
        )
        document_id = new_id("doc")
        ctx.db.insert(
            "documents",
            {
                "id": document_id,
                "project_id": project_id,
                "title": title or filename,
                "source_document_id": source_id,
                "source_revision": artifact["revision"],
                "status": "parsed",
                "created_at": now_iso(),
                "updated_at": now_iso(),
            },
        )
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
            "source_revision": artifact["revision"],
            "blocks_json": canonical_json(items),
            "context_json": canonical_json({"title": title or filename, "item_count": len(items)}),
            "created_at": now_iso(),
        },
    )
    return {
        "document_id": document_id,
        "revision": revision,
        "title": title or filename,
        "block_count": len(items),
    }


def run_sft_generation(
    ctx: AppContext,
    job: JobContext,
    *,
    project_id: str,
    document_id: str,
    mode: str = "teacher_direct",
    provider: str = "",
    model: str = "",
) -> dict[str, Any]:
    doc = ctx.db.query_one("SELECT * FROM documents WHERE id = ?", (document_id,))
    if not doc:
        raise ValueError(f"找不到文档：{document_id}")
    rev = ctx.db.query_one(
        "SELECT * FROM document_revisions WHERE document_id = ? ORDER BY revision DESC LIMIT 1",
        (document_id,),
    )
    blocks = json.loads((rev or {}).get("blocks_json") or "[]")
    if not blocks:
        raise ValueError("语料文档没有可用样本条目")

    policy = ctx.policy_for(project_id)
    prefs_text = "\n".join(f"- {p.content}" for p in policy.preferences if p.status == "active")
    active_provider = provider or getattr(ctx.settings.providers, "default_teacher_provider", "ollama") or "ollama"

    generation_id = new_id("gen")
    ctx.db.insert(
        "generations",
        {
            "id": generation_id,
            "project_id": project_id,
            "document_id": document_id,
            "mode": mode,
            "status": "running",
            "provider": active_provider if mode != "rule_only" else "manual_import",
            "model": model,
            "policy_version": policy.version,
            "prompt_version": "sft-prompt-001",
            "job_id": job.job_id,
            "tex_artifact_id": "",
            "pdf_artifact_id": "",
            "stats_json": "{}",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        },
    )

    recorded_ids: list[str] = []
    preview_lines: list[str] = []
    calls = 0
    failed = 0
    actual_model = model

    for index, item in enumerate(blocks, start=1):
        job.progress(done=index, total=len(blocks), stage="generate", message=f"处理样本 {index}/{len(blocks)}")
        prompt_text = str(item.get("text") or "").strip()
        meta = item.get("meta") or {}
        system_text = str(meta.get("system") or DEFAULT_SFT_SYSTEM_PROMPT).strip()
        hint_target = str(meta.get("target_hint") or "").strip()
        answer_text = hint_target
        origin = "manual_import" if hint_target else "rule_only"
        model_digest = ""

        if mode in {"teacher_direct", "tool_assisted"} or (mode == "rule_only" and not hint_target):
            if mode != "rule_only":
                teacher_prompt = prompt_text
                if prefs_text:
                    teacher_prompt = f"项目规范：\n{prefs_text}\n\n输入请求：\n{prompt_text}"
                if mode == "tool_assisted" and hint_target:
                    teacher_prompt += f"\n\n现有参考草稿（请检查并精炼修正）：\n{hint_target}"
                result = ctx.teacher.call(
                    project_id,
                    active_provider,
                    ProviderRequest(
                        prompt=teacher_prompt,
                        system=system_text,
                        temperature=0.2,
                        max_tokens=2048,
                        seed=int(getattr(ctx.settings.providers.ollama, "seed", 42)),
                    ),
                    model=model,
                    policy_version=policy.version,
                    prompt_version="sft-prompt-001",
                )
                if result.error_kind == "budget":
                    raise JobPaused(STATUS_PAUSED_BUDGET, f"预算不足：{result.error}")
                calls += 1
                if result.ok and result.content.strip():
                    cleaned, _ = strip_thinking(result.content)
                    answer_text = cleaned or answer_text
                    origin = "teacher_generated"
                    actual_model = result.model or actual_model
                    model_digest = str((result.raw or {}).get("model_digest") or "")
                else:
                    failed += 1

        if not answer_text:
            continue

        group_id = f"{document_id}:{item.get('block_id', index)}"
        target_json = canonical_json({"text": answer_text})
        dup = ctx.db.query_one(
            "SELECT id FROM training_examples WHERE project_id = ? AND source_group_id = ? AND target_payload_json = ?",
            (project_id, group_id, target_json),
        )
        if dup:
            recorded_ids.append(dup["id"])
        else:
            ex_id = new_id("ex")
            ctx.db.insert(
                "training_examples",
                {
                    "id": ex_id,
                    "project_id": project_id,
                    "task_type": "general_sft",
                    "source_group_id": group_id,
                    "source_revision": doc.get("source_revision", ""),
                    "input_payload_json": canonical_json(
                        {
                            "system": system_text,
                            "prompt": prompt_text,
                            "markdown": prompt_text,
                            "blocks": [item],
                            "context": {"title": doc.get("title", "")},
                        }
                    ),
                    "target_payload_json": target_json,
                    "teacher_json": canonical_json(
                        {
                            "provider": active_provider if origin == "teacher_generated" else origin,
                            "model": actual_model,
                            "revision": model_digest or "unknown",
                            "mode": mode,
                        }
                    ),
                    "policy_version": policy.version,
                    "prompt_version": "sft-prompt-001",
                    "reference_ids_json": "[]",
                    "annotation_ids_json": "[]",
                    "label_origin": origin,
                    "validation_report_id": None,
                    "status": "candidate",
                    "split": "train",
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                },
            )
            recorded_ids.append(ex_id)
        preview_lines.append(json.dumps({"id": item.get("block_id"), "prompt": prompt_text, "output": answer_text}, ensure_ascii=False))

    preview_payload = "\n".join(preview_lines) + ("\n" if preview_lines else "")
    preview_art = ctx.store.put_bytes(
        project_id,
        "sft_preview",
        preview_payload.encode("utf-8"),
        media_type="application/jsonl",
        document_id=document_id,
        meta={"mode": mode, "generation_id": generation_id},
    )
    stats = {"calls": calls, "failed": failed, "recorded": len(recorded_ids), "model": actual_model}
    ctx.db.update(
        "generations",
        generation_id,
        {
            "status": "succeeded",
            "model": actual_model,
            "tex_artifact_id": preview_art["id"],
            "stats_json": canonical_json(stats),
            "updated_at": now_iso(),
        },
    )
    return {
        "generation_id": generation_id,
        "tex_artifact_id": preview_art["id"],
        "recorded": len(recorded_ids),
        "stats": stats,
        "verdict": "pass" if recorded_ids else "warn",
    }
