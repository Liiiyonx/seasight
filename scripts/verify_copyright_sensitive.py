"""软著材料提交前的敏感信息自检。

对生成的 docx 做两类检查：
  1. 正文文本里的口令 / 真实姓名
  2. **内嵌图片**里的敏感信息 —— 这一类最容易漏，因为文字层扫不到。

★ 为什么必须扫图片：审查报告 P0-4 就是登录页**截图**里明文显示
  4 组账号口令，而 docx 正文里根本没有这些字。只扫 text 会漏。

图片侧做不到 OCR（不引依赖），所以采用**内容哈希比对**：
把截图源文件与 docx 内嵌图逐一比哈希，命中即说明"这张图进了材料"。
再配合人工确认该图是否含敏感信息。

用法：
    python scripts/verify_copyright_sensitive.py <docx> [...]
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import sys
import zipfile

#: 正文里出现即视为敏感的字面量
FORBIDDEN = [
    "admin123456", "operator123456", "approver123456", "viewer123456",
    "刘乾松",# 审查报告 P1-2 指出的真实姓名
]

#: 内嵌图若命中这些源文件，说明"这张图进了材料"，需人工确认
IMAGE_SOURCES = [
    "artifacts/ui-responsive/1920x1080-login.png",
    "artifacts/ui-responsive/1920x1080-dashboard.png",
]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def scan_text(z: zipfile.ZipFile, label: str) -> list[str]:
    xml = z.read("word/document.xml").decode("utf-8", "replace")
    txt = re.sub(r"<[^>]+>", "", xml)
    return [k for k in FORBIDDEN if k in txt]


def scan_images(z: zipfile.ZipFile) -> list[str]:
    """返回「进了材料的敏感源图」列表。"""
    src_hashes = {}
    for rel in IMAGE_SOURCES:
        p = os.path.join(ROOT, rel)
        if os.path.isfile(p):
            src_hashes[hashlib.md5(open(p, "rb").read()).hexdigest()] = rel
    hits = []
    for name in z.namelist():
        if not name.startswith("word/media/"):
            continue
        h = hashlib.md5(z.read(name)).hexdigest()
        if h in src_hashes:
            hits.append("%s ← %s" % (name, src_hashes[h]))
    return hits


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    bad = 0
    for docx in sys.argv[1:]:
        print("=== %s ===" % os.path.basename(docx))
        z = zipfile.ZipFile(docx)
        hits = scan_text(z, docx)
        if hits:
            print("  ✗ 正文含敏感字面量：%s" % ", ".join(hits))
            bad += 1
        else:
            print("  ✓ 正文无口令 / 真实姓名")

        imgs = scan_images(z)
        n_media = len([n for n in z.namelist() if n.startswith("word/media/")])
        print("  · 内嵌图 %d 张，其中需人工确认的：%d 张" % (n_media, len(imgs)))
        for i in imgs:
            print("    %s" % i)
        z.close()
        print()
    if bad:
        print("✗ 有 %d 个文件未通过" % bad)
        return 1
    print("✓ 全部通过（图片侧仍需人工确认一次）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
