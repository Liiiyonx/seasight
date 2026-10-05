# -*- coding: utf-8 -*-
"""生成 2026 年海峡大学生人工智能创意大赛作品介绍 PPT。

纪律：本 PPT **不得出现**学校名称、专业班级、指导老师、队员姓名、
手机号、邮箱等任何身份信息——初赛为匿名网络评审。

用法：
    python scripts/build_strait_ai_pptx.py \
        --out "<输出目录>/探海灵眸Oceanus_作品介绍.pptx"
"""

from __future__ import annotations

import argparse
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

# 主题色：深海蓝系（浅色背景下文字用深色）
NAVY = RGBColor(0x0F, 0x2E, 0x4C)
OCEAN = RGBColor(0x1D, 0x6A, 0x9C)
TEAL = RGBColor(0x00, 0x9C, 0xA8)
ACCENT = RGBColor(0xF2, 0x8A, 0x3C)
LIGHT = RGBColor(0xEE, 0xF4, 0xF8)
LIGHT2 = RGBColor(0xE2, 0xEC, 0xF3)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
DARK = RGBColor(0x1B, 0x2A, 0x38)
GRAY = RGBColor(0x5B, 0x6B, 0x7A)
RED = RGBColor(0xC0, 0x39, 0x2B)
GREEN = RGBColor(0x1E, 0x7A, 0x4C)

FONT = "Microsoft YaHei"
W = Inches(13.333)
H = Inches(7.5)


# --------------------------------------------------------------------------
# 基础绘制工具
# --------------------------------------------------------------------------

def solid(shape, color: RGBColor) -> None:
    shape.fill.solid()
    shape.fill.fore_color.rgb = color


def noline(shape) -> None:
    shape.line.fill.background()


def rect(slide, x, y, w, h, color, *, radius=False):
    kind = MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE
    sh = slide.shapes.add_shape(kind, int(x), int(y), int(w), int(h))
    solid(sh, color)
    noline(sh)
    if radius:
        try:
            sh.adjustments[0] = 0.06
        except (IndexError, ValueError):
            pass
    return sh


def textbox(slide, x, y, w, h, text, *, size=18, color=DARK, bold=False,
            align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, spacing=1.0, wrap=True):
    tb = slide.shapes.add_textbox(int(x), int(y), int(w), int(h))
    tf = tb.text_frame
    tf.word_wrap = wrap
    tf.vertical_anchor = anchor
    lines = text.split("\n")
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.line_spacing = spacing
        run = p.add_run()
        run.text = line
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = color
        run.font.name = FONT
    return tb


def bullets(slide, x, y, w, h, items, *, size=16, color=DARK,
            bullet_color=TEAL, gap=8, spacing=1.25):
    """带方块项目符号的列表。items 为 str 或 (标题, 说明) 元组。"""
    tb = slide.shapes.add_textbox(int(x), int(y), int(w), int(h))
    tf = tb.text_frame
    tf.word_wrap = True
    for i, item in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.line_spacing = spacing
        p.space_after = Pt(gap)
        dot = p.add_run()
        dot.text = "▪ "
        dot.font.size = Pt(size)
        dot.font.color.rgb = bullet_color
        dot.font.bold = True
        dot.font.name = FONT
        if isinstance(item, tuple):
            head, body = item
            r1 = p.add_run()
            r1.text = head
            r1.font.size = Pt(size)
            r1.font.bold = True
            r1.font.color.rgb = color
            r1.font.name = FONT
            r2 = p.add_run()
            r2.text = body
            r2.font.size = Pt(size)
            r2.font.color.rgb = GRAY
            r2.font.name = FONT
        else:
            r = p.add_run()
            r.text = item
            r.font.size = Pt(size)
            r.font.color.rgb = color
            r.font.name = FONT
    return tb


def header(slide, kicker: str, title: str, page: int, total: int):
    """统一页眉：色带 + 章节标签 + 标题 + 页码。"""
    rect(slide, 0, 0, W, Inches(0.09), TEAL)
    rect(slide, 0, Inches(0.09), Inches(0.16), Inches(1.02), NAVY)
    textbox(slide, Inches(0.42), Inches(0.16), Inches(8), Inches(0.3),
            kicker, size=12, color=TEAL, bold=True)
    textbox(slide, Inches(0.42), Inches(0.44), Inches(10.5), Inches(0.62),
            title, size=27, color=NAVY, bold=True)
    textbox(slide, W - Inches(1.5), Inches(0.32), Inches(1.1), Inches(0.35),
            f"{page:02d} / {total:02d}", size=12, color=GRAY, align=PP_ALIGN.RIGHT)


def blank(prs: Presentation, *, bg=WHITE):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    rect(slide, 0, 0, W, H, bg)
    return slide


def footer(slide, text: str):
    textbox(slide, Inches(0.42), H - Inches(0.52), Inches(12.5), Inches(0.34),
            text, size=10.5, color=GRAY)


def card(slide, x, y, w, h, title, body, *, accent=OCEAN, title_size=15,
         body_size=12.5):
    """浅底卡片：标题条 + 正文。"""
    rect(slide, x, y, w, h, LIGHT, radius=True)
    rect(slide, x, y, Inches(0.055), h, accent)
    textbox(slide, x + Inches(0.24), y + Inches(0.14), w - Inches(0.4),
            Inches(0.34), title, size=title_size, color=NAVY, bold=True)
    textbox(slide, x + Inches(0.24), y + Inches(0.52), w - Inches(0.42),
            h - Inches(0.62), body, size=body_size, color=GRAY, spacing=1.2)


def chip(slide, x, y, w, h, label, value, *, color=OCEAN):
    rect(slide, x, y, w, h, LIGHT, radius=True)
    textbox(slide, x, y + Inches(0.1), w, Inches(0.3), label,
            size=11, color=GRAY, align=PP_ALIGN.CENTER)
    textbox(slide, x, y + Inches(0.36), w, Inches(0.42), value,
            size=21, color=color, bold=True, align=PP_ALIGN.CENTER)


def arrow(slide, x, y, w, h, *, color=TEAL):
    sh = slide.shapes.add_shape(MSO_SHAPE.RIGHT_ARROW, int(x), int(y),
                                int(w), int(h))
    solid(sh, color)
    noline(sh)
    return sh


# --------------------------------------------------------------------------
# 幻灯片内容
# --------------------------------------------------------------------------

TOTAL = 22


def slide_cover(prs):
    s = blank(prs, bg=NAVY)
    rect(s, 0, 0, Inches(0.22), H, TEAL)
    # 装饰：右下角同心矩形，暗示海面层次
    for i, (cx, cy, cw) in enumerate([
            (8.9, 4.5, 4.6), (9.7, 5.05, 3.4), (10.5, 5.6, 2.2)]):
        sh = rect(s, Inches(cx), Inches(cy), Inches(cw), Inches(0.42),
                  RGBColor(0x16, 0x3C, 0x5E) if i % 2 == 0 else RGBColor(0x1B, 0x4A, 0x70))
        sh.fill.transparency = 0

    textbox(s, Inches(0.95), Inches(1.5), Inches(9), Inches(0.4),
            "2026 年海峡大学生人工智能创意大赛", size=15, color=TEAL, bold=True)
    textbox(s, Inches(0.95), Inches(2.05), Inches(10.5), Inches(1.0),
            "探海灵眸 Oceanus", size=50, color=WHITE, bold=True)
    textbox(s, Inches(0.95), Inches(3.12), Inches(10.5), Inches(0.9),
            "AI 驱动的县域海漂垃圾\n智能监测与调度系统", size=25,
            color=RGBColor(0xC9, 0xE4, 0xF0), spacing=1.35)
    rect(s, Inches(0.95), Inches(4.42), Inches(1.5), Inches(0.035), ACCENT)
    textbox(s, Inches(0.95), Inches(4.72), Inches(10.5), Inches(0.9),
            "感知 — 决策 — 执行 — 核算  完整闭环\n"
            "确定性治理智能体内核 · 可审计决策证据链 · 可插拔执行端",
            size=14.5, color=RGBColor(0x9F, 0xC4, 0xD8), spacing=1.4)
    return s


