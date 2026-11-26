"""项目、材料、文档、系统预检相关路由。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..context import AppContext
from ..controller import actions as controller_actions
from ..policy import engine as policy_engine
from ..taskpacks.latex import compiler as latex_compiler
from ..taskpacks.latex import pandoc_base
from ..taskpacks.latex.pipeline import parse_markdown_document
from ..util import canonical_json, new_id, now_iso
from .deps import get_ctx

router = APIRouter(tags=["project"])

MATERIAL_KINDS = {
    "markdown": "text/markdown",
    "corpus": "text/plain",
    "dataset": "application/jsonl",
    "asset": "application/octet-stream",
    "reference_md": "text/markdown",
    "reference_tex": "application/x-tex",
    "reference_pdf": "application/pdf",
}


class ProjectCreate(BaseModel):
    name: str
    taskpack: str = "markdown_to_latex"
    description: str = ""


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    active_model: str | None = None
    settings: dict[str, Any] | None = None


class DocumentCreate(BaseModel):
    artifact_id: str
    title: str = ""


# --------------------------------------------------------------------------
# 系统预检
# --------------------------------------------------------------------------
@router.get("/api/system/preflight")
def preflight(request: Request, ctx: AppContext = Depends(get_ctx)) -> dict:
    settings = ctx.settings
    engine_binary, engine_message = latex_compiler.detect_engine(settings)
    pandoc_ok = pandoc_base.is_available(settings)
    providers = ctx.registry.health()
    fontset = latex_compiler.resolve_fontset(settings)
    font_probe = {"ok": None, "fontset": fontset, "message": "未检测（没有可用的 LaTeX 引擎）"}
    if engine_binary:
        font_probe = latex_compiler.probe_fonts(settings)
    return {
        "platform": latex_compiler.platform_info(),
        "latex": {
            "ok": bool(engine_binary),
            "engine": settings.latex.engine,
            "binary": engine_binary or "",
            "message": engine_message,
            "version": latex_compiler.engine_version(engine_binary) if engine_binary else "",
            "cjk_main_font": settings.latex.cjk_main_font,
            "fontset": fontset,
            "font_probe": font_probe,
            # 隔离是恒定的（没有开关），如实说明而不是回显一个不存在的配置项
            "isolation": "恒定：-no-shell-escape + 独立临时目录 + 无网络代理 + 资源限制",
        },
        "pandoc": {
            "ok": pandoc_ok,
            "binary": pandoc_base.pandoc_binary(settings),
            "version": pandoc_base.pandoc_version(settings) if pandoc_ok else "",
        },
        "providers": providers,
        "default_teacher_provider": settings.providers.default_teacher_provider,
        "provider_capabilities": ctx.registry.capabilities(),
        "taskpacks": ctx.taskpacks.list_specs(),
        "budget": {
            "paid_calls_enabled": settings.budget.paid_calls_enabled,
            # 局域网会话只需要知道"能不能用"，不需要知道主人的预算额度
            "total_cents": _scoped_budget(request, settings.budget.total_cents),
            "per_call_cents": _scoped_budget(request, settings.budget.per_call_cents),
            "max_calls": settings.budget.max_calls,
            "max_seconds": settings.budget.max_seconds,
        },
        "worker": {
            "mode": settings.worker.mode,
            "max_jobs": settings.worker.max_jobs,
            "lease_seconds": settings.worker.lease_seconds,
        },
        "data_dir": _scoped_data_dir(request, str(settings.data_dir)),
    }


def _scoped_session(request: Request) -> bool:
    """这个请求是不是来自局域网会话（有项目范围）。"""
    return bool(getattr(request.state, "pairing_project", None))


def _scoped_data_dir(request: Request, value: str) -> str:
    """局域网会话不暴露本机路径。"""
    return "" if _scoped_session(request) else value


def _scoped_budget(request: Request, value: int) -> int:
    """局域网会话不暴露主人的预算额度。"""
    return 0 if _scoped_session(request) else int(value)


@router.get("/api/system/pairing")
def pairing_info(
    request: Request,
    project_id: str = "",
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    """获取配对码。

    只有本机（回环）请求可以拿到配对码；局域网其它设备必须凭码访问。
    码是**项目级**的：要给某个项目签发码，必须指定 project_id。
    """
    pairing = getattr(request.app.state, "pairing", None)
    if pairing is None:
        return {"required": False, "code": "", "expires_in": 0, "project_id": ""}
    client = request.client
    host = (client.host if client else "") or ""
    is_local = host in {"127.0.0.1", "::1", "localhost", "testclient"}
    # 只有本机请求才会在过期时轮换配对码；
    # 局域网设备的轮询不能把用户正在看的码换掉。
    state = pairing.current(project_id, rotate_if_expired=is_local)
    if state["required"] and not is_local:
        # 非本机请求不能直接领取配对码
        return {"required": True, "code": "", "expires_in": 0, "project_id": project_id}
    return state


# 兑换失败次数限制（按来源 IP）：配对码空间很大（32^8），
# 但仍然限一下速率，避免有人拿它当口令爆破器。
_REDEEM_FAILURES: dict[str, list[float]] = {}
_REDEEM_MAX_FAILURES = 10
_REDEEM_WINDOW_SECONDS = 60


def _client_key(request: Request) -> str:
    client = request.client
    host = (client.host if client else "") or "unknown"
    return host


def _too_many_failures(key: str) -> bool:
    import time as _time

    now = _time.time()
    hits = [t for t in _REDEEM_FAILURES.get(key, []) if now - t < _REDEEM_WINDOW_SECONDS]
    _REDEEM_FAILURES[key] = hits
    return len(hits) >= _REDEEM_MAX_FAILURES


def _clear_failures(key: str) -> None:
    _REDEEM_FAILURES.pop(key, None)


def _record_failure(key: str) -> None:
    import time as _time

    _REDEEM_FAILURES.setdefault(key, []).append(_time.time())


@router.post("/api/system/pairing/redeem")
def pairing_redeem(
    request: Request, payload: dict, ctx: AppContext = Depends(get_ctx)
) -> dict:
    """用配对码兑换项目级会话。

    局域网设备拿它解锁的项目 id 与一个会话令牌；
    之后所有请求都要带这个令牌（X-Godfather-Session 头）。
    """
    from fastapi import HTTPException

    pairing = getattr(request.app.state, "pairing", None)
    code = str((payload or {}).get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="请输入配对码")
    if pairing is None:
        raise HTTPException(status_code=400, detail="本机未启用配对，无需配对码")
    key = _client_key(request)
    session = pairing.redeem(code)
    if session:
        # 成功就清零失败计数：
        # 否则一台设备连发 10 个错码，就能把全网（共用 NAT 出口 IP）的正确码也挡住。
        _clear_failures(key)
    else:
        _record_failure(key)
        if _too_many_failures(key):
            raise HTTPException(
                status_code=429,
                detail=f"失败次数过多，请 {_REDEEM_WINDOW_SECONDS} 秒后再试",
            )
        raise HTTPException(status_code=401, detail="配对码无效或已过期")
    project = ctx.db.query_one(
        "SELECT id, name FROM projects WHERE id = ?", (session["project_id"],)
    )
    return {
        "token": session["token"],
        "project_id": session["project_id"],
        "project_name": (project or {}).get("name", ""),
        "expires_at": session["expires_at"],
    }


@router.post("/api/system/pairing/rotate")
def pairing_rotate(
    request: Request,
    payload: dict | None = None,
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    """重新生成某个项目的配对码（只有本机可以）。"""
    pairing = getattr(request.app.state, "pairing", None)
    if pairing is None:
        raise HTTPException(status_code=400, detail="本机未启用配对")
    client = request.client
    host = (client.host if client else "") or ""
    if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        return JSONResponse(status_code=403, content={"detail": "只有本机可以重新生成配对码"})
    project_id = str((payload or {}).get("project_id") or "")
    if not project_id:
        raise HTTPException(status_code=400, detail="缺少 project_id")
    if not ctx.project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    code = pairing.issue_code(project_id)
    # 旧码作废的同时，把旧会话也清掉：码换了就意味着之前的设备不该继续访问
    pairing.revoke_project_sessions(project_id)
    return {
        "required": True,
        "code": code,
        "expires_in": pairing.ttl_seconds,
        "project_id": project_id,
    }


@router.get("/api/system/pairing/sessions")
def pairing_sessions(
    request: Request,
    project_id: str = "",
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    """列出已配对的设备会话（只有本机可见）。"""
    pairing = getattr(request.app.state, "pairing", None)
    if pairing is None:
        return {"sessions": []}
    client = request.client
    host = (client.host if client else "") or ""
    if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        return JSONResponse(status_code=403, content={"detail": "只有本机可以查看会话"})
    return {"sessions": pairing.sessions(project_id)}


# --------------------------------------------------------------------------
# 项目
# --------------------------------------------------------------------------
@router.post("/api/projects")
def create_project(
    request: Request, payload: ProjectCreate, ctx: AppContext = Depends(get_ctx)
) -> dict:
    # 局域网会话只解锁一个项目：不允许它新建项目
    # （新建的项目它也拿不到配对码，只会留下一个谁都进不去的空项目）。
    if getattr(request.state, "pairing_project", None):
        raise HTTPException(
            status_code=403,
            detail="局域网会话不能新建项目：请在运行 Godfather 的电脑上创建",
        )
    if not ctx.taskpacks.has(payload.taskpack):
        available = ", ".join(ctx.taskpacks.ids()) or "无"
        raise HTTPException(
            status_code=400,
            detail=f"未挂载任务包：{payload.taskpack}（当前已启用：{available}）",
        )
    project_id = new_id("prj")
    ctx.db.insert(
        "projects",
        {
            "id": project_id,
            "name": payload.name,
            "taskpack": payload.taskpack,
            "description": payload.description,
            "active_model": "",
            "policy_version": "",
            "settings_json": "{}",
            "status": "created",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        },
    )
    policy_engine.save_policy(
        ctx.db,
        project_id,
        policy_engine.parse_policy("", "policy-001"),
        status="active",
        notes="初始化默认策略包",
    )
    return {"id": project_id, "name": payload.name, "taskpack": payload.taskpack}


@router.get("/api/projects")
def list_projects(request: Request, ctx: AppContext = Depends(get_ctx)) -> list[dict]:
    """项目列表。

    局域网会话只解锁了一个项目，就只能看到那一个项目
    （否则配对码就等于拿到了所有项目的入口）。
    """
    scoped = getattr(request.state, "pairing_project", None)
    if scoped:
        rows = ctx.db.query_all(
            "SELECT * FROM projects WHERE id = ? ORDER BY created_at DESC", (scoped,)
        )
    else:
        rows = ctx.db.query_all("SELECT * FROM projects ORDER BY created_at DESC")
    return rows


@router.get("/api/projects/{project_id}")
def get_project(project_id: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    project = ctx.project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    project["settings"] = json.loads(project.pop("settings_json") or "{}")
    return project


@router.patch("/api/projects/{project_id}")
def update_project(project_id: str, payload: ProjectUpdate, ctx: AppContext = Depends(get_ctx)) -> dict:
    if not ctx.project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    values: dict[str, Any] = {"updated_at": now_iso()}
    if payload.name is not None:
        values["name"] = payload.name
    if payload.description is not None:
        values["description"] = payload.description
    if payload.active_model is not None:
        values["active_model"] = payload.active_model
    if payload.settings is not None:
        values["settings_json"] = canonical_json(payload.settings)
    ctx.db.update("projects", project_id, values)
    return get_project(project_id, ctx)


@router.delete("/api/projects/{project_id}")
def delete_project(
    project_id: str, request: Request, ctx: AppContext = Depends(get_ctx)
) -> dict:
    # 局域网会话不能删项目：删除会连带清掉磁盘上的产物与训练数据。
    if getattr(request.state, "pairing_project", None):
        raise HTTPException(
            status_code=403,
            detail="局域网会话不能删除项目：请在运行 Godfather 的电脑上操作",
        )
    """删除项目：连带清掉没有外键的记录与磁盘上的产物文件。

    provider_calls 没有外键约束，不清会变成孤儿记录；
    产物文件按内容寻址存在 artifacts 目录里，不清会一直占磁盘。
    """
    if not ctx.project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    artifacts = ctx.db.query_all("SELECT id, path FROM artifacts WHERE project_id = ?", (project_id,))
    ctx.db.execute("DELETE FROM provider_calls WHERE project_id = ?", (project_id,))
    ctx.db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    removed = 0
    # 产物文件按内容寻址存放，删掉不再被任何产物引用的文件
    store_root = Path(ctx.settings.artifacts_dir)
    referenced = {
        row["path"]
        for row in ctx.db.query_all("SELECT DISTINCT path FROM artifacts")
    }
    for artifact in artifacts:
        candidate = Path(artifact["path"])
        try:
            candidate = candidate.resolve()
            store_root = store_root.resolve()
        except OSError:
            continue
        if not str(candidate).startswith(str(store_root)):
            continue
        if str(candidate) in referenced:
            continue
        try:
            candidate.unlink()
            removed += 1
        except OSError:
            continue
    return {"deleted": project_id, "artifacts_removed": removed}


@router.get("/api/projects/{project_id}/overview")
def project_overview(project_id: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    """首页：处理进度、失败原因、花费、待回答问题、下一步建议。"""
    if not ctx.project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    state = controller_actions.analyze_project(ctx.db, project_id)
    from ..budget import get_state

    budget_state = get_state(ctx.db, project_id, ctx.settings)
    recent_generations = ctx.db.query_all(
        "SELECT id, document_id, mode, status, provider, model, tex_artifact_id, pdf_artifact_id, "
        "created_at, updated_at, stats_json FROM generations "
        "WHERE project_id = ? ORDER BY created_at DESC LIMIT 5",
        (project_id,),
    )
    for generation in recent_generations:
        generation["stats"] = json.loads(generation.pop("stats_json") or "{}")

    next_action = controller_actions.deterministic_next_action(ctx.db, project_id)
    active_jobs = ctx.db.query_all(
        "SELECT id, type, status, stage, progress_done, progress_total, created_at FROM jobs "
        "WHERE project_id = ? AND status IN ('queued','running') ORDER BY created_at",
        (project_id,),
    )
    return {
        "project": get_project(project_id, ctx),
        "counts": state["counts"],
        "validation_by_layer": state["validation_by_layer"],
        "jobs": state["jobs"],
        "active_jobs": active_jobs,
        "recent_generations": recent_generations,
        "open_questions": state["open_questions"],
        "budget": budget_state.to_dict(),
        "next_action": next_action,
        "policy": ctx.policy_for(project_id).to_dict(),
    }


# --------------------------------------------------------------------------
# 材料（导入）
# --------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/materials")
async def upload_material(
    project_id: str,
    kind: str = Form("markdown"),
    filename: str = Form(""),
    dialect: str = Form("pandoc-markdown"),
    ctx: AppContext = Depends(get_ctx),
    file: UploadFile = File(...),
) -> dict:
    project = ctx.project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    if kind not in MATERIAL_KINDS:
        raise HTTPException(status_code=400, detail=f"未知材料类型：{kind}")
    # M6: 上传要有大小上限 —— 原来整块读进内存，
    # 局域网开放时任意大文件就能把服务内存打爆。
    max_bytes = int(getattr(ctx.settings.storage, "max_upload_bytes", 32 * 1024 * 1024))
    limit_text = (
        f"{max_bytes // (1024 * 1024)} MB" if max_bytes >= 1024 * 1024 else f"{max_bytes} 字节"
    )
    declared = file.headers.get("content-length")
    if declared:
        try:
            if int(declared) > max_bytes:
                raise HTTPException(status_code=413, detail=f"文件过大：超过上限 {limit_text}")
        except ValueError:
            pass
    # 分块读并累计：整块 read() 会把超大文件一次性读进内存，
    # 客户端不报 content-length 时就能把服务内存打爆。
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail=f"文件过大：超过上限 {limit_text}")
        chunks.append(chunk)
    payload = b"".join(chunks)
    if not payload:
        raise HTTPException(status_code=400, detail="空文件")
    name = filename or (file.filename or "upload.bin")
    media_type = MATERIAL_KINDS[kind]
    artifact = ctx.store.put_bytes(
        project_id,
        "asset" if kind == "asset" else kind,
        payload,
        media_type=media_type,
        meta={"filename": name, "kind": kind},
    )

    document_id = None
    if kind in {"markdown", "corpus", "dataset"}:
        task_type = str(project.get("taskpack") or "markdown_to_latex")
        spec = ctx.taskpacks.get_optional(task_type)
        if task_type == "markdown_to_latex" or spec is None or not callable(spec.ingest_material):
            source_id = new_id("src")
            ctx.db.insert(
                "source_documents",
                {
                    "id": source_id,
                    "project_id": project_id,
                    "filename": name,
                    "media_type": media_type,
                    "sha256": artifact["revision"],
                    "revision": 1,
                    "artifact_id": artifact["id"],
                    "size_bytes": artifact["size_bytes"],
                    "dialect": dialect or "pandoc-markdown",
                    "status": "imported",
                    "meta_json": "{}",
                    "created_at": now_iso(),
                },
            )
            parsed = parse_markdown_document(
                ctx,
                project_id,
                artifact_id=artifact["id"],
                title=Path(name).stem,
                dialect=dialect or "pandoc-markdown",
            )
            document_id = parsed["document_id"]
            ctx.db.update("source_documents", source_id, {"status": "parsed"})
            ctx.db.update("documents", document_id, {"source_document_id": source_id})
        else:
            parsed = spec.ingest_material(
                ctx,
                project_id=project_id,
                artifact_id=artifact["id"],
                title=Path(name).stem,
                dialect=dialect or "",
            )
            document_id = parsed.get("document_id")
            doc_row = ctx.db.query_one("SELECT source_document_id FROM documents WHERE id = ?", (document_id,)) if document_id else None
            source_id = (doc_row or {}).get("source_document_id")
        return {
            "artifact": artifact,
            "source_document_id": source_id,
            "document_id": document_id,
            "parse": {k: v for k, v in parsed.items() if k != "context"},
        }

    if kind.startswith("reference_"):
        reference_id = new_id("ref")
        ctx.db.insert(
            "reference_examples",
            {
                "id": reference_id,
                "project_id": project_id,
                "title": Path(name).stem,
                "md_artifact_id": artifact["id"] if kind == "reference_md" else None,
                "tex_artifact_id": artifact["id"] if kind == "reference_tex" else None,
                "pdf_artifact_id": artifact["id"] if kind == "reference_pdf" else None,
                "notes": "",
                "scope": "project",
                "created_at": now_iso(),
            },
        )
        return {"artifact": artifact, "reference_id": reference_id}

    return {"artifact": artifact}


@router.get("/api/projects/{project_id}/materials")
def list_materials(project_id: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    sources = ctx.db.query_all(
        "SELECT * FROM source_documents WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
    )
    for source in sources:
        source["meta"] = json.loads(source.pop("meta_json") or "{}")
    references = ctx.db.query_all(
        "SELECT * FROM reference_examples WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
    )
    artifacts = ctx.store.list_by_project(project_id)
    return {"source_documents": sources, "reference_examples": references, "artifacts": artifacts}


# --------------------------------------------------------------------------
# 文档
# --------------------------------------------------------------------------
@router.get("/api/projects/{project_id}/documents")
def list_documents(project_id: str, ctx: AppContext = Depends(get_ctx)) -> list[dict]:
    docs = ctx.db.query_all(
        "SELECT * FROM documents WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
    )
    if not docs:
        return []
    revs = ctx.db.query_all(
        "SELECT dr.document_id, dr.revision, dr.blocks_json "
        "FROM document_revisions dr "
        "INNER JOIN (SELECT document_id, MAX(revision) AS max_rev FROM document_revisions GROUP BY document_id) latest "
        "ON latest.document_id = dr.document_id AND latest.max_rev = dr.revision"
    )
    rev_map = {r["document_id"]: r for r in revs}
    for doc in docs:
        rev = rev_map.get(doc["id"])
        doc["revision"] = int(rev["revision"]) if rev else 0
        try:
            doc["block_count"] = len(json.loads(rev["blocks_json"] or "[]")) if rev else 0
        except Exception:  # noqa: BLE001
            doc["block_count"] = 0
    return docs


@router.get("/api/documents/{document_id}")
def get_document(document_id: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    document = ctx.db.query_one("SELECT * FROM documents WHERE id = ?", (document_id,))
    if not document:
        raise HTTPException(status_code=404, detail="文档不存在")
    revisions = ctx.db.query_all(
        "SELECT id, revision, source_revision, created_at FROM document_revisions WHERE document_id = ? ORDER BY revision DESC",
        (document_id,),
    )
    latest = ctx.db.query_one(
        "SELECT * FROM document_revisions WHERE document_id = ? ORDER BY revision DESC LIMIT 1", (document_id,)
    )
    blocks = json.loads(latest["blocks_json"] or "[]") if latest else []
    context = json.loads(latest["context_json"] or "{}") if latest else {}
    generations = ctx.db.query_all(
        "SELECT id, mode, status, pdf_artifact_id, tex_artifact_id, created_at FROM generations "
        "WHERE document_id = ? ORDER BY created_at DESC",
        (document_id,),
    )
    return {
        "document": document,
        "revisions": revisions,
        "blocks": blocks,
        "context": context,
        "generations": generations,
    }


@router.post("/api/projects/{project_id}/documents")
def create_document(project_id: str, payload: DocumentCreate, ctx: AppContext = Depends(get_ctx)) -> dict:
    if not ctx.project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    artifact = ctx.store.get(payload.artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="产物不存在")
    if artifact.get("project_id") and artifact["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="产物不存在")
    parsed = parse_markdown_document(
        ctx, project_id, artifact_id=payload.artifact_id, title=payload.title
    )
    return parsed


# --------------------------------------------------------------------------
# 策略包
# --------------------------------------------------------------------------
@router.get("/api/projects/{project_id}/policies")
def list_policies(project_id: str, ctx: AppContext = Depends(get_ctx)) -> list[dict]:
    return policy_engine.list_policies(ctx.db, project_id)


@router.patch("/api/projects/{project_id}/policies/{policy_id}")
def activate_policy(project_id: str, policy_id: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    """把某个历史策略版本设为当前启用版本（其余置为 draft）。"""
    if not ctx.project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    row = ctx.db.query_one(
        "SELECT * FROM policies WHERE id = ? AND project_id = ?",
        (policy_id, project_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="策略版本不存在")
    ctx.db.execute(
        "UPDATE policies SET status = 'draft' WHERE project_id = ? AND status = 'active'",
        (project_id,),
    )
    ctx.db.update("policies", policy_id, {"status": "active"})
    ctx.db.update("projects", project_id, {"policy_version": row["version"], "updated_at": now_iso()})
    return {"id": policy_id, "project_id": project_id, "version": row["version"], "status": "active"}


@router.get("/api/projects/{project_id}/policies/active")
def active_policy(project_id: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    return ctx.policy_for(project_id).to_dict()


@router.post("/api/projects/{project_id}/policies")
def save_policy(project_id: str, payload: dict, ctx: AppContext = Depends(get_ctx)) -> dict:
    if not ctx.project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    text = payload.get("yaml")
    if text is None:
        package = policy_engine.parse_policy("", payload.get("version", "policy-002"))
        for pref in payload.get("preferences") or []:
            if not isinstance(pref, dict):
                raise HTTPException(status_code=400, detail="preferences 必须是对象数组")
            try:
                priority = int(pref.get("priority", 50))
                confidence = float(pref.get("confidence", 0.5))
            except (TypeError, ValueError):
                # 类型错误要返回 400，不能变成 500 并把内部异常回显给客户端
                raise HTTPException(status_code=400, detail="priority 必须是整数，confidence 必须是数字") from None
            package.preferences.append(
                policy_engine.Preference(
                    pref_id=pref.get("pref_id") or f"pref-{len(package.preferences) + 1:03d}",
                    content=pref.get("content", ""),
                    scope=pref.get("scope", "project"),
                    scope_value=pref.get("scope_value", ""),
                    conditions=pref.get("conditions", {}),
                    priority=priority,
                    confidence=confidence,
                    status=pref.get("status", "active"),
                    evidence=pref.get("evidence", []),
                    rule=pref.get("rule", {}),
                )
            )
    else:
        if not isinstance(text, str):
            raise HTTPException(status_code=400, detail="yaml 必须是字符串")
        package = policy_engine.parse_policy(text, payload.get("version", "policy-002"))
    saved = policy_engine.save_policy(
        ctx.db,
        project_id,
        package,
        status=payload.get("status", "active"),
        notes=payload.get("notes", ""),
    )
    return saved
