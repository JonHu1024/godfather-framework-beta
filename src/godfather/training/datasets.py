"""把数据快照转换成训练后端可用的格式。

输出（默认放在 `<data>/training/<run_id>/dataset/`）：
- `train.jsonl` / `dev.jsonl`：messages 格式（system / user / assistant）
- `dataset_info.json`：LLaMA-Factory 的数据集登记表（alpaca 格式）
- `manifest.json`：条数、切分、哈希、样本来源分布

设计约束（§4.1）：教师与学生看到的信息必须可对齐 ——
学生的输入里包含与教师一致的上下文（标题路径、全局上下文、适用偏好），而不是只有裸 Markdown。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..db import Database
from ..util import canonical_json, sha256_text

# system prompt 必须与训练目标一致：
# 训练目标（assistant）是**整篇 .tex 文档**（含导言区、\begin{document}…\end{document}），
# 以前这里写的是"只输出 LaTeX 片段" —— 模型被教"输出片段"、
# 实际学的是"输出整篇"，推理时还会被 stop 序列在导言区砍断。
SYSTEM_PROMPT = (
    "你是一个 Markdown → LaTeX 转换器。输出完整的 LaTeX 文档"
    "（从 \\documentclass 开始，到 \\end{document} 结束），"
    "不要输出解释，不要使用 Markdown 代码围栏，不要编造原文没有的内容。"
)


@dataclass
class PreparedDataset:
    directory: Path
    train_path: Path
    dev_path: Path
    manifest: dict[str, Any]

    def to_dict(self) -> dict:
        return {
            "directory": str(self.directory),
            "train_path": str(self.train_path),
            "dev_path": str(self.dev_path),
            "manifest": self.manifest,
        }


def _example_payload(
    example: dict, *, taskpacks: Any = None
) -> tuple[str, str, str]:
    """把一条训练样本转成 `(system, user, assistant)`；优先委派给对应的 Cordis 任务包。"""
    task_type = str(example.get("task_type") or "markdown_to_latex")
    if taskpacks is not None and taskpacks.has(task_type):
        spec = taskpacks.get(task_type)
        if callable(spec.format_training_example):
            result = spec.format_training_example(example)
            if isinstance(result, tuple) and len(result) == 3:
                return str(result[0]), str(result[1]), str(result[2])

    payload = example.get("input_payload") or {}
    target = example.get("target_payload") or {}

    if task_type != "markdown_to_latex" and ("prompt" in payload or "text" in target or "output" in target):
        sys_txt = str(payload.get("system") or "你是一个专业且严谨的本地微调助手。")
        user_txt = str(payload.get("prompt") or payload.get("markdown") or "")
        ans_txt = str(target.get("text") or target.get("output") or target.get("tex") or "")
        return sys_txt, user_txt, ans_txt

    blocks = payload.get("blocks") or []
    title = (payload.get("context") or {}).get("title", "")

    parts: list[str] = []
    if title:
        parts.append(f"文档标题：{title}")
    heading_path = ""
    for block in blocks:
        if isinstance(block, dict) and block.get("path"):
            heading_path = " / ".join(block["path"])
            break
    if heading_path:
        parts.append(f"所属章节：{heading_path}")
    markdown = payload.get("markdown") or "\n\n".join(
        b.get("text", "") for b in blocks if isinstance(b, dict)
    )
    parts.append("以下是待转换的 Markdown：\n\n" + markdown)
    return SYSTEM_PROMPT, "\n\n".join(parts), str(target.get("tex") or "")


def example_to_messages(example: dict, *, taskpacks: Any = None) -> dict[str, Any]:
    system_text, user, assistant = _example_payload(example, taskpacks=taskpacks)
    return {
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant},
        ],
        "id": example.get("id"),
        "task_type": example.get("task_type") or "markdown_to_latex",
        "source_group_id": example.get("source_group_id"),
        "label_origin": example.get("label_origin"),
        "split": example.get("split"),
    }


def load_snapshot_examples(db: Database, snapshot_id: str) -> list[dict]:
    """读快照文件。

    必须校验文件哈希：快照的 sha256 是"这份数据是什么"的唯一凭据。
    不校验的话，文件在冻结后被改动（误编辑、手工改、外部工具）时，
    训练会静默使用与记录不符的数据 —— 可复现性直接失效，
    而且评测出来的指标说的也不是这份快照了。
    """
    snapshot = db.query_one("SELECT * FROM dataset_snapshots WHERE id = ?", (snapshot_id,))
    if not snapshot:
        return []
    path = Path(snapshot["jsonl_path"])
    if not path.exists():
        return []
    raw_bytes = path.read_bytes()
    expected = (snapshot["sha256"] or "").strip()
    if expected:
        actual = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
        if actual != expected:
            raise ValueError(
                f"快照 {snapshot_id} 的文件与记录的哈希不一致（{path}）："
                f"记录 {expected[:19]}…，实际 {actual[:19]}…；"
                "数据在冻结后被改动过，请重新冻结快照"
            )
    raw = raw_bytes.decode("utf-8")
    records = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        records.append(json.loads(line))
    return records


ALLOWED_SPLITS = ("train", "dev", "holdout")


def prepare_dataset(
    db: Database,
    snapshot_id: str,
    output_dir: Path,
    *,
    train_split: str = "train",
    eval_split: str = "dev",
    max_train: int = -1,
    max_eval: int = 32,
    taskpacks: Any = None,
) -> PreparedDataset:
    # split 必须是固定枚举：否则 "../../../escaped" 这类值会把文件写到数据集目录之外，
    # 而且 train/dev 互换会让"训练集"与"评测集"悄悄对调。
    if train_split not in ALLOWED_SPLITS:
        raise ValueError(f"未知的训练划分：{train_split}（允许：{', '.join(ALLOWED_SPLITS)}）")
    if eval_split not in ALLOWED_SPLITS:
        raise ValueError(f"未知的评测划分：{eval_split}（允许：{', '.join(ALLOWED_SPLITS)}）")
    if train_split == eval_split:
        raise ValueError("训练划分与评测划分不能相同")

    records = load_snapshot_examples(db, snapshot_id)
    directory = Path(output_dir) / "dataset"
    directory.mkdir(parents=True, exist_ok=True)

    train_records = [r for r in records if r.get("split") == train_split]
    eval_records = [r for r in records if r.get("split") == eval_split]
    if max_train and max_train > 0:
        train_records = train_records[:max_train]
    if max_eval and max_eval > 0:
        eval_records = eval_records[:max_eval]

    train_messages = [example_to_messages(r, taskpacks=taskpacks) for r in train_records]
    eval_messages = [example_to_messages(r, taskpacks=taskpacks) for r in eval_records]

    train_path = directory / f"{train_split}.jsonl"
    dev_path = directory / f"{eval_split}.jsonl"
    train_path.write_text(
        "\n".join(canonical_json(record) for record in train_messages) + ("\n" if train_messages else ""),
        encoding="utf-8",
        newline="\n",
    )
    dev_path.write_text(
        "\n".join(canonical_json(record) for record in eval_messages) + ("\n" if eval_messages else ""),
        encoding="utf-8",
        newline="\n",
    )

    # LLaMA-Factory 的 alpaca 风格数据集登记表
    dataset_info = {
        "identity": {
            train_split: {
                "file_name": train_path.name,
                "columns": {"prompt": "instruction", "query": "input", "response": "output"},
                "tags": {"role_tag": "from", "query_tag": "human", "value_tag": "gpt"},
            },
            eval_split: {
                "file_name": dev_path.name,
                "columns": {"prompt": "instruction", "query": "input", "response": "output"},
                "tags": {"role_tag": "from", "query_tag": "human", "value_tag": "gpt"},
            },
        }
    }
    (directory / "dataset_info.json").write_text(
        json.dumps(dataset_info, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )

    origins: dict[str, int] = {}
    for record in train_records:
        origin = record.get("label_origin") or "unknown"
        origins[origin] = origins.get(origin, 0) + 1

    manifest = {
        "snapshot_id": snapshot_id,
        "train_split": train_split,
        "eval_split": eval_split,
        "train_count": len(train_records),
        "eval_count": len(eval_records),
        "label_origins": origins,
        "source_groups": sorted({r.get("source_group_id", "") for r in train_records}),
        "train_sha256": sha256_text(train_path.read_text(encoding="utf-8")),
        "eval_sha256": sha256_text(dev_path.read_text(encoding="utf-8")),
        "system_prompt": SYSTEM_PROMPT,
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    return PreparedDataset(
        directory=directory,
        train_path=train_path,
        dev_path=dev_path,
        manifest=manifest,
    )


def build_prompt(example: dict) -> str:
    """评测用：构造与训练时一致的 **user 正文**。

    不包含 system：Ollama 侧会按 Modelfile 注入 SYSTEM，
    这里再拼一遍就会让系统提示词出现两次，与训练时的分布不一致。
    """
    messages = example_to_messages(example).get("messages", [])
    return "\n\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
