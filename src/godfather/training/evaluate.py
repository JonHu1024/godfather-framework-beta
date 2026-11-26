"""固定开发集评测与基线对照。

设计要求（§9.3）：
- 在相同输入与工具条件下比较候选与当前版本；
- 额外区分"裸模型能力"与"完整系统能力"；
- 报告质量、延迟、外部调用成本与本机资源占用；
- 小数据集报告每项指标的分母与错误数量，不只给一个漂亮的综合分。
"""
from __future__ import annotations

import difflib
import json
import re
import time
from typing import Any

import httpx

from ..context import AppContext
from ..db import Database
from ..jobs import JobContext
from . import resources
from ..util import canonical_json, new_id, now_iso
from .datasets import build_prompt, load_snapshot_examples

LATEX_MARKERS = ("\\section", "\\subsection", "\\begin{", "\\textbf", "\\item", "\\chapter", "\\paragraph")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _normalize(text: str) -> str:
    r"""归一化文本，用于相似度与块覆盖比较。

    **保留 LaTeX 结构**：以前把所有命令与括号都删掉，
    于是"输出同样文字但完全没有 LaTeX"的纯文本能拿 similarity=1.0，
    而真正的 LaTeX 因为命令名不同反而得分更低 ——
    指标与"输出的是不是 LaTeX"正好相反。

    现在把命令与环境名保留成 token（``\section`` → `` cmd:section ``），
    纯文本与 LaTeX 的相似度就会明显区分开。
    """
    text = text or ""
    # 命令与环境名 → 归一化 token
    text = re.sub(r"\\begin\{([a-zA-Z*]+)\}", lambda m: f" env:{m.group(1).lower()} ", text)
    text = re.sub(r"\\end\{([a-zA-Z*]+)\}", lambda m: f" /env:{m.group(1).lower()} ", text)
    text = re.sub(r"\\([a-zA-Z]+)\*?", lambda m: f" cmd:{m.group(1).lower()} ", text)
    # 其它特殊字符仍然丢掉（它们只是排版噪声）
    text = re.sub(r"[{}\\$&%#_~^]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def list_ollama_models(base_url: str) -> list[str] | None:
    """列出 Ollama 里已安装的模型；服务不可达时返回 None（不阻断评测）。"""
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=10.0)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return None
    names: list[str] = []
    for item in models:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
        elif isinstance(item, str):
            names.append(item)
    return names


def _call_ollama(
    base_url: str,
    model: str,
    prompt: str,
    timeout: int = 300,
    seed: int | None = None,
) -> tuple[str, dict]:
    options: dict = {"temperature": 0.0, "num_predict": 2048}
    # 固定种子 → 可复现（DeepInfer 的"确定性采样"）
    if seed is not None:
        options["seed"] = int(seed)
    payload = {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
        "options": options,
    }
    started = time.time()
    try:
        response = httpx.post(f"{base_url.rstrip('/')}/api/chat", json=payload, timeout=timeout)
        duration = int((time.time() - started) * 1000)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return "", {"error": f"{type(exc).__name__}: {exc}", "duration_ms": int((time.time() - started) * 1000)}
    content = ((data.get("message") or {}).get("content") or "")
    return content, {
        "duration_ms": duration,
        "input_tokens": int(data.get("prompt_eval_count") or 0),
        "output_tokens": int(data.get("eval_count") or 0),
    }


def score_response(response: str, reference: str, example: dict) -> dict[str, Any]:
    """单条打分：结构有效、块覆盖、与参考的相似度。"""
    response = response or ""
    normalized_response = _normalize(response)
    normalized_reference = _normalize(reference)
    similarity = difflib.SequenceMatcher(None, normalized_reference, normalized_response).ratio() if normalized_reference else 0.0

    blocks = (example.get("input_payload") or {}).get("blocks") or []
    covered = 0
    considered = 0
    for block in blocks:
        text = (block.get("text") or "").strip()
        if len(text) < 8:
            continue
        considered += 1
        probe = _normalize(text)[:16]
        if probe and probe in normalized_response:
            covered += 1

    return {
        "non_empty": bool(response.strip()),
        "has_latex": any(marker in response for marker in LATEX_MARKERS),
        "block_coverage": (covered / considered) if considered else None,
        "similarity": round(similarity, 4),
        "blocks_considered": considered,
        "blocks_covered": covered,
        "cjk_preserved": bool(CJK_RE.search(reference)) is False or bool(CJK_RE.search(response)),
        "response_chars": len(response),
    }