def slide_agenda(prs):
    s = blank(prs)
    header(s, "CONTENTS", "汇报提纲", 2, TOTAL)
    items = [
        ("01", "问题提出", "为什么海面「看见垃圾」不等于「问题被解决」"),
        ("02", "解决方案", "感知—决策—执行—核算的四层架构与三个核心创新"),
        ("03", "技术验证", "自动化测试、智能体评测与软件集成验收"),
        ("04", "应用价值", "对治理主体的价值、可推广性与能力边界"),
    ]
    y = Inches(1.75)
    for i, (num, title, sub) in enumerate(items):
        rect(s, Inches(0.85), y, Inches(11.6), Inches(1.06), LIGHT, radius=True)
        rect(s, Inches(0.85), y, Inches(1.0), Inches(1.06), NAVY)
        textbox(s, Inches(0.85), y + Inches(0.29), Inches(1.0), Inches(0.5),
                num, size=25, color=WHITE, bold=True, align=PP_ALIGN.CENTER)
        textbox(s, Inches(2.15), y + Inches(0.2), Inches(9.9), Inches(0.4),
                title, size=19, color=NAVY, bold=True)
        textbox(s, Inches(2.15), y + Inches(0.6), Inches(9.9), Inches(0.36),
                sub, size=13, color=GRAY)
        y += Inches(1.22)
    return s


def slide_problem(prs):
    s = blank(prs)
    header(s, "01问题提出", "真正的断点不在「看见」，而在「流转」", 3, TOTAL)
    textbox(s, Inches(0.62), Inches(1.62), Inches(12.1), Inches(0.44),
            "海漂垃圾治理包含五个环节，每个环节都有独立的失效方式：",
            size=15, color=DARK)

    cols = [
        ("发现", "摄像头巡查 / 无人机飞检 / 群众上报", "覆盖时段有限；单帧误报多，人工复核成本高", RED),
        ("确认", "值班人员肉眼判断是否真实存在", "判断标准因人而异；无复核记录", RED),
        ("派单", "电话通知作业队", "依赖个人经验；无优先级依据", ACCENT),
        ("执行", "作业船按经验航行", "空驶多；近处设备被远派", ACCENT),
        ("核算", "人工汇总台账", "数据滞后、口径不一；难以证明闭环", RED),
    ]
    cw = Inches(2.32)
    x = Inches(0.62)
    for name, cur, bad, col in cols:
        rect(s, x, Inches(2.2), cw, Inches(2.05), LIGHT, radius=True)
        rect(s, x, Inches(2.2), cw, Inches(0.42), col)
        textbox(s, x, Inches(2.28), cw, Inches(0.3), name,
                size=16, color=WHITE, bold=True, align=PP_ALIGN.CENTER)
        textbox(s, x + Inches(0.18), Inches(2.72), cw - Inches(0.36), Inches(0.5),
                cur, size=11.5, color=NAVY, bold=True, spacing=1.15)
        textbox(s, x + Inches(0.18), Inches(3.3), cw - Inches(0.36), Inches(0.85),
                bad, size=11, color=GRAY, spacing=1.18)
        x += cw + Inches(0.12)

    rect(s, Inches(0.62), Inches(4.52), Inches(12.1), Inches(1.9),
         RGBColor(0xFD, 0xF3, 0xE7), radius=True)
    rect(s, Inches(0.62), Inches(4.52), Inches(0.06), Inches(1.9), ACCENT)
    textbox(s, Inches(0.92), Inches(4.72), Inches(11.5), Inches(0.4),
            "关键洞察", size=17, color=NAVY, bold=True)
    bullets(s, Inches(0.92), Inches(5.2), Inches(11.5), Inches(1.1), [
        ("真正稀缺的不是「发现能力」，而是 ", "从发现到核算的可信流转。"),
        ("即使检测出 100 个漂浮物，", "若无法证明「哪一条被处理了、效果如何、是否值得继续投入」，"
                                    "系统对治理的价值仍接近于零。"),
    ], size=13.5, bullet_color=ACCENT)
    return s


def slide_jtbd(prs):
    s = blank(prs)
    header(s, "01 问题提出", "目标用户的核心任务与四问", 4, TOTAL)
    rect(s, Inches(0.62), Inches(1.6), Inches(12.1), Inches(1.15),
         NAVY, radius=True)
    textbox(s, Inches(0.95), Inches(1.78), Inches(11.5), Inches(0.85),
            "当辖区海面出现影响航道、养殖、岸线或旅游环境的漂浮垃圾时，\n"
            "我要在有限人力和预算下，快速确认问题、派出合适设备、跟踪执行过程，\n"
            "并向上级证明问题已经闭环 —— 而不是只看到一堆视频和告警。",
            size=14.5, color=WHITE, spacing=1.35)

    qs = [
        ("Q1", "问题是否真实存在？", "证据是什么"),
        ("Q2", "应该派谁、何时去？", "代价是多少"),
        ("Q3", "执行是否顺利完成？", "失败后如何处理"),
        ("Q4", "最终清理了什么？", "是否值得继续采购"),
    ]
    cw = Inches(2.92)
    x = Inches(0.62)
    for num, q, sub in qs:
        rect(s, x, Inches(3.0), cw, Inches(1.35), LIGHT, radius=True)
        textbox(s, x + Inches(0.22), Inches(3.14), Inches(1.0), Inches(0.4),
                num, size=20, color=TEAL, bold=True)
        textbox(s, x + Inches(0.22), Inches(3.58), cw - Inches(0.44), Inches(0.42),
                q, size=14.5, color=NAVY, bold=True)
        textbox(s, x + Inches(0.22), Inches(4.0), cw - Inches(0.44), Inches(0.3),
                sub, size=12, color=GRAY)
        x += cw + Inches(0.14)

    textbox(s, Inches(0.62), Inches(4.62), Inches(12.1), Inches(0.4),
            "需求优先级：按「对闭环可信性的影响」排序，而非实现难度",
            size=15, color=NAVY, bold=True)
    rows = [
        ("P0", "单帧误报抑制", "值班人员被大量假目标消耗，最终关闭告警，系统整体失效", TEAL),
        ("P0", "决策过程可回溯", "无法向上级证明结论，汇报只能靠人工台账", TEAL),
        ("P0", "失败可恢复", "设备离线即任务悬挂，作业队不知为何停工", TEAL),
        ("P1", "派单依据量化", "空驶成本高，运营费用不可持续", OCEAN),
        ("P1", "高风险动作审批", "自动执行一旦出错，无人能拦截，责任不可追溯", OCEAN),
    ]
    y = Inches(5.1)
    for tag, name, why, col in rows:
        rect(s, Inches(0.62), y, Inches(0.72), Inches(0.34), col, radius=True)
        textbox(s, Inches(0.62), y + Inches(0.05), Inches(0.72), Inches(0.26),
                tag, size=12, color=WHITE, bold=True, align=PP_ALIGN.CENTER)
        textbox(s, Inches(1.5), y + Inches(0.03), Inches(2.4), Inches(0.3),
                name, size=13.5, color=NAVY, bold=True)
        textbox(s, Inches(4.0), y + Inches(0.04), Inches(8.7), Inches(0.3),
                why, size=12, color=GRAY)
        y += Inches(0.4)
    footer(s, "设计决策：把「可信流转」放在「识别精度」之前 —— 精度不足只会漏报，流转不可信会让整个系统被使用者抛弃。")
    return s


