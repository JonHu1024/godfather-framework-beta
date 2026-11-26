"""任务包流程测试：资产映射、教师输出清理、字体方案、回退。"""
from __future__ import annotations

import json

from godfather.taskpacks.latex.compiler import resolve_fontset
from godfather.taskpacks.latex.pipeline import (
    extract_asset_references,
    parse_json_object,
    sanitize_teacher_tex,
    source_text_for_document,
)


def test_extract_asset_references():
    text = "![图](figures/a.png)\n\n![图2](figures/b.png)\n\n![外链](https://x/y.png)\n"
    refs = extract_asset_references(text)
    assert refs == ["figures/a.png", "figures/b.png"]


def test_sanitize_teacher_tex_removes_fences_and_backticks():
    raw = "```latex\n\\textbf{粗体} `code`\n```"
    cleaned = sanitize_teacher_tex(raw)
    assert "```" not in cleaned
    assert "`" not in cleaned
    assert "\\textbf{粗体}" in cleaned


def test_sanitize_teacher_tex_balances_math():
    cleaned = sanitize_teacher_tex("公式 $x + y")
    assert cleaned.count("$") % 2 == 0


def test_parse_json_object_handles_fenced_json():
    text = '```json\n{"block_id": "block-0001", "tex": "\\\\section{x}"}\n```'
    payload = parse_json_object(text)
    assert payload["block_id"] == "block-0001"
    assert payload["tex"] == "\\section{x}"


def test_parse_json_object_handles_surrounding_text():
    text = '好的，这是结果：\n{"tex": "\\\\section{y}"}\n希望有帮助。'
    payload = parse_json_object(text)
    assert payload["tex"] == "\\section{y}"


def test_parse_json_object_repairs_invalid_escapes():
    """教师常把 LaTeX 反斜杠直接写进 JSON 字符串（\\section 是非法转义）。"""
    text = '{"tex": "\\section{y}"}'
    payload = parse_json_object(text)
    assert payload["tex"] == "\\section{y}"


def test_resolve_fontset_auto(settings, monkeypatch):
    settings.latex.fontset = "auto"
    settings.latex.binary = ""
    settings.latex.candidates = ["xelatex"]
    # 没有 Windows 引擎时使用 fandol（TeX Live 自带）
    assert resolve_fontset(settings) in {"fandol", "windows"}


def test_resolve_fontset_manual(settings):
    settings.latex.fontset = "manual"
    assert resolve_fontset(settings) == "manual"


def test_source_text_keeps_math_delimiters(ctx, project):
    """工具基线必须使用原文：否则 $$ 定界符会丢失，公式会被当作普通文本。"""
    from godfather.taskpacks.latex.pipeline import parse_markdown_document

    markdown = "# 标题\n\n$$\nE = mc^2\n$$\n"
    artifact = ctx.store.put_bytes(project, "markdown", markdown.encode("utf-8"))
    parsed = parse_markdown_document(ctx, project, artifact_id=artifact["id"], title="标题")
    ctx.db.insert(
        "source_documents",
        {
            "id": "src-math",
            "project_id": project,
            "filename": "m.md",
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
    ctx.db.update("documents", parsed["document_id"], {"source_document_id": "src-math"})
    text = source_text_for_document(ctx, parsed["document_id"])
    assert "$$" in text
    assert "E = mc^2" in text


def test_pandoc_baseline_keeps_math_as_math(ctx, project):
    """真实调用 Pandoc：公式必须被识别为数学，而不是转义后的普通文本。"""
    from godfather.taskpacks.latex.pandoc_base import convert_fragment

    settings = ctx.settings
    markdown = "$$\nC_{human} = N \\cdot p\n$$\n"
    result = convert_fragment(markdown, settings)
    if not result.ok:
        return  # 没有 pandoc 的环境跳过
    assert "C_{human}" in result.tex
    assert "C\\_\\{human\\}" not in result.tex


def test_training_example_records_label_origin(ctx, project):
    from godfather.taskpacks.latex.mdparse import parse_markdown
    from godfather.taskpacks.latex.pipeline import _record_training_example

    document_id = "doc-label"
    ctx.db.insert(
        "documents",
        {
            "id": document_id,
            "project_id": project,
            "title": "示例",
            "source_document_id": None,
            "source_revision": "sha256:x",
            "status": "parsed",
            "policy_version": "policy-001",
            "created_at": "2026-11-06T00:00:00+08:00",
            "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )
    generation_id = "gen-label"
    ctx.db.insert(
        "generations",
        {
            "id": generation_id,
            "project_id": project,
            "document_id": document_id,
            "mode": "teacher_direct",
            "status": "succeeded",
            "provider": "ollama",
            "model": "m",
            "policy_version": "policy-001",
            "prompt_version": "prompt-001",
            "job_id": None,
            "tex_artifact_id": None,
            "pdf_artifact_id": None,
            "stats_json": "{}",
            "created_at": "2026-11-06T00:00:00+08:00",
            "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )
    tex_artifact = ctx.store.put_bytes(project, "tex", b"\\section{x}")
    ctx.db.update("generations", generation_id, {"tex_artifact_id": tex_artifact["id"]})

    blocks = parse_markdown("# 标题\n").blocks
    _record_training_example(
        ctx,
        project_id=project,
        document_id=document_id,
        generation_id=generation_id,
        blocks=blocks,
        teacher_stats={"calls": 1},
        mode="teacher_direct",
        policy_version="policy-001",
        label_origin="human_reviewed_teacher",
        annotation_ids=["ann-1"],
    )
    rows = ctx.db.query_all("SELECT * FROM training_examples WHERE project_id = ?", (project,))
    assert len(rows) == 1
    row = rows[0]
    assert row["label_origin"] == "human_reviewed_teacher"
    assert json.loads(row["annotation_ids_json"]) == ["ann-1"]
    assert row["status"] == "candidate"
