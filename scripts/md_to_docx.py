#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把提交材料目录下的 Markdown 转成排版良好的 DOCX。

用于满足竞赛规程对「研究报告 / 设计文档」的文件要求（.docx）。
纯 Markdown 源文件同时保留，便于后续修改后重新生成。

依赖：python-docx
用法：
    python scripts/md_to_docx.py --src-dir<材料目录> --out-dir <输出目录>
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor, Cm

# 与 PPT 保持一致的海洋主题
NAVY = RGBColor(0x0F, 0x2E, 0x4C)
OCEAN = RGBColor(0x1D, 0x6A, 0x9C)
TEAL = RGBColor(0x00, 0x8C, 0x96)
GRAY = RGBColor(0x44, 0x55, 0x66)
RED = RGBColor(0xB0, 0x30, 0x22)
DARK = RGBColor(0x1B, 0x2A, 0x38)

CJK = "Microsoft YaHei"
MONO = "Consolas"

H1_COLOR = NAVY
H2_COLOR = OCEAN


# --------------------------------------------------------------------------
# 低层排版工具
# --------------------------------------------------------------------------

def set_cjk_font(run, font: str) -> None:
    """python-docx 对中文字体需同时设置 eastAsia 属性。"""
    run.font.name = font
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), font)
    rfonts.set(qn("w:ascii"), font)
    rfonts.set(qn("w:hAnsi"), font)


def shade_cell(cell, hex_color: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tc_pr.append(shd)


def add_hr(paragraph, color="CCCCCC") -> None:
    """给段落加一条下边框，用作分隔线。"""
    p_pr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), color)
    borders.append(bottom)
    p_pr.append(borders)


def add_page_number_footer(section) -> None:
    """页脚：左作品名，右页码。"""
    p = section.footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("探海灵眸 Oceanus  ·  ")
    run.font.size = Pt(8)
    run.font.color.rgb = GRAY
    set_cjk_font(run, CJK)

    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), "PAGE")
    inner_r = OxmlElement("w:r")
    inner_rpr = OxmlElement("w:rPr")
    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), "16")
    inner_rpr.append(sz)
    inner_r.append(inner_rpr)
    t = OxmlElement("w:t")
    t.text = "1"
    inner_r.append(t)
    fld.append(inner_r)
    p._p.append(fld)


# --------------------------------------------------------------------------
# 行内解析：**加粗** 与 `等宽`
# --------------------------------------------------------------------------

INLINE_RE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")


def add_inline(paragraph, text: str, *, size=10.5, color=DARK, base_bold=False):
    """解析 **粗体** 与 `等宽`，写入 paragraph。"""
    for part in INLINE_RE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            run = paragraph.add_run(part[2:-2])
            run.bold = True
            set_cjk_font(run, CJK)
            run.font.size = Pt(size)
            run.font.color.rgb = color
        elif part.startswith("`") and part.endswith("`"):
            run = paragraph.add_run(part[1:-1])
            set_cjk_font(run, MONO)
            run.font.size = Pt(size - 0.5)
            run.font.color.rgb = RED
        else:
            run = paragraph.add_run(part)
            run.bold = base_bold
            set_cjk_font(run, CJK)
            run.font.size = Pt(size)
            run.font.color.rgb = color
    return paragraph


# --------------------------------------------------------------------------
# 块级解析
# --------------------------------------------------------------------------

