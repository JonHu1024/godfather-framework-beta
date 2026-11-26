"""Provider 注册表 + 教师调用服务（缓存、预算、审计的统一入口）。"""
from __future__ import annotations

import json
from typing import Any

from ..budget import BudgetExceeded, cache_key, check_and_reserve, settle
from ..db import Database
from ..events import emit
from ..util import canonical_json
from .base import (
    COST_FREE,
    COST_KNOWN,
    COST_UNKNOWN,
    Provider,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)
from .manual import ManualImportProvider
from .ollama import OllamaProvider
from .openai_compatible import OpenAICompatibleProvider


class ProviderRegistry:
    def __init__(self, settings: Any):
        self.settings = settings
        self._providers: dict[str, Provider] = {}
        self.configure_from_settings(settings)

    def configure_from_settings(self, settings: Any) -> None:
        """按当前 Settings 同步内置 Provider 实例（支持热更新配置）。"""
        self.settings = settings
        ollama_cfg = settings.providers.ollama
        if ollama_cfg.enabled:
            self._providers["ollama"] = OllamaProvider(
                base_url=ollama_cfg.base_url,
                timeout_seconds=ollama_cfg.timeout_seconds,
                default_model=ollama_cfg.teacher_model,
            )
        else:
            self._providers.pop("ollama", None)

        oa_cfg = getattr(settings.providers, "openai_compatible", None)
        if oa_cfg and oa_cfg.enabled:
            self._providers["openai_compatible"] = OpenAICompatibleProvider(
                base_url=oa_cfg.base_url,
                api_key=oa_cfg.api_key,
                default_model=oa_cfg.teacher_model,
                timeout_seconds=oa_cfg.timeout_seconds,
                max_tokens=oa_cfg.max_tokens,
                strip_think_tags=oa_cfg.strip_think_tags,
                pricing_known=oa_cfg.pricing_known,
                input_price_per_1m_cents=oa_cfg.input_price_per_1m_cents,
                output_price_per_1m_cents=oa_cfg.output_price_per_1m_cents,
                display_name=oa_cfg.display_name,
            )
        else:
            self._providers.pop("openai_compatible", None)

        if settings.providers.manual.enabled:
            self._providers["manual_import"] = ManualImportProvider()
        else:
            self._providers.pop("manual_import", None)

    def register(self, name: str, provider: Provider):
        """注册 Provider 并返回 Cordis 兼容清理句柄。"""
        self._providers[name] = provider

        def _unregister() -> None:
            if self._providers.get(name) is provider:
                self._providers.pop(name, None)

        return _unregister

    def unregister(self, name: str) -> None:
        self._providers.pop(name, None)

    def get(self, name: str) -> Provider:
        if name not in self._providers:
            raise KeyError(f"未启用的 provider: {name}（已启用: {', '.join(self._providers) or '无'}）")
        return self._providers[name]

    def has(self, name: str) -> bool:
        return name in self._providers

    def names(self) -> list[str]:
        return list(self._providers)

    def capabilities(self) -> list[dict]:
        out = []
        for provider in self._providers.values():
            cap = provider.capabilities().to_dict()
            cap["default_model"] = getattr(provider, "default_model", "") or ""
            out.append(cap)
        return out

    def list_models(self, name: str) -> list[str]:
        provider = self.get(name)
        getter = getattr(provider, "list_models", None)
        if callable(getter):
            return list(getter() or [])
        return []

    def health(self) -> list[dict]:
        out = []
        for name, provider in self._providers.items():
            ok, message = provider.healthcheck()
            out.append({"provider": name, "ok": ok, "message": message})
        return out


