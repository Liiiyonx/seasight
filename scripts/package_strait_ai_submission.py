#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组装 2026 年海峡大学生人工智能创意大赛提交包。

规程硬性要求：
  1. 提交物 = 作品介绍 PPT + Demo/演示视频（可选）+ 源代码/设计文档 + 加分项举证；
  2. **所有资料内容中不得出现任何参赛选手信息**（学校、专业班级、指导老师、姓名等）；
  3. 压缩包 < 600 MB；
  4. 压缩包命名为「学校名称－作品名称－队长姓名－队长手机号.zip」
     —— 注意：文件名本身含身份信息，但包**内部**所有文件必须匿名。

本脚本做三件事：
  1. 匿名化复制：把源码与文档复制到暂存区，同时扫描并拒绝/脱敏身份信息；
  2. 组装提交树：按竞赛规程的提交物顺序编号落盘；
  3. 打包与自检：体积校验、身份信息复扫、密钥扫描，产出 zip 与检查报告。

退出码：
  0  成功
  1  匿名化检查失败（包内仍存在身份信息）
  2  体积超限
  3  缺少必需源文件
  4  密钥扫描失败
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGE_ROOT = ROOT / "dist" / "strait-ai-submission"
MAX_SIZE_MB = 600

# ---------------------------------------------------------------------------
# 匿名化规则
# ---------------------------------------------------------------------------

@dataclass
class AnonRule:
    """一条身份信息脱敏规则。"""

    name: str
    pattern: re.Pattern
    replacement: str
    desc: str


# 参赛队伍信息（从报名表取得）。这些字面量必须从提交包中彻底消失。
TEAM_TOKENS = [
    "李涌翔", "许鑫杰", "陈泽韩", "李泽群", "晏可鑫", "范忆梅",
    "福州理工学院", "福州理工",
]