def slide_architecture(prs):
    s = blank(prs)
    header(s, "02 解决方案", "系统总体架构：四层闭环", 5, TOTAL)
    layers = [
        ("感知层", "岸基摄像头 / 无人机 / 人工上报  →  独立 HTTP 推理服务（OpenCV 检测 · ONNX 多 provider）", OCEAN),
        ("事件层", "Schema 校验 → 双重幂等去重 → 时序验证（跟踪 · 连续命中 · 冷却）→ 可信事件", TEAL),
        ("决策层", "治理智能体内核：Run → Planner → Policy Guard → Approval → Tool Executor → Observer → Verifier", NAVY),
        ("执行层", "任务状态机 → MQTT 下发 → ACK → 进度遥测 → 异常换车重派  |  执行末端可插拔", OCEAN),
        ("核算层", "证据归档 → 日报月报聚合 → 治理热力图 → 单位成本核算 → 审计日志", TEAL),
    ]
    y = Inches(1.68)
    for name, body, col in layers:
        rect(s, Inches(0.62), y, Inches(2.0), Inches(0.82), col, radius=True)
        textbox(s, Inches(0.62), y + Inches(0.24), Inches(2.0), Inches(0.4),
                name, size=16, color=WHITE, bold=True, align=PP_ALIGN.CENTER)
        rect(s, Inches(2.72), y, Inches(10.0), Inches(0.82), LIGHT, radius=True)
        textbox(s, Inches(2.98), y + Inches(0.26), Inches(9.6), Inches(0.4),
                body, size=12.5, color=NAVY)
        y += Inches(0.94)
        if name != "核算层":
            arrow(s, Inches(1.45), y - Inches(0.12), Inches(0.3), Inches(0.11))

    textbox(s, Inches(0.62), Inches(6.42), Inches(12.1), Inches(0.34),
            "依赖单向：下层不反向依赖上层；执行端设备不直接写数据库；"
            "感知层不直接触发派单（必须经过时序验证）。",
            size=12, color=GRAY)
    return s


def slide_stack(prs):
    s = blank(prs)
    header(s, "02 解决方案", "技术栈与选型理由", 6, TOTAL)
    rows = [
        ("后端", "Python + FastAPI", "异步 IO 适合同时处理设备上报与指令下发；Pydantic 提供 Schema 强校验"),
        ("数据库", "PostgreSQL 16 + PostGIS", "事件与点位是天然空间数据；PostGIS 支持就近派单的空间查询"),
        ("消息", "EMQX (MQTT) + Redis Streams", "设备下行用 MQTT（弱网适配好）；内部事件流支持消费组、PEL 与重投"),
        ("存储", "MinIO", "证据帧与影像对象存储，兼容 S3 语义"),
        ("前端", "Vue 3 + 地图可视化", "业务控制台与大屏统一"),
        ("推理", "ONNX Runtime（多 provider）", "不把加速卡写死：按 TensorRT → CUDA → CPU 顺序探测可用 provider"),
    ]
    y = Inches(1.78)
    for name, tech, why in rows:
        rect(s, Inches(0.62), y, Inches(1.35), Inches(0.62), NAVY, radius=True)
        textbox(s, Inches(0.62), y + Inches(0.18), Inches(1.35), Inches(0.3),
                name, size=13.5, color=WHITE, bold=True, align=PP_ALIGN.CENTER)
        rect(s, Inches(2.06), y, Inches(3.5), Inches(0.62), LIGHT, radius=True)
        textbox(s, Inches(2.24), y + Inches(0.19), Inches(3.2), Inches(0.3),
                tech, size=13, color=NAVY, bold=True)
        textbox(s, Inches(5.72), y + Inches(0.1), Inches(7.0), Inches(0.5),
                why, size=12, color=GRAY, spacing=1.15)
        y += Inches(0.74)

    rect(s, Inches(0.62), Inches(6.22), Inches(12.1), Inches(0.78),
         RGBColor(0xFD, 0xF3, 0xE7), radius=True)
    rect(s, Inches(0.62), Inches(6.22), Inches(0.06), Inches(0.78), ACCENT)
    textbox(s, Inches(0.92), Inches(6.36), Inches(11.6), Inches(0.52),
            "关于智能层的设计选择：治理规则实现为确定性内核，而非交给大模型决策。"
            "派单涉及设备与公共资源，行为必须可预测、可解释、可追责，且断网时仍须工作。",
            size=12.5, color=DARK, spacing=1.15)
    return s


def slide_innovation1(prs):
    s = blank(prs)
    header(s, "02 解决方案", "创新一：时序先验的误报抑制链路", 7, TOTAL)
    textbox(s, Inches(0.62), Inches(1.62), Inches(7.4), Inches(0.8),
            "海面检测最大工程难题是误报：浪花、镜面反光、飞鸟、水草都会触发检测，\n"
            "而其中绝大多数在下一帧就消失。",
            size=14, color=DARK, spacing=1.3)

    mechs = [
        ("抽帧", "按固定间隔取帧，降低无效计算"),
        ("目标跟踪", "基于位置与尺寸的历史窗口做关联，避免同一目标被当作多次独立检出"),
        ("连续命中计数", "目标需连续在 N 帧中被检出才计数为有效"),
        ("冷却窗口", "同一目标在冷却期内不重复生成事件"),
    ]
    y = Inches(2.6)
    for name, desc in mechs:
        rect(s, Inches(0.62), y, Inches(7.4), Inches(0.82), LIGHT, radius=True)
        rect(s, Inches(0.62), y, Inches(0.055), Inches(0.82), TEAL)
        textbox(s, Inches(0.88), y + Inches(0.13), Inches(2.1), Inches(0.3),
                name, size=14, color=NAVY, bold=True)
        textbox(s, Inches(0.88), y + Inches(0.44), Inches(6.9), Inches(0.34),
                desc, size=11.5, color=GRAY, spacing=1.15)
        y += Inches(0.94)

    rect(s, Inches(8.3), Inches(1.62), Inches(4.42), Inches(3.5),
         RGBColor(0x0F, 0x2E, 0x4C), radius=True)
    textbox(s, Inches(8.6), Inches(1.86), Inches(3.9), Inches(0.34),
            "合成海面测试结果", size=13, color=TEAL, bold=True)
    textbox(s, Inches(8.6), Inches(2.28), Inches(3.9), Inches(0.9),
            "≈ 76%", size=54, color=WHITE, bold=True)
    textbox(s, Inches(8.6), Inches(3.24), Inches(3.9), Inches(0.34),
            "时序链路误报抑制率", size=15, color=WHITE, bold=True)
    textbox(s, Inches(8.6), Inches(3.68), Inches(3.9), Inches(1.3),
            "口径：被时序环节过滤的检测数\n÷ 输入检测总数",
            size=12, color=RGBColor(0xA8, 0xC8, 0xDC), spacing=1.3)

    rect(s, Inches(0.62), Inches(5.32), Inches(12.1), Inches(1.68),
         RGBColor(0xFC, 0xEC, 0xEA), radius=True)
    rect(s, Inches(0.62), Inches(5.32), Inches(0.06), Inches(1.68), RED)
    textbox(s, Inches(0.92), Inches(5.5), Inches(11.6), Inches(0.34),
            "必须明确的边界", size=15, color=RED, bold=True)
    textbox(s, Inches(0.92), Inches(5.9), Inches(11.6), Inches(1.0),
            "76% 不是识别精度、不是召回率、不是准确率。分母是输入检测数，它衡量的是时序链路的过滤能力。\n"
            "系统真实检测精度受限于独立测试集尚未放行，当前评测结论为「未评测」，本文不做任何精度宣称。\n"
            "一个能说清数字含义与分母的指标，比一个含糊的「准确率 92%」更值得信任。",
            size=12.5, color=DARK, spacing=1.3)
    return s