def _aggregate(records: list[dict]) -> dict[str, Any]:
    total = len(records)
    if not total:
        return {"n": 0, "note": "没有样本"}
    # 调用失败要与"模型输出差"区分开：模型不存在/服务不可达时，
    # 所有样本都会失败，汇总看起来只是"指标很低"，无法定位真正原因。
    failed = [r for r in records if r.get("error")]
    usable = [r for r in records if not r.get("error")]
    non_empty = sum(1 for r in usable if r["score"]["non_empty"])
    has_latex = sum(1 for r in usable if r["score"]["has_latex"])
    coverage_values = [r["score"]["block_coverage"] for r in usable if r["score"]["block_coverage"] is not None]
    similarities = [r["score"]["similarity"] for r in usable]
    durations = [r["latency_ms"] for r in usable if r.get("latency_ms")]
    cjk_ok = sum(1 for r in usable if r["score"]["cjk_preserved"])
    # 比率的分母用**总样本数**，失败的样本算 0：
    # 以前用"成功样本数"当分母 → 10 个样本里 5 个调用失败、5 个完美时，
    # non_empty_rate 会是 1.0（满分），deltas 还会显示候选优于基线 ——
    # 调用失败一半反而"指标更好"，直接误导。
    denominator = total or 1
    return {
        "n": total,
        "errors": len(failed),
        "errors_rate": round(len(failed) / total, 4) if total else 0.0,
        "evaluated": len(usable),
        "non_empty": non_empty,
        "non_empty_rate": round(non_empty / denominator, 4),
        "has_latex": has_latex,
        "has_latex_rate": round(has_latex / denominator, 4),
        "mean_block_coverage": round(sum(coverage_values) / len(coverage_values), 4) if coverage_values else None,
        "coverage_denominator": len(coverage_values),
        "mean_similarity": round(sum(similarities) / len(similarities), 4) if similarities else None,
        "cjk_preserved": cjk_ok,
        "cjk_rate": round(cjk_ok / denominator, 4),
        # 只按成功样本算的一套（保留，便于对照）
        "rates_over_usable": {
            "non_empty_rate": round(non_empty / (len(usable) or 1), 4),
            "has_latex_rate": round(has_latex / (len(usable) or 1), 4),
            "cjk_rate": round(cjk_ok / (len(usable) or 1), 4),
        },
        "mean_latency_ms": int(sum(durations) / len(durations)) if durations else None,
        "latency_denominator": len(durations),
        "failures": [
            {
                "id": r["id"],
                "reason": r.get("error")
                or ("空输出" if not r["score"]["non_empty"] else "无 LaTeX 结构"),
                "call_failed": bool(r.get("error")),
            }
            for r in records
            if r.get("error") or not r["score"]["non_empty"] or not r["score"]["has_latex"]
        ],
    }


