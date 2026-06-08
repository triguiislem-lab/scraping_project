#!/usr/bin/env python3
"""Build the final consolidated data release.

This script is intentionally conservative: it does not mutate any upstream run
outputs. It reads the latest normalized CSV artifacts, writes a compact release
folder, and records quality flags so support-only evidence is visible without
being confused with local/official RCP evidence.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence


SECTION_FIELDS = [
    "section_id",
    "row_id",
    "amm",
    "nom",
    "nom_generique",
    "source_system",
    "source_file",
    "source_record_id",
    "match_query",
    "section_kind",
    "section_title",
    "section_text",
    "language",
    "authority_level",
    "confidence",
    "evidence_rank",
    "retrieved_at",
    "content_hash",
    "quality_tier",
    "quality_flags",
    "accepted_for_clinical_use",
    "source_csv",
]

CLINICAL_SECTION_KINDS = {
    "indication",
    "dosage",
    "contraindication",
    "interaction",
    "warning",
    "adverse_effect",
    "special_population",
    "overdose",
    "pharmacology",
    "pharmacodynamic",
    "pharmacokinetic",
    "administration",
    "renal_hepatic_adjustment",
    "composition",
    "storage",
}

SOURCE_CONFIGS = [
    {
        "path": "dpm_live_out/rcp_section_extraction_candidates.csv",
        "source_system": "tunisia_dpm_local_rcp_pdf",
        "quality_tier": "A_local_official_rcp",
        "rank": "95",
        "language": "fr",
        "authority": "local_dpm_rcp",
    },
    {
        "path": "dpm_live_out/lab_document_section_candidates.csv",
        "source_system": "tunisia_lab_local_document",
        "quality_tier": "A_local_lab_document",
        "rank": "90",
        "language": "fr",
        "authority": "local_lab_document",
    },
    {
        "path": "dpm_live_out/local_document_recovered_sections.csv",
        "source_system": "local_ocr_html_recovered_document",
        "quality_tier": "A_local_recovered_document",
        "rank": "88",
        "language": "fr",
        "authority": "local_recovered_document",
    },
    {
        "path": "dpm_live_out/fallback_label_section.csv",
        "source_system": "",
        "quality_tier": "C_cached_us_label_fallback",
        "rank": "",
        "language": "",
        "authority": "",
    },
    {
        "path": "dpm_live_out/bdpm_api_fallback_sections.csv",
        "source_system": "bdpm_api_medicaments_fr",
        "quality_tier": "B_bdpm_fr_metadata",
        "rank": "58",
        "language": "fr",
        "authority": "fallback_bdpm_fr",
    },
    {
        "path": "dpm_live_out/openfda_live_fallback_sections.csv",
        "source_system": "openfda_live_label",
        "quality_tier": "B_us_official_label",
        "rank": "68",
        "language": "en",
        "authority": "fallback_openfda_live",
    },
    {
        "path": "dpm_live_out/eu_uk_live_fallback_sections.csv",
        "source_system": "",
        "quality_tier": "",
        "rank": "",
        "language": "en",
        "authority": "",
    },
    {
        "path": "dpm_live_out/global_regulatory_cima_cache_recovered_sections.csv",
        "source_system": "aemps_cima_ficha_tecnica",
        "quality_tier": "B_eu_regulatory_rcp",
        "rank": "74",
        "language": "es",
        "authority": "fallback_aemps_cima",
    },
    {
        "path": "dpm_live_out/swissmedic_aips_targeted_sections.csv",
        "source_system": "swissmedic_aips_professional_info",
        "quality_tier": "B_ch_regulatory_professional_info",
        "rank": "76",
        "language": "de/fr/it",
        "authority": "fallback_swissmedic_aips",
    },
    {
        "path": "dpm_live_out/who_vaccine_fallback_sections.csv",
        "source_system": "who_vaccine_product_information",
        "quality_tier": "B_who_vaccine_product_info",
        "rank": "74",
        "language": "en",
        "authority": "fallback_who_vaccine",
    },
    {
        "path": "dpm_live_out/global_regulatory_fallback_sections_cecmed_v3.csv",
        "source_system": "cecmed_rcp_pdf",
        "quality_tier": "B_foreign_regulatory_rcp",
        "rank": "70",
        "language": "es",
        "authority": "fallback_cecmed_rcp",
    },
    {
        "path": "dpm_live_out/global_regulatory_fallback_sections.csv",
        "source_system": "",
        "quality_tier": "C_global_regulatory_metadata",
        "rank": "50",
        "language": "",
        "authority": "",
    },
]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = " ".join(clean(item) for item in value)
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
    fixes = {
        "Ã©": "é",
        "Ã¨": "è",
        "Ãª": "ê",
        "Ã«": "ë",
        "Ã ": "à",
        "Ã¢": "â",
        "Ã§": "ç",
        "Ã®": "î",
        "Ã¯": "ï",
        "Ã´": "ô",
        "Ã¹": "ù",
        "Ã»": "û",
        "Ã¼": "ü",
        "Ã‰": "É",
        "Ã€": "À",
        "Ã‡": "Ç",
        "Ã”": "Ô",
        "Ã–": "Ö",
        "Â°": "°",
        "Âµ": "µ",
        "Â": " ",
        "â– ": " ",
        "â‰¥": "≥",
        "â‰¤": "≤",
        "â€™": "'",
        "â€œ": '"',
        "â€": '"',
    }
    for bad, good in fixes.items():
        text = text.replace(bad, good)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9]+", " ", clean(value).upper())).strip()


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(clean(value)))
    except Exception:
        return default


def sha1(*parts: Any) -> str:
    payload = "|".join(clean(part) for part in parts)
    return hashlib.sha1(payload.encode("utf-8", "ignore")).hexdigest()


def read_csv(path: Path) -> Iterator[Dict[str, str]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8-sig", newline="", errors="ignore") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            yield dict(row)


def write_csv(path: Path, fields: Sequence[str], rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="", errors="ignore") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: clean(row.get(field, "")) for field in fields})


def jsonl_write(handle, row: Dict[str, Any]) -> None:
    handle.write(json.dumps({key: row.get(key, "") for key in row}, ensure_ascii=False) + "\n")


def source_quality(source_system: str, default_tier: str, default_rank: str) -> tuple[str, str]:
    if source_system == "emc_smpc_html":
        return "B_uk_regulatory_smpc", "63"
    if source_system == "chembl_ebi_api":
        return "D_support_pharmacology", "56"
    if source_system == "pubchem_annotations":
        return "D_support_pubchem_annotation", "53"
    if source_system == "bdpm_api_medicaments_fr":
        return "B_bdpm_fr_metadata", "58"
    if source_system == "openfda_live_label":
        return "B_us_official_label", "68"
    if source_system == "dailymed_spl":
        return "B_us_official_label", "72"
    return default_tier, default_rank


def readable_bdpm_text(section_kind: str, title: str, text_value: str) -> str:
    raw = clean(text_value)
    if not raw or raw[0] not in "[{":
        return raw
    try:
        payload = json.loads(raw)
    except Exception:
        return raw

    def flatten(value: Any, limit: int = 24) -> List[str]:
        out: List[str] = []

        def walk(item: Any) -> None:
            if len(out) >= limit or item in (None, "", [], {}):
                return
            if isinstance(item, dict):
                for key, nested in item.items():
                    if len(out) >= limit:
                        break
                    if isinstance(nested, (dict, list)):
                        walk(nested)
                    else:
                        text = clean(nested)
                        if text:
                            out.append(f"{clean(key)} : {text}")
            elif isinstance(item, list):
                for nested in item[:limit]:
                    walk(nested)
            else:
                text = clean(item)
                if text:
                    out.append(text)

        walk(value)
        return out

    if isinstance(payload, dict) and section_kind == "identity":
        parts = []
        if payload.get("name"):
            parts.append(f"Médicament : {clean(payload.get('name'))}.")
        if payload.get("cis"):
            parts.append(f"Code CIS : {clean(payload.get('cis'))}.")
        if payload.get("form"):
            parts.append(f"Forme pharmaceutique : {clean(payload.get('form'))}.")
        if payload.get("holder"):
            parts.append(f"Titulaire ou exploitant : {clean(payload.get('holder'))}.")
        if payload.get("status"):
            parts.append(f"Statut administratif : {clean(payload.get('status'))}.")
        return clean(" ".join(parts)) or raw

    values = flatten(payload)
    label = {
        "composition": "Composition BDPM",
        "presentation": "Présentation BDPM",
        "generic_group": "Groupe générique BDPM",
        "prescription_condition": "Conditions de prescription et de délivrance BDPM",
        "has_assessment": "Avis HAS / SMR / ASMR BDPM",
    }.get(section_kind, clean(title) or "Donnée BDPM")
    return clean(f"{label}. " + ". ".join(values) + ".") if values else raw


def quality_flags(row: Dict[str, str], source_system: str) -> tuple[str, bool]:
    flags: List[str] = []
    accepted = True
    section_text = clean(row.get("section_text"))
    section_kind = clean(row.get("section_kind"))
    if not section_text or len(section_text) < 20:
        flags.append("empty_or_too_short")
        accepted = False
    if source_system == "chembl_ebi_api" and norm(section_text) == "UNKNOWN":
        flags.append("noninformative_unknown_mechanism")
        accepted = False
    if source_system == "pubchem_annotations":
        query = norm(row.get("match_query"))
        brand = norm(row.get("nom"))
        generic = norm(row.get("nom_generique"))
        if query and brand and query == brand and query != generic:
            flags.append("brand_query_support_source_needs_review")
            accepted = False
    if source_system in {"pubchem_annotations", "chembl_ebi_api"}:
        flags.append("support_only_not_full_rcp")
    if section_kind and section_kind not in CLINICAL_SECTION_KINDS:
        flags.append("non_clinical_or_metadata_section")
    return ";".join(flags) if flags else "ok", accepted


def normalize_section_row(row: Dict[str, str], config: Dict[str, str], source_csv: str) -> Optional[Dict[str, Any]]:
    row_id = clean(row.get("row_id") or row.get("ROW_ID"))
    if not row_id:
        return None
    source_system = clean(row.get("source_system")) or config["source_system"]
    if not source_system:
        source_system = Path(source_csv).stem
    tier, rank = source_quality(source_system, config["quality_tier"], config["rank"])
    section_kind = clean(row.get("section_kind") or row.get("kind"))
    title = clean(row.get("section_title") or row.get("title") or row.get("section_code"))
    section_text = clean(row.get("section_text") or row.get("text"))
    if source_system == "bdpm_api_medicaments_fr":
        section_text = readable_bdpm_text(section_kind, title, section_text)
    if not section_text:
        return None
    content_hash = clean(row.get("content_hash")) or sha1(row_id, source_system, section_kind, section_text[:500])
    quality, accepted = quality_flags(
        {
            **row,
            "section_kind": section_kind,
            "section_text": section_text,
            "source_system": source_system,
        },
        source_system,
    )
    section_id = clean(row.get("section_id")) or f"ev_{content_hash[:24]}"
    return {
        "section_id": section_id,
        "row_id": row_id,
        "amm": clean(row.get("amm")),
        "nom": clean(row.get("nom")),
        "nom_generique": clean(row.get("nom_generique")),
        "source_system": source_system,
        "source_file": clean(row.get("source_file") or row.get("source_path")),
        "source_record_id": clean(row.get("source_record_id") or row.get("source_id")),
        "match_query": clean(row.get("match_query") or row.get("match_term")),
        "section_kind": section_kind,
        "section_title": title,
        "section_text": section_text,
        "language": clean(row.get("language")) or config["language"],
        "authority_level": clean(row.get("authority_level")) or config["authority"],
        "confidence": clean(row.get("confidence")) or "0.70",
        "evidence_rank": clean(rank or row.get("evidence_rank")),
        "retrieved_at": clean(row.get("retrieved_at")) or now_utc(),
        "content_hash": content_hash,
        "quality_tier": tier,
        "quality_flags": quality,
        "accepted_for_clinical_use": "1" if accepted else "0",
        "source_csv": source_csv,
    }


def load_medicines(list_amm: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for idx, row in enumerate(read_csv(list_amm), start=1):
        out = {key: clean(value) for key, value in row.items()}
        out["row_id"] = str(idx)
        rows.append(out)
    return rows


def table_columns(rows: Sequence[Dict[str, Any]]) -> List[str]:
    seen: set[str] = set()
    columns: List[str] = []
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    return columns


def create_sqlite(db_path: Path, medicine_rows: List[Dict[str, Any]], medicine_fields: List[str], section_csv: Path, manifest_rows: List[Dict[str, Any]], summary: Dict[str, Any]) -> None:
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute("PRAGMA temp_store=MEMORY")
        con.execute("CREATE TABLE medicines (" + ",".join(f'"{col}" TEXT' for col in medicine_fields) + ")")
        placeholders = ",".join("?" for _ in medicine_fields)
        con.executemany(
            "INSERT INTO medicines VALUES (" + placeholders + ")",
            ([clean(row.get(col, "")) for col in medicine_fields] for row in medicine_rows),
        )
        con.execute("CREATE TABLE evidence_sections (" + ",".join(f'"{col}" TEXT' for col in SECTION_FIELDS) + ")")
        batch: List[List[str]] = []
        for row in read_csv(section_csv):
            batch.append([clean(row.get(col, "")) for col in SECTION_FIELDS])
            if len(batch) >= 1000:
                con.executemany("INSERT INTO evidence_sections VALUES (" + ",".join("?" for _ in SECTION_FIELDS) + ")", batch)
                batch.clear()
        if batch:
            con.executemany("INSERT INTO evidence_sections VALUES (" + ",".join("?" for _ in SECTION_FIELDS) + ")", batch)
        con.execute("CREATE TABLE source_manifest (source_csv TEXT, exists_flag TEXT, size_bytes TEXT, rows_loaded TEXT, rows_accepted TEXT, rows_rejected TEXT)")
        con.executemany(
            "INSERT INTO source_manifest VALUES (?,?,?,?,?,?)",
            (
                [
                    clean(row.get("source_csv")),
                    clean(row.get("exists_flag")),
                    clean(row.get("size_bytes")),
                    clean(row.get("rows_loaded")),
                    clean(row.get("rows_accepted")),
                    clean(row.get("rows_rejected")),
                ]
                for row in manifest_rows
            ),
        )
        con.execute("CREATE TABLE release_summary_json (payload TEXT)")
        con.execute("INSERT INTO release_summary_json VALUES (?)", (json.dumps(summary, ensure_ascii=False, sort_keys=True),))
        con.executescript(
            """
            CREATE INDEX idx_medicines_row_id ON medicines(row_id);
            CREATE INDEX idx_sections_row_id ON evidence_sections(row_id);
            CREATE INDEX idx_sections_source ON evidence_sections(source_system);
            CREATE INDEX idx_sections_kind ON evidence_sections(section_kind);
            CREATE INDEX idx_sections_accepted ON evidence_sections(accepted_for_clinical_use);
            CREATE VIEW v_medicine_evidence_summary AS
            SELECT
              m.row_id,
              m.amm,
              m.nom,
              m.nom_generique,
              COUNT(e.section_id) AS evidence_section_rows,
              SUM(CASE WHEN e.accepted_for_clinical_use='1' THEN 1 ELSE 0 END) AS accepted_section_rows,
              MAX(CAST(CASE WHEN e.accepted_for_clinical_use='1' THEN e.evidence_rank ELSE '0' END AS INTEGER)) AS best_evidence_rank,
              GROUP_CONCAT(DISTINCT CASE WHEN e.accepted_for_clinical_use='1' THEN e.source_system END) AS accepted_sources
            FROM medicines m
            LEFT JOIN evidence_sections e ON e.row_id=m.row_id
            GROUP BY m.row_id;
            CREATE VIEW v_uncovered_medicines AS
            SELECT * FROM v_medicine_evidence_summary
            WHERE COALESCE(accepted_section_rows,0)=0
            ORDER BY nom;
            CREATE VIEW v_source_counts AS
            SELECT source_system, quality_tier, accepted_for_clinical_use, COUNT(*) AS section_rows, COUNT(DISTINCT row_id) AS medicine_rows
            FROM evidence_sections
            GROUP BY source_system, quality_tier, accepted_for_clinical_use
            ORDER BY accepted_for_clinical_use DESC, medicine_rows DESC, section_rows DESC;
            """
        )
        con.commit()
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-amm", default="medicaments_all_data.csv")
    parser.add_argument("--out-dir", default="dpm_live_out/final_data_release")
    parser.add_argument("--skip-large-cache-fallback", action="store_true", help="Exclude fallback_label_section.csv if disk/runtime is a concern.")
    args = parser.parse_args()

    csv.field_size_limit(min(sys.maxsize, 2_147_483_647))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    medicines = load_medicines(Path(args.list_amm))
    by_row = {row["row_id"]: row for row in medicines}

    sections_csv = out_dir / "final_evidence_sections.csv"
    sections_jsonl = out_dir / "final_evidence_sections.jsonl"
    manifest_rows: List[Dict[str, Any]] = []
    seen_hashes: set[str] = set()
    accepted_by_row: dict[str, int] = defaultdict(int)
    total_by_row: dict[str, int] = defaultdict(int)
    best_rank_by_row: dict[str, int] = defaultdict(int)
    best_source_by_row: dict[str, str] = {}
    source_counts: Counter[str] = Counter()
    source_accepted_counts: Counter[str] = Counter()
    source_rejected_counts: Counter[str] = Counter()
    quality_counts: Counter[str] = Counter()
    rejected_reason_counts: Counter[str] = Counter()

    with sections_csv.open("w", encoding="utf-8", newline="", errors="ignore") as csv_handle, sections_jsonl.open("w", encoding="utf-8", errors="ignore") as jsonl_handle:
        writer = csv.DictWriter(csv_handle, fieldnames=SECTION_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for config in SOURCE_CONFIGS:
            source_path = Path(config["path"])
            if args.skip_large_cache_fallback and source_path.name == "fallback_label_section.csv":
                continue
            loaded = accepted = rejected = 0
            exists = source_path.exists()
            if exists:
                for raw_row in read_csv(source_path):
                    loaded += 1
                    row = normalize_section_row(raw_row, config, str(source_path))
                    if row is None:
                        rejected += 1
                        continue
                    dedupe_key = row["content_hash"]
                    if dedupe_key in seen_hashes:
                        rejected += 1
                        rejected_reason_counts["duplicate_content_hash"] += 1
                        continue
                    seen_hashes.add(dedupe_key)
                    writer.writerow({field: clean(row.get(field, "")) for field in SECTION_FIELDS})
                    jsonl_write(jsonl_handle, row)
                    row_id = clean(row.get("row_id"))
                    total_by_row[row_id] += 1
                    source_counts[row["source_system"]] += 1
                    quality_counts[row["quality_tier"]] += 1
                    if row["accepted_for_clinical_use"] == "1":
                        accepted += 1
                        accepted_by_row[row_id] += 1
                        source_accepted_counts[row["source_system"]] += 1
                        rank = safe_int(row["evidence_rank"])
                        if rank > best_rank_by_row[row_id]:
                            best_rank_by_row[row_id] = rank
                            best_source_by_row[row_id] = clean(row["source_system"])
                    else:
                        rejected += 1
                        source_rejected_counts[row["source_system"]] += 1
                        for flag in clean(row["quality_flags"]).split(";"):
                            if flag and flag != "ok":
                                rejected_reason_counts[flag] += 1
            manifest_rows.append(
                {
                    "source_csv": str(source_path),
                    "exists_flag": "1" if exists else "0",
                    "size_bytes": source_path.stat().st_size if exists else 0,
                    "rows_loaded": loaded,
                    "rows_accepted": accepted,
                    "rows_rejected": rejected,
                }
            )

    for row in medicines:
        row_id = row["row_id"]
        row["final_evidence_section_rows"] = str(total_by_row.get(row_id, 0))
        row["final_accepted_section_rows"] = str(accepted_by_row.get(row_id, 0))
        row["final_best_evidence_rank"] = str(best_rank_by_row.get(row_id, 0))
        row["final_best_source_system"] = best_source_by_row.get(row_id, "")
        row["final_evidence_status"] = "covered" if accepted_by_row.get(row_id, 0) else "uncovered"

    medicine_fields = table_columns(medicines)
    medicines_csv = out_dir / "final_medicines.csv"
    medicines_json = out_dir / "final_medicines.json"
    medicines_jsonl = out_dir / "final_medicines.jsonl"
    uncovered_csv = out_dir / "final_uncovered_medicines.csv"
    manifest_csv = out_dir / "final_source_manifest.csv"
    summary_json = out_dir / "final_release_summary.json"
    db_path = out_dir / "final_data_release.db"

    write_csv(medicines_csv, medicine_fields, medicines)
    medicines_json.write_text(json.dumps(medicines, ensure_ascii=False, indent=2), encoding="utf-8")
    with medicines_jsonl.open("w", encoding="utf-8", errors="ignore") as handle:
        for row in medicines:
            jsonl_write(handle, row)
    write_csv(uncovered_csv, medicine_fields, [row for row in medicines if row["final_evidence_status"] == "uncovered"])
    write_csv(manifest_csv, ["source_csv", "exists_flag", "size_bytes", "rows_loaded", "rows_accepted", "rows_rejected"], manifest_rows)

    summary = {
        "created_at": now_utc(),
        "total_medicine_rows": len(medicines),
        "covered_medicine_rows": sum(1 for row in medicines if row["final_evidence_status"] == "covered"),
        "uncovered_medicine_rows": sum(1 for row in medicines if row["final_evidence_status"] == "uncovered"),
        "total_evidence_section_rows": sum(source_counts.values()),
        "accepted_evidence_section_rows": sum(source_accepted_counts.values()),
        "rejected_or_review_section_rows": sum(source_rejected_counts.values()),
        "source_counts": dict(source_counts),
        "source_accepted_counts": dict(source_accepted_counts),
        "source_rejected_counts": dict(source_rejected_counts),
        "quality_tier_counts": dict(quality_counts),
        "rejected_reason_counts": dict(rejected_reason_counts),
        "outputs": {
            "database": str(db_path),
            "medicines_csv": str(medicines_csv),
            "medicines_json": str(medicines_json),
            "medicines_jsonl": str(medicines_jsonl),
            "evidence_sections_csv": str(sections_csv),
            "evidence_sections_jsonl": str(sections_jsonl),
            "uncovered_csv": str(uncovered_csv),
            "source_manifest_csv": str(manifest_csv),
            "summary_json": str(summary_json),
        },
    }
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    create_sqlite(db_path, medicines, medicine_fields, sections_csv, manifest_rows, summary)

    readme = out_dir / "README_FINAL_DATA_FILES.md"
    readme.write_text(
        "\n".join(
            [
                "# Final Data Release",
                "",
                "Generated by `build_final_data_release.py`.",
                "",
                "## Primary Files",
                "",
                "- `final_data_release.db`: SQLite database with `medicines`, `evidence_sections`, `source_manifest`, and summary views.",
                "- `final_medicines.csv/json/jsonl`: one row per normalized `medicaments_all_data.csv` medicine, with final coverage fields.",
                "- `final_evidence_sections.csv/jsonl`: normalized evidence sections from local, BDPM, US, EU/UK, Swissmedic, WHO, and CECMED outputs.",
                "- `final_uncovered_medicines.csv`: medicines with no accepted clinical evidence section after the full pipeline.",
                "- `final_source_manifest.csv`: exact input files used and row counts.",
                "- `final_release_summary.json`: machine-readable release summary.",
                "",
                "Rows flagged as support-only or review-risk remain in `final_evidence_sections`, but `accepted_for_clinical_use=0` excludes them from final coverage counts.",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