def slide_innovation2(prs):
    s = blank(prs)
    header(s, "02 解决方案", "创新二：确定性治理智能体内核", 8, TOTAL)
    textbox(s, Inches(0.62), Inches(1.58), Inches(12.1), Inches(0.36),
            "识别、调度、报表都是能力模块，不是独立智能体。够格的治理智能体必须具备七类原语：",
            size=14, color=DARK)

    prims = [
        ("Run", "一次完整治理过程的容器", "必须有触发源、最大步数、超时与终止原因"),
        ("Step", "一次决策或工具调用", "重规划产生新 step，历史步骤永不被覆盖"),
        ("Tool", "受控的能力调用单元", "含版本、Schema、权限、超时、幂等键、错误码"),
        ("Policy", "动作前的策略守卫", "违规动作必须在工具执行前被阻断"),
        ("Approval", "高风险动作的人工闸门", "留审批人、理由、时间，不可绕过"),
        ("Replay", "运行轨迹完整回放", "可从任意 step 重放，用于排障与审计"),
        ("Eval", "固定场景回归评测", "每次变更可一键对比，防止改好一处坏另一处"),
    ]
    # 前 4 个与后 3 个分两行排布：4 列 × 2 行
    cw = Inches(2.92)
    gap = Inches(0.13)
    for i, (name, sub, constraint) in enumerate(prims):
        row, colidx = divmod(i, 4)
        x = Inches(0.62) + colidx * (cw + gap)
        y = Inches(2.08) + row * Inches(1.3)
        rect(s, x, y, cw, Inches(1.18), LIGHT, radius=True)
        rect(s, x, y, cw, Inches(0.34), NAVY)
        textbox(s, x, y + Inches(0.05), cw, Inches(0.28), name,
                size=13.5, color=WHITE, bold=True, align=PP_ALIGN.CENTER)
        textbox(s, x + Inches(0.2), y + Inches(0.4), cw - Inches(0.4), Inches(0.28),
                sub, size=11.5, color=NAVY, bold=True)
        textbox(s, x + Inches(0.2), y + Inches(0.72), cw - Inches(0.4), Inches(0.44),
                constraint, size=10.5, color=GRAY, spacing=1.12)

    textbox(s, Inches(0.62), Inches(4.86), Inches(12.1), Inches(0.34),
            "运行状态机", size=15, color=NAVY, bold=True)
    states = ["created", "planning", "waiting_policy", "waiting_approval",
              "executing", "observing", "verifying", "succeeded"]
    x = Inches(0.62)
    bw = Inches(1.36)
    for i, st in enumerate(states):
        col = GREEN if st == "succeeded" else (NAVY if i < 3 else OCEAN)
        rect(s, x, Inches(5.26), bw, Inches(0.42), col, radius=True)
        # 长状态名缩小字号并禁用自动换行，避免溢出框外
        fs = 9 if len(st) > 12 else 10.5
        textbox(s, x, Inches(5.34), bw, Inches(0.28), st,
                size=fs, color=WHITE, bold=True, align=PP_ALIGN.CENTER,
                wrap=False)
        if i < len(states) - 1:
            arrow(s, x + bw + Inches(0.015), Inches(5.39), Inches(0.19), Inches(0.15),
                  color=TEAL)
        x += bw + Inches(0.22)

    rect(s, Inches(0.62), Inches(5.86), Inches(12.1), Inches(0.42),
         RGBColor(0xFC, 0xEC, 0xEA), radius=True)
    textbox(s, Inches(0.85), Inches(5.92), Inches(11.7), Inches(0.3),
            "失败路径：failed / cancelled / expired  →  进入 Replanner 产生新 Step  |  "
            "达到最大步数则安全终止  |  失败原因结构化保存",
            size=11.5, color=RED)
    footer(s, "结构化失败原因：no_robot_available / tool_timeout / policy_denied / approval_rejected / task_conflict / max_steps_exceeded")
    return s


def slide_llm_role(prs):
    s = blank(prs)
    header(s, "02 解决方案", "大模型的位置：增强项，不是依赖项", 9, TOTAL)
    textbox(s, Inches(0.62), Inches(1.62), Inches(12.1), Inches(0.36),
            "治理场景涉及设备与公共资源，行为必须可预测、可解释、可追责，且断网时仍须工作。",
            size=14, color=DARK)

    card(s, Inches(0.62), Inches(2.15), Inches(5.9), Inches(2.5),
         "确定性内核承担（代码）",
         "▪ 状态管理\n▪ 规则判断\n▪ 工具调用\n"
         "▪ 超时与重试\n▪ 终止条件判定",
         accent=TEAL, title_size=16, body_size=13.5)
    card(s, Inches(6.82), Inches(2.15), Inches(5.9), Inches(2.5),
         "可选大模型承担（增强）",
         "▪ 意图理解\n▪ 异常解释\n"
         "▪ 计划候选生成\n▪ 自然语言总结",
         accent=ACCENT, title_size=16, body_size=13.5)

    rect(s, Inches(0.62), Inches(4.85), Inches(12.1), Inches(0.78),
         NAVY, radius=True)
    textbox(s, Inches(0.62), Inches(4.98), Inches(12.1), Inches(0.5),
            "大模型输出必须经三重过滤：  JSON Schema 校验  →  策略守卫  →  工具白名单",
            size=15, color=WHITE, bold=True, align=PP_ALIGN.CENTER)

    rect(s, Inches(0.62), Inches(5.78), Inches(12.1), Inches(1.2),
         RGBColor(0xE9, 0xF6, 0xEF), radius=True)
    rect(s, Inches(0.62), Inches(5.78), Inches(0.06), Inches(1.2), GREEN)
    textbox(s, Inches(0.92), Inches(5.95), Inches(11.6), Inches(0.9),
            "模型不可用时自动回落到规则模式，系统核心治理闭环不中断。\n"
            "这是本作品与「套壳大模型」的根本区别 —— 把大模型放在它真正擅长、且失败不致命的位置。",
            size=13.5, color=DARK, bold=False, spacing=1.3)
    return s


def slide_tools(prs):
    s = blank(prs)
    header(s, "02 解决方案", "工具契约：每个动作都可被审计", 10, TOTAL)
    textbox(s, Inches(0.62), Inches(1.58), Inches(12.1), Inches(0.34),
            "所有工具必须声明：名称 / 版本 / 输入输出 Schema / 权限 / 风险级别 / 超时 / "
            "幂等键 / 审计事件 / 错误码 —— 缺一不得注册",
            size=13, color=GRAY)

    rows = [
        ("event.get", "读取事件与证据", "只读", "low", "—", "event_not_found", TEAL),
        ("device.query_available", "查询可用设备", "只读", "low", "—", "device_unreachable", TEAL),
        ("dispatch.plan", "生成候选派单（含淘汰原因）", "只读", "low", "—", "no_candidate", TEAL),
        ("task.create_or_merge", "建单或合并任务", "写", "medium", "event+window", "task_conflict", OCEAN),
        ("task.transition", "状态迁移", "写", "medium", "task+status", "illegal_transition", OCEAN),
        ("mqtt.send_task", "下发设备指令", "写", "high", "task+attempt", "ack_timeout", RED),
        ("human.request_approval", "请求人工审批", "写", "medium", "run+action", "approval_rejected", OCEAN),
        ("report.generate", "生成治理日报", "写", "low", "window+type", "data_incomplete", OCEAN),
    ]
    cols = [(Inches(0.62), Inches(2.85)), (Inches(3.57), Inches(3.55)),
            (Inches(7.22), Inches(0.82)), (Inches(8.14), Inches(0.9)),
            (Inches(9.14), Inches(1.5)), (Inches(10.74), Inches(1.98))]
    heads = ["工具", "用途", "权限", "风险", "幂等键", "关键错误码"]
    for (cx, cw), hd in zip(cols, heads):
        rect(s, cx, Inches(2.06), cw, Inches(0.4), NAVY)
        textbox(s, cx + Inches(0.1), Inches(2.13), cw - Inches(0.2), Inches(0.28),
                hd, size=11, color=WHITE, bold=True)

    y = Inches(2.5)
    for name, use, perm, risk, idem, err, col in rows:
        rect(s, Inches(0.62), y, Inches(12.1), Inches(0.44),
             LIGHT if int(y / Inches(0.46)) % 2 == 0 else WHITE)
        vals = [name, use, perm, risk, idem, err]
        for (cx, cw), v in zip(cols, vals):
            textbox(s, cx + Inches(0.1), y + Inches(0.11), cw - Inches(0.18),
                    Inches(0.26), v, size=10,
                    color=col if v == "high" else (NAVY if name == v else DARK),
                    bold=(name == v or v == "high"), wrap=False)
        y += Inches(0.46)

    rect(s, Inches(0.62), Inches(6.3), Inches(12.1), Inches(0.72),
         RGBColor(0xFD, 0xF3, 0xE7), radius=True)
    rect(s, Inches(0.62), Inches(6.3), Inches(0.06), Inches(0.72), ACCENT)
    textbox(s, Inches(0.92), Inches(6.42), Inches(11.6), Inches(0.5),
            "注意 dispatch.plan 必须输出淘汰原因 —— 不仅说明选了谁，也说明为什么没选另一个。"
            "这让调度决策可被复核，而非黑箱。",
            size=12.5, color=DARK)
    return s


