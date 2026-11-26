"""训练后端接口与配置。

设计要点（设计文档 §9）：
- 首版只实现一种训练后端，但接口要能容纳其它实现；
- 训练与推理串行调度，避免抢显存；
- 记录基座修订、分词器、chat template、随机种子、参数、依赖与数据版本；
- 取消与恢复语义单独说明：没有完整优化器状态时只能重新启动，不能冒充精确续训。
"""
from __future__ import annotations

import abc
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..util import canonical_json, new_id, now_iso


@dataclass
class TrainingConfig:
    """一次训练运行的完整配置（会被原样记录进 training_runs.params_json）。"""

    # 基座
    base_model: str = ""                 # 本地目录或 HuggingFace 名称
    base_revision: str = ""              # 精确的权重修订（commit hash）
    tokenizer_revision: str = ""         # 分词器修订
    chat_template: str = ""              # chat template 标识（记录用）
    context_length: int = 4096
    stop_words: list[str] = field(default_factory=list)

    # LoRA / QLoRA
    use_lora: bool = True
    load_in_4bit: bool = True            # QLoRA
    lora_rank: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    target_modules: list[str] = field(
        default_factory=lambda: ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]
    )

    # 优化
    max_seq_length: int = 2048
    per_device_train_batch_size: int = 1
    gradient_accumulation_steps: int = 16
    learning_rate: float = 2e-4
    num_train_epochs: float = 3.0
    warmup_ratio: float = 0.05
    weight_decay: float = 0.0
    lr_scheduler_type: str = "cosine"
    max_steps: int = -1
    logging_steps: int = 5
    save_steps: int = 100
    seed: int = 42
    gradient_checkpointing: bool = True
    optim: str = "paged_adamw_8bit"

    # 数据
    dataset_snapshot_id: str = ""
    train_split: str = "train"
    eval_split: str = "dev"
    max_train_samples: int = -1
    max_eval_samples: int = 32

    # 运行时
    backend: str = "local"
    device: str = "auto"                 # auto / cpu / cuda
    output_dir: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrainingConfig:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


@dataclass
class PreflightCheck:
    name: str
    ok: bool
    detail: str = ""
    fatal: bool = True

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "fatal": self.fatal}


@dataclass
class BackendCapabilities:
    name: str
    available: bool
    description: str = ""
    supports_qlora: bool = True
    supports_resume: bool = False
    device: str = "unknown"
    executable: str = ""
    version: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class TrainingBackend(abc.ABC):
    """训练后端：负责产出可执行命令与解析日志，实际执行由 runner 负责。"""

    name: str = "base"
    description: str = ""

    @abc.abstractmethod
    def capabilities(self) -> BackendCapabilities: ...

    @abc.abstractmethod
    def preflight(self, config: TrainingConfig) -> list[PreflightCheck]: ...

    @abc.abstractmethod
    def build_command(
        self,
        config: TrainingConfig,
        *,
        dataset_dir: Path,
        output_dir: Path,
        config_path: Path,
    ) -> list[str]: ...

    def parse_log_line(self, line: str) -> dict[str, Any] | None:
        """解析一行日志 → {level, message, metrics}；不识别则返回 None。"""
        return None

    # ---- 便捷实现 ----
    def write_backend_config(self, config: TrainingConfig, config_path: Path, dataset_dir: Path, output_dir: Path) -> None:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = config.to_dict()
        payload["dataset_dir"] = str(dataset_dir)
        payload["output_dir"] = str(output_dir)
        config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def python_executable() -> str:
        return sys.executable

    @staticmethod
    def env_for_run() -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "HF_HUB_OFFLINE": os.environ.get("HF_HUB_OFFLINE", "0"),
            "TRANSFORMERS_VERBOSITY": "error",
            "HF_HOME": os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface")),
        }
        # Windows 上 PyTorch/WinSock/CUDA/字体缓存初始化依赖这些基础系统变量（BUG-02：缺少时触发 WinError 10106）
        for win_key in (
            "SystemRoot",
            "WINDIR",
            "COMSPEC",
            "USERPROFILE",
            "LOCALAPPDATA",
            "APPDATA",
            "TEMP",
            "TMP",
            "HOMEDRIVE",
            "HOMEPATH",
            "ProgramData",
            "SystemDrive",
            "CUDA_VISIBLE_DEVICES",
            "CUDA_HOME",
            "CUDA_PATH",
            "TORCH_HOME",
        ):
            val = os.environ.get(win_key)
            if val:
                env[win_key] = val
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            if key in os.environ:
                env[key] = os.environ[key]
        return env


def new_run_id() -> str:
    return new_id("run")


def now() -> str:
    return now_iso()


def canonical(value: Any) -> str:
    return canonical_json(value)