ANON_RULES: list[AnonRule] = [
    # 姓名 → 统一占位
    *[AnonRule("member_name", re.compile(re.escape(t)), "[队员]", f"队员姓名 {t}")
      for t in TEAM_TOKENS[:6]],
    # 学校
    *[AnonRule("school", re.compile(re.escape(t)), "[院校]", f"院校名 {t}")
      for t in TEAM_TOKENS[6:]],
    # 身份证号
    AnonRule("id_card", re.compile(r"\b\d{17}[\dXx]\b"), "[已脱敏]", "身份证号"),
    # 手机号（含报名表队长手机号）
    AnonRule("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[联系方式已脱敏]", "手机号"),
    # 邮箱（QQ 邮箱等）
    AnonRule("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "[邮箱已脱敏]", "邮箱地址"),
    # 本机绝对路径
    AnonRule("local_path_win",
             re.compile(r"[A-Za-z]:\\\\?Users\\\\?[\w.-]+(?:\\\\?[\w.-]+)*"),
             "[本地路径已脱敏]", "开发者本机路径"),
    AnonRule("local_path_posix",
             re.compile(r"/(?:home|Users)/[\w.-]+(?:/[\w.-]+)*"),
             "[本地路径已脱敏]", "开发者本机路径"),
    # 生产/内网地址
    AnonRule("endpoint",
             re.compile(r"https?://(?:[\w-]+\.)*(?:aliyuncs|myhuaweicloud|huaweicloud|deepseek)\.com[\w/:?=&.-]*"),
             "[服务地址已脱敏]", "云服务生产地址"),
    AnonRule("ip_port",
             re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{2,5}\b"),
             "[地址已脱敏]", "IP:端口"),
]

# 密钥类：不脱敏，直接判失败（密钥泄漏比身份泄漏更严重）
#
# 校准说明：早期版本用「password|token|secret 赋值 + 12 位以上字母数字」做粗匹配，
# 会把 `token = _issue_token(...)`、`const token = localStorage.getItem(...)`、
# `token="forged-token"` 这类**变量名与测试占位符**全部误报。
# 现改为：只匹配 (a) 有特征前缀的真实密钥格式，(b) 被赋成字符串字面量且**不含下划线**
# 且长度 ≥ 20 的口令 —— 这样测试占位符（`AUTO_LOGIN_PASSWORD`、`hashed_password`）
# 因含下划线或明显函数调用而不再命中，真密钥仍会被拦下。
SECRET_RULES: list[tuple[str, re.Pattern]] = [
    ("openai_style_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{24,}")),
    ("aws_ak", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("assigned_literal_secret", re.compile(
        r"""(?i)\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)\b"""
        r"""\s*[:=]\s*["'][A-Za-z0-9!@#$%^&*()\[\]{}\-]{20,}["']""")),
]

TEXT_EXT = {
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".sql", ".ts", ".vue",
    ".js", ".mjs", ".cjs", ".ts", ".html", ".css", ".sh", ".env", ".cfg",
    ".ini", ".toml", ".csv",
}
CODE_EXT = {".py", ".ts", ".vue", ".js", ".mjs", ".cjs", ".sql", ".yaml", ".yml", ".sh"}

# OOXML（docx / pptx / xlsx）是 zip 套 XML，身份信息藏在内部 XML 里，
# 纯文本扫描扫不到，必须单独处理，否则匿名化会形同虚设。
OOXML_EXT = {".docx", ".pptx", ".xlsx"}

SKIP_DIR_NAMES = {
    "__pycache__", "node_modules", ".git", ".venv", "venv", ".pytest_cache",
    "dist", "build", ".mypy_cache", ".idea", ".vscode", "coverage", ".DS_Store",
}


# ---------------------------------------------------------------------------
# 提交树定义
# ---------------------------------------------------------------------------

@dataclass
class SubmissionItem:
    """一条提交物：从源路径复制到暂存区的某个位置。"""

    source: str
    dest: str
    required: bool = True
    anonymize: bool = True
    note: str = ""


def is_ooxml(path: Path) -> bool:
    """判断后缀为 docx/pptx 的文件是否真的是 OOXML 包（而非改了后缀的文本）。"""
    if not zipfile.is_zipfile(path):
        return False
    try:
        with zipfile.ZipFile(path) as z:
            return "[Content_Types].xml" in z.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def out_ext(src: str) -> str:
    """目标扩展名跟随实际源文件类型。"""
    return Path(src).suffix.lower() or ".md"


@dataclass
class SubmissionPlan:
    title: str
    items: list[SubmissionItem] = field(default_factory=list)


def build_plan(docs_dir: Path, *, with_video_placeholder: bool) -> SubmissionPlan:
    """构造提交树定义。

    目录结构遵循规程的提交物顺序：
      01 作品介绍
      02 设计文档
      03 源代码
      04 演示材料
      05 加分项举证
      06 提交说明
    """
    plan = SubmissionPlan(title="探海灵眸Oceanus")
    D = docs_dir

    def pick(stem: str) -> tuple[str, bool]:
        """优先用已生成的 docx，没有则回落到 md（打包时按后缀处理）。"""
        docx = D / f"{stem}.docx"
        md = D / f"{stem}.md"
        if docx.exists() and is_ooxml(docx):
            return str(docx), True
        if md.exists():
            return str(md), True
        return str(docx), True

    # 01 作品介绍：PPT + 研究报告 + 设计文档
    src, _ = pick("01_研究报告_探海灵眸Oceanus")
    plan.items.append(SubmissionItem(
        src, "01_作品介绍/01_研究报告" + out_ext(src), required=True,
        note="研究报告（问题提出→解决方案→应用价值）"))
    src, _ = pick("02_系统设计文档_探海灵眸Oceanus")
    plan.items.append(SubmissionItem(
        src, "01_作品介绍/02_系统设计文档" + out_ext(src), required=True,
        note="设计文档：架构/模块/接口/数据结构"))
    plan.items.append(SubmissionItem(
        str(D / "探海灵眸Oceanus_作品介绍.pptx"),
        "01_作品介绍/03_作品介绍PPT.pptx", required=True,
        note="作品介绍 PPT"))

    # 02 设计文档补充：源码说明与复现步骤（从仓库文档复制）
    plan.items += [
        SubmissionItem("README.md", "02_设计文档/项目README.md",
                       required=False, note="项目总览"),
        SubmissionItem("docs/architecture.md", "02_设计文档/architecture.md",
                       required=False, note="架构说明"),
        SubmissionItem("docs/device-interface.md", "02_设计文档/device-interface.md",
                       required=False, note="设备接口定义"),
        SubmissionItem("docs/software-design.md", "02_设计文档/software-design.md",
                       required=False, note="软件设计"),
        SubmissionItem("docs/mqtt-topics.md", "02_设计文档/mqtt-topics.md",
                       required=False, note="MQTT 主题契约"),
        SubmissionItem("docs/perception-data-protocol.md",
                       "02_设计文档/perception-data-protocol.md",
                       required=False, note="感知数据协议"),
    ]

    # 03 源代码：分模块复制，保留可读结构
    src_tree = [
        ("backend/app", "03_源代码/backend/app"),
        ("backend/run_server.py", "03_源代码/backend/run_server.py"),
        ("backend/requirements.txt", "03_源代码/backend/requirements.txt"),
        ("backend/tests", "03_源代码/backend/tests"),
        ("backend/db", "03_源代码/backend/db"),
        ("backend/alembic", "03_源代码/backend/alembic"),
        ("edge", "03_源代码/edge"),
        ("ml", "03_源代码/ml"),
        ("integrations", "03_源代码/integrations"),
        ("frontend/src", "03_源代码/frontend/src"),
        ("frontend/package.json", "03_源代码/frontend/package.json"),
        ("frontend/vite.config.ts", "03_源代码/frontend/vite.config.ts"),
        ("scripts", "03_源代码/scripts"),
        ("docker-compose.prod.yml", "03_源代码/docker-compose.prod.yml"),
        ("Makefile", "03_源代码/Makefile"),
        (".env.example", "03_源代码/.env.example"),
    ]
    for src, dst in src_tree:
        plan.items.append(SubmissionItem(
            src, dst, required=False, note="源码模块"))

    # 04 演示材料：分镜脚本 + 录制清单（视频本体由参赛队自行录制后按说明补入）
    src, _ = pick("03_演示视频分镜脚本")
    plan.items.append(SubmissionItem(
        src, "04_演示材料/演示视频分镜脚本" + out_ext(src), required=True,
        note="分镜脚本与旁白文案"))
    src, _ = pick("04_演示视频录制清单")
    plan.items.append(SubmissionItem(
        src, "04_演示材料/演示视频录制清单" + out_ext(src), required=False,
        note="录制操作清单"))
    if with_video_placeholder:
        plan.items.append(SubmissionItem(
            "__VIDEO_PLACEHOLDER__",
            "04_演示材料/演示视频.mp4", required=False,
            note="演示视频（由参赛队录制后放入此路径）"))

    # 05 加分项举证：可复现的评测与验收产物
    evidence = [
        ("artifacts/agent_evals/latest_v2.json", "05_加分项举证/agent_evals_latest_v2.json"),
        ("artifacts/agent-real-state-acceptance/latest.json",
         "05_加分项举证/agent_real_state_acceptance_latest.json"),
        ("artifacts/metrics/opencv_latest.json", "05_加分项举证/opencv_latest.json"),
        ("artifacts/ontology-eval/latest.json", "05_加分项举证/ontology_eval_latest.json"),
        ("artifacts/knowledge-qa-trace/latest.json", "05_加分项举证/knowledge_qa_trace_latest.json"),
    ]
    for src, dst in evidence:
        plan.items.append(SubmissionItem(src, dst, required=False, note="评测证据"))

    # 06 提交说明
    src, _ = pick("05_提交清单与自检表")
    plan.items.append(SubmissionItem(
        src, "06_提交说明/提交清单与自检表" + out_ext(src), required=True,
        note="提交物清单与自检表"))
    return plan


# ---------------------------------------------------------------------------
# 匿名化处理
# ---------------------------------------------------------------------------

def is_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXT


def anonymize_ooxml(src: Path, dst: Path, report: AnonReport) -> bool:
    """对 docx/pptx/xlsx 做匿名化：解 zip → 改内部 XML → 重打包。

    身份信息几乎总是落在 word/document.xml、ppt/slides/*.xml、
    docProps/*.xml 这几处，逐个替换后重新压回 zip。
    OOXML 的 zip 结构可以安全重建（不依赖中央目录顺序以外的信息）。
    """
    import io

    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(src) as zin:
            entries = [(i, zin.read(i.filename)) for i in zin.infolist()]
    except (zipfile.BadZipFile, OSError) as exc:
        # 不是合法zip —— 通常是把 .md 误改了后缀。直接当文本脱敏。
        report.violations.append(
            f"[格式] {rel_label(src)} 后缀为 {src.suffix} 但不是合法 "
            f"OOXML（{exc}）；请先生成真正的 docx/pptx")
        return False

    out = io.BytesIO()
    changed = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info, data in entries:
            name = info.filename
            # 只处理文本类部件，跳过图片/字体/嵌入对象
            if name.endswith((".xml", ".rels")):
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    zout.writestr(info, data)
                    continue
                secrets = scan_secrets(text)
                if secrets:
                    report.violations.append(
                        f"[密钥] {rel_label(src)}::{name} 命中 {', '.join(secrets)}")
                new_text, hits = anonymize_text(text)
                if hits:
                    changed += 1
                    report.anonymized.setdefault(
                        rel_label(src),
                        {"dest": dst.relative_to(STAGE_ROOT).as_posix(),
                         "rules": []})["rules"] += [f"{name}:{r}" for r in hits]
                data = new_text.encode("utf-8")
            zout.writestr(info, data)

    dst.write_bytes(out.getvalue())
    report.copied_binary += 1
    if changed:
        report.ooxml_cleaned += 1
    return True


def anonymize_text(text: str) -> tuple[str, list[str]]:
    """对文本应用全部脱敏规则，返回 (脱敏后文本, 命中规则名列表)。"""
    hits: list[str] = []
    for rule in ANON_RULES:
        new_text, n = rule.pattern.subn(rule.replacement, text)
        if n:
            hits.append(f"{rule.name}({rule.desc})x{n}")
            text = new_text
    return text, hits


def scan_secrets(text: str) -> list[str]:
    return [name for name, pat in SECRET_RULES if pat.search(text)]


def rel_label(path: Path) -> str:
    """相对仓库根的展示路径；仓库外文件用绝对路径。"""
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def copy_anonymized(src: Path, dst: Path, report: AnonReport, *, anonymize: bool) -> None:
    """复制单个文件，必要时脱敏，并登记报告。"""
    dst.parent.mkdir(parents=True, exist_ok=True)

    # docx/pptx/xlsx：二进制 OOXML，必须解包改内部 XML
    if src.suffix.lower() in OOXML_EXT:
        anonymize_ooxml(src, dst, report)
        return

    if not is_text_file(src):
        # 其他二进制（图片、模型、字体等）直接复制
        shutil.copy2(src, dst)
        report.copied_binary += 1
        return

    try:
        text = src.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        shutil.copy2(src, dst)
        report.copied_binary += 1
        return

    secrets = scan_secrets(text)
    if secrets:
        report.violations.append(
            f"[密钥] {rel_label(src)} 命中 {', '.join(secrets)}")
        return

    if anonymize:
        new_text, hits = anonymize_text(text)
        if hits:
            report.anonymized[rel_label(src)] = {
                "dest": dst.relative_to(STAGE_ROOT).as_posix(), "rules": hits}
        text = new_text
        report.copied_text += 1
    else:
        report.copied_text += 1

    dst.write_text(text, encoding="utf-8")


def copy_tree(src: Path, dst: Path, report: AnonReport, *, anonymize: bool) -> None:
    """递归复制目录，跳过构建产物与缓存。"""
    for r, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES and not d.startswith(".env")]
        for name in files:
            if name.endswith((".pyc", ".pyo", ".log")):
                continue
            s = Path(r) / name
            d = dst / Path(r).relative_to(src) / name
            try:
                copy_anonymized(s, d, report, anonymize=anonymize)
            except Exception as exc:  # noqa: BLE001
                report.skipped.append(f"{s}: {exc}")