def slide_evidence_chain(prs):
    s = blank(prs)
    header(s, "02 解决方案", "创新三：可审计的证据链与数据完整性", 11, TOTAL)
    textbox(s, Inches(0.62), Inches(1.58), Inches(12.1), Inches(0.34),
            "治理系统要能被追问，就必须为每个结论保留证据。对「数据不可信」做显式工程防御：",
            size=14, color=DARK)

    rows = [
        ("幂等去重", "设备重复上报、网络重传", "(device_id, seq) 与 event_id 双重判重"),
        ("状态机守卫", "非法状态跳转（未派单直接完成）", "显式允许的迁移表，非法迁移被拒绝"),
        ("ACK 超时恢复", "设备收到指令未确认", "超时回退任务并换车，不重复建单"),
        ("PEL 回收", "消费者崩溃导致消息滞留", "Redis Streams 消费组 + Pending 回收"),
        ("字段可空性", "无数据源时输出 0", "coverage_area 改为 nullable，无来源返回空"),
        ("操作留痕", "谁在何时改了阈值", "审计日志覆盖所有写操作与审批决定"),
    ]
    y = Inches(2.1)
    for name, scene, handle in rows:
        rect(s, Inches(0.62), y, Inches(2.3), Inches(0.5), NAVY, radius=True)
        textbox(s, Inches(0.62), y + Inches(0.11), Inches(2.3), Inches(0.3),
                name, size=12.5, color=WHITE, bold=True, align=PP_ALIGN.CENTER)
        rect(s, Inches(3.02), y, Inches(4.4), Inches(0.5), LIGHT, radius=True)
        textbox(s, Inches(3.2), y + Inches(0.12), Inches(4.1), Inches(0.3),
                scene, size=11.5, color=GRAY)
        rect(s, Inches(7.52), y, Inches(5.2), Inches(0.5), WHITE)
        textbox(s, Inches(7.7), y + Inches(0.12), Inches(4.9), Inches(0.3),
                handle, size=11.5, color=NAVY, bold=True)
        y += Inches(0.6)

    rect(s, Inches(0.62), Inches(5.92), Inches(12.1), Inches(1.08),
         RGBColor(0xE9, 0xF6, 0xEF), radius=True)
    rect(s, Inches(0.62), Inches(5.92), Inches(0.06), Inches(1.08), GREEN)
    textbox(s, Inches(0.92), Inches(6.06), Inches(11.6), Inches(0.34),
            "一条容易被忽视但极重要的原则：宁可留空，不可编造",
            size=14, color=GREEN, bold=True)
    textbox(s, Inches(0.92), Inches(6.42), Inches(11.6), Inches(0.5),
            "在治理报表里，一个凭空的 0 会被读成「该区域无覆盖」或「该月无清理」，直接污染决策。"
            "所以 coverage_area 无来源时返回空，不写 0。",
            size=12, color=DARK)
    return s


def slide_hardware(prs):
    s = blank(prs)
    header(s, "02 解决方案", "软硬结合：可插拔执行端", 12, TOTAL)
    rect(s, Inches(0.62), Inches(1.66), Inches(12.1), Inches(1.5), LIGHT, radius=True)
    flow = ["岸基检测", "平台研判", "自动派单", "MQTT 下发", "执行端拾取", "结果回传"]
    x = Inches(0.92)
    for i, f in enumerate(flow):
        rect(s, x, Inches(2.06), Inches(1.62), Inches(0.62),
             NAVY if i < 4 else OCEAN, radius=True)
        textbox(s, x, Inches(2.23), Inches(1.62), Inches(0.3), f,
                size=12.5, color=WHITE, bold=True, align=PP_ALIGN.CENTER)
        if i < len(flow) - 1:
            arrow(s, x + Inches(1.66), Inches(2.28), Inches(0.22), Inches(0.18),
                  color=TEAL)
        x += Inches(1.88)

    textbox(s, Inches(0.62), Inches(3.42), Inches(12.1), Inches(0.34),
            "为什么把执行末端设计成可插拔", size=16, color=NAVY, bold=True)
    bullets(s, Inches(0.62), Inches(3.84), Inches(12.1), Inches(1.5), [
        ("真实海域的约束来自执行器的物理特性：", "无人机受风力、续航、禁飞区限制；"
                                    "水面机器人受浅滩与渔网缠绕限制。"),
        ("受限的是某一种执行器，不是系统架构。", "任务与通信契约标准化后，"
                                     "换个执行器契约不用改。"),
        ("平台负责目标点、状态与证据回传，", "不冒充已具备自主导航与避障能力。"),
    ], size=13.5, bullet_color=OCEAN)

    rect(s, Inches(0.62), Inches(5.5), Inches(12.1), Inches(1.5),
         RGBColor(0xFC, 0xEC, 0xEA), radius=True)
    rect(s, Inches(0.62), Inches(5.5), Inches(0.06), Inches(1.5), RED)
    textbox(s, Inches(0.92), Inches(5.66), Inches(11.6), Inches(0.34),
            "证据等级说明（必须如实标注）", size=14, color=RED, bold=True)
    textbox(s, Inches(0.92), Inches(6.04), Inches(11.6), Inches(0.86),
            "当前执行端为模拟设备 / 受控实验环境，尚未开展真实海域验证。\n"
            "可表述：已完成 MQTT 派单链路与执行端动作的受控仿真或实验室联调（E1/E2）。\n"
            "不可表述：已在真实海面完成验证 —— 除非取得现场记录（E3）。",
            size=12, color=DARK, spacing=1.3)
    return s


def slide_compute(prs):
    s = blank(prs)
    header(s, "02 解决方案", "国产算力与工具链：已实测 / 方案级严格区分", 13, TOTAL)

    rect(s, Inches(0.62), Inches(1.66), Inches(6.0), Inches(4.62),
         RGBColor(0xE9, 0xF6, 0xEF), radius=True)
    rect(s, Inches(0.62), Inches(1.66), Inches(6.0), Inches(0.46), GREEN)
    textbox(s, Inches(0.62), Inches(1.76), Inches(6.0), Inches(0.3),
            "已完成实测（E2 级 · 软件集成）", size=14, color=WHITE, bold=True,
            align=PP_ALIGN.CENTER)
    bullets(s, Inches(0.92), Inches(2.28), Inches(5.45), Inches(3.9), [
        ("ONNX 模型导出链路", "：固定 batch=1、opset 17、图简化，"
                        "参数与昇腾 ATC 工具链的静态形状要求对齐"),
        ("推理服务化与多 provider 探测", "：独立 HTTP 服务，按 "
                              "TensorRT → CUDA → CPU 顺序装配，"
                              "实际 provider 通过健康检查暴露；"
                              "加载失败降级 stub 并继续启动"),
        ("智能体编排与知识检索", "：在华为云 ModelArts / AgentArts 平台完成部署"
                        "与多轮工具调用验收，无运行错误"),
        ("知识图谱增强决策问答", "：多跳检索与引用固化，"
                       "每条结论可回溯到具体资产与版本"),
    ], size=12.5, bullet_color=GREEN, spacing=1.25)

    rect(s, Inches(6.84), Inches(1.66), Inches(5.88), Inches(4.62),
         RGBColor(0xFD, 0xF3, 0xE7), radius=True)
    rect(s, Inches(6.84), Inches(1.66), Inches(5.88), Inches(0.46), ACCENT)
    textbox(s, Inches(6.84), Inches(1.76), Inches(5.88), Inches(0.3),
            "方案级设计（尚未在目标硬件实测）", size=14, color=WHITE, bold=True,
            align=PP_ALIGN.CENTER)
    bullets(s, Inches(7.14), Inches(2.28), Inches(5.3), Inches(3.9), [
        ("昇腾 Atlas + CANN 路径", "：ONNX → om 转换（ATC 工具链）→ "
                          "CANNExecutionProvider 推理 → OM 版本管理与热加载"),
        ("代码改造点明确", "：替换 provider 即可，不需改检测算法"),
        ("大模型服务接入", "：OpenAI 兼容协议（base_url + Bearer 鉴权 + 超时"
                    "与错误分类 + 回落），已接线并通过行为测试，默认关闭"),
    ], size=12.5, bullet_color=ACCENT, spacing=1.25)
    textbox(s, Inches(7.14), Inches(5.55), Inches(5.3), Inches(0.6),
            "未在昇腾硬件上实测，因此按「方案级」表述，不作为已验证结论。",
            size=11.5, color=GRAY, spacing=1.2)

    rect(s, Inches(0.62), Inches(6.44), Inches(12.1), Inches(0.56),
         NAVY, radius=True)
    textbox(s, Inches(0.62), Inches(6.54), Inches(12.1), Inches(0.38),
            "把「已跑通」和「设计好」分开陈述 —— 这是评审信任的基础",
            size=13.5, color=WHITE, bold=True, align=PP_ALIGN.CENTER)
    return s


