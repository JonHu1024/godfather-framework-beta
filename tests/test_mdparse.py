"""Markdown 结构解析与切块测试。"""
from __future__ import annotations

from godfather.taskpacks.latex.mdparse import chunk_blocks, parse_markdown

SAMPLE = """# 标题一

第一段正文，包含行内公式 $E = mc^2$。

## 标题一之一

- 列表项 A
- 列表项 B

| 列 A | 列 B |
| --- | --- |
| 1 | 2 |

```python
print("不要从这里切断")
```

$$
\\int_0^1 x^2 dx = \\frac{1}{3}
$$

结尾段落。
"""


def test_blocks_are_stable_and_ordered():
    parsed = parse_markdown(SAMPLE)
    ids = [block.block_id for block in parsed.blocks]
    assert ids == sorted(ids)
    assert ids[0] == "block-0001"
    assert len(set(ids)) == len(ids)

    again = parse_markdown(SAMPLE)
    assert [b.block_id for b in again.blocks] == ids


def test_heading_numbering_and_path():
    parsed = parse_markdown(SAMPLE)
    headings = parsed.context["headings"]
    assert headings[0]["number"] == "1"
    assert headings[1]["number"] == "1.1"
    assert headings[1]["path"] == ["标题一", "标题一之一"]


def test_code_fence_is_one_block():
    parsed = parse_markdown(SAMPLE)
    code = [b for b in parsed.blocks if b.kind == "code"]
    assert len(code) == 1
    assert "不要从这里切断" in code[0].text


def test_math_block_and_inline_math_detected():
    parsed = parse_markdown(SAMPLE)
    math_blocks = [b for b in parsed.blocks if b.kind == "math"]
    assert len(math_blocks) == 1
    assert "int_0^1" in math_blocks[0].text
    paragraph = next(b for b in parsed.blocks if b.kind == "paragraph" and "行内公式" in b.text)
    assert paragraph.meta["has_math"] is True


def test_table_columns_and_rows():
    parsed = parse_markdown(SAMPLE)
    table = next(b for b in parsed.blocks if b.kind == "table")
    assert table.meta["columns"] == 2
    assert table.meta["rows"] == 2


def test_chunks_never_split_inside_code_or_math():
    parsed = parse_markdown(SAMPLE)
    chunks = chunk_blocks(parsed.blocks, max_chars=60)
    assert len(chunks) > 1
    originals = {b.block_id: b for b in parsed.blocks}
    for chunk in chunks:
        for block in chunk["blocks"]:
            # 块内容完整，不被截断
            assert block["text"] == originals[block["block_id"]].text
            assert block["block_id"] in chunk["block_ids"]
    # 所有块都出现且只出现一次
    seen = [bid for chunk in chunks for bid in chunk["block_ids"]]
    assert sorted(seen) == sorted(b.block_id for b in parsed.blocks)
    assert len(seen) == len(set(seen))


def test_context_contains_references_and_labels():
    text = "# T\n\n见公式 \\eqref{eq:1}，标签 \\label{eq:1}。\n"
    parsed = parse_markdown(text)
    assert "eq:1" in parsed.context["references"]
    assert "eq:1" in parsed.context["labels"]
