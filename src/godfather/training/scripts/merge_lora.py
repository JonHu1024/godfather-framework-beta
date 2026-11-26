"""合并 LoRA 适配器与基座权重，产出可转换的完整模型目录。

用法：python -m godfather.training.scripts.merge_lora --config config.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _dtype_kwargs(dtype) -> dict:
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


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    base_model = config["base_model"]
    adapter_dir = config["adapter_dir"]
    output_dir = Path(config["merged_dir"])
    # 必须带上训练时用的基座修订：否则合并的可能是同一仓库更新后的权重
    base_revision = config.get("base_revision") or ""
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except Exception as exc:
        emit({"event": "fatal", "message": f"缺少依赖：{exc}"})
        return 2

    dtype = torch.float16 if torch.cuda.is_available() else torch.float32
    emit({"event": "log", "message": f"加载基座 {base_model}"})
    try:
        load_kwargs = {**_dtype_kwargs(dtype), "low_cpu_mem_usage": True, "trust_remote_code": True}
        if base_revision:
            load_kwargs["revision"] = base_revision
        model = AutoModelForCausalLM.from_pretrained(base_model, **load_kwargs)
        tokenizer = AutoTokenizer.from_pretrained(base_model, trust_remote_code=True)
    except Exception as exc:
        emit({"event": "fatal", "message": f"加载基座失败：{exc}"})
        return 3

    emit({"event": "log", "message": f"加载适配器 {adapter_dir}"})
    try:
        model = PeftModel.from_pretrained(model, adapter_dir)
        model = model.merge_and_unload()
    except Exception as exc:
        emit({"event": "fatal", "message": f"合并失败：{exc}"})
        return 4

    emit({"event": "log", "message": f"写出合并模型到 {output_dir}"})
    try:
        model.save_pretrained(str(output_dir), safe_serialization=True)
        tokenizer.save_pretrained(str(output_dir))
    except Exception as exc:
        emit({"event": "fatal", "message": f"保存失败：{exc}"})
        return 5

    emit({"event": "done", "merged_dir": str(output_dir)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