def slide_validation(prs):
    s = blank(prs)
    header(s, "03 技术验证", "验证体系：每一项结论都可复现", 14, TOTAL)

    chips = [
        ("自动化测试", "全绿", "pytest · 业务/状态机/幂等/契约", OCEAN),
        ("固定场景评测", "27 / 27", "无网络 · 无真实模型 · 无真实设备", TEAL),
        ("集成验收检查", "129 / 129", "真实 HTTP · 鉴权 · PG · 审批 · 幂等", GREEN),
        ("感知评测", "未评测", "独立测试集未放行 · 主动留空", RED),
    ]
    x = Inches(0.62)
    for label, value, note, col in chips:
        rect(s, x, Inches(1.72), Inches(2.92), Inches(1.32), LIGHT, radius=True)
        rect(s, x, Inches(1.72), Inches(2.92), Inches(0.05), col)
        textbox(s, x + Inches(0.2), Inches(1.86), Inches(2.6), Inches(0.28),
                label, size=11.5, color=GRAY)
        textbox(s, x + Inches(0.2), Inches(2.14), Inches(2.6), Inches(0.44),
                value, size=24, color=col, bold=True)
        textbox(s, x + Inches(0.2), Inches(2.6), Inches(2.62), Inches(0.4),
                note, size=10.5, color=GRAY, spacing=1.1)
        x += Inches(3.06)

    rows = [
        ("契约漂移检查", "接口 Schema 与文档一致性", "通过 26 / 警告 0 / 失败 0"),
        ("故障演练", "健康降级 · ACK 恢复 · 孪生闭环 · 持久化幂等", "4 组 / 127 用例"),
        ("前端浏览器回归", "登录 · 权限 · 取消确认 · 关闭与刷新恢复", "交互场景全部通过"),
        ("智能体评测环境", "network_access=false · model_access=false · device_access=false", "隔离运行"),
    ]
    y = Inches(3.3)
    for name, scope, result in rows:
        rect(s, Inches(0.62), y, Inches(2.7), Inches(0.48), NAVY, radius=True)
        textbox(s, Inches(0.62), y + Inches(0.1), Inches(2.7), Inches(0.3),
                name, size=12, color=WHITE, bold=True, align=PP_ALIGN.CENTER)
        rect(s, Inches(3.42), y, Inches(6.0), Inches(0.48), LIGHT, radius=True)
        textbox(s, Inches(3.6), y + Inches(0.11), Inches(5.7), Inches(0.3),
                scope, size=11.5, color=GRAY)
        rect(s, Inches(9.52), y, Inches(3.2), Inches(0.48), WHITE)
        textbox(s, Inches(9.7), y + Inches(0.11), Inches(2.9), Inches(0.3),
                result, size=11.5, color=TEAL, bold=True)
        y += Inches(0.56)

    rect(s, Inches(0.62), Inches(5.78), Inches(12.1), Inches(1.24),
         RGBColor(0xFD, 0xF3, 0xE7), radius=True)
    rect(s, Inches(0.62), Inches(5.78), Inches(0.06), Inches(1.24), ACCENT)
    textbox(s, Inches(0.92), Inches(5.92), Inches(11.6), Inches(0.34),
            "主动披露弱项：评测中的两项未达设计目标的指标", size=14,
            color=ACCENT, bold=True)
    textbox(s, Inches(0.92), Inches(6.28), Inches(11.6), Inches(0.68),
            "recovery_success_rate 33.3%、invalid_loop_rate 7.4%。原因：评测集包含预期失败的负向场景，"
            "该指标反映「注入故障能否被完整恢复」，不等于正常流程成功率。已完成定位并列为下一迭代优化项。\n"
            "会主动披露自己弱项并给出定位的团队，它已披露的强项才更值得相信。",
            size=11.5, color=DARK, spacing=1.25)
    return s


def slide_evidence_level(prs):
    s = blank(prs)
    header(s, "03 技术验证", "能力边界：E0–E4 证据分级", 15, TOTAL)
    lv = [("E0", "规划", "想法 / 文档 / 接口草案", GRAY),
          ("E1", "软件测试", "代码 · 单元测试 · 确定性仿真", OCEAN),
          ("E2", "合成集成", "多模块联调 · 合成数据 / 模拟设备", TEAL),
          ("E3", "现场验证", "真实设备 / 真实海域 / 真实用户", GREEN),
          ("E4", "商业验证", "合同 · 订单 · 回款 · 验收", NAVY)]
    x = Inches(0.62)
    for code, name, desc, col in lv:
        rect(s, x, Inches(1.66), Inches(2.35), Inches(0.86), col, radius=True)
        textbox(s, x, Inches(1.74), Inches(2.35), Inches(0.36), code,
                size=19, color=WHITE, bold=True, align=PP_ALIGN.CENTER)
        textbox(s, x, Inches(2.12), Inches(2.35), Inches(0.26), name,
                size=12.5, color=WHITE, bold=True, align=PP_ALIGN.CENTER)
        textbox(s, x, Inches(2.6), Inches(2.35), Inches(0.5), desc,
                size=10, color=GRAY, align=PP_ALIGN.CENTER, spacing=1.12)
        x += Inches(2.45)

    textbox(s, Inches(0.62), Inches(3.26), Inches(12.1), Inches(0.34),
            "本作品当前状态（不做越级宣称）", size=15, color=NAVY, bold=True)
    rows = [
        ("软件整体架构与治理闭环", "已实现并通过自动化测试", "E1", OCEAN),
        ("智能体内核固定场景评测", "27 场景全通过（无网络/无模型/无设备）", "E1", OCEAN),
        ("真实 HTTP + 鉴权 + 数据库 + 审批 + 幂等 + 重启回放", "129 项检查全部通过", "E2", TEAL),
        ("模型导出、推理服务化、多 provider 探测", "已实现并自检", "E1", OCEAN),
        ("云平台智能体部署与多轮工具调用验收", "工具调用无运行错误", "E2", TEAL),
        ("检测精度", "独立测试集未放行，评测输出 not_evaluated", "未评测", RED),
        ("昇腾硬件推理", "路径设计完成，未在目标硬件实测", "方案级", ACCENT),
        ("真实海域验证 / 商业合同订单", "未开展 / 未取得", "未取得", RED),
    ]
    y = Inches(3.66)
    for cap, status, tag, col in rows:
        rect(s, Inches(0.62), y, Inches(6.6), Inches(0.35),
             LIGHT if int(y / Inches(0.38)) % 2 == 0 else WHITE)
        textbox(s, Inches(0.78), y + Inches(0.05), Inches(6.4), Inches(0.26),
                cap, size=11, color=DARK)
        textbox(s, Inches(7.32), y + Inches(0.05), Inches(4.1), Inches(0.26),
                status, size=10.5, color=GRAY)
        rect(s, Inches(11.55), y + Inches(0.02), Inches(1.15), Inches(0.3),
             col, radius=True)
        textbox(s, Inches(11.55), y + Inches(0.06), Inches(1.15), Inches(0.24),
                tag, size=10.5, color=WHITE, bold=True, align=PP_ALIGN.CENTER)
        y += Inches(0.38)

    footer(s, "本作品真正的价值不在于某个漂亮的演示，而在于建立了一条从感知到核算、每个环节都可被追问和核验的治理链路。")
    return s


