"""数据集快照与 JSONL 导出。

规则（设计文档 §4）：
- 先按源文档/模板族/原始场景分组，再划分 train/dev/holdout，之后才切块与生成变体；
- 同一文档的段落、同一原始样例的改写不能跨集合；
- 参考案例不得与留出集重叠；
- 快照一旦冻结不可修改：已冻结的数据集不能被后续写入覆盖。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .db import Database
from .util import canonical_json, new_id, now_iso

SPLIT_RATIOS = {"train": 0.8, "dev": 0.1, "holdout": 0.1}
VALID_STATUSES = {"accepted", "candidate"}


@dataclass
class SnapshotResult:
    id: str
    version: str
    path: str
    sha256: str
    counts: dict
    manifest: dict


def normalize_ratios(ratios: dict[str, float] | None) -> dict[str, float]:
    """校验并归一化 train/dev/holdout 切分比例，保证非负且总和 > 0。"""
    source = dict(SPLIT_RATIOS if ratios is None else ratios)
    cleaned: dict[str, float] = {}
    for split in ("train", "dev", "holdout"):
        try:
            val = float(source.get(split, 0.0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"切分比例 {split} 必须是数字") from exc
        if val < 0.0:
            raise ValueError(f"切分比例 {split} 不能为负数：{val}")
        cleaned[split] = val
    total = sum(cleaned.values())
    if total <= 0.0:
        raise ValueError("train / dev / holdout 比例之和必须大于 0")
    return {split: value / total for split, value in cleaned.items()}


def _group_split(group_id: str, seed: str, ratios: dict[str, float]) -> str:
    """按分组 ID 的稳定哈希划分集合，保证同一分组永远落在同一集合。"""
    normalized = normalize_ratios(ratios)
    digest = hashlib.sha256(f"{seed}::{group_id}".encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    cumulative = 0.0
    fallback = "train"
    for split in ("train", "dev", "holdout"):
        weight = normalized.get(split, 0.0)
        if weight > 0.0:
            fallback = split
        cumulative += weight
        if weight > 0.0 and bucket < cumulative:
            return split
    return fallback


def _fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _input_content_key(example: dict) -> str:
    """输入侧的**内容**指纹。

    不能直接对整个 `input_payload_json` 取哈希：
    那个 JSON 里除了正文还有 `mode`、`context` 等信封字段，
    `mode` 一变指纹就变 —— 于是"同一篇 Markdown 跑两种模式"
    会被当成两个无关的样本分到不同划分，dev/holdout 里出现 train 的原文。
    这里只取真正决定输入内容���部分：markdown 正文 + 各块的文本。
    """
    payload = example.get("input_payload")
    if not isinstance(payload, dict):
        # 数据库行里只有 input_payload_json（字符串）
        raw = example.get("input_payload_json")
        payload = None
        if isinstance(raw, str) and raw:
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                payload = None
        elif isinstance(raw, dict):
            payload = raw
        if not isinstance(payload, dict):
            payload = {}
    markdown = str(payload.get("markdown") or "")
    blocks = payload.get("blocks") or []
    texts = []
    for block in blocks:
        if isinstance(block, dict):
            texts.append(str(block.get("text") or ""))
        else:
            texts.append(str(block))
    body = "\n\n".join(texts)
    if not markdown:
        markdown = body
    return _fingerprint(f"{markdown}\x00{body}")


def _cluster_groups_by_content(groups: dict[str, list[dict]]) -> list[set[str]]:
    """把"内容相关"的源分组合并成簇，划分时按簇划分。

    两种内容关系都会造成评测泄漏，两个都要合并：

    1) **输入相同**（同一篇文档被教师跑了两次，输出不同）：
       模型在 dev 上见过同一份原文，指标虚高；
    2) **目标相同**（不同原文生成了同一段 TeX）：
       模型在 dev 上见过标准答案，指标虚高。

    只按 (输入+目标) 一起做指纹是不够的：第 1 种情况指纹会不同，
    于是同一篇文档会被分到不同划分里。

    用并查集：共享任一指纹的分组归到同一个簇；
    代表元按字典序取，保证划分结果稳定可复现。
    """
    parent: dict[str, str] = {group: group for group in groups}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            # 固定一个代表（按字典序），保证划分结果稳定可复现
            if a < b:
                parent[b] = a
            else:
                parent[a] = b

    # 输入指纹与目标指纹分开记录：任意一个相同就要合并
    seen_input: dict[str, str] = {}
    seen_target: dict[str, str] = {}
    for group, group_examples in groups.items():
        for example in group_examples:
            input_key = _input_content_key(example)
            target_key = _fingerprint(example.get("target_payload_json") or "")
            for seen, key in ((seen_input, input_key), (seen_target, target_key)):
                if key in seen:
                    union(seen[key], group)
                else:
                    seen[key] = group

    clusters: dict[str, set[str]] = {}
    for group in groups:
        clusters.setdefault(find(group), set()).add(group)
    return list(clusters.values())


def collect_examples(db: Database, project_id: str, *, statuses: tuple[str, ...] = ("accepted",)) -> list[dict]:
    placeholders = ",".join("?" for _ in statuses)
    return db.query_all(
        f"SELECT * FROM training_examples WHERE project_id = ? AND status IN ({placeholders}) ORDER BY created_at",
        (project_id, *statuses),
    )


def _plan_snapshot_split(
    db: Database,
    project_id: str,
    *,
    seed: str = "godfather-1",
    ratios: dict[str, float] | None = None,
    include_candidates: bool = False,
) -> tuple[dict[str, float], dict[str, list[dict]], dict[str, str], dict[str, int], list[str], int]:
    """统一计算快照分组聚类、划分归属、计数与提示信息（供预览与冻结共用）。"""
    normalized = normalize_ratios(ratios)
    statuses = ("accepted", "candidate") if include_candidates else ("accepted",)
    examples = collect_examples(db, project_id, statuses=statuses)

    groups: dict[str, list[dict]] = {}
    for example in examples:
        group = example["source_group_id"] or example["id"]
        groups.setdefault(group, []).append(example)

    # 划分必须同时满足两件事：
    # 1) 同一源分组（同一篇文档/同一份来源）永远在同一个划分里 —— 避免近似泄漏；
    # 2) **内容完全相同的样本也必须在同一个划分里** ——
    #    否则 dev/holdout 里会出现 train 的原文，评测时模型是照着背的，
    #    指标虚高，候选模型与基线的比较就失去意义。
    # 所以：把"含有相同内容"的源分组合并成一个簇，按簇划分。
    clusters = _cluster_groups_by_content(groups)
    assigned: dict[str, str] = {}
    for cluster in clusters:
        split = _group_split(min(cluster), seed, normalized)
        for group in cluster:
            assigned[group] = split

    counts = {"train": 0, "dev": 0, "holdout": 0}
    for group, group_examples in groups.items():
        counts[assigned[group]] += len(group_examples)

    warnings: list[str] = []
    if len(examples) == 0:
        warnings.append("当前没有符合状态的训练样本，冻结后的快照将为空。")
    if counts.get("dev", 0) == 0 and normalized.get("dev", 0.0) > 0:
        warnings.append(
            "开发集为空：当前只有 %d 个源分组（%d 个去重簇），按分组切分无法分出开发集。"
            "请导入更多来源不同的文档，或调整切分比例。" % (len(groups), len(clusters))
        )
    if counts.get("holdout", 0) == 0 and normalized.get("holdout", 0.0) > 0:
        warnings.append(
            "留出集为空：按分组切分未能分出留出样本，最终验收将没有独立集合。"
        )
    return normalized, groups, assigned, counts, warnings, len(clusters)


def preview_snapshot_split(
    db: Database,
    project_id: str,
    *,
    seed: str = "godfather-1",
    ratios: dict[str, float] | None = None,
    include_candidates: bool = False,
) -> dict:
    """只读试算快照切分分布（不写库、不落盘），供前端与 Agent 调参。"""
    normalized, groups, assigned, counts, warnings, cluster_count = _plan_snapshot_split(
        db,
        project_id,
        seed=seed,
        ratios=ratios,
        include_candidates=include_candidates,
    )
    return {
        "seed": seed,
        "ratios": normalized,
        "include_candidates": include_candidates,
        "total_examples": sum(counts.values()),
        "group_count": len(groups),
        "cluster_count": cluster_count,
        "counts": counts,
        "group_assignments": assigned,
        "warnings": warnings,
    }


def build_snapshot(
    db: Database,
    project_id: str,
    *,
    seed: str = "godfather-1",
    ratios: dict[str, float] | None = None,
    version: str | None = None,
    include_candidates: bool = False,
    export_dir: Path | None = None,
) -> SnapshotResult:
    normalized, groups, assigned, counts, warnings, _cluster_count = _plan_snapshot_split(
        db,
        project_id,
        seed=seed,
        ratios=ratios,
        include_candidates=include_candidates,
    )

    records: list[dict] = []
    for group, group_examples in groups.items():
        split = assigned[group]
        for example in group_examples:
            records.append(
                {
                    "id": example["id"],
                    "task_type": example["task_type"],
                    "source_group_id": group,
                    "source_revision": example["source_revision"],
                    "input_payload": json.loads(example["input_payload_json"] or "{}"),
                    "target_payload": json.loads(example["target_payload_json"] or "{}"),
                    "teacher": json.loads(example["teacher_json"] or "{}"),
                    "policy_version": example["policy_version"],
                    "prompt_version": example["prompt_version"],
                    "reference_ids": json.loads(example["reference_ids_json"] or "[]"),
                    "annotation_ids": json.loads(example["annotation_ids_json"] or "[]"),
                    "label_origin": example["label_origin"],
                    "validation_report_id": example["validation_report_id"],
                    "status": example["status"],
                    "split": split,
                    "created_at": example["created_at"],
                }
            )

    version = version or f"snap-{now_iso()[:10].replace('-', '')}-{new_id('v')[-4:]}"
    export_dir = export_dir or (db_path_parent(db) / "exports")
    export_dir.mkdir(parents=True, exist_ok=True)
    snapshot_id = new_id("snap")
    # 文件名带快照 id：同一 version 再次冻结时不会覆盖已冻结的文件
    # （覆盖会让旧快照记录的 sha256 与磁盘内容对不上，复现性直接失效）。
    jsonl_path = export_dir / f"{project_id}-{snapshot_id}.jsonl"
    payload = "\n".join(canonical_json(record) for record in records)
    if payload:
        payload += "\n"
    digest = "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
    # 原子写入：显式使用 LF，避免 Windows 默认 CRLF 破坏与 sha256 摘要的一致性
    tmp_path = jsonl_path.with_name(jsonl_path.name + ".tmp")
    tmp_path.write_text(payload, encoding="utf-8", newline="\n")
    tmp_path.replace(jsonl_path)

    manifest = {
        "project_id": project_id,
        "version": version,
        "seed": seed,
        "ratios": normalized,
        "group_assignments": assigned,
        "source_groups": sorted(groups.keys()),
        "label_origins": sorted({record["label_origin"] for record in records}),
        "policy_versions": sorted({record["policy_version"] for record in records if record["policy_version"]}),
        "holdout_excluded_from_prompt": True,
        "warnings": warnings,
        "created_at": now_iso(),
    }

    db.insert(
        "dataset_snapshots",
        {
            "id": snapshot_id,
            "project_id": project_id,
            "version": version,
            "jsonl_path": str(jsonl_path),
            "sha256": digest,
            "counts_json": canonical_json(counts),
            "manifest_json": canonical_json(manifest),
            "status": "frozen",
            "created_at": now_iso(),
        },
    )
    return SnapshotResult(
        id=snapshot_id,
        version=version,
        path=str(jsonl_path),
        sha256=digest,
        counts=counts,
        manifest=manifest,
    )


def db_path_parent(db: Database) -> Path:
    return Path(db.path).parent


def list_snapshots(db: Database, project_id: str) -> list[dict]:
    rows = db.query_all(
        "SELECT * FROM dataset_snapshots WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
    )
    for row in rows:
        row["counts"] = json.loads(row.pop("counts_json") or "{}")
        row["manifest"] = json.loads(row.pop("manifest_json") or "{}")
    return rows


def get_snapshot(db: Database, snapshot_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM dataset_snapshots WHERE id = ?", (snapshot_id,))
    if not row:
        return None
    row["counts"] = json.loads(row.pop("counts_json") or "{}")
    row["manifest"] = json.loads(row.pop("manifest_json") or "{}")
    return row


def examples_of_split(db: Database, project_id: str, split: str) -> list[dict]:
    rows = db.query_all(
        "SELECT * FROM training_examples WHERE project_id = ? ORDER BY created_at", (project_id,)
    )
    out = []
    for row in rows:
        if row["split"] != split:
            continue
        out.append(row)
    return out


def holdout_group_ids(db: Database, project_id: str) -> set[str]:
    """返回留出集涉及的分组 ID，用于阻止其进入提示词示例与检索。"""
    snapshots = list_snapshots(db, project_id)
    if not snapshots:
        return set()
    manifest = snapshots[0].get("manifest") or {}
    assignments = manifest.get("group_assignments") or {}
    return {group for group, split in assignments.items() if split == "holdout"}


def build_manual_taskpack(
    db: Database,
    project_id: str,
    *,
    limit: int = 50,
) -> dict:
    """导出人工导入模式的任务包（设计文档 §7.2）。"""
    from .policy import engine as policy_engine

    policy = policy_engine.load_active_policy(db, project_id)
    holdout = holdout_group_ids(db, project_id)
    candidates = [
        example
        for example in collect_examples(db, project_id, statuses=("accepted", "candidate"))
        if (example["source_group_id"] or example["id"]) not in holdout
    ][:limit]

    samples = []
    for example in candidates:
        payload = json.loads(example["input_payload_json"] or "{}")
        input_hash = "sha256:" + hashlib.sha256(
            canonical_json(payload).encode("utf-8")
        ).hexdigest()
        samples.append(
            {
                "sample_id": example["id"],
                "input_hash": input_hash,
                "task_type": example["task_type"],
                "policy_version": example["policy_version"],
                "prompt_version": example["prompt_version"],
                "input": payload,
                "expected_schema": {
                    "type": "object",
                    "required": ["block_id", "tex"],
                    "properties": {
                        "block_id": {"type": "string"},
                        "tex": {"type": "string"},
                        "required_assets": {"type": "array", "items": {"type": "string"}},
                        "warnings": {"type": "array", "items": {"type": "string"}},
                        "questions": {"type": "array", "items": {"type": "string"}},
                    },
                },
            }
        )
    return {
        "project_id": project_id,
        "policy_version": policy.version,
        "prompt_version": "prompt-001",
        "created_at": now_iso(),
        "instructions": (
            "请在获授权的外部服务上完成这些样本，并按 "
            '{"samples": [{"sample_id", "input_hash", "output"}]} 的格式导回。'
            "输出必须是目标 LaTeX 片段，不要带 Markdown 代码围栏。"
        ),
        "samples": samples,
    }