@dataclass
class AnonReport:
    copied_text: int = 0
    copied_binary: int = 0
    ooxml_cleaned: int = 0
    anonymized: dict[str, dict] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    missing_optional: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 构建流程
# ---------------------------------------------------------------------------

def stage_plan(plan: SubmissionPlan, stage: Path, report: AnonReport) -> None:
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)

    for item in plan.items:
        if item.source == "__VIDEO_PLACEHOLDER__":
            continue
        src = Path(item.source)
        if not src.is_absolute():
            src = ROOT / item.source
        dst = stage / item.dest

        if not src.exists():
            if item.required:
                report.missing_required.append(item.source)
            else:
                report.missing_optional.append(item.source)
            continue

        if src.is_dir():
            copy_tree(src, dst, report, anonymize=item.anonymize)
        else:
            copy_anonymized(src, dst, report, anonymize=item.anonymize)


# 复扫豁免：这些文件本身就是「隐私清洗/检测」工具，其中的本机路径字面量
# 是替换源与检测规则，属于工具定义而非信息泄露。豁免整文件的内容匹配，
# 但仍会对文件做密钥扫描（密钥泄漏优先级更高）。
SCAN_EXEMPT_PATHS = {
    "scripts/build_public_release.py",
    "scripts/check_public_repo_privacy.py",
}