def slide_dataset(prs):
    s = blank(prs)
    header(s, "03 技术验证", "感知评测方法论：数据不足时，宁可不报数", 16, TOTAL)
    textbox(s, Inches(0.62), Inches(1.6), Inches(12.1), Inches(0.34),
            "当前检测路线为传统视觉（背景建模 + 颜色/形状规则 + ROI）：零数据依赖、可解释、可纯 CPU 运行，"
            "但不等同于已训练模型。",
            size=12.5, color=GRAY, spacing=1.2)

    sets = [("训练集", "模型拟合", "不得混入验证或测试样本", OCEAN),
            ("验证集", "调参与阈值选择", "不得混入测试样本", OCEAN),
            ("独立测试集", "最终评测", "按日期与点位隔离", TEAL),
            ("现场盲测集", "交付前封存", "演示前不得解封", TEAL)]
    x = Inches(0.62)
    for name, use, rule, col in sets:
        rect(s, x, Inches(2.2), Inches(2.95), Inches(1.42), LIGHT, radius=True)
        rect(s, x, Inches(2.2), Inches(2.95), Inches(0.38), col)
        textbox(s, x, Inches(2.27), Inches(2.95), Inches(0.28), name,
                size=13.5, color=WHITE, bold=True, align=PP_ALIGN.CENTER)
        textbox(s, x + Inches(0.2), Inches(2.68), Inches(2.6), Inches(0.28),
                use, size=12, color=NAVY, bold=True)
        textbox(s, x + Inches(0.2), Inches(3.0), Inches(2.62), Inches(0.5),
                rule, size=11, color=GRAY, spacing=1.15)
        x += Inches(3.07)

    rect(s, Inches(0.62), Inches(3.86), Inches(12.1), Inches(1.1),
         RGBColor(0xFC, 0xEC, 0xEA), radius=True)
    rect(s, Inches(0.62), Inches(3.86), Inches(0.06), Inches(1.1), RED)
    textbox(s, Inches(0.92), Inches(4.0), Inches(11.6), Inches(0.34),
            "评测脚本的设计原则", size=14, color=RED, bold=True)
    textbox(s, Inches(0.92), Inches(4.36), Inches(11.6), Inches(0.54),
            "清单未就绪时输出 not_evaluated 并附具体原因 —— 禁止空集下输出任何数字。"
            "这一设计避免了在数据不足时用不可信指标误导决策。",
            size=12.5, color=DARK, spacing=1.25)

    textbox(s, Inches(0.62), Inches(5.16), Inches(12.1), Inches(0.34),
            "目标指标体系（研发目标，非当前成绩）", size=15, color=NAVY, bold=True)
    items = ["各类别分别报告 Precision / Recall / F1", "AP50 / AP50-95",
             "每千帧误报数", "事件级发现率", "目标出现→事件确认时延",
             "按天气与光照分层", "样本量与置信区间"]
    x = Inches(0.62)
    for it in items:
        w = Inches(1.72) if len(it) < 8 else Inches(3.05)
        rect(s, x, Inches(5.56), w, Inches(0.44), LIGHT, radius=True)
        textbox(s, x + Inches(0.12), Inches(5.64), w - Inches(0.24), Inches(0.3),
                it, size=11, color=NAVY, bold=True)
        x += w + Inches(0.11)
    footer(s, "禁止只报告单一「准确率」；禁止用训练集结果对外；每个指标必须能追溯到原始事件和任务。")
    return s


def slide_value(prs):
    s = blank(prs)
    header(s, "04 应用价值", "对治理主体的价值与可核验指标", 17, TOTAL)
    rows = [
        ("效率", "空驶减少：就近派单替代经验派单", "空驶距离占比 / 总航行距离", OCEAN),
        ("响应", "事件确认到任务下发自动触发", "治理响应时延（发现→任务开始）", OCEAN),
        ("风险", "高危动作纳入审批与策略守卫", "高风险动作审批覆盖率（目标 100%）", TEAL),
        ("可信", "每个结论可回溯到原始证据", "有证据支撑的结论占比", TEAL),
        ("成本", "单位闭环成本可计算", "每吨综合清理成本 / 每闭环任务成本", TEAL),
    ]
    y = Inches(1.7)
    for name, value, metric, col in rows:
        rect(s, Inches(0.62), y, Inches(1.15), Inches(0.62), col, radius=True)
        textbox(s, Inches(0.62), y + Inches(0.17), Inches(1.15), Inches(0.3),
                name, size=14, color=WHITE, bold=True, align=PP_ALIGN.CENTER)
        rect(s, Inches(1.87), y, Inches(6.05), Inches(0.62), LIGHT, radius=True)
        textbox(s, Inches(2.05), y + Inches(0.18), Inches(5.8), Inches(0.3),
                value, size=12.5, color=NAVY)
        rect(s, Inches(8.02), y, Inches(4.7), Inches(0.62), WHITE)
        textbox(s, Inches(8.2), y + Inches(0.11), Inches(4.45), Inches(0.44),
                metric, size=11.5, color=GRAY, spacing=1.12)
        y += Inches(0.72)

    rect(s, Inches(0.62), Inches(5.42), Inches(12.1), Inches(1.58), NAVY, radius=True)
    textbox(s, Inches(0.98), Inches(5.58), Inches(11.4), Inches(0.32),
            "建议的北极星指标", size=13, color=TEAL, bold=True)
    textbox(s, Inches(0.98), Inches(5.92), Inches(11.4), Inches(0.5),
            "单位有效成本下完成闭环并经过证据确认的垃圾清理量",
            size=21, color=WHITE, bold=True)
    textbox(s, Inches(0.98), Inches(6.5), Inches(11.4), Inches(0.42),
            "只有这个指标同时约束了成本与质量 —— 避免「多花钱多检测但没闭环」"
            "与「少花钱但数据造假」两种失真路径。",
            size=12, color=RGBColor(0xA8, 0xC8, 0xDC), spacing=1.2)
    return s


def slide_social(prs):
    s = blank(prs)
    header(s, "04 应用价值", "社会价值与可推广性", 18, TOTAL)
    items = [
        ("降低临水作业风险", "把部分高风险水域的清理动作从「必须人到现场」转为「远程派单 + 可控执行」。"),
        ("提升治理资源效率", "让有限的作业队与设备优先处理高价值目标，减少无效消耗。"),
        ("支撑绿色海洋治理", "海漂垃圾治理是近岸生态治理的长期议题，本系统提供可量化的过程与效果数据。"),
        ("数字化能力沉淀", "为沿海县区提供可复制的「感知—决策—执行—核算」信息基础设施。"),
    ]
    x = Inches(0.62)
    for name, desc in items:
        rect(s, x, Inches(1.72), Inches(2.95), Inches(2.0), LIGHT, radius=True)
        rect(s, x, Inches(1.72), Inches(2.95), Inches(0.05), TEAL)
        textbox(s, x + Inches(0.24), Inches(1.92), Inches(2.5), Inches(0.6),
                name, size=14.5, color=NAVY, bold=True, spacing=1.15)
        textbox(s, x + Inches(0.24), Inches(2.58), Inches(2.52), Inches(1.05),
                desc, size=11.5, color=GRAY, spacing=1.22)
        x += Inches(3.07)

    textbox(s, Inches(0.62), Inches(4.06), Inches(12.1), Inches(0.36),
            "可推广性：核心逻辑与具体水域无关，迁移只需三步",
            size=16, color=NAVY, bold=True)
    steps = [("接入检测源", "摄像头 / 无人机 / 上报"),
             ("标定感知参数", "ROI · 类别规则 · 时序窗口"),
             ("配置派单策略", "距离 / 电量 / 仓容的场景化权重")]
    x = Inches(0.62)
    for i, (name, desc) in enumerate(steps, 1):
        rect(s, x, Inches(4.52), Inches(3.9), Inches(1.0), WHITE)
        rect(s, x, Inches(4.52), Inches(0.055), Inches(1.0), OCEAN)
        textbox(s, x + Inches(0.22), Inches(4.64), Inches(3.5), Inches(0.3),
                f"STEP {i}  {name}", size=13, color=NAVY, bold=True)
        textbox(s, x + Inches(0.22), Inches(4.98), Inches(3.5), Inches(0.42),
                desc, size=11.5, color=GRAY)
        x += Inches(4.06)

    rect(s, Inches(0.62), Inches(5.72), Inches(12.1), Inches(1.24),
         RGBColor(0xFD, 0xF3, 0xE7), radius=True)
    rect(s, Inches(0.62), Inches(5.72), Inches(0.06), Inches(1.24), ACCENT)
    textbox(s, Inches(0.92), Inches(5.88), Inches(11.6), Inches(0.34),
            "成本模型的纪律", size=14, color=ACCENT, bold=True)
    textbox(s, Inches(0.92), Inches(6.24), Inches(11.6), Inches(0.68),
            "单位闭环成本 =（设备折旧 + 能源 + 运维 + 人工协同 + 通信）÷ 已验收清理量\n"
            "每一项成本标注来源类型（公开资料 / 询价 / 访谈 / 假设），可做敏感性分析。"
            "所有商业数字当前为测算值，不构成成交承诺。",
            size=11.5, color=DARK, spacing=1.28)
    return s


