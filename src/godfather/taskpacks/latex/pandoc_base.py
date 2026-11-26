"""Pandoc 基线转换。

Pandoc 负责"确定性可做"的部分（结构转换），loc/教师负责"语义与样式"的部分。
两种模式分别存数据、分别评测，不把工具完成的收益记到模型头上。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
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


_LTCAPTYPE_NONE_RE = re.compile(r"((?:^|\{)\s*)\\def\\LTcaptype\{none\}", re.MULTILINE)
_LONGTABLE_PROVIDES_RE = re.compile(r"\\ProvidesPackage\{longtable\}\s*\[\s*(\d{4})[-/](\d{2})[-/](\d{2})")


def _find_kpsewhich(settings: Any) -> str:
    """定位与当前 LaTeX 发行版配套的 kpsewhich，用于检测 longtable 宏包版本。"""
    candidates: list[str] = []
    try:
        from .compiler import detect_engine

        engine, _ = detect_engine(settings)
        if engine:
            engine_dir = Path(engine).resolve().parent
            for name in ("kpsewhich.exe", "kpsewhich.EXE", "kpsewhich"):
                candidates.append(str(engine_dir / name))
    except Exception:
        pass
    which_kp = shutil.which("kpsewhich")
    if which_kp:
        candidates.append(which_kp)
    for cand in candidates:
        if Path_exists(cand):
            return cand
    return ""


@lru_cache(maxsize=8)
def _inspect_longtable_sty_empty_captype(kpsewhich_bin: str) -> bool:
    """检查 longtable.sty 是否以空 \\LTcaptype 表示无编号（2024+ 新版行为）。"""
    if not kpsewhich_bin:
        return False
    try:
        proc = subprocess.run(
            [kpsewhich_bin, "longtable.sty"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        sty_path = (proc.stdout or "").strip().splitlines()
        if not sty_path:
            return False
        content = Path(sty_path[0].strip()).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False

    if r"\tl_if_empty" in content and "LTcaptype" in content:
        return True
    match = _LONGTABLE_PROVIDES_RE.search(content)
    if match:
        return int(match.group(1)) >= 2024
    return False


def _longtable_uses_empty_captype(settings: Any) -> bool:
    """在单元测试禁用外部命令或未装 TeX 时，尊重环境变量/kpsewhich 自动检测结果。"""
    override = os.environ.get("GODFATHER_LONGTABLE_EMPTY_CAPTYPE", "").strip().lower()
    if override in {"1", "true", "yes"}:
        return True
    if override in {"0", "false", "no"}:
        return False
    return _inspect_longtable_sty_empty_captype(_find_kpsewhich(settings))


def _compat_captionless_longtable(tex: str, *, empty_captype: bool) -> str:
    """按 longtable 版本兼容 Pandoc 生成的无标题表格标记（保留有标题表格正常编号）。

    Pandoc 3.x 为无标题表格输出 ``\\def\\LTcaptype{none}``；
    新版 longtable（TeX Live 2024+）改用空值 ``\\def\\LTcaptype{}`` 表示不编号，
    否则会触发 ``No counter 'none' defined``。
    """
    if not empty_captype or r"\def\LTcaptype{none}" not in tex:
        return tex
    return _LTCAPTYPE_NONE_RE.sub(lambda m: f"{m.group(1)}\\def\\LTcaptype{{}}", tex)


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
    raw_tex = proc.stdout or ""
    compat_tex = _compat_captionless_longtable(
        raw_tex,
        empty_captype=_longtable_uses_empty_captype(settings),
    )
    return PandocResult(ok=True, tex=compat_tex, duration_ms=duration, version=pandoc_version(settings))
