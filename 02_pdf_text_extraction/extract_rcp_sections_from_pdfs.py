#!/usr/bin/env python
"""
Batch-extract structured RCP sections from downloaded DPM PDFs.

This script is intentionally additive: it does not modify the master database.
It creates a reviewable CSV of section candidates that can later be loaded into
automatic_prescription_master.db after quality checks.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import re
import sys
import unicodedata
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


DEFAULT_STRICT_LAST_SECTION_MAX_CHARS = 12000
DEFAULT_FALLBACK_LAST_SECTION_MAX_CHARS = 20000


SECTION_DEFS = [
    ("4.1", "indication", "Indications therapeutiques", r"^\s*(?:4\s*[\.\)]\s*1\s+)?indications?\s+therapeutiques?\s*$"),
    ("4.2", "dosage", "Posologie et mode d'administration", r"^\s*(?:4\s*[\.\)]\s*2\s+)?posologie(?:\s+et\s+mode\s+d\s*['’]?\s*administration)?\s*$"),
    ("4.3", "contraindication", "Contre-indications", r"^\s*(?:4\s*[\.\)]\s*3\s+)?contre\s*-?\s*indications?\s*$"),
    ("4.4", "warning", "Mises en garde et precautions d'emploi", r"^\s*(?:4\s*[\.\)]\s*4\s+)?mises?\s+en\s+garde(?:\s+et\s+precautions?\s+d\s*['’]?\s*emploi)?\s*$"),
    ("4.5", "interaction", "Interactions medicamenteuses", r"^\s*(?:4\s*[\.\)]\s*5\s+)?interactions?(?:\s+medicamenteuses?)?\s*$"),
    ("4.6", "special_population", "Fertilite, grossesse et allaitement", r"^\s*(?:4\s*[\.\)]\s*6\s+)?(?:fertilite|grossesse|grossesse\s+et\s+allaitement)\s*$"),
    ("4.7", "driving", "Effets sur l'aptitude a conduire", r"^\s*(?:4\s*[\.\)]\s*7\s+)?effets?\s+sur\s+l[ea]ptitude\s*$"),
    ("4.8", "adverse_effect", "Effets indesirables", r"^\s*(?:4\s*[\.\)]\s*8\s+)?effets?\s+indesirables?\s*$"),
    ("4.9", "overdose", "Surdosage", r"^\s*(?:4\s*[\.\)]\s*9\s+)?surdosage\s*$"),
    ("5.1", "pharmacodynamic", "Proprietes pharmacodynamiques", r"^\s*(?:5\s*[\.\)]\s*1\s+)?proprietes?\s+pharmacodynamiques?\s*$"),
]

FALLBACK_SECTION_DEFS = [
    ("4.1", "indication", "Indications therapeutiques", [r"\bindications?\s+therapeutiques?\b", r"\best\s+indiqu[ee]\b"]),
    ("4.2", "dosage", "Posologie et mode d'administration", [r"\bposologie\b", r"\bmode\s+d\s*['’]?\s*administration\b"]),
    ("4.3", "contraindication", "Contre-indications", [r"\bcontre\s*-?\s*indications?\b", r"\bne\s+doit\s+pas\s+etre\s+utilis"]),
    ("4.4", "warning", "Mises en garde et precautions d'emploi", [r"\bmises?\s+en\s+garde\b", r"\bprecautions?\s+d\s*['’]?\s*emploi\b"]),
    ("4.5", "interaction", "Interactions medicamenteuses", [r"\binteractions?\s+medicamenteuses?\b", r"\bassociation\s+deconseillee\b"]),
    ("4.6", "special_population", "Fertilite, grossesse et allaitement", [r"\bgrossesse\b", r"\ballaitement\b", r"\bfertilite\b"]),
    ("4.8", "adverse_effect", "Effets indesirables", [r"\beffets?\s+indesirables?\b"]),
    ("4.9", "overdose", "Surdosage", [r"\bsurdosage\b"]),
    ("5.1", "pharmacodynamic", "Proprietes pharmacodynamiques", [r"\bproprietes?\s+pharmacodynamiques?\b", r"\bclasse\s+pharmacotherapeutique\b"]),
]


OUTPUT_FIELDS = [
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
    "source_text_chars",
    "start_offset",
    "end_offset",
    "extraction_mode",
    "boundary_mode",
    "window_limit_chars",
    "hit_window_limit",
    "parser",
    "confidence",
    "sha256",
    "extraction_status",
    "error",
]

STATUS_FIELDS = [
    "row_id",
    "amm",
    "nom",
    "source_path",
    "status",
    "parser",
    "sha256",
    "text_chars",
    "sections",
    "strict_section_rows",
    "fallback_section_rows",
    "hit_window_limit_rows",
    "error",
]


@dataclass
class PdfText:
    text: str
    page_start_offsets: list[int]
    parser: str


def strip_accents(value: str) -> str:
    parts: list[str] = []
    for char in value:
        decomposed = unicodedata.normalize("NFD", char)
        base = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
        parts.append(base[:1] if base else char)
    return "".join(parts)


def normalize_for_match(value: str) -> str:
    value = strip_accents(value).lower()
    value = value.replace("\x00", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    return value


def compact_text(value: str) -> str:
    value = value.replace("\x00", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return clean_unicode(value.strip())


def clean_unicode(value: str) -> str:
    # PDF extractors can emit lone surrogate code points. UTF-8 writers reject
    # them, so we replace invalid sequences before persisting CSV/JSON output.
    return value.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def clean_row(row: dict[str, str], fields: list[str]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for field in fields:
        value = row.get(field, "")
        cleaned[field] = clean_unicode(str(value)) if value is not None else ""
    return cleaned


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def get_pdf_magic(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(5)


def is_probably_pdf(path: Path) -> bool:
    return get_pdf_magic(path).startswith(b"%PDF-")


def resolve_path(raw_path: str, workspace: Path) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = workspace / path
    return path.resolve()


def import_pdf_reader():
    try:
        from pypdf import PdfReader  # type: ignore

        return PdfReader, "pypdf"
    except Exception:
        try:
            from PyPDF2 import PdfReader  # type: ignore

            return PdfReader, "PyPDF2"
        except Exception as exc:
            raise RuntimeError(
                "No PDF parser available. Install pypdf or PyPDF2, then rerun this script."
            ) from exc


def extract_pdf_text(path: Path) -> PdfText:
    PdfReader, parser_name = import_pdf_reader()
    reader = PdfReader(str(path))
    page_texts: list[str] = []
    offsets: list[int] = []
    cursor = 0
    for page in reader.pages:
        offsets.append(cursor)
        text = page.extract_text() or ""
        page_texts.append(text)
        cursor += len(text) + 1
    return PdfText(text="\n".join(page_texts), page_start_offsets=offsets, parser=parser_name)


def page_for_offset(offsets: list[int], offset: int) -> int:
    if not offsets:
        return 0
    page = 1
    for i, start in enumerate(offsets, start=1):
        if start <= offset:
            page = i
        else:
            break
    return page


def find_section_headers(norm_text: str) -> list[tuple[int, str, str, str]]:
    hits: list[tuple[int, str, str, str]] = []
    for code, kind, title, pattern in SECTION_DEFS:
        for match in re.finditer(pattern, norm_text, flags=re.IGNORECASE | re.MULTILINE):
            hits.append((match.start(), code, kind, title))
    hits.sort(key=lambda item: item[0])

    deduped: list[tuple[int, str, str, str]] = []
    seen_positions: set[tuple[int, str]] = set()
    for item in hits:
        position_bucket = item[0] // 10
        key = (position_bucket, item[1])
        if key in seen_positions:
            continue
        seen_positions.add(key)
        deduped.append(item)
    return deduped


def find_fallback_headers(norm_text: str) -> list[tuple[int, str, str, str]]:
    hits: list[tuple[int, str, str, str]] = []
    seen_codes: set[str] = set()
    for code, kind, title, patterns in FALLBACK_SECTION_DEFS:
        best_start: int | None = None
        for pattern in patterns:
            match = re.search(pattern, norm_text, flags=re.IGNORECASE)
            if match and (best_start is None or match.start() < best_start):
                best_start = match.start()
        if best_start is not None and code not in seen_codes:
            hits.append((best_start, code, kind, title))
            seen_codes.add(code)
    hits.sort(key=lambda item: item[0])
    return hits


def resolve_section_boundary(
    headers: list[tuple[int, str, str, str]],
    idx: int,
    text_len: int,
    start: int,
    last_section_max_chars: int,
) -> tuple[int, str, str, bool]:
    if idx + 1 < len(headers):
        return headers[idx + 1][0], "next_header", "", False
    if last_section_max_chars <= 0:
        return text_len, "document_tail", "", False
    end = min(text_len, start + last_section_max_chars)
    return end, "last_section_window", str(last_section_max_chars), end < text_len


def extract_sections(
    row: dict[str, str],
    pdf_path: Path,
    workspace: Path,
    strict_last_section_max_chars: int = DEFAULT_STRICT_LAST_SECTION_MAX_CHARS,
    fallback_last_section_max_chars: int = DEFAULT_FALLBACK_LAST_SECTION_MAX_CHARS,
) -> tuple[list[dict[str, str]], dict[str, str]]:
    row_id = row.get("ROW_ID") or row.get("row_id") or ""
    amm = row.get("AMM") or row.get("amm") or ""
    nom = row.get("NOM") or row.get("nom") or ""
    try:
        source_path = str(pdf_path.relative_to(workspace))
    except ValueError:
        source_path = str(pdf_path)
    file_hash = sha256_file(pdf_path)

    pdf_text = extract_pdf_text(pdf_path)
    text = compact_text(pdf_text.text)
    norm_text = normalize_for_match(text)
    headers = find_section_headers(norm_text)
    used_fallback = False
    if not headers and len(norm_text) >= 1000:
        headers = find_fallback_headers(norm_text)
        used_fallback = bool(headers)

    if not headers:
        status = {
            "row_id": row_id,
            "amm": amm,
            "nom": nom,
            "source_path": source_path,
            "status": "no_section_headers_found" if len(text) > 0 else "no_text_extracted",
            "parser": pdf_text.parser,
            "sha256": file_hash,
            "text_chars": str(len(text)),
        }
        return [], status

    rows: list[dict[str, str]] = []
    for idx, (start, code, kind, title) in enumerate(headers):
        end, boundary_mode, window_limit_chars, hit_window_limit = resolve_section_boundary(
            headers=headers,
            idx=idx,
            text_len=len(text),
            start=start,
            last_section_max_chars=fallback_last_section_max_chars if used_fallback else strict_last_section_max_chars,
        )
        section_text = compact_text(text[start:end])
        if len(section_text) < 80:
            continue
        page_start = page_for_offset(pdf_text.page_start_offsets, start)
        page_end = page_for_offset(pdf_text.page_start_offsets, max(start, end - 1))
        confidence = "0.60" if used_fallback else ("0.95" if re.match(r"^[45]\.", code) else "0.80")
        section_id = f"rcp_{row_id or amm}_{code.replace('.', '_')}_{idx + 1}{'_fallback' if used_fallback else ''}"
        rows.append(
            {
                "section_id": section_id,
                "row_id": row_id,
                "amm": amm,
                "nom": nom,
                "section_code": code,
                "section_kind": kind,
                "section_title": title,
                "section_text": section_text,
                "source_path": source_path,
                "page_start": str(page_start),
                "page_end": str(page_end),
                "text_chars": str(len(section_text)),
                "source_text_chars": str(len(text)),
                "start_offset": str(start),
                "end_offset": str(end),
                "extraction_mode": "keyword_fallback" if used_fallback else "strict_header",
                "boundary_mode": boundary_mode,
                "window_limit_chars": window_limit_chars,
                "hit_window_limit": "1" if hit_window_limit else "0",
                "parser": f"{pdf_text.parser}{'+keyword_fallback' if used_fallback else ''}",
                "confidence": confidence,
                "sha256": file_hash,
                "extraction_status": "section_extracted",
                "error": "",
            }
        )

    fallback_section_rows = sum(1 for row in rows if row.get("extraction_mode") == "keyword_fallback")
    hit_window_limit_rows = sum(1 for row in rows if row.get("hit_window_limit") == "1")
    status = {
        "row_id": row_id,
        "amm": amm,
        "nom": nom,
        "source_path": source_path,
        "status": "fallback_sections_extracted" if used_fallback and rows else ("sections_extracted" if rows else "headers_found_but_no_sections_kept"),
        "parser": f"{pdf_text.parser}{'+keyword_fallback' if used_fallback else ''}",
        "sha256": file_hash,
        "text_chars": str(len(text)),
        "sections": str(len(rows)),
        "strict_section_rows": str(len(rows) - fallback_section_rows),
        "fallback_section_rows": str(fallback_section_rows),
        "hit_window_limit_rows": str(hit_window_limit_rows),
    }
    return rows, status


def iter_manifest_rows(manifest_path: Path, workspace: Path) -> Iterable[tuple[dict[str, str], Path]]:
    with manifest_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            status = (row.get("RCP_VERIFY_STATUS") or row.get("rcp_verify_status") or "").lower()
            raw_path = row.get("DOWNLOADED_RCP_FILE") or row.get("downloaded_rcp_file") or ""
            if status != "verified" or not raw_path:
                continue
            pdf_path = resolve_path(raw_path, workspace)
            yield row, pdf_path


def iter_lab_scan_rows(scan_path: Path, workspace: Path) -> Iterable[tuple[dict[str, str], Path]]:
    with scan_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for idx, row in enumerate(reader, start=1):
            raw_path = row.get("pdf_path") or ""
            mapped_ids = row.get("mapped_row_ids") or row.get("name_matched_row_ids") or ""
            if not raw_path or not mapped_ids:
                continue
            row_ids = [part.strip() for part in re.split(r"[;,]", mapped_ids) if part.strip()]
            if not row_ids:
                continue
            first_row_id = row_ids[0]
            amm_matches = row.get("amm_matches") or ""
            first_amm = ""
            if amm_matches:
                first_amm = re.split(r"[;,]", amm_matches)[0].strip()
            source = row.get("source") or "lab"
            doc_kind = row.get("doc_kind") or "unknown"
            name_key = row.get("name_match_key") or ""
            synthetic = {
                "ROW_ID": first_row_id,
                "AMM": first_amm,
                "NOM": name_key or f"{source}_{doc_kind}_{idx}",
                "LAB_SOURCE": source,
                "DOC_KIND": doc_kind,
                "MAPPED_ROW_IDS": mapped_ids,
                "NAME_MATCH_SCORE": row.get("name_match_score") or "",
            }
            yield synthetic, resolve_path(raw_path, workspace)


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", errors="replace", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(clean_row(row, OUTPUT_FIELDS))


def write_status_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", errors="replace", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=STATUS_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(clean_row(row, STATUS_FIELDS))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract structured RCP section candidates from downloaded PDFs.")
    parser.add_argument("--manifest", default="dpm_live_out/checkpoint_rcp_manifest.csv")
    parser.add_argument("--input-kind", choices=["dpm_rcp", "lab_scan"], default="dpm_rcp")
    parser.add_argument("--output", default="dpm_live_out/rcp_section_extraction_candidates.csv")
    parser.add_argument("--status-output", default="dpm_live_out/rcp_section_extraction_status.csv")
    parser.add_argument("--summary", default="dpm_live_out/rcp_section_extraction_summary.json")
    parser.add_argument("--limit", type=int, default=0, help="Limit processed PDFs for smoke tests. 0 means no limit.")
    parser.add_argument(
        "--strict-last-section-max-chars",
        type=int,
        default=DEFAULT_STRICT_LAST_SECTION_MAX_CHARS,
        help="Maximum tail window for a strict-header last section. 0 means keep the full remaining document tail.",
    )
    parser.add_argument(
        "--fallback-last-section-max-chars",
        type=int,
        default=DEFAULT_FALLBACK_LAST_SECTION_MAX_CHARS,
        help="Maximum tail window for a keyword-fallback last section. 0 means keep the full remaining document tail.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Parse inputs and print summary without writing CSV.")
    parser.add_argument("--verbose-parser", action="store_true", help="Show PDF parser warnings.")
    args = parser.parse_args(argv)

    if not args.verbose_parser:
        warnings.filterwarnings("ignore")
        logging.getLogger("pypdf").setLevel(logging.ERROR)
        logging.getLogger("PyPDF2").setLevel(logging.ERROR)

    workspace = Path.cwd().resolve()
    manifest_path = resolve_path(args.manifest, workspace)
    output_path = resolve_path(args.output, workspace)
    status_output_path = resolve_path(args.status_output, workspace)
    summary_path = resolve_path(args.summary, workspace)

    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    output_rows: list[dict[str, str]] = []
    status_rows: list[dict[str, str]] = []
    processed = 0
    missing_files = 0
    failed = 0

    row_iter = iter_lab_scan_rows(manifest_path, workspace) if args.input_kind == "lab_scan" else iter_manifest_rows(manifest_path, workspace)

    for row, pdf_path in row_iter:
        if args.limit and processed >= args.limit:
            break
        processed += 1
        row_id = row.get("ROW_ID") or row.get("row_id") or ""
        amm = row.get("AMM") or row.get("amm") or ""
        nom = row.get("NOM") or row.get("nom") or ""
        if not pdf_path.exists():
            missing_files += 1
            status_rows.append(
                {
                    "row_id": row_id,
                    "amm": amm,
                    "nom": nom,
                    "source_path": str(pdf_path),
                    "status": "pdf_missing",
                    "error": "file not found",
                }
            )
            continue
        try:
            file_hash = sha256_file(pdf_path)
            magic = get_pdf_magic(pdf_path)
            if not magic.startswith(b"%PDF-"):
                failed += 1
                status_rows.append(
                    {
                        "row_id": row_id,
                        "amm": amm,
                        "nom": nom,
                        "source_path": str(pdf_path),
                        "status": "not_a_pdf",
                        "parser": "",
                        "sha256": file_hash,
                        "text_chars": "0",
                        "sections": "0",
                        "error": f"invalid PDF header: {magic!r}",
                    }
                )
                continue
        except Exception as exc:
            failed += 1
            status_rows.append(
                {
                    "row_id": row_id,
                    "amm": amm,
                    "nom": nom,
                    "source_path": str(pdf_path),
                    "status": "preflight_failed",
                    "error": str(exc),
                }
            )
            continue
        try:
            sections, status = extract_sections(
                row,
                pdf_path,
                workspace,
                strict_last_section_max_chars=args.strict_last_section_max_chars,
                fallback_last_section_max_chars=args.fallback_last_section_max_chars,
            )
            output_rows.extend(sections)
            status_rows.append(status)
        except Exception as exc:
            failed += 1
            status_rows.append(
                {
                    "row_id": row_id,
                    "amm": amm,
                    "nom": nom,
                    "source_path": str(pdf_path),
                    "status": "extract_failed",
                    "error": str(exc),
                }
            )

    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manifest": str(manifest_path),
        "output": str(output_path),
        "status_output": str(status_output_path),
        "strict_last_section_max_chars": args.strict_last_section_max_chars,
        "fallback_last_section_max_chars": args.fallback_last_section_max_chars,
        "processed_pdfs": processed,
        "missing_files": missing_files,
        "failed_pdfs": failed,
        "section_rows": len(output_rows),
        "status_counts": {},
    }
    counts: dict[str, int] = {}
    for status in status_rows:
        key = status.get("status", "unknown")
        counts[key] = counts.get(key, 0) + 1
    summary["status_counts"] = counts
    summary["fallback_section_rows"] = sum(1 for row in output_rows if row.get("extraction_mode") == "keyword_fallback")
    summary["strict_section_rows"] = len(output_rows) - int(summary["fallback_section_rows"])
    summary["hit_window_limit_rows"] = sum(1 for row in output_rows if row.get("hit_window_limit") == "1")

    if args.dry_run:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    write_csv(output_path, output_rows)
    write_status_csv(status_output_path, status_rows)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_text = clean_unicode(json.dumps(summary, indent=2, ensure_ascii=False))
    summary_path.write_text(summary_text, encoding="utf-8", errors="replace")
    print(summary_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
