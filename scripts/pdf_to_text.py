"""把厂商 PDF 教程提取成文本，便于检索与精读。

用法：
    python scripts/pdf_to_text.py <pdf 路径> [输出 txt 路径]
    python scripts/pdf_to_text.py --batch <目录>     # 批量转换整个目录

产出统一放artifacts/vendor-docs/<相对路径>.txt，
不改动原始 PDF。
"""
from __future__ import annotations

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_ROOT = os.path.join(ROOT, 'artifacts', 'vendor-docs')


def extract(path: str) -> str:
    import fitz  # PyMuPDF

    doc = fitz.open(path)
    parts = []
    for i, page in enumerate(doc, 1):
        parts.append('\n===== 第 %d 页 =====' % i)
        parts.append(page.get_text())
    doc.close()
    return '\n'.join(parts)


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2

    if args[0] == '--batch':
        src_dir = args[1]
        n = 0
        for root, _dirs, files in os.walk(src_dir):
            for f in files:
                if not f.lower().endswith('.pdf'):
                    continue
                src = os.path.join(root, f)
                rel = os.path.relpath(src, src_dir)
                dst = os.path.join(OUT_ROOT, os.path.splitext(rel)[0] + '.txt')
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                try:
                    text = extract(src)
                    io.open(dst, 'w', encoding='utf-8').write(text)
                    print('%-60s -> %6d 字' % (rel[:58], len(text)))
                    n += 1
                except Exception as exc:  # noqa: BLE001
                    print('FAIL %s: %s' % (rel, exc))
        print('\n共 %d 个 PDF 已转换' % n)
        return 0

    src = args[0]
    text = extract(src)
    if len(args) > 1:
        dst = args[1]
    else:
        os.makedirs(OUT_ROOT, exist_ok=True)
        dst = os.path.join(OUT_ROOT, os.path.splitext(os.path.basename(src))[0] + '.txt')
    io.open(dst, 'w', encoding='utf-8').write(text)
    print('%s -> %s（%d 字）' % (src, dst, len(text)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
