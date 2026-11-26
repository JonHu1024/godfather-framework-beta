"""自有 SFT 训练脚本（后端 backend=local）。

被 `python -m godfather.training.scripts.train_local --config config.json` 调用。
特点：
- 只依赖 transformers + peft（trl 可选）；不依赖 LLaMA-Factory；
- 全程把进度以 JSONL 形式打到 stdout（{"event": "step", ...}），便于上层解析；
- 只在 assistant 回答上计算 loss（prompt 部分 label 置 -100）；
- 4bit（QLoRA）在没有 bitsandbytes 或没有 CUDA 时**明确降级**并记录，不假装用了 QLoRA。

注意：这是真实的训练脚本，不接受 mock。无法训练时以非零退出码结束并说明原因。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

PROMPT_TEMPLATE = "### 指令\n{prompt}\n\n### 回答\n{answer}"


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def log(message: str, level: str = "info", **extra: Any) -> None:
    emit({"event": "log", "level": level, "message": message, **extra})


def dtype_kwargs(dtype) -> dict:
    """兼容 transformers 4.x（torch_dtype）与 5.x（dtype）的参数名。"""
    import inspect

    from transformers import AutoModelForCausalLM

    try:
        accepted = set(inspect.signature(AutoModelForCausalLM.from_pretrained).parameters)
    except (TypeError, ValueError):
        accepted = set()
    if "dtype" in accepted:
        return {"dtype": dtype}
    return {"torch_dtype": dtype}



def encode_pair(tokenizer, prompt_text: str, answer_text: str, max_len: int) -> tuple[list[int], list[int], bool]:
    """把 (prompt, answer) 编码成 (input_ids, labels, truncated)。

    只从答案上计算 loss；序列超长时截断 **prompt 的开头**（保留靠近答案的部分），
    否则答案会被切掉，训练会"成功但没有任何监督信号"（loss 恒为 0）。
    """
    prompt_ids = tokenizer(prompt_text, add_special_tokens=False)["input_ids"]
    answer_ids = tokenizer(answer_text, add_special_tokens=False)["input_ids"] if answer_text else []
    truncated = False

    if not answer_ids:
        # 空答案的样本没有可学的目标：交给调用方剔除。
        # 绝不能拿 prompt 的最后一个 token 当监督目标 —— 那等于教模型
        # "把输入的最后一个字符抄出来"，会污染正常样本。
        return list(prompt_ids), [-100] * len(prompt_ids), truncated

    if len(prompt_ids) + len(answer_ids) > max_len:
        keep = max(0, max_len - len(answer_ids))
        if keep < len(prompt_ids):
            prompt_ids = prompt_ids[-keep:] if keep else []
            truncated = True
        answer_ids = answer_ids[: max(0, max_len - len(prompt_ids))]

    # 答案末尾补结束符：没有结束符，模型在推理时会一路生成到上限，
    # 既拖慢评测又拉低质量。
    eos_id = getattr(tokenizer, "eos_token_id", None)
    if isinstance(eos_id, int) and eos_id >= 0 and (not answer_ids or answer_ids[-1] != eos_id):
        answer_ids = list(answer_ids) + [eos_id]

    input_ids = list(prompt_ids) + list(answer_ids)
    labels = [-100] * len(prompt_ids) + list(answer_ids)
    return input_ids, labels, truncated



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    dataset_dir = Path(config["dataset_dir"])
    output_dir = Path(config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    log("开始导入训练依赖")
    try:
        import torch
        from torch.utils.data import Dataset as TorchDataset
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            Trainer,
            TrainingArguments,
        )
    except Exception as exc:
        log(f"依赖缺失：{type(exc).__name__}: {exc}", level="error")
        emit({"event": "fatal", "message": f"缺少训练依赖：{exc}"})
        return 2

    base_model = config.get("base_model") or ""
    if not base_model:
        log("未指定 base_model", level="error")
        emit({"event": "fatal", "message": "未指定 base_model"})
        return 2
    if not Path(base_model).exists():
        log(f"基座模型目录不存在：{base_model}", level="error")
        emit({"event": "fatal", "message": f"基座模型不存在：{base_model}"})
        return 2

    device_request = config.get("device", "auto")
    if device_request == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    else:
        device = device_request
    log(f"计算设备：{device}（torch {torch.__version__}）")

    # ---- 4bit / QLoRA 判定 ----
    use_4bit = bool(config.get("load_in_4bit"))
    quant_mode = "none"
    if use_4bit:
        if device != "cuda":
            log("请求 4bit，但当前没有 CUDA：降级为非量化训练（如实记录）", level="warning")
            use_4bit = False
        else:
            try:
                import bitsandbytes

                quant_mode = "nf4"
            except Exception as exc:
                log(f"bitsandbytes 不可用（{exc}）：降级为非量化训练", level="warning")
                use_4bit = False

    # ---- 分词器 ----
    tokenizer = AutoTokenizer.from_pretrained(
        base_model,
        revision=config.get("tokenizer_revision") or None,
        trust_remote_code=True,
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    has_chat_template = bool(getattr(tokenizer, "chat_template", None))
    log(f"分词器就绪：vocab={len(tokenizer)}，chat_template={'有' if has_chat_template else '无'}")

    # ---- 数据 ----
    train_file = dataset_dir / f"{config.get('train_split', 'train')}.jsonl"
    records = []
    for line in train_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    if not records:
        log(f"训练集为空：{train_file}", level="error")
        emit({"event": "fatal", "message": "训练集为空"})
        return 3
    log(f"训练样本：{len(records)} 条")

    max_len = int(config.get("max_seq_length", 2048))

    def render(record: dict) -> tuple[str, str]:
        messages = record.get("messages", [])
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        user = next((m["content"] for m in messages if m.get("role") == "user"), "")
        answer = next((m["content"] for m in messages if m.get("role") == "assistant"), "")
        prompt = f"{system}\n\n{user}" if system else user
        if has_chat_template:
            prompt_text = tokenizer.apply_chat_template(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                tokenize=False,
                add_generation_prompt=True,
            )
            full_text = prompt_text + answer
        else:
            prompt_text = PROMPT_TEMPLATE.format(prompt=prompt, answer="")
            full_text = PROMPT_TEMPLATE.format(prompt=prompt, answer=answer)
        return prompt_text, full_text

    # 按样本下标去重统计：同一个样本会被每个 epoch 访问一次，
    # 直接累加会把数量放大到 epoch 倍。
    truncated_samples: set[int] = set()
    skipped_samples: set[int] = set()

    class SFTDataset(TorchDataset):
        """按 prompt / answer 分开编码：

        只从**答案**上计算 loss。序列超长时截断 prompt 的开头（保留靠近答案的部分），
        而不是截断整段文本的尾部 —— 否则答案会被切掉，训练会"成功但没有任何监督信号"。
        """

        def __init__(self, items: list[dict]):
            self.items = items

        def __len__(self) -> int:
            return len(self.items)

        def __getitem__(self, index: int) -> dict:
            item = self.items[index]
            prompt_text, full_text = render(item)
            answer_text = full_text[len(prompt_text):] if full_text.startswith(prompt_text) else ""
            full_ids, labels, truncated = encode_pair(tokenizer, prompt_text, answer_text, max_len)
            if truncated:
                truncated_samples.add(index)
            if not any(label != -100 for label in labels):
                # 没有任何监督 token（例如目标为空）：记录下来，不静默训练空样本
                skipped_samples.add(index)
                return {"input_ids": full_ids or [tokenizer.eos_token_id], "labels": [-100] * len(full_ids)}
            return {"input_ids": full_ids, "labels": labels}

    def collate(features: list[dict]) -> dict:
        max_length = max(len(f["input_ids"]) for f in features)
        pad_id = tokenizer.pad_token_id or 0
        input_ids, labels, attention = [], [], []
        for feature in features:
            length = len(feature["input_ids"])
            padding = max_length - length
            input_ids.append(feature["input_ids"] + [pad_id] * padding)
            labels.append(feature["labels"] + [-100] * padding)
            attention.append([1] * length + [0] * padding)
        return {
            "input_ids": torch.tensor(input_ids, dtype=torch.long),
            "labels": torch.tensor(labels, dtype=torch.long),
            "attention_mask": torch.tensor(attention, dtype=torch.long),
        }

    # 空答案的样本要**在进数据集之前**剔除：
    # 只在 __getitem__ 里标记的话，这条样本照样参与训练，
    # 整个 micro-batch 没有有效 label 时 loss 会退化成 0/0（nan 会污染权重）。
    usable_records: list[dict] = []
    empty_answer_indices: set[int] = set()
    for index, item in enumerate(records):
        prompt_text, full_text = render(item)
        answer_text = full_text[len(prompt_text):] if full_text.startswith(prompt_text) else ""
        if not answer_text.strip():
            empty_answer_indices.add(index)
            continue
        usable_records.append(item)
    dropped_empty = len(records) - len(usable_records)
    if dropped_empty:
        print(f"[data] 剔除空答案样本 {dropped_empty} 条（共 {len(records)} 条）")
    records = usable_records
    if not records:
        emit({"event": "fatal", "message": "没有可用的训练样本（全部为空答案）"})
        raise SystemExit("没有可用的训练样本（全部为空答案）")

    # 预扫描分词长度：在加载大型基座权重之前提前拦截 max_seq_length 过小导致的零有效标签/全截断异常（BUG-09）
    precheck_records: list[dict] = []
    max_prompt_tokens = 0
    max_answer_tokens = 0
    truncated_precheck = 0
    answer_truncated_precheck = 0
    dropped_zero_labels = 0
    for item in records:
        prompt_text, full_text = render(item)
        answer_text = full_text[len(prompt_text):] if full_text.startswith(prompt_text) else ""
        p_ids = tokenizer(prompt_text, add_special_tokens=False)["input_ids"]
        a_ids = tokenizer(answer_text, add_special_tokens=False)["input_ids"] if answer_text else []
        max_prompt_tokens = max(max_prompt_tokens, len(p_ids))
        max_answer_tokens = max(max_answer_tokens, len(a_ids))
        full_ids, labels, was_truncated = encode_pair(tokenizer, prompt_text, answer_text, max_len)
        if was_truncated:
            truncated_precheck += 1
        if len(a_ids) > max_len:
            answer_truncated_precheck += 1
        supervised_tokens = sum(1 for lbl in labels if lbl != -100)
        if supervised_tokens <= 0 or not full_ids:
            dropped_zero_labels += 1
            continue
        precheck_records.append(item)

    log(
        f"分词预检：可用 {len(precheck_records)} 条，"
        f"最大 prompt={max_prompt_tokens} tokens，最大 answer={max_answer_tokens} tokens，"
        f"max_seq_length={max_len}，prompt 截断={truncated_precheck} 条，answer 超长截断={answer_truncated_precheck} 条"
    )
    if not precheck_records:
        msg = (
            f"没有可监督的训练样本：max_seq_length={max_len} 过小或样本编码后监督标签为空"
            f"（最大 prompt={max_prompt_tokens} tokens，最大 answer={max_answer_tokens} tokens）。"
            "请在训练配置中调大 max_seq_length 后重试。"
        )
        log(msg, level="error")
        emit({"event": "fatal", "message": msg})
        return 3
    if answer_truncated_precheck and len(precheck_records) == answer_truncated_precheck:
        msg = (
            f"所有 {len(precheck_records)} 条样本的答案均超过 max_seq_length={max_len}"
            f"（最长 answer={max_answer_tokens} tokens），提示词被完全切空，无法有效训练。"
            f"请将 max_seq_length 调大至 {max_prompt_tokens + max_answer_tokens + 64} 以上。"
        )
        log(msg, level="error")
        emit({"event": "fatal", "message": msg})
        return 3
    if truncated_precheck:
        log(
            f"提示：{truncated_precheck}/{len(precheck_records)} 条样本超出 max_seq_length={max_len}，已优先截断 prompt 开头并保留 answer",
            level="warning",
        )
    records = precheck_records
    dropped_empty += dropped_zero_labels

    dataset = SFTDataset(records)

    # ---- 模型 ----
    load_kwargs: dict[str, Any] = {"trust_remote_code": True, "low_cpu_mem_usage": True}
    if config.get("base_revision"):
        load_kwargs["revision"] = config["base_revision"]
    if quant_mode == "nf4":
        from transformers import BitsAndBytesConfig

        load_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
        )
        load_kwargs["device_map"] = {"": 0}
    else:
        load_kwargs.update(dtype_kwargs(torch.float32 if device == "cpu" else torch.bfloat16))

    log(f"加载基座模型：{base_model}")
    model = AutoModelForCausalLM.from_pretrained(base_model, **load_kwargs)
    model.config.use_cache = False

    used_lora = False
    if config.get("use_lora", True):
        try:
            from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training

            if quant_mode == "nf4":
                model = prepare_model_for_kbit_training(
                    model, use_gradient_checkpointing=bool(config.get("gradient_checkpointing", True))
                )
            lora_config = LoraConfig(
                r=int(config.get("lora_rank", 16)),
                lora_alpha=int(config.get("lora_alpha", 32)),
                lora_dropout=float(config.get("lora_dropout", 0.05)),
                target_modules=list(config.get("target_modules") or ["q_proj", "v_proj"]),
                bias="none",
                task_type="CAUSAL_LM",
            )
            model = get_peft_model(model, lora_config)
            model.print_trainable_parameters()
            used_lora = True
        except Exception as exc:
            log(f"LoRA 不可用（{exc}）：退化为全参数训练", level="warning")

    if device == "cpu":
        model.to("cpu")

    # ---- 训练参数 ----
    # transformers 4.x 与 5.x 的 TrainingArguments 参数有差异（例如 5.x 去掉了 warmup_ratio），
    # 这里按实际签名过滤，并做已知的重命名，保证两种版本都能跑。
    import inspect

    desired = {
        "output_dir": str(output_dir),
        "per_device_train_batch_size": int(config.get("per_device_train_batch_size", 1)),
        "gradient_accumulation_steps": int(config.get("gradient_accumulation_steps", 16)),
        "learning_rate": float(config.get("learning_rate", 2e-4)),
        "num_train_epochs": float(config.get("num_train_epochs", 3)),
        "weight_decay": float(config.get("weight_decay", 0.0)),
        "lr_scheduler_type": config.get("lr_scheduler_type", "cosine"),
        "logging_steps": int(config.get("logging_steps", 5)),
        "save_steps": int(config.get("save_steps", 100)),
        "save_total_limit": 2,
        "max_steps": int(config.get("max_steps", -1)),
        "seed": int(config.get("seed", 42)),
        "bf16": False,
        "fp16": False,
        "gradient_checkpointing": bool(config.get("gradient_checkpointing", True)),
        "report_to": [],
        "optim": config.get("optim") or "adamw_torch",
        "remove_unused_columns": False,
        "dataloader_num_workers": 0,
    }
    warmup_ratio = float(config.get("warmup_ratio", 0.05))
    accepted = set(inspect.signature(TrainingArguments.__init__).parameters)
    if "warmup_ratio" in accepted:
        desired["warmup_ratio"] = warmup_ratio
    elif "warmup_steps" in accepted:
        # 用总步数换算成 warmup_steps
        steps_per_epoch = max(
            1,
            math.ceil(
                len(dataset)
                / max(
                    1,
                    int(desired["per_device_train_batch_size"]) * int(desired["gradient_accumulation_steps"]),
                )
            ),
        )
        total_steps = int(desired["max_steps"]) if int(desired["max_steps"]) > 0 else steps_per_epoch * max(
            1, int(desired["num_train_epochs"])
        )
        desired["warmup_steps"] = max(0, int(round(warmup_ratio * total_steps)))
        log(f"transformers 版本不提供 warmup_ratio，已换算为 warmup_steps={desired['warmup_steps']}")
    # bitsandbytes 优化器在 CPU 或未安装 bitsandbytes 时不可用：降级并如实说明
    bnb_optimizers = {"paged_adamw_8bit", "paged_adamw_32bit", "adamw_bnb_8bit", "adamw_bnb_32bit"}
    if desired.get("optim") in bnb_optimizers:
        bnb_available = False
        if device == "cuda":
            try:
                import bitsandbytes  # noqa: F401  （只探测是否可用）

                bnb_available = True
            except Exception:
                bnb_available = False
        if not bnb_available:
            desired["optim"] = "adamw_torch"
            log(
                f"优化器 {config.get('optim')} 需要 bitsandbytes（当前设备 {device}），已降级为 adamw_torch",
                level="warning",
            )

    dropped = sorted(set(desired) - accepted)
    if dropped:
        log(f"当前 transformers 不支持以下参数，已忽略：{', '.join(dropped)}")
    training_args = TrainingArguments(**{k: v for k, v in desired.items() if k in accepted})

    class LoggingTrainer(Trainer):
        def log(self, logs: dict, *a, **kw):
            super().log(logs, *a, **kw)
            payload = {"event": "step", **{k: v for k, v in logs.items() if isinstance(v, (int, float))}}
            payload["step"] = int(self.state.global_step)
            epoch = float(logs.get("epoch") or 0.0)
            payload["epoch"] = round(epoch, 4)
            emit(payload)

    trainer = LoggingTrainer(
        model=model,
        args=training_args,
        train_dataset=dataset,
        data_collator=collate,
    )

    started = time.time()
    try:
        result = trainer.train()
    except Exception as exc:
        log(f"训练失败：{type(exc).__name__}: {exc}", level="error")
        emit({"event": "fatal", "message": f"{type(exc).__name__}: {exc}"})
        return 4
    duration = time.time() - started

    # ---- 保存适配器 ----
    adapter_dir = output_dir / "adapter"
    try:
        if used_lora:
            model.save_pretrained(str(adapter_dir))
        else:
            trainer.save_model(str(adapter_dir))
    except Exception as exc:
        log(f"保存模型失败：{exc}", level="error")
        emit({"event": "fatal", "message": f"保存模型失败：{exc}"})
        return 5
    tokenizer.save_pretrained(str(adapter_dir))

    metrics = {
        "train_loss": float(getattr(result, "training_loss", 0.0) or 0.0),
        "truncated_samples": len(truncated_samples),
        "skipped_samples": len(skipped_samples),
        # 构造数据集前就被剔除的空答案样本（不进训练）
        "dropped_empty_answer_samples": dropped_empty,
        "global_step": int(trainer.state.global_step),
        "epochs": float(trainer.state.epoch or 0.0),
        "duration_seconds": round(duration, 2),
        "samples": len(records),
        "device": device,
        "quantization": quant_mode,
        "used_lora": used_lora,
        # 产物类型：LoRA 适配器 还是 完整模型（PEFT 不可用时会降级）
        "artifact_type": "adapter" if used_lora else "full",
        "max_seq_length": max_len,
        "seed": int(config.get("seed", 42)),
        "base_model": base_model,
        # **prompt 格式**：导出成 Ollama 模型时必须用同一个格式，
        # 否则推理时提示词与训练时不一样，模型表现会莫名其妙地差。
        # 有 chat_template = 用分词器自带的（GGUF 里也会带上）；
        # 没有 = 用 PROMPT_TEMPLATE（导出时要生成对应的 Ollama 模板）。
        "prompt_format": "chat_template" if has_chat_template else "instruct",
        "has_chat_template": bool(has_chat_template),
    }
    (output_dir / "train_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"训练完成：{metrics}")
    emit({"event": "done", "metrics": metrics, "adapter_dir": str(adapter_dir)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
