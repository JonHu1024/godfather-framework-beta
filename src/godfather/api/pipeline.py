"""生成、产物、批注、数据集、控制器路由。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .. import datasets as dataset_service
from ..context import AppContext
from ..controller import actions as controller_actions
from ..util import canonical_json, new_id, now_iso
from .deps import get_ctx

router = APIRouter(tags=["pipeline"])


class GenerationCreate(BaseModel):
    document_id: str
    mode: str = "pandoc_baseline"
    provider: str = ""
    model: str = ""
    template_name: str = "preamble_zh_ctex"


class AnnotationCreate(BaseModel):
    artifact_id: str
    artifact_revision: str
    document_id: str | None = None
    page_index: int = 0
    viewport_rotation: int = 0
    bbox: list[float] = Field(default_factory=list)
    comment: str = ""
    category: str = "other"
    scope: str = "document"
    source_block_candidates: list[str] = Field(default_factory=list)
    mapping_confidence: str = "needs_review"
    label_origin: str = "human"


class AnnotationUpdate(BaseModel):
    comment: str | None = None
    category: str | None = None
    scope: str | None = None
    status: str | None = None
    source_block_candidates: list[str] | None = None
    mapping_confidence: str | None = None


class AcceptAnnotationRequest(BaseModel):
    block_ids: list[str] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    template_name: str = "preamble_zh_ctex"
    confirm: bool = False
    force: bool = False


class ExampleUpdate(BaseModel):
    status: str


class BatchExampleStatusRequest(BaseModel):
    example_ids: list[str] = Field(min_length=1)
    status: str


class ManualImport(BaseModel):
    payload: dict


# --------------------------------------------------------------------------
# 生成
# --------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/generations")
def create_generation(project_id: str, payload: GenerationCreate, ctx: AppContext = Depends(get_ctx)) -> dict:
    from ..taskpacks.latex.pipeline import MODES

    project = ctx.project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    task_type = str(project.get("taskpack") or "markdown_to_latex")
    spec = ctx.taskpacks.get_optional(task_type)
    allowed_modes = (
        tuple(spec.capabilities.get("supported_modes") or ())
        if spec and spec.capabilities.get("supported_modes")
        else MODES
    )
    if payload.mode not in allowed_modes:
        raise HTTPException(
            status_code=400,
            detail=f"未知模式：{payload.mode}，可用：{', '.join(allowed_modes)}",
        )
    document = ctx.db.query_one("SELECT * FROM documents WHERE id = ?", (payload.document_id,))
    if not document:
        raise HTTPException(status_code=404, detail="文档不存在")
    if document["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="文档不存在")
    document_revision = (
        ctx.db.scalar(
            "SELECT MAX(revision) AS r FROM document_revisions WHERE document_id = ?",
            (payload.document_id,),
        )
        or 0
    )
    effective_provider = (
        payload.provider
        or getattr(ctx.settings.providers, "default_teacher_provider", "ollama")
        or "ollama"
    )
    job_kind = (spec.default_job_kind if spec and spec.default_job_kind else "latex.generate")
    job = ctx.queue.submit(
        job_kind,
        {
            "project_id": project_id,
            "document_id": payload.document_id,
            "mode": payload.mode,
            "provider": effective_provider,
            "model": payload.model,
            "template_name": payload.template_name or "preamble_zh_ctex",
        },
        project_id=project_id,
        idempotency_key=(
            f"{job_kind}:{payload.document_id}:{document_revision}"
            f":{payload.mode}:{effective_provider}:{payload.model or ''}"
            f":{payload.template_name or 'preamble_zh_ctex'}"
        ),
    )
    return {
        "job_id": job["id"],
        "status": job["status"],
        "job_type": job_kind,
        "provider": effective_provider,
        "reused": bool(job.get("reused")),
    }


@router.get("/api/projects/{project_id}/generations")
def list_generations(project_id: str, ctx: AppContext = Depends(get_ctx)) -> list[dict]:
    rows = ctx.db.query_all(
        "SELECT * FROM generations WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
    )
    for row in rows:
        row["stats"] = json.loads(row.pop("stats_json") or "{}")
    return rows


def _require_owned(request: Request, row: dict | None, what: str = "资源") -> dict:
    """对象级归属校验（纵深防御）。

    中间件已经按路径解析过项目，但端点自己更清楚对象属于谁 ——
    这里再比一次：会话解锁的项目必须就是对象所属的项目。
    越权时返回 404（不泄露"存在但不属于你"）。
    """
    from fastapi import HTTPException

    unlocked = getattr(request.state, "pairing_project", None)
    if unlocked:
        owner = (row or {}).get("project_id")
        if owner != unlocked:
            raise HTTPException(status_code=404, detail=f"{what}不存在")
    return row or {}


@router.get("/api/generations/{generation_id}")
def get_generation(
    generation_id: str, request: Request, ctx: AppContext = Depends(get_ctx)
) -> dict:
    generation = ctx.db.query_one("SELECT * FROM generations WHERE id = ?", (generation_id,))
    if not generation:
        raise HTTPException(status_code=404, detail="生成轮次不存在")
    _require_owned(request, generation, "生成轮次")
    generation["stats"] = json.loads(generation.pop("stats_json") or "{}")
    reports = ctx.db.query_all(
        "SELECT * FROM validation_reports WHERE generation_id = ? ORDER BY created_at", (generation_id,)
    )
    for report in reports:
        report["details"] = json.loads(report.pop("details_json") or "{}")
    generation["validation_reports"] = reports
    return generation


# --------------------------------------------------------------------------
# 任务
# --------------------------------------------------------------------------
@router.get("/api/jobs/{job_id}")
def get_job(job_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> dict:
    job = ctx.queue.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    _require_owned(request, job, "任务")
    job["result"] = json.loads(job.pop("result_json") or "{}")
    job["logs"] = ctx.queue.logs(job_id)
    return job


@router.get("/api/projects/{project_id}/jobs")
def list_jobs(project_id: str, status: str = "", ctx: AppContext = Depends(get_ctx)) -> list[dict]:
    return ctx.queue.list_jobs(project_id=project_id, status=status or None)


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> dict:
    _require_owned(request, ctx.queue.get(job_id), "任务")
    if not ctx.queue.cancel(job_id):
        raise HTTPException(status_code=400, detail="任务无法取消（不存在或已结束）")
    return {"cancelled": job_id}


@router.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> dict:
    _require_owned(request, ctx.queue.get(job_id), "任务")
    if not ctx.queue.resume(job_id):
        raise HTTPException(status_code=400, detail="任务无法恢复")
    return {"resumed": job_id}


# --------------------------------------------------------------------------
# 产物
# --------------------------------------------------------------------------
@router.get("/api/artifacts/{artifact_id}")
def get_artifact(
    artifact_id: str, request: Request, ctx: AppContext = Depends(get_ctx)
) -> dict:
    artifact = ctx.store.get(artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="产物不存在")
    _require_owned(request, artifact, "产物")
    artifact["meta"] = json.loads(artifact.pop("meta_json") or "{}")
    return artifact


@router.get("/api/artifacts/{artifact_id}/content")
def get_artifact_content(
    artifact_id: str, request: Request, ctx: AppContext = Depends(get_ctx)
):
    artifact = ctx.store.get(artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="产物不存在")
    _require_owned(request, artifact, "产物")
    path = Path(artifact["path"])
    if not path.exists():
        raise HTTPException(status_code=410, detail="产物文件已不存在")
    media_type = artifact["media_type"]
    if media_type.startswith("text/") or media_type in {"application/x-tex", "application/json"}:
        return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))
    return FileResponse(path, media_type=media_type, filename=path.name)


# --------------------------------------------------------------------------
# 批注
# --------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/annotations")
def create_annotation(project_id: str, payload: AnnotationCreate, ctx: AppContext = Depends(get_ctx)) -> dict:
    if not ctx.project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    artifact = ctx.store.get(payload.artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="产物不存在")
    if artifact.get("project_id") and artifact["project_id"] != project_id:
        # 不能把别的项目的产物挂到本项目的批注上（会造成跨项目内容泄漏）
        raise HTTPException(status_code=404, detail="产物不存在")
    if artifact["revision"] != payload.artifact_revision:
        raise HTTPException(
            status_code=409,
            detail="产物版本不匹配：批注必须绑定具体 revision（重排后不允许静默错位）",
        )
    # 文档必须属于本项目（否则可以往别的项目的文档上挂批注）
    document_row = ctx.db.query_one(
        "SELECT project_id FROM documents WHERE id = ?", (payload.document_id,)
    )
    if not document_row or document_row["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="文档不存在")
    # 记录批注绑定的文档版本：文档重新解析后源块编号会重排，
    # 没有这个字段就无法发现"批注落在另一个版本上"的错位。
    document_revision = 0
    doc_row = ctx.db.query_one(
        "SELECT MAX(revision) AS rev FROM document_revisions WHERE document_id = ?",
        (payload.document_id,),
    )
    if doc_row and doc_row.get("rev") is not None:
        document_revision = int(doc_row["rev"])

    annotation_id = new_id("ann")
    ctx.db.insert(
        "annotations",
        {
            "id": annotation_id,
            "project_id": project_id,
            "document_id": payload.document_id,
            "document_revision": document_revision,
            "artifact_id": payload.artifact_id,
            "artifact_revision": payload.artifact_revision,
            "page_index": payload.page_index,
            "viewport_rotation": payload.viewport_rotation,
            "bbox_json": canonical_json(payload.bbox),
            "comment": payload.comment,
            "category": payload.category,
            "scope": payload.scope,
            "source_block_candidates_json": canonical_json(payload.source_block_candidates),
            "mapping_confidence": payload.mapping_confidence,
            "label_origin": payload.label_origin,
            "status": "open",
            "superseded_by": None,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        },
    )
    return {"id": annotation_id}


@router.get("/api/projects/{project_id}/annotations")
def list_annotations(
    project_id: str,
    document_id: str = "",
    status: str = "",
    ctx: AppContext = Depends(get_ctx),
) -> list[dict]:
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if document_id:
        clauses.append("document_id = ?")
        params.append(document_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    rows = ctx.db.query_all(
        f"SELECT * FROM annotations WHERE {' AND '.join(clauses)} ORDER BY created_at DESC", params
    )
    for row in rows:
        row["bbox"] = json.loads(row.pop("bbox_json") or "[]")
        row["source_block_candidates"] = json.loads(row.pop("source_block_candidates_json") or "[]")
    return rows


@router.patch("/api/annotations/{annotation_id}")
def update_annotation(
    annotation_id: str,
    payload: AnnotationUpdate,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    annotation = ctx.db.query_one("SELECT * FROM annotations WHERE id = ?", (annotation_id,))
    if not annotation:
        raise HTTPException(status_code=404, detail="批注不存在")
    _require_owned(request, annotation, "批注")
    values: dict[str, Any] = {"updated_at": now_iso()}
    if payload.comment is not None:
        values["comment"] = payload.comment
    if payload.category is not None:
        values["category"] = payload.category
    if payload.scope is not None:
        values["scope"] = payload.scope
    if payload.status is not None:
        values["status"] = payload.status
    if payload.mapping_confidence is not None:
        values["mapping_confidence"] = payload.mapping_confidence
    if payload.source_block_candidates is not None:
        values["source_block_candidates_json"] = canonical_json(payload.source_block_candidates)
    ctx.db.update("annotations", annotation_id, values)
    return {"updated": annotation_id}


@router.delete("/api/annotations/{annotation_id}")
def delete_annotation(
    annotation_id: str,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    annotation = ctx.db.query_one("SELECT * FROM annotations WHERE id = ?", (annotation_id,))
    if not annotation:
        raise HTTPException(status_code=404, detail="批注不存在")
    _require_owned(request, annotation, "批注")
    ctx.db.execute("DELETE FROM annotations WHERE id = ?", (annotation_id,))
    return {"deleted": annotation_id}


@router.post("/api/annotations/{annotation_id}/accept")
def accept_annotation(
    annotation_id: str,
    request: Request,
    payload: AcceptAnnotationRequest | None = Body(default=None),
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    """用户确认源块并接受批注 → 提交局部修订任务（支持多块联合修订 + BUG-03/08）。"""
    annotation = ctx.db.query_one("SELECT * FROM annotations WHERE id = ?", (annotation_id,))
    if not annotation:
        raise HTTPException(status_code=404, detail="批注不存在")
    _require_owned(request, annotation, "批注")
    if not annotation["comment"].strip():
        raise HTTPException(status_code=400, detail="批注没有文字意见，无法自动生成修订要求")

    explicit_blocks = [str(b).strip() for b in ((payload.block_ids if payload else []) or []) if str(b).strip()]
    if explicit_blocks:
        # 保持顺序去重并持久化确认态
        deduped: list[str] = []
        for b in explicit_blocks:
            if b not in deduped:
                deduped.append(b)
        candidates = deduped
        now = now_iso()
        ctx.db.update(
            "annotations",
            annotation_id,
            {
                "source_block_candidates_json": canonical_json(candidates),
                "mapping_confidence": "confirmed",
                "updated_at": now,
            },
        )
        effective_confidence = "confirmed"
        updated_stamp = now
    else:
        candidates = json.loads(annotation["source_block_candidates_json"] or "[]")
        effective_confidence = str(annotation.get("mapping_confidence") or "needs_review")
        if payload and payload.confirm and candidates:
            effective_confidence = "confirmed"
            now = now_iso()
            ctx.db.update(
                "annotations",
                annotation_id,
                {"mapping_confidence": "confirmed", "updated_at": now},
            )
            updated_stamp = now
        else:
            updated_stamp = str(annotation.get("updated_at") or "")

    if not candidates:
        raise HTTPException(
            status_code=400,
            detail="批注未定位到源块（mapping_confidence=needs_review），请先勾选或确认至少一个源码块",
        )
    if effective_confidence == "needs_review":
        raise HTTPException(
            status_code=400,
            detail="当前源块定位尚处于待确认（mapping_confidence=needs_review），请先在源块选择器中确认/勾选源块后再修订",
        )

    effective_provider = (
        (payload.provider if payload else "")
        or getattr(ctx.settings.providers, "default_teacher_provider", "ollama")
        or "ollama"
    )
    effective_model = payload.model if payload else ""
    template_name = (payload.template_name if payload else "") or "preamble_zh_ctex"

    idem_digest = hashlib.sha1(
        canonical_json(
            {
                "annotation_id": annotation_id,
                "block_ids": candidates,
                "comment": annotation["comment"],
                "provider": effective_provider,
                "model": effective_model,
                "updated_at": updated_stamp,
                "force": now_iso() if (payload and payload.force) else "",
            }
        ).encode("utf-8")
    ).hexdigest()[:16]

    job = ctx.queue.submit(
        "latex.revise",
        {
            "project_id": annotation["project_id"],
            "document_id": annotation["document_id"],
            "annotation_id": annotation_id,
            "block_id": candidates[0],
            "block_ids": candidates,
            "instruction": annotation["comment"],
            "provider": effective_provider,
            "model": effective_model,
            "template_name": template_name,
        },
        project_id=annotation["project_id"],
        idempotency_key=f"latex.revise:{annotation_id}:{idem_digest}",
    )
    return {
        "job_id": job["id"],
        "status": job["status"],
        "block_id": candidates[0],
        "block_ids": candidates,
        "provider": effective_provider,
        "model": effective_model,
        "reused": bool(job.get("reused")),
    }


# --------------------------------------------------------------------------
# 训练样本 / 数据集
# --------------------------------------------------------------------------
@router.get("/api/projects/{project_id}/examples")
def list_examples(project_id: str, ctx: AppContext = Depends(get_ctx)) -> list[dict]:
    rows = ctx.db.query_all(
        "SELECT * FROM training_examples WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
    )
    for row in rows:
        row["input_payload"] = json.loads(row.pop("input_payload_json") or "{}")
        row["target_payload"] = json.loads(row.pop("target_payload_json") or "{}")
        row["teacher"] = json.loads(row.pop("teacher_json") or "{}")
        row["reference_ids"] = json.loads(row.pop("reference_ids_json") or "[]")
        row["annotation_ids"] = json.loads(row.pop("annotation_ids_json") or "[]")
    return rows


@router.patch("/api/examples/{example_id}")
def update_example(
    example_id: str,
    payload: ExampleUpdate,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    example = ctx.db.query_one("SELECT * FROM training_examples WHERE id = ?", (example_id,))
    if not example:
        raise HTTPException(status_code=404, detail="样本不存在")
    _require_owned(request, example, "样本")
    if payload.status not in {"candidate", "accepted", "rejected", "needs_review"}:
        raise HTTPException(status_code=400, detail="非法状态")
    ctx.db.update("training_examples", example_id, {"status": payload.status, "updated_at": now_iso()})
    return {"updated": example_id, "status": payload.status}


@router.post("/api/projects/{project_id}/examples/batch-status")
def batch_update_examples(
    project_id: str,
    payload: BatchExampleStatusRequest,
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    if not ctx.project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    if payload.status not in {"candidate", "accepted", "rejected", "needs_review"}:
        raise HTTPException(status_code=400, detail="非法状态")
    ids = [str(i).strip() for i in payload.example_ids if str(i).strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="未选择任何样本")
    placeholders = ",".join("?" for _ in ids)
    now = now_iso()
    with ctx.db.transaction():
        cursor = ctx.db.conn.execute(
            f"UPDATE training_examples SET status = ?, updated_at = ? WHERE project_id = ? AND id IN ({placeholders})",
            (payload.status, now, project_id, *ids),
        )
        updated = cursor.rowcount
    return {"updated_count": updated, "status": payload.status}


@router.post("/api/projects/{project_id}/datasets/snapshots/preview")
def preview_snapshot(project_id: str, payload: dict, ctx: AppContext = Depends(get_ctx)) -> dict:
    if not ctx.project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    try:
        return dataset_service.preview_snapshot_split(
            ctx.db,
            project_id,
            seed=payload.get("seed", "godfather-1"),
            ratios=payload.get("ratios"),
            include_candidates=bool(payload.get("include_candidates")),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/api/projects/{project_id}/datasets/snapshots")
def create_snapshot(project_id: str, payload: dict, ctx: AppContext = Depends(get_ctx)) -> dict:
    if not ctx.project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    try:
        snapshot = dataset_service.build_snapshot(
            ctx.db,
            project_id,
            seed=payload.get("seed", "godfather-1"),
            ratios=payload.get("ratios"),
            version=payload.get("version"),
            include_candidates=bool(payload.get("include_candidates")),
            export_dir=ctx.settings.exports_dir,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {
        "id": snapshot.id,
        "version": snapshot.version,
        "path": snapshot.path,
        "sha256": snapshot.sha256,
        "counts": snapshot.counts,
        "manifest": snapshot.manifest,
    }


@router.get("/api/projects/{project_id}/datasets/snapshots")
def list_snapshots(project_id: str, ctx: AppContext = Depends(get_ctx)) -> list[dict]:
    return dataset_service.list_snapshots(ctx.db, project_id)


@router.get("/api/projects/{project_id}/datasets/taskpack")
def export_taskpack(project_id: str, limit: int = 50, ctx: AppContext = Depends(get_ctx)) -> dict:
    limit = max(1, min(int(limit), 1000))
    """导出人工导入模式的任务包。"""
    return dataset_service.build_manual_taskpack(ctx.db, project_id, limit=limit)


@router.post("/api/projects/{project_id}/datasets/import")
def import_teacher_results(project_id: str, payload: ManualImport, ctx: AppContext = Depends(get_ctx)) -> dict:
    """导回人工教师结果：校验 sample_id 与 input_hash 对应关系。"""
    from ..providers.manual import match_samples, validate_import_payload

    ok, problems = validate_import_payload(payload.payload or {})
    if not ok:
        raise HTTPException(status_code=400, detail={"errors": problems})

    examples = {
        example["id"]: {
            "sample_id": example["id"],
            "input_hash": "sha256:"
            + __import__("hashlib").sha256(
                canonical_json(json.loads(example["input_payload_json"] or "{}")).encode("utf-8")
            ).hexdigest(),
            "example_id": example["id"],
            "task_type": example["task_type"],
        }
        for example in ctx.db.query_all(
            "SELECT * FROM training_examples WHERE project_id = ?", (project_id,)
        )
    }
    matched, failed = match_samples(payload.payload, examples)
    imported = []
    # 导入的样本来自**外部模型**（用户自己在别处跑的），来源是模型 → 默认标
    # teacher_generated。只有用户显式声明"这一条我人工复核过"才标
    # human_reviewed_teacher —— 无条件标成"人工复核过"就是把模型产物
    # 冒充成人审过的，污染 SFT 数据的来源统计。
    payload_reviewed = bool((payload.payload or {}).get("human_reviewed"))
    for item in matched:
        output = item.get("output")
        example_id = item["example_id"]
        teacher_model = item.get("teacher_model") or "declared-model"
        # 单条显式值优先：顶层说"全部复核过"不能覆盖单条的"这一条没复核"。
        reviewed = (
            bool(item["human_reviewed"])
            if "human_reviewed" in item
            else payload_reviewed
        )
        ctx.db.update(
            "training_examples",
            example_id,
            {
                "target_payload_json": canonical_json({"tex": output}),
                "teacher_json": canonical_json(
                    {
                        "provider": "manual_import",
                        "model": teacher_model,
                        "revision": item.get("revision") or "unknown",
                        "notes": item.get("notes", ""),
                        "human_reviewed": reviewed,
                    }
                ),
                "label_origin": "human_reviewed_teacher" if reviewed else "teacher_generated",
                "status": "candidate",
                "updated_at": now_iso(),
            },
        )
        imported.append(example_id)
    return {"imported": imported, "failed": failed, "counts": {"imported": len(imported), "failed": len(failed)}}


# --------------------------------------------------------------------------
# 控制器
# --------------------------------------------------------------------------
@router.get("/api/projects/{project_id}/controller/state")
def controller_state(project_id: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    return controller_actions.analyze_project(ctx.db, project_id)


@router.post("/api/projects/{project_id}/controller/propose")
def controller_propose(project_id: str, payload: dict, ctx: AppContext = Depends(get_ctx)) -> dict:
    """提出下一步建议。

    默认使用确定性分析（低成本、可复现）；若提供 provider/model 且可用，
    可调用模型生成结构化建议，但仍必须经过同一套校验。
    """
    if not ctx.project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    action = payload.get("action")
    if not action:
        action = controller_actions.deterministic_next_action(ctx.db, project_id)
    validation = controller_actions.validate_action(ctx.db, project_id, action)
    action_id = controller_actions.record_proposal(ctx.db, project_id, action, validation)
    return {
        "action_id": action_id,
        "action": action,
        "valid": validation.ok,
        "errors": validation.errors,
        "warnings": validation.warnings,
    }


@router.post("/api/projects/{project_id}/controller/actions")
def controller_execute(project_id: str, payload: dict, ctx: AppContext = Depends(get_ctx)) -> dict:
    action = payload.get("action") or {}
    if not isinstance(action, dict):
        # 类型错误要返回 400，不能变成 500 并把内部异常回显给客户端
        raise HTTPException(status_code=400, detail="action 必须是对象")
    validation = controller_actions.validate_action(ctx.db, project_id, action)
    action_id = controller_actions.record_proposal(ctx.db, project_id, action, validation)
    if not validation.ok:
        return {
            "action_id": action_id,
            "executed": False,
            "errors": validation.errors,
            "warnings": validation.warnings,
        }
    result = controller_actions.execute_action(ctx, project_id, validation.normalized)
    ctx.db.update(
        "controller_actions",
        action_id,
        {
            "status": "executed" if result["status"] == "succeeded" else "failed",
            "result_json": canonical_json(result),
            "executed_at": now_iso(),
        },
    )
    return {"action_id": action_id, "executed": True, **result}


@router.get("/api/projects/{project_id}/controller/actions")
def controller_action_history(project_id: str, ctx: AppContext = Depends(get_ctx)) -> list[dict]:
    rows = ctx.db.query_all(
        "SELECT * FROM controller_actions WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
    )
    for row in rows:
        row["arguments"] = json.loads(row.pop("arguments_json") or "{}")
        row["validation"] = json.loads(row.pop("validation_json") or "{}")
        row["result"] = json.loads(row.pop("result_json") or "{}")
    return rows


# --------------------------------------------------------------------------
# 问题卡
# --------------------------------------------------------------------------
@router.get("/api/projects/{project_id}/questions")
def list_questions(project_id: str, ctx: AppContext = Depends(get_ctx)) -> list[dict]:
    rows = ctx.db.query_all(
        "SELECT * FROM question_cards WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
    )
    for row in rows:
        row["context"] = json.loads(row.pop("context_json") or "{}")
        row["options"] = json.loads(row.pop("options_json") or "[]")
        row["answer"] = json.loads(row.pop("answer_json") or "{}")
    return rows


@router.post("/api/questions/{question_id}/answer")
def answer_question(
    question_id: str, payload: dict, request: Request, ctx: AppContext = Depends(get_ctx)
) -> dict:
    card = ctx.db.query_one("SELECT * FROM question_cards WHERE id = ?", (question_id,))
    if not card:
        raise HTTPException(status_code=404, detail="问题卡不存在")
    _require_owned(request, card, "问题卡")
    ctx.db.update(
        "question_cards",
        question_id,
        {"status": "answered", "answer_json": canonical_json(payload), "answered_at": now_iso()},
    )
    return {"answered": question_id}
