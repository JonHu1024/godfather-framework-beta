"""Cordis 风格微内核与热插拔契约。"""
from __future__ import annotations

from .builtins import mount_builtin_plugins
from .contracts import (
    TaskPackRegistry,
    TaskPackSpec,
    TrainingBackendRegistry,
)
from .kernel import (
    CordisContext,
    CordisKernel,
    EffectScope,
    PluginFiber,
    PluginMeta,
    Service,
)

__all__ = [
    "CordisContext",
    "CordisKernel",
    "EffectScope",
    "PluginFiber",
    "PluginMeta",
    "Service",
    "TaskPackRegistry",
    "TaskPackSpec",
    "TrainingBackendRegistry",
    "mount_builtin_plugins",
]
