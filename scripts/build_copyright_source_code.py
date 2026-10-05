#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成软件著作权登记的「程序鉴别材料」（源代码 60 页文档）。

规范依据（中国版权保护中心登记要求）：
  1. 提交前、后各连续 30 页源程序，共 60 页；
  2. 每页不少于 50 行（末页为程序结尾）；
  3. 页眉标注软件全称及版本号；
  4. 每页标注页码。

本脚本从仓库精选自研核心代码，按「入口 → 核心引擎 → 领域服务 →
消息链路 → 边缘感知 → 前端」的顺序拼接为一份源代码流，再截取
前 1500 行（30 页）与后 1500 行（30 页），生成排版固定的 DOCX。

用法：
    .\\.venv-analysis\\Scripts\\python.exe scripts\\build_copyright_source_code.py \
        --out "<USER_HOME>\\Desktop\\Oceanus软著材料"

产物：
    源代码鉴别材料_探海灵眸海洋环境治理智能体软件V1.0.docx
    源程序量统计.txt（供申请表「源程序量」填报）
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parent.parent

SOFTWARE_FULL_NAME = "探海灵眸海洋环境治理智能体软件"
SOFTWARE_SHORT_NAME = "Oceanus"
VERSION = "V1.0"
HEADER_TEXT = f"{SOFTWARE_FULL_NAME}[简称:{SOFTWARE_SHORT_NAME}] {VERSION}"

LINES_PER_PAGE = 50
PAGES_EACH_SIDE = 30
# 超宽行截断，避免 Word 自动换行破坏每页 50 行。
# 版心宽 = 21.0 - 2.0 - 1.6 = 17.4cm ≈ 493pt；Consolas 8.5pt 半角宽约 4.7pt，
# 493 / 4.7 ≈ 104 字符，取 100 留余量，确保不触发软换行。
MAX_LINE_WIDTH = 100

# 排版参数（2026-10-05 按软著审查报告实测反推调整）：
# 审查报告实测原参数（8.5pt / 13.6pt 行距）只有 **33~49 行、平均 40.4 行**，
# 60 页无一页达标 —— 根因是行距为字号的 1.72 倍，明显偏松。
# 现改为 8.0pt / 13.0pt（对标已过审的聆心材料：7.5pt / 13.0pt），
# 并配合 PAD_LINES 补偿 + 剔除源码空行，让「行位」与「有字行」两种口径都 = 50。
FONT_SIZE = 8.0
LINE_SPACING_PT = 13.0
# ★ 分页页的行位补偿：含 <w:br w:type="page"/> 的那一页会额外少排
#   1~2 个行位，必须补回来，否则每页只有 48~49 行。
#   ★ 2026-10-05 本机实测标定：PAD=2 时渲染 49 行（差 1 行），
#     故取 3。这是**实测**值，不是推导值。
#   ⚠️ 换机器 / 换 LibreOffice 版本必须重新标定。
PAD_LINES = 3

GRAY = RGBColor(0x60, 0x60, 0x60)
DARK = RGBColor(0x1B, 0x2A, 0x38)

# Consolas/Courier New 无字形符号 → ASCII 标记（仅排版层替换，不动源码语义）。
# 只替换符号/图形区（Symbol、Emoticons、Dingbats）中 Consolas 确实缺字形的字符；
# 中文标点（、。「」）与全角箭头由 eastAsia 的 Microsoft YaHei 正常渲染，保留原样。
EMOJI_MAP = {
    "★": "[!]",   # ★
    "✅": "[v]",  # ✅
    "⚠": "[!]",   # ⚠
    "❌": "[x]",  # ❌
    "✓": "v",     # ✓
    "✗": "x",     # ✗
    "✕": "x",     # ✕
    "●": "*",     # ●
    "◆": "*",     # ◆
    "▲": "^",     # ▲
    "▼": "v",     # ▼
    "☰": "=",     # ☰
    "⌘": "Cmd",   # ⌘
    "⏱": "T",     # ⏱
}




