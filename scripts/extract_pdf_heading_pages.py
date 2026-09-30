#!/usr/bin/env python3
"""Map Markdown headings to PDF page numbers for static DOCX table of contents."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from pypdf import PdfReader


def normalize(value: str) -> str:
    return re.sub(r"\s+", "", value)


def numbered_headings(markdown_path: Path) -> list[tuple[str, str]]:
    headings: list[tuple[str, str]] = []
    for line in markdown_path.read_text(encoding="utf-8").splitlines():
        # The title is H1 and the numbered major sections are H2 in the
        # controlled source. Word maps those H2 sections to Heading 1.
        match = re.match(r"^##\s+(\d+)\.\s+(.*)$", line)
        if match:
            headings.append((match.group(1), match.group(2).strip()))
    return headings


def extract_pages(pdf_path: Path, markdown_path: Path, skip_pages: int = 2) -> dict[str, int]:
    headings = numbered_headings(markdown_path)
    pages: list[str] = []
    for page in PdfReader(str(pdf_path)).pages:
        text = normalize(page.extract_text() or "")
        # Every controlled H1 section starts on a new page. Drop the repeated
        # header through the rendered page number so body list markers cannot
        # be mistaken for section numbers.
        pages.append(re.sub(r"^.*?第\d+页", "", text))
    mapping: dict[str, int] = {}
    search_start = min(skip_pages, len(pages))
    for number, heading in headings:
        location = None
        for page_index in range(search_start, len(pages)):
            if pages[page_index].startswith(f"{number}."):
                location = page_index + 1
                break
        if location is None:
            raise RuntimeError(f"Heading number not found in PDF: {number}. {heading}")
        mapping[f"{number}. {heading}"] = location
        search_start = location - 1
    return mapping


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("markdown", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--skip-pages", type=int, default=2)
    args = parser.parse_args()
    mapping = extract_pages(args.pdf, args.markdown, args.skip_pages)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(mapping, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