def evaluate_models(
    ctx: AppContext,
    job: JobContext | None = None,
    *,
    project_id: str,
    candidate_model: str,
    baseline_model: str,
    snapshot_id: str,
    split: str = "dev",
    limit: int = 32,
    run_id: str = "",
) -> dict:
    """在固定开发集上评测候选模型，并与基线对照。"""
    records = load_snapshot_examples(ctx.db, snapshot_id)
    examples = [r for r in records if r.get("split") == split][:limit]
    if not examples:
        raise ValueError(f"快照 {snapshot_id} 的 {split} 划分为空，无法评测")

    base_url = ctx.settings.providers.ollama.base_url
    models = [("baseline", baseline_model), ("candidate", candidate_model)]
    all_records: dict[str, list[dict]] = {}
    aggregates: dict[str, dict] = {}

    # F13: 先确认模型确实存在 —— 模型不存在时直接失败，
    # 而不是把所有样本记成"输出为空"，让"模型很差"和"模型没装"混为一谈。
    available = list_ollama_models(base_url)
    if available is not None:
        missing = [name for _role, name in models if name and name not in available]
        if missing:
            raise ValueError(
                "Ollama 里没有这些模型："
                + "、".join(missing)
                + f"（当前可用：{', '.join(available) if available else '无'}）"
            )

    # 评测也要占 GPU 锁（docs/08 §7.1 的承诺）：
    # Ollama 推理会吃显存，与训练并发会 OOM。
    # 以前评测完全不占锁 → 文档承诺的"串行"并不成立。
    owner = f"eval:{project_id}"
    acquired = resources.wait_for_resource(
        ctx.db,
        resources.GPU_RESOURCE,
        owner,
        project_id=project_id,
        timeout_seconds=int(getattr(ctx.settings.worker, "gpu_wait_seconds", 900)),
        on_wait=(
            (lambda: job.emit_event(
                stage="eval",
                message="等待 GPU 资源（其它训练或推理占用中）…",
                level="info",
            ))
            if job
            else None
        ),
    )
    if not acquired:
        if job:
            job.emit_event(
                stage="eval",
                message="等待 GPU 资源超时，评测未开始",
                level="warning",
            )
        raise RuntimeError("等待 GPU 资源超时，评测未开始（可能同时有训练在跑）")

    try:
        # **配对式评测**：同一个样本先跑基线、紧接着跑候选。
        #
        # 以前是"先把基线跑完，再跑候选" —— 两轮之间机器状态会漂移
        # （别的进程占卡、模型冷启动、页面缩放……），
        # 差值里就混进了与模型无关的噪声。
        # DeepInfer 专门强调"batch-invariant inference"，
        # 原因正是"批量组成不同，结果就不同"。
        # 交错执行让漂移同时作用于两边，差值才是模型之间的差值。
        active = [(role, model) for role, model in models if model]
        role_records: dict[str, list[dict]] = {role: [] for role, _ in active}
        seed = int(getattr(ctx.settings.providers.ollama, "seed", 42))
        taskpacks = getattr(ctx, "taskpacks", None)
        for index, example in enumerate(examples, start=1):
            prompt = build_prompt(example)
            target_payload = example.get("target_payload") or {}
            reference = str(
                target_payload.get("tex")
                or target_payload.get("text")
                or target_payload.get("output")
                or ""
            )
            task_type = str(example.get("task_type") or "markdown_to_latex")
            scorer = score_response
            if taskpacks is not None and taskpacks.has(task_type):
                spec = taskpacks.get(task_type)
                if callable(spec.score_prediction):
                    scorer = spec.score_prediction
            for role, model in active:
                response, meta = _call_ollama(base_url, model, prompt, seed=seed)
                role_records[role].append(
                    {
                        "id": example.get("id"),
                        "model": model,
                        "score": scorer(response, reference, example),
                        "latency_ms": meta.get("duration_ms", 0),
                        "error": meta.get("error", ""),
                        "response_preview": response[:400],
                    }
                )
            if job:
                job.emit_event(
                    stage="eval",
                    done=index,
                    total=len(examples),
                    message=f"评测 {index}/{len(examples)}（{' vs '.join(m for _, m in active)}）",
                )
        for role, _model in active:
            all_records[role] = role_records[role]
            aggregates[role] = _aggregate(role_records[role])
    finally:
        if acquired:
            resources.release(ctx.db, resources.GPU_RESOURCE, owner)

    deltas = {}
    if "baseline" in aggregates and "candidate" in aggregates:
        base = aggregates["baseline"]
        cand = aggregates["candidate"]
        for metric in ("non_empty_rate", "has_latex_rate", "mean_block_coverage", "mean_similarity", "cjk_rate"):
            if base.get(metric) is None or cand.get(metric) is None:
                continue
            deltas[metric] = round(cand[metric] - base[metric], 4)

    evaluation_id = new_id("ev")
    ctx.db.insert(
        "evaluation_runs",
        {
            "id": evaluation_id,
            "project_id": project_id,
            "candidate_model_id": candidate_model,
            "baseline_model_id": baseline_model,
            "split": split,
            "metrics_json": canonical_json(
                {
                    "aggregates": aggregates,
                    "deltas": deltas,
                    "n_examples": len(examples),
                    "snapshot_id": snapshot_id,
                    "run_id": run_id,
                    # 评测方法本身也要记下来，否则结果不可复现也不可比较：
                    "method": {
                        "paired": True,        # 同一输入先基线后候选，抵消机器漂移
                        "seed": int(getattr(ctx.settings.providers.ollama, "seed", 42)),
                        "temperature": 0.0,
                        "split": split,
                        "baseline_model": baseline_model,
                        "candidate_model": candidate_model,
                    },
                }
            ),
            "per_example_json": canonical_json(all_records),
            "created_at": now_iso(),
        },
    )
    return {
        "evaluation_id": evaluation_id,
        "n_examples": len(examples),
        "split": split,
        "aggregates": aggregates,
        "deltas": deltas,
        "per_example": all_records,
        "method": {
            "paired": True,
            "seed": int(getattr(ctx.settings.providers.ollama, "seed", 42)),
            "temperature": 0.0,
        },
    }


def list_evaluations(db: Database, project_id: str, limit: int = 50) -> list[dict]:
    rows = db.query_all(
        "SELECT * FROM evaluation_runs WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
        (project_id, limit),
    )
    for row in rows:
        row["metrics"] = json.loads(row.pop("metrics_json") or "{}")
    return rows
