"""隔离编译测试：用假引擎验证参数、超时与失败处理。"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from godfather.taskpacks.latex.compiler import (
    compile_tex,
    detect_engine,
    engine_version,
)


def _make_fake_cli(tmp_path: Path, name: str, py_code: str) -> Path:
    """生成跨平台可执行的假 CLI（Windows 下自动配 .cmd 包装器）。"""
    py_file = tmp_path / f"{name}.py"
    py_file.write_text(py_code, encoding="utf-8")
    if os.name == "nt":
        wrapper = tmp_path / f"{name}.cmd"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{py_file}" %*\r\n',
            encoding="utf-8",
        )
        return wrapper
    wrapper = tmp_path / name
    wrapper.write_text(f"#!{sys.executable}\n{py_code}", encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return wrapper


@pytest.fixture()
def fake_engine(tmp_path: Path):
    """创建一个假 xelatex：记录参数到 argv.txt，生成 main.pdf。"""
    return _make_fake_cli(
        tmp_path,
        "fake-xelatex",
        "import sys\n"
        "from pathlib import Path\n"
        "Path('argv.txt').write_text('\\n'.join(sys.argv[1:]) + '\\n', encoding='utf-8')\n"
        "print('This is XeTeX, Version 3.141592653 (Fake)')\n"
        "print('Output written on main.pdf (1 page).')\n"
        "Path('main.pdf').write_bytes(b'%PDF-1.7\\n%fake\\n')\n",
    )


def test_detect_engine_prefers_configured_binary(settings, fake_engine):
    settings.latex.binary = str(fake_engine)
    binary, message = detect_engine(settings)
    assert binary == str(fake_engine)
    assert "fake-xelatex" in message
    assert "Fake" in engine_version(str(fake_engine))


def test_detect_engine_reports_missing(settings, tmp_path):
    settings.latex.binary = str(tmp_path / "nope")
    settings.latex.candidates = [str(tmp_path / "nope")]
    binary, message = detect_engine(settings)
    assert binary is None
    assert "未找到 LaTeX 引擎" in message


def test_compile_disables_shell_escape_and_isolates_dir(settings, fake_engine, tmp_path):
    settings.latex.binary = str(fake_engine)
    result = compile_tex(
        "\\documentclass{article}\\begin{document}hi\\end{document}",
        settings=settings,
        work_root=tmp_path / "work",
        job_id="job-1",
    )
    assert result.ok is True
    assert result.pdf_path.endswith("main.pdf")

    argv = (Path(result.work_dir) / "argv.txt").read_text(encoding="utf-8").splitlines()
    assert "-no-shell-escape" in argv
    assert "-interaction=nonstopmode" in argv
    assert "-halt-on-error" in argv
    assert not any(arg.startswith("-shell-escape") for arg in argv)
    # 编译在独立目录中发生，主目录不产生文件
    assert (Path(result.work_dir) / "main.tex").exists()


def test_compile_reports_failure(settings, tmp_path):
    failing = _make_fake_cli(
        tmp_path,
        "fail-xelatex",
        "import sys\nprint('! Undefined control sequence.')\nprint('l.3 \\\\badcommand')\nsys.exit(1)\n",
    )
    settings.latex.binary = str(failing)
    result = compile_tex("x", settings=settings, work_root=tmp_path / "work", job_id="job-2")
    assert result.ok is False
    assert result.exit_code == 1
    assert "Undefined control sequence" in result.error


def test_compile_timeout_is_terminated(settings, tmp_path):
    slow = _make_fake_cli(
        tmp_path,
        "slow-xelatex",
        "import time\ntime.sleep(30)\n",
    )
    settings.latex.binary = str(slow)
    settings.latex.timeout_seconds = 2
    result = compile_tex("x", settings=settings, work_root=tmp_path / "work", job_id="job-3")
    assert result.ok is False
    assert result.timed_out is True
    assert "超时" in result.error


def test_missing_engine_returns_clear_error(settings, tmp_path):
    settings.latex.binary = str(tmp_path / "none")
    settings.latex.candidates = []
    result = compile_tex("x", settings=settings, work_root=tmp_path / "work")
    assert result.ok is False
    assert result.engine_found is False
    assert "LaTeX" in result.error
