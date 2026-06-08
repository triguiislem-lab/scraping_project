#!/usr/bin/env python3
"""
Fetch BDPM/API Medicaments FR fallback data for queued Tunisia medicines.

Network script. It reads `bdpm_live_query_queue.csv`, calls the public API Medicaments
FR endpoint, stores raw JSON responses, and emits normalized fallback sections.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


BDPM_FILE_NAMES = {
    "cis": "CIS_bdpm.txt",
    "composition": "CIS_COMPO_bdpm.txt",
    "presentation": "CIS_CIP_bdpm.txt",
    "generique": "CIS_GENER_bdpm.txt",
    "condition": "CIS_CPD_bdpm.txt",
}

BDPM_COMPOSITION_COLUMNS = [
    "codeCIS",
    "designationElement",
    "codeSubstance",
    "denominationSubstance",
    "dosage",
    "referenceDosage",
    "natureComposant",
    "numeroLiaison",
]

BDPM_PRESENTATION_COLUMNS = [
    "codeCIS",
    "codeCIP7",
    "libelle",
    "statutAdministratif",
    "etatCommercialisation",
    "dateDeclarationCommercialisation",
    "codeCIP13",
    "agrementCollectivites",
    "tauxRemboursement",
    "prixHT",
    "indicationsRemboursement",
]

BDPM_GENERIQUE_COLUMNS = [
    "identifiantGroupe",
    "libelle",
    "codeCIS",
    "typeGenerique",
    "numeroTri",
]

BDPM_CONDITION_COLUMNS = [
    "codeCIS",
    "conditionPrescriptionDelivrance",
]


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).encode("utf-8", "ignore").decode("utf-8", "ignore")
    try:
        repaired = text.encode("latin-1").decode("utf-8")
        if repaired.count("�") <= text.count("�") and (
            repaired.count("é") + repaired.count("è") + repaired.count("à") + repaired.count("ç")
            >= text.count("é") + text.count("è") + text.count("à") + text.count("ç")
        ):
            text = repaired
    except Exception:
        pass
    mojibake_fixes = {
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
    for bad, good in mojibake_fixes.items():
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


def row_hash(*parts: Any) -> str:
    return hashlib.sha1("|".join(clean(part) for part in parts).encode("utf-8", "ignore")).hexdigest()


def safe_filename(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")[:80]
    return slug or "query"


def query_cache_path(raw_dir: Path, query: str) -> Path:
    digest = hashlib.sha1(clean(query).upper().encode("utf-8", "ignore")).hexdigest()[:16]
    return raw_dir / f"query_{digest}_{safe_filename(query)}.json"


def read_bdpm_txt(path: Path) -> List[List[str]]:
    if not path.exists():
        return []
    rows: List[List[str]] = []
    with path.open("r", encoding="latin-1", errors="ignore", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        for row in reader:
            rows.append([clean(cell) for cell in row])
    return rows


def rows_to_dicts(rows: List[List[str]], columns: Sequence[str]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for row in rows:
        padded = row + [""] * max(0, len(columns) - len(row))
        out.append({column: clean(padded[idx]) for idx, column in enumerate(columns)})
    return out


def load_bdpm_bulk_index(bulk_dir: Path) -> Dict[str, List[Dict[str, Any]]]:
    if not bulk_dir.exists():
        return {}
    cis_rows = read_bdpm_txt(bulk_dir / BDPM_FILE_NAMES["cis"])
    if not cis_rows:
        return {}

    aux_by_cis: Dict[str, Dict[str, List[Dict[str, str]]]] = {}
    aux_specs = [
        ("composition", BDPM_FILE_NAMES["composition"], BDPM_COMPOSITION_COLUMNS),
        ("presentation", BDPM_FILE_NAMES["presentation"], BDPM_PRESENTATION_COLUMNS),
        ("generique", BDPM_FILE_NAMES["generique"], BDPM_GENERIQUE_COLUMNS),
        ("condition", BDPM_FILE_NAMES["condition"], BDPM_CONDITION_COLUMNS),
    ]
    for key, filename, columns in aux_specs:
        for item in rows_to_dicts(read_bdpm_txt(bulk_dir / filename), columns):
            cis = clean(item.get("codeCIS", ""))
            if cis:
                aux_by_cis.setdefault(cis, {}).setdefault(key, []).append(item)

    records: List[Dict[str, Any]] = []
    for row in cis_rows:
        if len(row) < 2:
            continue
        cis = row[0]
        record = {
            "codeCIS": cis,
            "denomination": row[1] if len(row) > 1 else "",
            "formePharmaceutique": row[2] if len(row) > 2 else "",
            "voiesAdministration": row[3] if len(row) > 3 else "",
            "statutAdministratif": row[6] if len(row) > 6 else "",
            "typeProcedure": row[7] if len(row) > 7 else "",
            "etatCommercialisation": row[8] if len(row) > 8 else "",
            "dateAMM": row[10] if len(row) > 10 else "",
            "titulaire": row[12] if len(row) > 12 else "",
            "surveillanceRenforcee": row[13] if len(row) > 13 else "",
        }
        aux = aux_by_cis.get(cis, {})
        if aux.get("composition"):
            record["compositions"] = aux["composition"]
        if aux.get("presentation"):
            record["presentations"] = aux["presentation"]
        if aux.get("generique"):
            record["generiques"] = aux["generique"]
        if aux.get("condition"):
            record["conditionsPrescriptionDelivrance"] = aux["condition"]
        records.append(record)

    index: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        searchable = norm(" ".join(flatten_text(record, max_items=40)))
        for token in set(searchable.split()):
            if len(token) >= 4:
                index.setdefault(token, []).append(record)
    index["__records__"] = records
    return index


def query_bdpm_bulk(query: str, bulk_index: Dict[str, List[Dict[str, Any]]], limit: int = 50) -> List[Dict[str, Any]]:
    if not bulk_index:
        return []
    query_norm = norm(query)
    tokens = [token for token in query_norm.split() if len(token) >= 4]
    if not tokens:
        return []
    candidates: Dict[str, Dict[str, Any]] = {}
    for token in tokens:
        for record in bulk_index.get(token, [])[:500]:
            cis = clean(record.get("codeCIS"))
            if cis:
                candidates[cis] = record
    scored = []
    for record in candidates.values():
        searchable = norm(" ".join(flatten_text(record, max_items=40)))
        overlap = sum(1 for token in tokens if token in searchable)
        normalized_score = overlap / len(tokens)
        exact_bonus = 0.2 if query_norm in searchable else 0.0
        if overlap:
            scored.append((normalized_score + exact_bonus, record))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [record for _score, record in scored[:limit]]


def request_json(url: str, timeout: int) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "tunisia-cdss-data-remediation/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read()
    return json.loads(body.decode("utf-8", "ignore"))


def request_json_with_retries(url: str, timeout: int, max_retries: int, retry_sleep: float) -> tuple[str, str, Any]:
    attempt = 0
    while True:
        try:
            return "ok", "", request_json(url, timeout)
        except urllib.error.HTTPError as exc:
            status = f"http_{exc.code}"
            message = clean(exc)
            if exc.code == 429 and attempt < max_retries:
                wait = retry_sleep * (attempt + 1)
                log(f"Rate limit HTTP 429. Waiting {wait:.0f}s before retry {attempt + 1}/{max_retries}.")
                time.sleep(wait)
                attempt += 1
                continue
            return status, message, {}
        except Exception as exc:
            return "error", clean(exc), {}


def load_json_file(path: Path) -> Optional[Any]:
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        return json.loads(path.read_text(encoding="utf-8-sig", errors="ignore"))
    except Exception:
        return None


def log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def result_items(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("data", "results", "items", "medicaments", "content"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = result_items(value)
            if nested:
                return nested
    if any(key.lower() in {"cis", "codecis", "denomination", "nom", "libelle"} for key in data.keys()):
        return [data]
    return []


def fetch_query(
    query: str,
    raw_dir: Path,
    base_url: str,
    timeout: int,
    max_retries: int,
    retry_sleep: float,
    resume: bool,
    force_refresh: bool,
    bulk_index: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> tuple[str, str, Any, Path, bool]:
    raw_path = query_cache_path(raw_dir, query)
    bulk_items = query_bdpm_bulk(query, bulk_index or {})
    if bulk_items:
        return "bulk", "", {"data": bulk_items}, raw_path, True
    if resume and not force_refresh:
        cached = load_json_file(raw_path)
        if cached is not None:
            return "cached", "", cached, raw_path, True
    url = f"{base_url.rstrip('/')}/medicaments?search={urllib.parse.quote(query)}"
    status, message, data = request_json_with_retries(url, timeout, max_retries, retry_sleep)
    if status == "ok":
        raw_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return status, message, data, raw_path, False


def pick(record: Dict[str, Any], *keys: str) -> str:
    lower = {key.lower(): key for key in record.keys()}
    for key in keys:
        real = lower.get(key.lower())
        if real is not None:
            return clean(record.get(real))
    return ""


def flatten_text(value: Any, max_items: int = 12) -> List[str]:
    parts: List[str] = []

    def walk(item: Any) -> None:
        if len(parts) >= max_items:
            return
        if item in (None, "", [], {}):
            return
        if isinstance(item, dict):
            for key, nested in item.items():
                if len(parts) >= max_items:
                    break
                if isinstance(nested, (dict, list)):
                    walk(nested)
                else:
                    text = clean(nested)
                    if text:
                        parts.append(f"{clean(key)}: {text}")
        elif isinstance(item, list):
            for nested in item[:max_items]:
                walk(nested)
        else:
            text = clean(item)
            if text:
                parts.append(text)

    walk(value)
    return parts


def record_to_readable(kind: str, title: str, payload: Any, record: Dict[str, Any]) -> str:
    name = pick(record, "denomination", "nom", "libelle", "name")
    form = pick(record, "formePharmaceutique", "forme", "forme_pharmaceutique")
    holder = pick(record, "titulaire", "laboratoire", "exploitant")
    status = pick(record, "statutAdministratif", "statut", "etatCommercialisation")
    cis = pick(record, "codeCIS", "cis", "code_cis", "id")

    if kind == "identity":
        parts = [
            f"Médicament : {name}." if name else "",
            f"Code CIS : {cis}." if cis else "",
            f"Forme pharmaceutique : {form}." if form else "",
            f"Titulaire ou exploitant : {holder}." if holder else "",
            f"Statut administratif : {status}." if status else "",
        ]
        return clean(" ".join(part for part in parts if part))

    values = flatten_text(payload)
    if not values:
        return ""
    label_by_kind = {
        "composition": "Composition BDPM",
        "presentation": "Présentation BDPM",
        "generic_group": "Groupe générique BDPM",
        "prescription_condition": "Conditions de prescription et de délivrance BDPM",
        "has_assessment": "Avis HAS / SMR / ASMR BDPM",
    }
    prefix = label_by_kind.get(kind, title)
    context = f" pour {name}" if name else ""
    return clean(f"{prefix}{context}. " + ". ".join(values) + ".")


def score_match(queue_row: Dict[str, str], record: Dict[str, Any]) -> float:
    query_generic = norm(queue_row.get("query_generic") or queue_row.get("nom_generique"))
    query_brand = norm(queue_row.get("query_brand") or queue_row.get("nom"))
    generic_text = norm(
        " ".join(
            flatten_text(
                {
                    "substances": record.get("substances") or record.get("compositions") or record.get("composition"),
                    "denominationSubstance": pick(record, "denominationSubstance", "substance", "substanceName"),
                },
                max_items=20,
            )
        )
    )
    brand_text = norm(" ".join([pick(record, "denomination", "nom", "libelle", "name"), pick(record, "marque", "brandName")]))
    form_text = norm(pick(record, "formePharmaceutique", "forme", "forme_pharmaceutique"))
    dosage_text = norm(" ".join(flatten_text(record.get("compositions") or record.get("composition"), max_items=20)))
    score = 0.0
    if query_generic and (query_generic in generic_text or query_generic in brand_text):
        score += 0.55
    if query_brand and query_brand in brand_text:
        score += 0.35
    if norm(queue_row.get("forme")) and norm(queue_row.get("forme")) in form_text:
        score += 0.05
    if norm(queue_row.get("dosage")) and norm(queue_row.get("dosage")) in dosage_text:
        score += 0.05
    # Avoid rejecting threshold matches because of binary floating point
    # representation, e.g. 0.35 + 0.05 + 0.05 -> 0.44999999999999996.
    return round(min(score, 0.95), 4)


def section_rows_for_record(row: Dict[str, str], record: Dict[str, Any], source_file: str, match_score: float) -> List[Dict[str, Any]]:
    cis = pick(record, "codeCIS", "cis", "code_cis", "id")
    name = pick(record, "denomination", "nom", "libelle", "name")
    form = pick(record, "formePharmaceutique", "forme", "forme_pharmaceutique")
    holder = pick(record, "titulaire", "laboratoire", "exploitant")
    status = pick(record, "statutAdministratif", "statut", "etatCommercialisation")

    sections = [
        ("identity", "BDPM identity", {"cis": cis, "name": name, "form": form, "holder": holder, "status": status}),
    ]
    for key, kind, title in (
        ("compositions", "composition", "BDPM composition"),
        ("composition", "composition", "BDPM composition"),
        ("presentations", "presentation", "BDPM presentations"),
        ("presentation", "presentation", "BDPM presentation"),
        ("generiques", "generic_group", "BDPM generic group"),
        ("generique", "generic_group", "BDPM generic group"),
        ("conditionsPrescriptionDelivrance", "prescription_condition", "BDPM prescription and dispensing conditions"),
        ("conditionsPrescription", "prescription_condition", "BDPM prescription conditions"),
        ("avisSmr", "has_assessment", "BDPM HAS SMR/ASMR assessment"),
        ("avis", "has_assessment", "BDPM HAS assessment"),
    ):
        if key in record and record.get(key):
            sections.append((kind, title, record.get(key)))

    out: List[Dict[str, Any]] = []
    seen_kinds: set[str] = set()
    for kind, title, payload in sections:
        if kind in seen_kinds:
            continue
        text = record_to_readable(kind, title, payload, record)
        if not text:
            continue
        seen_kinds.add(kind)
        out.append(
            {
                "row_id": row.get("row_id", ""),
                "amm": row.get("amm", ""),
                "nom": row.get("nom", ""),
                "nom_generique": row.get("nom_generique", ""),
                "source_system": "bdpm_api_medicaments_fr",
                "source_file": source_file,
                "source_record_id": cis,
                "match_query": row.get("query_primary", ""),
                "match_score": f"{match_score:.2f}",
                "section_kind": kind,
                "section_title": title,
                "section_text": text,
                "language": "fr",
                "authority_level": "fallback_bdpm_fr",
                "confidence": f"{max(match_score, 0.50):.2f}",
                "evidence_rank": "58",
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "content_hash": row_hash(row.get("row_id", ""), kind, text[:500]),
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", default="dpm_live_out/bdpm_live_query_queue.csv")
    parser.add_argument("--base-url", default="https://medicaments-api.giygas.dev/v1")
    parser.add_argument("--bdpm-bulk-dir", default="", help="Optional directory containing official BDPM text exports such as CIS_bdpm.txt.")
    parser.add_argument("--raw-dir", default="dpm_live_out/api_cache/bdpm")
    parser.add_argument("--sections-output", default="dpm_live_out/bdpm_api_fallback_sections.csv")
    parser.add_argument("--match-output", default="dpm_live_out/bdpm_api_match_results.csv")
    parser.add_argument("--summary", default="dpm_live_out/bdpm_api_fallback_summary.json")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.4)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--min-match-score", type=float, default=0.45)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--resume", action="store_true", default=True, help="Reuse existing raw JSON files when present.")
    parser.add_argument("--force-refresh", action="store_true", help="Ignore existing raw JSON files and call the API again.")
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--retry-sleep", type=float, default=60.0)
    parser.add_argument("--max-consecutive-rate-limits", type=int, default=5)
    parser.add_argument("--dedupe-queries", action="store_true", default=True, help="Call the API once per unique query and reuse the response for all rows.")
    parser.add_argument("--query-status-output", default="dpm_live_out/bdpm_api_query_status.csv")
    args = parser.parse_args()

    raw_dir = Path(args.raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    bulk_index = load_bdpm_bulk_index(Path(args.bdpm_bulk_dir)) if args.bdpm_bulk_dir else {}
    if bulk_index:
        log(f"Loaded BDPM bulk index: {len(bulk_index.get('__records__', []))} CIS records from {args.bdpm_bulk_dir}")
    queue_rows = read_csv(Path(args.queue))
    if args.limit > 0:
        queue_rows = queue_rows[: args.limit]

    section_rows: List[Dict[str, Any]] = []
    match_rows: List[Dict[str, Any]] = []
    query_status_rows: List[Dict[str, Any]] = []
    status_counts: Dict[str, int] = {}
    start = time.time()
    consecutive_rate_limits = 0
    stopped_early = ""
    rows_processed = 0

    estimated_seconds = len(queue_rows) * max(args.sleep, 0)
    unique_queries = {
        clean(row.get("query_primary") or row.get("query_generic") or row.get("query_brand") or row.get("nom"))
        for row in queue_rows
    }
    unique_queries.discard("")
    effective_calls = len(unique_queries) if args.dedupe_queries else len(queue_rows)
    log(
        "Starting BDPM API fallback: "
        f"{len(queue_rows)} queued rows, {len(unique_queries)} unique queries, "
        f"dedupe={args.dedupe_queries}, sleep={args.sleep}s, "
        f"minimum sleep-only time ~= {(effective_calls * max(args.sleep, 0)) / 60:.1f} min"
    )

    query_response_cache: Dict[str, tuple[str, str, Any, Path]] = {}

    if args.dedupe_queries:
        rows_by_query: Dict[str, List[Dict[str, str]]] = {}
        original_query_by_key: Dict[str, str] = {}
        for row in queue_rows:
            query = clean(row.get("query_primary") or row.get("query_generic") or row.get("query_brand") or row.get("nom"))
            if not query:
                continue
            key = query.upper()
            rows_by_query.setdefault(key, []).append(row)
            original_query_by_key.setdefault(key, query)

        for qidx, key in enumerate(sorted(rows_by_query.keys()), start=1):
            query = original_query_by_key[key]
            status, message, data, raw_path, used_cache = fetch_query(
                query,
                raw_dir,
                args.base_url,
                args.timeout,
                args.max_retries,
                args.retry_sleep,
                args.resume,
                args.force_refresh,
                bulk_index,
            )
            query_response_cache[key] = (status, message, data, raw_path)
            status_counts[status] = status_counts.get(status, 0) + 1
            items = result_items(data)
            query_status_rows.append(
                {
                    "query": query,
                    "status": status,
                    "api_items": len(items),
                    "row_count": len(rows_by_query[key]),
                    "raw_file": str(raw_path) if status in {"ok", "cached", "bulk"} else "",
                    "message": message,
                }
            )
            if status == "http_429":
                consecutive_rate_limits += 1
            else:
                consecutive_rate_limits = 0
            if qidx == 1 or qidx % args.progress_every == 0 or qidx == len(rows_by_query):
                elapsed = time.time() - start
                rate = qidx / elapsed if elapsed > 0 else 0
                remaining = len(rows_by_query) - qidx
                eta_seconds = remaining / rate if rate > 0 else 0
                log(
                    f"query {qidx}/{len(rows_by_query)} | query='{query}' | status={status} | "
                    f"api_items={len(items)} | affected_rows={len(rows_by_query[key])} | "
                    f"elapsed={elapsed / 60:.1f}m | eta={eta_seconds / 60:.1f}m"
                )
            if consecutive_rate_limits >= args.max_consecutive_rate_limits:
                stopped_early = (
                    f"Stopped after {consecutive_rate_limits} consecutive HTTP 429 rate-limit responses. "
                    "Rerun later; cached successful query responses will be reused."
                )
                log(stopped_early)
                break
            if not used_cache and qidx < len(rows_by_query) and args.sleep > 0:
                time.sleep(args.sleep)

        write_csv(
            Path(args.query_status_output),
            ["query", "status", "api_items", "row_count", "raw_file", "message"],
            query_status_rows,
        )

    row_start = time.time()
    for idx, row in enumerate(queue_rows, start=1):
        query = clean(row.get("query_primary") or row.get("query_generic") or row.get("query_brand") or row.get("nom"))
        raw_path = query_cache_path(raw_dir, query) if args.dedupe_queries else raw_dir / f"{clean(row.get('row_id'))}_{safe_filename(query)}.json"
        status = "ok"
        message = ""
        data: Any = {}
        used_cache = False
        query_key = clean(query).upper()
        if args.dedupe_queries and query_key in query_response_cache:
            status, message, data, raw_path = query_response_cache[query_key]
            used_cache = True
        elif args.dedupe_queries and stopped_early:
            status = "skipped_due_to_rate_limit"
            message = "Query not fetched because unique-query phase stopped after rate limits."
            data = {}
            query_response_cache[query_key] = (status, message, data, raw_path)
            used_cache = True
        elif args.resume and not args.force_refresh:
            cached = load_json_file(raw_path)
            if cached is not None:
                data = cached
                status = "cached"
                used_cache = True
        if not used_cache:
            bulk_items = query_bdpm_bulk(query, bulk_index)
            if bulk_items:
                status, message, data = "bulk", "", {"data": bulk_items}
                used_cache = True
            else:
                status, message, data = request_json_with_retries(
                    f"{args.base_url.rstrip('/')}/medicaments?search={urllib.parse.quote(query)}",
                    args.timeout,
                    args.max_retries,
                    args.retry_sleep,
                )
                if status == "ok":
                    raw_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        if args.dedupe_queries and query_key not in query_response_cache:
            query_response_cache[query_key] = (status, message, data, raw_path)
        if not args.dedupe_queries:
            if status == "http_429":
                consecutive_rate_limits += 1
            else:
                consecutive_rate_limits = 0
            status_counts[status] = status_counts.get(status, 0) + 1

        items = result_items(data)
        scored = sorted(((score_match(row, item), item) for item in items), key=lambda pair: pair[0], reverse=True)
        accepted = 0
        for score, item in scored[:3]:
            if score < args.min_match_score:
                continue
            accepted += 1
            rows = section_rows_for_record(row, item, str(raw_path), score)
            section_rows.extend(rows)
            match_rows.append(
                {
                    "row_id": row.get("row_id", ""),
                    "amm": row.get("amm", ""),
                    "nom": row.get("nom", ""),
                    "nom_generique": row.get("nom_generique", ""),
                    "query": query,
                    "status": status,
                    "match_score": f"{score:.2f}",
                    "source_record_id": pick(item, "codeCIS", "cis", "code_cis", "id"),
                    "source_name": pick(item, "denomination", "nom", "libelle", "name"),
                    "raw_file": str(raw_path),
                    "message": message,
                }
            )
        if not accepted:
            match_rows.append(
                {
                    "row_id": row.get("row_id", ""),
                    "amm": row.get("amm", ""),
                    "nom": row.get("nom", ""),
                    "nom_generique": row.get("nom_generique", ""),
                    "query": query,
                    "status": status,
                    "match_score": "",
                    "source_record_id": "",
                    "source_name": "",
                    "raw_file": str(raw_path) if status in {"ok", "bulk"} else "",
                    "message": message or "No accepted BDPM match",
                }
            )
        rows_processed = idx
        if idx == 1 or idx % args.progress_every == 0 or idx == len(queue_rows):
            elapsed = time.time() - row_start
            rate = idx / elapsed if elapsed > 0 else 0
            remaining = len(queue_rows) - idx
            eta_seconds = remaining / rate if rate > 0 else 0
            log(
                f"{idx}/{len(queue_rows)} rows processed | "
                f"query='{query}' | status={status} | api_items={len(items)} | "
                f"accepted={accepted} | sections={len(section_rows)} | "
                f"rows_with_sections={len({r['row_id'] for r in section_rows})} | "
                f"elapsed={elapsed / 60:.1f}m | eta={eta_seconds / 60:.1f}m"
            )
        if (not args.dedupe_queries) and idx < len(queue_rows) and args.sleep > 0:
            should_sleep = True
            if should_sleep:
                time.sleep(args.sleep)
        if (not args.dedupe_queries) and consecutive_rate_limits >= args.max_consecutive_rate_limits:
            stopped_early = (
                f"Stopped after {consecutive_rate_limits} consecutive HTTP 429 rate-limit responses. "
                "Increase --sleep/--retry-sleep or resume later."
            )
            log(stopped_early)
            break

    write_csv(
        Path(args.sections_output),
        [
            "row_id",
            "amm",
            "nom",
            "nom_generique",
            "source_system",
            "source_file",
            "source_record_id",
            "match_query",
            "match_score",
            "section_kind",
            "section_title",
            "section_text",
            "language",
            "authority_level",
            "confidence",
            "evidence_rank",
            "retrieved_at",
            "content_hash",
        ],
        section_rows,
    )
    write_csv(
        Path(args.match_output),
        ["row_id", "amm", "nom", "nom_generique", "query", "status", "match_score", "source_record_id", "source_name", "raw_file", "message"],
        match_rows,
    )
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "queue_rows_processed": rows_processed,
        "unique_queries": len(unique_queries),
        "query_api_cache_entries": len(query_response_cache),
        "bdpm_bulk_records": len(bulk_index.get("__records__", [])) if bulk_index else 0,
        "query_status_rows": len(query_status_rows),
        "match_rows": len(match_rows),
        "section_rows": len(section_rows),
        "row_ids_with_bdpm_sections": len({row["row_id"] for row in section_rows}),
        "status_counts": status_counts,
        "stopped_early": stopped_early,
        "outputs": {
            "sections": args.sections_output,
            "matches": args.match_output,
            "raw_dir": args.raw_dir,
            "query_status": args.query_status_output,
            "summary": args.summary,
        },
    }
    Path(args.summary).write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
