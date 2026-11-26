"""FastAPI 应用工厂。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import api_router
from .api.scope import resolve_target_projects
from .context import AppContext
from .pairing import PairingManager
from .settings import Settings, load_settings

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"


def _is_loopback(request: Request) -> bool:
    """判断请求是否来自本机（127.0.0.1 / ::1）。"""
    client = request.client
    if client is None:
        return True
    host = (client.host or "").lower()
    return host in {"127.0.0.1", "::1", "localhost", "testclient"}


def register_job_handlers(ctx: AppContext) -> None:
    from .cordis.builtins import mount_builtin_plugins

    mount_builtin_plugins(ctx)

    # ---- M1：训练、导出、评测 ----
    def _handle_training(job):
        from .training.base import TrainingConfig
        from .training.runner import run_training

        payload = job.payload
        config = TrainingConfig.from_dict(payload.get("config") or {})
        return run_training(
            ctx,
            job,
            project_id=payload["project_id"],
            config=config,
            run_id=payload["run_id"],
        )

    def _handle_export(job):
        from .training.export import export_run

        payload = job.payload
        return export_run(
            ctx,
            job,
            project_id=payload["project_id"],
            run_id=payload["run_id"],
            quantization=payload.get("quantization") or "q4_k_m",
            model_name=payload.get("model_name", ""),
            template=payload.get("template", ""),
            system_prompt=payload.get("system_prompt", ""),
        )

    def _handle_evaluation(job):
        from .training.evaluate import evaluate_models

        payload = job.payload
        return evaluate_models(
            ctx,
            job,
            project_id=payload["project_id"],
            candidate_model=payload["candidate_model"],
            baseline_model=payload.get("baseline_model", ""),
            snapshot_id=payload["snapshot_id"],
            split=payload.get("split", "dev"),
            limit=int(payload.get("limit", 32)),
            run_id=payload.get("run_id", ""),
        )

    ctx.queue.register("training.run", _handle_training)
    ctx.queue.register("training.export", _handle_export)
    ctx.queue.register("training.evaluate", _handle_evaluation)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    ctx = AppContext.create(settings)
    register_job_handlers(ctx)

    app = FastAPI(
        title="Godfather",
        description="通用本地模型微调与热插拔任务工作台（Cordis 内核）",
        version="0.2.0",
    )
    app.state.ctx = ctx
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 只要监听地址不是回环，就必须启用配对 —— 不能只依赖 config 里的布尔量，
    # 否则 `--host 0.0.0.0` 之类的方式会开出一个完全没有鉴权的局域网入口。
    bind_host = (getattr(settings.server, "host", "") or "").strip()
    exposed = bool(settings.server.lan_bind) or bind_host not in ("", "127.0.0.1", "localhost", "::1")
    if exposed and not settings.server.lan_bind:
        print(
            f"[安全] 监听地址 {bind_host} 不是回环地址，已自动启用配对凭证",
        )
    pairing = PairingManager(
        db=ctx.db,
        ttl_seconds=settings.server.pairing_code_ttl_seconds,
        enabled=exposed,
    )
    app.state.pairing = pairing

    # 免检路径：配对信息本身与兑换端点（否则没法登录）
    PAIRING_EXEMPT = ("/api/system/pairing", "/api/system/pairing/redeem")

    @app.middleware("http")
    async def _pairing_guard(request: Request, call_next):
        """局域网访问需要配对凭证；本机回环请求不受限。

        项目级访问控制：会话令牌绑定一个项目，
        访问别的项目的资源一律 403（scope_mismatch）。
        """
        if not pairing.enabled:
            return await call_next(request)
        path = request.url.path
        if path in PAIRING_EXEMPT:
            return await call_next(request)
        if not path.startswith("/api"):
            # 局域网访问时连接口文档也不开放，避免泄露完整接口面
            if path in ("/docs", "/redoc", "/openapi.json") and not _is_loopback(request):
                return JSONResponse(status_code=401, content={"detail": "需要配对凭证", "code": "pairing_required"})
            return await call_next(request)
        if request.method == "OPTIONS":
            # CORS 预检不带自定义头（浏览器不让预检带），
            # 直接拦掉的话跨域调用会彻底不可用。预检本身不返回数据，放行没有风险。
            return await call_next(request)
        if _is_loopback(request):
            return await call_next(request)

        # 候选凭证要**逐个试**：只取第一个非空的话，
        # "过期的 session 头 + 正确的配对码"会整体 401（看起来像码无效）。
        candidates = [
            request.headers.get("x-godfather-session"),
            request.headers.get("x-godfather-pairing"),
            request.query_params.get("session"),
            request.query_params.get("pairing"),
        ]
        unlocked = None
        for candidate in candidates:
            if not candidate:
                continue
            unlocked = pairing.verify(candidate)
            if unlocked is not None:
                break
            # 也接受直接用配对码（方便 curl / 简单客户端）：
            # 码兑换成会话后会一直有效到会话过期。
            session = pairing.redeem(candidate)
            if session:
                unlocked = session["project_id"]
                break
        if unlocked is None:
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "需要配对凭证：请在运行 Godfather 的电脑上查看**该项目的**配对码，并在手机端输入",
                    "code": "pairing_required",
                },
            )

        if not _is_loopback(request) and (
            path.startswith("/api/models-access") or path.startswith("/api/plugins")
        ):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "仅限本机控制台管理模型密钥与服务端插件，局域网配对端无权访问",
                    "code": "local_only",
                },
            )

        # 会话绑定了一个项目：请求涉及的**所有**项目都必须就是它。
        # 只比对其中一个会被绕过：按 id 取对象的端点
        # （/api/artifacts/{id} 等）只要带上 ?project_id=<自己的项目>
        # 就能读到别的项目的数据 —— 所以候选集合里任何一项对不上就拒绝。
        targets = resolve_target_projects(ctx, path, dict(request.query_params))
        # unlocked 为空 = 没解析出会话（fail-closed，不放行）
        if not unlocked:
            return JSONResponse(
                status_code=401,
                content={"detail": "需要配对凭证", "code": "pairing_required"},
            )
        mismatched = [t for t in targets if t != unlocked]
        if mismatched:
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "当前配对码只解锁了另一个项目；请改用该项目的配对码",
                    "code": "scope_mismatch",
                    "unlocked_project_id": unlocked,
                },
            )
        # 记下来，供端点做"列表按会话范围过滤"
        request.state.pairing_project = unlocked
        return await call_next(request)

    @app.on_event("startup")
    async def _startup() -> None:
        if settings.worker.mode == "embedded":
            ctx.queue.start_embedded(count=max(1, settings.worker.max_jobs))
        ctx.db.execute(
            "INSERT INTO events(project_id, run_id, job_id, stage, done, total, ts, artifact_ids_json,"
            " cost_json, message, payload_json) VALUES(NULL,NULL,NULL,'startup',NULL,NULL,?,'[]','{}',?, '{}')",
            (
                __import__("godfather.util", fromlist=["now_iso"]).now_iso(),
                "服务已启动",
            ),
        )

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        ctx.queue.stop()
        ctx.kernel.shutdown()

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):  # pragma: no cover
        # 详细堆栈只进日志，不回显给客户端（避免泄露绝对路径与内部结构）
        import logging
        import traceback

        logging.getLogger("godfather.api").error(
            "未处理的异常 %s %s: %s\n%s",
            request.method,
            request.url.path,
            exc,
            traceback.format_exc(),
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "服务内部错误，详情见服务端日志"},
        )

    app.include_router(api_router)

    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

    return app


app = None


def get_app() -> FastAPI:
    global app
    if app is None:
        app = create_app()
    return app
