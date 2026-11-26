"""Cordis 标准服务契约：TaskPack（任务包）与 TrainingBackend（训练后端）注册表。

让通用本地微调框架彻底摆脱对特定领域（Markdown→LaTeX）的硬编码绑定：
新任务（例如代码审查、SQL 生成、通用指令微调）只需实现一个 TaskPackSpec 并通过
`ctx.taskpacks.register(spec)` 挂载到 Cordis 内核即可立即介入完整训练与评测闭环。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .kernel import Disposer, Service


@dataclass
class TaskPackSpec:
    """一个可热插拔的任务包契约。"""

    id: str
    name: str
    description: str
    version: str = "0.1.0"
    input_formats: tuple[str, ...] = (".md", ".txt")
    modes: list[dict[str, Any]] = field(default_factory=list)
    system_prompt: str = ""
    stop_sequences: list[str] = field(default_factory=list)
    capabilities: dict[str, Any] = field(default_factory=dict)

    # ---- 可插拔生命周期回调 ----
    ingest_material: Callable[..., dict[str, Any]] | None = None
    run_generation: Callable[..., dict[str, Any]] | None = None
    run_revision: Callable[..., dict[str, Any]] | None = None
    format_training_example: Callable[[dict[str, Any]], tuple[str, str, str]] | None = None
    score_prediction: Callable[..., dict[str, Any]] | None = None
    analyze_project: Callable[..., dict[str, Any]] | None = None
    default_job_kind: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "input_formats": list(self.input_formats),
            "modes": list(self.modes),
            "system_prompt": self.system_prompt,
            "stop_sequences": list(self.stop_sequences),
            "capabilities": dict(self.capabilities),
            "default_job_kind": self.default_job_kind,
        }


class TaskPackRegistry(Service):
    """Cordis 服务：任务包注册表（`ctx.taskpacks`）。"""

    service_name = "taskpacks"

    def __init__(self) -> None:
        self._specs: dict[str, TaskPackSpec] = {}

    def register(self, spec: TaskPackSpec) -> Disposer:
        self._specs[spec.id] = spec

        def _unregister() -> None:
            if self._specs.get(spec.id) is spec:
                self._specs.pop(spec.id, None)

        return _unregister

    def get(self, task_type: str) -> TaskPackSpec:
        if task_type not in self._specs:
            available = ", ".join(self._specs) or "无"
            raise KeyError(f"未挂载任务包 '{task_type}'（当前已启用：{available}）")
        return self._specs[task_type]

    def get_optional(self, task_type: str) -> TaskPackSpec | None:
        return self._specs.get(task_type)

    def has(self, task_type: str) -> bool:
        return task_type in self._specs

    def ids(self) -> list[str]:
        return list(self._specs.keys())

    def list_specs(self) -> list[dict[str, Any]]:
        return [spec.to_public_dict() for spec in self._specs.values()]


class TrainingBackendRegistry(Service):
    """Cordis 服务：训练后端注册表（`ctx.training_backends`）。"""

    service_name = "training_backends"

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self._backends: dict[str, Any] = initial if initial is not None else {}

    def register(self, backend: Any) -> Disposer:
        name = getattr(backend, "name", "")
        if not name:
            raise ValueError("训练后端缺少 name 属性")
        self._backends[name] = backend

        def _unregister() -> None:
            if self._backends.get(name) is backend:
                self._backends.pop(name, None)

        return _unregister

    def get(self, name: str) -> Any:
        if name not in self._backends:
            raise KeyError(f"未知训练后端：{name}（可用：{', '.join(self._backends) or '无'}）")
        return self._backends[name]

    def names(self) -> list[str]:
        return list(self._backends.keys())

    def list_capabilities(self) -> list[dict[str, Any]]:
        return [b.capabilities().to_dict() for b in self._backends.values()]
