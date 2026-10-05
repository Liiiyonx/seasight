#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成软件著作权登记的「软件说明书」（文档鉴别材料，操作手册体裁）。

规范依据（中国版权保护中心登记要求）：
  1. 文档鉴别材料可为用户手册 / 操作手册 / 设计说明书之一；
  2. 前、后各连续 30 页，共 60 页；整个文档不足 60 页的，提交全部；
  3. 页眉标注软件全称及版本号，页码；
  4. 有画面的页面可不足 30 行文字。

体裁选择：操作手册（最直观、与系统截图强绑定）。
截图来源：artifacts/ui-responsive/1920x1080-*.png 及 vision-preview 检测效果图。

用法：
    .\\.venv-analysis\\Scripts\\python.exe scripts\\build_copyright_manual.py \
        --out "<USER_HOME>\\Desktop\\Oceanus软著材料"
"""

from __future__ import annotations

import argparse
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

NAVY = RGBColor(0x0F, 0x2E, 0x4C)
OCEAN = RGBColor(0x1D, 0x6A, 0x9C)
GRAY = RGBColor(0x60, 0x60, 0x60)
DARK = RGBColor(0x1B, 0x2A, 0x38)
RED = RGBColor(0xB0, 0x30, 0x22)

CJK = "Microsoft YaHei"
MONO = "Consolas"

UI = ROOT / "artifacts" / "ui-responsive"
VP = ROOT / "artifacts" / "vision-preview"


def set_cjk(run, font=CJK, size=10.5, color=DARK, bold=False):
    run.font.name = font
    run.font.size = Pt(size)
    run.font.color.rgb = color
    run.bold = bold
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), font)
    rfonts.set(qn("w:ascii"), font)
    rfonts.set(qn("w:hAnsi"), font)


class Manual:
    def __init__(self) -> None:
        self.doc = Document()
        self._setup()

    def _setup(self) -> None:
        section = self.doc.sections[0]
        section.page_width = Cm(21.0)
        section.page_height = Cm(29.7)
        section.top_margin = Cm(2.4)
        section.bottom_margin = Cm(2.2)
        section.left_margin = Cm(2.6)
        section.right_margin = Cm(2.4)
        section.header_distance = Cm(1.2)
        section.footer_distance = Cm(1.1)

        hp = section.header.paragraphs[0]
        hp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = hp.add_run(HEADER_TEXT)
        set_cjk(run, size=8, color=GRAY)

        fp = section.footer.paragraphs[0]
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        pre = fp.add_run("第 ")
        set_cjk(pre, size=8, color=GRAY)
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
        set_cjk(post, size=8, color=GRAY)

    def page_break(self) -> None:
        self.doc.add_page_break()

    def h1(self, text: str) -> None:
        p = self.doc.add_paragraph()
        p.paragraph_format.space_before = Pt(6)
        p.paragraph_format.space_after = Pt(10)
        set_cjk(p.add_run(text), size=17, color=NAVY, bold=True)

    def h2(self, text: str) -> None:
        p = self.doc.add_paragraph()
        p.paragraph_format.space_before = Pt(12)
        p.paragraph_format.space_after = Pt(6)
        set_cjk(p.add_run(text), size=13.5, color=OCEAN, bold=True)

    def h3(self, text: str) -> None:
        p = self.doc.add_paragraph()
        p.paragraph_format.space_before = Pt(8)
        p.paragraph_format.space_after = Pt(4)
        set_cjk(p.add_run(text), size=11.5, color=NAVY, bold=True)

    def para(self, text: str, size=10.5) -> None:
        p = self.doc.add_paragraph()
        p.paragraph_format.first_line_indent = Cm(0.74)
        p.paragraph_format.line_spacing = 1.4
        set_cjk(p.add_run(text), size=size)

    def bullets(self, items: list[str]) -> None:
        for it in items:
            p = self.doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.6)
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.line_spacing = 1.35
            set_cjk(p.add_run("•  "), size=10.5, color=OCEAN, bold=True)
            set_cjk(p.add_run(it), size=10.5)

    def steps(self, items: list[str]) -> None:
        for i, it in enumerate(items, 1):
            p = self.doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.6)
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.line_spacing = 1.35
            set_cjk(p.add_run(f"步骤{i}  "), size=10.5, color=RED, bold=True)
            set_cjk(p.add_run(it), size=10.5)

    def table(self, headers: list[str], rows: list[list[str]]) -> None:
        t = self.doc.add_table(rows=1 + len(rows), cols=len(headers))
        t.style = "Table Grid"
        for j, h in enumerate(headers):
            cell = t.rows[0].cells[j]
            cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
            set_cjk(cell.paragraphs[0].add_run(h), size=10, color=NAVY, bold=True)
            tc_pr = cell._tc.get_or_add_tcPr()
            shd = OxmlElement("w:shd")
            shd.set(qn("w:val"), "clear")
            shd.set(qn("w:fill"), "E8F1F7")
            tc_pr.append(shd)
        for i, row in enumerate(rows, 1):
            for j, val in enumerate(row):
                cell = t.rows[i].cells[j]
                set_cjk(cell.paragraphs[0].add_run(val), size=9.5)
        self.doc.add_paragraph().paragraph_format.space_after = Pt(2)

    def image(self, path: Path, caption: str, width_cm=15.5) -> None:
        if not path.exists():
            self.para(f"［截图待补：{caption}（{path.name}）］")
            return
        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run().add_picture(str(path), width=Cm(width_cm))
        cap = self.doc.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        set_cjk(cap.add_run(caption), size=9, color=GRAY)

    def caption_note(self, text: str) -> None:
        p = self.doc.add_paragraph()
        p.paragraph_format.space_after = Pt(6)
        set_cjk(p.add_run(text), size=9, color=GRAY)

    def build(self) -> Document:
        return self.doc


def build_cover(m: Manual) -> None:
    for _ in range(5):
        m.doc.add_paragraph()
    p = m.doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_cjk(p.add_run(SOFTWARE_FULL_NAME), size=26, color=NAVY, bold=True)
    p = m.doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_cjk(p.add_run(f"（简称：{SOFTWARE_SHORT_NAME}）"), size=16, color=OCEAN)
    p = m.doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_cjk(p.add_run(f"{VERSION}"), size=20, color=NAVY, bold=True)
    for _ in range(2):
        m.doc.add_paragraph()
    p = m.doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_cjk(p.add_run("软 件 说 明 书"), size=22, color=DARK, bold=True)
    for _ in range(6):
        m.doc.add_paragraph()
    for line in ("文档类别：操作手册", "著作权人：福州理工学院", "编写日期：2026 年 10 月"):
        p = m.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        set_cjk(p.add_run(line), size=12, color=GRAY)
    m.page_break()


def build_toc(m: Manual) -> None:
    m.h1("目  录")
    toc = [
        "1  引言",
        "　1.1  编写目的",
        "　1.2  软件背景",
        "　1.3  术语与缩略语",
        "2  软件概述",
        "　2.1  软件定位与治理闭环",
        "　2.2  功能清单",
        "　2.3  技术架构",
        "3  运行环境",
        "　3.1  硬件环境",
        "　3.2  软件环境",
        "4  部署与初始化",
        "5  功能操作说明",
        "　5.1  用户登录",
        "　5.2  治理大屏",
        "　5.3  事件管理",
        "　5.4  工单管理",
        "　5.5  设备管理",
        "　5.6  统计报表",
        "　5.7  AI 助手",
        "　5.8  智能体工作台",
        "　5.9  知识域工作台",
        "　5.10  消息通知中心",
        "　5.11  边缘检测效果说明",
        "6  常见问题与处理",
        "7  附录：接口与数据约定",
    ]
    # ★ 目录不用手打页码 —— 手写的页码永远对不上（内容一改就漂）。
    #   改用 Word 的 TOC 域：打开时自动更新，页码由排版引擎计算。
    #   域代码 \\o "1-2" 表示收录 1~2 级标题。
    p = m.doc.add_paragraph()
    p.paragraph_format.line_spacing = 1.6
    run = p.add_run()
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = 'TOC \\o "1-2" \\h \\z \\u'
    fld_sep = OxmlElement("w:fldChar")
    fld_sep.set(qn("w:fldCharType"), "separate")
    # separate 与 end 之间放占位文字：Word 未更新域时也能看到内容
    for el in (fld_begin, instr, fld_sep):
        run._element.append(el)
    ph = p.add_run("（在 Word 中按 Ctrl+A 后 F9 更新目录，即可生成带页码的完整目录）")
    set_cjk(ph, size=9, color=RGBColor(0x80, 0x80, 0x80))
    end_run = p.add_run()
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    end_run._element.append(fld_end)

    # 另附一份静态清单（不依赖域更新，方便打印稿直接阅读）
    m.h2("章节清单")
    for line in toc:
        tp = m.doc.add_paragraph()
        tp.paragraph_format.line_spacing = 1.5
        set_cjk(tp.add_run(line), size=10)
    m.page_break()


def build_intro(m: Manual) -> None:
    m.h1("1  引言")
    m.h2("1.1  编写目的")
    m.para(
        "本说明书是「探海灵眸海洋环境治理智能体软件[简称:Oceanus] V1.0」的操作手册，"
        "面向系统管理员、值班研判人员、业务操作员与审批人员，"
        "说明软件的功能构成、运行环境、部署方式以及各功能模块的操作方法。"
    )
    m.h2("1.2  软件背景")
    m.para(
        "近海养殖区海漂垃圾治理长期依赖人工巡查与人工派工，存在发现不及时、"
        "清理无台账、责任难追溯等问题。本软件面向县域海洋环境治理场景，"
        "构建“岸基感知—平台决策—机器人执行—数据回传”的闭环体系："
        "岸基摄像头实时识别水面漂浮垃圾并上报事件，平台智能体对事件进行研判、"
        "审批与派单，水面机器人执行清理并回传结果，全过程形成可审计的证据链"
        "与量化治理报表。"
    )
    m.h2("1.3  术语与缩略语")
    m.table(
        ["术语", "说明"],
        [
            ["事件（Event）", "边缘端识别到的一片垃圾记录，含类别、位置、时间与证据图"],
            ["工单（Task）", "派发给执行设备的清理任务，具有完整状态机"],
            ["派单（Dispatch）", "把事件转化为工单的决策过程，含五步筛选与防抖合并"],
            ["垃圾四分类", "foam（泡沫）、plastic（塑胶）、fishing_gear（渔具）、other（其他）"],
            ["ACK 回执", "执行设备收到指令后的确认应答，超时未确认自动换设备重派"],
            ["智能体（Agent）", "进行事件研判与决策的运行实体，决策轨迹全程留痕可审计"],
            ["知识资产", "登记入库的文档、表格、图片、事件、遥测等多模态数据资产"],
            ["时序校验", "边缘端误报抑制机制：单帧检测不确认，连续命中方可上报"],
        ],
    )
    m.page_break()


def build_overview(m: Manual) -> None:
    m.h1("2  软件概述")
    m.h2("2.1  软件定位与治理闭环")
    m.para(
        "本软件是一套面向海洋养殖区海漂垃圾治理的三端协同智能系统，"
        "覆盖“感知—决策—执行—数据价值”全链路："
    )
    m.bullets([
        "感知：岸基摄像头旁的边缘盒完成取流、检测、时序校验与事件上报；",
        "决策：平台对事件进行智能研判、防抖合并、人工审批与工单派发；",
        "执行：水面机器人接收指令执行清理，回传 ACK 回执与作业数据；",
        "数据价值：热力图、派单调度、量化报表与溯源分析沉淀治理资产。",
    ])
    m.para(
        "与市面单机清理设备相比，本软件的差异在于系统级闭环：识别对象针对"
        "中国养殖区特色垃圾（EPS 泡沫浮球碎片、废旧渔具、饵料袋），"
        "打捞执行端可替换、可离线，软件侧独立形成事件理解、规划、工具调用、"
        "策略守卫、审批、派单、审计与回放的完整能力。"
    )
    m.h2("2.2  功能清单")
    m.table(
        ["子系统", "主要功能"],
        [
            ["边缘感知软件", "视频取流、垃圾检测、时序校验误报抑制、事件上报"],
            ["数据平台后端", "事件接入、派单引擎、工单状态机、设备管理、报表统计、权限与审计"],
            ["智能体内核", "事件研判、决策规划、工具调用、策略守卫、人工审批、记忆与复盘"],
            ["知识智能体", "多模态知识资产登记、动态本体构建、跨文档多跳检索、证据链"],
            ["Web 前端", "治理大屏、事件/工单/设备/报表管理、AI 助手、知识域与审批工作台"],
            ["消息链路", "MQTT 设备接入、ACK 超时追踪、WebSocket 实时推送"],
        ],
    )
    m.h2("2.3  技术架构")
    m.para("软件采用分层架构，主要技术选型如下：")
    m.table(
        ["层次", "技术", "用途"],
        [
            ["后端服务", "Python 3.11 / FastAPI", "异步接口服务、OpenAPI 文档"],
            ["数据存储", "PostgreSQL + PostGIS / Redis", "业务与空间数据、缓存与事件队列"],
            ["消息总线", "EMQX（MQTT）", "移动网络友好的设备接入"],
            ["对象存储", "MinIO", "告警帧与作业影像存储"],
            ["流媒体", "go2rtc", "RTSP 视频转 WebRTC/FLV 低延迟播放"],
            ["前端", "Vue 3 / Vite / Pinia", "单页应用与实时状态管理"],
            ["可视化", "ECharts / Leaflet / Three.js", "图表、地图与机械臂三维仿真"],
            ["边缘检测", "OpenCV（主链路）/ YOLO-World（对照）", "漂浮垃圾检测"],
            ["容器化", "Docker Compose", "一键部署全部服务"],
        ],
    )
    m.page_break()


def build_env(m: Manual) -> None:
    m.h1("3  运行环境")
    m.h2("3.1  硬件环境")
    m.table(
        ["节点", "最低配置", "说明"],
        [
            ["平台服务器", "8 核 CPU / 16 GB 内存 / 200 GB 磁盘", "运行后端、数据库、消息与应用服务"],
            ["边缘计算盒", "4 栒 CPU / 8 GB 内存", "岸基摄像头旁部署，运行边缘感知软件"],
            ["客户端", "2 栒 CPU / 4 GB 内存，1920×1080 显示器", "Chrome / Edge 浏览器访问"],
        ],
    )
    m.h2("3.2  软件环境")
    m.table(
        ["类别", "要求"],
        [
            ["操作系统", "Linux（服务端，推荐 Ubuntu 22.04）；边缘盒支持 Linux/Windows"],
            ["容器环境", "Docker 24+ 与 Docker Compose v2"],
            ["数据库", "PostgreSQL 14+（含 PostGIS 扩展）、Redis 7"],
            ["消息中间件", "EMQX 5.x"],
            ["浏览器", "Chrome 100+ / Edge 100+（推荐 1920×1080 分辨率）"],
        ],
    )
    m.page_break()


def build_deploy(m: Manual) -> None:
    m.h1("4  部署与初始化")
    m.steps([
        "准备服务器并安装 Docker 与 Docker Compose；",
        "复制 .env.example 为 .env，按现场填写数据库、消息队列、对象存储等连接参数；",
        "执行 docker compose up -d 启动全部服务（后端、前端、数据库、Redis、EMQX、MinIO、流媒体）；",
        "执行初始化脚本创建演示账号与基础数据；",
        "浏览器访问平台地址，使用管理员账号登录，进入「设备管理」完成摄像头与执行设备的登记；",
        "在边缘盒部署边缘感知软件，配置点位与上报地址，启动后事件自动上报至平台。",
    ])
    m.para(
        "部署完成后，系统提供四类预置角色：admin（全部权限）、operator（写操作）、"
        "approver（只读与智能体审批）、viewer（只读）。权限由服务端签名令牌承载，"
        "所有写操作均记录审计日志。"
    )
    m.page_break()


def build_operations(m: Manual) -> None:
    m.h1("5  功能操作说明")
    m.para(
        "本章按页面逐一说明各功能模块的操作方法。截图取自实际运行的 Web 前端。"
    )

    # 5.1 登录
    m.h2("5.1  用户登录")
    m.para(
        "打开平台地址进入登录页。输入账号与密码完成登录；系统按角色签发"
        "服务端签名令牌，并跳转至对应权限的默认页面。连续输错将触发限速保护。"
    )
    m.image(UI / "1920x1080-login.png", "图 5-1  登录页面")
    m.page_break()

    # 5.2 大屏
    m.h2("5.2  治理大屏")
    m.para(
        "治理大屏是系统的主视图，实时汇聚关键治理指标：今日事件数、待处理工单、"
        "清理量统计、垃圾类别分布、事件热力地图与实时告警流。数据通过 WebSocket "
        "实时推送，页面无需手动刷新。"
    )
    m.image(UI / "1920x1080-dashboard.png", "图 5-2  治理大屏（指标、热力地图与实时事件）")
    m.bullets([
        "顶部统计卡片：今日事件、待派单、进行中工单、累计清理量；",
        "中部热力地图：按网格聚合事件密度，点击网格可查看明细；",
        "右侧实时事件流：新事件上报后秒级出现，可一键跳转详情。",
    ])
    m.page_break()

    # 5.3 事件
    m.h2("5.3  事件管理")
    m.para(
        "事件页面管理边缘端上报的全部垃圾事件。支持按类别、时间段、状态、"
        "点位筛选；每条事件包含类别、位置、时间、置信度与证据图。"
        "对同一位置短时间内的重复事件，系统自动防抖合并，避免重复派单。"
    )
    m.image(UI / "1920x1080-events.png", "图 5-3  事件管理页面")
    m.steps([
        "在筛选栏选择类别（泡沫/塑胶/渔具/其他）与时间范围；",
        "点击事件行查看详情与证据图；",
        "确认事件有效后点击「派单」，选择执行设备并提交；",
        "系统生成工单并进入工单流转。",
    ])
    m.page_break()

    # 5.4 工单
    m.h2("5.4  工单管理")
    m.para(
        "工单页面跟踪清理任务的完整生命周期：待确认 → 已下发 → 执行中 → "
        "已完成 / 已取消。设备收到指令后回传 ACK 回执，超时未确认的工单"
        "自动换设备重派；执行完成后回传作业数据与影像。"
    )
    m.image(UI / "1920x1080-tasks.png", "图 5-4  工单管理页面")
    m.bullets([
        "状态机流转全程留痕，任何状态变更均记录操作人与时间；",
        "支持人工补派、取消与备注；",
        "工单详情页可查看 ACK 回执时间线与执行结果影像。",
    ])
    m.page_break()

    # 5.5 设备
    m.h2("5.5  设备管理")
    m.para(
        "设备页面管理三类设备：岸基摄像头、无人机与水面机器人。"
        "可查看设备在线状态、最近心跳、绑定点位与负责区域；"
        "支持设备登记、编辑、启停与协议配置。"
    )
    m.image(UI / "1920x1080-devices.png", "图 5-5  设备管理页面")
    m.steps([
        "点击「新增设备」，选择设备类型并填写名称、点位坐标与通信参数；",
        "保存后设备进入离线状态，边缘程序或机载程序上线后自动转为在线；",
        "对异常设备可执行停用操作，停用后不再参与派单。",
    ])
    m.page_break()

    # 5.6 报表
    m.h2("5.6  统计报表")
    m.para(
        "报表页面提供治理成效的量化统计：按日/周/月的事件趋势、清理量趋势、"
        "类别构成、点位排名与设备作业量。报表支持导出，作为治理台账留档。"
    )
    m.image(UI / "1920x1080-reports.png", "图 5-6  统计报表页面")
    m.bullets([
        "趋势图支持时间粒度切换（日/周/月）；",
        "类别构成饼图反映四类垃圾占比变化；",
        "导出结果与页面数据一致，可作为考核依据。",
    ])
    m.page_break()

    # 5.7 AI 助手
    m.h2("5.7  AI 助手")
    m.para(
        "AI 助手是面向值班人员的自然语言问答入口，可查询事件、工单、设备与"
        "统计数据，并触发常用操作。助手由大模型与规则兜底共同驱动，"
        "对高风险操作（如派单）会先请求人工确认。"
    )
    m.image(UI / "1920x1080-assistant.png", "图 5-7  AI 助手对话页面")
    m.steps([
        "在输入框用自然语言提问，例如“今天泡沫类事件有多少”；",
        "助手返回结构化结果并附数据来源；",
        "对涉及写操作的请求，页面弹出确认框，人工确认后执行。",
    ])
    m.page_break()

    # 5.8 智能体
    m.h2("5.8  智能体工作台")
    m.para(
        "智能体工作台展示智能体的决策运行轨迹：每次运行的规划步骤、工具调用、"
        "审批节点与记忆更新全程留痕。审批人员可在此对智能体的高风险决策"
        "进行批准或驳回，所有决定记录在案，形成可审计的证据链。"
    )
    m.image(UI / "1920x1080-agents.png", "图 5-8  智能体工作台（运行轨迹与审批）")
    m.bullets([
        "运行列表：按时间与状态筛选智能体运行记录；",
        "轨迹视图：逐步展示规划、工具调用与结果；",
        "审批视图：approver 角色可批准或驳回待审批决策。",
    ])
    m.page_break()

    # 5.9 知识域
    m.h2("5.9  知识域工作台")
    m.para(
        "知识域工作台管理系统的多模态知识资产：政策文件、监测报告、台账表格、"
        "事件与遥测数据登记入库后形成版本不可变的知识资产；系统支持半自动"
        "本体构建与跨文档多跳检索，为治理决策提供可溯源的证据支撑。"
    )
    m.image(UI / "1920x1080-knowledge.png", "图 5-9  知识域工作台")
    m.steps([
        "在资产页上传或登记文档/表格/图片，系统计算内容哈希并生成版本；",
        "在本体页审核系统抽取的本体候选，发布本体版本；",
        "在检索页进行跨文档检索，结果附带原文定位与证据链。",
    ])
    m.page_break()

    # 5.10 通知
    m.h2("5.10  消息通知中心")
    m.para(
        "通知中心汇聚系统告警与业务消息：设备离线、事件激增、工单超时、"
        "审批待办等。支持移动端自适应布局，值班人员可在手机上即时收到提醒。"
    )
    m.image(UI / "390x844-notifications.png", "图 5-10  通知中心（移动端自适应布局）", width_cm=8.0)
    m.page_break()

    # 5.11 检测效果
    m.h2("5.11  边缘检测效果说明")
    m.para(
        "边缘感知软件对养殖区特色垃圾进行检测识别，以下为实际检测效果示例，"
        "识别结果叠加在视频帧上并附带类别与置信度。检测采用时序校验机制，"
        "单帧命中不确认，连续命中方上报事件，有效抑制水面波光与反光误报。"
    )
    m.image(VP / "foam-pellets-4k-detected.jpg", "图 5-11  泡沫浮球碎片检测效果", width_cm=14.5)
    m.image(VP / "fishing-net-4k-detected.jpg", "图 5-12  废弃渔网检测效果", width_cm=14.5)
    m.image(VP / "beach-mixed-debris-4k-detected.jpg", "图 5-13  混合垃圾检测效果", width_cm=14.5)
    m.page_break()


def build_faq(m: Manual) -> None:
    m.h1("6  常见问题与处理")
    m.table(
        ["现象", "可能原因", "处理方法"],
        [
            ["登录后页面空白", "账号无对应角色权限", "联系管理员确认角色分配"],
            ["大屏数据不更新", "WebSocket 连接中断", "检查网络后刷新页面；仍异常则查看后端日志"],
            ["事件持续无上报", "边缘盒离线或时序校验未命中", "在设备页检查边缘盒心跳；核对摄像头取流地址"],
            ["工单长时间待确认", "执行设备未回 ACK", "等待系统超时自动重派，或人工补派至其他设备"],
            ["检测误报较多", "点位反光或阈值不适配", "在边缘配置中调整时序校验与置信度阈值"],
        ],
    )
    m.page_break()


def build_appendix(m: Manual) -> None:
    m.h1("7  附录：接口与数据约定")
    m.para("软件对外提供 REST API（/api/v1 前缀）与 WebSocket 实时通道，主要约定如下：")
    m.table(
        ["类别", "约定"],
        [
            ["认证", "登录签发 JWT 令牌，后续请求携带 Authorization: Bearer 头"],
            ["响应信封", "统一 {code, message, data} 结构，附 trace_id 便于追踪"],
            ["事件上报", "边缘端经 MQTT 上报，QoS1 幂等去重"],
            ["工单指令", "平台经 MQTT 下发，设备回传 ACK 回执与执行结果"],
            ["实时推送", "WebSocket 推送事件、工单状态与设备上下线消息"],
        ],
    )
    m.para(
        "本说明书随软件版本迭代更新。如与系统实际界面存在差异，以软件实际运行版本为准。"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="生成软著软件说明书")
    parser.add_argument(
        "--out", default=r"<USER_HOME>\Desktop\Oceanus软著材料", help="输出目录"
    )
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    m = Manual()
    build_cover(m)
    build_toc(m)
    build_intro(m)
    build_overview(m)
    build_env(m)
    build_deploy(m)
    build_operations(m)
    build_faq(m)
    build_appendix(m)

    doc = m.build()
    path = out_dir / f"软件说明书_{SOFTWARE_FULL_NAME}{VERSION}.docx"
    doc.save(path)
    print(f"已生成: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
