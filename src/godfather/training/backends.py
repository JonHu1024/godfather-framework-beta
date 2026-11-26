"""训练后端实现。

- `local`：自有 PEFT/transformers 训练脚本（backend=local）。
- `llamafactory`：LLaMA-Factory CLI 适配器（backend=llamafactory）。

两者都只负责"给出可执行命令 + 解析日志"，实际执行由 runner 统一处理，
这样取消、超时、资源锁、日志落库的行为完全一致。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .base import BackendCapabilities, PreflightCheck, TrainingBackend, TrainingConfig

STEP_RE = re.compile(r"^\{.*\}$")


class LocalBackend(TrainingBackend):
    """自有训练脚本（transformers + peft）。"""

    name = "local"
    description = "自有 SFT 脚本（transformers + PEFT，可用 QLoRA；CPU 也能跑）"

    def capabilities(self) -> BackendCapabilities:
        executable = TrainingBackend.python_executable()
        versions = {}
        for module in ("torch", "transformers", "peft"):
            try:
                imported = __import__(module)
                versions[module] = getattr(imported, "__version__", "unknown")
            except Exception:
                versions[module] = ""
        device = "cpu"
        if versions.get("torch"):
            try:
                import torch

                device = "cuda" if torch.cuda.is_available() else "cpu"
            except Exception:
                device = "cpu"
        available = bool(versions.get("torch") and versions.get("transformers"))
        return BackendCapabilities(
            name=self.name,
            available=available,
            description=self.description,
            supports_qlora=True,
            supports_resume=False,
            device=device,
            executable=executable,
            version=f"torch {versions.get('torch', '?')} / transformers {versions.get('transformers', '?')} / peft {versions.get('peft', '?')}",
            notes=(
                "无 CUDA 时仍可训练，但速度很慢；4bit 会自动降级并在日志中说明"
                if device == "cpu"
                else "检测到 CUDA，可使用 QLoRA（4bit）"
            ),
        )

    def preflight(self, config: TrainingConfig) -> list[PreflightCheck]:
        checks: list[PreflightCheck] = []
        caps = self.capabilities()
        checks.append(
            PreflightCheck(
                name="训练依赖",
                ok=caps.available,
                detail=caps.version if caps.available else "缺少 torch / transformers，请先 pip install",
            )
        )
        base = Path(config.base_model) if config.base_model else None
        if base is None or not base.exists():
            checks.append(
                PreflightCheck(name="基座模型", ok=False, detail=f"目录不存在：{config.base_model or '(未指定)'}")
            )
        else:
            checks.append(
                PreflightCheck(name="基座模型", ok=True, detail=str(base))
            )
        if config.load_in_4bit and caps.device != "cuda":
            checks.append(
                PreflightCheck(
                    name="4bit 量化",
                    ok=True,
                    detail="当前无 CUDA，4bit 会降级为普通精度训练（已记录）",
                    fatal=False,
                )
            )
        checks.append(
            PreflightCheck(
                name="显存",
                ok=True,
                detail=f"设备 {caps.device}；训练与推理会串行调度",
                fatal=False,
            )
        )
        return checks

    def build_command(
        self,
        config: TrainingConfig,
        *,
        dataset_dir: Path,
        output_dir: Path,
        config_path: Path,
    ) -> list[str]:
        self.write_backend_config(config, config_path, dataset_dir, output_dir)
        return [
            TrainingBackend.python_executable(),
            "-m",
            "godfather.training.scripts.train_local",
            "--config",
            str(config_path),
        ]

    def parse_log_line(self, line: str) -> dict | None:
        line = line.strip()
        if not line.startswith("{"):
            return None
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        event = payload.get("event")
        if event == "step":
            return {"level": "info", "message": f"step {payload.get('step')} loss={payload.get('loss')}", "metrics": payload}
        if event == "log":
            return {"level": payload.get("level", "info"), "message": payload.get("message", "")}
        if event == "fatal":
            return {"level": "error", "message": payload.get("message", "训练失败")}
        if event == "done":
            return {"level": "info", "message": "训练完成", "metrics": payload.get("metrics", {})}
        return None


class LlamaFactoryBackend(TrainingBackend):
    """LLaMA-Factory CLI 适配器（设计文档指定的优先实现）。"""

    name = "llamafactory"
    description = "LLaMA-Factory CLI（SFT + LoRA/QLoRA，多种训练流程）"

    def _executable(self) -> str:
        found = shutil.which("llamafactory-cli") or shutil.which("llamafactory")
        return found or ""

    def capabilities(self) -> BackendCapabilities:
        executable = self._executable()
        version = ""
        if executable:
            try:
                proc = subprocess.run(
                    [executable, "version"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    check=False,
                )
                version = (proc.stdout or proc.stderr or "").strip().splitlines()[-1] if (proc.stdout or proc.stderr) else ""
            except Exception:
                version = ""
        return BackendCapabilities(
            name=self.name,
            available=bool(executable),
            description=self.description,
            supports_qlora=True,
            supports_resume=True,
            device="external",
            executable=executable,
            version=version,
            notes="需要先安装 LLaMA-Factory（pip install llamafactory）",
        )

    def preflight(self, config: TrainingConfig) -> list[PreflightCheck]:
        caps = self.capabilities()
        checks = [
            PreflightCheck(
                name="llamafactory-cli",
                ok=caps.available,
                detail=caps.executable or "未找到（pip install llamafactory）",
            )
        ]
        base = Path(config.base_model) if config.base_model else None
        checks.append(
            PreflightCheck(
                name="基座模型",
                ok=bool(base and base.exists()),
                detail=str(base) if base else "(未指定)",
            )
        )
        return checks

    def write_backend_config(self, config: TrainingConfig, config_path: Path, dataset_dir: Path, output_dir: Path) -> None:
        import yaml

        train_split = config.train_split
        payload = {
            "model_name_or_path": config.base_model,
            "stage": "sft",
            "do_train": True,
            "finetuning_type": "lora" if config.use_lora else "full",
            "dataset": f"godfather_{train_split}",
            "dataset_dir": str(dataset_dir),
            "template": config.chat_template or "qwen",
            "cutoff_len": config.max_seq_length,
            "overwrite_cache": True,
            "preprocessing_num_workers": 4,
            "output_dir": str(output_dir),
            "logging_steps": config.logging_steps,
            "save_steps": config.save_steps,
            "plot_loss": True,
            "overwrite_output_dir": True,
            "per_device_train_batch_size": config.per_device_train_batch_size,
            "gradient_accumulation_steps": config.gradient_accumulation_steps,
            "learning_rate": config.learning_rate,
            "num_train_epochs": config.num_train_epochs,
            "warmup_ratio": config.warmup_ratio,
            "lr_scheduler_type": config.lr_scheduler_type,
            "weight_decay": config.weight_decay,
            "seed": config.seed,
            "bf16": False,
            "fp16": False,
            "report_to": "none",
        }
        if config.use_lora:
            payload.update(
                {
                    "lora_rank": config.lora_rank,
                    "lora_alpha": config.lora_alpha,
                    "lora_dropout": config.lora_dropout,
                    "lora_target": "all",
                }
            )
        if config.load_in_4bit:
            payload["quantization_bit"] = 4
        if config.base_revision:
            payload["model_revision"] = config.base_revision
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def build_command(
        self,
        config: TrainingConfig,
        *,
        dataset_dir: Path,
        output_dir: Path,
        config_path: Path,
    ) -> list[str]:
        self.write_backend_config(config, config_path, dataset_dir, output_dir)
        executable = self._executable()
        if not executable:
            raise RuntimeError("未找到 llamafactory-cli")
        return [executable, "train", str(config_path)]

    def parse_log_line(self, line: str) -> dict | None:
        stripped = line.strip()
        if "'loss':" in stripped or "'loss': " in stripped:
            return {"level": "info", "message": stripped[:300]}
        if stripped.startswith("{'loss'") or stripped.startswith('{"loss"'):
            return {"level": "info", "message": stripped[:300]}
        return None


BACKENDS: dict[str, TrainingBackend] = {
    LocalBackend.name: LocalBackend(),
    LlamaFactoryBackend.name: LlamaFactoryBackend(),
}


def register_backend(backend: TrainingBackend):
    """动态注册训练后端，返回 Cordis 兼容注销闭包。"""
    BACKENDS[backend.name] = backend

    def _unregister() -> None:
        if BACKENDS.get(backend.name) is backend:
            BACKENDS.pop(backend.name, None)

    return _unregister


def get_backend(name: str) -> TrainingBackend:
    if name not in BACKENDS:
        raise KeyError(f"未知训练后端：{name}（可用：{', '.join(BACKENDS)}）")
    return BACKENDS[name]


def list_backends() -> list[dict]:
    return [backend.capabilities().to_dict() for backend in BACKENDS.values()]