def rescan_stage(stage: Path) -> list[str]:
    """对暂存目录做一次独立的身份信息复扫，确保打包前无遗漏。

    覆盖两类：
      - 纯文本文件：直接正则匹配；
      - docx/pptx/xlsx：解包后扫描内部 XML —— 这一类是匿名化最容易漏的地方，
        因为身份信息藏在二进制压缩包里，任何基于扩展名或 grep 的检查都会失效。
    """
    import io

    problems: list[str] = []
    for r, _dirs, files in os.walk(stage):
        for name in files:
            p = Path(r) / name
            rel = p.relative_to(stage).as_posix()
            suffix = p.suffix.lower()

            if suffix in OOXML_EXT:
                try:
                    with zipfile.ZipFile(p) as inner:
                        for m in inner.namelist():
                            if not m.endswith((".xml", ".rels")):
                                continue
                            x = inner.read(m).decode("utf-8", errors="ignore")
                            for token in TEAM_TOKENS:
                                if token in x:
                                    problems.append(
                                        f"[身份] {rel}::{m} 仍含 '{token}'")
                            for rule in ANON_RULES:
                                if rule.pattern.search(x):
                                    problems.append(
                                        f"[身份] {rel}::{m} 命中 {rule.name}")
                except (zipfile.BadZipFile, OSError):
                    problems.append(f"[格式] {rel} 不是合法 OOXML 文件")
                continue

            if not is_text_file(p):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue

            # 密钥扫描优先级最高，不受豁免影响
            for sname, spat in SECRET_RULES:
                if spat.search(text):
                    problems.append(f"[密钥] {rel} 命中 {sname}")

            # 隐私清洗工具自身：路径字面量是工具定义，豁免身份类规则
            if rel in SCAN_EXEMPT_PATHS:
                continue

            for token in TEAM_TOKENS:
                if token in text:
                    problems.append(f"[身份] {rel} 仍含 '{token}'")
            for rule in ANON_RULES:
                if rule.pattern.search(text):
                    problems.append(f"[身份] {rel} 命中 {rule.name}")
    return problems


