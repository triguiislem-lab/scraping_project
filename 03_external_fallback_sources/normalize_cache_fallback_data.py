#!/usr/bin/env python3
"""
Normalize cached openFDA/DailyMed/RxNorm/RxClass fallback data for list_amm gaps.

The cache folder contains hashed responses without a universal request manifest, so this
script only promotes files whose own content exposes a usable drug name, generic name,
RxCUI, class, or label section. Unmappable cache files are still counted in the trace.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


SECTION_MAP: Dict[str, Tuple[str, str]] = {
    "indications_and_usage": ("indication", "Indications and usage"),
    "dosage_and_administration": ("dosage", "Dosage and administration"),
    "contraindications": ("contraindication", "Contraindications"),
    "boxed_warning": ("warning", "Boxed warning"),
    "warnings": ("warning", "Warnings"),
    "warnings_and_cautions": ("warning", "Warnings and cautions"),
    "warnings_and_precautions": ("warning", "Warnings and precautions"),
    "drug_interactions": ("interaction", "Drug interactions"),
    "adverse_reactions": ("adverse_effect", "Adverse reactions"),
    "use_in_specific_populations": ("special_population", "Use in specific populations"),
    "pregnancy": ("special_population", "Pregnancy"),
    "labor_and_delivery": ("special_population", "Labor and delivery"),
    "nursing_mothers": ("special_population", "Nursing mothers"),
    "pediatric_use": ("special_population", "Pediatric use"),
    "geriatric_use": ("special_population", "Geriatric use"),
    "overdosage": ("overdose", "Overdosage"),
    "clinical_pharmacology": ("pharmacology", "Clinical pharmacology"),
    "mechanism_of_action": ("pharmacology", "Mechanism of action"),
    "pharmacodynamics": ("pharmacology", "Pharmacodynamics"),
    "pharmacokinetics": ("pharmacology", "Pharmacokinetics"),
}

DAILYMED_SECTION_TITLES: Dict[str, str] = {
    "INDICATIONS AND USAGE": "indication",
    "DOSAGE AND ADMINISTRATION": "dosage",
    "CONTRAINDICATIONS": "contraindication",
    "WARNINGS": "warning",
    "WARNINGS AND PRECAUTIONS": "warning",
    "DRUG INTERACTIONS": "interaction",
    "ADVERSE REACTIONS": "adverse_effect",
    "USE IN SPECIFIC POPULATIONS": "special_population",
    "PREGNANCY": "special_population",
    "NURSING MOTHERS": "special_population",
    "PEDIATRIC USE": "special_population",
    "GERIATRIC USE": "special_population",
    "OVERDOSAGE": "overdose",
    "CLINICAL PHARMACOLOGY": "pharmacology",
}

OUTPUT_LABEL_FIELDS = [
    "row_id",
    "amm",
    "nom",
    "nom_generique",
    "source_system",
    "source_file",
    "source_record_id",
    "match_type",
    "match_term",
    "matched_source_terms",
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

OUTPUT_TERM_FIELDS = [
    "row_id",
    "amm",
    "nom",
    "nom_generique",
    "source_system",
    "source_file",
    "match_type",
    "match_term",
    "rxcui",
    "rxaui",
    "tty",
    "source_name",
    "normalized_name",
    "score",
    "authority_level",
    "confidence",
    "content_hash",
]

OUTPUT_CLASS_FIELDS = [
    "row_id",
    "amm",
    "nom",
    "nom_generique",
    "source_system",
    "source_file",
    "match_type",
    "match_term",
    "rxcui",
    "drug_name",
    "class_id",
    "class_name",
    "class_type",
    "relationship",
    "relationship_source",
    "authority_level",
    "confidence",
    "content_hash",
]

OUTPUT_TRACE_FIELDS = [
    "source_file",
    "source_system",
    "parsed_status",
    "records_seen",
    "matched_row_count",
    "label_section_rows",
    "terminology_rows",
    "drug_class_rows",
    "indexed_terms",
    "message",
]


@dataclass(frozen=True)
class AmmRow:
    row_id: str
    amm: str
    nom: str
    nom_generique: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = "\n".join(clean_text(item) for item in value)
    elif not isinstance(value, str):
        value = str(value)
    value = value.encode("utf-8", "ignore").decode("utf-8", "ignore")
    mojibake_fixes = {
        "â€™": "'",
        "â€œ": '"',
        "â€": '"',
        "â€": '"',
        "â€“": "-",
        "â€”": "-",
        "â€¢": "-",
        "â– ": "-",
        "Â®": "",
        "Â©": "",
        "Â": " ",
        "\ufeff": "",
    }
    for bad, good in mojibake_fixes.items():
        value = value.replace(bad, good)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def norm_key(value: Any) -> str:
    text = clean_text(value).upper()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def content_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", "ignore")).hexdigest()


def split_names(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        out: List[str] = []
        for item in value:
            out.extend(split_names(item))
        return out
    text = clean_text(value)
    if not text:
        return []
    parts = re.split(r"\s*(?:;|,|\+|\band\b|\bet\b|/)\s*", text, flags=re.IGNORECASE)
    return [part.strip() for part in parts if part.strip()]


def load_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_target_rows(overlap_path: Path, missing_local_path: Optional[Path]) -> Tuple[List[AmmRow], Dict[str, Set[str]]]:
    overlap_rows = load_csv_rows(overlap_path)
    missing_ids: Optional[Set[str]] = None
    if missing_local_path and str(missing_local_path) and missing_local_path.exists():
        missing_ids = {str(row.get("row_id", "")).strip() for row in load_csv_rows(missing_local_path)}

    reason_by_row: Dict[str, Set[str]] = defaultdict(set)
    rows: List[AmmRow] = []
    seen: Set[str] = set()
    for row in overlap_rows:
        row_id = str(row.get("row_id", "")).strip()
        if not row_id or row_id in seen:
            continue
        if missing_ids is not None:
            if row_id not in missing_ids:
                continue
        elif "missing_local_document" not in clean_text(row.get("gap_flags", "")):
            continue
        if str(row.get("cache_any_hit", "")).strip() not in {"1", "true", "True", "TRUE"}:
            continue
        flags = {
            "cache_label_brand_hit": "label_brand",
            "cache_label_generic_hit": "label_generic",
            "cache_terminology_brand_hit": "terminology_brand",
            "cache_terminology_generic_hit": "terminology_generic",
        }
        for field, reason in flags.items():
            if str(row.get(field, "")).strip() in {"1", "true", "True", "TRUE"}:
                reason_by_row[row_id].add(reason)
        rows.append(
            AmmRow(
                row_id=row_id,
                amm=clean_text(row.get("amm", "")),
                nom=clean_text(row.get("nom", "")),
                nom_generique=clean_text(row.get("nom_generique", "")),
            )
        )
        seen.add(row_id)
    return rows, reason_by_row


def build_term_index(rows: Sequence[AmmRow]) -> Dict[str, List[Tuple[AmmRow, str, str]]]:
    index: Dict[str, List[Tuple[AmmRow, str, str]]] = defaultdict(list)
    for row in rows:
        for match_type, raw in (("brand", row.nom), ("generic", row.nom_generique)):
            for term in split_names(raw):
                key = norm_key(term)
                if len(key) < 3:
                    continue
                index[key].append((row, match_type, term))
    return index


def match_terms(source_terms: Iterable[str], term_index: Dict[str, List[Tuple[AmmRow, str, str]]]) -> List[Tuple[AmmRow, str, str, str]]:
    matches: List[Tuple[AmmRow, str, str, str]] = []
    seen: Set[Tuple[str, str, str]] = set()
    for term in source_terms:
        clean = clean_text(term)
        key = norm_key(clean)
        if not key:
            continue
        for row, match_type, row_term in term_index.get(key, []):
            marker = (row.row_id, match_type, key)
            if marker not in seen:
                matches.append((row, match_type, row_term, clean))
                seen.add(marker)
    return matches


def add_unique_row(rows: List[Dict[str, Any]], row: Dict[str, Any], seen: Set[str], key_fields: Sequence[str]) -> None:
    key = "\x1f".join(clean_text(row.get(field, "")) for field in key_fields)
    if key in seen:
        return
    seen.add(key)
    rows.append(row)


def open_json(path: Path) -> Optional[Any]:
    try:
        with path.open("r", encoding="utf-8-sig", errors="ignore") as handle:
            text = handle.read()
        if not text.strip():
            return None
        return json.loads(text)
    except Exception:
        return None


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def openfda_terms(record: Dict[str, Any]) -> List[str]:
    openfda = record.get("openfda") or {}
    terms: List[str] = []
    for key in ("generic_name", "brand_name", "substance_name", "manufacturer_name"):
        terms.extend(split_names(openfda.get(key)))
    terms.extend(split_names(record.get("spl_product_data_elements")))
    terms.extend(split_names(record.get("active_ingredient")))
    return sorted({clean_text(term) for term in terms if clean_text(term)})


def openfda_rxcuis(record: Dict[str, Any]) -> List[str]:
    openfda = record.get("openfda") or {}
    return [clean_text(item) for item in as_list(openfda.get("rxcui")) if clean_text(item)]


def parse_openfda_label(
    path: Path,
    data: Dict[str, Any],
    term_index: Dict[str, List[Tuple[AmmRow, str, str]]],
    label_rows: List[Dict[str, Any]],
    term_rows: List[Dict[str, Any]],
    label_seen: Set[str],
    term_seen: Set[str],
    max_sections_per_row_kind: int,
) -> Dict[str, Any]:
    stats = {
        "source_system": "openfda_label",
        "parsed_status": "parsed",
        "records_seen": 0,
        "matched_row_count": 0,
        "label_section_rows": 0,
        "terminology_rows": 0,
        "drug_class_rows": 0,
        "indexed_terms": set(),
        "message": "",
    }
    row_kind_counts: Counter[Tuple[str, str]] = Counter()
    matched_rows: Set[str] = set()

    for record in as_list(data.get("results")):
        if not isinstance(record, dict):
            continue
        stats["records_seen"] += 1
        terms = openfda_terms(record)
        stats["indexed_terms"].update(norm_key(term) for term in terms if norm_key(term))
        matches = match_terms(terms, term_index)
        if not matches:
            continue
        source_record_id = clean_text(record.get("id") or record.get("set_id") or record.get("effective_time") or "")
        retrieved_at = clean_text((data.get("meta") or {}).get("last_updated", ""))
        rxcuis = openfda_rxcuis(record)
        for amm_row, match_type, match_term, source_term in matches:
            matched_rows.add(amm_row.row_id)
            confidence = "0.72" if match_type == "generic" else "0.62"
            for rxcui in rxcuis:
                add_unique_row(
                    term_rows,
                    {
                        "row_id": amm_row.row_id,
                        "amm": amm_row.amm,
                        "nom": amm_row.nom,
                        "nom_generique": amm_row.nom_generique,
                        "source_system": "openfda_label",
                        "source_file": str(path),
                        "match_type": match_type,
                        "match_term": match_term,
                        "rxcui": rxcui,
                        "rxaui": "",
                        "tty": "",
                        "source_name": "openFDA.openfda.rxcui",
                        "normalized_name": source_term,
                        "score": "",
                        "authority_level": "fallback_terminology",
                        "confidence": confidence,
                        "content_hash": content_hash(f"{amm_row.row_id}|{rxcui}|{path.name}"),
                    },
                    term_seen,
                    ("row_id", "source_system", "rxcui", "source_file"),
                )
                stats["terminology_rows"] += 1

            for section_key, (section_kind, section_title) in SECTION_MAP.items():
                section_text = clean_text(record.get(section_key))
                if not section_text:
                    continue
                count_key = (amm_row.row_id, section_kind)
                if row_kind_counts[count_key] >= max_sections_per_row_kind:
                    continue
                row_kind_counts[count_key] += 1
                add_unique_row(
                    label_rows,
                    {
                        "row_id": amm_row.row_id,
                        "amm": amm_row.amm,
                        "nom": amm_row.nom,
                        "nom_generique": amm_row.nom_generique,
                        "source_system": "openfda_label",
                        "source_file": str(path),
                        "source_record_id": source_record_id,
                        "match_type": match_type,
                        "match_term": match_term,
                        "matched_source_terms": "; ".join(terms[:12]),
                        "section_kind": section_kind,
                        "section_title": section_title,
                        "section_text": section_text,
                        "language": "en",
                        "authority_level": "fallback_openfda_label",
                        "confidence": confidence,
                        "evidence_rank": "70",
                        "retrieved_at": retrieved_at,
                        "content_hash": content_hash(f"{amm_row.row_id}|{section_kind}|{section_text[:400]}"),
                    },
                    label_seen,
                    ("row_id", "source_system", "section_kind", "section_text"),
                )
                stats["label_section_rows"] += 1

    stats["matched_row_count"] = len(matched_rows)
    return stats


def parse_rxclass(
    path: Path,
    data: Dict[str, Any],
    term_index: Dict[str, List[Tuple[AmmRow, str, str]]],
    class_rows: List[Dict[str, Any]],
    class_seen: Set[str],
) -> Dict[str, Any]:
    stats = {
        "source_system": "rxclass",
        "parsed_status": "parsed",
        "records_seen": 0,
        "matched_row_count": 0,
        "label_section_rows": 0,
        "terminology_rows": 0,
        "drug_class_rows": 0,
        "indexed_terms": set(),
        "message": "",
    }
    matched_rows: Set[str] = set()
    entries = (((data.get("rxclassDrugInfoList") or {}).get("rxclassDrugInfo")) or [])
    for entry in as_list(entries):
        if not isinstance(entry, dict):
            continue
        stats["records_seen"] += 1
        concept = entry.get("minConcept") or {}
        class_item = entry.get("rxclassMinConceptItem") or {}
        drug_name = clean_text(concept.get("name", ""))
        stats["indexed_terms"].add(norm_key(drug_name))
        matches = match_terms([drug_name], term_index)
        if not matches:
            continue
        for amm_row, match_type, match_term, source_term in matches:
            matched_rows.add(amm_row.row_id)
            add_unique_row(
                class_rows,
                {
                    "row_id": amm_row.row_id,
                    "amm": amm_row.amm,
                    "nom": amm_row.nom,
                    "nom_generique": amm_row.nom_generique,
                    "source_system": "rxclass",
                    "source_file": str(path),
                    "match_type": match_type,
                    "match_term": match_term,
                    "rxcui": clean_text(concept.get("rxcui", "")),
                    "drug_name": source_term,
                    "class_id": clean_text(class_item.get("classId", "")),
                    "class_name": clean_text(class_item.get("className", "")),
                    "class_type": clean_text(class_item.get("classType", "")),
                    "relationship": clean_text(entry.get("rela", "")),
                    "relationship_source": clean_text(entry.get("relaSource", "")),
                    "authority_level": "fallback_rxclass",
                    "confidence": "0.68" if match_type == "generic" else "0.58",
                    "content_hash": content_hash(f"{amm_row.row_id}|{concept.get('rxcui','')}|{class_item.get('classId','')}|{path.name}"),
                },
                class_seen,
                ("row_id", "rxcui", "class_id", "relationship", "source_file"),
            )
            stats["drug_class_rows"] += 1
    stats["matched_row_count"] = len(matched_rows)
    return stats


def parse_approximate_rxnorm(
    path: Path,
    data: Dict[str, Any],
    term_index: Dict[str, List[Tuple[AmmRow, str, str]]],
    term_rows: List[Dict[str, Any]],
    term_seen: Set[str],
) -> Dict[str, Any]:
    stats = {
        "source_system": "rxnorm_approximate",
        "parsed_status": "parsed",
        "records_seen": 0,
        "matched_row_count": 0,
        "label_section_rows": 0,
        "terminology_rows": 0,
        "drug_class_rows": 0,
        "indexed_terms": set(),
        "message": "",
    }
    matched_rows: Set[str] = set()
    candidates = (((data.get("approximateGroup") or {}).get("candidate")) or [])
    for candidate in as_list(candidates):
        if not isinstance(candidate, dict):
            continue
        stats["records_seen"] += 1
        name = clean_text(candidate.get("name", ""))
        if not name:
            continue
        stats["indexed_terms"].add(norm_key(name))
        matches = match_terms([name], term_index)
        if not matches:
            continue
        for amm_row, match_type, match_term, source_term in matches:
            matched_rows.add(amm_row.row_id)
            add_unique_row(
                term_rows,
                {
                    "row_id": amm_row.row_id,
                    "amm": amm_row.amm,
                    "nom": amm_row.nom,
                    "nom_generique": amm_row.nom_generique,
                    "source_system": "rxnorm_approximate",
                    "source_file": str(path),
                    "match_type": match_type,
                    "match_term": match_term,
                    "rxcui": clean_text(candidate.get("rxcui", "")),
                    "rxaui": clean_text(candidate.get("rxaui", "")),
                    "tty": "",
                    "source_name": clean_text(candidate.get("source", "")),
                    "normalized_name": source_term,
                    "score": clean_text(candidate.get("score", "")),
                    "authority_level": "fallback_terminology",
                    "confidence": "0.66" if match_type == "generic" else "0.55",
                    "content_hash": content_hash(f"{amm_row.row_id}|{candidate.get('rxcui','')}|{candidate.get('rxaui','')}|{path.name}"),
                },
                term_seen,
                ("row_id", "source_system", "rxcui", "rxaui", "source_name"),
            )
            stats["terminology_rows"] += 1
    stats["matched_row_count"] = len(matched_rows)
    return stats


def parse_rxnorm_properties(
    path: Path,
    data: Dict[str, Any],
    term_index: Dict[str, List[Tuple[AmmRow, str, str]]],
    term_rows: List[Dict[str, Any]],
    term_seen: Set[str],
) -> Dict[str, Any]:
    stats = {
        "source_system": "rxnorm_properties",
        "parsed_status": "parsed",
        "records_seen": 0,
        "matched_row_count": 0,
        "label_section_rows": 0,
        "terminology_rows": 0,
        "drug_class_rows": 0,
        "indexed_terms": set(),
        "message": "",
    }
    groups = (((data.get("propConceptGroup") or {}).get("propConcept")) or [])
    matched_rows: Set[str] = set()
    for concept in as_list(groups):
        if not isinstance(concept, dict):
            continue
        stats["records_seen"] += 1
        name = clean_text(concept.get("propValue") or concept.get("name") or "")
        if not name:
            continue
        stats["indexed_terms"].add(norm_key(name))
        matches = match_terms([name], term_index)
        if not matches:
            continue
        for amm_row, match_type, match_term, source_term in matches:
            matched_rows.add(amm_row.row_id)
            add_unique_row(
                term_rows,
                {
                    "row_id": amm_row.row_id,
                    "amm": amm_row.amm,
                    "nom": amm_row.nom,
                    "nom_generique": amm_row.nom_generique,
                    "source_system": "rxnorm_properties",
                    "source_file": str(path),
                    "match_type": match_type,
                    "match_term": match_term,
                    "rxcui": clean_text(concept.get("rxcui", "")),
                    "rxaui": "",
                    "tty": clean_text(concept.get("propName", "")),
                    "source_name": "RxNorm property",
                    "normalized_name": source_term,
                    "score": "",
                    "authority_level": "fallback_terminology",
                    "confidence": "0.64" if match_type == "generic" else "0.54",
                    "content_hash": content_hash(f"{amm_row.row_id}|{concept.get('rxcui','')}|{name}|{path.name}"),
                },
                term_seen,
                ("row_id", "source_system", "rxcui", "normalized_name", "source_file"),
            )
            stats["terminology_rows"] += 1
    stats["matched_row_count"] = len(matched_rows)
    return stats


def xml_text(element: ET.Element) -> str:
    return clean_text(" ".join(part for part in element.itertext() if clean_text(part)))


def find_xml_text(root: ET.Element, local_name: str) -> List[str]:
    out: List[str] = []
    for element in root.iter():
        if element.tag.split("}")[-1] == local_name:
            text = xml_text(element)
            if text:
                out.append(text)
    return out


def parse_dailymed_xml(
    path: Path,
    term_index: Dict[str, List[Tuple[AmmRow, str, str]]],
    label_rows: List[Dict[str, Any]],
    label_seen: Set[str],
    max_sections_per_row_kind: int,
) -> Dict[str, Any]:
    stats = {
        "source_system": "dailymed_spl",
        "parsed_status": "parsed",
        "records_seen": 0,
        "matched_row_count": 0,
        "label_section_rows": 0,
        "terminology_rows": 0,
        "drug_class_rows": 0,
        "indexed_terms": set(),
        "message": "",
    }
    try:
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
        root = ET.fromstring(text)
    except Exception as exc:
        stats["parsed_status"] = "parse_failed"
        stats["message"] = str(exc)[:180]
        return stats

    names = find_xml_text(root, "name")[:30]
    titles = find_xml_text(root, "title")[:10]
    source_terms = sorted({name for name in names + titles if name})
    stats["indexed_terms"].update(norm_key(term) for term in source_terms if norm_key(term))
    matches = match_terms(source_terms, term_index)
    if not matches:
        return stats

    matched_rows: Set[str] = set()
    row_kind_counts: Counter[Tuple[str, str]] = Counter()
    sections: List[Tuple[str, str, str]] = []
    for section in root.iter():
        if section.tag.split("}")[-1] != "section":
            continue
        title = ""
        for child in list(section):
            if child.tag.split("}")[-1] == "title":
                title = xml_text(child)
                break
        title_norm = norm_key(title)
        section_kind = ""
        for known_title, known_kind in DAILYMED_SECTION_TITLES.items():
            if known_title in title_norm:
                section_kind = known_kind
                break
        if not section_kind:
            continue
        body = xml_text(section)
        if len(body) < 80:
            continue
        sections.append((section_kind, title or section_kind, body))
    stats["records_seen"] = len(sections)

    for amm_row, match_type, match_term, source_term in matches:
        matched_rows.add(amm_row.row_id)
        confidence = "0.70" if match_type == "generic" else "0.60"
        for section_kind, section_title, section_text in sections:
            count_key = (amm_row.row_id, section_kind)
            if row_kind_counts[count_key] >= max_sections_per_row_kind:
                continue
            row_kind_counts[count_key] += 1
            add_unique_row(
                label_rows,
                {
                    "row_id": amm_row.row_id,
                    "amm": amm_row.amm,
                    "nom": amm_row.nom,
                    "nom_generique": amm_row.nom_generique,
                    "source_system": "dailymed_spl",
                    "source_file": str(path),
                    "source_record_id": "",
                    "match_type": match_type,
                    "match_term": match_term,
                    "matched_source_terms": "; ".join(source_terms[:12]),
                    "section_kind": section_kind,
                    "section_title": section_title,
                    "section_text": section_text,
                    "language": "en",
                    "authority_level": "fallback_dailymed_spl",
                    "confidence": confidence,
                    "evidence_rank": "72",
                    "retrieved_at": "",
                    "content_hash": content_hash(f"{amm_row.row_id}|{section_kind}|{section_text[:400]}"),
                },
                label_seen,
                ("row_id", "source_system", "section_kind", "section_text"),
            )
            stats["label_section_rows"] += 1
    stats["matched_row_count"] = len(matched_rows)
    return stats


def trace_row(path: Path, stats: Dict[str, Any]) -> Dict[str, str]:
    indexed_terms = stats.get("indexed_terms", set())
    if not isinstance(indexed_terms, set):
        indexed_terms = set()
    return {
        "source_file": str(path),
        "source_system": clean_text(stats.get("source_system", "unknown")),
        "parsed_status": clean_text(stats.get("parsed_status", "skipped")),
        "records_seen": str(stats.get("records_seen", 0)),
        "matched_row_count": str(stats.get("matched_row_count", 0)),
        "label_section_rows": str(stats.get("label_section_rows", 0)),
        "terminology_rows": str(stats.get("terminology_rows", 0)),
        "drug_class_rows": str(stats.get("drug_class_rows", 0)),
        "indexed_terms": "; ".join(sorted(term for term in indexed_terms if term)[:20]),
        "message": clean_text(stats.get("message", "")),
    }


def classify_json(data: Any) -> str:
    if not isinstance(data, dict):
        return "unknown_json"
    if "results" in data and isinstance(data.get("results"), list):
        return "openfda_label"
    if "rxclassDrugInfoList" in data:
        return "rxclass"
    if "approximateGroup" in data:
        return "rxnorm_approximate"
    if "propConceptGroup" in data:
        return "rxnorm_properties"
    if "idGroup" in data:
        return "rxnorm_idgroup_unmappable"
    if "feed" in data:
        return "medlineplus_or_connect"
    return "unknown_json"


def write_csv(path: Path, fields: Sequence[str], rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="", errors="ignore") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            safe = {field: clean_text(row.get(field, "")) for field in fields}
            writer.writerow(safe)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", default="cache")
    parser.add_argument("--overlap", default="dpm_live_out/cache_missing_detail_overlap.csv")
    parser.add_argument(
        "--missing-local",
        default="",
        help=(
            "Optional fixed row list to filter targets. By default the script uses "
            "cache_missing_detail_overlap.csv rows whose gap_flags contain missing_local_document, "
            "which makes reruns stable after fallback coverage summaries are recomputed."
        ),
    )
    parser.add_argument("--label-output", default="dpm_live_out/fallback_label_section.csv")
    parser.add_argument("--terminology-output", default="dpm_live_out/fallback_terminology_map.csv")
    parser.add_argument("--class-output", default="dpm_live_out/fallback_drug_class.csv")
    parser.add_argument("--trace-output", default="dpm_live_out/fallback_source_trace.csv")
    parser.add_argument("--summary", default="dpm_live_out/cache_fallback_normalization_summary.json")
    parser.add_argument("--max-sections-per-row-kind", type=int, default=1)
    parser.add_argument("--include-dailymed-xml", action="store_true", default=True)
    parser.add_argument("--limit-files", type=int, default=0)
    args = parser.parse_args(argv)

    cache_dir = Path(args.cache_dir)
    if not cache_dir.exists():
        raise SystemExit(f"Cache directory not found: {cache_dir}")

    missing_local_path = Path(args.missing_local) if clean_text(args.missing_local) else None
    target_rows, reason_by_row = load_target_rows(Path(args.overlap), missing_local_path)
    term_index = build_term_index(target_rows)
    if not target_rows:
        raise SystemExit("No target rows loaded from overlap/missing-local inputs.")

    label_rows: List[Dict[str, Any]] = []
    term_rows: List[Dict[str, Any]] = []
    class_rows: List[Dict[str, Any]] = []
    trace_rows: List[Dict[str, Any]] = []
    label_seen: Set[str] = set()
    term_seen: Set[str] = set()
    class_seen: Set[str] = set()
    source_counts: Counter[str] = Counter()

    files = sorted([path for path in cache_dir.iterdir() if path.is_file()])
    if args.limit_files > 0:
        files = files[: args.limit_files]

    for path in files:
        suffix = path.suffix.lower()
        if suffix == ".json":
            data = open_json(path)
            if data is None:
                stats = {
                    "source_system": "json",
                    "parsed_status": "parse_failed",
                    "records_seen": 0,
                    "matched_row_count": 0,
                    "label_section_rows": 0,
                    "terminology_rows": 0,
                    "drug_class_rows": 0,
                    "indexed_terms": set(),
                    "message": "empty or invalid JSON",
                }
            else:
                kind = classify_json(data)
                source_counts[kind] += 1
                if kind == "openfda_label":
                    stats = parse_openfda_label(
                        path,
                        data,
                        term_index,
                        label_rows,
                        term_rows,
                        label_seen,
                        term_seen,
                        args.max_sections_per_row_kind,
                    )
                elif kind == "rxclass":
                    stats = parse_rxclass(path, data, term_index, class_rows, class_seen)
                elif kind == "rxnorm_approximate":
                    stats = parse_approximate_rxnorm(path, data, term_index, term_rows, term_seen)
                elif kind == "rxnorm_properties":
                    stats = parse_rxnorm_properties(path, data, term_index, term_rows, term_seen)
                else:
                    stats = {
                        "source_system": kind,
                        "parsed_status": "unmappable" if "unmappable" in kind else "skipped",
                        "records_seen": 1,
                        "matched_row_count": 0,
                        "label_section_rows": 0,
                        "terminology_rows": 0,
                        "drug_class_rows": 0,
                        "indexed_terms": set(),
                        "message": "No exposed drug name/section mapping promoted by this normalizer.",
                    }
            trace_rows.append(trace_row(path, stats))
        elif suffix in {".txt", ".xml"} and args.include_dailymed_xml:
            source_counts["dailymed_spl_candidate"] += 1
            stats = parse_dailymed_xml(
                path,
                term_index,
                label_rows,
                label_seen,
                args.max_sections_per_row_kind,
            )
            trace_rows.append(trace_row(path, stats))
        else:
            source_counts["skipped_other"] += 1
            trace_rows.append(
                trace_row(
                    path,
                    {
                        "source_system": "other",
                        "parsed_status": "skipped",
                        "records_seen": 0,
                        "matched_row_count": 0,
                        "label_section_rows": 0,
                        "terminology_rows": 0,
                        "drug_class_rows": 0,
                        "indexed_terms": set(),
                        "message": f"Unsupported suffix {suffix}",
                    },
                )
            )

    write_csv(Path(args.label_output), OUTPUT_LABEL_FIELDS, label_rows)
    write_csv(Path(args.terminology_output), OUTPUT_TERM_FIELDS, term_rows)
    write_csv(Path(args.class_output), OUTPUT_CLASS_FIELDS, class_rows)
    write_csv(Path(args.trace_output), OUTPUT_TRACE_FIELDS, trace_rows)

    label_row_ids = {row["row_id"] for row in label_rows}
    term_row_ids = {row["row_id"] for row in term_rows}
    class_row_ids = {row["row_id"] for row in class_rows}
    all_fallback_row_ids = label_row_ids | term_row_ids | class_row_ids
    label_kind_counts = Counter(row.get("section_kind", "") for row in label_rows)
    label_source_counts = Counter(row.get("source_system", "") for row in label_rows)
    term_source_counts = Counter(row.get("source_system", "") for row in term_rows)

    summary = {
        "created_at": utc_now(),
        "cache_dir": str(cache_dir.resolve()),
        "target_rows": len(target_rows),
        "target_rows_with_overlap_reasons": len(reason_by_row),
        "cache_files_scanned": len(files),
        "source_file_counts": dict(source_counts),
        "label_section_rows": len(label_rows),
        "terminology_rows": len(term_rows),
        "drug_class_rows": len(class_rows),
        "row_ids_with_fallback_label_sections": len(label_row_ids),
        "row_ids_with_fallback_terminology": len(term_row_ids),
        "row_ids_with_fallback_drug_classes": len(class_row_ids),
        "row_ids_with_any_fallback": len(all_fallback_row_ids),
        "label_section_kind_counts": dict(label_kind_counts),
        "label_source_counts": dict(label_source_counts),
        "terminology_source_counts": dict(term_source_counts),
        "outputs": {
            "label_sections": args.label_output,
            "terminology": args.terminology_output,
            "drug_classes": args.class_output,
            "source_trace": args.trace_output,
            "summary": args.summary,
        },
        "notes": [
            "Fallback rows are non-local evidence and must rank below Tunisian RCP/notice/lab evidence.",
            "Hashed RxNorm idGroup files without exposed query terms are counted as unmappable; a request manifest would allow richer normalization.",
        ],
    }
    Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
    Path(args.summary).write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
