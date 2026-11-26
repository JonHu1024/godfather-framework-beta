"""Ollama Provider：本地推理（教师 mod 与学生 loc 都走这里）。"""
from __future__ import annotations

import time
from typing import Any
import uuid

import httpx

from .base import (
    COST_FREE,
    Capabilities,
    Provider,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
    strip_thinking,
)


class OllamaProvider(Provider):
    name = "ollama"
    protocol = "ollama"

    def __init__(self, base_url: str, timeout_seconds: int = 180, default_model: str = ""):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.default_model = default_model

    # ---- 能力 ----
    def capabilities(self) -> Capabilities:
        return Capabilities(
            provider=self.name,
            name="Ollama 本地推理",
            protocol=self.protocol,
            text=True,
            image=False,
            structured_output=True,
            context_limit=32768,
            concurrency=1,
            usage_visible=True,
            tools=[],
            supports_cancel=False,
            notes="本地运行，无外部费用；需自行保证 GPU 显存与模型已拉取",
        )

    def list_models(self) -> list[str]:
        return [name for name, _digest in self.list_models_with_digest().items()]

    def list_models_with_digest(self) -> dict[str, str]:
        """列出模型及其**权重摘要**（digest）。

        digest 是"这份权重到底是什么"的唯一凭据：
        同一个 tag（qwen3:0.6b）被重新 pull 过之后权重就变了，
        只记 tag 等于没记 —— 这正是 DeepInfer 里"固定模型修订"要解决的问题。
        """
        try:
            response = httpx.get(f"{self.base_url}/api/tags", timeout=5.0)
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return {}
        models = payload.get("models") or []
        out: dict[str, str] = {}
        for item in models:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            digest = item.get("digest") or ""
            details = item.get("details") or {}
            if not digest and isinstance(details, dict):
                digest = details.get("digest") or ""
            out[str(name)] = str(digest or "")
        return out

    def model_digest(self, model: str) -> str:
        """取某个模型的权重摘要（取不到就返回空串，不阻断调用）。"""
        if not model:
            return ""
        digests = self.list_models_with_digest()
        if model in digests:
            return digests[model]
        # 无 tag 的模型在 /api/tags 里会列成 name:latest
        base = model.split(":")[0]
        for name, digest in digests.items():
            if name.split(":")[0] == base:
                return digest
        return ""

    def resolve_model(self, requested: str) -> tuple[str, bool]:
        """请求的模型不存在时，回退到本机已安装的某个模型。

        返回 (实际使用的模型, 是否发生了切换)。切换会在调用审计里如实记录。
        """
        available = self.list_models()
        if not available:
            return requested, False
        if requested in available:
            return requested, False
        # 允许省略版本标签的情况（qwen3 → qwen3:0.6b）
        base = requested.split(":")[0]
        for name in available:
            if name.split(":")[0] == base:
                return name, True
        return available[0], True

    def healthcheck(self) -> tuple[bool, str]:
        try:
            response = httpx.get(f"{self.base_url}/api/tags", timeout=5.0)
            response.raise_for_status()
        except Exception as exc:
            return False, f"Ollama 不可达：{type(exc).__name__}: {exc}"
        models = self.list_models()
        if not models:
            return True, "Ollama 已连接，但本地还没有任何模型（请先 `ollama pull`）"
        return True, f"Ollama 已连接，本地模型 {len(models)} 个"

    def estimate_cost(self, request: ProviderRequest) -> tuple[float, str]:
        return 0.0, COST_FREE

    # ---- 生成 ----
    def generate(self, request: ProviderRequest) -> ProviderResult:
        model = request.metadata.get("model") or self.default_model
        if not model:
            return ProviderResult(
                ok=False,
                provider=self.name,
                error="未指定模型名称（metadata.model）",
                error_kind="config",
            )
        model, switched = self.resolve_model(model)
        if switched:
            request.metadata = {**request.metadata, "model": model}
        # 记录实际使用的权重摘要（切换模型后也要记切换后那份）
        model_digest = self.model_digest(model)
        options: dict = {
            "temperature": request.temperature,
            "num_predict": request.max_tokens,
        }
        # 固定种子 → 同输入同模型同输出（可复现）
        if request.seed is not None:
            options["seed"] = int(request.seed)
        payload: dict = {
            "model": model,
            "stream": False,
            "messages": [],
            "options": options,
        }
        if request.system:
            payload["messages"].append({"role": "system", "content": request.system})
        payload["messages"].append({"role": "user", "content": request.prompt})
        if request.json_mode:
            payload["format"] = "json"

        request_id = uuid.uuid4().hex
        started = time.time()
        try:
            response = httpx.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=request.timeout_seconds or self.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            return ProviderResult(
                ok=False,
                provider=self.name,
                model=model,
                request_id=request_id,
                error=f"请求超时：{exc}",
                error_kind="timeout",
                duration_ms=int((time.time() - started) * 1000),
            )
        except Exception as exc:
            return ProviderResult(
                ok=False,
                provider=self.name,
                model=model,
                request_id=request_id,
                error=f"{type(exc).__name__}: {exc}",
                error_kind="network",
                duration_ms=int((time.time() - started) * 1000),
            )

        duration_ms = int((time.time() - started) * 1000)
        if response.status_code >= 400:
            return ProviderResult(
                ok=False,
                provider=self.name,
                model=model,
                request_id=request_id,
                error=f"HTTP {response.status_code}: {response.text[:400]}",
                error_kind="http",
                duration_ms=duration_ms,
                raw={"status_code": response.status_code},
            )

        try:
            data = response.json()
        except Exception as exc:
            return ProviderResult(
                ok=False,
                provider=self.name,
                model=model,
                request_id=request_id,
                error=f"响应解析失败：{exc}",
                error_kind="parse",
                duration_ms=duration_ms,
            )

        message = data.get("message") or {}
        raw_content = message.get("content") or ""
        thinking_text = message.get("thinking") or ""
        content = str(raw_content)
        if not request.metadata.get("keep_thinking"):
            content, inline_thinking = strip_thinking(content)
            if inline_thinking:
                thinking_text = f"{thinking_text}\n{inline_thinking}".strip()
        usage = ProviderUsage(
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            cost_cents=0.0,
            cost_status=COST_FREE,
            usage_visible=True,
        )
        return ProviderResult(
            ok=True,
            content=content,
            provider=self.name,
            model=model,
            request_id=request_id,
            usage=usage,
            raw={
                "done_reason": data.get("done_reason"),
                "total_duration": data.get("total_duration"),
                "load_duration": data.get("load_duration"),
                "thinking_stripped": bool(thinking_text),
                "thinking_chars": len(str(thinking_text)),
                # 权重摘要：用来把这次产物绑定到"确切的那份权重"上
                "model_digest": model_digest,
                "seed": request.seed,
            },
            duration_ms=duration_ms,
        )

    def unload_model(self, model: str = "") -> dict[str, Any]:
        """主动卸载 Ollama 常驻模型（keep_alive=0）以释放显存供本地微调使用。"""
        targets = [model] if model else self.list_running_models()
        unloaded: list[str] = []
        errors: list[str] = []
        for name in targets:
            try:
                resp = httpx.post(
                    f"{self.base_url}/api/generate",
                    json={"model": name, "prompt": "", "keep_alive": 0, "stream": False},
                    timeout=10.0,
                )
                if resp.status_code < 400:
                    unloaded.append(name)
                else:
                    errors.append(f"{name}: HTTP {resp.status_code}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {exc}")
        return {"ok": not errors, "unloaded": unloaded, "errors": errors}

    def list_running_models(self) -> list[str]:
        """列出当前正加载在 Ollama 显存/内存中的模型。"""
        try:
            resp = httpx.get(f"{self.base_url}/api/ps", timeout=5.0)
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            return []
        out: list[str] = []
        for item in payload.get("models") or []:
            if isinstance(item, dict) and item.get("name"):
                out.append(str(item["name"]))
        return out
