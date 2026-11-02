#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Markdown -> DOCX 转换器（中文合同/文档排版）。

用法:
    python scripts/md2docx.py <input.md> <output.docx>

支持: H1~H3 标题、段落、粗体(**x**)、表格(GFM)、无序/有序列表(含缩进)、
      引用块(>)、分隔线(---)。中文字体：标题黑体，正文宋体，A4 页面。
"""
import re
import sys
from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

BODY_FONT = "宋体"
HEAD_FONT = "黑体"
QUOTE_COLOR = RGBColor(0x60, 0x60, 0x60)


def set_run_font(run, name=BODY_FONT, size=12.0, bold=False, color=None):
    """设置 run 字体（含 eastAsia，保证中文正确显示）。"""
    run.font.name = name
    run.font.size = Pt(size)
    run.bold = bold
    if color is not None:
        run.font.color.rgb = color
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    for attr in ("w:eastAsia", "w:ascii", "w:hAnsi"):
        rFonts.set(qn(attr), name)


def add_runs(par, text, name=BODY_FONT, size=12.0, bold=False, color=None):
    """把含 **粗体** 标记的文本拆成多个 run 写入段落。"""
    text = text.replace("\\", "")
    parts = re.split(r"(\*\*.+?\*\*)", text)
    for part in parts:
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            r = par.add_run(part[2:-2])
            set_run_font(r, name, size, True, color)
        else:
            r = par.add_run(part)
            set_run_font(r, name, size, bold, color)


def set_para_spacing(par, before=0, after=6, line=1.5, indent=0):
    pf = par.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    pf.line_spacing = line
    if indent:
        pf.left_indent = Cm(indent)


def is_table_sep(cells):
    return all(re.fullmatch(r":?-{3,}:?", c.replace(" ", "")) for c in cells) and len(cells) > 0


def split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def convert(md_path, docx_path):
    with open(md_path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    doc = Document()
    # 页面 A4 + 页边距
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2.8)
    sec.top_margin = sec.bottom_margin = Cm(2.5)
    # 默认样式
    style = doc.styles["Normal"]
    style.font.name = BODY_FONT
    style.font.size = Pt(12)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)

    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # 空行
        if not stripped:
            i += 1
            continue

        # 分隔线
        if re.fullmatch(r"-{3,}", stripped):
            i += 1
            continue

        # 表格
        if stripped.startswith("|"):
            block = []
            while i < n and lines[i].strip().startswith("|"):
                block.append(lines[i].strip())
                i += 1
            rows = [split_row(b) for b in block]
            header = rows[0]
            body = [r for r in rows[1:] if not is_table_sep(r)]
            ncols = len(header)
            table = doc.add_table(rows=1 + len(body), cols=ncols)
            table.style = "Table Grid"
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            # 表头
            for ci, val in enumerate(header):
                cell = table.cell(0, ci)
                cell.text = ""
                p = cell.paragraphs[0]
                p.alignment = WD_ALIGN_PARAGRAPH.LEFT
                add_runs(p, val, HEAD_FONT, 10.5, True)
                set_para_spacing(p, 2, 2, 1.2)
            # 数据行
            for ri, row in enumerate(body, start=1):
                for ci in range(ncols):
                    val = row[ci] if ci < len(row) else ""
                    cell = table.cell(ri, ci)
                    cell.text = ""
                    p = cell.paragraphs[0]
                    add_runs(p, val, BODY_FONT, 10.5)
                    set_para_spacing(p, 2, 2, 1.2)
            doc.add_paragraph()
            continue

        # 标题
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            level, text = len(m.group(1)), m.group(2).strip()
            par = doc.add_paragraph()
            if level == 1:
                par.alignment = WD_ALIGN_PARAGRAPH.CENTER
                add_runs(par, text, HEAD_FONT, 18, True)
                set_para_spacing(par, 0, 14, 1.5)
            elif level == 2:
                add_runs(par, text, HEAD_FONT, 14, True)
                set_para_spacing(par, 12, 6, 1.5)
            else:
                add_runs(par, text, HEAD_FONT, 12, True)
                set_para_spacing(par, 8, 4, 1.5)
            i += 1
            continue

        # 引用块
        if stripped.startswith(">"):
            block = []
            while i < n and lines[i].strip().startswith(">"):
                block.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            text = " ".join(x.strip() for x in block if x.strip())
            par = doc.add_paragraph()
            add_runs(par, text, BODY_FONT, 10.5, color=QUOTE_COLOR)
            set_para_spacing(par, 4, 8, 1.4, indent=0.6)
            continue

        # 列表项
        m = re.match(r"^(\s*)([-*]|\d+[.)])\s+(.*)$", line)
        if m:
            ind, marker, text = m.group(1), m.group(2), m.group(3)
            level = len(ind) // 2
            bullet = "· " if marker in "-*" else f"{marker} "
            par = doc.add_paragraph()
            add_runs(par, bullet + text, BODY_FONT, 12)
            set_para_spacing(par, 0, 4, 1.5, indent=0.75 * (level + 1))
            i += 1
            continue

        # 普通段落
        par = doc.add_paragraph()
        add_runs(par, stripped, BODY_FONT, 12)
        set_para_spacing(par, 0, 6, 1.5)
        i += 1

    doc.save(docx_path)
    print(f"[OK] {docx_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    convert(sys.argv[1], sys.argv[2])
