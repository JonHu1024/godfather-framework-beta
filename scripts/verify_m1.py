#!/usr/bin/env python
"""M1 端到端验证：训练 → 导出 GGUF/Ollama → 评测 → 切换版本。

用法（建议在能访问 GPU 的机器上跑，例如 Windows）：
    python scripts/verify_m1.py --base-model D:/models/Qwen2.5-1.5B-Instruct
    python scripts/verify_m1.py --base-model D:/models/... --device cuda --epochs 1 --max-steps 20

脚本不做任何 mock：
- 依赖缺失、显存不足、编译失败、导出失败都会如实打印并以非零退出码结束；
- 训练数据不足时直接拒绝训练。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from godfather.context import AppContext
from godfather.settings import load_settings
from godfather.training import backends as backend_registry
from godfather.training import models as model_registry
from godfather.training.base import TrainingConfig
from godfather.training.evaluate import evaluate_models
from godfather.training.export import export_run
from godfather.training.runner import run_training
from godfather.util import canonical_json, new_id, now_iso


class FakeJob:
    """把任务事件直接打印出来（验证脚本用）。"""

    def __init__(self, project_id: str):
        self.job_id = "verify-m1"
        self.project_id = project_id

    def emit_event(self, **kwargs) -> None:
        message = kwargs.get("message", "")
        stage = kwargs.get("stage", "")
        if message:
            print(f"  [{stage}] {message[:200]}")


def seed_examples(ctx, project_id: str, count: int = 8) -> None:
    """写入几条示例训练样本（真实数据请从 Web 界面产出）。

    分组名按**分组切分**规则挑选，保证 train / dev 都有样本
    （切分是按源分组做的，组名决定它落在哪个集合）。
    """
    from godfather.datasets import _group_split

    seed = "verify-m1"
    # 三个划分都要有：以前只挑 train/dev，
    # holdout 恒为空 → "最终验收集"永远不存在，脚本只打印告警不失败。
    wanted = {"train": max(1, count // 2), "dev": max(1, count // 4), "holdout": max(1, count // 4)}
    groups: dict[str, list[str]] = {"train": [], "dev": [], "holdout": []}
    index = 0
    while any(len(groups[s]) < wanted[s] for s in groups) and index < 500:
        name = f"src-{index}"
        split = _group_split(name, seed, {"train": 0.8, "dev": 0.1, "holdout": 0.1})
        if split in groups and len(groups[split]) < wanted[split]:
            groups[split].append(name)
        index += 1

    plan = [(group, split) for split, names in groups.items() for group in names]
    for index, (group, split) in enumerate(plan):
        ctx.db.insert(
            "training_examples",
            {
                "id": new_id("ex"),
                "project_id": project_id,
                "task_type": "markdown_to_latex",
                "source_group_id": group,
                "source_revision": "sha256:verify",
                "input_payload_json": canonical_json(
                    {
                        "mode": "teacher_direct",
                        "markdown": f"# 标题 {index}\n\n正文段落 {index}，包含公式 $E=mc^2$。",
                        "blocks": [
                            {"block_id": "block-0001", "kind": "heading", "text": f"标题 {index}", "path": ["标题"]},
                            {"block_id": "block-0002", "kind": "paragraph", "text": f"正文段落 {index}，包含公式 E=mc^2。", "path": ["标题"]},
                        ],
                        "context": {"title": "验证文档"},
                    }
                ),
                "target_payload_json": canonical_json(
                    {"tex": f"\\section{{标题 {index}}}\n正文段落 {index}，包含公式 $E=mc^2$。"}
                ),
                "teacher_json": canonical_json({"provider": "manual", "model": "human", "revision": "unknown"}),
                "policy_version": "policy-001",
                "prompt_version": "prompt-001",
                "reference_ids_json": "[]",
                "annotation_ids_json": "[]",
                # 脚本自己造的样例没有经过任何人工复核：
            # 标成 human_reviewed_teacher 会让训练 manifest 的来源分布失真。
            "label_origin": "rule_synthesized",
                "validation_report_id": None,
                "status": "accepted",
                "split": split,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            },
        )


def _is_finite_number(value) -> bool:
    """loss 必须是有限数（nan / inf 都不算训练成功）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and number not in (float("inf"), float("-inf"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-model", required=True, help="基座模型本地目录")
    parser.add_argument("--data-dir", default=str(ROOT / "data" / "verify-m1"))
    parser.add_argument("--backend", default="local")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--epochs", type=float, default=1.0)
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--seq-len", type=int, default=512)
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--accum", type=int, default=4)
    parser.add_argument("--no-4bit", action="store_true")
    parser.add_argument("--quantization", default="q4_k_m")
    parser.add_argument("--skip-export", action="store_true")
    parser.add_argument("--skip-eval", action="store_true")
    parser.add_argument(
        "--activate-anyway",
        action="store_true",
        help="即使候选劣于基线也激活为当前版本（默认不激活）",
    )
    parser.add_argument("--seed-examples", action="store_true", help="写入示例样本（否则用已有数据）")
    args = parser.parse_args()

    settings = load_settings()
    settings.storage.data_dir = args.data_dir
    settings.ensure_dirs()
    ctx = AppContext.create(settings)

    # 项目 id 带时间戳：不要复用固定 id（会级联删掉上一次的运行记录，
    # 让"验证脚本"破坏历史数据）
    project_id = f"prj-verify-m1-{now_iso()[:19].replace(':', '').replace('-', '')}"
    ctx.db.insert(
        "projects",
        {
            "id": project_id,
            "name": "M1 验证项目",
            "taskpack": "markdown_to_latex",
            "description": "verify_m1.py",
            "active_model": "",
            "policy_version": "",
            "settings_json": "{}",
            "status": "created",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        },
    )

    print("== 后端预检 ==")
    for capability in backend_registry.list_backends():
        flag = "可用" if capability["available"] else "不可用"
        print(f"  {capability['name']:<14} {flag:<6} device={capability['device']:<8} {capability['version']}")
    if args.seed_examples:
        seed_examples(ctx, project_id)
        print(f"  已写入示例样本：{ctx.db.scalar('SELECT COUNT(*) FROM training_examples WHERE project_id=?', (project_id,))} 条")

    from godfather.datasets import build_snapshot

    snapshot = build_snapshot(ctx.db, project_id, seed="verify-m1", include_candidates=True)
    print(f"  快照 {snapshot.version}：{snapshot.counts}")
    for warning in snapshot.manifest.get("warnings", []):
        print(f"  ⚠ {warning}")

    config = TrainingConfig(
        base_model=args.base_model,
        backend=args.backend,
        device=args.device,
        dataset_snapshot_id=snapshot.id,
        num_train_epochs=args.epochs,
        max_steps=args.max_steps,
        max_seq_length=args.seq_len,
        per_device_train_batch_size=args.batch,
        gradient_accumulation_steps=args.accum,
        load_in_4bit=not args.no_4bit,
        logging_steps=1,
        save_steps=1000,
        output_dir=str(settings.data_dir / "training" / "verify-m1"),
    )

    print("\n== 训练 ==")
    run_id = new_id("run")
    ctx.db.insert(
        "training_runs",
        {
            "id": run_id,
            "project_id": project_id,
            "dataset_snapshot_id": snapshot.id,
            "backend": args.backend,
            "base_model": args.base_model,
            "base_revision": "",
            "params_json": canonical_json(config.to_dict()),
            "status": "queued",
            "log_path": "",
            "metrics_json": "{}",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        },
    )
    job = FakeJob(project_id)
    try:
        result = run_training(ctx, job, project_id=project_id, config=config, run_id=run_id)
    except Exception as exc:
        print(f"  训练失败：{type(exc).__name__}: {exc}")
        ctx.db.update(
            "training_runs",
            run_id,
            {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "updated_at": now_iso()},
        )
        return 1
    metrics = result.get("metrics", {})
    print(f"  训练完成：loss={metrics.get('train_loss')} steps={metrics.get('global_step')} "
          f"用时={metrics.get('duration_seconds')}s device={metrics.get('device')} "
          f"4bit={metrics.get('quantization')} lora={metrics.get('used_lora')}")

    # ---- 断言 1：训练结果本身必须成立 ----
    # 以前这个脚本**没有任何断言**（只有 try/except 转成退出码）：
    # loss=nan、steps=0 也照样"成功"退出 0。
    problems: list[str] = []
    loss = metrics.get("train_loss")
    if loss is None or not _is_finite_number(loss):
        problems.append(f"训练 loss 不是有限数：{loss!r}")
    steps = int(metrics.get("global_step") or 0)
    if steps <= 0:
        problems.append(f"训练步数为 0（global_step={metrics.get('global_step')!r}）")
    if problems:
        print("\n✗ 训练结果不成立：")
        for item in problems:
            print(f"   - {item}")
        return 1

    if args.skip_export:
        return 0

    print("\n== 导出（合并 → GGUF → 量化 → Ollama） ==")
    try:
        export = export_run(
            ctx,
            job,
            project_id=project_id,
            run_id=run_id,
            quantization=args.quantization,
        )
    except Exception as exc:
        print(f"  导出失败：{type(exc).__name__}: {exc}")
        return 2
    for step in export["steps"]:
        print(f"  {step['name']:<12} {'成功' if step['ok'] else '失败'}  {step['detail']}")
    print(f"  模型名：{export['name']}（{export['quantization']}，{export['size_bytes']} 字节）")

    if args.skip_eval:
        return 0

    print("\n== 评测（候选 vs 基线） ==")
    baseline = settings.providers.ollama.teacher_model or ""
    try:
        evaluation = evaluate_models(
            ctx,
            job,
            project_id=project_id,
            candidate_model=export["name"],
            baseline_model=baseline,
            snapshot_id=snapshot.id,
            split="dev",
            limit=8,
            run_id=run_id,
        )
    except Exception as exc:
        print(f"  评测失败：{type(exc).__name__}: {exc}")
        return 3
    print(f"  样本数：{evaluation['n_examples']}")
    for role, aggregate in evaluation["aggregates"].items():
        print(f"  {role:<10} n={aggregate['n']} 非空={aggregate['non_empty_rate']} "
              f"有结构={aggregate['has_latex_rate']} 覆盖={aggregate.get('mean_block_coverage')} "
              f"相似={aggregate.get('mean_similarity')} 延迟={aggregate.get('mean_latency_ms')}ms")
    if evaluation["deltas"]:
        print(f"  差值：{evaluation['deltas']}")

    # ---- 断言 2：候选模型必须真的能用 ----
    # 以前这里没有断言，候选在所有指标上都劣于基线也会被激活成"当前版本"。
    problems = []
    candidate = (evaluation.get("aggregates") or {}).get("candidate") or {}
    if int(candidate.get("evaluated") or 0) <= 0:
        problems.append("候选模型没有一次成功的调用")
    if float(candidate.get("non_empty_rate") or 0) <= 0:
        problems.append("候选模型输出为空（non_empty_rate=0）")
    if problems:
        print("\n✗ 候选模型不可用，不予激活：")
        for item in problems:
            print(f"   - {item}")
        return 4
    deltas = evaluation.get("deltas") or {}
    worse = [
        key for key in ("non_empty_rate", "has_latex_rate", "mean_similarity", "mean_block_coverage")
        if key in deltas and float(deltas.get(key) or 0) < -1e-9
    ]
    if worse:
        # 候选明显劣于基线 → **不**把它切成"当前版本"：
        # 以前这里无条件激活，验收脚本会把更差的模型推上线还返回 0。
        print(f"\n⚠ 候选模型在 {', '.join(worse)} 上劣于基线（差值 {deltas}）。")
        print("  链路本身是通的（训练→导出→注册→评测都成功），但候选尚不比基线好，")
        print("  因此**不自动激活**为当前版本。加 --activate-anyway 可强制激活。")
        print("\n完成。注意：小样本结果只能作为试验性参考。")
        if not args.activate_anyway:
            return 5

    print("\n== 切换版本 ==")
    activated = model_registry.activate(ctx.db, project_id, export["model_id"])
    print(f"  当前版本：{activated['activated']}（原 {activated['previous'] or '无'}）")
    try:
        print(f"  回滚一次：{model_registry.rollback(ctx.db, project_id)}")
    except ValueError as exc:
        print(f"  没有可回滚的历史版本（{exc}）—— 这是首次发布时的正常情况")
    print("\n完成。注意：小样本结果只能作为试验性参考。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
