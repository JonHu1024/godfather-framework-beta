"""Markdown 结构解析：分配稳定 block_id，抽取全局上下文。

设计要点（设计文档 §5.3）：
- 标题、段落、公式、表格、图片分配稳定 block_id；
- 归集全局标题层级、术语、引用标识、交叉引用；
- 切块不得从公式、代码围栏或表格中间截断。

本模块是纯 Python 实现，不依赖 Pandoc；即使机器上没有 Pandoc，
资料导入、结构解析与切块依然可用（Pandoc 只用于基线转换）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

ATX_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+-]*)\s*$")
MATH_BLOCK_RE = re.compile(r"^\s*\$\$\s*$")
LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
HTML_BLOCK_RE = re.compile(r"^\s*<(?P<tag>[a-zA-Z][\w-]*)")
IMAGE_ONLY_RE = re.compile(r"^\s*!\[(?P<alt>[^\]]*)\]\((?P<src>[^)\s]+)(?P<rest>[^)]*)\)\s*$")
REF_RE = re.compile(r"\\(?:ref|eqref|autoref|pageref|cref|Cref)\{([^}]*)\}")
CITE_RE = re.compile(r"\\(?:cite|citep|citet)\{([^}]*)\}")
LABEL_RE = re.compile(r"\\label\{([^}]*)\}")
MATH_INLINE_RE = re.compile(r"\$[^$\n]+\$")
FENCE_BODY_RE = re.compile(r"^\s*(`{3,}|~{3,})")


@dataclass
class Block:
    block_id: str
    kind: str
    text: str = ""
    level: int = 0
    path: list[str] = field(default_factory=list)
    line_start: int = 0
    line_end: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = {
            "block_id": self.block_id,
            "kind": self.kind,
            "text": self.text,
            "level": self.level,
            "path": self.path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "meta": self.meta,
        }
        # 修订过的块要保留修订后的 TeX，否则写回文档版本也没有意义
        revised = self.meta.get("revised_tex")
        if revised:
            data["revised_tex"] = revised
        return data


@dataclass
class ParsedDocument:
    title: str
    blocks: list[Block]
    context: dict[str, Any] = field(default_factory=dict)

    def block_texts(self) -> dict[str, str]:
        return {block.block_id: block.text for block in self.blocks}


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _starts_new_block(
    line: str, next_line: str | None = None, *, table_capable: bool = True
) -> bool:
    """判断这一行是否已经是另一个块的开头。

    未闭合的 ``` 或 $$ 会让后面的内容被整段吞掉（标题消失、正文变公式），
    所以扫描块内容时必须在"新块开头"处停下来。
    """
    if not line.strip():
        return True
    if ATX_RE.match(line):
        return True
    if FENCE_RE.match(line):
        return True
    if MATH_BLOCK_RE.match(line):
        return True
    if LIST_RE.match(line) or HR_RE.match(line):
        return True
    if line.lstrip().startswith(">"):
        return True
    if HTML_BLOCK_RE.match(line):
        return True
    # 表格：当前行像表格且下一行是分隔行
    if (
        table_capable
        and "|" in line
        and next_line is not None
        and TABLE_SEP_RE.match(next_line)
    ):
        return True
    return False



# 支持管道表格的方言（其余方言按标准 Markdown 处理，表格不识别）
TABLE_CAPABLE_DIALECTS = ("pandoc-markdown", "gfm", "github", "pipe-tables")


def parse_markdown(text: str, title: str = "", dialect: str = "pandoc-markdown") -> ParsedDocument:
    """把 Markdown 解析为结构块序列。

    ``dialect`` 决定是否识别管道表格：
    - ``pandoc-markdown`` / ``gfm``：识别（默认）
    - 其它（如 ``commonmark`` / ``standard``）：不识别，表格行按普通段落处理

    以前这个字段只写进数据库、没有任何读取方 ——
    用户在界面上选了方言却发现解析行为一字未变。
    """
    lines = _normalize(text).split("\n")
    table_capable = str(dialect or "").strip().lower() in TABLE_CAPABLE_DIALECTS
    blocks: list[Block] = []
    heading_stack: list[str] = []
    counters = [0] * 6
    index = 0
    block_counter = 0
    total = len(lines)
    headings: list[dict] = []
    figures: list[dict] = []
    tables: list[dict] = []
    equations: list[dict] = []
    code_blocks: list[dict] = []
    quotes: list[dict] = []
    warnings: list[str] = []

    def next_id() -> str:
        nonlocal block_counter
        block_counter += 1
        return f"block-{block_counter:04d}"

    while index < total:
        raw = lines[index]
        line_no = index + 1

        if not raw.strip():
            index += 1
            continue

        atx = ATX_RE.match(raw)
        if atx:
            level = len(atx.group(1))
            content = atx.group(2).strip()
            counters[level - 1] += 1
            for idx in range(level, 6):
                counters[idx] = 0
            number = ".".join(str(counters[i]) for i in range(level) if counters[i])
            heading_stack = heading_stack[: level - 1] + [content]
            block = Block(
                block_id=next_id(),
                kind="heading",
                text=content,
                level=level,
                path=list(heading_stack),
                line_start=line_no,
                line_end=line_no,
                meta={"number": number, "numbered_path": ".".join(heading_stack)},
            )
            blocks.append(block)
            headings.append(
                {
                    "block_id": block.block_id,
                    "level": level,
                    "text": content,
                    "number": number,
                    "path": list(heading_stack),
                }
            )
            index += 1
            continue

        fence = FENCE_RE.match(raw)
        if fence:
            marker = fence.group(1)[0] * len(fence.group(1))
            lang = fence.group(2) or ""
            body: list[str] = []
            start = line_no
            index += 1
            while index < total and not FENCE_BODY_RE.match(lines[index]):
                body.append(lines[index])
                index += 1
            if index < total:
                index += 1  # 跳过收尾围栏
            block = Block(
                block_id=next_id(),
                kind="code",
                text="\n".join(body),
                path=list(heading_stack),
                line_start=start,
                line_end=index,
                meta={"language": lang, "fence": marker},
            )
            blocks.append(block)
            code_blocks.append({"block_id": block.block_id, "language": lang})
            continue

        if MATH_BLOCK_RE.match(raw):
            body = []
            start = line_no
            index += 1
            closed = False
            while index < total and not MATH_BLOCK_RE.match(lines[index]):
                # 数学块内允许行首 +, -, *, >, | 等符号（BUG-05），
                # 仅在明显忘记闭合且遇到新章节标题或代码围栏时截断以防吞掉全文。
                if body and (ATX_RE.match(lines[index]) or FENCE_RE.match(lines[index])):
                    break
                body.append(lines[index])
                index += 1
            if index < total and MATH_BLOCK_RE.match(lines[index]):
                index += 1
                closed = True
            if not closed:
                warnings.append(f"第 {start} 行的块级公式没有闭合（缺少 $$），已按原样保留到下一个块之前")
            source = "\n".join(body)
            labels = LABEL_RE.findall(source)
            block = Block(
                block_id=next_id(),
                kind="math",
                text=source,
                path=list(heading_stack),
                line_start=start,
                line_end=index,
                meta={"labels": labels},
            )
            blocks.append(block)
            equations.append({"block_id": block.block_id, "labels": labels})
            continue

        # 表格：当前行像表格且下一行是分隔行
        if (
            table_capable
            and "|" in raw
            and index + 1 < total
            and TABLE_SEP_RE.match(lines[index + 1])
        ):
            start = line_no
            body = []
            while index < total and "|" in lines[index] and lines[index].strip():
                body.append(lines[index])
                index += 1
            columns = _table_columns(body[0]) if body else 0
            block = Block(
                block_id=next_id(),
                kind="table",
                text="\n".join(body),
                path=list(heading_stack),
                line_start=start,
                line_end=index,
                meta={"columns": columns, "rows": max(0, len(body) - 1)},
            )
            blocks.append(block)
            tables.append({"block_id": block.block_id, "columns": columns})
            continue

        if HR_RE.match(raw):
            block = Block(
                block_id=next_id(),
                kind="hr",
                text=raw.strip(),
                path=list(heading_stack),
                line_start=line_no,
                line_end=line_no,
            )
            blocks.append(block)
            index += 1
            continue

        if raw.lstrip().startswith(">"):
            body = []
            start = line_no
            while index < total and lines[index].lstrip().startswith(">"):
                body.append(re.sub(r"^\s*>\s?", "", lines[index]))
                index += 1
            block = Block(
                block_id=next_id(),
                kind="quote",
                text="\n".join(body),
                path=list(heading_stack),
                line_start=start,
                line_end=index,
            )
            blocks.append(block)
            quotes.append({"block_id": block.block_id})
            continue

        list_match = LIST_RE.match(raw)
        if list_match:
            body = []
            start = line_no
            base_indent = len(list_match.group(1))
            while index < total:
                current = lines[index]
                if not current.strip():
                    # 空行后若仍是列表则继续，否则结束
                    if index + 1 < total and LIST_RE.match(lines[index + 1]):
                        body.append("")
                        index += 1
                        continue
                    break
                match = LIST_RE.match(current)
                if not match:
                    if current.startswith("  ") and body:
                        body.append(current)
                        index += 1
                        continue
                    break
                if len(match.group(1)) < base_indent:
                    break
                body.append(current)
                index += 1
            block = Block(
                block_id=next_id(),
                kind="list",
                text="\n".join(body),
                path=list(heading_stack),
                line_start=start,
                line_end=index,
                meta={"items": len([ln for ln in body if LIST_RE.match(ln)])},
            )
            blocks.append(block)
            continue

        html = HTML_BLOCK_RE.match(raw)
        if html:
            body = []
            start = line_no
            while index < total and lines[index].strip():
                # HTML 块不能吞掉后面的标题
                if body and _starts_new_block(
                    lines[index],
                    lines[index + 1] if index + 1 < total else None,
                    table_capable=table_capable,
                ):
                    break
                body.append(lines[index])
                index += 1
            block = Block(
                block_id=next_id(),
                kind="html",
                text="\n".join(body),
                path=list(heading_stack),
                line_start=start,
                line_end=index,
                meta={"tag": html.group("tag")},
            )
            blocks.append(block)
            continue

        # 段落（可能以独占一行的图片开头）
        body = []
        start = line_no
        while index < total and lines[index].strip():
            candidate = lines[index]
            if body and _starts_new_block(
                candidate,
                lines[index + 1] if index + 1 < total else None,
                table_capable=table_capable,
            ):
                break
            body.append(candidate)
            index += 1

        text = "\n".join(body).strip()
        image = IMAGE_ONLY_RE.match(text)
        if image:
            block = Block(
                block_id=next_id(),
                kind="image",
                text=text,
                path=list(heading_stack),
                line_start=start,
                line_end=index,
                meta={"src": image.group("src"), "alt": image.group("alt"), "rest": image.group("rest")},
            )
            blocks.append(block)
            figures.append({"block_id": block.block_id, "src": image.group("src"), "alt": image.group("alt")})
            continue

        block = Block(
            block_id=next_id(),
            kind="paragraph",
            text=text,
            path=list(heading_stack),
            line_start=start,
            line_end=index,
            meta={"has_math": bool(MATH_INLINE_RE.search(text))},
        )
        blocks.append(block)

    title = title or (headings[0]["text"] if headings else "未命名文档")

    context = {
        "title": title,
        "warnings": warnings,
        "headings": headings,
        "figures": figures,
        "tables": tables,
        "equations": equations,
        "code_blocks": code_blocks,
        "quotes": quotes,
        "references": sorted({m for block in blocks for m in REF_RE.findall(block.text)}),
        "citations": sorted({c.strip() for block in blocks for c in CITE_RE.findall(block.text) for c in c.split(",")}),
        "labels": sorted({m for block in blocks for m in LABEL_RE.findall(block.text)}),
        "block_count": len(blocks),
        "heading_outline": [
            {"level": h["level"], "text": h["text"], "number": h["number"]} for h in headings
        ],
    }

    return ParsedDocument(title=title, blocks=blocks, context=context)


def _table_columns(header_line: str) -> int:
    stripped = header_line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return len([cell for cell in stripped.split("|")])


def chunk_blocks(
    blocks: list[Block],
    max_chars: int = 4000,
    max_blocks: int = 12,
    context_chars: int = 600,
) -> list[dict]:
    """按结构切块：不在公式、代码围栏或表格中间截断。

    每个块附带相邻语境（当前标题路径 + 前后各一小段摘要），
    使教师/学生在局部作答时仍能看到上下文。
    """
    chunks: list[dict] = []
    current: list[Block] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if not current:
            return
        chunk_id = f"chunk-{len(chunks) + 1:03d}"
        chunks.append(
            {
                "chunk_id": chunk_id,
                "blocks": [b.to_dict() for b in current],
                "block_ids": [b.block_id for b in current],
                "char_count": current_len,
                "context": _neighbor_context(current, blocks, context_chars),
            }
        )
        current = []
        current_len = 0

    for block in blocks:
        block_len = len(block.text) + 2
        if current and (current_len + block_len > max_chars or len(current) >= max_blocks):
            flush()
        current.append(block)
        current_len += block_len
    flush()
    return chunks


def _neighbor_context(chunk: list[Block], all_blocks: list[Block], limit: int) -> dict:
    first_index = all_blocks.index(chunk[0])
    last_index = all_blocks.index(chunk[-1])
    before = all_blocks[max(0, first_index - 1)]
    after = all_blocks[min(len(all_blocks) - 1, last_index + 1)]
    heading_path = chunk[0].path if chunk else []
    return {
        "heading_path": heading_path,
        "prev_block_id": before.block_id if first_index > 0 else None,
        "prev_excerpt": before.text[:limit] if first_index > 0 else "",
        "next_block_id": after.block_id if last_index + 1 < len(all_blocks) else None,
        "next_excerpt": after.text[:limit] if last_index + 1 < len(all_blocks) else "",
    }
