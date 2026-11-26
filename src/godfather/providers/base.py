"""Provider 统一接口（设计文档 §7.1）。

约定：
- `capabilities()` 描述能力矩阵（文本/图像/结构化输出/上下文上限/并发/用量可见性/工具/协议）。
- `healthcheck()` 探测可用性，不产生计费调用。
- `estimate_cost()` 返回费用估算与"费用状态"（已知价 / 未知）。
- `generate()` 返回标准化内容、用量、费用状态与错误；不直接抛业务异常。
- `cancel()` 尽力取消在途请求（本地 provider 通常不支持）。
"""
from __future__ import annotations

import abc
from dataclasses import asdict, dataclass, field
import re
from typing import Any

COST_FREE = "free"
COST_KNOWN = "known"
COST_UNKNOWN = "unknown"
COST_NOT_APPLICABLE = "not_applicable"

_THINK_BLOCK_RE = re.compile(r"<think>([\s\S]*?)</think>", re.IGNORECASE)
_THINK_UNCLOSED_RE = re.compile(r"<think>[\s\S]*$", re.IGNORECASE)


def strip_thinking(text: str) -> tuple[str, str]:
    """剥离推理模型（DeepSeek-R1 / Qwen3 等）输出中的 <think>...</think> 思考段（BUG-08）。

    返回 `(正文, 被剥离的思考内容)`；若因 max_tokens 截断导致缺少闭合 `</think>`，
    也会清理残留前缀以免污染 LaTeX 正文或 JSON 解析。
    """
    raw = str(text or "")
    thoughts: list[str] = [m.group(1).strip() for m in _THINK_BLOCK_RE.finditer(raw) if m.group(1).strip()]
    cleaned = _THINK_BLOCK_RE.sub("", raw)
    unclosed = _THINK_UNCLOSED_RE.search(cleaned)
    if unclosed:
        tail = unclosed.group(0)[len("<think>"):].strip()
        if tail:
            thoughts.append(tail)
        cleaned = cleaned[: unclosed.start()]
    return cleaned.strip(), "\n\n".join(thoughts)


@dataclass
class Capabilities:
    provider: str
    name: str
    protocol: str = "native"
    text: bool = True
    image: bool = False
    structured_output: bool = False
    context_limit: int = 8192
    concurrency: int = 1
    usage_visible: bool = False
    tools: list[str] = field(default_factory=list)
    supports_cancel: bool = False
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProviderRequest:
    prompt: str
    system: str | None = None
    images: list[str] = field(default_factory=list)
    temperature: float = 0.2
    max_tokens: int = 2048
    json_mode: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int | None = None
    # 采样种子：固定它才能复现同一次调用。
    # DeepInfer 把"确定性采样"当成推理框架的基本承诺 ——
    # 我们这边对应的是"同���份输入 + 同一个模型 + 同一个种子 = 同一个输出"，
    # 否则教师产出的训练样本无法追溯，评测的差值也分不清是模型差异还是采样噪声。
    seed: int | None = None


@dataclass
class ProviderUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_cents: float = 0.0
    cost_status: str = COST_UNKNOWN
    usage_visible: bool = False


@dataclass
class ProviderResult:
    ok: bool
    content: str = ""
    provider: str = ""
    model: str = ""
    request_id: str = ""
    usage: ProviderUsage = field(default_factory=ProviderUsage)
    raw: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    error_kind: str = ""      # timeout / http / parse / auth / budget / unknown
    duration_ms: int = 0
    cached: bool = False
    tool_events: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "content": self.content,
            "provider": self.provider,
            "model": self.model,
            "request_id": self.request_id,
            "usage": asdict(self.usage),
            "error": self.error,
            "error_kind": self.error_kind,
            "duration_ms": self.duration_ms,
            "cached": self.cached,
            "tool_events": self.tool_events,
        }


class Provider(abc.ABC):
    name: str = "base"
    protocol: str = "native"

    @abc.abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abc.abstractmethod
    def healthcheck(self) -> tuple[bool, str]: ...

    @abc.abstractmethod
    def estimate_cost(self, request: ProviderRequest) -> tuple[float, str]: ...

    @abc.abstractmethod
    def generate(self, request: ProviderRequest) -> ProviderResult: ...

    def cancel(self, request_id: str) -> bool:
        return False
