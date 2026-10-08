"""HTML 构建块：Dashboard 页面与 HTML Export 共用（11-dashboard-and-export.md §9）。

安全约定：

- 所有动态文本一律经过 :func:`esc`（``html.escape``，含引号转义）；
- ``table`` / ``dl`` / ``details`` 等构建函数对传入单元格 / 值自动转义，
  调用方无法绕过（不提供接受 raw HTML 的入口）；
- Event 中的 HTML / JS 永远按纯文本显示，Patch 内容只在 ``<pre>`` 中展示。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from html import escape


def esc(value: object) -> str:
    """任意值 -> 转义后的 HTML 文本（XSS 防线，见 11 §9）。"""
    return escape("" if value is None else str(value), quote=True)


def tag(name: str, *children: str, **attributes: str) -> str:
    """单标签构建；属性值与子内容均视为文本（子内容需调用方已转义）。"""
    parts = [f"<{name}"]
    for key, value in attributes.items():
        key = key.rstrip("_")  # class_ 等避开关键字
        parts.append(f' {key}="{esc(value)}"')
    inner = "".join(children)
    parts.append(f">{inner}</{name}>")
    return "".join(parts)


def cell(value: object, *, heading: bool = False) -> str:
    name = "th" if heading else "td"
    return tag(name, esc(value))


def table(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    """转义表格：headers / cells 都是纯文本。"""
    head = "".join(cell(h, heading=True) for h in headers)
    body = "".join("".join(cell(c) for c in row) for row in rows)
    return tag("table", tag("thead", tag("tr", head)), tag("tbody", body))


def ul(values: Iterable[object]) -> str:
    return tag("ul", *(tag("li", esc(v)) for v in values))


def para(text: object, **attributes: str) -> str:
    return tag("p", esc(text), **attributes)


def details_block(summary: object, *body: str, open_: bool = False) -> str:
    """可折叠区块（Checkpoint 等默认折叠，Timeline 11 §2）。"""
    attrs = {"open": "open"} if open_ else {}
    return tag("details", tag("summary", esc(summary)), *body, **attrs)


def pre(text: str) -> str:
    """纯文本预览（Patch 以纯文本显示，11 §9）；内容仍需先转义。"""
    return tag("pre", esc(text))