@dataclass(frozen=True)
class SourceFile:
    """一个待拼接的源代码文件。"""

    path: str
    label: str  # 材料里的模块说明


# 精选文件清单：前 30 页落在「入口 + 派单引擎 + Agent 运行时」，
# 后 30 页落在「工单页面 + 实时通道 + API 封装」并以程序结尾收尾。
CURATED_FILES: list[SourceFile] = [
    # ── 选材原则（P1-4 修���后的口径）──────────────────────────────
    # 软著提交「前 30 页 + 后 30 页」。审查报告 P1-4 指出后 30 页
    # 落在 .vue 的 CSS 规则上，观感是「排版样式」而非「业务逻辑」。
    #
    # 做法：**按行数从小到大排列业务逻辑文件**，让前 30 页落在派单引擎、
    # 智能体规划与工具层；后 30 页（序列尾部）落在机械臂驱动、
    # MQTT 处理、边缘检测 —— 全是业务逻辑，且体现最新工作。
    # .vue 单文件组件放最后，仅在前两段都排不满时才轮到。
    #
    # 为什么不用「把 .vue 放最后」这一招：前 3 个文件合计 1899 行
    # 已经吃掉 1500 行的前 30 页，尾部根本轮不到 .vue —— 实测确认。
    # 必须靠**行数升序**来控制前后两段的落点。
    SourceFile("frontend/src/api/http.js", "前端 API 封装"),
    SourceFile("frontend/src/utils/realtime.js", "WebSocket 实时通道"),
    SourceFile("backend/app/services/agents/planner.py", "智能体规划器"),
    SourceFile("backend/app/services/agents/memory.py", "智能体记忆"),
    SourceFile("frontend/src/stores/realtime.js", "前端实时状态仓库"),
    SourceFile("backend/app/main.py", "平台应用入口（FastAPI lifespan 启动流程）"),
    SourceFile("edge/arm_bridge/ros_driver.py", "机械臂 ROS 控制栈驱动"),
    SourceFile("backend/app/services/agents/tools.py", "智能体工具层"),
    SourceFile("edge/arm_bridge/bridge.py", "机械臂桥接与报文契约"),
    SourceFile("backend/app/api/v1/simulations.py", "后端接口层（工单/轨迹/控制）"),
    SourceFile("backend/app/services/dispatch.py", "事件派单引擎（五步筛选 + 防抖合并）"),
    SourceFile("edge/detector/detector.py", "边缘双通道检测器"),
    SourceFile("edge/main.py", "边缘感知程序入口（取流→检测→时序→上报）"),
    SourceFile("backend/app/mqtt/handlers.py", "MQTT 消息处理"),
    SourceFile("backend/app/services/agents/model.py", "智能体模型定义"),
    SourceFile("backend/app/services/agents/model_adapter.py", "智能体模型适配层"),
    SourceFile("backend/app/services/agents/runtime.py", "智能体运行时内核"),
    SourceFile("backend/app/services/knowledge.py", "知识智能体服务"),
    SourceFile("edge/simulator/simulator.py", "时序校验与边缘模拟"),
    SourceFile("frontend/src/views/DashboardView.vue", "前端大屏页面"),
    SourceFile("frontend/src/views/EventsView.vue", "前端事件页面"),
    SourceFile("frontend/src/views/TasksView.vue", "前端工单页面"),
]