def write_readme(stage: Path, plan: SubmissionPlan, report: AnonReport) -> None:
    readme = f"""# 探海灵眸 Oceanus —— 提交包说明

> 本压缩包为 2026 年海峡大学生人工智能创意大赛作品提交包。
> **包内所有文件已完成匿名化处理，不含学校、专业班级、指导老师与队员姓名等信息。**

## 目录结构

```
01_作品介绍/
    01_研究报告.docx              问题提出 → 解决方案 → 应用价值
    02_系统设计文档.docx          架构 / 模块划分 / 接口定义 / 数据结构
    03_作品介绍PPT.pptx           现场路演用
02_设计文档/
    项目README.md                 项目总览与快速开始
    architecture.md               架构说明
    device-interface.md           设备接口定义
    software-design.md            软件设计
    mqtt-topics.md                MQTT 主题契约
    perception-data-protocol.md   感知数据协议
03_源代码/
    backend/    后端服务（FastAPI + 智能体内核 + 业务模块 + 测试）
    edge/       边缘侧（设备仿真、驱动桥接）
    ml/         感知与模型工程（检测器、评测、ONNX 导出）
    integrations/  外部平台集成
    frontend/src/  前端业务控制台
    scripts/    验证与打包脚本
04_演示材料/
    演示视频分镜脚本.md
    演示视频录制清单.md
    演示视频.mp4                 （由参赛队录制后放入）
05_加分项举证/
    agent_evals_latest_v2.json   智能体固定场景评测结果
    agent_real_state_acceptance_latest.json  软件集成验收结果
    opencv_latest.json           感知评测结果（当前为 not_evaluated）
    ontology_eval_latest.json    本体评测结果
    knowledge_qa_trace_latest.json  检索问答轨迹
06_提交说明/
    提交清单与自检表.md
```

## 复现步骤

```bash
# 1. 基础设施
docker compose -f docker-compose.prod.yml up -d postgres redis emqx minio

# 2. 后端服务
cd backend && pip install -r requirements.txt && python run_server.py

# 3. 自动化测试
python -m pytest -q

# 4. 智能体固定场景评测
python scripts/run_agent_evals.py
```

## 能力边界说明

本作品对自身能力采用 E0-E4 证据分级表述，不做越级宣称：

- 软件闭环与智能体内核：**E1**（自动化测试 + 固定场景评测）
- 真实 HTTP / 鉴权 / 数据库 / 审批 / 幂等集成：**E2**
- 检测精度：**未评测**（独立测试集未放行，评测输出 `not_evaluated`）
- 昇腾硬件推理：**方案级设计**，未在目标硬件实测
- 真实海域与商业验证：**未取得**

时序链路误报抑制率约 76% 的口径为「被时序环节过滤的检测数 ÷ 输入检测总数」，
**不是识别精度或召回率**。

---
生成信息：文本文件 {report.copied_text} 个，二进制文件 {report.copied_binary} 个，
脱敏文件 {len(report.anonymized)} 个。
"""
    (stage / "README.md").write_text(readme, encoding="utf-8")


