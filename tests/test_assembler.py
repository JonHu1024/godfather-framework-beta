"""组装器测试：转义、环境选择、行映射。"""
from __future__ import annotations

from godfather.taskpacks.latex.assembler import (
    assemble_document,
    escape_latex,
    render_block,
)
from godfather.taskpacks.latex.mdparse import parse_markdown


def test_escape_keeps_math_untouched():
    text = "价格是 $x_1 + y$ & 结束 100%"
    escaped = escape_latex(text)
    assert "$x_1 + y$" in escaped
    assert "\\&" in escaped
    assert "100\\%" in escaped


def test_heading_rendering_numbers_stripped():
    parsed = parse_markdown("## 1.2 详细设计\n\n正文。\n")
    tex, warnings, assets = render_block(parsed.blocks[0])
    assert "\\subsection{详细设计}" in tex
    assert "\\label{block-0001}" in tex
    assert not warnings


def test_table_renders_tabular():
    parsed = parse_markdown("| A | B |\n| --- | --- |\n| 1 | 2 |\n")
    tex, _w, _a = render_block(parsed.blocks[0])
    assert "\\begin{tabular}" in tex
    assert "1 & 2" in tex


def test_image_block_collects_asset():
    parsed = parse_markdown("![示意图](figures/a.png)\n")
    tex, _w, assets = render_block(parsed.blocks[0])
    assert "figures/a.png" in tex
    assert assets == ["figures/a.png"]


def test_assemble_produces_document_and_line_map(settings):
    parsed = parse_markdown("# 标题\n\n正文内容。\n\n## 二级\n\n更多正文。\n")
    result = assemble_document(parsed.blocks, settings=settings, title="标题")
    assert result.tex.startswith("% ===== Godfather 自动生成导言区")
    assert "\\begin{document}" in result.tex
    assert result.tex.rstrip().endswith("\\end{document}")
    assert "block-0001" in result.line_map
    start = result.line_map["block-0001"]["start"]
    assert start < result.tex.count("\n")


def test_teacher_output_overrides_rule_rendering(settings):
    parsed = parse_markdown("# 标题\n\n正文内容。\n")
    result = assemble_document(
        parsed.blocks,
        settings=settings,
        title="标题",
        teacher_outputs={"block-0002": "\\textbf{教师改写}"},
    )
    assert "\\textbf{教师改写}" in result.tex
    assert "正文内容。" not in result.tex
