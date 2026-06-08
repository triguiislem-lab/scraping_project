#!/usr/bin/env python3
"""
OCR local DPM PDFs from local_document_ocr_queue.csv and create section candidates.

The script uses PyMuPDF to render PDF pages and Tesseract via pytesseract for OCR.
It writes:
- local_document_recovered_sections.csv
- local_document_ocr_status.csv
- local_document_ocr_texts/*.txt
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import fitz  # PyMuPDF
import pytesseract
from PIL import Image


SECTION_FIELDS = [
    "section_id",
    "row_id",
    "amm",
    "nom",
    "section_code",
    "section_kind",
    "section_title",
    "section_text",
    "source_path",
    "page_start",
    "page_end",
    "text_chars",
    "parser",
    "confidence",
    "sha256",
    "extraction_status",
    "error",
]

SECTION_PATTERNS: List[Tuple[str, str, str, re.Pattern[str]]] = [
    ("4.1", "indication", "Indications therapeutiques", re.compile(r"(?:4[\.\s]*1\s*)?indications?\s+th[eé]rapeutiques?", re.I)),
    ("4.2", "dosage", "Posologie et mode d'administration", re.compile(r"(?:4[\.\s]*2\s*)?posologie(?:\s+et\s+mode\s+d['’]administration)?", re.I)),
    ("4.3", "contraindication", "Contre-indications", re.compile(r"(?:4[\.\s]*3\s*)?contre[\s-]?indications?", re.I)),
    ("4.4", "warning", "Mises en garde et precautions", re.compile(r"(?:4[\.\s]*4\s*)?mises?\s+en\s+garde|pr[eé]cautions?\s+d['’]emploi", re.I)),
    ("4.5", "interaction", "Interactions", re.compile(r"(?:4[\.\s]*5\s*)?interactions?\s+avec\s+d['’]autres\s+m[eé]dicaments|interactions?", re.I)),
    ("4.6", "special_population", "Grossesse et allaitement", re.compile(r"(?:4[\.\s]*6\s*)?grossesse|allaitement|fertilit[eé]", re.I)),
    ("4.8", "adverse_effect", "Effets indesirables", re.compile(r"(?:4[\.\s]*8\s*)?effets?\s+ind[eé]sirables?", re.I)),
    ("4.9", "overdose", "Surdosage", re.compile(r"(?:4[\.\s]*9\s*)?surdosage", re.I)),
    ("5.1", "pharmacodynamic", "Proprietes pharmacodynamiques", re.compile(r"(?:5[\.\s]*1\s*)?propri[eé]t[eé]s?\s+pharmacodynamiques?", re.I)),
]


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).encode("utf-8", "ignore").decode("utf-8", "ignore")
    if "Ã" in text or "Â" in text:
        try:
            repaired = text.encode("latin1", "ignore").decode("utf-8", "ignore")
            if repaired.count("�") <= text.count("�"):
                text = repaired
        except Exception:
            pass
    fixes = {
        "Ã©": "é",
        "Ã¨": "è",
        "Ãª": "ê",
        "Ã ": "à",
        "Ã¢": "â",
        "Ã§": "ç",
        "Ã´": "ô",
        "Â": " ",
    }
    for bad, good in fixes.items():
        text = text.replace(bad, good)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fields: Sequence[str], rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="", errors="ignore") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: clean(row.get(field, "")) for field in fields})


def sha_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def page_to_image(page: fitz.Page, dpi: int) -> Image.Image:
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def ocr_pdf(path: Path, lang: str, tessdata_dir: str, dpi: int, max_pages: int) -> Tuple[str, int]:
    texts: List[str] = []
    with fitz.open(path) as doc:
        pages = len(doc) if max_pages <= 0 else min(max_pages, len(doc))
        for idx in range(pages):
            page = doc.load_page(idx)
            image = page_to_image(page, dpi)
            tessdata_config = Path(tessdata_dir).resolve().as_posix() if tessdata_dir else ""
            config = f"--psm 6 --tessdata-dir {tessdata_config}" if tessdata_config else "--psm 6"
            page_text = pytesseract.image_to_string(image, lang=lang, config=config)
            page_text = clean(page_text)
            texts.append(f"\n\n--- page {idx + 1} ---\n{page_text}")
    return clean("\n".join(texts)), pages


def find_sections(text: str) -> List[Tuple[str, str, str, int, int]]:
    matches: List[Tuple[str, str, str, int]] = []
    for code, kind, title, pattern in SECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            matches.append((code, kind, title, match.start()))
    matches.sort(key=lambda item: item[3])
    sections: List[Tuple[str, str, str, int, int]] = []
    for idx, (code, kind, title, start) in enumerate(matches):
        end = matches[idx + 1][3] if idx + 1 < len(matches) else len(text)
        if end - start >= 120:
            sections.append((code, kind, title, start, end))
    return sections


def chunk_text(text: str, max_chars: int = 4500) -> List[str]:
    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            split_at = text.rfind("\n", start, end)
            if split_at > start + 1000:
                end = split_at
        chunk = clean(text[start:end])
        if len(chunk) >= 200:
            chunks.append(chunk)
        start = end
    return chunks


def make_section_rows(row: Dict[str, str], text: str, source_path: Path, pages: int, parser: str) -> List[Dict[str, Any]]:
    sha = sha_file(source_path)
    rows: List[Dict[str, Any]] = []
    sections = find_sections(text)
    if sections:
        for seq, (code, kind, title, start, end) in enumerate(sections, start=1):
            section_text = clean(text[start:end])
            rows.append(
                {
                    "section_id": f"ocr_{row.get('row_id')}_{code}_{seq}",
                    "row_id": row.get("row_id", ""),
                    "amm": row.get("amm", ""),
                    "nom": row.get("nom", ""),
                    "section_code": code,
                    "section_kind": kind,
                    "section_title": title,
                    "section_text": section_text,
                    "source_path": str(source_path),
                    "page_start": "",
                    "page_end": pages,
                    "text_chars": len(section_text),
                    "parser": parser,
                    "confidence": "0.62",
                    "sha256": sha,
                    "extraction_status": "ocr_sections_extracted",
                    "error": "",
                }
            )
    else:
        for seq, chunk in enumerate(chunk_text(text), start=1):
            rows.append(
                {
                    "section_id": f"ocr_{row.get('row_id')}_full_{seq}",
                    "row_id": row.get("row_id", ""),
                    "amm": row.get("amm", ""),
                    "nom": row.get("nom", ""),
                    "section_code": "ocr",
                    "section_kind": "ocr_full_text",
                    "section_title": "OCR full local document text",
                    "section_text": chunk,
                    "source_path": str(source_path),
                    "page_start": "",
                    "page_end": pages,
                    "text_chars": len(chunk),
                    "parser": parser,
                    "confidence": "0.45",
                    "sha256": sha,
                    "extraction_status": "ocr_full_text_only",
                    "error": "",
                }
            )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", default="dpm_live_out/local_document_ocr_queue.csv")
    parser.add_argument("--sections-output", default="dpm_live_out/local_document_recovered_sections.csv")
    parser.add_argument("--status-output", default="dpm_live_out/local_document_ocr_status.csv")
    parser.add_argument("--text-dir", default="dpm_live_out/local_document_ocr_texts")
    parser.add_argument("--summary", default="dpm_live_out/local_document_ocr_summary.json")
    parser.add_argument("--lang", default="fra")
    parser.add_argument("--tessdata-dir", default="ocr_tessdata")
    parser.add_argument("--dpi", type=int, default=180)
    parser.add_argument("--max-pages", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    queue_rows = read_csv(Path(args.queue))
    if args.limit > 0:
        queue_rows = queue_rows[: args.limit]

    text_dir = Path(args.text_dir)
    text_dir.mkdir(parents=True, exist_ok=True)

    section_rows: List[Dict[str, Any]] = []
    status_rows: List[Dict[str, Any]] = []
    for idx, row in enumerate(queue_rows, start=1):
        source_path = Path(row.get("resolved_source_path") or row.get("source_path") or "")
        if not source_path.is_absolute():
            source_path = Path.cwd() / source_path
        print(f"[{datetime.now().strftime('%H:%M:%S')}] OCR {idx}/{len(queue_rows)} {row.get('nom')} {source_path}", flush=True)
        status = "ok"
        message = ""
        pages = 0
        text = ""
        rows: List[Dict[str, Any]] = []
        try:
            text, pages = ocr_pdf(source_path, args.lang, args.tessdata_dir, args.dpi, args.max_pages)
            text_path = text_dir / f"{row.get('row_id')}_{row.get('amm')}.txt"
            text_path.write_text(text, encoding="utf-8")
            if len(text) < 200:
                status = "low_text"
                message = "OCR produced too little text."
            else:
                rows = make_section_rows(row, text, source_path, pages, f"pymupdf_tesseract_{args.lang}")
                section_rows.extend(rows)
        except Exception as exc:
            status = "failed"
            message = str(exc)
        status_rows.append(
            {
                "row_id": row.get("row_id", ""),
                "amm": row.get("amm", ""),
                "nom": row.get("nom", ""),
                "nom_generique": row.get("nom_generique", ""),
                "source_path": str(source_path),
                "status": status,
                "pages_processed": pages,
                "ocr_text_chars": len(text),
                "section_rows": len(rows),
                "message": message,
            }
        )

    write_csv(Path(args.sections_output), SECTION_FIELDS, section_rows)
    write_csv(
        Path(args.status_output),
        ["row_id", "amm", "nom", "nom_generique", "source_path", "status", "pages_processed", "ocr_text_chars", "section_rows", "message"],
        status_rows,
    )
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input_rows": len(queue_rows),
        "section_rows": len(section_rows),
        "row_ids_with_ocr_sections": len({row["row_id"] for row in section_rows}),
        "status_counts": {status: sum(1 for row in status_rows if row["status"] == status) for status in sorted({row["status"] for row in status_rows})},
        "outputs": {
            "sections": args.sections_output,
            "status": args.status_output,
            "texts": args.text_dir,
            "summary": args.summary,
        },
    }
    Path(args.summary).write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
