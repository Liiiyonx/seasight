#!/usr/bin/env python3
"""Extract a reproducible public open-data corpus for track-3 evaluation.

The corpus is built only from publicly accessible pages published by the
Ministry of Ecology and Environment of the People's Republic of China
(mee.gov.cn). The pages are standard announcements/notices about marine
environmental standards and approval processes.

Level and boundaries:
- The corpus is public open data, NOT a real de-identified industry dataset.
- The download/access date and exact source URL are recorded in a manifest.
- The extracted text is normalized for the deterministic ontology evaluation;
  normalization is not a claim of legal or editorial completeness.

Writes:
    artifacts/open-data-eval/corpus/manifest.json
    artifacts/open-data-eval/corpus/<article-id>.txt
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = ROOT / "artifacts" / "open-data-eval" / "corpus"
MANIFEST = CORPUS_DIR / "manifest.json"

SOURCE_PAGES: list[dict[str, str]] = [
    {
        "file": "2025-海洋环境标准征求意见通知.html",
        "title": "关于公开征求国家生态环境标准《生态环境损害鉴定评估技术指南 环境要素 第3部分：海洋（征求意见稿）》意见的通知",
        "source_url": "https://www.mee.gov.cn/xxgk2018/xxgk/xxgk06/202506/t20250620_1121662.html",
        "publisher": "中华人民共和国生态环境部",
        "asset_id": "OD-SEA-STD-2025-01",
        "asset_type": "document",
        "standard_codes": ["marine-environment-standard", "open-data"],
    },
    {
        "file": "t20260324_1147645.html",
        "title": "关于发布国家生态环境标准《海洋倾倒在线监控技术规范》的公告",
        "source_url": "https://www.mee.gov.cn/xxgk2018/xxgk/xxgk01/202603/t20260324_1147645.html",
        "publisher": "中华人民共和国生态环境部",
        "asset_id": "OD-SEA-STD-2026-02",
        "asset_type": "document",
        "standard_codes": ["marine-dumping-standard", "open-data"],
    },
    {
        "file": "t20260807_1163728.html",
        "title": "关于发布国家生态环境标准《海洋倾倒区选划技术导则》的公告",
        "source_url": "https://www.mee.gov.cn/xxgk2018/xxgk/xxgk01/202608/t20260807_1163728.html",
        "publisher": "中华人民共和国生态环境部",
        "asset_id": "OD-SEA-STD-2026-03",
        "asset_type": "document",
        "standard_codes": ["marine-dumping-standard", "open-data"],
    },
    {
        "file": "t20260904_1165188.html",
        "title": "关于公开征求国家生态环境标准《海洋倾倒物质评价规范 惰性无机地质材料（征求意见稿）》意见的通知",
        "source_url": "https://www.mee.gov.cn/xxgk2018/xxgk/xxgk06/202609/t20260904_1165188.html",
        "publisher": "中华人民共和国生态环境部",
        "asset_id": "OD-SEA-STD-2026-04",
        "asset_type": "document",
        "standard_codes": ["marine-dumping-standard", "open-data"],
    },
    {
        "file": "t20260804_1163420.html",
        "title": "关于公开征求国家生态环境标准《海洋倾倒物质评价规范 渔业废料（征求意见稿）》意见的通知",
        "source_url": "https://www.mee.gov.cn/xxgk2018/xxgk/xxgk06/202608/t20260804_1163420.html",
        "publisher": "中华人民共和国生态环境部",
        "asset_id": "OD-SEA-STD-2026-05",
        "asset_type": "document",
        "standard_codes": ["marine-dumping-standard", "open-data"],
    },
    {
        "file": "t20260804_1163421.html",
        "title": "关于公开征求国家生态环境标准《海洋倾倒物质评价规范 疏浚物（征求意见稿）》意见的通知",
        "source_url": "https://www.mee.gov.cn/xxgk2018/xxgk/xxgk06/202608/t20260804_1163421.html",
        "publisher": "中华人民共和国生态环境部",
        "asset_id": "OD-SEA-STD-2026-06",
        "asset_type": "document",
        "standard_codes": ["marine-dumping-standard", "open-data"],
    },
    {
        "file": "t20250701_1122520.html",
        "title": "关于公开征求国家生态环境标准《海洋倾倒区选划技术导则（征求意见稿）》意见的通知",
        "source_url": "https://www.mee.gov.cn/xxgk2018/xxgk/xxgk06/202507/t20250701_1122520.html",
        "publisher": "中华人民共和国生态环境部",
        "asset_id": "OD-SEA-STD-2025-02",
        "asset_type": "document",
        "standard_codes": ["marine-dumping-standard", "open-data"],
    },
    {
        "file": "t20220527_983664.html",
        "title": "关于生态环境部流域海域生态环境监督管理局承担“废弃物海洋倾倒许可证核发”审批事项的公告",
        "source_url": "https://www.mee.gov.cn/xxgk2018/xxgk/xxgk01/202205/t20220527_983664.html",
        "publisher": "中华人民共和国生态环境部",
        "asset_id": "OD-SEA-PERMIT-2022-01",
        "asset_type": "document",
        "standard_codes": ["marine-dumping-permit", "open-data"],
    },
]


_CONTENT_RE = re.compile(
    r"<div[^>]+(?:id|class)=[\"'](?P<key>[^\"']*(?:TRS_Editor|content|article|zoom|xl_content|text)[^\"']*)[\"'][^>]*>(?P<body>.*?)</div>",
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<script.*?</script>", re.DOTALL | re.IGNORECASE)
_STYLE_RE = re.compile(r"<style.*?</style>", re.DOTALL | re.IGNORECASE)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_html_text(raw: str) -> str:
    cleaned = _COMMENT_RE.sub("", raw)
    cleaned = _SCRIPT_RE.sub("", cleaned)
    cleaned = _STYLE_RE.sub("", cleaned)
    cleaned = _TAG_RE.sub("", cleaned)
    cleaned = cleaned.replace("&nbsp;", " ")
    cleaned = re.sub(r"[\r\n\t]+", "\n", cleaned)
    cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return cleaned.strip()


def pick_content_text(html: str) -> str:
    """Return the longest plausible article body among content-like blocks."""
    candidates = []
    for match in _CONTENT_RE.finditer(html):
        body = match.group("body")
        text = extract_html_text(body)
        if text:
            candidates.append((len(text), text))
    if not candidates:
        text = extract_html_text(html)
        return text
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def title_from_source(source: dict[str, str]) -> str:
    return source["title"]


def write_corpus(manifest_path: Path = MANIFEST) -> list[dict[str, Any]]:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for source in SOURCE_PAGES:
        html_path = manifest_path.parent / source["file"]
        if not html_path.exists():
            raise FileNotFoundError(f"missing downloaded page: {html_path}")
        html = html_path.read_text(encoding="utf-8", errors="ignore")
        text = pick_content_text(html)
        if len(text) < 200:
            raise ValueError(f"extracted text too short: {source['file']}")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        txt_name = source["asset_id"] + ".txt"
        (manifest_path.parent / txt_name).write_text(text, encoding="utf-8")
        records.append(
            {
                **source,
                "extracted_file": txt_name,
                "text_chars": len(text),
                "content_sha256": digest,
            }
        )
    manifest = {
        "status": "ok",
        "generated_at": utc_now(),
        "purpose": "public open-data corpus for deterministic ontology evaluation",
        "boundary": (
            "public open data published by mee.gov.cn; NOT a real de-identified "
            "industry dataset; extracted text is normalized for evaluation only"
        ),
        "documents": records,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return records


if __name__ == "__main__":
    records = write_corpus()
    print(f"CORPUS OK -> {MANIFEST}")
    print(f"documents={len(records)} total_chars={sum(r['text_chars'] for r in records)}")