def slide_demo(prs):
    s = blank(prs)
    header(s, "04 应用价值", "演示案例：完整闭环与异常路径", 19, TOTAL)
    rect(s, Inches(0.62), Inches(1.66), Inches(12.1), Inches(0.86), NAVY, radius=True)
    textbox(s, Inches(0.9), Inches(1.8), Inches(11.6), Inches(0.6),
            "模拟事件注入 → 时序验证与研判 → 智能体生成派单计划（含淘汰原因）→ 策略校验 / 审批\n"
            "→ MQTT 下发 → 设备 ACK 与进度遥测 → 作业结果回传 → 证据归档 → 治理报表与单位成本输出",
            size=12.5, color=WHITE, spacing=1.35)

    textbox(s, Inches(0.62), Inches(2.74), Inches(12.1), Inches(0.36),
            "同时演示异常路径 —— 出问题时的表现，比顺利流程更能说明系统成熟度",
            size=15, color=NAVY, bold=True)
    rows = [
        ("无可用设备", "不误报成功，记录 no_robot_available，进入待补派队列"),
        ("工具超时", "有重试上限，能重规划或安全失败，不无限循环"),
        ("策略拒绝", "动作在工具执行前被阻断，产生结构化原因"),
        ("审批拒绝", "Run 终止，不发送任何设备指令"),
        ("模型不可用", "自动回落规则模式，系统功能不降级"),
    ]
    x = Inches(0.62)
    for name, desc in rows:
        rect(s, x, Inches(3.2), Inches(2.35), Inches(1.66), LIGHT, radius=True)
        rect(s, x, Inches(3.2), Inches(2.35), Inches(0.42), RED)
        textbox(s, x, Inches(3.28), Inches(2.35), Inches(0.3), name,
                size=12.5, color=WHITE, bold=True, align=PP_ALIGN.CENTER)
        textbox(s, x + Inches(0.2), Inches(3.74), Inches(1.98), Inches(1.0),
                desc, size=11, color=GRAY, spacing=1.2)
        x += Inches(2.46)

    rect(s, Inches(0.62), Inches(5.14), Inches(12.1), Inches(1.86),
         RGBColor(0xE9, 0xF6, 0xEF), radius=True)
    rect(s, Inches(0.62), Inches(5.14), Inches(0.06), Inches(1.86), GREEN)
    textbox(s, Inches(0.92), Inches(5.32), Inches(11.6), Inches(0.34),
            "五种异常场景的共同点", size=15, color=GREEN, bold=True)
    textbox(s, Inches(0.92), Inches(5.7), Inches(11.6), Inches(0.5),
            "系统从不假装成功。",
            size=19, color=NAVY, bold=True)
    textbox(s, Inches(0.92), Inches(6.24), Inches(11.6), Inches(0.72),
            "没有设备时记录原因并等待补派，而不是伪造一个「已派单」；审批被拒时直接终止，不下发指令；"
            "模型不可用时回落规则模式，功能不下降。\n"
            "另：后端不可用时前端必须显示真实错误与降级状态，禁止展示静态的「在线」「运行中」。",
            size=12, color=DARK, spacing=1.28)
    return s


def slide_limits(prs):
    s = blank(prs)
    header(s, "04 应用价值", "已知限制与下一步", 20, TOTAL)
    textbox(s, Inches(0.62), Inches(1.62), Inches(12.1), Inches(0.34),
            "诚实列出限制，比声称完备更有利于长期信任：",
            size=15, color=DARK)
    rows = [
        ("检测精度未评测", "独立测试集未放行", "无法给出真实精度指标", "采集并放行标注数据，按日期点位隔离评测"),
        ("昇腾路径未实测", "设计完成，未上真机", "国产算力适配无硬件实证", "申请赛事算力平台，完成一次真实推理验证"),
        ("模型未接入", "适配器已接线但默认关闭", "意图理解与异常解释受限", "接入国产大模型服务并做效果评测"),
        ("无真实海域验证", "未开展", "不能声称现场可用", "在受控水域完成一次现场短时验证"),
        ("无商业验证", "无合同 / 订单 / 回款", "商业模式为测算", "推进单点试点，取得可核验的成本数据"),
        ("部分恢复指标偏低", "恢复率 33.3%", "故障恢复能力待增强", "已定位原因，列入下一迭代优化项"),
    ]
    y = Inches(2.1)
    for name, status, impact, nextstep in rows:
        rect(s, Inches(0.62), y, Inches(2.45), Inches(0.62), LIGHT, radius=True)
        rect(s, Inches(0.62), y, Inches(0.05), Inches(0.62), RED)
        textbox(s, Inches(0.82), y + Inches(0.09), Inches(2.2), Inches(0.3),
                name, size=12.5, color=NAVY, bold=True)
        textbox(s, Inches(0.82), y + Inches(0.35), Inches(2.2), Inches(0.24),
                status, size=10.5, color=GRAY)
        textbox(s, Inches(3.24), y + Inches(0.16), Inches(3.6), Inches(0.4),
                impact, size=11.5, color=GRAY)
        rect(s, Inches(6.98), y, Inches(5.74), Inches(0.62), RGBColor(0xE9, 0xF6, 0xEF), radius=True)
        textbox(s, Inches(7.18), y + Inches(0.16), Inches(5.4), Inches(0.4),
                nextstep, size=11.5, color=GREEN, bold=True)
        y += Inches(0.72)

    footer(s, "每一项限制都对应明确的下一步工作与所需条件，而非含糊的「有待完善」。")
    return s


def slide_ending(prs):
    s = blank(prs, bg=NAVY)
    rect(s, 0, 0, Inches(0.22), H, TEAL)
    textbox(s, Inches(1.1), Inches(1.72), Inches(11), Inches(0.5),
            "探海灵眸 Oceanus", size=38, color=WHITE, bold=True)
    rect(s, Inches(1.1), Inches(2.44), Inches(1.5), Inches(0.035), ACCENT)
    textbox(s, Inches(1.1), Inches(2.78), Inches(11), Inches(1.1),
            "让每一次发现，\n都能被证明已经闭环",
            size=30, color=RGBColor(0xC9, 0xE4, 0xF0), spacing=1.35)
    textbox(s, Inches(1.1), Inches(4.36), Inches(11), Inches(0.9),
            "核心价值不在于某一个漂亮的算法，\n"
            "而在于一条从感知到核算、每个环节都可被追问和核验的治理链路。",
            size=15, color=RGBColor(0x9F, 0xC4, 0xD8), spacing=1.4)
    textbox(s, Inches(1.1), Inches(5.86), Inches(11), Inches(0.4),
            "谢谢各位评委  ·  恳请批评指正", size=17, color=TEAL, bold=True)
    return s


# --------------------------------------------------------------------------
# 构建
# --------------------------------------------------------------------------

BUILDERS = [
    slide_cover, slide_agenda, slide_problem, slide_jtbd,
    slide_architecture, slide_stack, slide_innovation1, slide_innovation2,
    slide_llm_role, slide_tools, slide_evidence_chain, slide_hardware,
    slide_compute, slide_validation, slide_evidence_level, slide_dataset,
    slide_value, slide_social, slide_demo, slide_limits, slide_ending,
]


def build(out_path: Path) -> Path:
    prs = Presentation()
    prs.slide_width = W
    prs.slide_height = H
    for fn in BUILDERS:
        fn(prs)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out_path))
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser(description="生成海峡大学生 AI 大赛作品介绍 PPT")
    ap.add_argument("--out", required=True, help="输出 .pptx 路径")
    args = ap.parse_args()
    out = build(Path(args.out))
    print(f"[OK] 已生成 {out}（{len(BUILDERS)} 页）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())