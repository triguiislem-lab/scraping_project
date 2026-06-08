#!/usr/bin/env python3
"""
Build a consolidated detail/audit CSV for medicines still missing usable evidence.

The script intentionally uses local artifacts only. It does not call network APIs.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


ROOT = Path(".")
OUT_DIR = Path("dpm_live_out")
MISSING_PATH = OUT_DIR / "treated_medicines_missing_all_local_evidence.csv"
DETAILS_OUT = OUT_DIR / "remaining_448_medicines_all_available_details.csv"
SOURCE_AUDIT_OUT = OUT_DIR / "remaining_448_source_crosscheck.csv"
SUMMARY_OUT = OUT_DIR / "remaining_448_details_summary.json"

SECTION_FILES = {
    "fallback_label_sections": OUT_DIR / "fallback_label_section.csv",
    "bdpm_api_sections": OUT_DIR / "bdpm_api_fallback_sections.csv",
    "openfda_live_sections": OUT_DIR / "openfda_live_fallback_sections.csv",
    "eu_uk_live_sections": OUT_DIR / "eu_uk_live_fallback_sections.csv",
    "global_regulatory_sections": OUT_DIR / "global_regulatory_fallback_sections.csv",
    "rcp_sections": OUT_DIR / "rcp_section_extraction_candidates.csv",
    "lab_sections": OUT_DIR / "lab_document_section_candidates.csv",
    "local_recovered_sections": OUT_DIR / "local_document_recovered_sections.csv",
}

csv.field_size_limit(min(sys.maxsize, 2_147_483_647))


MOJIBAKE_FIXES = {
    "Ã‰": "É",
    "ÃÈ": "È",
    "ÃÊ": "Ê",
    "Ã‹": "Ë",
    "Ã€": "À",
    "Ã‚": "Â",
    "Ã„": "Ä",
    "Ã‡": "Ç",
    "ÃŽ": "Î",
    "ÃÏ": "Ï",
    "Ã”": "Ô",
    "Ã–": "Ö",
    "Ã™": "Ù",
    "Ã›": "Û",
    "Ãœ": "Ü",
    "Ã©": "é",
    "Ã¨": "è",
    "Ãª": "ê",
    "Ã«": "ë",
    "Ã ": "à",
    "Ã¢": "â",
    "Ã¤": "ä",
    "Ã§": "ç",
    "Ã®": "î",
    "Ã¯": "ï",
    "Ã´": "ô",
    "Ã¶": "ö",
    "Ã¹": "ù",
    "Ã»": "û",
    "Ã¼": "ü",
    "Â°": "°",
    "Âµ": "µ",
    "Â": " ",
}


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    try:
        repaired = text.encode("latin-1").decode("utf-8")
        if repaired.count("�") <= text.count("�") and (
            repaired.count("é") + repaired.count("è") + repaired.count("à") + repaired.count("ç")
            >= text.count("é") + text.count("è") + text.count("à") + text.count("ç")
        ):
            text = repaired
    except Exception:
        pass
    for bad, good in MOJIBAKE_FIXES.items():
        text = text.replace(bad, good)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def norm(value: Any) -> str:
    text = clean(value).upper()
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def compact(values: Iterable[Any], max_items: int = 4, max_chars: int = 900) -> str:
    seen = []
    used = set()
    for value in values:
        text = clean(value)
        if not text or text in used:
            continue
        used.add(text)
        seen.append(text)
        if len(seen) >= max_items:
            break
    out = " || ".join(seen)
    return out[:max_chars]


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as handle:
        return [{clean(k): clean(v) for k, v in row.items()} for row in csv.DictReader(handle)]


def stream_csv_rows(path: Path) -> Iterable[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            yield {clean(k): clean(v) for k, v in row.items()}


def write_csv(path: Path, fields: Sequence[str], rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: clean(row.get(field, "")) for field in fields})


def current_remaining_paths(row_count: int) -> Tuple[Path, Path, Path]:
    return (
        OUT_DIR / f"remaining_{row_count}_medicines_all_available_details.csv",
        OUT_DIR / f"remaining_{row_count}_source_crosscheck.csv",
        OUT_DIR / f"remaining_{row_count}_details_summary.json",
    )


def note_source(audit: List[Dict[str, Any]], path: Path, join_method: str, scanned_rows: int, matched_rows: int, matched_ids: Iterable[str], notes: str = "") -> None:
    audit.append(
        {
            "source_path": str(path),
            "exists": str(path.exists()),
            "join_method": join_method,
            "scanned_rows": scanned_rows,
            "matched_rows": matched_rows,
            "matched_unique_row_ids": len(set(str(x) for x in matched_ids if str(x))),
            "notes": notes,
        }
    )


def add_prefixed(target: Dict[str, Any], prefix: str, row: Dict[str, Any], fields: Sequence[str]) -> None:
    for field in fields:
        target[f"{prefix}_{field}"] = clean(row.get(field, ""))


def aggregate_rows(rows: Iterable[Dict[str, str]], fields: Sequence[str]) -> Dict[str, str]:
    rows = list(rows)
    out: Dict[str, str] = {"row_count": str(len(rows))}
    for field in fields:
        out[field] = compact(row.get(field, "") for row in rows)
    return out


def group_file_by_row_id(
    path: Path,
    missing_ids: set[str],
    fields: Sequence[str],
    audit: List[Dict[str, Any]],
    notes: str = "",
) -> Dict[str, Dict[str, str]]:
    out: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    scanned = matched = 0
    if path.exists():
        for row in stream_csv_rows(path):
            scanned += 1
            row_id = clean(row.get("row_id") or row.get("ROW_ID"))
            if row_id in missing_ids:
                matched += 1
                out[row_id].append(row)
    note_source(audit, path, "row_id", scanned, matched, out.keys(), notes)
    return {row_id: aggregate_rows(rows, fields) for row_id, rows in out.items()}


def group_file_by_amm(
    path: Path,
    amm_to_row_ids: Dict[str, List[str]],
    fields: Sequence[str],
    audit: List[Dict[str, Any]],
    notes: str = "",
) -> Dict[str, Dict[str, str]]:
    out: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    scanned = matched = 0
    matched_ids: set[str] = set()
    if path.exists():
        for row in stream_csv_rows(path):
            scanned += 1
            amm = clean(row.get("amm") or row.get("AMM"))
            for row_id in amm_to_row_ids.get(norm(amm), []):
                matched += 1
                matched_ids.add(row_id)
                out[row_id].append(row)
    note_source(audit, path, "amm", scanned, matched, matched_ids, notes)
    return {row_id: aggregate_rows(rows, fields) for row_id, rows in out.items()}


def index_position_file(
    path: Path,
    missing_ids: set[str],
    fields: Sequence[str],
    audit: List[Dict[str, Any]],
    row_id_start: int = 1,
    notes: str = "",
) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    scanned = matched = 0
    if path.exists():
        for idx, row in enumerate(stream_csv_rows(path), start=row_id_start):
            scanned += 1
            row_id = str(idx)
            if row_id in missing_ids:
                matched += 1
                out[row_id] = {field: clean(row.get(field, "")) for field in fields}
    note_source(audit, path, f"file_order_row_id_start_{row_id_start}", scanned, matched, out.keys(), notes)
    return out


def index_query_status(path: Path, query_fields: Sequence[str], rows: Dict[str, Dict[str, Any]], audit: List[Dict[str, Any]], prefix: str) -> None:
    by_query: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    scanned = 0
    if path.exists():
        for row in stream_csv_rows(path):
            scanned += 1
            q = norm(row.get("query"))
            if q:
                by_query[q].append(row)
    matched_ids: set[str] = set()
    matched_rows = 0
    for row_id, out in rows.items():
        possible = [
            out.get("bdpm_queue_query_primary", ""),
            out.get("bdpm_queue_query_generic", ""),
            out.get("bdpm_queue_query_brand", ""),
            out.get("nom_generique", ""),
            out.get("nom", ""),
        ]
        hits: List[Dict[str, str]] = []
        for q in possible:
            hits.extend(by_query.get(norm(q), []))
        if hits:
            matched_ids.add(row_id)
            matched_rows += len(hits)
            agg = aggregate_rows(hits, query_fields)
            for key, value in agg.items():
                out[f"{prefix}_{key}"] = value
    note_source(audit, path, "query_to_missing_row_queries", scanned, matched_rows, matched_ids)


def add_count_from_row_id_file(path: Path, missing_ids: set[str], rows: Dict[str, Dict[str, Any]], audit: List[Dict[str, Any]], column_name: str, sample_fields: Sequence[str] = ()) -> None:
    counts: Counter[str] = Counter()
    samples: Dict[str, List[str]] = defaultdict(list)
    scanned = matched = 0
    if path.exists():
        for row in stream_csv_rows(path):
            scanned += 1
            row_id = clean(row.get("row_id") or row.get("ROW_ID"))
            if row_id in missing_ids:
                matched += 1
                counts[row_id] += 1
                if sample_fields and len(samples[row_id]) < 3:
                    samples[row_id].append(" | ".join(f"{field}={clean(row.get(field, ''))}" for field in sample_fields))
    for row_id, count in counts.items():
        rows[row_id][column_name] = count
        if sample_fields:
            rows[row_id][f"{column_name}_sample"] = compact(samples[row_id], max_items=3, max_chars=800)
    for row_id in missing_ids:
        rows[row_id].setdefault(column_name, 0)
    note_source(audit, path, "row_id_count", scanned, matched, counts.keys())


def scan_local_files(rows: Dict[str, Dict[str, Any]], audit: List[Dict[str, Any]]) -> None:
    roots = [
        Path("medis"),
        Path("opalia"),
        Path("teriak"),
        Path("unimed"),
        OUT_DIR / "rcp_pdfs",
        OUT_DIR / "rcp_texts",
        OUT_DIR / "local_document_ocr_texts",
    ]
    ids = set(rows)
    matched_ids: set[str] = set()
    scanned = 0
    for root in roots:
        if not root.exists():
            note_source(audit, root, "filesystem_path_contains_amm_or_row_id", 0, 0, [], "directory missing")
            continue
        root_scanned = root_matched = 0
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                scanned += 1
                root_scanned += 1
                path = Path(dirpath) / filename
                hay = norm(str(path))
                for row_id, row in rows.items():
                    amm = norm(row.get("amm"))
                    if not amm:
                        continue
                    row_id_token = f" {row_id} "
                    if amm in hay or row_id_token in f" {hay} ":
                        matched_ids.add(row_id)
                        root_matched += 1
                        row["filesystem_local_file_hit_count"] = int(row.get("filesystem_local_file_hit_count", 0) or 0) + 1
                        existing = row.get("filesystem_local_file_hit_samples", "")
                        sample = str(path)
                        row["filesystem_local_file_hit_samples"] = compact([existing, sample], max_items=4, max_chars=900)
        note_source(audit, root, "filesystem_path_contains_amm_or_row_id", root_scanned, root_matched, matched_ids)
    for row in rows.values():
        row.setdefault("filesystem_local_file_hit_count", 0)
        row.setdefault("filesystem_local_file_hit_samples", "")
    note_source(audit, Path("<local_document_directories_total>"), "filesystem_path_contains_amm_or_row_id", scanned, sum(int(r.get("filesystem_local_file_hit_count", 0) or 0) for r in rows.values()), matched_ids)


def enrich_from_sqlite(rows: Dict[str, Dict[str, Any]], audit: List[Dict[str, Any]]) -> None:
    db_path = OUT_DIR / "automatic_prescription_master.db"
    if not db_path.exists():
        note_source(audit, db_path, "sqlite_row_id", 0, 0, [], "database missing")
        return
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    ids = set(rows)

    def fetch_by_row_id(table: str, fields: Sequence[str], prefix: str) -> None:
        scanned = con.execute(f"select count(*) from {table}").fetchone()[0]
        matched = 0
        matched_ids: set[str] = set()
        placeholders = ",".join("?" for _ in ids)
        query = f"select * from {table} where row_id in ({placeholders})"
        for db_row in con.execute(query, list(ids)):
            row_id = clean(db_row["row_id"])
            matched += 1
            matched_ids.add(row_id)
            for field in fields:
                rows[row_id][f"{prefix}_{field}"] = clean(db_row[field]) if field in db_row.keys() else ""
        note_source(audit, db_path, f"sqlite:{table}.row_id", scanned, matched, matched_ids)

    fetch_by_row_id(
        "medicine_profile",
        [
            "cdss_product_id",
            "cdss_product_label",
            "cdss_dci_api",
            "cdss_therapeutic_class",
            "cdss_therapeutic_subclass",
            "cdss_reimbursement_category",
            "cdss_match_type",
            "cdss_match_score",
            "ingredient_link_count",
            "indication_rule_count",
            "dosage_rule_count",
            "contraindication_rule_count",
            "interaction_rule_count",
            "adverse_effect_count",
            "renal_hepatic_adjustment_count",
            "special_population_rule_count",
            "administration_rule_count",
            "substitution_rule_count",
            "automatic_prescription_readiness",
            "gap_flags",
        ],
        "db_profile",
    )
    fetch_by_row_id(
        "raw_dpm_rcp_manifest",
        ["RCP_VERIFY_STATUS", "RCP_HTTP_STATUS", "VERIFIED_RCP_URL", "DOWNLOADED_RCP_FILE", "RCP_BYTES"],
        "db_rcp_manifest",
    )

    ingredient_rows = con.execute(
        """
        select mi.row_id, ir.ingredient_name, ir.ingredient_name_fr, ir.ingredient_name_en,
               ir.rxnorm_rxcui, ir.rxnorm_tty, ir.atc_code
        from medicine_ingredient mi
        left join cdss_ingredient_reference ir on ir.ingredient_id = mi.ingredient_id
        """
    ).fetchall()
    by_row: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for item in ingredient_rows:
        row_id = clean(item["row_id"])
        if row_id in ids:
            by_row[row_id].append(item)
    for row_id, items in by_row.items():
        rows[row_id]["db_ingredient_names"] = compact(item["ingredient_name"] or item["ingredient_name_fr"] or item["ingredient_name_en"] for item in items)
        rows[row_id]["db_ingredient_rxcuis"] = compact(item["rxnorm_rxcui"] for item in items)
        rows[row_id]["db_ingredient_atc_codes"] = compact(item["atc_code"] for item in items)
        rows[row_id]["db_ingredient_row_count"] = len(items)
    note_source(audit, db_path, "sqlite:medicine_ingredient.row_id", len(ingredient_rows), sum(len(v) for v in by_row.values()), by_row.keys())

    local_doc_rows = con.execute("select matched_row_ids, file_path, document_kind, source_root, match_method, match_score from local_document_file").fetchall()
    by_doc: Dict[str, List[str]] = defaultdict(list)
    for item in local_doc_rows:
        matched = re.split(r"[;,]\s*", clean(item["matched_row_ids"]))
        for row_id in matched:
            if row_id in ids:
                by_doc[row_id].append(
                    f"{item['source_root']}|{item['document_kind']}|{item['match_method']}|{item['match_score']}|{item['file_path']}"
                )
    for row_id, samples in by_doc.items():
        rows[row_id]["db_local_document_file_count"] = len(samples)
        rows[row_id]["db_local_document_file_samples"] = compact(samples, max_items=3, max_chars=900)
    note_source(audit, db_path, "sqlite:local_document_file.matched_row_ids", len(local_doc_rows), sum(len(v) for v in by_doc.values()), by_doc.keys())
    con.close()


def enrich_from_cdss_product_db(rows: Dict[str, Dict[str, Any]], audit: List[Dict[str, Any]]) -> None:
    db_path = Path("cdss_tn_prod/artifacts/cdss_tn.db")
    if not db_path.exists():
        note_source(audit, db_path, "sqlite_amm", 0, 0, [], "database missing")
        return
    amms = {norm(row.get("amm")): row_id for row_id, row in rows.items() if norm(row.get("amm"))}
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    scanned = con.execute("select count(*) from product_catalog").fetchone()[0]
    matched = 0
    matched_ids: set[str] = set()
    for item in con.execute(
        """
        select amm, product_id, canonical_product_id, source_dataset, source_reference,
               product_label, brand_name, dci_api, therapeutic_class, therapeutic_subclass,
               laboratory, indications_text, price_public_tnd, reimbursement_category,
               generic_princeps_biosimilar, veic_status, market_action, importer
        from product_catalog
        """
    ):
        row_id = amms.get(norm(item["amm"]))
        if not row_id:
            continue
        matched += 1
        matched_ids.add(row_id)
        rows[row_id].setdefault("cdss_product_db_match_count", 0)
        rows[row_id]["cdss_product_db_match_count"] = int(rows[row_id]["cdss_product_db_match_count"]) + 1
        for field in item.keys():
            if field == "amm":
                continue
            key = f"cdss_product_db_{field}"
            rows[row_id][key] = compact([rows[row_id].get(key, ""), item[field]], max_items=4, max_chars=900)
    note_source(audit, db_path, "sqlite:product_catalog.amm", scanned, matched, matched_ids)
    con.close()


def scan_all_row_level_csvs(missing_ids: set[str], missing_amms: set[str], audit: List[Dict[str, Any]], already_scanned: set[str]) -> None:
    skip_parts = {".venv", "api_cache", "rcp_texts", "local_document_ocr_texts"}
    for path in ROOT.rglob("*.csv"):
        rel_parts = set(path.relative_to(ROOT).parts)
        if rel_parts & skip_parts:
            continue
        if str(path) in already_scanned:
            continue
        try:
            with path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as handle:
                reader = csv.DictReader(handle)
                headers = [clean(h) for h in (reader.fieldnames or [])]
                if not headers:
                    note_source(audit, path, "csv_no_header", 0, 0, [], "no header")
                    continue
                row_id_header = next((h for h in headers if h.lower() == "row_id"), "")
                amm_header = next((h for h in headers if h.lower() == "amm"), "")
                if not row_id_header and not amm_header:
                    note_source(audit, path, "not_joinable_csv_no_row_id_or_amm", 0, 0, [], "not scanned row-by-row")
                    continue
                scanned = matched = 0
                matched_ids: set[str] = set()
                for raw in reader:
                    scanned += 1
                    row = {clean(k): clean(v) for k, v in raw.items()}
                    if row_id_header:
                        row_id = clean(row.get(row_id_header))
                        if row_id in missing_ids:
                            matched += 1
                            matched_ids.add(row_id)
                            continue
                    if amm_header:
                        amm = norm(row.get(amm_header))
                        if amm in missing_amms:
                            matched += 1
                note_source(audit, path, "generic_row_id_or_amm_scan", scanned, matched, matched_ids)
        except Exception as exc:
            note_source(audit, path, "scan_failed", 0, 0, [], clean(exc))


def main() -> int:
    if not MISSING_PATH.exists():
        raise SystemExit(f"Missing input not found: {MISSING_PATH}")

    missing_rows = read_csv_rows(MISSING_PATH)
    audit: List[Dict[str, Any]] = []
    rows: Dict[str, Dict[str, Any]] = {}
    for row in missing_rows:
        row_id = clean(row.get("row_id"))
        if not row_id:
            continue
        rows[row_id] = dict(row)

    missing_ids = set(rows)
    amm_to_row_ids: Dict[str, List[str]] = defaultdict(list)
    for row_id, row in rows.items():
        amm = norm(row.get("amm"))
        if amm:
            amm_to_row_ids[amm].append(row_id)
    missing_amms = set(amm_to_row_ids)

    scanned_sources: set[str] = set()

    def remember(path: Path) -> None:
        scanned_sources.add(str(path))

    list_amm = group_file_by_amm(
        Path("liste_amm.csv"),
        amm_to_row_ids,
        [
            "Nom",
            "Dosage",
            "Forme",
            "Présentation",
            "DCI",
            "Classe",
            "Sous Classe",
            "Laboratoire",
            "AMM",
            "Date AMM",
            "Conditionnement primaire",
            "Spécifocation Conditionnement primaire",
            "tableau",
            "Durée de conservation",
            "Indications",
            "G/P/B",
            "VEIC",
        ],
        audit,
        notes="Root AMM CSV is joined by AMM because its row order can differ from pipeline row_id order.",
    )
    remember(Path("liste_amm.csv"))
    for row_id, data in list_amm.items():
        rows[row_id]["list_amm_row_count"] = data.pop("row_count", "")
        for key, value in data.items():
            rows[row_id][f"list_amm_{key}"] = value

    medicaments = group_file_by_amm(
        Path("medicaments_all_data.csv"),
        amm_to_row_ids,
        [
            "nom",
            "dosage",
            "forme",
            "presentation",
            "nom_generique",
            "labo",
            "pays",
            "amm",
            "date_amm",
            "g_p",
            "detail_url",
            "rcp_url",
            "notice_url",
            "has_detail",
            "has_rcp",
            "has_notice",
        ],
        audit,
        notes="Root enriched catalog is joined by AMM.",
    )
    remember(Path("medicaments_all_data.csv"))
    for row_id, data in medicaments.items():
        rows[row_id]["catalog_row_count"] = data.pop("row_count", "")
        for key, value in data.items():
            rows[row_id][f"catalog_{key}"] = value

    row_sources = [
        (
            OUT_DIR / "local_document_coverage.csv",
            "localcov",
            [
                "local_rcp_available",
                "local_notice_available",
                "local_unknown_doc_available",
                "local_any_document_available",
                "local_dpm_rcp_by_filename",
                "local_mapped_downloaded_rcp_exists",
                "local_mapped_rcp_text_exists",
                "local_lab_sources",
                "local_lab_doc_kinds",
                "local_lab_match_methods",
                "local_lab_file_count_for_row",
                "mapped_rcp_verify_status",
            ],
        ),
        (
            OUT_DIR / "list_amm_pdf_document_coverage.csv",
            "pdfcov",
            [
                "pdf_any_covered",
                "dpm_rcp_pdf_available",
                "dpm_rcp_verify_status",
                "dpm_rcp_url",
                "dpm_rcp_pdf_path",
                "lab_pdf_available",
                "lab_sources",
                "lab_doc_kinds",
                "lab_match_methods",
                "lab_pdf_count",
                "lab_pdf_paths",
            ],
        ),
        (
            OUT_DIR / "medicine_document_availability.csv",
            "docavail",
            [
                "mapped_rcp_verify_status",
                "mapped_rcp_http_status",
                "mapped_verified_rcp_url",
                "catalog_rcp_url",
                "catalog_rcp_verify_status",
                "catalog_rcp_http_status",
                "catalog_verified_rcp_url",
                "catalog_rcp_verify_error",
                "catalog_notice_url",
                "catalog_notice_verify_status",
                "catalog_notice_http_status",
                "catalog_verified_notice_url",
                "catalog_notice_verify_error",
                "rcp_actual_available",
                "notice_actual_available",
                "actual_detail_available",
                "lab_document_detected_by_amm",
                "lab_document_sources",
                "actual_or_lab_available",
                "rcp_link_404",
                "notice_link_404",
                "any_link_404",
            ],
        ),
        (
            OUT_DIR / "automatic_prescription_missing_details.csv",
            "ap_missing",
            [
                "automatic_prescription_readiness",
                "gap_flags",
                "local_any_document_available",
                "local_rcp_available",
                "local_notice_available",
                "dpm_rcp_text_available",
                "cdss_product_available",
                "indication_rule_count",
                "dosage_rule_count",
                "contraindication_rule_count",
                "interaction_rule_count",
                "adverse_effect_count",
                "renal_hepatic_adjustment_count",
                "special_population_rule_count",
                "substitution_rule_count",
            ],
        ),
        (
            OUT_DIR / "automatic_prescription_safety_gap_queue.csv",
            "safety_gap",
            [
                "automatic_prescription_readiness",
                "gap_flags",
                "next_action",
                "cdss_product_id",
                "cdss_match_type",
                "local_any_document_available",
                "ingredient_link_count",
                "indication_rule_count",
                "dosage_rule_count",
                "contraindication_rule_count",
                "interaction_rule_count",
                "adverse_effect_count",
                "renal_hepatic_adjustment_count",
                "special_population_rule_count",
                "substitution_rule_count",
            ],
        ),
        (
            OUT_DIR / "automatic_prescription_cdss_unmatched.csv",
            "cdss_unmatched",
            [
                "cdss_product_id",
                "cdss_product_label",
                "cdss_dci_api",
                "cdss_therapeutic_class",
                "cdss_therapeutic_subclass",
                "cdss_reimbursement_category",
                "cdss_match_type",
                "cdss_match_score",
                "ingredient_link_count",
                "automatic_prescription_readiness",
                "gap_flags",
            ],
        ),
        (
            OUT_DIR / "automatic_prescription_bdpm_fallback_candidates.csv",
            "bdpm_candidates",
            [
                "bdpm_dci_all_parts_found",
                "bdpm_dci_part_hits",
                "bdpm_dci_parts",
                "bdpm_product_name_exact",
            ],
        ),
        (
            OUT_DIR / "bdpm_fallback_candidate_map.csv",
            "bdpm_map",
            [
                "bdpm_dci_all_parts_found",
                "bdpm_dci_part_hits",
                "bdpm_dci_parts",
                "bdpm_product_name_exact",
                "candidate_strength",
                "candidate_confidence",
                "next_action",
            ],
        ),
        (
            OUT_DIR / "bdpm_live_query_queue.csv",
            "bdpm_queue",
            [
                "priority",
                "bdpm_candidate_available",
                "bdpm_candidate_strength",
                "query_primary",
                "query_brand",
                "query_generic",
                "recommended_endpoint",
                "fallback_after_bdpm",
            ],
        ),
        (
            OUT_DIR / "bdpm_api_match_results.csv",
            "bdpm_match",
            [
                "query",
                "status",
                "match_score",
                "source_record_id",
                "source_name",
                "raw_file",
                "message",
            ],
        ),
        (
            OUT_DIR / "eu_uk_live_fallback_query_status.csv",
            "eu_uk_status",
            [
                "chosen_source",
                "chosen_query",
                "chosen_record",
                "sections",
                "status",
            ],
        ),
        (
            OUT_DIR / "cache_missing_detail_overlap.csv",
            "cache_overlap",
            [
                "readiness",
                "gap_flags",
                "cache_label_brand_hit",
                "cache_label_generic_hit",
                "cache_terminology_brand_hit",
                "cache_terminology_generic_hit",
                "cache_any_hit",
            ],
        ),
        (
            OUT_DIR / "local_mapping_review_queue.csv",
            "local_review",
            [
                "match_method",
                "match_score",
                "source",
                "doc_kind",
                "pdf_path",
                "review_reason",
            ],
        ),
        (
            OUT_DIR / "local_document_recovery_status.csv",
            "local_recovery",
            [
                "status",
                "source_path",
                "text_chars",
                "error",
                "recommended_action",
                "local_authority_priority",
            ],
        ),
        (
            OUT_DIR / "local_document_ocr_status.csv",
            "ocr_status",
            [
                "status",
                "source_path",
                "text_path",
                "text_chars",
                "section_rows",
                "error",
            ],
        ),
    ]

    for path, prefix, fields in row_sources:
        remember(path)
        grouped = group_file_by_row_id(path, missing_ids, fields, audit)
        for row_id, data in grouped.items():
            rows[row_id][f"{prefix}_row_count"] = data.pop("row_count", "")
            for key, value in data.items():
                rows[row_id][f"{prefix}_{key}"] = value

    amm_sources = [
        (
            OUT_DIR / "cdss_tn_prod_medicaments_coverage.csv",
            "cdsscov",
            [
                "cdss_coverage_status",
                "cdss_raw_covered",
                "cdss_raw_match_type",
                "cdss_canonical_covered",
                "cdss_canonical_match_type",
                "cdss_canonical_product_id",
                "cdss_product_label",
                "cdss_source_datasets",
                "cdss_source_references",
                "cdss_dci_api",
                "cdss_therapeutic_class",
                "cdss_therapeutic_subclass",
                "cdss_indications_present",
                "cdss_price_public_tnd",
                "cdss_reimbursement_category",
                "cdss_rxnorm_mapping_status",
                "cdss_rxnorm_primary_rxcui",
                "cdss_rxnorm_primary_name",
                "cdss_rxnorm_primary_tty",
                "cdss_rxnorm_match_strategy",
                "cdss_rxnorm_match_confidence",
                "cdss_ingredient_match_count",
                "cdss_ingredient_total_count",
                "cdss_ingredient_rxcuis",
            ],
        ),
        (
            Path("cdss_tn_prod/data/derived/canonical_tunisia_rxnorm_master.csv"),
            "rxnorm_master",
            [
                "canonical_product_id",
                "source_datasets",
                "source_references",
                "product_label",
                "brand_name",
                "dci_api",
                "dci_api_rxnorm_en",
                "ingredient_parts_fr",
                "ingredient_parts_en",
                "ingredient_match_count",
                "ingredient_total_count",
                "ingredient_rxcuis",
                "ingredient_rxnorm_names",
                "ingredient_rxnorm_ttys",
                "rxnorm_primary_rxcui",
                "rxnorm_primary_name",
                "rxnorm_primary_tty",
                "rxnorm_primary_match_strategy",
                "rxnorm_primary_match_confidence",
                "rxnorm_mapping_status",
                "therapeutic_class",
                "therapeutic_subclass",
                "indications",
                "generic_princeps_biosimilar",
                "veic_status",
                "reimbursement_category",
                "price_public_tnd",
                "market_action",
                "importer",
            ],
        ),
        (
            Path("cdss_tn_prod/data/derived/canonical_tunisia_rxnorm_unmatched.csv"),
            "rxnorm_unmatched",
            [
                "canonical_product_id",
                "product_label",
                "dci_api",
                "ingredient_parts_fr",
                "ingredient_parts_en",
                "rxnorm_mapping_status",
                "therapeutic_class",
                "therapeutic_subclass",
                "indications",
            ],
        ),
        (
            Path("cdss_tn_prod/data/raw/tunisian_drugs/tunisia_drugs_all_sources_long_2026-03-30.csv"),
            "raw_tunisia_long",
            [
                "source_dataset",
                "source_reference",
                "product_label",
                "brand_name",
                "dci_api",
                "therapeutic_class",
                "therapeutic_subclass",
                "laboratory",
                "indications",
                "generic_princeps_biosimilar",
                "veic_status",
                "price_public_tnd",
                "tarif_reference_tnd",
                "reimbursement_category",
                "market_action",
                "importer",
            ],
        ),
    ]

    for path, prefix, fields in amm_sources:
        remember(path)
        grouped = group_file_by_amm(path, amm_to_row_ids, fields, audit)
        for row_id, data in grouped.items():
            rows[row_id][f"{prefix}_row_count"] = data.pop("row_count", "")
            for key, value in data.items():
                rows[row_id][f"{prefix}_{key}"] = value

    live_plan = group_file_by_row_id(
        OUT_DIR / "live_api_enrichment_plan.csv",
        missing_ids,
        ["priority", "stage", "source", "query", "endpoint_template", "run_condition"],
        audit,
        notes="Current plan regenerated after EU/UK rollup.",
    )
    remember(OUT_DIR / "live_api_enrichment_plan.csv")
    for row_id, data in live_plan.items():
        rows[row_id]["live_plan_row_count"] = data.pop("row_count", "")
        for key, value in data.items():
            rows[row_id][f"live_plan_{key}"] = value

    index_query_status(
        OUT_DIR / "bdpm_api_query_status.csv",
        ["status", "api_items", "row_count", "raw_file", "message"],
        rows,
        audit,
        "bdpm_query_status",
    )
    remember(OUT_DIR / "bdpm_api_query_status.csv")
    index_query_status(
        OUT_DIR / "us_live_fallback_query_status.csv",
        ["row_count", "chosen_source", "sections_per_row", "source_record_id", "openfda_status", "dailymed_status", "message"],
        rows,
        audit,
        "us_live_status",
    )
    remember(OUT_DIR / "us_live_fallback_query_status.csv")

    for column_name, path in SECTION_FILES.items():
        remember(path)
        add_count_from_row_id_file(path, missing_ids, rows, audit, column_name, ["source_system", "section_kind", "section_title"])

    add_count_from_row_id_file(
        OUT_DIR / "fallback_terminology_map.csv",
        missing_ids,
        rows,
        audit,
        "fallback_terminology_rows",
        ["source_system", "match_type", "rxcui", "source_name", "normalized_name"],
    )
    remember(OUT_DIR / "fallback_terminology_map.csv")
    add_count_from_row_id_file(
        OUT_DIR / "fallback_drug_class.csv",
        missing_ids,
        rows,
        audit,
        "fallback_drug_class_rows",
        ["source_system", "drug_name", "class_name", "relationship"],
    )
    remember(OUT_DIR / "fallback_drug_class.csv")

    enrich_from_sqlite(rows, audit)
    enrich_from_cdss_product_db(rows, audit)
    scan_local_files(rows, audit)
    scan_all_row_level_csvs(missing_ids, missing_amms, audit, scanned_sources)

    # Compact recommendation/status columns for rapid review.
    for row_id, row in rows.items():
        fill_map = {
            "nom_generique": ["catalog_nom_generique", "list_amm_DCI", "rxnorm_master_dci_api", "raw_tunisia_long_dci_api"],
            "dosage": ["catalog_dosage", "list_amm_Dosage"],
            "forme": ["catalog_forme", "list_amm_Forme"],
            "presentation": ["catalog_presentation", "list_amm_Présentation"],
            "date_amm": ["catalog_date_amm", "list_amm_Date AMM"],
            "g_p": ["catalog_g_p", "list_amm_G/P/B"],
            "labo": ["catalog_labo", "list_amm_Laboratoire"],
            "pays": ["catalog_pays"],
        }
        for target, candidates in fill_map.items():
            if clean(row.get(target)):
                continue
            for candidate in candidates:
                value = clean(row.get(candidate))
                if value:
                    row[target] = value
                    break

        usable_counts = sum(
            int(row.get(col, 0) or 0)
            for col in (
                "fallback_label_sections",
                "bdpm_api_sections",
                "openfda_live_sections",
                "eu_uk_live_sections",
                "global_regulatory_sections",
                "rcp_sections",
                "lab_sections",
                "local_recovered_sections",
            )
        )
        row["usable_section_rows_found_after_crosscheck"] = usable_counts
        row["has_non_section_metadata"] = "yes" if any(
            clean(row.get(col))
            for col in (
                "list_amm_DCI",
                "list_amm_Classe",
                "db_profile_cdss_product_id",
                "rxnorm_master_rxnorm_primary_rxcui",
                "fallback_terminology_rows",
                "fallback_drug_class_rows",
            )
        ) else "no"
        if usable_counts:
            row["remaining_review_status"] = "needs_summary_recheck_usable_sections_found"
        elif int(row.get("fallback_terminology_rows", 0) or 0) or int(row.get("fallback_drug_class_rows", 0) or 0):
            row["remaining_review_status"] = "metadata_only_no_clinical_sections"
        elif row.get("bdpm_match_status") or row.get("eu_uk_status_status") or row.get("us_live_status_chosen_source"):
            row["remaining_review_status"] = "queried_no_usable_sections"
        else:
            row["remaining_review_status"] = "catalog_only_or_unqueried"

    preferred = [
        "row_id",
        "amm",
        "nom",
        "nom_generique",
        "dosage",
        "forme",
        "presentation",
        "labo",
        "pays",
        "evidence_status",
        "remaining_review_status",
        "usable_section_rows_found_after_crosscheck",
        "has_non_section_metadata",
        "fallback_terminology_rows",
        "fallback_drug_class_rows",
        "fallback_label_sections",
        "bdpm_api_sections",
        "openfda_live_sections",
        "eu_uk_live_sections",
        "global_regulatory_sections",
        "rcp_sections",
        "lab_sections",
        "local_recovered_sections",
        "filesystem_local_file_hit_count",
        "filesystem_local_file_hit_samples",
    ]
    all_fields: List[str] = []
    for field in preferred:
        if field not in all_fields:
            all_fields.append(field)
    for row in rows.values():
        for field in row.keys():
            if field not in all_fields:
                all_fields.append(field)

    ordered_rows = [rows[row_id] for row_id in sorted(rows, key=lambda x: int(x) if x.isdigit() else x)]
    write_csv(DETAILS_OUT, all_fields, ordered_rows)
    write_csv(
        SOURCE_AUDIT_OUT,
        ["source_path", "exists", "join_method", "scanned_rows", "matched_rows", "matched_unique_row_ids", "notes"],
        audit,
    )
    current_details_out, current_source_audit_out, current_summary_out = current_remaining_paths(len(ordered_rows))
    if current_details_out != DETAILS_OUT:
        write_csv(current_details_out, all_fields, ordered_rows)
    if current_source_audit_out != SOURCE_AUDIT_OUT:
        write_csv(
            current_source_audit_out,
            ["source_path", "exists", "join_method", "scanned_rows", "matched_rows", "matched_unique_row_ids", "notes"],
            audit,
        )

    status_counts = Counter(row.get("remaining_review_status", "") for row in ordered_rows)
    section_found_rows = [row for row in ordered_rows if int(row.get("usable_section_rows_found_after_crosscheck", 0) or 0) > 0]
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "remaining_input_rows": len(missing_rows),
        "remaining_output_rows": len(ordered_rows),
        "sources_audited": len(audit),
        "usable_section_rows_found_after_crosscheck": sum(int(row.get("usable_section_rows_found_after_crosscheck", 0) or 0) for row in ordered_rows),
        "row_ids_with_usable_sections_found_after_crosscheck": len(section_found_rows),
        "remaining_review_status_counts": dict(status_counts),
        "outputs": {
            "details": str(DETAILS_OUT),
            "source_audit": str(SOURCE_AUDIT_OUT),
            "summary": str(SUMMARY_OUT),
            "current_details": str(current_details_out),
            "current_source_audit": str(current_source_audit_out),
            "current_summary": str(current_summary_out),
        },
        "notes": [
            "Raw hashed cache JSON/TXT files are represented through normalized fallback CSVs; files without row_id/amm/query manifests cannot be safely joined directly.",
            "Package/test data under .venv and API cache payload directories are excluded from generic CSV scans unless normalized into row-level artifacts.",
        ],
    }
    SUMMARY_OUT.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    if current_summary_out != SUMMARY_OUT:
        current_summary_out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
