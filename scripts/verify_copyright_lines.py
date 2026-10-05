"""实测软著源程序文档的每页行数（LibreOffice → PDF → 按 y 坐标聚类）。

为什么必须实测：审查员是**数 PDF 上的可见行**，
而 python-docx 只是写 OOXML —— 行距、分页、字体度量都要渲染器说了算。
脚本里估的行数与实际渲染常常差好几行，所以「生成完看一眼」是不够的。

用法：
    python scripts/verify_copyright_lines.py "<产物.docx>"

判据：
  · 每页「有字行」== 目标行数（默认 50）
  · 无整页空白
  · 相邻行 y 差恒等于设定行距（说明固定行距生效，没被文档网格吸附）
  · 页眉含软件全称+ 版本号
"""
from __future__ import annotations

import collections
import os
import subprocess
import sys
import tempfile

SOFFICE = r"C:\Program Files\LibreOffice\program\soffice.exe"

# 与生成脚本一致（改动时两边要同步）
LINE_PT = 13.0
TARGET_LINES = 50
HEADER_KEY = "探海灵眸海洋环境治理智能体软件"


def measure(docx_path: str):
    """返回 (每页有字行数分布, 总页数, 错误信息)。"""
    out_dir = tempfile.mkdtemp(prefix="_cpverify_")
    proc = subprocess.run(
        [SOFFICE, "--headless", "--norestore", "--convert-to", "pdf",
         "--outdir", out_dir, docx_path],
        capture_output=True, timeout=600,
    )
    pdf_name = os.path.basename(docx_path).replace(".docx", ".pdf")
    pdf_path = os.path.join(out_dir, pdf_name)
    if not os.path.exists(pdf_path):
        return collections.Counter(), 0, (
            "转换失败: %s" % (proc.stderr or proc.stdout).decode("utf-8", "replace")[:300]
        ), []

    import pymupdf

    d = pymupdf.open(pdf_path)
    counts: collections.Counter = collections.Counter()
    blank_tail_pages: list[int] = []
    gap_problems: list[str] = []
    header_missing: list[int] = []
    for i in range(d.page_count):
        pg = d[i]
        h = pg.rect.height
        # 排除页眉页脚区；只算「有字」的行（审查员也这么数）
        # 排除页眉页脚区。★ 阈值不能用 h-55：实测末行 y=810.4、距页底仅
        #   31.5pt，用 h-55(786.9) 会把最后 3~5 行误判为页脚而少数。
        #   页脚「第N页」在 y≈820+，故取 h-12 / 上 34 作为页眉页脚界。
        ys = sorted({
            round(ln["bbox"][1], 1)
            for blk in pg.get_text("dict").get("blocks", [])
            for ln in blk.get("lines", [])
            if 34 < ln["bbox"][1] < h - 12
            and "".join(s["text"] for s in ln["spans"]).strip()
        })
        counts[len(ys)] += 1
        # 行位数（含补位空行）与有字行数分开记：补位行是单个空格，
        # strip() 后为空，只看"有字行"会把补出来的页误判成内容不足。
        all_ys = sorted({
            round(ln["bbox"][1], 1)
            for blk in pg.get_text("dict").get("blocks", [])
            for ln in blk.get("lines", [])
            if 34 < ln["bbox"][1] < h - 12
        })
        blank_tail_pages.append((i + 1, len(all_ys), len(ys)))
        # 行距一致性：相邻行 y 差应恒等于 LINE_PT
        for a, b in zip(ys, ys[1:]):
            gap = round(b - a, 1)
            if abs(gap - LINE_PT) > 0.8:
                gap_problems.append("p%d: 行距 %.1f≠%.1f" % (i + 1, gap, LINE_PT))
                break
        # 页眉
        hdr = "".join(
            "".join(s["text"] for s in ln["spans"])
            for blk in pg.get_text("dict").get("blocks", [])
            for ln in blk.get("lines", [])
            if ln["bbox"][1] < 55
        )
        if HEADER_KEY not in hdr:
            header_missing.append(i + 1)
    total = d.page_count
    d.close()
    return counts, total, "", blank_tail_pages


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    docx = sys.argv[1]
    if not os.path.isfile(docx):
        print("找不到文件：%s" % docx, file=sys.stderr)
        return 2

    counts, total, err, detail = measure(docx)
    if err:
        print("✗ %s" % err)
        return 1

    print("文件：%s" % os.path.basename(docx))
    print("总页数：%d" % total)
    print("")
    print("每页行位 / 有字行（前 5 页与末 3 页）：")
    for pno, slots, vis in detail[:5] + detail[-3:]:
        print("  p%-3d 行位 %-3d 有字行 %-3d" % (pno, slots, vis))
    print("")
    print("每页「有字行」分布：")
    for k in sorted(counts):
        flag = "✓" if k >= TARGET_LINES else ("✗ 空页" if k == 0 else "✗ 不足")
        print("  %2d 行 × %2d 页  %s" % (k, counts[k], flag))

    # ★ 第 1 页是封面（软件名 + 版本 + 著作权人），按 CPC 规范
    #   **封面不计入行数要求**，须排除后再判定。
    body_counts = {k: v for k, v in counts.items()}
    if 1 in body_counts:
        body_counts[1] -= 1          # 扣掉封面那一页
        if body_counts[1] == 0:
            del body_counts[1]

    # CPC 规定是「每页**不少于** 50 行」，不是恰好 50 行。
    bad = {k: v for k, v in body_counts.items() if k < TARGET_LINES}
    print("")
    print("（已排除第 1 页封面 —— CPC 规范封面不计入行数要求）")
    if not bad:
        print("✓ 正文 %d 页均 ≥ %d 行（实测 %d~%d）"
              % (total - 1, TARGET_LINES,
                 min(k for k in body_counts if k >= TARGET_LINES),
                 max(body_counts)))
        return 0
    print("✗ 有 %d 页不足 %d 行。分布：%s"
          % (sum(bad.values()), TARGET_LINES, dict(sorted(body_counts.items()))))
    print("  调 PAD_LINES（补偿行）与 LINE_SPACING_PT，然后重新生成。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