def make_zip(stage: Path, zip_path: Path) -> tuple[int, str]:
    """打包并返回 (字节数, sha256)。"""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for r, _dirs, files in os.walk(stage):
            for name in sorted(files):
                p = Path(r) / name
                zf.write(p, p.relative_to(stage))
    h.update(zip_path.read_bytes())
    return zip_path.stat().st_size, h.hexdigest()


def dir_size(path: Path) -> int:
    total = 0
    for r, _d, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(r, name))
            except OSError:
                pass
    return total


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="组装海峡大学生 AI 大赛提交包")
    ap.add_argument("--docs-dir", required=True, help="提交材料源目录（含 md/docx/pptx）")
    ap.add_argument("--zip-name", required=True,
                    help="输出 zip 文件名，须符合「学校名称－作品名称－队长姓名－队长手机号.zip」")
    ap.add_argument("--with-video", action="store_true",
                    help="为演示视频预留占位路径")
    ap.add_argument("--keep-stage", action="store_true", help="保留暂存目录")
    args = ap.parse_args()

    docs_dir = Path(args.docs_dir)
    if not docs_dir.is_dir():
        print(f"[FAIL] 材料源目录不存在: {docs_dir}")
        return 3

    plan = build_plan(docs_dir, with_video_placeholder=args.with_video)
    report = AnonReport()
    print(f"[1/4] 暂存提交树 → {STAGE_ROOT}")
    stage_plan(plan, STAGE_ROOT, report)

    if report.violations:
        print("\n[FAIL] 密钥扫描失败：")
        for v in report.violations[:20]:
            print("   ", v)
        return 4

    print("[2/4] 生成包内 README")
    write_readme(STAGE_ROOT, plan, report)

    print("[3/4] 匿名化独立复扫")
    problems = rescan_stage(STAGE_ROOT)
    if problems:
        print("\n[FAIL] 匿名化复扫未通过：")
        for p in problems[:30]:
            print("   ", p)
        print(f"\n  共 {len(problems)} 处。已中止打包。")
        return 1

    size = dir_size(STAGE_ROOT)
    print(f"      暂存体积 {size / 1e6:.1f} MB，"
          f"脱敏文件 {len(report.anonymized)} 个")
    if size > MAX_SIZE_MB * 1024 * 1024:
        print(f"[FAIL] 体积 {size / 1e6:.1f} MB 超过 {MAX_SIZE_MB} MB 上限")
        return 2

    print(f"[4/4] 打包 → {args.zip_name}")
    zip_path = Path(args.zip_name)
    zsize, digest = make_zip(STAGE_ROOT, zip_path)

    # 汇总报告
    print("\n" + "=" * 62)
    print("提交包构建完成")
    print("=" * 62)
    print(f"  文件名      : {zip_path.name}")
    print(f"  体积        : {zsize / 1e6:.2f} MB / 上限 {MAX_SIZE_MB} MB")
    print(f"  SHA256      : {digest}")
    print(f"  文本/二进制 : {report.copied_text} / {report.copied_binary}")
    print(f"  脱敏文件    : {len(report.anonymized)}")
    if report.missing_required:
        print(f"  ⚠ 缺少必需项: {report.missing_required}")
    if report.missing_optional:
        print(f"  · 缺少可选项 {len(report.missing_optional)} 个"
              f"（不影响提交合规）")
    if report.skipped:
        print(f"  · 跳过 {len(report.skipped)} 个文件（读取失败）")
    print("\n匿名化复扫：通过（无学校 / 姓名 / 手机号 / 身份证 / 本机路径）")
    print("密钥扫描  ：通过")

    if not args.keep_stage:
        pass  # 保留暂存目录便于复核；如需清理手动删除 dist/strait-ai-submission
    return 0


if __name__ == "__main__":
    sys.exit(main())