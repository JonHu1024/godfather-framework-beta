#!/usr/bin/env python
"""M0 端到端验证脚本：真实跑一遍「导入 → 生成 → 编译 → 批注 → 修订 → 数据集」。

用法：
    python scripts/verify_m0.py                # 使用默认数据目录 data/verify
    python scripts/verify_m0.py --data-dir X   # 指定数据目录

脚本不会伪造任何结果：编译失败、字体不可用、教师不可达都会如实打印。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from godfather.app import register_job_handlers
from godfather.context import AppContext
from godfather.datasets import build_snapshot
from godfather.settings import load_settings
from godfather.taskpacks.latex.compiler import probe_fonts, resolve_fontset
from godfather.taskpacks.latex.pipeline import parse_markdown_document

SAMPLE = ROOT / "examples" / "sample_zh.md"
FIGURE = ROOT / "examples" / "figures" / "overview.png"


def line(title: str = "") -> None:
    print("\n" + ("─" * 3) + f" {title} " + "─" * 3)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(ROOT / "data" / "verify"))
    parser.add_argument("--model", default="", help="教师模型名（默认用配置里的）")
    args = parser.parse_args()

    settings = load_settings()
    settings.storage.data_dir = args.data_dir
    settings.ensure_dirs()
    ctx = AppContext.create(settings)
    register_job_handlers(ctx)

    print(f"数据目录：{settings.data_dir}")
    print(f"LaTeX 字体方案：{resolve_fontset(settings)}")
    probe = probe_fonts(settings)
    print(f"字体探针：{'可用' if probe['ok'] else '不可用'} — {probe['message']}")

    project_id = "prj-verify"
    ctx.db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    ctx.db.insert(
        "projects",
        {
            "id": project_id,
            "name": "M0 验证项目",
            "taskpack": "markdown_to_latex",
            "description": "verify_m0.py 自动生成",
            "active_model": "",
            "policy_version": "",
            "settings_json": "{}",
            "status": "created",
            "created_at": "2026-11-06T00:00:00+08:00",
            "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )

    line("1. 导入材料")
    markdown = SAMPLE.read_bytes()
    artifact = ctx.store.put_bytes(project_id, "markdown", markdown, media_type="text/markdown")
    ctx.store.put_bytes(
        project_id, "asset", FIGURE.read_bytes(), media_type="image/png", meta={"filename": FIGURE.name}
    )
    source_id = "src-verify"
    ctx.db.insert(
        "source_documents",
        {
            "id": source_id,
            "project_id": project_id,
            "filename": SAMPLE.name,
            "media_type": "text/markdown",
            "sha256": artifact["revision"],
            "revision": 1,
            "artifact_id": artifact["id"],
            "size_bytes": artifact["size_bytes"],
            "dialect": "pandoc-markdown",
            "status": "parsed",
            "meta_json": "{}",
            "created_at": "2026-11-06T00:00:00+08:00",
        },
    )
    parsed = parse_markdown_document(ctx, project_id, artifact_id=artifact["id"], title=SAMPLE.stem)
    ctx.db.update("documents", parsed["document_id"], {"source_document_id": source_id})
    print(f"  文档 {parsed['document_id']}，结构块 {parsed['block_count']} 个")

    line("2. 四种模式生成（真实编译）")
    modes = ("pandoc_baseline", "rule_only", "tool_assisted", "teacher_direct")
    results = {}
    for mode in modes:
        job = ctx.queue.submit(
            "latex.generate",
            {
                "project_id": project_id,
                "document_id": parsed["document_id"],
                "mode": mode,
                "provider": "ollama",
                "model": args.model,
            },
            project_id=project_id,
            idempotency_key=f"verify:{mode}",
        )
        claimed = ctx.queue._claim("verify")
        ctx.queue._run_job(claimed, "verify")
        generation = ctx.db.query_one(
            "SELECT * FROM generations WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        )
        stats = json.loads(generation["stats_json"] or "{}")
        compile_info = stats.get("compile", {})
        results[mode] = {
            "verdict": stats.get("verdict"),
            "compile_ok": compile_info.get("ok"),
            "fallback": compile_info.get("fallback_to_rules"),
            "pdf": bool(generation["pdf_artifact_id"]),
            "teacher": stats.get("teacher", {}),
            "validation": stats.get("validation", []),
            "duration_ms": compile_info.get("duration_ms"),
        }
        print(
            f"  {mode:<18} 结论={results[mode]['verdict']:<5} 编译={'成功' if results[mode]['compile_ok'] else '失败'}"
            f" 回退={results[mode]['fallback']} PDF={'有' if results[mode]['pdf'] else '无'}"
            f" 教师调用={results[mode]['teacher'].get('calls', 0)} 用时={results[mode]['duration_ms']}ms"
        )

    line("3. 批注（绑定产物版本与归一化坐标）")
    generation = ctx.db.query_one(
        "SELECT * FROM generations WHERE project_id = ? AND pdf_artifact_id IS NOT NULL "
        "AND pdf_artifact_id != '' ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    )
    if not generation:
        print("  没有生成 PDF，跳过批注环节")
        return 1
    pdf_artifact = ctx.store.get(generation["pdf_artifact_id"])
    annotation_id = "ann-verify"
    ctx.db.execute("DELETE FROM annotations WHERE id = ?", (annotation_id,))
    ctx.db.insert(
        "annotations",
        {
            "id": annotation_id,
            "project_id": project_id,
            "document_id": parsed["document_id"],
            "artifact_id": pdf_artifact["id"],
            "artifact_revision": pdf_artifact["revision"],
            "page_index": 0,
            "viewport_rotation": 0,
            "bbox_json": json.dumps([0.12, 0.24, 0.72, 0.38]),
            "comment": "这里的表格过宽了，缩小一点，但不要把字缩得太小",
            "category": "table_overflow",
            "scope": "document",
            "source_block_candidates_json": json.dumps(["block-0008"]),
            "mapping_confidence": "confirmed",
            "label_origin": "human",
            "status": "open",
            "superseded_by": None,
            "created_at": "2026-11-06T00:00:00+08:00",
            "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )
    print(f"  批注 {annotation_id} → 产物 {pdf_artifact['id']}（revision {pdf_artifact['revision'][:20]}…）")

    line("4. 接受修订（教师改写 → 重新编译验证）")
    job = ctx.queue.submit(
        "latex.revise",
        {
            "project_id": project_id,
            "document_id": parsed["document_id"],
            "annotation_id": annotation_id,
            "block_id": "block-0008",
            "instruction": "表格过宽，缩小一点但不要把字缩得太小",
        },
        project_id=project_id,
        idempotency_key="verify:revise",
    )
    claimed = ctx.queue._claim("verify")
    ctx.queue._run_job(claimed, "verify")
    job_row = ctx.queue.get(job["id"])
    result = json.loads(job_row["result_json"] or "{}")
    print(f"  任务状态：{job_row['status']}")
    if job_row["status"] == "succeeded":
        print(f"  修订结论：{result.get('verdict')}，PDF={'有' if result.get('pdf_artifact_id') else '无'}")
    else:
        print(f"  失败原因：{job_row['error']}")

    line("5. 数据集快照")
    snapshot = build_snapshot(ctx.db, project_id, seed="verify-1", include_candidates=True)
    print(f"  快照 {snapshot.version}：{snapshot.counts}")
    print(f"  文件：{snapshot.path}")
    print(f"  哈希：{snapshot.sha256}")

    line("6. 训练样本标签来源")
    for row in ctx.db.query_all(
        "SELECT label_origin, status, COUNT(*) AS n FROM training_examples WHERE project_id = ? GROUP BY label_origin, status",
        (project_id,),
    ):
        print(f"  {row['label_origin']:<24} {row['status']:<10} {row['n']} 条")

    line("结果")
    ok = all(results[mode]["compile_ok"] for mode in results)
    print("  全部模式编译成功" if ok else "  存在编译失败的模式（见上）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
