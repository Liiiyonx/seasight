"""软著材料提交前的敏感信息自检。

对生成的 docx 做三类检查：
  1. 正文文本里的口令 / 真实姓名 / 现场 IP
  2. **内嵌图片的图片来源**（白名单，未登记者报警）
  3. 图片一枚一源：docx 里每张图都必须能在仓库里找到对应文件

★ 为什么必须扫图片：审查报告 P0-4 就是登录页**截图**里明文显示
  4 组账号口令，而 docx 正文里根本没有这些字。只扫 text 会漏。

★★ 2026-10-13 改白名单（原来错在黑名单）：
  旧版维护一张「敏感源图」黑名单，逐张比对哈希。这套逻辑**必然漏**——
  只有被想到、被登记过的图才会报警。实测就漏了一次：拆分登记第 4 篇新增的
  机械臂控制台截图印着「真机热点 192.168.149.1」与「登录账号 ubuntu / hiwonder」，
  因为没进黑名单，脚本报「√ 全部通过」，凭据就这样进了准备递交的文件。
  改成白名单后方向反过来：**任何一张没登记过来源的嵌入图都会报警**，
  新增截图默认被拦住，而不是默认放行。宁可多报，不可漏报。

图片侧仍不做 OCR（不引依赖）。白名单只能保证「图是审查过的那些」，
不能保证「审查过的图里没字」。所以新增图片时**必须人工看一眼**，
看完把来源目录登记进 REVIEWED_IMAGE_DIRS 或 REVIEWED_IMAGES。

用法：
    python scripts/verify_copyright_sensitive.py <docx> [...]
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import zipfile

#: 正文 / 图注里出现即视为敏感的字面量。
#: ★ 每发现一处新的现场凭据就往这里加一条 —— 这是唯一的人工维护点。
FORBIDDEN = [
    # 平台演示账号口令（主软著审查报告 P0-4）
    "admin123456", "operator123456", "approver123456", "viewer123456",
    "刘乾松",  # 审查报告 P1-2 指出的真实姓名
    # 机械臂篇控制台的现场接入信息（2026-10-13 实拍截图暴露）
    "192.168.149.1",   # 演示机热点地址
    "ubuntu / hiwonder",  # 真机默认登录账号
    "登录账号",          # 该行的行名本身：文档里出现它，等于在描述一个凭据栏
    "真机热点",
]

#: 正文里的通用敏感形态（不依赖具体字面量，避免「换个 IP 就漏」）
FORBIDDEN_PATTERNS: list[tuple[str, str]] = [
    # 私有网段 IP —— 公开登记文件里不该出现现场地址
    (r"\b(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b", "私有网段 IP"),
    # 明文口令赋值（password: "xxx"）
    (r'(?i)\bpassword\s*[:=]\s*["\'](?!CHANGE_ME|\*+)[^"\']{3,}["\']', "明文口令赋值"),
    # 常见令牌前缀
    (r"\b(?:sk-|ghp_|gho_|AKIA)[A-Za-z0-9]{12,}", "疑似 API 令牌"),
]

#: 已人工核对、确认无害的**具体字面量**例外。
#: ★ 只写具体值，不写通配 —— 换个 IP 必须重新报警。
#: ★ 每条都要写清「为什么无害」，否则下一个人无从判断能不能删。
ALLOWED_LITERALS: dict[str, str] = {
    "192.168.1.100":
        "edge/arm_bridge/config.yaml 里 http 驱动的**示例端点**占位，"
        "配套的是 dobot_example 这类厂商示例块，不是本站点地址；"
        "193.168.1.x 是家用路由最常见的默认网段，本身不指向任何真实主机。",
    "192.168.1.101":
        "同上，dobot_example 厂商示例块的占位端点。",
}


def _allowed(x: str) -> bool:
    return x.strip() in ALLOWED_LITERALS


#: 已审查过、可以出现在材料里的图片来源目录（仓库相对路径）。
#: 落在这些目录下的图视为已人工确认无敏感信息。
REVIEWED_IMAGE_DIRS = [
    "artifacts/ui-responsive/",      # 平台 Web 截图（登录页除外，见下）
    "artifacts/vision-preview/",     # 水面检测效果图
    "artifacts/copyright-figs/",     # 拆分登记各篇的裁切图
    "artifacts/arm-console/",        # 机械臂控制台截图（已按凭据行裁剪，见 shot_arm_console.mjs）
]

#: 目录白名单之外，仍需单独点名的例外
REVIEWED_IMAGES: list[str] = []

#: 明确**不得**进入材料的源图（与目录白名单取交集时的排除项）
BANNED_IMAGE_HINTS = [
    "1920x1080-login",  # 登录页截图曾明文显示 4 组演示口令
    "login",
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _hash(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.md5(fh.read()).hexdigest()


def build_allowed_hashes() -> dict[str, str]:
    """把白名单目录下的所有图建成 {md5: 仓库相对路径}。"""
    allowed: dict[str, str] = {}
    for rel_dir in REVIEWED_IMAGE_DIRS:
        abs_dir = os.path.join(ROOT, rel_dir)
        if not os.path.isdir(abs_dir):
            continue
        for name in os.listdir(abs_dir):
            rel = rel_dir + name
            if any(h in name.lower() for h in BANNED_IMAGE_HINTS):
                continue
            p = os.path.join(ROOT, rel)
            if os.path.isfile(p):
                allowed[_hash(p)] = rel
    for rel in REVIEWED_IMAGES:
        p = os.path.join(ROOT, rel)
        if os.path.isfile(p):
            allowed[_hash(p)] = rel
    return allowed


def scan_text(z: zipfile.ZipFile) -> list[str]:
    xml = z.read("word/document.xml").decode("utf-8", "replace")
    txt = re.sub(r"<[^>]+>", "", xml)
    hits = [k for k in FORBIDDEN if k in txt and not _allowed(k)]
    for pat, label in FORBIDDEN_PATTERNS:
        for m in re.findall(pat, txt):
            text = m if isinstance(m, str) else next((g for g in m if g), "")
            if _allowed(text):
                continue
            hits.append("%s（%s）" % (label, text))
    return hits


def scan_images(z: zipfile.ZipFile, allowed: dict[str, str]) -> tuple[list[str], list[str]]:
    """返回 (未知来源的图, 命中禁用源图)。"""
    unknown, banned = [], []
    for name in z.namelist():
        if not name.startswith("word/media/"):
            continue
        h = hashlib.md5(z.read(name)).hexdigest()
        if h in allowed:
            continue
        # 未登记：可能是新截图，也可能是被禁的登录页图
        for rel_dir in REVIEWED_IMAGE_DIRS:
            abs_dir = os.path.join(ROOT, rel_dir)
            if not os.path.isdir(abs_dir):
                continue
            for fname in os.listdir(abs_dir):
                if any(k in fname.lower() for k in BANNED_IMAGE_HINTS):
                    p = os.path.join(abs_dir, fname)
                    if os.path.isfile(p) and _hash(p) == h:
                        banned.append("%s ← %s%s" % (name, rel_dir, fname))
                        break
        else:
            unknown.append(name)
    return unknown, banned


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    allowed = build_allowed_hashes()
    print("已登记来源的图 %d 张（白名单）" % len(allowed))
    print()
    bad = 0
    for docx in sys.argv[1:]:
        print("=== %s ===" % os.path.basename(docx))
        z = zipfile.ZipFile(docx)
        hits = scan_text(z)
        if hits:
            print("  ✗ 正文含敏感内容：%s" % "；".join(hits))
            bad += 1
        else:
            print("  ✓ 正文无口令 / 真实姓名 / 现场 IP")

        unknown, banned = scan_images(z, allowed)
        n_media = len([n for n in z.namelist() if n.startswith("word/media/")])
        print("  · 内嵌图 %d 张" % n_media)
        if banned:
            print("  ✗ 含禁用源图 %d 张：" % len(banned))
            for b in banned:
                print("    %s" % b)
            bad += 1
        if unknown:
            print("  ✗ 来源未登记的图 %d 张（白名单模型下默认拦截，需人工看过后登记）："
                  % len(unknown))
            for u in unknown:
                print("    %s" % u)
            bad += 1
        if not unknown and not banned:
            print("  ✓ 内嵌图全部可追溯至已审查来源")
        z.close()
        print()
    if bad:
        print("✗ 有 %d 项未通过" % bad)
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
