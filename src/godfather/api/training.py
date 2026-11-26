"""训练与模型中心 API（M1）。"""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..context import AppContext
from ..training import backends as backend_registry
from ..training import evaluate as evaluation_service
from ..training import models as model_registry
from ..training import resources as resource_service
from ..training.base import TrainingConfig
from ..util import now_iso
from .deps import get_ctx

router = APIRouter(prefix="/api/training", tags=["training"])


def _scoped_project(request: Request, project_id: str) -> str:
    """把请求限定在会话解锁的项目上。

    局域网会话只解锁一个项目：不带 project_id 时用它，
    带了别的 project_id 时中间件会直接 403，这里再兜一层。
    """
    scoped = getattr(request.state, "pairing_project", None)
    if scoped:
        return scoped
    return project_id


class RunCreate(BaseModel):
    """训练参数。取值范围必须校验：

    例如 max_seq_length=0 会让每个样本的 labels 全是 -100，
    训练"成功"但完全没有监督信号（loss=NaN）。
    """

    dataset_snapshot_id: str = Field(min_length=1)
    base_model: str = Field(min_length=1)
    base_revision: str = ""
    tokenizer_revision: str = ""
    chat_template: str = ""
    backend: str = "local"
    device: Literal["auto", "cpu", "cuda"] = "auto"
    use_lora: bool = True
    load_in_4bit: bool = True
    lora_rank: int = Field(default=16, ge=1, le=256)
    lora_alpha: int = Field(default=32, ge=1, le=512)
    lora_dropout: float = Field(default=0.05, ge=0.0, le=0.9)
    max_seq_length: int = Field(default=2048, ge=16, le=32768)
    per_device_train_batch_size: int = Field(default=1, ge=1, le=64)
    gradient_accumulation_steps: int = Field(default=16, ge=1, le=512)
    learning_rate: float = Field(default=2e-4, gt=0, le=1.0)
    num_train_epochs: float = Field(default=3.0, gt=0, le=100)
    warmup_ratio: float = Field(default=0.05, ge=0.0, le=1.0)
    max_steps: int = -1
    logging_steps: int = Field(default=5, ge=1, le=1000)
    save_steps: int = Field(default=100, ge=1, le=10000)
    seed: int = 42
    gradient_checkpointing: bool = True
    train_split: Literal["train", "dev", "holdout"] = "train"
    eval_split: Literal["train", "dev", "holdout"] = "dev"
    max_train_samples: int = -1
    max_eval_samples: int = Field(default=32, ge=0, le=10000)
    notes: str = ""


class ExportCreate(BaseModel):
    quantization: str = ""
    model_name: str = ""
    template: str = ""
    system_prompt: str = ""


class EvaluationCreate(BaseModel):
    candidate_model: str
    baseline_model: str = ""
    snapshot_id: str = ""
    # 只允许评测划分：拿 train 评测等于拿训练集考试，
    # 指标会虚高还会被当成正式评测记录写进 evaluation_runs。
    split: Literal["dev", "holdout"] = "dev"
    limit: int = Field(default=32, ge=1, le=1000)
    run_id: str = ""


def _config_from_payload(payload: RunCreate) -> TrainingConfig:
    return TrainingConfig(
        base_model=payload.base_model,
        base_revision=payload.base_revision,
        tokenizer_revision=payload.tokenizer_revision,
        chat_template=payload.chat_template,
        use_lora=payload.use_lora,
        load_in_4bit=payload.load_in_4bit,
        lora_rank=payload.lora_rank,
        lora_alpha=payload.lora_alpha,
        lora_dropout=payload.lora_dropout,
        max_seq_length=payload.max_seq_length,
        per_device_train_batch_size=payload.per_device_train_batch_size,
        gradient_accumulation_steps=payload.gradient_accumulation_steps,
        learning_rate=payload.learning_rate,
        num_train_epochs=payload.num_train_epochs,
        warmup_ratio=payload.warmup_ratio,
        max_steps=payload.max_steps,
        logging_steps=payload.logging_steps,
        save_steps=payload.save_steps,
        seed=payload.seed,
        gradient_checkpointing=payload.gradient_checkpointing,
        dataset_snapshot_id=payload.dataset_snapshot_id,
        train_split=payload.train_split,
        eval_split=payload.eval_split,
        max_train_samples=payload.max_train_samples,
        max_eval_samples=payload.max_eval_samples,
        backend=payload.backend,
        device=payload.device,
    )


