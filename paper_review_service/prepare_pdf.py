"""Bounded PDF extraction; launched in its own process by the container worker."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from pypdf import PdfReader


def extract(work: Path, max_pages: int) -> list[dict]:
    reader = PdfReader(work / "input.pdf")
    if reader.is_encrypted:
        raise ValueError("Encrypted PDFs are not supported")
    if not 1 <= len(reader.pages) <= max_pages:
        raise ValueError(f"PDF must contain between 1 and {max_pages} pages")
    pages = []
    for number, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        if len(text) > 200_000:
            raise ValueError(f"Page {number} exceeds the text extraction limit")
        pages.append({"page": number, "text": text, "needs_visual_reading": not text.strip()})
    if sum(len(page["text"]) for page in pages) > 2_000_000:
        raise ValueError("PDF exceeds the total text extraction limit")
    (work / "pages.json").write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
    directory = work / "pages"
    directory.mkdir(exist_ok=True)
    for page in pages:
        (directory / f"page-{page['page']:03d}.txt").write_text(page["text"], encoding="utf-8")
    (work / "paper.txt").write_text("\n\n".join(
        f"=== PDF PAGE {page['page']} ===\n{page['text']}" for page in pages), encoding="utf-8")
    return pages


if __name__ == "__main__":
    extract(Path(sys.argv[1]), int(sys.argv[2]))
