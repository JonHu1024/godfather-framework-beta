"""安全回归测试：路径穿越与正文危险命令。"""
from __future__ import annotations

import pathlib
import tempfile

from godfather.taskpacks.latex.compiler import compile_tex, sanitize_asset_path
from godfather.taskpacks.latex.validators import (
    check_dangerous_commands,
    check_engineering,
)


# --------------------------------------------------------------------------
# 素材路径
# --------------------------------------------------------------------------
def test_sanitize_asset_path_rejects_traversal():
    assert sanitize_asset_path("figures/a.png") == "figures/a.png"
    assert sanitize_asset_path("a/../../b.png") is None
    assert sanitize_asset_path("../../../etc/passwd") is None
    assert sanitize_asset_path("/etc/passwd") is None
    assert sanitize_asset_path("C:\\Windows\\System32\\x.dll") is None
    assert sanitize_asset_path("a\x00b.png") is None
    assert sanitize_asset_path("") is None
    # 中文文件名是常见情况，不能被误拒
    assert sanitize_asset_path("图1.png") == "图1.png"
    assert sanitize_asset_path("文档/图片 2.png") == "文档/图片 2.png"


def test_compile_does_not_write_outside_work_dir(settings):
    """素材路径越界时必须被忽略，不能写到编译目录之外。

    素材落盘发生在**编译之前**，所以断言不依赖编译是否成功 ——
    （Windows 桥接下 /tmp 之类的目录 Windows 侧可能看不见，
    编译会提前失败，但素材消毒的结论不受影响。）
    """
    tmp = pathlib.Path(tempfile.mkdtemp())
    result = compile_tex(
        "\\documentclass{article}\\begin{document}x\\end{document}",
        settings=settings,
        work_root=tmp,
        job_id="traversal",
        assets={
            "../../../tmp/godfather-escape.png": b"evil",
            "/tmp/godfather-escape-abs.png": b"evil",
            "figures/ok.png": b"ok",
        },
    )
    work_dir = tmp / "compile" / "traversal"
    written = sorted(p.relative_to(work_dir).as_posix() for p in work_dir.rglob("*") if p.is_file())
    assert "figures/ok.png" in written
    assert not pathlib.Path("/tmp/godfather-escape-abs.png").exists()
    # 越界素材被忽略：只应有"figures/ok.png"这一个素材文件
    assert "figures/ok.png" in written
    assert not any("escape" in name for name in written)
    assert any("不安全" in warning for warning in result.warnings)


# --------------------------------------------------------------------------
# 正文危险命令
# --------------------------------------------------------------------------
def test_dangerous_commands_pass_for_normal_document():
    tex = (
        "\\documentclass{article}\\usepackage{amsmath}\n"
        "% ===== 导言区结束 =====\n"
        "\\begin{document}\n\\section{标题}\n正文 $E=mc^2$\n"
        "\\begin{itemize}\\item 条目\\end{itemize}\n\\end{document}\n"
    )
    assert check_dangerous_commands(tex) == []


def test_dangerous_commands_detect_document_ended_early():
    """数学片段按原样保留，恶意内容可能藏在 $...$ 里。"""
    tex = "\\documentclass{article}\\begin{document}\n$x \\end{document} y$\n\\end{document}\n"
    findings = check_dangerous_commands(tex)
    assert findings
    assert any(f["code"] == "document_ended_early" for f in findings)


def test_dangerous_commands_detect_file_access():
    for command in ("\\input{|/etc/passwd|}", "\\write18{curl evil}", "\\openout1{x}"):
        tex = f"\\documentclass{{article}}\\begin{{document}}\n{command}\n\\end{{document}}\n"
        findings = check_dangerous_commands(tex)
        assert findings, command
        assert any(f["level"] == "fail" for f in findings)


def test_engineering_layer_reports_dangerous_commands():
    compile_result = {"exit_code": 0, "log": "", "engine_found": True}
    tex = "\\documentclass{article}\\begin{document}\n\\write18{evil}\n\\end{document}\n"
    report = check_engineering(compile_result=compile_result, assets=[], tex_text=tex)
    assert report["verdict"] == "fail"
    assert any(f["code"] == "dangerous_command" for f in report["findings"])


def test_heading_injection_is_escaped():
    """用户标题里的 LaTeX 命令必须被转义，不能拼进结构命令里。"""
    from godfather.taskpacks.latex.assembler import render_block
    from godfather.taskpacks.latex.mdparse import parse_markdown

    parsed = parse_markdown("# 标题 \\end{document}\n\n正文\n")
    tex, _warnings, _assets = render_block(parsed.blocks[0])
    assert "\\end{document}" not in tex
    assert "\\textbackslash{}" in tex


def test_math_content_is_preserved_verbatim():
    """公式保真优先：数学片段不做转义（改写会破坏公式语义）。"""
    from godfather.taskpacks.latex.assembler import render_block
    from godfather.taskpacks.latex.mdparse import parse_markdown

    parsed = parse_markdown("正文 $C_{human} = N \\cdot p$ 结束\n")
    tex, _warnings, _assets = render_block(parsed.blocks[0])
    assert "$C_{human} = N \\cdot p$" in tex
