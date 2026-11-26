"""Cordis 插件中心、TaskPack 发现与模型接入配置 API。

安全边界：涉及宿主 API Key 与插件装卸的修改端点在局域网配对模式下已被 `_pairing_guard` 限制为本机只读/禁入。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..context import AppContext
from ..providers.base import ProviderRequest
from ..settings import save_runtime_overrides
from .deps import get_ctx

router = APIRouter(tags=["plugins-and-models"])

PROVIDER_PRESETS = [
    {
        "id": "deepseek",
        "label": "DeepSeek 官方 API",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "notes": "deepseek-chat（V3）高性价比；deepseek-reasoner（R1）推理能力强，已自动剥离 <think> 段。",
    },
    {
        "id": "minimax",
        "label": "MiniMax 开放平台",
        "base_url": "https://api.minimax.chat/v1",
        "models": ["MiniMax-M2.5", "MiniMax-Text-01"],
        "notes": "支持超长上下文，建议在系统预算中打开「允许外部付费 API 调用」。",
    },
    {
        "id": "dashscope",
        "label": "阿里云百炼 / Qwen",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus", "qwen-max", "qwen3-235b-a22b"],
        "notes": "与 Qwen 本地微调基座同系，教师迁移分布一致性极佳。",
    },
    {
        "id": "moonshot",
        "label": "Kimi / Moonshot",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-32k", "kimi-k2-0711-preview"],
        "notes": "适合整章长文档结构分析与学术润色。",
    },
    {
        "id": "zhipu",
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4-air"],
        "notes": "OpenAI 兼容路径为 /api/paas/v4（不带 /v1）。",
    },
    {
        "id": "local_vllm",
        "label": "本地 vLLM / LM Studio / OneAPI",
        "base_url": "http://127.0.0.1:8000/v1",
        "models": [],
        "notes": "无需真实云端计费，api_key 可填 local-dummy。",
    },
]


class ModelsAccessUpdate(BaseModel):
    default_teacher_provider: str | None = None
    ollama: dict[str, Any] | None = None
    openai_compatible: dict[str, Any] | None = None
    manual: dict[str, Any] | None = None
    budget: dict[str, Any] | None = None
    latex: dict[str, Any] | None = None
    export: dict[str, Any] | None = None
    clear_api_key: bool = False


class ProviderTestRequest(BaseModel):
    provider: str = "openai_compatible"
    model: str = ""
    ping_chat: bool = False
    base_url: str | None = None
    api_key: str | None = None


class UnloadOllamaRequest(BaseModel):
    model: str = ""


def _require_local(request: Request) -> None:
    if getattr(request.state, "pairing_project", None):
        raise HTTPException(
            status_code=403,
            detail="仅限运行 Godfather 的本机控制台访问或修改模型密钥与插件配置",
        )


def _sync_provider_plugins(ctx: AppContext) -> None:
    """让 Settings 里的配置修改同步到 ProviderRegistry 与 Cordis provider fibers。"""
    ctx.registry.configure_from_settings(ctx.settings)
    kernel = ctx.kernel
    for pname, enabled in (
        ("ollama", bool(ctx.settings.providers.ollama.enabled)),
        ("openai_compatible", bool(ctx.settings.providers.openai_compatible.enabled)),
        ("manual_import", bool(ctx.settings.providers.manual.enabled)),
    ):
        fiber_name = f"provider-{pname}"
        fiber = kernel.get_fiber(fiber_name)
        if not fiber:
            continue
        if enabled:
            kernel.reload(fiber_name)
        else:
            kernel.disable(fiber_name)


# --------------------------------------------------------------------------
# Cordis 插件与任务包中心
# --------------------------------------------------------------------------
@router.get("/api/taskpacks")
def list_taskpacks(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return {
        "taskpacks": ctx.taskpacks.list_specs(),
        "default": "markdown_to_latex" if ctx.taskpacks.has("markdown_to_latex") else (ctx.taskpacks.ids()[0] if ctx.taskpacks.ids() else ""),
    }


@router.get("/api/plugins")
def list_plugins(request: Request, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    is_remote = bool(getattr(request.state, "pairing_project", None))
    fibers = ctx.kernel.list_fibers()
    if is_remote:
        for item in fibers:
            item["file_path"] = ""
    return {
        "fibers": fibers,
        "services": ctx.kernel.list_services(),
        "taskpacks": ctx.taskpacks.list_specs(),
        "backends": ctx.training_backends.list_capabilities(),
        "plugins_dir": "" if is_remote else str(ctx.settings.plugins_dir),
    }


@router.post("/api/plugins/discover")
def discover_plugins(request: Request, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    _require_local(request)
    pdir = ctx.settings.plugins_dir
    pdir.mkdir(parents=True, exist_ok=True)
    loaded = ctx.kernel.discover_directory(pdir)
    return {
        "discovered": [f.name for f in loaded],
        "fibers": ctx.kernel.list_fibers(),
        "taskpacks": ctx.taskpacks.list_specs(),
    }


@router.post("/api/plugins/{plugin_name}/enable")
def enable_plugin(plugin_name: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    _require_local(request)
    try:
        fiber = ctx.kernel.enable(plugin_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return {"fiber": fiber.to_dict(ctx.kernel), "taskpacks": ctx.taskpacks.list_specs()}


@router.post("/api/plugins/{plugin_name}/disable")
def disable_plugin(plugin_name: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    _require_local(request)
    if plugin_name == "taskpack-markdown-to-latex":
        # 允许停用演示热插拔，但返回清晰提示
        pass
    try:
        fiber = ctx.kernel.disable(plugin_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return {"fiber": fiber.to_dict(ctx.kernel), "taskpacks": ctx.taskpacks.list_specs()}


@router.post("/api/plugins/{plugin_name}/reload")
def reload_plugin(plugin_name: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    _require_local(request)
    try:
        fiber = ctx.kernel.reload(plugin_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"fiber": fiber.to_dict(ctx.kernel), "taskpacks": ctx.taskpacks.list_specs()}


# --------------------------------------------------------------------------
# 模型接入中心（BUG-03：远程 API + Ollama + 预算 + LaTeX 引擎配置）
# --------------------------------------------------------------------------
@router.get("/api/models-access")
def get_models_access(request: Request, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    _require_local(request)
    masked = ctx.settings.to_dict()
    ollama_provider = ctx.registry._providers.get("ollama")
    running_ollama = ollama_provider.list_running_models() if ollama_provider and hasattr(ollama_provider, "list_running_models") else []
    ollama_models = ollama_provider.list_models() if ollama_provider and hasattr(ollama_provider, "list_models") else []
    return {
        "default_teacher_provider": ctx.settings.providers.default_teacher_provider,
        "providers": masked["providers"],
        "budget": masked["budget"],
        "latex": masked["latex"],
        "export": masked["export"],
        "capabilities": ctx.registry.capabilities(),
        "health": ctx.registry.health(),
        "ollama_models": ollama_models,
        "running_ollama_models": running_ollama,
        "presets": PROVIDER_PRESETS,
    }


@router.put("/api/models-access")
def update_models_access(
    payload: ModelsAccessUpdate, request: Request, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    _require_local(request)
    patch: dict[str, Any] = {}
    providers_patch: dict[str, Any] = {}
    if payload.default_teacher_provider is not None:
        providers_patch["default_teacher_provider"] = payload.default_teacher_provider
    if payload.ollama is not None:
        providers_patch["ollama"] = dict(payload.ollama)
    if payload.manual is not None:
        providers_patch["manual"] = dict(payload.manual)
    if payload.openai_compatible is not None:
        oa_patch = dict(payload.openai_compatible)
        # 界面提交空字符串 api_key 代表保留现有已存密钥；只有显式 clear_api_key 才清空
        if not payload.clear_api_key and not str(oa_patch.get("api_key") or "").strip():
            oa_patch.pop("api_key", None)
        providers_patch["openai_compatible"] = oa_patch
    elif payload.clear_api_key:
        providers_patch["openai_compatible"] = {"api_key": ""}

    if providers_patch:
        patch["providers"] = providers_patch
    if payload.budget is not None:
        patch["budget"] = dict(payload.budget)
    if payload.latex is not None:
        patch["latex"] = dict(payload.latex)
    if payload.export is not None:
        patch["export"] = dict(payload.export)

    save_runtime_overrides(ctx.settings, patch)
    _sync_provider_plugins(ctx)
    return get_models_access(request, ctx)


@router.get("/api/models-access/models")
def list_provider_models(
    provider: str = "ollama", request: Request = None, ctx: AppContext = Depends(get_ctx)  # type: ignore[assignment]
) -> dict[str, Any]:
    if not ctx.registry.has(provider):
        return {"provider": provider, "enabled": False, "models": []}
    return {
        "provider": provider,
        "enabled": True,
        "models": ctx.registry.list_models(provider),
    }


@router.post("/api/models-access/test")
def test_provider_connection(
    payload: ProviderTestRequest, request: Request, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    _require_local(request)
    # 支持在保存之前先临时用表单里的 base_url / api_key 试连
    if payload.provider == "openai_compatible" and (payload.base_url or payload.api_key):
        from ..providers.openai_compatible import OpenAICompatibleProvider

        cfg = ctx.settings.providers.openai_compatible
        prov = OpenAICompatibleProvider(
            base_url=payload.base_url or cfg.base_url,
            api_key=(payload.api_key if payload.api_key else cfg.api_key),
            default_model=payload.model or cfg.teacher_model,
            timeout_seconds=min(30, cfg.timeout_seconds),
            max_tokens=128,
        )
    else:
        try:
            prov = ctx.registry.get(payload.provider)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    ok, message = prov.healthcheck()
    models = prov.list_models() if hasattr(prov, "list_models") else []
    chat_test = None
    if payload.ping_chat:
        res = prov.generate(
            ProviderRequest(
                prompt="请只回复字母 OK。",
                temperature=0.0,
                max_tokens=32,
                timeout_seconds=30,
                metadata={"model": payload.model} if payload.model else {},
            )
        )
        chat_test = {
            "ok": res.ok,
            "model": res.model,
            "content": res.content[:120],
            "duration_ms": res.duration_ms,
            "error": res.error,
        }
        if res.ok:
            ok = True
            message = f"对话测试通过（模型 {res.model}，耗时 {res.duration_ms} ms）"
        else:
            ok = False
            message = f"对话测试失败：{res.error}"
    return {
        "ok": ok,
        "provider": payload.provider,
        "message": message,
        "models": models,
        "chat_test": chat_test,
    }


@router.post("/api/models-access/ollama/unload")
def unload_ollama(
    payload: UnloadOllamaRequest, request: Request, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    _require_local(request)
    if not ctx.registry.has("ollama"):
        raise HTTPException(status_code=400, detail="Ollama 未启用")
    ollama_provider = ctx.registry.get("ollama")
    if not hasattr(ollama_provider, "unload_model"):
        raise HTTPException(status_code=400, detail="当前 Ollama Provider 不支持显存卸载")
    return ollama_provider.unload_model(model=payload.model)