class TeacherService:
    """把 provider 调用包成：缓存 → 预算预留 → 调用 → 结算 → 审计。"""

    def __init__(self, db: Database, settings: Any, registry: ProviderRegistry):
        self.db = db
        self.settings = settings
        self.registry = registry

    def call(
        self,
        project_id: str,
        provider_name: str,
        request: ProviderRequest,
        *,
        model: str = "",
        policy_version: str = "",
        prompt_version: str = "",
        template_version: str = "",
        validator_version: str = "",
        use_cache: bool = True,
        run_id: str | None = None,
    ) -> ProviderResult:
        provider = self.registry.get(provider_name)
        if model:
            request.metadata = {**request.metadata, "model": model}

        # 缓存键要带上**权重摘要**：同一个 tag 重新 pull 过之后，
        # 权重变了 → 旧缓存必须失效（否则训练样本里混着另一份权重的答案）。
        model_digest = ""
        getter = getattr(provider, "model_digest", None)
        if callable(getter):
            try:
                model_digest = getter(model or request.metadata.get("model", "") or "")
            except Exception:  # noqa: BLE001
                model_digest = ""

        key = cache_key(
            provider=provider_name,
            model=model or request.metadata.get("model", ""),
            prompt=request.prompt,
            system=request.system,
            policy_version=policy_version,
            prompt_version=prompt_version,
            template_version=template_version,
            validator_version=validator_version,
            project_id=project_id,
            model_digest=model_digest,
            extra=request.metadata.get("cache_extra"),
        )

        if use_cache:
            cached = self.db.query_one(
                "SELECT * FROM provider_calls WHERE cache_key = ? AND project_id = ? AND status = 'succeeded' "
                "ORDER BY created_at DESC LIMIT 1",
                (key, project_id),
            )
            if cached:
                response = json.loads(cached["response_json"] or "{}")
                result = ProviderResult(
                    ok=True,
                    content=response.get("content", ""),
                    provider=provider_name,
                    model=cached["model"],
                    request_id=cached["request_id"],
                    usage=ProviderUsage(
                        input_tokens=cached["input_tokens"],
                        output_tokens=cached["output_tokens"],
                        cost_cents=0.0,
                        cost_status=cached["cost_status"],
                        usage_visible=(
                            cached["cost_status"] in (COST_FREE, COST_KNOWN)
                            or bool(cached["input_tokens"] or cached["output_tokens"])
                        ),
                    ),
                    raw={"cached_from": cached["id"], "model_digest": model_digest},
                    cached=True,
                )
                emit(
                    self.db,
                    project_id=project_id,
                    run_id=run_id,
                    stage="provider",
                    message="命中缓存，跳过教师调用",
                    payload={"cache_key": key, "provider": provider_name},
                )
                return result

        estimated, cost_status = provider.estimate_cost(request)
        try:
            call = check_and_reserve(
                self.db,
                project_id,
                self.settings,
                estimated_cents=estimated,
                cost_status=cost_status,
            )
        except BudgetExceeded as exc:
            emit(
                self.db,
                project_id=project_id,
                run_id=run_id,
                stage="provider",
                message=f"预算检查未通过：{exc.reason}",
                level="warning",
                payload=exc.detail,
            )
            return ProviderResult(
                ok=False,
                provider=provider_name,
                error=f"预算检查未通过：{exc.reason}",
                error_kind="budget",
            )

        self.db.update(
            "provider_calls",
            call["id"],
            {
                "provider": provider_name,
                "model": model or request.metadata.get("model", ""),
                "cache_key": key,
                "request_json": canonical_json(
                    {
                        "prompt": request.prompt[:8000],
                        "system": (request.system or "")[:2000],
                        "temperature": request.temperature,
                        "max_tokens": request.max_tokens,
                        "json_mode": request.json_mode,
                        "seed": request.seed,
                        "model_digest": model_digest,
                        "metadata": {k: v for k, v in request.metadata.items() if k != "cache_extra"},
                    }
                ),
            },
        )

        try:
            result = provider.generate(request)
        except Exception as exc:
            # 结算预留：否则 pending 行会永久占用额度
            settle(
                self.db,
                call,
                status="failed",
                actual_cents=0.0,
                cost_status=COST_UNKNOWN,
                duration_ms=0,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

        if result.model and result.model != call.get("model"):
            # 记录实际使用的模型（例如本地没有请求的模型时自动切换）
            self.db.update("provider_calls", call["id"], {"model": result.model})

        settle(
            self.db,
            call,
            status="succeeded" if result.ok else "failed",
            actual_cents=result.usage.cost_cents,
            cost_status=result.usage.cost_status,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            duration_ms=result.duration_ms,
            error=result.error,
            response={"content": result.content, "raw": result.raw},
        )
        emit(
            self.db,
            project_id=project_id,
            run_id=run_id,
            stage="provider",
            message=(
                f"教师调用完成：{provider_name}/{model or '默认模型'}"
                f"（{'成功' if result.ok else '失败'}，{result.duration_ms} ms）"
            ),
            cost={
                "cost_cents": result.usage.cost_cents,
                "cost_status": result.usage.cost_status,
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
            },
            payload={"provider": provider_name, "request_id": result.request_id, "cached": result.cached},
            level="info" if result.ok else "warning",
        )
        return result
