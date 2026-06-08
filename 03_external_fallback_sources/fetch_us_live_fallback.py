#!/usr/bin/env python3
"""
Fetch second-line US fallback labels for medicines still missing after BDPM.

Order:
1. openFDA drug label API by generic/DCI query.
2. DailyMed SPL API by drug_name, only when openFDA yields no sections.

The output file is compatible with summarize_treated_medicines.ps1 through
`dpm_live_out/openfda_live_fallback_sections.csv`.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


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

DAILYMED_TITLE_MAP = {
    "INDICATIONS AND USAGE": "indication",
    "DOSAGE AND ADMINISTRATION": "dosage",
    "CONTRAINDICATIONS": "contraindication",
    "WARNINGS": "warning",
    "WARNINGS AND PRECAUTIONS": "warning",
    "DRUG INTERACTIONS": "interaction",
    "ADVERSE REACTIONS": "adverse_effect",
    "USE IN SPECIFIC POPULATIONS": "special_population",
    "PREGNANCY": "special_population",
    "PEDIATRIC USE": "special_population",
    "GERIATRIC USE": "special_population",
    "OVERDOSAGE": "overdose",
    "CLINICAL PHARMACOLOGY": "pharmacology",
}

OUTPUT_FIELDS = [
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


def clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = "\n".join(clean(item) for item in value)
    text = str(value).encode("utf-8", "ignore").decode("utf-8", "ignore")
    fixes = {"Â": " ", "â€™": "'", "â€œ": '"', "â€": '"', "â€“": "-", "â€”": "-", "â€¢": "-", "â– ": "-"}
    for bad, good in fixes.items():
        text = text.replace(bad, good)
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


def sha(*parts: Any) -> str:
    return hashlib.sha1("|".join(clean(part) for part in parts).encode("utf-8", "ignore")).hexdigest()


def safe_filename(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", clean(value)).strip("_")[:80]
    return slug or "query"


def cache_path(raw_dir: Path, source: str, query: str, suffix: str) -> Path:
    digest = sha(source, query)[:16]
    return raw_dir / source / f"query_{digest}_{safe_filename(query)}.{suffix}"


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def request_bytes(url: str, timeout: int) -> Tuple[str, str, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "tunisia-cdss-data-remediation/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return "ok", "", response.read()
    except urllib.error.HTTPError as exc:
        return f"http_{exc.code}", clean(exc), b""
    except Exception as exc:
        return "error", clean(exc), b""


def append_api_key(url: str, api_key: str) -> str:
    if not api_key:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}api_key={urllib.parse.quote(api_key)}"


def load_json(path: Path) -> Optional[Any]:
    try:
        if path.exists() and path.stat().st_size > 0:
            return json.loads(path.read_text(encoding="utf-8-sig", errors="ignore"))
    except Exception:
        return None
    return None


def load_text(path: Path) -> str:
    try:
        if path.exists() and path.stat().st_size > 0:
            return path.read_text(encoding="utf-8-sig", errors="ignore")
    except Exception:
        return ""
    return ""


def fetch_json(url: str, raw_path: Path, timeout: int, resume: bool) -> Tuple[str, str, Any, bool]:
    if resume:
        cached = load_json(raw_path)
        if cached is not None:
            return "cached", "", cached, True
    status, message, body = request_bytes(url, timeout)
    if status == "ok":
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(body)
        return status, message, json.loads(body.decode("utf-8", "ignore")), False
    return status, message, {}, False


def fetch_text(url: str, raw_path: Path, timeout: int, resume: bool) -> Tuple[str, str, str, bool]:
    if resume:
        cached = load_text(raw_path)
        if cached:
            return "cached", "", cached, True
    status, message, body = request_bytes(url, timeout)
    if status == "ok":
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(body)
        return status, message, body.decode("utf-8", "ignore"), False
    return status, message, "", False


def openfda_sections(query: str, data: Any, raw_path: Path) -> Tuple[str, List[Dict[str, Any]]]:
    rows: List[Dict[str, Any]] = []
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or not results:
        return "", rows
    record = results[0]
    record_id = clean(record.get("id") or record.get("set_id") or "")
    for key, (kind, title) in SECTION_MAP.items():
        text = clean(record.get(key))
        if not text:
            continue
        rows.append(
            {
                "source_system": "openfda_live_label",
                "source_file": str(raw_path),
                "source_record_id": record_id,
                "match_query": query,
                "section_kind": kind,
                "section_title": title,
                "section_text": text,
                "language": "en",
                "authority_level": "fallback_openfda_live",
                "confidence": "0.68",
                "evidence_rank": "68",
            }
        )
    return record_id, rows


def xml_text(element: ET.Element) -> str:
    return clean(" ".join(part for part in element.itertext() if clean(part)))


def direct_child_text(section: ET.Element) -> str:
    parts: List[str] = []
    skip_tags = {"component", "subject"}
    for child in list(section):
        tag = child.tag.split("}")[-1]
        if tag in skip_tags or tag == "title":
            continue
        parts.append(xml_text(child))
    return clean(" ".join(part for part in parts if part))


def dailymed_sections(query: str, xml_text_value: str, raw_path: Path, setid: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_text_value)
    except Exception:
        return rows
    seen_kinds = set()

    def walk(parent: ET.Element) -> None:
        for section in list(parent):
            if section.tag.split("}")[-1] != "section":
                walk(section)
                continue
        if section.tag.split("}")[-1] != "section":
            
            title = ""
        for child in list(section):
            if child.tag.split("}")[-1] == "title":
                title = xml_text(child)
                break
        title_norm = norm(title)
        kind = ""
        for known_title, known_kind in DAILYMED_TITLE_MAP.items():
            if known_title in title_norm:
                kind = known_kind
                break
        if kind and kind not in seen_kinds:
            body = direct_child_text(section) or xml_text(section)
            if len(body) >= 80:
                seen_kinds.add(kind)
                rows.append(
                    {
                        "source_system": "dailymed_live_spl",
                        "source_file": str(raw_path),
                        "source_record_id": setid,
                        "match_query": query,
                        "section_kind": kind,
                        "section_title": title or kind,
                        "section_text": body,
                        "language": "en",
                        "authority_level": "fallback_dailymed_live",
                        "confidence": "0.64",
                        "evidence_rank": "66",
                    }
                )
        walk(section)

    walk(root)
    return rows


def openfda_search_urls(query: str, raw_dir: Path, api_key: str) -> List[Tuple[str, Path, str]]:
    escaped = query.replace('"', r"\"")
    strategies = [
        ("generic_exact", f'openfda.generic_name:"{escaped}"'),
        ("generic_wildcard", f"openfda.generic_name:{query}*"),
        ("substance", f'openfda.substance_name:"{escaped}"'),
        ("active_ingredient", f'active_ingredient:"{escaped}"'),
        ("brand", f'openfda.brand_name:"{escaped}"'),
    ]
    urls: List[Tuple[str, Path, str]] = []
    for strategy, search in strategies:
        url = f"https://api.fda.gov/drug/label.json?search={urllib.parse.quote(search)}&limit=1"
        urls.append((append_api_key(url, api_key), cache_path(raw_dir, f"openfda_{strategy}", query, "json"), strategy))
    return urls


def apply_to_row(base_sections: List[Dict[str, Any]], row: Dict[str, str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for section in base_sections:
        section_row = dict(section)
        section_row.update(
            {
                "row_id": row.get("row_id", ""),
                "amm": row.get("amm", ""),
                "nom": row.get("nom", ""),
                "nom_generique": row.get("nom_generique", ""),
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        section_row["content_hash"] = sha(row.get("row_id", ""), section.get("source_system", ""), section.get("section_kind", ""), section.get("section_text", "")[:500])
        out.append(section_row)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", default="dpm_live_out/bdpm_live_query_queue.csv")
    parser.add_argument("--raw-dir", default="dpm_live_out/api_cache/us_live")
    parser.add_argument("--output", default="dpm_live_out/openfda_live_fallback_sections.csv")
    parser.add_argument("--query-status-output", default="dpm_live_out/us_live_fallback_query_status.csv")
    parser.add_argument("--summary", default="dpm_live_out/us_live_fallback_summary.json")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.35)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--pagesize", type=int, default=3)
    parser.add_argument("--resume", action="store_true", default=True)
    parser.add_argument("--openfda-api-key", default="", help="Optional openFDA API key for higher rate limits.")
    args = parser.parse_args()

    queue_rows = read_csv(Path(args.queue))
    if args.limit > 0:
        queue_rows = queue_rows[: args.limit]
    rows_by_query: Dict[str, List[Dict[str, str]]] = {}
    query_by_key: Dict[str, str] = {}
    for row in queue_rows:
        query = clean(row.get("query_generic") or row.get("query_primary") or row.get("nom_generique") or row.get("nom"))
        if not query:
            continue
        key = query.upper()
        rows_by_query.setdefault(key, []).append(row)
        query_by_key.setdefault(key, query)

    raw_dir = Path(args.raw_dir)
    section_rows: List[Dict[str, Any]] = []
    status_rows: List[Dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    started = time.time()
    log(f"Starting US live fallback: {len(queue_rows)} rows, {len(rows_by_query)} unique queries")

    for idx, key in enumerate(sorted(rows_by_query.keys()), start=1):
        query = query_by_key[key]
        source = "openfda"
        openfda_status = ""
        openfda_msg = ""
        openfda_strategy = ""
        source_record_id = ""
        query_sections: List[Dict[str, Any]] = []
        for openfda_url, openfda_raw, strategy in openfda_search_urls(query, raw_dir, args.openfda_api_key):
            openfda_status, openfda_msg, openfda_data, _ = fetch_json(openfda_url, openfda_raw, args.timeout, args.resume)
            status_counts[f"openfda_{strategy}_{openfda_status}"] += 1
            source_record_id, query_sections = openfda_sections(query, openfda_data, openfda_raw)
            if query_sections:
                openfda_strategy = strategy
                break

        dailymed_status = ""
        dailymed_msg = ""
        if not query_sections:
            source = "dailymed"
            spls_raw = cache_path(raw_dir, "dailymed_search", query, "json")
            spls_url = (
                "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json?"
                f"drug_name={urllib.parse.quote(query)}&name_type=both&pagesize={args.pagesize}"
            )
            dailymed_status, dailymed_msg, spls_data, _ = fetch_json(spls_url, spls_raw, args.timeout, args.resume)
            status_counts[f"dailymed_search_{dailymed_status}"] += 1
            candidates = spls_data.get("data") if isinstance(spls_data, dict) else None
            if isinstance(candidates, list) and candidates:
                setid = clean(candidates[0].get("setid", ""))
                if setid:
                    xml_raw = raw_dir / "dailymed_spl" / f"{setid}.xml"
                    xml_url = f"https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/{urllib.parse.quote(setid)}.xml"
                    xml_status, dailymed_msg, xml_value, _ = fetch_text(xml_url, xml_raw, args.timeout, args.resume)
                    status_counts[f"dailymed_xml_{xml_status}"] += 1
                    if xml_status in {"ok", "cached"}:
                        source_record_id = setid
                        query_sections = dailymed_sections(query, xml_value, xml_raw, setid)

        if query_sections:
            source_counts[source] += 1
            for row in rows_by_query[key]:
                section_rows.extend(apply_to_row(query_sections, row))
        status_rows.append(
            {
                "query": query,
                "row_count": len(rows_by_query[key]),
                "chosen_source": source if query_sections else "",
                "sections_per_row": len(query_sections),
                "source_record_id": source_record_id,
                "openfda_status": openfda_status,
                "openfda_strategy": openfda_strategy,
                "dailymed_status": dailymed_status,
                "message": openfda_msg or dailymed_msg,
            }
        )
        if idx == 1 or idx % 10 == 0 or idx == len(rows_by_query):
            elapsed = time.time() - started
            rate = idx / elapsed if elapsed > 0 else 0
            eta = (len(rows_by_query) - idx) / rate if rate > 0 else 0
            log(
                f"query {idx}/{len(rows_by_query)} | '{query}' | source={source if query_sections else 'none'} | "
                f"sections_per_row={len(query_sections)} | rows_with_sections={len({r['row_id'] for r in section_rows})} | "
                f"elapsed={elapsed/60:.1f}m | eta={eta/60:.1f}m"
            )
        if args.sleep > 0:
            time.sleep(args.sleep)

    write_csv(Path(args.output), OUTPUT_FIELDS, section_rows)
    write_csv(Path(args.query_status_output), ["query", "row_count", "chosen_source", "sections_per_row", "source_record_id", "openfda_status", "openfda_strategy", "dailymed_status", "message"], status_rows)
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "queue_rows_processed": len(queue_rows),
        "unique_queries": len(rows_by_query),
        "section_rows": len(section_rows),
        "row_ids_with_us_live_sections": len({row["row_id"] for row in section_rows}),
        "source_query_counts": dict(source_counts),
        "status_counts": dict(status_counts),
        "outputs": {"sections": args.output, "query_status": args.query_status_output, "raw_dir": args.raw_dir, "summary": args.summary},
    }
    Path(args.summary).write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
