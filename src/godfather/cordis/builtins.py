"""Cordis 内置插件与外部目录插件装载入口。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .kernel import CordisContext, PluginMeta

if TYPE_CHECKING:
    from ..context import AppContext


def _make_provider_plugin(provider_name: str, display_name: str, description: str):
    meta = PluginMeta(
        name=f"provider-{provider_name}",
        display_name=display_name,
        version="0.2.0",
        description=description,
        kind="provider",
        inject=("app", "providers"),
        source="builtin",
        hot_reloadable=True,
    )

    def _apply(ctx: CordisContext) -> None:
        app_ctx = ctx.inject("app")
        registry = ctx.inject("providers")
        if provider_name == "ollama":
            cfg = app_ctx.settings.providers.ollama
            from ..providers.ollama import OllamaProvider

            inst = OllamaProvider(
                base_url=cfg.base_url,
                timeout_seconds=cfg.timeout_seconds,
                default_model=cfg.teacher_model,
            )
            ctx.effect(registry.register("ollama", inst))
        elif provider_name == "openai_compatible":
            cfg = app_ctx.settings.providers.openai_compatible
            if cfg.enabled:
                from ..providers.openai_compatible import OpenAICompatibleProvider

                inst = OpenAICompatibleProvider(
                    base_url=cfg.base_url,
                    api_key=cfg.api_key,
                    default_model=cfg.teacher_model,
                    timeout_seconds=cfg.timeout_seconds,
                    max_tokens=cfg.max_tokens,
                    strip_think_tags=cfg.strip_think_tags,
                    pricing_known=cfg.pricing_known,
                    input_price_per_1m_cents=cfg.input_price_per_1m_cents,
                    output_price_per_1m_cents=cfg.output_price_per_1m_cents,
                    display_name=cfg.display_name,
                )
                ctx.effect(registry.register("openai_compatible", inst))
        elif provider_name == "manual_import":
            from ..providers.manual import ManualImportProvider

            ctx.effect(registry.register("manual_import", ManualImportProvider()))

    _apply.PLUGIN_META = meta  # type: ignore[attr-defined]
    return _apply


def mount_builtin_plugins(ctx: AppContext) -> None:
    """将内置 Provider、TrainingBackend、TaskPack 与 `./plugins` 外部插件统一挂载到 Cordis。"""
    from ..taskpacks.general_sft import plugin as general_sft_plugin
    from ..taskpacks.latex import plugin as latex_plugin

    kernel = ctx.kernel
    # 若已挂载核心 taskpack 且处理器就绪，仅重新同步动态发现即可（幂等调用，供 drain_jobs / create_app 共用）
    if "taskpack-markdown-to-latex" in kernel._fibers and kernel.get_fiber("taskpack-markdown-to-latex").state == "active":
        return

    provider_specs = (
        ("ollama", "Ollama 本地推理提供方", "对接本机或局域网 Ollama HTTP API（免费、可固定采样种子与权重 digest）。", bool(ctx.settings.providers.ollama.enabled)),
        ("openai_compatible", "OpenAI 兼容远程 API 提供方", "对接 DeepSeek / MiniMax / Qwen / Moonshot / vLLM 等远程兼容 API。", bool(ctx.settings.providers.openai_compatible.enabled)),
        ("manual_import", "离线手工导入提供方", "导出提示包并在任意外部聊天窗口完成回答后再导回结果。", bool(ctx.settings.providers.manual.enabled)),
    )
    for pname, display, desc, enabled in provider_specs:
        kernel.mount(_make_provider_plugin(pname, display, desc), name=f"provider-{pname}", enable=enabled)

    kernel.mount(latex_plugin, name="taskpack-markdown-to-latex", enable=True)
    kernel.mount(general_sft_plugin, name="taskpack-general-sft", enable=True)

    plugins_dir = getattr(ctx.settings, "plugins_dir", None)
    if plugins_dir and plugins_dir.is_dir():
        kernel.discover_directory(plugins_dir)
