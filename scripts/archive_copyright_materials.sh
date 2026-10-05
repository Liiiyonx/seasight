#!/usr/bin/env bash
# 软著材料归档打包 —— 带内容清单与校验和。
#
# 为什么不用zip 直接了事：
#   软著材料是**要提交给版权局的正式产物**，出问题时需要能证明
#   「提交的那一版就是本地这一版」。所以包内带 SHA256 清单，
#   任何人解开都能核对是不是同一批文件。
#
# 用法：
#   bash scripts/archive_copyright_materials.sh
#   bash scripts/archive_copyright_materials.sh "<USER_HOME>\Desktop\Oceanus软著材料"
set -euo pipefail

SRC="${1:-<USER_HOME>/Desktop/Oceanus软著材料}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$SRC/archive-$STAMP"

if [ ! -d "$SRC" ]; then
  echo "找不到材料目录：$SRC" >&2
  exit 1
fi

mkdir -p "$DEST"

# 只打包要提交的材料，排除历史归档与审查过程文件
for f in \
  "源代码鉴别材料_探海灵眸海洋环境治理智能体软件V1.0.docx" \
  "软件说明书_探海灵眸海洋环境治理智能体软件V1.0.docx" \
  "源程序量统计.txt" \
  "软著申请指南.md"
do
  if [ -f "$SRC/$f" ]; then
    cp "$SRC/$f" "$DEST/"
    echo "  + $f"
  else
    echo "  ! 缺失：$f" >&2
  fi
done

# 内容清单：路径 + 大小 + SHA256。解开后可核对是否被改动。
cd "$DEST"
sha256sum *.docx *.txt *.md > SHA256SUMS.txt 2>/dev/null || true
{
  echo "# 软著材料归档清单"
  echo ""
  echo "打包时间：$(date -Iseconds)"
  echo "来源目录：$SRC"
  echo ""
  echo "| 文件 | 大小 |"
  echo "| --- | --- |"
  for f in *.docx *.txt *.md; do
    [ -f "$f" ] && echo "| $f | $(du -h "$f" | cut -f1) |"
  done
  echo ""
  echo "校验：\`sha256sum -c SHA256SUMS.txt\`"
} > MANIFEST.md

echo ""
echo "已归档到：$DEST"
ls -la "$DEST" | tail -n +2 | awk '{printf "  %-58s %s\n", $NF, $5}'
echo ""
echo "校验：cd \"$DEST\" && sha256sum -c SHA256SUMS.txt"