@router.get("/backends")
def list_backends(ctx: AppContext = Depends(get_ctx)) -> dict:
    return {
        "backends": backend_registry.list_backends(),
        "resources": resource_service.gpu_state(ctx.db, ctx.settings.providers.ollama.base_url),
        "quantize_methods": ["q4_k_m", "q4_0", "q5_k_m", "q8_0", "f16"],
    }


@router.get("/resources")
def resources_state(ctx: AppContext = Depends(get_ctx)) -> dict:
    return resource_service.gpu_state(ctx.db, ctx.settings.providers.ollama.base_url)


@router.post("/runs")
def create_run(project_id: str, payload: RunCreate, ctx: AppContext = Depends(get_ctx)) -> dict:
    if not ctx.project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    snapshot = ctx.db.query_one(
        "SELECT * FROM dataset_snapshots WHERE id = ?", (payload.dataset_snapshot_id,)
    )
    if not snapshot:
        raise HTTPException(status_code=404, detail="数据快照不存在")
    # 快照必须属于本项目：否则可以用 A 项目的会话拿 B 的数据快照开训
    # （训练行挂在 A 名下，数据却是 B 的）
    if snapshot.get("project_id") != project_id:
        raise HTTPException(status_code=404, detail="数据快照不存在")
    try:
        backend_registry.get_backend(payload.backend)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    from ..training.runner import start_run

    config = _config_from_payload(payload)
    run = start_run(ctx, project_id, config)
    job = ctx.queue.submit(
        "training.run",
        {"project_id": project_id, "run_id": run["run_id"], "config": config.to_dict()},
        project_id=project_id,
        idempotency_key=f"training.run:{run['run_id']}",
        max_attempts=1,
    )
    ctx.db.update("training_runs", run["run_id"], {"notes": payload.notes})
    return {"run_id": run["run_id"], "job_id": job["id"], "status": run["status"]}


@router.get("/runs")
def list_runs(
    request: Request, project_id: str = "", ctx: AppContext = Depends(get_ctx)
) -> list[dict]:
    project_id = _scoped_project(request, project_id)
    rows = ctx.db.query_all(
        "SELECT * FROM training_runs WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
    )
    for row in rows:
        row["params"] = json.loads(row.pop("params_json") or "{}")
        row["metrics"] = json.loads(row.pop("metrics_json") or "{}")
    return rows


@router.get("/runs/{run_id}")
def get_run(run_id: str, project_id: str = "", ctx: AppContext = Depends(get_ctx)) -> dict:
    row = ctx.db.query_one("SELECT * FROM training_runs WHERE id = ?", (run_id,))
    if not row:
        raise HTTPException(status_code=404, detail="训练运行不存在")
    if project_id and row["project_id"] != project_id:
        # 跨项目访问一律 404：不能靠别人的 run_id 读到训练记录与产物路径
        raise HTTPException(status_code=404, detail="训练运行不存在")
    row["params"] = json.loads(row.pop("params_json") or "{}")
    row["metrics"] = json.loads(row.pop("metrics_json") or "{}")
    jobs = ctx.db.query_all(
        "SELECT id, status, error, created_at, updated_at FROM jobs WHERE type LIKE 'training.%' "
        "AND json_extract(payload_json, '$.run_id') = ? ORDER BY created_at DESC",
        (run_id,),
    )
    row["jobs"] = jobs
    return row


CANCELLABLE_STATUSES = {"queued", "running", "paused", "paused_budget", "paused_resource"}


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    """取消训练。只允许取消尚未结束的训练；已完成的训练不能被改成 cancelled。"""
    from ..training.runner import cancel_run as cancel_process

    run = ctx.db.query_one("SELECT * FROM training_runs WHERE id = ?", (run_id,))
    if not run:
        raise HTTPException(status_code=404, detail="训练运行不存在")
    if run["status"] not in CANCELLABLE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"训练运行已结束（状态 {run['status']}），不能取消",
        )

    killed = cancel_process(run_id)
    # L9: 用 json_extract 精确匹配，别用 LIKE —— run_id 含 % 或 _ 时会匹配到别的任务
    job = ctx.db.query_one(
        "SELECT id FROM jobs WHERE type = 'training.run' "
        "AND json_extract(payload_json, '$.run_id') = ? ORDER BY created_at DESC LIMIT 1",
        (run_id,),
    )
    if job:
        ctx.queue.cancel(job["id"])
    ctx.db.update(
        "training_runs",
        run_id,
        {"status": "cancelled", "updated_at": now_iso(), "notes": "已取消；续训语义为重新开始（不含优化器状态）"},
    )
    return {"cancelled": run_id, "process_killed": killed}


