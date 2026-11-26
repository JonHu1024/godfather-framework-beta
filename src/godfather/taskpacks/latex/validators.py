"""四层质量检查（设计文档 §5.4）。

| 层次 | 检查内容 | 限制 |
| --- | --- | --- |
| 工程 | 编译退出码、资源缺失、未定义引用、日志错误 | 编译成功不代表内容正确 |
| 内容 | 标题/列表/表格结构、公式与关键符号保留、缺段重复、引用完整性 | PDF 文本提取不可靠 |
| 版面 | 越界、遮挡、表格裁切、图片比例、异常分页 | 自动检测需保留原图供人复核 |
| 偏好 | 字体层级、颜色、定理环境、疏密与模板一致性 | 审美评价不是客观真值 |

每个检查返回 dict：{layer, verdict(pass|warn|fail|skipped), findings:[...], summary}
判定只做“预警”，不宣称绝对正确；`fail` 也不等于人工确认。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .mdparse import Block

LAYERS = ("engineering", "content", "layout", "preference")

FINDING_REFERENCE_UNDEFINED = re.compile(r"LaTeX Warning: Reference `([^']+)' on page \d+ undefined")
FINDING_CITATION_UNDEFINED = re.compile(r"LaTeX Warning: Citation `([^']+)' on page \d+ undefined")
# 必须带 MULTILINE：TeX 日志里 Overfull/Underfull 出现在任意一行，
# 没有 MULTILINE 时 ^ 只匹配整个字符串开头，真实日志一条都命中不了。
FINDING_OVERFULL = re.compile(r"^(Overfull|Underfull) \\([hv])box", re.MULTILINE)
FINDING_MISSING_FILE = re.compile(r"(?i)(! LaTeX Error: File `([^']+)' not found|Can't find (?:file )?`([^']+)')")
FINDING_MULTI_DEF = re.compile(r"Label `([^']+)' multiply defined")
FINDING_FLOAT_LOST = re.compile(r"Float too large for page|Float(s) lost")
FINDING_UNDEFINED_REF_TEXT = re.compile(r"\\ref\{([^}]+)\}")
MATH_INLINE = re.compile(r"\$\$?[^$\n]+\$\$?")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _finding(level: str, code: str, message: str, **extra: Any) -> dict:
    return {"level": level, "code": code, "message": message, **extra}


def _verdict(findings: list[dict]) -> str:
    if any(f["level"] == "fail" for f in findings):
        return "fail"
    if any(f["level"] == "warn" for f in findings):
        return "warn"
    return "pass"


# --------------------------------------------------------------------------
# 工程层
# --------------------------------------------------------------------------
# 正文里不允许出现的命令（导言区之外的正文出现即视为可疑）
DANGEROUS_COMMANDS = (
    # 文件访问 / 可执行 / 展开类命令（正文里不应出现）
    (r"\\(?:input|include|openout\d*|read\d*|newwrite|write18|immediate|special|csname|expandafter)\b",
     "dangerous_command", "正文出现文件访问或可执行类命令", "fail"),
    # 宏包与宏定义（正文里出现通常意味着拼装错误）
    (r"\\(?:usepackage|RequirePackage)\b", "dangerous_package", "正文出现宏包加载命令", "warn"),
    (r"\\(?:def|let|newcommand|renewcommand)\b", "dangerous_definition", "正文出现宏定义命令", "warn"),
)

PREAMBLE_END_MARKER = "% ===== 导言区结束 ====="


def body_of(tex_text: str) -> str:
    """取正文部分（导言区里的宏包是合法的）。"""
    if PREAMBLE_END_MARKER in tex_text:
        return tex_text.split(PREAMBLE_END_MARKER, 1)[1]
    if "\\begin{document}" in tex_text:
        return tex_text.split("\\begin{document}", 1)[1]
    return tex_text


# Pandoc 3.x 会在嵌套枚举前自动生成的标准列表编号宏定义（BUG-07：不应误报为危险定义）
_SAFE_PANDOC_ENUM_DEF_RE = re.compile(
    r"^\\def\\labelenum(?:i|ii|iii|iv)\{\\(?:arabic|alph|Alph|roman|Roman)\{enum(?:i|ii|iii|iv)\}[^}]{0,12}\}"
)


def check_dangerous_commands(tex_text: str) -> list[dict]:
    """检查正文里是否混入了危险命令。

    数学片段按原样保留（公式保真优先），因此恶意内容可能藏在 $...$ 里；
    这里做检测并报告，交由人判断，而不是悄悄改写公式。
    """
    findings: list[dict] = []
    body = body_of(tex_text)

    # \end{document} / \begin{document} 只能各出现一次：多出来的就是"提前截断"式注入
    for command, code, message in (
        ("\\end{document}", "document_ended_early", "正文中 \\end{document} 出现多次，可能提前截断文档"),
        ("\\begin{document}", "document_restarted", "正文中 \\begin{document} 出现多次"),
    ):
        count = body.count(command)
        if count > 1:
            findings.append(_finding("fail", code, f"{message}（{count} 次）"))

    for pattern, code, message, level in DANGEROUS_COMMANDS:
        for match in re.finditer(pattern, body):
            tail = body[match.start() :]
            if code == "dangerous_definition" and _SAFE_PANDOC_ENUM_DEF_RE.match(tail):
                continue
            snippet = body[max(0, match.start() - 30) : match.start() + 60].replace("\n", " ")
            findings.append(
                _finding(level, code, f"{message}：{snippet.strip()[:120]}")
            )
            break  # 每类只报一次，避免刷屏
    return findings



def check_engineering(
    *,
    compile_result: dict[str, Any] | None,
    assets: list[str],
    available_assets: set[str] | None = None,
    tex_text: str = "",
) -> dict:
    findings: list[dict] = []
    if compile_result is None:
        return {
            "layer": "engineering",
            "verdict": "skipped",
            "findings": [_finding("warn", "no_compile", "尚未编译，跳过工程层检查")],
            "summary": "未编译",
        }

    if not compile_result.get("engine_found", True):
        findings.append(
            _finding(
                "fail",
                "engine_missing",
                compile_result.get("error") or "未找到 LaTeX 引擎",
            )
        )
        return _report("engineering", findings)

    exit_code = compile_result.get("exit_code", -1)
    if compile_result.get("timed_out"):
        findings.append(_finding("fail", "timeout", compile_result.get("error") or "编译超时"))
    elif exit_code != 0:
        findings.append(
            _finding("fail", "compile_failed", f"编译失败（退出码 {exit_code}）", error=compile_result.get("error", ""))
        )
    elif not compile_result.get("pdf_path"):
        findings.append(_finding("fail", "no_pdf", "编译成功但未生成 PDF"))

    log = compile_result.get("log", "") or ""
    for match in FINDING_REFERENCE_UNDEFINED.finditer(log):
        findings.append(
            _finding("warn", "undefined_reference", f"未定义引用：{match.group(1)}", label=match.group(1))
        )
    for match in FINDING_CITATION_UNDEFINED.finditer(log):
        findings.append(
            _finding("warn", "undefined_citation", f"未定义引文：{match.group(1)}", label=match.group(1))
        )
    for match in FINDING_MISSING_FILE.finditer(log):
        name = match.group(2) or match.group(3) or "未知文件"
        findings.append(_finding("fail", "missing_file", f"缺少文件：{name}", asset=name))
    for match in FINDING_MULTI_DEF.finditer(log):
        findings.append(_finding("warn", "multiply_defined", f"标签重复定义：{match.group(1)}"))
    if "Error" in log and "!" not in log:
        findings.append(_finding("warn", "log_error", "日志中出现错误文本，请人工复核"))

    findings.extend(check_dangerous_commands(tex_text))

    for asset in assets:
        if available_assets is None:
            continue
        names = {Path(item).name for item in available_assets}
        if Path(asset).name not in names:
            findings.append(_finding("fail", "missing_asset", f"引用了不存在的资源：{asset}", asset=asset))

    return _report("engineering", findings)


# --------------------------------------------------------------------------
# 内容层
# --------------------------------------------------------------------------
def check_content(
    *,
    blocks: list[Block],
    tex_text: str,
    source_text: str = "",
) -> dict:
    findings: list[dict] = []
    normalized_tex = _normalize_for_compare(tex_text)

    # 标题结构
    for block in blocks:
        if block.kind != "heading":
            continue
        content = re.sub(r"^\s*\d+(?:\.\d+)*[.、\s]+", "", block.text).strip()
        if content and _normalize_for_compare(content, source=True) not in normalized_tex:
            findings.append(
                _finding("warn", "missing_heading", f"标题内容未在 TeX 中找到：{content[:60]}", block_id=block.block_id)
            )

    # 列表条目数量（整体比较，转换后无法按块一一对应）
    source_items = 0
    for block in blocks:
        if block.kind != "list":
            continue
        source_items += len([ln for ln in block.text.split("\n") if re.match(r"^\s*([-*+]|\d+[.)])\s", ln)])
    tex_items = len(re.findall(r"\\item\b", tex_text))
    if source_items and tex_items < source_items:
        findings.append(
            _finding(
                "warn",
                "list_items_lost",
                f"列表条目可能缺失：源 {source_items} 项，TeX 共 {tex_items} 个 \\item",
            )
        )

    # 公式与关键符号保留（按源串比对；允许等价改写，故只预警）
    # 注意：mdparse 已经把 $$ 定界符剥掉了，再从 source_text 里用正则找公式必然找不到，
    # 这个检查会变成"永远 pass"。这里直接用 math 块的原文。
    source_math: list[str] = []
    for block in blocks:
        if block.kind != "math":
            continue
        expr = (block.text or "").strip()
        if expr:
            source_math.append(expr)
    if not source_math and source_text:
        source_math = _extract_math(source_text)
    for expr in source_math:
        if expr and expr not in tex_text:
            findings.append(
                _finding(
                    "warn",
                    "math_changed",
                    "公式源串未原样出现在 TeX 中（可能是等价改写，需人工确认）",
                    expression=expr[:120],
                )
            )

    # 缺段 / 重复段
    paragraph_texts = [
        _normalize_for_compare(b.text) for b in blocks if b.kind == "paragraph" and len(b.text.strip()) >= 12
    ]
    seen: dict[str, int] = {}
    for text in paragraph_texts:
        seen[text] = seen.get(text, 0) + 1
    for text, count in seen.items():
        if count > 1:
            findings.append(_finding("warn", "duplicate_paragraph", "源文档存在重复段落", excerpt=text[:60]))

    normalized_tex_counts: dict[str, int] = {}
    for text in paragraph_texts:
        if text in normalized_tex:
            normalized_tex_counts[text] = normalized_tex.count(text)
    for text, count in normalized_tex_counts.items():
        if count > 1:
            findings.append(_finding("warn", "duplicate_in_tex", "TeX 中出现重复段落", excerpt=text[:60]))

    # 引用完整性：\ref{label} 是否在文档中有对应 \label
    labels = set(re.findall(r"\\label\{([^}]+)\}", tex_text))
    for match in FINDING_UNDEFINED_REF_TEXT.finditer(tex_text):
        label = match.group(1)
        if label not in labels:
            findings.append(
                _finding("warn", "ref_without_label", f"\\ref{{{label}}} 没有对应的 \\label", label=label)
            )

    # 中文丢失检测
    if source_text and CJK_RE.search(source_text) and not CJK_RE.search(tex_text):
        findings.append(_finding("fail", "cjk_lost", "源文含中文但 TeX 中未出现任何中文，疑似字体/编码问题"))

    # 思考标签泄漏或输出严重截断检测（BUG-08）
    if "<think>" in tex_text or "</think>" in tex_text:
        findings.append(_finding("warn", "thinking_tag_leaked", "TeX 中残留模型思考过程标签（<think>），建议清理或重跑"))

    body_norm = _normalize_for_compare(body_of(tex_text))
    missing_paras = [p for p in paragraph_texts if p not in normalized_tex]
    if len(paragraph_texts) >= 3 and len(missing_paras) / len(paragraph_texts) > 0.6:
        findings.append(
            _finding(
                "warn",
                "paragraphs_heavily_missing",
                f"较多源段落未出现在 TeX 中（缺失 {len(missing_paras)}/{len(paragraph_texts)} 段，可能发生输出截断）",
            )
        )
    elif blocks:
        source_joined = _normalize_for_compare(" ".join(b.text for b in blocks if b.text), source=True)
        if len(source_joined) >= 120 and len(body_norm) < max(20, int(len(source_joined) * 0.28)):
            findings.append(
                _finding(
                    "warn",
                    "content_severely_truncated",
                    f"TeX 正文长度相对原文明显偏短（原文约 {len(source_joined)} 字符，TeX 正文仅约 {len(body_norm)} 字符），请核对是否截断",
                )
            )

    return _report("content", findings)


# --------------------------------------------------------------------------
# 版面层
# --------------------------------------------------------------------------
def check_layout(*, compile_result: dict[str, Any] | None, page_images: list[str] | None = None) -> dict:
    findings: list[dict] = []
    if compile_result is None:
        return {
            "layer": "layout",
            "verdict": "skipped",
            "findings": [_finding("warn", "no_compile", "尚未编译，跳过版面层检查")],
            "summary": "未编译",
        }
    log = compile_result.get("log", "") or ""
    overfull = 0
    for match in FINDING_OVERFULL.finditer(log):
        overfull += 1
        if overfull <= 20:
            findings.append(
                _finding("warn", "overfull_box", f"可能越界：{match.group(0).strip()}", kind=match.group(1))
            )
    if overfull > 20:
        findings.append(_finding("warn", "overfull_many", f"共 {overfull} 处 overfull/underfull 警告"))
    for _float_match in FINDING_FLOAT_LOST.finditer(log):
        findings.append(_finding("warn", "float_lost", "浮动体（图片/表格）可能被丢弃"))
    if not page_images:
        findings.append(
            _finding("info", "no_page_images", "未生成页面图像，版面需人工在预览中复核")
        )
    return _report("layout", findings)


# --------------------------------------------------------------------------
# 偏好层
# --------------------------------------------------------------------------
def check_preference(*, tex_text: str, policy_preferences: list[dict] | None = None) -> dict:
    findings: list[dict] = []
    preferences = policy_preferences or []
    for pref in preferences:
        rule = (pref.get("rule") or {}) if isinstance(pref, dict) else {}
        kind = rule.get("kind")
        if kind == "require_package":
            package = rule.get("package")
            if package and f"\\usepackage{{{package}}}" not in tex_text:
                findings.append(
                    _finding("warn", "preference_package_missing", f"偏好要求的宏包缺失：{package}", pref_id=pref.get("pref_id"))
                )
        elif kind == "require_environment":
            env = rule.get("environment")
            if env and f"\\begin{{{env}}}" not in tex_text:
                findings.append(
                    _finding("warn", "preference_environment_missing", f"偏好要求的环境未出现：{env}", pref_id=pref.get("pref_id"))
                )
        elif kind == "forbid_command":
            command = rule.get("command")
            if command and command in tex_text:
                findings.append(
                    _finding("warn", "preference_command_present", f"偏好禁止的命令出现：{command}", pref_id=pref.get("pref_id"))
                )
    return _report("preference", findings)


def _report(layer: str, findings: list[dict]) -> dict:
    verdict = _verdict(findings)
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding["level"]] = counts.get(finding["level"], 0) + 1
    return {
        "layer": layer,
        "verdict": verdict,
        "counts": counts,
        "findings": findings,
        "summary": f"{layer}: {verdict}（{counts.get('fail', 0)} 失败 / {counts.get('warn', 0)} 警告）",
    }


def _normalize_for_compare(text: str, *, source: bool = False) -> str:
    r"""去掉 LaTeX 语法后再比较文本内容（保留命令参数里的文字）。

    source=True 表示这是 Markdown 源文本：反斜杠是转义符（\path 表示字面量 path），
    要保留字母；否则源里的 "\path" 与 TeX 里的 "\textbackslash{}path" 归一化结果不同，
    会把正常标题误判成"缺段"。

    反斜杠词法要统一：源里的 \path 与 TeX 里的 \textbackslash{} 归一化结果不同，
    会把正常标题误判成"缺段"。
    """
    # 转义写法与原写法统一成同一个占位符
    # \textbackslash{} 只是"转义了一个反斜杠"，后面的文字是字面量
    text = re.sub(r"\\textbackslash\{\}", "", text)
    if source:
        text = re.sub(r"\\([a-zA-Z]+)\*?", r"\1", text)
    else:
        text = re.sub(r"\\[a-zA-Z]+\*?", " ", text)
    text = re.sub(r"[{}\\$&%#_~^]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _extract_math(text: str) -> list[str]:
    return [m.group(0).strip() for m in MATH_INLINE.finditer(text)]


def run_all(
    *,
    compile_result: dict[str, Any] | None,
    blocks: list[Block],
    tex_text: str,
    source_text: str = "",
    assets: list[str] | None = None,
    available_assets: set[str] | None = None,
    policy_preferences: list[dict] | None = None,
    page_images: list[str] | None = None,
) -> list[dict]:
    return [
        check_engineering(
            compile_result=compile_result,
            assets=assets or [],
            available_assets=available_assets,
            tex_text=tex_text,
        ),
        check_content(blocks=blocks, tex_text=tex_text, source_text=source_text),
        check_layout(compile_result=compile_result, page_images=page_images),
        check_preference(tex_text=tex_text, policy_preferences=policy_preferences),
    ]
