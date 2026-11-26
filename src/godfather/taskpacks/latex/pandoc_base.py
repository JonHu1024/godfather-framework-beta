"""Pandoc 基线转换。

Pandoc 负责"确定性可做"的部分（结构转换），loc/教师负责"语义与样式"的部分。
两种模式分别存数据、分别评测，不把工具完成的收益记到模型头上。
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass
class PandocResult:
    ok: bool
    tex: str = ""
    error: str = ""
    duration_ms: int = 0
    version: str = ""


def pandoc_binary(settings: Any) -> str:
    configured = getattr(settings.pandoc, "binary", "pandoc") or "pandoc"
    if Path_exists(configured):
        return configured
    found = shutil.which(configured)
    return found or configured


def Path_exists(candidate: str) -> bool:
    from pathlib import Path

    try:
        return Path(candidate).is_file()
    except OSError:
        return False


def pandoc_version(settings: Any) -> str:
    try:
        proc = subprocess.run(
            [pandoc_binary(settings), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        first = (proc.stdout or "").splitlines()
        return first[0] if first else ""
    except Exception:
        return ""


def is_available(settings: Any) -> bool:
    return bool(shutil.which(pandoc_binary(settings))) or Path_exists(pandoc_binary(settings))


# 允许的 pandoc 扩展名白名单：
# 扩展名会直接拼进 "--from markdown+ext1+ext2"，
# 配置被污染时可以注入 --lua-filter（pandoc 的 Lua 过滤器等于任意代码执行）。
SAFE_PANDOC_EXTENSIONS = {
    "autolink_bare_uris", "bracketed_spans", "citations", "emoji", "escaped_line_breaks",
    "fancy_lists", "footnotes", "fractions", "hard_line_breaks", "header_attributes",
    "implicit_figures", "inline_notes", "link_attributes", "markdown_in_html_blocks",
    "native_divs", "native_spans", "pipe_tables", "raw_attribute", "raw_html",
    # 注意：raw_tex 不在白名单里 —— 它会让 Markdown 里的原始 TeX 直接进编译器。
    "rebracketed_spans", "smart", "space_in_atx_header", "strikeout", "subscript",
    "superscript", "table_captions", "task_lists", "tex_math_dollars", "tex_math_single_backslash",
    "tex_math_double_backslash", "yaml_metadata_block",
}


def _safe_extensions(extensions: list[str]) -> list[str]:
    """过滤扩展名白名单。"""
    safe: list[str] = []
    for name in extensions or []:
        cleaned = str(name).strip()
        if not cleaned or cleaned.startswith("-"):
            continue
        if cleaned not in SAFE_PANDOC_EXTENSIONS:
            continue
        safe.append(cleaned)
    return safe


def convert_fragment(markdown: str, settings: Any, *, extra_args: list[str] | None = None) -> PandocResult:
    """把一段 Markdown 转成 LaTeX 片段（不含导言区）。"""
    binary = pandoc_binary(settings)
    extensions = _safe_extensions(getattr(settings.pandoc, "markdown_extensions", []) or [])
    args = [
        binary,
        "--from",
        "markdown" + (("+" + "+".join(extensions)) if extensions else ""),
        "--to",
        "latex",
        "--wrap",
        "none",
        # 不使用语法高亮：避免依赖 shaded/framed 等额外宏包，导言区完全受控
        "--no-highlight",
    ]
    args.extend(extra_args or [])
    import time

    started = time.time()
    try:
        proc = subprocess.run(
            args,
            input=markdown,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except FileNotFoundError:
        return PandocResult(ok=False, error=f"未找到 pandoc（{binary}）")
    except Exception as exc:
        return PandocResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    duration = int((time.time() - started) * 1000)
    if proc.returncode != 0:
        return PandocResult(
            ok=False,
            error=(proc.stderr or "pandoc 转换失败")[:2000],
            duration_ms=duration,
        )
    return PandocResult(ok=True, tex=proc.stdout or "", duration_ms=duration, version=pandoc_version(settings))