def load_lines(path: Path) -> list[str]:
    """读取源文件并展开 Tab，返回纯文本行列表。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.replace("\t", "    ").splitlines()
    return [ln.rstrip() for ln in lines]


def build_concatenation() -> tuple[list[str], int]:
    """拼接精选文件，返回 (总行列表, 精选总行数)。

    空行处理：软著要求「每页不少于 50 行」，审查员按**有字行**计数。
    若保留源码空行，一页 50 个段落里只有 40 行有内容，会被判定行数不足。
    因此排版层剔除全部空行，保证每页 50 行全部有可见内容（与已过审的
    聆心材料同一处理思路）。空行剔除只影响排版，不改动任何源码语句。
    """
    all_lines: list[str] = []
    total_curated = 0
    for sf in CURATED_FILES:
        path = ROOT / sf.path
        if not path.exists():
            raise FileNotFoundError(f"精选文件缺失: {sf.path}")
        file_lines = load_lines(path)
        total_curated += len(file_lines)
        all_lines.append(f"# ===== 模块: {sf.label} ({sf.path}) =====")
        # ★ 剔除源码空行（P0-2）。软著审查是「数页面上看得见几行代码」，
        #   源码里的空行没有可见内容，白白占行位却不被计数 ——
        #   审查报告实测 15.8% 的行是空行。剔掉后「页面行位」与
        #   「有字行」两种口径都是 50，审查员怎么数都对。
        all_lines.extend(ln for ln in file_lines if ln.strip())
    return all_lines, total_curated


def pick_front_back(concat: list[str]) -> tuple[list[str], list[str], bool]:
    """截取前 1500 行与后 1500 行。返回 (前段, 后段, 是否全量)。

    两段都必须**恰好** 50 的整数倍，否则后段起始下标落在页中间，
    分页边界会整体错位（表现为总页数 ≠ 60 或出现空白页）。
    """
    need = LINES_PER_PAGE * PAGES_EACH_SIDE
    if len(concat) <= need * 2:
        return concat, [], True
    front = concat[:need]
    # 后段从尾部往前取 need 行；concat 已剔除空行，末行必为有效代码行。
    back = concat[-need:]
    return front, back, False


# ---------------------------------------------------------------------------
# DOCX 排版
# ---------------------------------------------------------------------------

def set_mono_font(run, size: float) -> None:
    run.font.name = "Consolas"
    run.font.size = Pt(size)
    run.font.color.rgb = DARK
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:ascii"), "Consolas")
    rfonts.set(qn("w:hAnsi"), "Consolas")
    rfonts.set(qn("w:eastAsia"), "Microsoft YaHei")


def add_code_line(doc: Document, text: str, line_spacing_pt: float,
                  page_break_before: bool = False) -> None:
    """写入一行代码。

    ★ 分页必须用显式 ``<w:br w:type="page"/>`` 包在 run 里。
      原来的 ``pf.page_break_before = True`` 在 Word 里有效，但
      **LibreOffice 转换 PDF 时会忽略它** —— 审查员正是按 PDF 数行的，
      于是表现为「每页只有 41 行 + 3 页整页空白」。

    另：含分页符的那一页会额外少排 1~2 个行位，所以分页页要补PAD 个
    空行做补偿（PAD 由本机实测标定，见 PAD_LINES）。
    """
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    pf.line_spacing = Pt(line_spacing_pt)
    # 关掉孤行控制：避免「整段不拆分」把段落整体下推，破坏每页行数
    pf.keep_together = False
    pf.widow_control = False
    pPr = p._element.get_or_add_pPr()
    for tag in ("w:widowControl", "w:keepNext", "w:keepLines"):
        el = OxmlElement(tag)
        el.set(qn("w:val"), "0")
        pPr.append(el)

    if page_break_before:
        # <w:p><w:r><w:br w:type="page"/></w:r>…</w:p>
        br_run = OxmlElement("w:r")
        br = OxmlElement("w:br")
        br.set(qn("w:type"), "page")
        br_run.append(br)
        p._element.append(br_run)

    run = p.add_run(text if text else " ")
    set_mono_font(run, FONT_SIZE)


def setup_section(doc: Document) -> None:
    """A4、窄边距、页眉软件名、页脚页码。

    正文区高度 = 841.9 − 1.5cm(42.5) − 1.4cm(39.7) ≈ 760pt，
    50 行 × 13.6pt = 680pt，余量 80pt，足以吸收渲染行距抖动。
    """
    section = doc.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(1.5)
    section.bottom_margin = Cm(1.4)
    section.left_margin = Cm(2.0)
    section.right_margin = Cm(1.6)
    section.header_distance = Cm(0.75)
    section.footer_distance = Cm(0.65)

    # 页眉：软件全称 + 版本号
    hp = section.header.paragraphs[0]
    hp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = hp.add_run(HEADER_TEXT)
    run.font.size = Pt(8)
    run.font.color.rgb = GRAY
    run.font.name = "Microsoft YaHei"
    rpr = run._element.get_or_add_rPr()
    rfonts = OxmlElement("w:rFonts")
    rfonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    rpr.append(rfonts)

    # 页脚：第 X 页（自动页码域）
    fp = section.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    pre = fp.add_run("第 ")
    pre.font.size = Pt(8)
    pre.font.color.rgb = GRAY
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
    fp._p.append(fld)
    post = fp.add_run(" 页")
    post.font.size = Pt(8)
    post.font.color.rgb = GRAY


def sanitize_emoji(text: str) -> str:
    """把 Consolas 无字形的符号替换为 ASCII 标记。

    Courier New / Consolas 缺 emoji 与部分几何符号字形，渲染时会回退字体
    导致行距失控并打出豆腐块。排版层替换为 ASCII，不改动源码语义。
    """
    for src, dst in EMOJI_MAP.items():
        text = text.replace(src, dst)
    return text


def write_pages(doc: Document, lines: list[str], start_line: int = 0,
                pad_to_page: bool = False) -> int:
    """按每页 50 行写入，返回写入行数。

    start_line 为该段在整体序列中的起始下标，用于判断是否需要段首分页。
    每页第 1 行设 page_break_before，避免插入多余空段落。

    ★ 补行只在**后面还有内容**时做。末页补行会让末页只剩几行 ——
      软著规定末页除外，但审查员看的是「每页都饱满」的观感，
      而且末页行数不足会显得像漏页。
    """
    written = 0
    total = len(lines)
    for i, raw in enumerate(lines):
        text = sanitize_emoji(raw[:MAX_LINE_WIDTH])
        pos = start_line + i
        need_break = (pos % LINES_PER_PAGE == 0) and (pos > 0)
        add_code_line(doc, text, LINE_SPACING_PT, page_break_before=need_break)
        written += 1
        # 分页页补行：含分页符那页会少排 1~2 个行位，补回来才够 50 行。
        # 用单个空格当占位（视觉上是正常空行），不改变源码内容归属。
        if need_break and (i + 1) < total:
            for _ in range(PAD_LINES):
                add_code_line(doc, "", LINE_SPACING_PT, page_break_before=False)

    # 段尾补齐会多出整页（60 页变 61 页并产生空白页），故**不做段尾补齐**：
    # 前后两段本身都恰好是 50 的整数倍，分页符自然落在页边界上。
    return written


def count_repo_lines() -> dict[str, int]:
    """统计仓库自研代码行数（供申请表填报）。"""
    import subprocess

    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    stats: dict[str, int] = {}
    exts = {".py": "Python", ".vue": "Vue", ".js": "JavaScript", ".ts": "TypeScript"}
    for rel in out:
        suffix = Path(rel).suffix.lower()
        if suffix in exts and "node_modules" not in rel:
            p = ROOT / rel
            if p.exists():
                n = len(load_lines(p))
                stats[exts[suffix]] = stats.get(exts[suffix], 0) + n
    stats["合计"] = sum(stats.values())
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="生成软著源代码鉴别材料")
    parser.add_argument(
        "--out",
        default=r"<USER_HOME>\Desktop\Oceanus软著材料",
        help="输出目录",
    )
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    concat, curated_total = build_concatenation()
    non_blank = [ln for ln in concat if ln.strip()]
    front, back, is_full = pick_front_back(non_blank)
    print(f"精选 {len(CURATED_FILES)} 个文件，拼接后 {curated_total:,} 行")
    print(f"剔除空行后 {len(non_blank):,} 行（排版序列）")

    doc = Document()
    setup_section(doc)

    if is_full:
        n = write_pages(doc, front, start_line=0)
        print(f"总量不足 60 页，提交全部源代码：{-(-n // LINES_PER_PAGE)} 页")
    else:
        n_front = write_pages(doc, front, start_line=0, pad_to_page=True)
        # 后 30 页接续排版；start_line 传 0 之外的奇偶性会影响分页判断，
        # 故这里让后段从「非整页位」开始，使其第 1 行自动触发分页。
        n_back = write_pages(doc, back, start_line=len(front))
        print(f"前 {PAGES_EACH_SIDE} 页 + 后 {PAGES_EACH_SIDE} 页，"
              f"共 {-(-(n_front + n_back) // LINES_PER_PAGE)} 页"
              f"（写入 {n_front + n_back} 行）")

    docx_path = out_dir / f"源代码鉴别材料_{SOFTWARE_FULL_NAME}{VERSION}.docx"
    doc.save(docx_path)
    print(f"已生成: {docx_path}")

    # 源程序量统计（申请表填报用）
    stats = count_repo_lines()
    # 提交序列实际包含的模块数（前后各 30 页可能只覆盖部分精选文件）
    submitted_modules = sorted(
        {sf.path for sf in CURATED_FILES
         for marker in (f"({sf.path})",)
         if any(marker in ln for ln in front + back)}
    )
    submit_lines = len(front) + len(back)
    submit_pages = -(-submit_lines // LINES_PER_PAGE)

    lines = [
        f"{SOFTWARE_FULL_NAME}[简称:{SOFTWARE_SHORT_NAME}] {VERSION} 源程序量统计",
        "生成方式: git ls-files 全量自研代码统计（不含开源依赖）",
        "",
    ]
    for lang, n in stats.items():
        lines.append(f"{lang}: {n:,} 行")
    lines += [
        "",
        "软件鉴别材料组成:",
        f"  精选自研文件 {len(CURATED_FILES)} 个，拼接 {curated_total:,} 行"
        f"（剔除空行后 {len(non_blank):,} 行）",
        f"  实际提交覆盖模块 {len(submitted_modules)} 个：",
    ]
    for path in submitted_modules:
        lines.append(f"    - {path}")
    lines += [
        f"  提交源程序量 {submit_lines:,} 行"
        f"（申请表「源程序量」建议填报此值 = 提交文档实有行数，可自证）",
        f"  折算总页数 {submit_pages:,} 页（= {submit_lines:,} ÷ {LINES_PER_PAGE} 向上取整）",
        "",
        f"  提交文档: 前 {PAGES_EACH_SIDE} 页 + 后 {PAGES_EACH_SIDE} 页"
        f"（每页 {LINES_PER_PAGE} 行），共 {PAGES_EACH_SIDE * 2} 页",
        f"  排版口径: 剔除源码空行，保证每页 {LINES_PER_PAGE} 行全部有可见内容"
        f"（行距 {LINE_SPACING_PT}pt，字号 {FONT_SIZE}pt Consolas）",
        "",
        "  仓库全量自研代码（git ls-files 统计，仅供参考，不作为申请表填报值）：",
    ]
    for lang, n in stats.items():
        if lang != "合计":
            lines.append(f"    {lang}: {n:,} 行")
    lines.append(f"    合计: {stats['合计']:,} 行（折算 {stats['合计'] // LINES_PER_PAGE + (1 if stats['合计'] % LINES_PER_PAGE else 0):,} 页）")
    stats_path = out_dir / "源程序量统计.txt"
    stats_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"已生成: {stats_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
