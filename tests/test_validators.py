"""四层质量检查测试。"""
from __future__ import annotations

from godfather.taskpacks.latex.mdparse import parse_markdown
from godfather.taskpacks.latex.validators import (
    check_content,
    check_engineering,
    check_layout,
    check_preference,
)

SOURCE = """# 标题

正文一段，包含公式 $E = mc^2$。

## 二级标题

- 项目一
- 项目二
"""


def test_engineering_detects_undefined_reference():
    compile_result = {
        "exit_code": 0,
        "log": "LaTeX Warning: Reference `fig:1' on page 1 undefined",
        "pdf_path": "/tmp/main.pdf",
        "engine_found": True,
        "timed_out": False,
    }
    report = check_engineering(compile_result=compile_result, assets=[])
    assert report["verdict"] == "warn"
    assert any(f["code"] == "undefined_reference" for f in report["findings"])


def test_engineering_fails_when_compile_failed():
    compile_result = {"exit_code": 1, "log": "! LaTeX Error: File `x.sty' not found.", "engine_found": True}
    report = check_engineering(compile_result=compile_result, assets=[])
    assert report["verdict"] == "fail"
    codes = {f["code"] for f in report["findings"]}
    assert "compile_failed" in codes
    assert "missing_file" in codes


def test_engineering_flags_missing_asset():
    compile_result = {"exit_code": 0, "log": "", "engine_found": True}
    report = check_engineering(compile_result=compile_result, assets=["a.png"], available_assets=set())
    assert report["verdict"] == "fail"
    assert any(f["code"] == "missing_asset" for f in report["findings"])


def test_content_warns_on_missing_heading():
    blocks = parse_markdown(SOURCE).blocks
    tex = "\\section{标题}\n正文一段，包含公式 $E = mc^2$。\n"  # 缺少二级标题
    report = check_content(blocks=blocks, tex_text=tex, source_text=SOURCE)
    assert report["verdict"] in {"warn", "fail"}
    assert any(f["code"] == "missing_heading" for f in report["findings"])


def test_content_detects_cjk_loss():
    blocks = parse_markdown(SOURCE).blocks
    report = check_content(blocks=blocks, tex_text="\\section{Title}\nBody only.\n", source_text=SOURCE)
    assert any(f["code"] == "cjk_lost" for f in report["findings"])


def test_layout_counts_overfull():
    compile_result = {"exit_code": 0, "log": "Overfull \\hbox (12.0pt too wide) in paragraph", "engine_found": True}
    report = check_layout(compile_result=compile_result)
    assert report["verdict"] == "warn"
    assert any(f["code"] == "overfull_box" for f in report["findings"])


def test_preference_checks_packages_and_environments():
    preferences = [
        {"pref_id": "p1", "rule": {"kind": "require_package", "package": "amsthm"}},
        {"pref_id": "p2", "rule": {"kind": "require_environment", "environment": "theorem"}},
        {"pref_id": "p3", "rule": {"kind": "forbid_command", "command": "\\tiny"}},
    ]
    tex = "\\documentclass{article}\\usepackage{amsthm}\\begin{theorem}x\\end{theorem}\n"
    report = check_preference(tex_text=tex, policy_preferences=preferences)
    assert report["verdict"] == "pass"

    report = check_preference(tex_text="\\documentclass{article}\\tiny\n", policy_preferences=preferences)
    codes = {f["code"] for f in report["findings"]}
    assert "preference_package_missing" in codes
    assert "preference_environment_missing" in codes
    assert "preference_command_present" in codes