@router.post("/runs/{run_id}/export")
def export_run(project_id: str, run_id: str, payload: ExportCreate, ctx: AppContext = Depends(get_ctx)) -> dict:
    run = ctx.db.query_one("SELECT * FROM training_runs WHERE id = ?", (run_id,))
    if not run:
        raise HTTPException(status_code=404, detail="训练运行不存在")
    if run["project_id"] != project_id:
        # 不能把别的项目的训练结果导出到自己名下
        raise HTTPException(status_code=404, detail="训练运行不存在")
    quantization = payload.quantization or ctx.settings.export.quantize_method
    job = ctx.queue.submit(
        "training.export",
        {
            "project_id": project_id,
            "run_id": run_id,
            "quantization": quantization,
            "model_name": payload.model_name,
            "template": payload.template,
            "system_prompt": payload.system_prompt,
        },
        project_id=project_id,
        idempotency_key=f"training.export:{run_id}:{quantization}",
        max_attempts=1,
    )
    return {"job_id": job["id"], "status": job["status"]}


@router.post("/evaluations")
def create_evaluation(project_id: str, payload: EvaluationCreate, ctx: AppContext = Depends(get_ctx)) -> dict:
    if not payload.candidate_model:
        raise HTTPException(status_code=400, detail="缺少候选模型")
    snapshot_id = payload.snapshot_id
    if not snapshot_id and payload.run_id:
        # 给了 run_id 就用**那次训练用的快照**：
        # 否则会用"项目最新快照"，候选模型在 S1 上训练却在 S2 上评测，
        # 候选与基线评的不是同一份数据，跨版本比较失去意义。
        run = ctx.db.query_one(
            "SELECT dataset_snapshot_id FROM training_runs WHERE id = ? AND project_id = ?",
            (payload.run_id, project_id),
        )
        snapshot_id = str((run or {}).get("dataset_snapshot_id") or "")
    if not snapshot_id:
        latest = ctx.db.query_one(
            "SELECT id FROM dataset_snapshots WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        )
        snapshot_id = latest["id"] if latest else ""
    if not snapshot_id:
        raise HTTPException(status_code=400, detail="没有可用于评测的数据快照")
    baseline = payload.baseline_model
    if not baseline:
        project = ctx.project(project_id) or {}
        baseline = project.get("active_model") or ""
    job = ctx.queue.submit(
        "training.evaluate",
        {
            "project_id": project_id,
            "candidate_model": payload.candidate_model,
            "baseline_model": baseline,
            "snapshot_id": snapshot_id,
            "split": payload.split,
            "limit": payload.limit,
            "run_id": payload.run_id,
        },
        project_id=project_id,
    )
    return {"job_id": job["id"], "status": job["status"], "baseline_model": baseline}


@router.get("/evaluations")
def list_evaluations(
    request: Request, project_id: str = "", ctx: AppContext = Depends(get_ctx)
) -> list[dict]:
    project_id = _scoped_project(request, project_id)
    return evaluation_service.list_evaluations(ctx.db, project_id)


@router.get("/models")
def list_models(request: Request, project_id: str = "", ctx: AppContext = Depends(get_ctx)) -> dict:
    project_id = _scoped_project(request, project_id)
    project = ctx.project(project_id) or {}
    return {
        "models": model_registry.list_models(ctx.db, project_id),
        "active_model": project.get("active_model", ""),
    }


@router.post("/models/{model_id}/activate")
def activate_model(
    project_id: str,
    model_id: str,
    force: bool = False,
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    """把某个模型设为当前版本。

    激活前会校验（"验证过才发布"）：
    - 模型还得在 Ollama 里、GGUF 文件还在；
    - 有评测记录的话，不能比基线明显更差。
    ``force=true`` 可跳过这些校验（用户明确知道自己在做什么）。
    """
    model = model_registry.get_model(ctx.db, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="模型版本不存在")
    available = None
    try:
        from ..training.evaluate import list_ollama_models

        available = list_ollama_models(ctx.settings.providers.ollama.base_url)
    except Exception:  # noqa: BLE001
        available = None
    try:
        model_registry.check_activation(
            ctx.db, project_id, model, available_models=available, force=force
        )
        return model_registry.activate(ctx.db, project_id, model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/models/rollback")
def rollback_model(project_id: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    try:
        return model_registry.rollback(ctx.db, project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