def build_document(md_text: str, title: str) -> Document:
    doc = Document()
    sec = doc.sections[0]
    sec.top_margin = Cm(2.2)
    sec.bottom_margin = Cm(2.0)
    sec.left_margin = Cm(2.4)
    sec.right_margin = Cm(2.4)
    add_page_number_footer(sec)

    style = doc.styles["Normal"]
    style.font.size = Pt(10.5)
    set_cjk_font(style.font._element.get_or_add_rPr() and style, CJK) \
        if False else None

    lines = md_text.split("\n")
    i = 0
    in_code = False
    code_buf: list[str] = []
    para_buf: list[str] = []

    def flush_para() -> None:
        nonlocal para_buf
        if not para_buf:
            return
        text = " ".join(s.strip() for s in para_buf).strip()
        para_buf = []
        if not text:
            return
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(6)
        p.paragraph_format.line_spacing = 1.35
        add_inline(p, text)

    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()

        # 代码块
        if line.strip().startswith("```"):
            if in_code:
                p = doc.add_paragraph()
                p.paragraph_format.space_before = Pt(4)
                p.paragraph_format.space_after = Pt(8)
                p.paragraph_format.line_spacing = 1.0
                p.paragraph_format.left_indent = Cm(0.5)
                run = p.add_run("\n".join(code_buf))
                set_cjk_font(run, MONO)
                run.font.size = Pt(9)
                run.font.color.rgb = RGBColor(0x22, 0x44, 0x55)
                code_buf = []
                in_code = False
            else:
                flush_para()
                in_code = True
            i += 1
            continue
        if in_code:
            code_buf.append(raw)
            i += 1
            continue

        # 表格
        if line.strip().startswith("|") and i + 1 < len(lines) \
                and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            flush_para()
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            add_table(doc, block)
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush_para()
            level = len(m.group(1))
            text = m.group(2).strip()
            add_heading(doc, text, level)
            i += 1
            continue

        # 分隔线
        if re.match(r"^\s*(-{3,}|\*{3,})\s*$", line):
            flush_para()
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(8)
            add_hr(p)
            i += 1
            continue

        # 引用块
        if line.strip().startswith(">"):
            flush_para()
            content = line.strip().lstrip(">").strip()
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.8)
            p.paragraph_format.space_before = Pt(3)
            p.paragraph_format.space_after = Pt(3)
            add_inline(p, content, size=10, color=GRAY)
            add_hr(p, "1D6A9C")
            i += 1
            continue

        # 列表
        m = re.match(r"^\s*([-*])\s+(.*)$", line)
        if m:
            flush_para()
            p = doc.add_paragraph(style="List Bullet")
            p.paragraph_format.space_after = Pt(3)
            p.paragraph_format.line_spacing = 1.3
            add_inline(p, m.group(2))
            i += 1
            continue

        m = re.match(r"^\s*(\d+)\.\s+(.*)$", line)
        if m:
            flush_para()
            p = doc.add_paragraph(style="List Number")
            p.paragraph_format.space_after = Pt(3)
            p.paragraph_format.line_spacing = 1.3
            add_inline(p, m.group(2))
            i += 1
            continue

        # 空行
        if not line.strip():
            flush_para()
            i += 1
            continue

        para_buf.append(line)
        i += 1

    flush_para()
    return doc


def add_heading(doc: Document, text: str, level: int) -> None:
    if level == 1:
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(18)
        p.paragraph_format.space_after = Pt(8)
        run = p.add_run(text)
        run.bold = True
        run.font.size = Pt(16)
        run.font.color.rgb = H1_COLOR
        set_cjk_font(run, CJK)
        add_hr(p, "1D6A9C")
    elif level == 2:
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(13)
        p.paragraph_format.space_after = Pt(5)
        add_inline(p, text, size=13, color=H2_COLOR, base_bold=True)
    else:
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(9)
        p.paragraph_format.space_after = Pt(4)
        add_inline(p, text, size=11.5, color=NAVY, base_bold=True)


def add_table(doc: Document, block: list[str]) -> None:
    rows = []
    for ln in block:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        rows.append(cells)
    if len(rows) < 2:
        return
    header, body = rows[0], rows[2:]
    ncol = len(header)
    if ncol == 0:
        return

    table = doc.add_table(rows=1 + len(body), cols=ncol)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    for j, txt in enumerate(header):
        cell = table.cell(0, j)
        cell.text = ""
        shade_cell(cell, "0F2E4C")
        p = cell.paragraphs[0]
        p.paragraph_format.space_after = Pt(2)
        p.paragraph_format.space_before = Pt(2)
        run = p.add_run(txt)
        run.bold = True
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        set_cjk_font(run, CJK)

    for r, cells in enumerate(body, start=1):
        for j in range(ncol):
            txt = cells[j] if j < len(cells) else ""
            cell = table.cell(r, j)
            cell.text = ""
            if r % 2 == 0:
                shade_cell(cell, "F2F6F9")
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.space_before = Pt(2)
            add_inline(p, txt, size=9)

    doc.add_paragraph().paragraph_format.space_after = Pt(4)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def convert_one(src: Path, out: Path) -> None:
    md = src.read_text(encoding="utf-8")
    # 去掉 markdown 顶部的一级标题（会在封面呈现）
    doc = build_document(md, src.stem)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))


def main() -> int:
    ap = argparse.ArgumentParser(description="Markdown 转 DOCX")
    ap.add_argument("--files", nargs="+", required=True,
                    help="需要转换的 .md 文件路径")
    args = ap.parse_args()
    for f in args.files:
        src = Path(f)
        out = src.with_suffix(".docx")
        convert_one(src, out)
        print(f"[OK] {src.name} → {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())