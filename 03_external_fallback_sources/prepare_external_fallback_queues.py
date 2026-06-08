#!/usr/bin/env python3
"""
Prepare external fallback queues for the remaining list_amm evidence gaps.

This script does not call the network. It turns the current missing rows, local BDPM
candidate hints, and failed local document statuses into auditable work queues.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence


CRITICAL_DCI = {
    "APIXABAN",
    "METHOTREXATE",
    "METHOTREXATE SODIUM",
    "RIVAROXABAN",
    "WARFARIN",
    "DIGOXIN",
    "LITHIUM",
    "CICLOSPORIN",
    "CYCLOSPORINE",
    "TACROLIMUS",
    "CLOZAPINE",
    "INSULIN",
    "FENTANYL",
    "MORPHINE",
    "AMIODARONE",
}


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).encode("utf-8", "ignore").decode("utf-8", "ignore")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def norm(value: Any) -> str:
    text = clean(value).upper()
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fields: Sequence[str], rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="", errors="ignore") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: clean(row.get(field, "")) for field in fields})


def amm_catalog(path: Path) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for idx, row in enumerate(read_csv(path), start=1):
        row = dict(row)
        row["row_id"] = str(idx)
        out[str(idx)] = row
    return out


def priority_for(row: Dict[str, str], bdpm: Dict[str, str]) -> str:
    dci_norm = norm(row.get("nom_generique", ""))
    if any(part in dci_norm.split() or part in dci_norm for part in CRITICAL_DCI):
        return "1_critical"
    if bdpm and bdpm.get("bdpm_dci_all_parts_found") == "True":
        return "2_bdpm_dci_complete"
    if bdpm:
        return "3_bdpm_candidate_partial"
    return "4_no_local_hint"


def query_term(row: Dict[str, str], fallback_name: str = "") -> str:
    generic = clean(row.get("nom_generique", ""))
    brand = clean(row.get("nom", fallback_name))
    if generic:
        return generic
    return brand


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default="medicaments_all_data.csv")
    parser.add_argument("--missing", default="dpm_live_out/treated_medicines_missing_all_local_evidence.csv")
    parser.add_argument("--bdpm-candidates", default="dpm_live_out/automatic_prescription_bdpm_fallback_candidates.csv")
    parser.add_argument("--rcp-status", default="dpm_live_out/rcp_section_extraction_status.csv")
    parser.add_argument("--bdpm-map-output", default="dpm_live_out/bdpm_fallback_candidate_map.csv")
    parser.add_argument("--bdpm-query-output", default="dpm_live_out/bdpm_live_query_queue.csv")
    parser.add_argument("--api-plan-output", default="dpm_live_out/live_api_enrichment_plan.csv")
    parser.add_argument("--recovery-output", default="dpm_live_out/local_document_recovery_queue.csv")
    parser.add_argument("--summary", default="dpm_live_out/external_fallback_queue_summary.json")
    args = parser.parse_args()

    catalog = amm_catalog(Path(args.catalog))
    missing_rows = read_csv(Path(args.missing))
    missing_ids = {clean(row.get("row_id")) for row in missing_rows}

    bdpm_by_row: Dict[str, Dict[str, str]] = {}
    for row in read_csv(Path(args.bdpm_candidates)):
        row_id = clean(row.get("row_id"))
        if row_id and row_id not in bdpm_by_row:
            bdpm_by_row[row_id] = row

    bdpm_map_rows: List[Dict[str, Any]] = []
    bdpm_query_rows: List[Dict[str, Any]] = []
    api_plan_rows: List[Dict[str, Any]] = []

    for missing in missing_rows:
        row_id = clean(missing.get("row_id"))
        cat = catalog.get(row_id, {})
        bdpm = bdpm_by_row.get(row_id, {})
        merged = {**cat, **missing}
        if cat.get("nom_generique"):
            merged["nom_generique"] = cat.get("nom_generique", "")
        if cat.get("dosage"):
            merged["dosage"] = cat.get("dosage", "")
        if cat.get("forme"):
            merged["forme"] = cat.get("forme", "")
        pri = priority_for(merged, bdpm)
        term = query_term(merged, missing.get("nom", ""))
        brand = clean(merged.get("nom", missing.get("nom", "")))

        if bdpm:
            all_parts = bdpm.get("bdpm_dci_all_parts_found") == "True"
            bdpm_map_rows.append(
                {
                    "row_id": row_id,
                    "amm": clean(merged.get("amm", "")),
                    "nom": brand,
                    "nom_generique": clean(merged.get("nom_generique", bdpm.get("nom_generique", ""))),
                    "dosage": clean(merged.get("dosage", bdpm.get("dosage", ""))),
                    "forme": clean(merged.get("forme", bdpm.get("forme", ""))),
                    "labo": clean(merged.get("labo", "")),
                    "bdpm_dci_all_parts_found": clean(bdpm.get("bdpm_dci_all_parts_found", "")),
                    "bdpm_dci_part_hits": clean(bdpm.get("bdpm_dci_part_hits", "")),
                    "bdpm_dci_parts": clean(bdpm.get("bdpm_dci_parts", "")),
                    "bdpm_product_name_exact": clean(bdpm.get("bdpm_product_name_exact", "")),
                    "candidate_strength": "strong_dci" if all_parts else "weak_partial",
                    "candidate_confidence": "0.78" if all_parts else "0.45",
                    "next_action": "query_bdpm_api_by_dci_then_brand",
                }
            )

        bdpm_query_rows.append(
            {
                "row_id": row_id,
                "amm": clean(merged.get("amm", "")),
                "nom": brand,
                "nom_generique": clean(merged.get("nom_generique", "")),
                "dosage": clean(merged.get("dosage", "")),
                "forme": clean(merged.get("forme", "")),
                "labo": clean(merged.get("labo", "")),
                "priority": pri,
                "bdpm_candidate_available": "yes" if bdpm else "no",
                "bdpm_candidate_strength": "strong_dci" if bdpm.get("bdpm_dci_all_parts_found") == "True" else ("weak_partial" if bdpm else ""),
                "query_primary": term,
                "query_brand": brand,
                "query_generic": clean(merged.get("nom_generique", "")),
                "recommended_endpoint": "/v1/medicaments?search={query}",
                "fallback_after_bdpm": "openfda_label_then_dailymed_spl_then_rxnorm_rxclass",
            }
        )

        for stage, source, endpoint in (
            ("1", "bdpm_api", "/v1/medicaments?search={query}"),
            ("2", "openfda_label", "https://api.fda.gov/drug/label.json?search=openfda.generic_name:{query}"),
            ("3", "dailymed_spl", "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json?drug_name={query}"),
            ("4", "rxnorm_rxclass", "https://rxnav.nlm.nih.gov/REST/rxcui.json?name={query}"),
        ):
            api_plan_rows.append(
                {
                    "row_id": row_id,
                    "amm": clean(merged.get("amm", "")),
                    "nom": brand,
                    "nom_generique": clean(merged.get("nom_generique", "")),
                    "priority": pri,
                    "stage": stage,
                    "source": source,
                    "query": term,
                    "endpoint_template": endpoint,
                    "run_condition": "run_if_previous_stage_has_no_label_sections" if stage != "1" else "always_first",
                }
            )

    recovery_statuses = {"not_a_pdf", "no_text_extracted", "extract_failed"}
    recovery_rows: List[Dict[str, Any]] = []
    for row in read_csv(Path(args.rcp_status)):
        status = clean(row.get("status"))
        if status not in recovery_statuses:
            continue
        row_id = clean(row.get("row_id"))
        cat = catalog.get(row_id, {})
        action = {
            "not_a_pdf": "inspect_html_and_extract_or_redownload",
            "no_text_extracted": "ocr_pdf_then_sectionize",
            "extract_failed": "retry_with_pdfplumber_or_pymupdf_then_ocr",
        }[status]
        recovery_rows.append(
            {
                "row_id": row_id,
                "amm": clean(row.get("amm", cat.get("amm", ""))),
                "nom": clean(row.get("nom", cat.get("nom", ""))),
                "nom_generique": clean(cat.get("nom_generique", "")),
                "status": status,
                "source_path": clean(row.get("source_path", "")),
                "text_chars": clean(row.get("text_chars", "")),
                "error": clean(row.get("error", "")),
                "recommended_action": action,
                "local_authority_priority": "high",
            }
        )

    write_csv(
        Path(args.bdpm_map_output),
        [
            "row_id",
            "amm",
            "nom",
            "nom_generique",
            "dosage",
            "forme",
            "labo",
            "bdpm_dci_all_parts_found",
            "bdpm_dci_part_hits",
            "bdpm_dci_parts",
            "bdpm_product_name_exact",
            "candidate_strength",
            "candidate_confidence",
            "next_action",
        ],
        bdpm_map_rows,
    )
    write_csv(
        Path(args.bdpm_query_output),
        [
            "row_id",
            "amm",
            "nom",
            "nom_generique",
            "dosage",
            "forme",
            "labo",
            "priority",
            "bdpm_candidate_available",
            "bdpm_candidate_strength",
            "query_primary",
            "query_brand",
            "query_generic",
            "recommended_endpoint",
            "fallback_after_bdpm",
        ],
        bdpm_query_rows,
    )
    write_csv(
        Path(args.api_plan_output),
        ["row_id", "amm", "nom", "nom_generique", "priority", "stage", "source", "query", "endpoint_template", "run_condition"],
        api_plan_rows,
    )
    write_csv(
        Path(args.recovery_output),
        ["row_id", "amm", "nom", "nom_generique", "status", "source_path", "text_chars", "error", "recommended_action", "local_authority_priority"],
        recovery_rows,
    )

    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "remaining_missing_rows": len(missing_rows),
        "bdpm_candidate_rows_total": len(read_csv(Path(args.bdpm_candidates))),
        "bdpm_candidates_still_missing": len(bdpm_map_rows),
        "bdpm_candidates_strong_dci": sum(1 for row in bdpm_map_rows if row["candidate_strength"] == "strong_dci"),
        "bdpm_live_query_rows": len(bdpm_query_rows),
        "api_plan_rows": len(api_plan_rows),
        "recovery_queue_rows": len(recovery_rows),
        "recovery_status_counts": dict(Counter(row["status"] for row in recovery_rows)),
        "priority_counts": dict(Counter(row["priority"] for row in bdpm_query_rows)),
        "outputs": {
            "bdpm_candidate_map": args.bdpm_map_output,
            "bdpm_live_query_queue": args.bdpm_query_output,
            "live_api_enrichment_plan": args.api_plan_output,
            "local_document_recovery_queue": args.recovery_output,
            "summary": args.summary,
        },
    }
    Path(args.summary).write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
