"""训练与模型中心（M1）。

包含：训练后端、数据准备、运行器、资源调度、导出、评测、模型版本管理。
"""
from .backends import BACKENDS, get_backend, list_backends
from .base import TrainingConfig
from .datasets import prepare_dataset
from .evaluate import evaluate_models
from .export import export_run
from .models import activate, list_models, rollback
from .resources import gpu_state
from .runner import cancel_run, run_training, start_run

__all__ = [
    "BACKENDS",
    "TrainingConfig",
    "activate",
    "cancel_run",
    "evaluate_models",
    "export_run",
    "get_backend",
    "gpu_state",
    "list_backends",
    "list_models",
    "prepare_dataset",
    "rollback",
    "run_training",
    "start_run",
]
