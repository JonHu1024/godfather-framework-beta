"""OpenAI 兼容远程 API Provider（支持 DeepSeek、MiniMax、Qwen、Moonshot、vLLM 等）。"""
from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from .base import (
    COST_KNOWN,
    COST_UNKNOWN,
    Capabilities,
    Provider,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
    strip_thinking,
)


def _normalize_base_url(base_url: str) -> str:
    url = (base_url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/models"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


class OpenAICompatibleProvider(Provider):
    """标准 OpenAI `/v1/chat/completions` & `/v1/models` 兼容客户端。"""

    name = "openai_compatible"
    protocol = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str = "https://api.deepseek.com/v1",
        api_key: str = "",
        default_model: str = "deepseek-chat",
        timeout_seconds: int = 180,
        max_tokens: int = 4096,
        strip_think_tags: bool = True,
        pricing_known: bool = False,
        input_price_per_1m_cents: float = 0.0,
        output_price_per_1m_cents: float = 0.0,
        display_name: str = "远程 OpenAI 兼容 API",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = _normalize_base_url(base_url)
        self.api_key = (api_key or "").strip()
        self.default_model = (default_model or "").strip()
        self.timeout_seconds = int(timeout_seconds or 180)
        self.max_tokens = int(max_tokens or 4096)
        self.strip_think_tags = bool(strip_think_tags)
        self.pricing_known = bool(pricing_known)
        self.input_price_per_1m_cents = float(input_price_per_1m_cents or 0.0)
        self.output_price_per_1m_cents = float(output_price_per_1m_cents or 0.0)
        self.display_name = display_name or "远程 OpenAI 兼容 API"
        self.extra_headers = dict(extra_headers or {})

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def capabilities(self) -> Capabilities:
        return Capabilities(
            provider=self.name,
            name=self.display_name,
            protocol=self.protocol,
            text=True,
            image=True,
            structured_output=True,
            context_limit=65536,
            concurrency=2,
            usage_visible=True,
            tools=[],
            supports_cancel=False,
            notes=f"端点 {self.base_url or '(未配置)'}；默认模型 {self.default_model or '(未配置)'}",
        )

    def list_models(self) -> list[str]:
        if not self.base_url:
            return []
        try:
            response = httpx.get(
                f"{self.base_url}/models",
                headers=self._headers(),
                timeout=8.0,
            )
            if response.status_code >= 400:
                return []
            payload = response.json()
        except Exception:
            return []
        items = payload.get("data") or payload.get("models") or []
        out: list[str] = []
        for item in items:
            if isinstance(item, dict):
                mid = item.get("id") or item.get("name")
                if mid:
                    out.append(str(mid))
            elif isinstance(item, str) and item:
                out.append(item)
        return sorted(set(out))

    def healthcheck(self) -> tuple[bool, str]:
        if not self.base_url:
            return False, "未配置 API Base URL"
        if not self.api_key and not any(
            h in self.base_url for h in ("127.0.0.1", "localhost", "::1")
        ):
            return False, "未配置 API Key"
        try:
            response = httpx.get(
                f"{self.base_url}/models",
                headers=self._headers(),
                timeout=8.0,
            )
            if response.status_code == 401:
                return False, "HTTP 401 鉴权失败（API Key 无效或已过期）"
            if response.status_code == 403:
                return False, "HTTP 403 权限不足（请检查 API Key 或账户余额）"
            if response.status_code == 404:
                # 少数兼容端点（如定制网关）不暴露 /models，仍可通过 /chat/completions 对话
                if self.default_model:
                    return True, f"端点已连接（未暴露 /models；当前默认模型：{self.default_model}）"
                return False, "端点未暴露 /models（HTTP 404），请填好默认模型后点击测试对话"
            if response.status_code >= 400:
                return False, f"HTTP {response.status_code}: {response.text[:160]}"
            models = self.list_models()
            count_note = f"共 {len(models)} 个模型" if models else "连接成功"
            if self.default_model:
                return True, f"远程 API 就绪（{count_note}；默认 {self.default_model}）"
            return True, f"远程 API 就绪（{count_note}）"
        except httpx.TimeoutException:
            return False, f"连接 {self.base_url} 超时"
        except Exception as exc:  # noqa: BLE001
            return False, f"无法连接 {self.base_url}：{exc}"

    def _compute_cost(self, input_tokens: int, output_tokens: int) -> tuple[float, str]:
        if not self.pricing_known:
            return 0.0, COST_UNKNOWN
        cents = (
            (max(0, input_tokens) / 1_000_000.0) * self.input_price_per_1m_cents
            + (max(0, output_tokens) / 1_000_000.0) * self.output_price_per_1m_cents
        )
        return round(cents, 4), COST_KNOWN

    def estimate_cost(self, request: ProviderRequest) -> tuple[float, str]:
        approx_in = max(1, (len(request.prompt or "") + len(request.system or "")) // 3)
        approx_out = min(int(request.max_tokens or self.max_tokens), 1500)
        return self._compute_cost(approx_in, approx_out)

    def _build_messages(self, request: ProviderRequest) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        if request.images:
            content: list[dict[str, Any]] = [{"type": "text", "text": request.prompt}]
            for img in request.images:
                url = img if str(img).startswith(("http://", "https://", "data:")) else f"data:image/png;base64,{img}"
                content.append({"type": "image_url", "image_url": {"url": url}})
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": request.prompt})
        return messages

    def generate(self, request: ProviderRequest) -> ProviderResult:
        request_id = uuid.uuid4().hex
        model = str(request.metadata.get("model") or self.default_model or "").strip()
        if not self.base_url:
            return ProviderResult(
                ok=False,
                provider=self.name,
                model=model,
                request_id=request_id,
                error="未配置远程 API Base URL",
                error_kind="auth",
            )
        if not model:
            return ProviderResult(
                ok=False,
                provider=self.name,
                model="",
                request_id=request_id,
                error="未指定远程模型名称（请在「模型接入」或任务参数中填写模型 ID）",
                error_kind="parse",
            )

        payload: dict[str, Any] = {
            "model": model,
            "messages": self._build_messages(request),
            "temperature": request.temperature,
            "max_tokens": int(request.max_tokens or self.max_tokens),
        }
        if request.seed is not None:
            payload["seed"] = int(request.seed)
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}

        timeout = request.timeout_seconds or self.timeout_seconds
        started = time.time()
        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=timeout,
            )
            # 部分兼容端点不支持 response_format={"type":"json_object"}，遇到 400 自动回退一次普通文本输出
            if response.status_code == 400 and "response_format" in payload:
                fallback_payload = {k: v for k, v in payload.items() if k != "response_format"}
                response = httpx.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=fallback_payload,
                    timeout=timeout,
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
        except Exception as exc:  # noqa: BLE001
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
            err_kind = "auth" if response.status_code in {401, 403} else "http"
            return ProviderResult(
                ok=False,
                provider=self.name,
                model=model,
                request_id=request_id,
                error=f"HTTP {response.status_code}: {response.text[:400]}",
                error_kind=err_kind,
                duration_ms=duration_ms,
                raw={"status_code": response.status_code},
            )

        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            return ProviderResult(
                ok=False,
                provider=self.name,
                model=model,
                request_id=request_id,
                error=f"JSON 响应解析失败：{exc}",
                error_kind="parse",
                duration_ms=duration_ms,
            )

        choices = data.get("choices") or []
        message = (choices[0].get("message") or {}) if choices else {}
        raw_content = message.get("content") or ""
        if isinstance(raw_content, list):
            raw_content = "".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in raw_content
            )
        reasoning_field = message.get("reasoning_content") or message.get("reasoning") or ""

        clean_content = str(raw_content)
        inline_thinking = ""
        if self.strip_think_tags and not request.metadata.get("keep_thinking"):
            clean_content, inline_thinking = strip_thinking(clean_content)

        usage_raw = data.get("usage") or {}
        in_tok = int(usage_raw.get("prompt_tokens") or 0)
        out_tok = int(usage_raw.get("completion_tokens") or 0)
        cost_cents, cost_status = self._compute_cost(in_tok, out_tok)
        usage = ProviderUsage(
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_cents=cost_cents,
            cost_status=cost_status,
            usage_visible=bool(in_tok or out_tok),
        )
        return ProviderResult(
            ok=True,
            content=clean_content,
            provider=self.name,
            model=str(data.get("model") or model),
            request_id=str(data.get("id") or request_id),
            usage=usage,
            raw={
                "finish_reason": choices[0].get("finish_reason") if choices else None,
                "thinking_stripped": bool(inline_thinking),
                "thinking_chars": len(inline_thinking) + len(str(reasoning_field)),
                "seed": request.seed,
            },
            duration_ms=duration_ms,
        )
