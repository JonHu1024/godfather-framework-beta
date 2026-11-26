"""确定性组装器：把结构块（+ 教师 TeX 片段）组装为完整 LaTeX 文档。

职责边界：
- 教师只产出正文片段（block_id → tex）；导言区、宏包、文件清单由本模块管理。
- 输出附带行映射（block_id → 汇编后 .tex 行号区间），供批注定位使用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .mdparse import Block
from .templates import load_template

LATEX_SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}

# 行内公式：$...$ 两侧不能紧挨着数字/货币语境（"价格 $5 和 $10 元"不是公式）。
# 规则：$ 后不能是数字，$ 前不能是数字或空白以外的可打印字符。
INLINE_MATH_RE = re.compile(
    r"(\$\$.+?\$\$|(?<![\w\\])\$[^$\n]*[^$\s]\$(?![\w])|\\\(.+?\\\)|\\\[.+?\\\])",
    re.DOTALL,
)
# 行内代码（支持单反引号与双反引号），优先于粗体/斜体提取，避免代码里的 * / _ 被误改
_INLINE_CODE_RE = re.compile(r"(?<!`)(`+)([^\n]+?)\1(?!`)")
_BOLD_ITALIC_STAR_RE = re.compile(r"\*\*\*(?!\s)(.+?)(?<!\s)\*\*\*")
_BOLD_STAR_RE = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*")
_BOLD_UNDER_RE = re.compile(r"(?<![A-Za-z0-9_])__(?!\s|_)(.+?)(?<!\s|_)__(?![A-Za-z0-9_])")
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*(?!\s|\*)(.+?)(?<!\s|\*)\*(?!\*)")
_ITALIC_UNDER_RE = re.compile(r"(?<![A-Za-z0-9_])_(?!\s|_)(.+?)(?<!\s|_)_(?![A-Za-z0-9_])")
LEADING_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)[.、\s]+(.*)$")
FIGURE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<src>[^)\s]+)(?P<rest>[^)]*)\)")

SECTION_COMMANDS = {1: "section", 2: "subsection", 3: "subsubsection", 4: "paragraph", 5: "subparagraph", 6: "subparagraph"}


@dataclass
class AssembleResult:
    tex: str
    line_map: dict[str, dict[str, int]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)
    preamble: str = ""


def escape_latex(text: str, *, escape_alignment: bool = False) -> str:
    r"""转义 LaTeX 特殊字符，但保留数学片段原样。

    ``escape_alignment=True`` 时，数学片段里的**裸** ``&`` 也要转义：
    表格单元格里 ``$a & b$`` 的裸 ``&`` 会被当成列分隔符，
    编译直接失败（Missing $ inserted）。数学上 ``\&`` 与 ``&`` 等价。
    已经写成 ``\&`` 的不再二次转义（否则会变成换行 + 对齐符）。
    """
    parts = INLINE_MATH_RE.split(text)
    out: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 1:
            if escape_alignment:
                # 只转义**裸** &：已经写成 \& 的不能再转义，
                # 否则 \& 变成 \\&（数学里 = 换行 + 对齐符 → 编译失败）。
                part = re.sub(r"(?<!\\)&", r"\\&", part)
            out.append(part)
        else:
            out.append(_escape_plain(part))
    return "".join(out)


def _escape_plain(text: str) -> str:
    out = []
    for char in text:
        out.append(LATEX_SPECIALS.get(char, char))
    return "".join(out)


def _strip_heading_number(text: str) -> tuple[str, str]:
    """剥离"章节编号"前缀。

    只剥离带点号/顿号的编号形态（"1.2 "、"一、"），不能剥掉 "2024 年度报告" 里的年份。
    """
    match = LEADING_NUMBER_RE.match(text)
    if not match:
        return "", text
    number = match.group(1)
    if not (number.endswith((".", "、", "．")) or re.search(r"[.．]\d*$", number)):
        return "", text
    return number, match.group(2)


def _render_emphasis(segment: str) -> str:
    """递归转换粗体/斜体并对普通文本进行 LaTeX 转义（占位符 \\x00T0\\x00 保持原样）。"""
    if not segment:
        return ""
    rules: tuple[tuple[re.Pattern[str], str], ...] = (
        (_BOLD_ITALIC_STAR_RE, "textbf_emph"),
        (_BOLD_STAR_RE, "textbf"),
        (_BOLD_UNDER_RE, "textbf"),
        (_ITALIC_STAR_RE, "emph"),
        (_ITALIC_UNDER_RE, "emph"),
    )
    earliest_match: re.Match[str] | None = None
    earliest_kind = ""
    for pattern, kind in rules:
        match = pattern.search(segment)
        if match is not None:
            if earliest_match is None or match.start() < earliest_match.start():
                earliest_match = match
                earliest_kind = kind
    if earliest_match is None:
        return _escape_plain(segment)

    prefix = _escape_plain(segment[: earliest_match.start()])
    inner = _render_emphasis(earliest_match.group(1))
    suffix = _render_emphasis(segment[earliest_match.end() :])
    if earliest_kind == "textbf_emph":
        wrapped = f"\\textbf{{\\emph{{{inner}}}}}"
    elif earliest_kind == "textbf":
        wrapped = f"\\textbf{{{inner}}}"
    else:
        wrapped = f"\\emph{{{inner}}}"
    return f"{prefix}{wrapped}{suffix}"


def _convert_inline(text: str, *, escape_alignment: bool = False) -> str:
    """段落内联转换：按“行内代码 → 数学公式 → 图片 → 强调/转义”顺序处理。

    行内代码必须在数学公式之前剥离，避免 ``$HOME`` 或 ``a + $x$`` 跨代码块配对成伪公式；
    数学与代码占位符均在强调解析与转义之前隔离，避免里面的 ``*`` / ``_`` 被当成粗斜体标记。
    """
    tokens: list[str] = []

    def _stash(tex_snippet: str) -> str:
        idx = len(tokens)
        tokens.append(tex_snippet)
        return f"\x00T{idx}\x00"

    # 1. 最先提取行内代码（代码中的 * _ $ \\ 等一律作为字面量放进 \texttt）
    def _take_code(match: re.Match[str]) -> str:
        code_body = match.group(2).strip() if len(match.group(1)) > 1 else match.group(2)
        return _stash(f"\\texttt{{{_escape_plain(code_body)}}}")

    work = _INLINE_CODE_RE.sub(_take_code, text)

    # 2. 再提取数学公式（保留公式内的 * _ & 等符号，仅在表格对齐场景转义裸 &）
    parts = INLINE_MATH_RE.split(work)
    math_protected: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 1:
            if escape_alignment:
                part = re.sub(r"(?<!\\)&", r"\\&", part)
            math_protected.append(_stash(part))
        else:
            math_protected.append(part)
    work = "".join(math_protected)

    # 3. 提取 Markdown 图片占位（与数学/代码互不干扰）
    def _take_figure(match: re.Match[str]) -> str:
        alt = match.group("alt") or match.group("src") or ""
        return _stash(f"\\textbf{{[图: {escape_latex(alt)}]}}")

    work = FIGURE_RE.sub(_take_figure, work)

    # 4. 转换剩余普通文本中的粗体/斜体与 LaTeX 特殊字符
    work = _render_emphasis(work)

    # 5. 逆序还原受保护片段，保证即便外层占位包裹了早先生成的内层占位也能完整展开
    for idx in range(len(tokens) - 1, -1, -1):
        work = work.replace(f"\x00T{idx}\x00", tokens[idx])
    return work


# 宿主环境名：这些环境一旦被内容闭合，注入的命令就会逃出原本的上下文
HOST_ENVIRONMENTS = (
    "figure", "table", "longtable", "tabular", "equation", "equation*", "align", "align*",
    "itemize", "enumerate", "description", "quote", "quotation", "verbatim", "Verbatim",
    "center", "flushleft", "flushright", "minipage", "lstlisting", "minted", "document",
)

_END_ENV_RE = re.compile(r"\\end\s*\{([^}]*)\}")
_BEGIN_ENV_RE = re.compile(r"\\begin\s*\{([^}]*)\}")


def neutralize_environment_breaks(text: str) -> str:
    r"""把内容里试图闭合宿主环境的 \end{...} / \begin{...} 变成无害文本。

    内容来自不可信 Markdown：代码块、公式块都要放进宿主环境，
    如果内容里出现 \end{Verbatim} 就能闭合环境、让后面的内容变成可执行命令。
    """
    def _replace(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if name.lower() in {env.lower() for env in HOST_ENVIRONMENTS}:
            return "\\texttt{\\textbackslash{}end\\{" + name + "\\}}"
        return match.group(0)

    return _END_ENV_RE.sub(_replace, _BEGIN_ENV_RE.sub(_replace, text))


def neutralize_verbatim_breaks(text: str) -> str:
    r"""Verbatim 环境专用的中和。

    Verbatim 里反斜杠是字面量（不能用 	exttt{} 转义，那会把转义本身也打印出来），
    唯一的办法是破坏掉那个精确的结束标记：
    只有行首正好是 "\end{Verbatim}" 才能闭合环境，
    把它变成 "\end{Verbatim }"（多一个空格）就不再闭合。
    """
    def _replace(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if name.lower() == "verbatim":
            return "\\end{" + name + " }"
        return match.group(0)

    return _END_ENV_RE.sub(_replace, text)


def safe_graphics_path(src: str) -> str:
    """图片路径：只允许安全的相对路径，且必须转义 LaTeX 特殊字符。"""
    from .compiler import sanitize_asset_path

    safe = sanitize_asset_path(src or "")
    if safe is None:
        return ""
    # 路径里的 LaTeX 特殊字符必须转义，否则可以闭合 \includegraphics 的参数
    return escape_latex(safe.replace("/", "/"))



_MATH_ENV_RE = re.compile(
    r"^\\begin\{(equation\*?|align\*?|gather\*?|multline\*?|flalign\*?|eqnarray\*?|displaymath)\}"
    r"([\s\S]*)"
    r"\\end\{\1\}$"
)
_TOP_MATH_ENV_NAMES = (
    "equation*",
    "equation",
    "align*",
    "align",
    "gather*",
    "gather",
    "multline*",
    "multline",
    "flalign*",
    "flalign",
    "eqnarray*",
    "eqnarray",
    "displaymath",
)


def _strip_outer_math_delimiters(tex: str) -> str:
    stripped = tex.strip()
    if stripped.startswith("$$") and stripped.endswith("$$") and len(stripped) >= 4:
        return stripped[2:-2].strip()
    if stripped.startswith("\\[") and stripped.endswith("\\]") and len(stripped) >= 4:
        return stripped[2:-2].strip()
    return stripped


def _render_math_block(source: str, block: Block) -> str:
    """渲染教师或修订产出的数学块，避免重复套用 equation 与重复 \\label（BUG-04）。"""
    cleaned = _strip_outer_math_delimiters(source)
    if not cleaned:
        cleaned = block.text.strip()
    labels = block.meta.get("labels") or []
    default_label = labels[0] if labels else block.block_id
    has_label = bool(re.search(r"\\label\{[^}]+\}", cleaned))

    match = _MATH_ENV_RE.match(cleaned)
    if match:
        env = match.group(1)
        inner = neutralize_environment_breaks(match.group(2).strip())
        if not has_label and not env.endswith("*") and env != "displaymath":
            return f"\\begin{{{env}}}\n{inner}\n\\label{{{default_label}}}\n\\end{{{env}}}\n"
        return f"\\begin{{{env}}}\n{inner}\n\\end{{{env}}}\n"

    # 多段连续独立数学环境（如连续两个 equation/align），同样逐段净化而非二次套壳
    for env in _TOP_MATH_ENV_NAMES:
        if cleaned.startswith(f"\\begin{{{env}}}") and cleaned.endswith(f"\\end{{{env}}}"):
            prefix = f"\\begin{{{env}}}"
            suffix = f"\\end{{{env}}}"
            inner = neutralize_environment_breaks(cleaned[len(prefix):-len(suffix)].strip())
            return f"{prefix}\n{inner}\n{suffix}\n"

    safe_body = neutralize_environment_breaks(cleaned)
    if has_label:
        return f"\\begin{{equation}}\n{safe_body}\n\\end{{equation}}\n"
    return f"\\begin{{equation}}\n{safe_body}\n\\label{{{default_label}}}\n\\end{{equation}}\n"


def render_block(block: Block, teacher_tex: str | None = None) -> tuple[str, list[str], list[str]]:
    """渲染单个块为 TeX。返回 (tex, warnings, assets)。"""
    warnings: list[str] = []
    assets: list[str] = []

    # 修订过的块采用修订后的 TeX
    # （否则"修订写回文档版本"只是记了个账，下一次生成仍按原文渲染）
    revised = (block.meta or {}).get("revised_tex")
    if revised and not teacher_tex:
        teacher_tex = revised

    if teacher_tex:
        tex = teacher_tex.strip()
        if tex.startswith("```"):
            tex = re.sub(r"^```[a-zA-Z]*\n?", "", tex)
            tex = re.sub(r"\n?```$", "", tex)
        if not tex:
            warnings.append(f"{block.block_id}: 教师返回空片段，回退到规则转换")
        elif block.kind == "code":
            # 代码块必须包在 Verbatim 里，并且先中和内容里的 \end{Verbatim}：
            # 直接返回原始内容会让 % 吃掉行尾、_ 触发编译错误，
            # 也能闭合宿主环境注入命令。
            body = neutralize_verbatim_breaks(tex)
            return (
                "\\begin{Verbatim}[fontsize=\\small]\n" + body + "\n\\end{Verbatim}\n",
                warnings,
                assets,
            )
        elif block.kind == "math":
            return _render_math_block(tex, block), warnings, assets
        else:
            # 其它类型：内容也要中和环境闭合（\end{...}）
            return neutralize_environment_breaks(tex) + "\n", warnings, assets

    kind = block.kind
    if kind == "heading":
        number, content = _strip_heading_number(block.text)
        command = SECTION_COMMANDS.get(block.level, "paragraph")
        label = f"\\label{{{block.block_id}}}"
        return f"\\{command}{{{_convert_inline(content)}}} {label}\n", warnings, assets

    if kind == "paragraph":
        body = _convert_inline(block.text)
        if not body.strip():
            return "", warnings, assets
        return f"{body}\n", warnings, assets

    if kind == "list":
        lines = [ln for ln in block.text.split("\n") if ln.strip()]
        ordered = bool(lines) and re.match(r"^\s*\d+[.)]\s", lines[0]) is not None
        env = "enumerate" if ordered else "itemize"
        items: list[str] = []
        for line in lines:
            stripped = re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", line)
            stripped = re.sub(r"^\s{2,}", "", stripped)
            items.append(f"  \\item {_convert_inline(stripped)}")
        return "\\begin{%s}\n%s\n\\end{%s}\n" % (env, "\n".join(items), env), warnings, assets

    if kind == "table":
        rows = [ln for ln in block.text.split("\n") if ln.strip()]
        if not rows:
            return "", warnings, assets
        column_count = max((_table_columns(row) for row in rows), default=1)
        alignment = "l" * column_count
        body_rows = []
        for position, row in enumerate(rows):
            if position == 1 and re.match(r"^\s*\|?\s*:?-{2,}", row):
                continue
            cells = _split_row(row)
            # 表格里必须转义对齐符：单元格内的数学片段带 & 会让编译失败
            rendered = " & ".join(
                _convert_inline(cell, escape_alignment=True) for cell in cells
            )
            body_rows.append(f"  {rendered} \\\\")
        env = "longtable" if len(rows) > 12 else "tabular"
        header = f"\\begin{{{env}}}{{{alignment}}}\n\\hline\n" + "\n".join(body_rows) + f"\n\\hline\n\\end{{{env}}}\n"
        return header, warnings, assets

    if kind == "code":
        # Verbatim 也会被内容里的 \end{Verbatim} 闭合，必须先中和
        body = neutralize_environment_breaks(block.text)
        return "\\begin{Verbatim}[fontsize=\\small]\n" + body + "\n\\end{Verbatim}\n", warnings, assets

    if kind == "math":
        source = block.text.strip()
        if not source:
            return "", warnings, assets
        labels = block.meta.get("labels") or []
        label = f"\\label{{{labels[0]}}}" if labels else f"\\label{{{block.block_id}}}"
        # 公式内容同样可能闭合宿主环境
        source = neutralize_environment_breaks(source)
        # 带 & 的公式不能放在 equation 里（不允许对齐符）：
        # equation 会报 "Misplaced alignment tab character &"，改用 align。
        env = "align" if "&" in source else "equation"
        return f"\\begin{{{env}}}\n{source}\n{label}\n\\end{{{env}}}\n", warnings, assets

    if kind == "quote":
        body = _convert_inline(block.text)
        return f"\\begin{{quote}}\n{body}\n\\end{{quote}}\n", warnings, assets

    if kind == "image":
        src = block.meta.get("src", "")
        alt = block.meta.get("alt", "")
        assets.append(src)
        safe_src = safe_graphics_path(src)
        if not safe_src:
            warnings.append(f"{block.block_id}: 图片路径不安全或越界，已忽略（{src}）")
        return (
            "\\begin{figure}[htbp]\n"
            "  \\centering\n"
            + (f"  \\includegraphics[width=0.8\\textwidth]{{{safe_src}}}\n" if safe_src else "")
            +
            f"  \\caption{{{escape_latex(alt)}}} % {block.block_id}\n"
            f"  \\label{{{block.block_id}}}\n"
            "\\end{figure}\n",
            warnings,
            assets,
        )

    if kind == "hr":
        return "\\noindent\\rule{\\textwidth}{0.4pt}\n", warnings, assets

    if kind == "html":
        warnings.append(f"{block.block_id}: HTML 块无法自动转换，已按原样注释保留")
        # 必须逐行加注释前缀：只转义 "%" 的话，第二行起就会逃出注释变成可执行命令
        commented = "\n".join(
            "% [HTML 原文] " + line.replace("%", chr(37) + chr(37)) for line in block.text.splitlines()
        )
        return commented + "\n", warnings, assets

    return escape_latex(block.text) + "\n", warnings, assets


def _table_columns(row: str) -> int:
    stripped = row.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return len(stripped.split("|"))


def _split_row(row: str) -> list[str]:
    stripped = row.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _font_chain(command: str, candidates: list[str]) -> str:
    """生成字体回退链：按顺序使用第一个存在的字体。"""
    unique: list[str] = []
    for name in candidates:
        if name and name not in unique:
            unique.append(name)
    if not unique:
        return f"\\{command}{{Latin Modern Roman}}"
    lines: list[str] = []
    indent = ""
    for name in unique[:-1]:
        lines.append(f"{indent}\\IfFontExistsTF{{{name}}}{{\\{command}{{{name}}}}}{{")
        indent += "  "
    lines.append(f"{indent}\\{command}{{{unique[-1]}}}")
    lines.append(indent + "}" * (len(unique) - 1))
    return "\n".join(lines)


def build_preamble(
    settings: Any,
    *,
    template_name: str = "preamble_zh_ctex",
    extra_packages: list[str] | None = None,
    extra_preamble: str = "",
    fontset: str = "",
    overrides_dir: str | Path | None = None,
) -> str:
    r"""生成导言区。

    fontset 为空时按设置自动选择：
    - 使用 ctex 内置字体集（windows / fandol / ubuntu …）时不再手动设置字体；
    - manual 模式使用 \IfFontExistsTF 回退链。
    """
    latex_cfg = settings.latex
    preamble = load_template(template_name, overrides_dir=overrides_dir)
    if fontset:
        resolved = fontset
    else:
        from .compiler import resolve_fontset

        resolved = resolve_fontset(settings)

    if resolved.lower() in {"manual", "none", ""}:
        preamble = preamble.replace("__FONTSET__", "none")
        main_chain = _font_chain(
            "setCJKmainfont",
            [
                latex_cfg.cjk_main_font,
                "Source Han Serif SC",
                "SimSun",
                "Noto Serif CJK SC",
                "FandolSong-Regular",
            ],
        )
        sans_chain = _font_chain(
            "setCJKsansfont",
            [
                latex_cfg.cjk_sans_font,
                "Source Han Sans SC",
                "SimHei",
                "Noto Sans CJK SC",
                "FandolHei-Regular",
            ],
        )
        mono_chain = _font_chain(
            "setCJKmonofont",
            [
                latex_cfg.cjk_mono_font,
                "Sarasa Mono SC",
                "SimHei",
                "Noto Sans Mono CJK SC",
                "FandolFang-Regular",
            ],
        )
        preamble = preamble.replace("__CJK_FONTS__", f"{main_chain}\n{sans_chain}\n{mono_chain}")
    else:
        preamble = preamble.replace("__FONTSET__", resolved)
        preamble = preamble.replace("__CJK_FONTS__", "")

    if extra_packages:
        packages = "\n".join(f"\\usepackage{{{pkg}}}" for pkg in extra_packages)
        preamble = preamble.replace("% ===== 导言区结束 =====", packages + "\n% ===== 导言区结束 =====")
    if extra_preamble:
        preamble = preamble.replace("% ===== 导言区结束 =====", extra_preamble + "\n% ===== 导言区结束 =====")
    return preamble


def assemble_document(
    blocks: list[Block],
    *,
    settings: Any,
    title: str = "",
    subtitle: str = "",
    teacher_outputs: dict[str, str] | None = None,
    template_name: str = "preamble_zh_ctex",
    extra_packages: list[str] | None = None,
    extra_preamble: str = "",
    overrides_dir: str | Path | None = None,
) -> AssembleResult:
    """组装完整 .tex，并返回行映射。"""
    teacher_outputs = teacher_outputs or {}
    preamble = build_preamble(
        settings,
        overrides_dir=overrides_dir,
        template_name=template_name,
        extra_packages=extra_packages,
        extra_preamble=extra_preamble,
    )
    title_block = load_template("title", overrides_dir=overrides_dir)
    title_block = title_block.replace("__TITLE__", escape_latex(title or "未命名文档"))
    title_block = title_block.replace("__SUBTITLE__", escape_latex(subtitle))

    lines: list[str] = []
    line_map: dict[str, dict[str, int]] = {}
    warnings: list[str] = []
    assets: list[str] = []

    def append(text: str) -> None:
        for line in text.rstrip("\n").split("\n"):
            lines.append(line)

    append(preamble)
    append(title_block)

    for block in blocks:
        tex, block_warnings, block_assets = render_block(block, teacher_outputs.get(block.block_id))
        warnings.extend(block_warnings)
        assets.extend(block_assets)
        start = len(lines) + 1
        if tex:
            append(tex)
        end = len(lines)
        line_map[block.block_id] = {"start": start, "end": max(start, end), "kind": block.kind}

    append("\\end{document}")
    text = "\n".join(lines) + "\n"
    return AssembleResult(
        tex=text,
        line_map=line_map,
        warnings=warnings,
        assets=sorted({a for a in assets if a}),
        preamble=preamble,
    )
