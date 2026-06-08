#!/usr/bin/env python3
"""
Fetch openFDA label fallback data after BDPM fallback has been attempted.

This script is intentionally second-line. It should be run for rows still missing after
local Tunisia evidence and BDPM fallback, then optionally followed by DailyMed SPL lookup.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence


SECTION_MAP = {
    "indications_and_usage": ("indication", "Indications and usage"),
    "dosage_and_administration": ("dosage", "Dosage and administration"),
    "contraindications": ("contraindication", "Contraindications"),
    "warnings_and_precautions": ("warning", "Warnings and precautions"),
    "warnings": ("warning", "Warnings"),
    "boxed_warning": ("warning", "Boxed warning"),
    "drug_interactions": ("interaction", "Drug interactions"),
    "adverse_reactions": ("adverse_effect", "Adverse reactions"),
    "use_in_specific_populations": ("special_population", "Use in specific populations"),
    "pregnancy": ("special_population", "Pregnancy"),
    "pediatric_use": ("special_population", "Pediatric use"),
    "geriatric_use": ("special_population", "Geriatric use"),
    "overdosage": ("overdose", "Overdosage"),
    "clinical_pharmacology": ("pharmacology", "Clinical pharmacology"),
}


def clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = "\n".join(clean(item) for item in value)
    text = str(value).encode("utf-8", "ignore").decode("utf-8", "ignore")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
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


def row_hash(*parts: Any) -> str:
    return hashlib.sha1("|".join(clean(part) for part in parts).encode("utf-8", "ignore")).hexdigest()


def safe_filename(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", clean(value)).strip("_")[:80]
    return slug or "query"


def request_json(url: str, timeout: int) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "tunisia-cdss-data-remediation/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read()
    return json.loads(body.decode("utf-8", "ignore"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", default="dpm_live_out/bdpm_live_query_queue.csv")
    parser.add_argument("--raw-dir", default="dpm_live_out/api_cache/openfda")
    parser.add_argument("--output", default="dpm_live_out/openfda_live_fallback_sections.csv")
    parser.add_argument("--status-output", default="dpm_live_out/openfda_live_fallback_status.csv")
    parser.add_argument("--summary", default="dpm_live_out/openfda_live_fallback_summary.json")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.35)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    rows = read_csv(Path(args.queue))
    if args.limit > 0:
        rows = rows[: args.limit]
    raw_dir = Path(args.raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)

    section_rows: List[Dict[str, Any]] = []
    status_rows: List[Dict[str, Any]] = []
    status_counts: Dict[str, int] = {}

    for idx, row in enumerate(rows, start=1):
        query = clean(row.get("query_generic") or row.get("query_primary") or row.get("nom_generique") or row.get("nom"))
        search = f'openfda.generic_name:"{query}"'
        url = f"https://api.fda.gov/drug/label.json?search={urllib.parse.quote(search)}&limit=1"
        raw_path = raw_dir / f"{clean(row.get('row_id'))}_{safe_filename(query)}.json"
        status = "ok"
        message = ""
        data: Dict[str, Any] = {}
        try:
            data = request_json(url, args.timeout)
            raw_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            status = "error"
            message = clean(exc)
        status_counts[status] = status_counts.get(status, 0) + 1

        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list) or not results:
            status_rows.append({**row, "status": status, "query": query, "raw_file": str(raw_path) if status == "ok" else "", "message": message or "No openFDA label result"})
        else:
            record = results[0]
            source_record_id = clean(record.get("id") or record.get("set_id") or "")
            count = 0
            for key, (kind, title) in SECTION_MAP.items():
                text = clean(record.get(key))
                if not text:
                    continue
                count += 1
                section_rows.append(
                    {
                        "row_id": row.get("row_id", ""),
                        "amm": row.get("amm", ""),
                        "nom": row.get("nom", ""),
                        "nom_generique": row.get("nom_generique", ""),
                        "source_system": "openfda_live_label",
                        "source_file": str(raw_path),
                        "source_record_id": source_record_id,
                        "match_query": query,
                        "section_kind": kind,
                        "section_title": title,
                        "section_text": text,
                        "language": "en",
                        "authority_level": "fallback_openfda_live",
                        "confidence": "0.68",
                        "evidence_rank": "68",
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        "content_hash": row_hash(row.get("row_id", ""), kind, text[:500]),
                    }
                )
            status_rows.append({**row, "status": status, "query": query, "raw_file": str(raw_path), "message": f"sections={count}"})
        if idx < len(rows) and args.sleep > 0:
            time.sleep(args.sleep)

    fields = [
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
    ]
    write_csv(Path(args.output), fields, section_rows)
    write_csv(Path(args.status_output), list(status_rows[0].keys()) if status_rows else ["status"], status_rows)
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "queue_rows_processed": len(rows),
        "section_rows": len(section_rows),
        "row_ids_with_openfda_sections": len({row["row_id"] for row in section_rows}),
        "status_counts": status_counts,
        "outputs": {"sections": args.output, "status": args.status_output, "raw_dir": args.raw_dir, "summary": args.summary},
    }
    Path(args.summary).write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
