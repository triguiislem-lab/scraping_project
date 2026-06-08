#!/usr/bin/env python3
"""
Swissmedic AIPS XML adapter for the Tunisia medicine-evidence remediation pipeline.

What it does
------------
- Reads remaining medicines from your existing remaining_*_details CSV.
- Optionally uses guaranteed_source_medicine_summary.csv to route only likely
  Swissmedic candidates.
- Reads Swissmedic AIPS XML from a local XML file, ZIP file, or directory.
- Optionally downloads an AIPS XML/ZIP URL if you provide --aips-url.
- Matches medicines by brand, active substance/DCI, laboratory/holder, dosage and form.
- Extracts RCP/SmPC-style sections from Swiss professional/patient information text.
- Emits rows using the same section CSV contract used by your global fallback scripts.

Recommended usage
-----------------
Download the Swissmedic AIPS XML/ZIP from https://download.swissmedicinfo.ch/
then run:

python swissmedic_aips_adapter.py \
  --remaining remaining_382_medicines_all_available_details.csv \
  --medicine-summary guaranteed_source_medicine_summary.csv \
  --covered-sections global_regulatory_fallback_sections_cecmed_v3.csv \
  --exclude-covered \
  --aips-input AipsDownload_YYYYMMDD.xml \
  --output swissmedic_aips_fallback_sections.csv \
  --query-status-output swissmedic_aips_query_status.csv \
  --summary swissmedic_aips_summary.json

The parser is intentionally schema-tolerant because Swissmedic's AIPS XML can
change; it searches XML tags, attributes, URLs, and embedded HTML/text.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
import xml.etree.ElementTree as ET

OUTPUT_FIELDS = [
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
]

STATUS_FIELDS = [
    "row_id",
    "amm",
    "nom",
    "nom_generique",
    "dosage",
    "forme",
    "labo",
    "pays",
    "already_covered_by_extra_sections",
    "candidate_reason",
    "top_source_name",
    "product_type",
    "chosen_source",
    "chosen_record_id",
    "chosen_record_title",
    "chosen_record_holder",
    "chosen_record_url",
    "match_score",
    "sections",
    "status",
    "attempts",
]

SWISSMEDIC_DOWNLOAD_HOME = "https://download.swissmedicinfo.ch/"
SWISSMEDIC_PRO_HOME = "https://www.swissmedicinfo-pro.ch/"
SWISSMEDIC_PATIENT_HOME = "https://www.swissmedicinfo.ch/"

# Section patterns across Swiss AIPS professional/patient information texts.
# section_kind names align with the current fallback contract.
SECTION_RULES: Sequence[Tuple[str, str]] = [
    # French professional information
    ("indication", r"\bIndications?\s*/?\s*(?:Possibilit[eé]s\s+d[’']?emploi)?\b"),
    ("dosage", r"\bPosologie\s*/?\s*(?:Mode\s+d[’']?emploi|Administration)?\b"),
    ("contraindication", r"\bContre[- ]indications?\b"),
    ("warning", r"\bMises?\s+en\s+garde\s+et\s+pr[eé]cautions?\b"),
    ("interaction", r"\bInteractions?\b"),
    ("special_population", r"\bGrossesse\s*/?\s*Allaitement\b|\bFertilit[eé]\b"),
    ("adverse_effect", r"\bEffets?\s+ind[eé]sirables?\b"),
    ("overdose", r"\bSurdosage\b"),
    ("pharmacology", r"\bPropri[eé]t[eé]s\s*/?\s*Effets?\b|\bPharmacodynamie\b"),
    ("pharmacology", r"\bPharmacocin[eé]tique\b"),
    ("storage", r"\bConservation\b|\bRemarques?\s+particuli[eè]res?\b"),
    # German professional information
    ("indication", r"\bIndikationen\s*/?\s*(?:Anwendungsm[oö]glichkeiten)?\b"),
    ("dosage", r"\bDosierung\s*/?\s*Anwendung\b|\bAnwendung\s+und\s+Dosierung\b"),
    ("contraindication", r"\bKontraindikationen\b"),
    ("warning", r"\bWarnhinweise\s+und\s+Vorsicht(?:smassnahmen|sma[ßs]nahmen)\b"),
    ("interaction", r"\bInteraktionen\b"),
    ("special_population", r"\bSchwangerschaft\s*/?\s*Stillzeit\b|\bFertilit[aä]t\b"),
    ("adverse_effect", r"\bUnerw[uü]nschte\s+Wirkungen\b|\bNebenwirkungen\b"),
    ("overdose", r"\b[ÜU]berdosierung\b"),
    ("pharmacology", r"\bEigenschaften\s*/?\s*Wirkungen\b|\bPharmakodynamik\b"),
    ("pharmacology", r"\bPharmakokinetik\b"),
    ("storage", r"\bSonstige\s+Hinweise\b|\bHaltbarkeit\b|\bLagerung\b"),
    # Italian professional information
    ("indication", r"\bIndicazioni\s*/?\s*(?:possibilit[aà]\s+d[’']?impiego)?\b"),
    ("dosage", r"\bPosologia\s*/?\s*(?:Impiego|Modo\s+di\s+somministrazione)?\b"),
    ("contraindication", r"\bControindicazioni\b"),
    ("warning", r"\bAvvertenze\s+e\s+precauzioni\b"),
    ("interaction", r"\bInterazioni\b"),
    ("special_population", r"\bGravidanza\s*/?\s*Allattamento\b|\bFertilit[aà]\b"),
    ("adverse_effect", r"\bEffetti\s+indesiderati\b"),
    ("overdose", r"\bSovradosaggio\b"),
    ("pharmacology", r"\bPropriet[aà]\s*/?\s*Effetti\b|\bFarmacodinamica\b"),
    ("pharmacology", r"\bFarmacocinetica\b"),
    ("storage", r"\bConservazione\b|\bAltre\s+indicazioni\b"),
    # English fallback, sometimes present in translated or manufacturer texts
    ("indication", r"\bIndications?\s+and\s+usage\b|\bTherapeutic\s+indications?\b"),
    ("dosage", r"\bDosage\s+and\s+administration\b|\bPosology\b"),
    ("contraindication", r"\bContraindications?\b"),
    ("warning", r"\bWarnings?\s+and\s+precautions?\b|\bSpecial\s+warnings?\b"),
    ("interaction", r"\bInteractions?\b"),
    ("special_population", r"\bPregnancy\s+and\s+lactation\b|\bFertility\b"),
    ("adverse_effect", r"\bUndesirable\s+effects?\b|\bAdverse\s+reactions?\b"),
    ("overdose", r"\bOverdose\b"),
    ("pharmacology", r"\bPharmacodynamic\s+properties\b"),
    ("pharmacology", r"\bPharmacokinetic\s+properties\b"),
    ("storage", r"\bStorage\b|\bShelf\s+life\b"),
]

PRODUCT_TAG_HINTS = {
    "productname", "product", "medicinalproduct", "medicinalproductname", "preparationname",
    "praeparat", "praeparatename", "preparatename", "title", "bezeichnung", "name",
    "drugname", "artikelname", "trade_name", "tradename",
}
ACTIVE_TAG_HINTS = {
    "activesubstance", "active", "substance", "ingredient", "wirkstoff", "wirkstoffe",
    "principeactif", "principe_actif", "principioattivo", "composition", "dci", "inn",
}
HOLDER_TAG_HINTS = {
    "holder", "authorizationholder", "authorisationholder", "zulassungsinhaber", "zulassungsinhaberin",
    "titulaire", "titolare", "company", "firm", "manufacturer", "laboratory", "laboratoire",
}
LANG_TAG_HINTS = {"lang", "language", "sprache", "langue", "lingua"}
URL_TAG_HINTS = {"url", "href", "link", "pdf", "html"}
ID_TAG_HINTS = {"id", "identifier", "authnr", "zulassungsnummer", "registrationnumber", "swissmedicno", "nr"}


def clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = "\n".join(clean(item) for item in value)
    text = html.unescape(str(value))
    text = text.replace("\xa0", " ")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def norm(value: Any) -> str:
    text = unicodedata.normalize("NFKD", clean(value).upper())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("Æ", "AE").replace("Œ", "OE")
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


def sha(*parts: Any) -> str:
    return hashlib.sha1("|".join(clean(part) for part in parts).encode("utf-8", "ignore")).hexdigest()


def read_csv(path: Path, fields: Optional[Sequence[str]] = None) -> List[Dict[str, str]]:
    if not path or not path.exists():
        return []
    wanted = list(fields) if fields else None
    out: List[Dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if wanted is None:
                out.append({clean(k): clean(v) for k, v in row.items()})
            else:
                out.append({field: clean(row.get(field, "")) for field in wanted})
    return out


def write_csv(path: Path, fields: Sequence[str], rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: clean(row.get(field, "")) for field in fields})


def load_covered_row_ids(path: Optional[Path]) -> set[str]:
    if not path or not path.exists():
        return set()
    out: set[str] = set()
    for row in read_csv(path, fields=["row_id"]):
        rid = clean(row.get("row_id"))
        if rid:
            out.add(rid)
    return out


def tokens(value: str, min_len: int = 3) -> List[str]:
    stop = {"MG", "ML", "UI", "IU", "GR", "G", "L", "DE", "DU", "LA", "LE", "ET", "POUR", "SANS", "AVEC"}
    return [tok for tok in norm(value).split() if len(tok) >= min_len and tok not in stop]


def first_brand_token(row: Dict[str, str]) -> str:
    return tokens(row.get("nom", ""), min_len=3)[0] if tokens(row.get("nom", ""), min_len=3) else ""


def dosage_numbers(value: str) -> set[str]:
    return set(re.findall(r"\d+(?:[.,]\d+)?", clean(value)))


def strip_html_preserve_lines(value: str) -> str:
    text = clean(value)
    # Add newlines before/after likely heading and block tags.
    text = re.sub(r"(?is)<\s*(h[1-6]|title|p|div|section|article|li|tr|br|strong|b)\b[^>]*>", "\n", text)
    text = re.sub(r"(?is)<\s*/\s*(h[1-6]|title|p|div|section|article|li|tr|strong|b)\s*>", "\n", text)
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    lines = [clean(line) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def element_text(el: ET.Element) -> str:
    parts: List[str] = []
    if el.text:
        parts.append(el.text)
    for child in list(el):
        parts.append(element_text(child))
        if child.tail:
            parts.append(child.tail)
    return clean("\n".join(part for part in parts if part))


def collect_text_by_tag(el: ET.Element, hints: set[str], max_values: int = 12) -> List[str]:
    values: List[str] = []
    for node in el.iter():
        tag = local_name(node.tag)
        if tag in hints or any(hint in tag for hint in hints if len(hint) >= 5):
            txt = clean(" ".join(node.itertext()))
            if txt and len(txt) <= 500 and txt not in values:
                values.append(txt)
                if len(values) >= max_values:
                    break
        for key, val in node.attrib.items():
            k = local_name(key)
            if k in hints or any(hint in k for hint in hints if len(hint) >= 5):
                txt = clean(val)
                if txt and txt not in values:
                    values.append(txt)
                    if len(values) >= max_values:
                        break
    return values


def collect_urls(el: ET.Element) -> List[str]:
    urls: List[str] = []
    text = element_text(el)
    for match in re.finditer(r"https?://[^\s<'\"]+", text):
        url = clean(match.group(0))
        if url and url not in urls:
            urls.append(url)
    for node in el.iter():
        for key, val in node.attrib.items():
            k = local_name(key)
            if k in URL_TAG_HINTS or clean(val).startswith("http"):
                url = clean(val)
                if url.startswith("http") and url not in urls:
                    urls.append(url)
    return urls


def infer_language(text: str, explicit: str = "") -> str:
    exp = norm(explicit)
    if exp in {"DE", "DEU", "GERMAN"}:
        return "de"
    if exp in {"FR", "FRA", "FRENCH"}:
        return "fr"
    if exp in {"IT", "ITA", "ITALIAN"}:
        return "it"
    if exp in {"EN", "ENG", "ENGLISH"}:
        return "en"
    n = norm(text[:5000])
    scores = {
        "de": sum(term in n for term in ["INDIKATIONEN", "DOSIERUNG", "KONTRAINDIKATIONEN", "UNERWUNSCHTE", "WARNHINWEISE"]),
        "fr": sum(term in n for term in ["INDICATIONS", "POSOLOGIE", "CONTRE INDICATIONS", "EFFETS INDESIRABLES", "GROSSESSE"]),
        "it": sum(term in n for term in ["INDICAZIONI", "POSOLOGIA", "CONTROINDICAZIONI", "EFFETTI INDESIDERATI"]),
        "en": sum(term in n for term in ["INDICATIONS", "DOSAGE", "CONTRAINDICATIONS", "ADVERSE REACTIONS"]),
    }
    best = max(scores.items(), key=lambda kv: kv[1])
    return best[0] if best[1] else ""


def classify_section_heading(heading: str) -> str:
    h = clean(heading)
    for kind, pattern in SECTION_RULES:
        if re.search(pattern, h, flags=re.I):
            return kind
    return ""


def extract_sections(text: str) -> List[Dict[str, str]]:
    # Use line-preserved text to avoid swallowing headings into paragraphs.
    raw = strip_html_preserve_lines(text)
    if not raw:
        return []
    normalized = re.sub(r"\n{2,}", "\n", raw)
    matches: List[Tuple[int, str, str]] = []
    for kind, pattern in SECTION_RULES:
        for m in re.finditer(pattern, normalized, flags=re.I):
            start = m.start()
            line_start = normalized.rfind("\n", 0, start) + 1
            line_end = normalized.find("\n", start)
            if line_end == -1:
                line_end = min(len(normalized), start + 180)
            heading = clean(normalized[line_start:line_end]) or m.group(0)
            if len(heading) > 220:
                heading = clean(m.group(0))
            matches.append((line_start, kind, heading))
    # Numeric Swiss/SmPC-style headings fallback.
    for m in re.finditer(r"(?m)^\s*(?:4\.[1-9]|5\.[12]|6\.[1-6])\s+([^\n]{3,180})", normalized):
        heading = clean(m.group(0))
        kind = classify_section_heading(heading)
        if kind:
            matches.append((m.start(), kind, heading))
    if not matches:
        return []
    deduped: List[Tuple[int, str, str]] = []
    for pos, kind, heading in sorted(matches, key=lambda item: item[0]):
        if any(abs(pos - seen_pos) < 25 for seen_pos, _kind, _h in deduped):
            continue
        deduped.append((pos, kind, heading))
    rows: List[Dict[str, str]] = []
    seen: set[Tuple[str, str]] = set()
    for idx, (start, kind, heading) in enumerate(deduped):
        end = deduped[idx + 1][0] if idx + 1 < len(deduped) else len(normalized)
        section_text = clean(normalized[start:min(end, start + 9000)])
        if len(section_text) < 80:
            continue
        key = (kind, norm(section_text[:400]))
        if key in seen:
            continue
        seen.add(key)
        rows.append({"section_kind": kind, "section_title": heading, "section_text": section_text})
    return rows


def looks_like_record(el: ET.Element) -> bool:
    text = element_text(el)
    if len(text) < 250:
        return False
    tags = {local_name(node.tag) for node in el.iter()}
    has_name = bool(tags & PRODUCT_TAG_HINTS) or any("product" in tag or "praeparat" in tag or tag == "title" for tag in tags)
    has_info = bool(re.search(r"Indikationen|Indications|Posologie|Dosierung|Kontraindikationen|Contre[- ]indications|Unerw", text, flags=re.I))
    return has_name or has_info


def iter_xml_records_from_bytes(data: bytes, source_name: str) -> Iterator[Dict[str, Any]]:
    try:
        root = ET.fromstring(data)
    except Exception as exc:
        sys.stderr.write(f"[WARN] XML parse failed for {source_name}: {exc}\n")
        return
    candidates: List[ET.Element] = []
    # Prefer direct child records if possible.
    for child in list(root):
        if looks_like_record(child):
            candidates.append(child)
    if not candidates and looks_like_record(root):
        # Find smaller nested records. Avoid returning the entire root unless needed.
        for el in root.iter():
            if el is root:
                continue
            tag = local_name(el.tag)
            if any(hint in tag for hint in ["product", "document", "preparation", "article", "information", "aips"]):
                if looks_like_record(el):
                    candidates.append(el)
        if not candidates:
            candidates = [root]
    seen_hashes: set[str] = set()
    for idx, el in enumerate(candidates):
        text = element_text(el)
        rec_hash = sha(source_name, idx, text[:1000])
        if rec_hash in seen_hashes:
            continue
        seen_hashes.add(rec_hash)
        product_names = collect_text_by_tag(el, PRODUCT_TAG_HINTS)
        active_names = collect_text_by_tag(el, ACTIVE_TAG_HINTS)
        holder_names = collect_text_by_tag(el, HOLDER_TAG_HINTS)
        languages = collect_text_by_tag(el, LANG_TAG_HINTS, max_values=3)
        ids = collect_text_by_tag(el, ID_TAG_HINTS, max_values=6)
        urls = collect_urls(el)
        title = product_names[0] if product_names else ""
        active = "; ".join(active_names[:6])
        holder = "; ".join(holder_names[:4])
        record_id = ids[0] if ids else sha(source_name, title, active, holder)[:16]
        language = infer_language(text, languages[0] if languages else "")
        yield {
            "record_id": record_id,
            "title": title,
            "active": active,
            "holder": holder,
            "language": language,
            "urls": urls,
            "source_name": source_name,
            "text": text,
            "sections": extract_sections(text),
        }


def iter_aips_inputs(path: Path) -> Iterator[Tuple[str, bytes]]:
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path, "r") as zf:
            for name in zf.namelist():
                if name.lower().endswith((".xml", ".html", ".xhtml")):
                    yield f"{path.name}:{name}", zf.read(name)
    elif path.is_file():
        yield path.name, path.read_bytes()
    elif path.is_dir():
        for sub in sorted(path.rglob("*")):
            if sub.is_file() and sub.suffix.lower() in {".xml", ".html", ".xhtml"}:
                yield str(sub.relative_to(path)), sub.read_bytes()
    else:
        raise FileNotFoundError(path)


def load_aips_records(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for source_name, data in iter_aips_inputs(path):
        if source_name.lower().endswith((".html", ".xhtml")):
            text = data.decode("utf-8", "ignore")
            records.append({
                "record_id": sha(source_name, text[:500])[:16],
                "title": "",
                "active": "",
                "holder": "",
                "language": infer_language(text),
                "urls": [],
                "source_name": source_name,
                "text": text,
                "sections": extract_sections(text),
            })
        else:
            records.extend(iter_xml_records_from_bytes(data, source_name))
    return records


def score_record(row: Dict[str, str], record: Dict[str, Any]) -> float:
    brand = norm(row.get("nom"))
    generic = norm(row.get("nom_generique"))
    lab = norm(row.get("labo"))
    form = norm(row.get("forme"))
    dosage = norm(row.get("dosage"))
    title = norm(record.get("title"))
    active = norm(record.get("active"))
    holder = norm(record.get("holder"))
    all_text_head = norm(" ".join([record.get("title", ""), record.get("active", ""), record.get("holder", ""), record.get("text", "")[:1500]]))
    score = 0.0
    if brand and title:
        if brand == title or brand in title or title in brand:
            score += 0.55
        else:
            bt = first_brand_token(row)
            if bt and (title.startswith(bt) or bt in title.split()[:4]):
                score += 0.40
    if generic:
        if active and (generic in active or active in generic):
            score += 0.30
        else:
            gen_tokens = [t for t in tokens(generic, 4) if len(t) >= 4]
            if gen_tokens and all(tok in all_text_head for tok in gen_tokens[:2]):
                score += 0.18
            elif gen_tokens and any(tok in all_text_head for tok in gen_tokens[:3]):
                score += 0.10
    if lab and holder:
        lab_tokens = [t for t in tokens(lab, 4) if len(t) >= 4]
        if lab_tokens and any(tok in holder for tok in lab_tokens[:4]):
            score += 0.08
    # Dosage/form are weak but help separate variants.
    dn = dosage_numbers(dosage)
    rn = dosage_numbers(record.get("text", "")[:2500]) | dosage_numbers(record.get("title", ""))
    if dn and rn and (dn & rn):
        score += 0.04
    form_tokens = [t for t in tokens(form, 5) if len(t) >= 5]
    if form_tokens and any(tok in all_text_head for tok in form_tokens[:3]):
        score += 0.03
    if record.get("sections"):
        score += 0.03
    return min(score, 0.97)


def source_url_for_record(record: Dict[str, Any], fallback_source: str) -> str:
    for url in record.get("urls", []):
        if "swissmedicinfo" in url or "refdata" in url or url.lower().endswith((".pdf", ".html")):
            return url
    return fallback_source


def source_system_for_record(record: Dict[str, Any]) -> str:
    text = norm(record.get("text", "")[:1500])
    if any(term in text for term in ["PATIENTENINFORMATION", "INFORMATION DESTINEE AUX PATIENTS", "INFORMATION DESTINEE AU PATIENT", "FOGLIETTO ILLUSTRATIVO"]):
        return "swissmedic_aips_patient_info"
    return "swissmedic_aips_professional_info"


def apply_sections(sections: List[Dict[str, str]], row: Dict[str, str], record: Dict[str, Any], score: float) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()
    source_system = source_system_for_record(record)
    source_file = source_url_for_record(record, record.get("source_name", "swissmedic_aips_xml"))
    for section in sections:
        srow = {
            "row_id": row.get("row_id", ""),
            "amm": row.get("amm", ""),
            "nom": row.get("nom", ""),
            "nom_generique": row.get("nom_generique", ""),
            "source_system": source_system,
            "source_file": source_file,
            "source_record_id": record.get("record_id", ""),
            "match_query": row.get("nom", "") or row.get("nom_generique", ""),
            "match_score": f"{score:.2f}",
            "section_kind": section.get("section_kind", ""),
            "section_title": section.get("section_title", ""),
            "section_text": section.get("section_text", ""),
            "language": record.get("language", ""),
            "authority_level": "fallback_swissmedic_aips",
            "confidence": f"{max(0.60, min(score, 0.93)):.2f}",
            "evidence_rank": "76",
            "retrieved_at": now,
        }
        srow["content_hash"] = sha(srow["row_id"], srow["source_system"], srow["section_kind"], srow["section_text"][:500])
        out.append(srow)
    return out


def row_is_swissmedic_candidate(row: Dict[str, str], summary_by_id: Dict[str, Dict[str, str]]) -> Tuple[bool, str]:
    rid = row.get("row_id", "")
    summary = summary_by_id.get(rid, {})
    top = summary.get("top_source_name", "")
    pays = norm(row.get("pays") or row.get("catalog_pays") or row.get("list_amm_pays") or summary.get("pays"))
    product_type = summary.get("product_type", "")
    if "Swissmedic" in top:
        return True, "top_source_swissmedic"
    if any(tok in pays for tok in ["SUISSE", "SWITZERLAND"]):
        return True, "country_switzerland"
    # AIPS is especially useful for blood/biologic products marketed across Europe.
    if product_type == "biologic_or_blood_product" and any(tok in pays for tok in ["ALLEMAGNE", "FRANCE", "AUTRICHE", "SUEDE", "DENEMARK"]):
        return True, "biologic_or_blood_eu_fallback"
    return False, "not_swissmedic_routed"


def load_remaining_rows(args: argparse.Namespace) -> Tuple[List[Dict[str, str]], Dict[str, Dict[str, str]], set[str], Dict[str, int]]:
    """Load and route rows for Swissmedic.

    v2 is intentionally less brittle than v1:
    - it reads the full CSVs instead of a fixed field subset, then normalizes the fields we need;
    - it reports raw/filtered/routed counts;
    - if routing returns 0 but the medicine-summary file contains Swissmedic rows, it falls back
      to building the candidate queue from the summary CSV itself. This prevents silent 0-row output
      when the remaining CSV has different country/header naming or was generated from another run.
    """
    diagnostics: Dict[str, int] = {}
    remaining_fields = ["row_id", "amm", "nom", "nom_generique", "dosage", "forme", "labo", "pays", "catalog_pays", "list_amm_pays"]

    raw_rows_full = read_csv(Path(args.remaining))
    rows: List[Dict[str, str]] = []
    for raw in raw_rows_full:
        rows.append({field: clean(raw.get(field, "")) for field in remaining_fields})
    diagnostics["raw_remaining_rows"] = len(rows)

    summary_full = read_csv(Path(args.medicine_summary)) if args.medicine_summary else []
    diagnostics["medicine_summary_rows"] = len(summary_full)
    summary_fields = ["row_id", "amm", "nom", "nom_generique", "dosage", "forme", "labo", "pays", "product_type", "top_source_name"]
    summary_by_id: Dict[str, Dict[str, str]] = {}
    for raw in summary_full:
        rid = clean(raw.get("row_id"))
        if rid:
            summary_by_id[rid] = {field: clean(raw.get(field, "")) for field in summary_fields}

    covered = load_covered_row_ids(Path(args.covered_sections)) if args.covered_sections else set()
    diagnostics["covered_row_ids_from_extra_sections"] = len(covered)

    if args.examples:
        wanted = [norm(item) for item in args.examples.split(",") if norm(item)]
        rows = [r for r in rows if any(w in norm(r.get("nom", "")) or w in norm(r.get("nom_generique", "")) for w in wanted)]
    diagnostics["after_examples_filter"] = len(rows)

    if args.exclude_covered:
        rows = [r for r in rows if r.get("row_id", "") not in covered]
    diagnostics["after_exclude_covered"] = len(rows)

    if not args.disable_routing:
        routed: List[Dict[str, str]] = []
        for r in rows:
            ok, reason = row_is_swissmedic_candidate(r, summary_by_id)
            if ok:
                r = dict(r)
                r["_candidate_reason"] = reason
                summary = summary_by_id.get(r.get("row_id", ""), {})
                r["_top_source_name"] = summary.get("top_source_name", "")
                r["_product_type"] = summary.get("product_type", "")
                routed.append(r)
        diagnostics["after_swissmedic_routing"] = len(routed)

        # Safety fallback: if remaining CSV headers/country fields do not route but the generated
        # guaranteed_source summary has Swissmedic candidates, build from that summary.
        if not routed and summary_by_id:
            fallback: List[Dict[str, str]] = []
            for rid, summary in summary_by_id.items():
                if args.exclude_covered and rid in covered:
                    continue
                top = summary.get("top_source_name", "")
                pays = norm(summary.get("pays", ""))
                ptype = summary.get("product_type", "")
                if "Swissmedic" in top or any(tok in pays for tok in ["SUISSE", "SWITZERLAND"]) or (
                    ptype == "biologic_or_blood_product" and any(tok in pays for tok in ["ALLEMAGNE", "FRANCE", "AUTRICHE", "SUEDE", "DENEMARK"])
                ):
                    row = {field: clean(summary.get(field, "")) for field in remaining_fields}
                    row["row_id"] = rid
                    row["_candidate_reason"] = "fallback_from_medicine_summary"
                    row["_top_source_name"] = top
                    row["_product_type"] = ptype
                    fallback.append(row)
            routed = fallback
            diagnostics["after_summary_fallback"] = len(routed)
        else:
            diagnostics["after_summary_fallback"] = 0
        rows = routed
    else:
        diagnostics["after_swissmedic_routing"] = len(rows)
        diagnostics["after_summary_fallback"] = 0

    if args.limit and args.limit > 0:
        rows = rows[:args.limit]
    diagnostics["final_rows"] = len(rows)
    return rows, summary_by_id, covered, diagnostics

def download_aips(url: str, output_path: Path, timeout: int = 60) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "tunisia-cdss-swissmedic-aips-adapter/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        output_path.write_bytes(response.read())
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remaining", required=True, help="remaining_382_medicines_all_available_details.csv")
    parser.add_argument("--medicine-summary", default="", help="guaranteed_source_medicine_summary.csv for routing")
    parser.add_argument("--covered-sections", default="", help="CSV of already recovered section rows, e.g. CECMED v3")
    parser.add_argument("--exclude-covered", action="store_true")
    parser.add_argument("--aips-input", default="", help="Local Swissmedic AIPS XML/ZIP/directory")
    parser.add_argument("--aips-url", default="", help="Optional direct URL to an AIPS XML/ZIP download")
    parser.add_argument("--download-path", default="swissmedic_aips_download.xml", help="Path to save --aips-url")
    parser.add_argument("--output", default="swissmedic_aips_fallback_sections.csv")
    parser.add_argument("--query-status-output", default="swissmedic_aips_query_status.csv")
    parser.add_argument("--summary", default="swissmedic_aips_summary.json")
    parser.add_argument("--examples", default="", help="Comma-separated examples to process")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--min-match-score", type=float, default=0.48)
    parser.add_argument("--max-records-per-row", type=int, default=3)
    parser.add_argument("--disable-routing", action="store_true", help="Try all remaining rows, not just Swissmedic-routed candidates")
    args = parser.parse_args()

    rows, summary_by_id, covered, diagnostics = load_remaining_rows(args)
    status_rows: List[Dict[str, Any]] = []
    section_rows: List[Dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()

    aips_path: Optional[Path] = Path(args.aips_input) if args.aips_input else None
    if args.aips_url:
        aips_path = download_aips(args.aips_url, Path(args.download_path))

    if not aips_path:
        for row in rows:
            status_rows.append({
                "row_id": row.get("row_id", ""),
                "amm": row.get("amm", ""),
                "nom": row.get("nom", ""),
                "nom_generique": row.get("nom_generique", ""),
                "dosage": row.get("dosage", ""),
                "forme": row.get("forme", ""),
                "labo": row.get("labo", ""),
                "pays": row.get("pays", ""),
                "already_covered_by_extra_sections": "yes" if row.get("row_id", "") in covered else "no",
                "candidate_reason": row.get("_candidate_reason", ""),
                "top_source_name": row.get("_top_source_name", summary_by_id.get(row.get("row_id", ""), {}).get("top_source_name", "")),
                "product_type": row.get("_product_type", summary_by_id.get(row.get("row_id", ""), {}).get("product_type", "")),
                "status": "missing_aips_input",
                "attempts": f"Download AIPS XML/ZIP from {SWISSMEDIC_DOWNLOAD_HOME} or pass --aips-url.",
            })
            status_counts["missing_aips_input"] += 1
        write_csv(Path(args.output), OUTPUT_FIELDS, [])
        write_csv(Path(args.query_status_output), STATUS_FIELDS, status_rows)
        summary = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            **diagnostics,
            "remaining_input_rows_after_filters": len(rows),
            "aips_input": "",
            "section_rows": 0,
            "row_ids_with_swissmedic_sections": 0,
            "status_counts": dict(status_counts),
            "outputs": {"sections": args.output, "query_status": args.query_status_output, "summary": args.summary},
            "note": "No --aips-input or --aips-url was provided. Status CSV is a Swissmedic candidate queue.",
        }
        Path(args.summary).write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    records = load_aips_records(aips_path)
    usable_records = [rec for rec in records if rec.get("sections")]
    print(f"Loaded AIPS records: {len(records)}; with parsed sections: {len(usable_records)}", file=sys.stderr)

    for row in rows:
        attempts: List[str] = []
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for rec in usable_records:
            score = score_record(row, rec)
            if score >= max(0.25, args.min_match_score - 0.18):
                scored.append((score, rec))
        scored.sort(key=lambda item: item[0], reverse=True)
        chosen_score = 0.0
        chosen: Optional[Dict[str, Any]] = None
        chosen_sections: List[Dict[str, str]] = []
        for score, rec in scored[: args.max_records_per_row]:
            attempts.append(f"{rec.get('title') or rec.get('record_id')}:{score:.2f}:{len(rec.get('sections', []))}")
            if score >= args.min_match_score and rec.get("sections"):
                chosen = rec
                chosen_score = score
                chosen_sections = rec.get("sections", [])
                break
        if chosen and chosen_sections:
            source_system = source_system_for_record(chosen)
            source_counts[source_system] += 1
            status = "ok_swissmedic_aips_sections"
            status_counts[status] += 1
            section_rows.extend(apply_sections(chosen_sections, row, chosen, chosen_score))
        else:
            status = "no_swissmedic_match_above_threshold" if scored else "no_candidate_records_scored"
            status_counts[status] += 1
        status_rows.append({
            "row_id": row.get("row_id", ""),
            "amm": row.get("amm", ""),
            "nom": row.get("nom", ""),
            "nom_generique": row.get("nom_generique", ""),
            "dosage": row.get("dosage", ""),
            "forme": row.get("forme", ""),
            "labo": row.get("labo", ""),
            "pays": row.get("pays", ""),
            "already_covered_by_extra_sections": "yes" if row.get("row_id", "") in covered else "no",
            "candidate_reason": row.get("_candidate_reason", ""),
            "top_source_name": row.get("_top_source_name", summary_by_id.get(row.get("row_id", ""), {}).get("top_source_name", "")),
            "product_type": row.get("_product_type", summary_by_id.get(row.get("row_id", ""), {}).get("product_type", "")),
            "chosen_source": source_system_for_record(chosen) if chosen else "",
            "chosen_record_id": chosen.get("record_id", "") if chosen else "",
            "chosen_record_title": chosen.get("title", "") if chosen else "",
            "chosen_record_holder": chosen.get("holder", "") if chosen else "",
            "chosen_record_url": source_url_for_record(chosen, chosen.get("source_name", "")) if chosen else "",
            "match_score": f"{chosen_score:.2f}" if chosen else "",
            "sections": len(chosen_sections),
            "status": status,
            "attempts": " | ".join(attempts[:5]),
        })

    write_csv(Path(args.output), OUTPUT_FIELDS, section_rows)
    write_csv(Path(args.query_status_output), STATUS_FIELDS, status_rows)
    covered_ids = {row.get("row_id", "") for row in section_rows if row.get("row_id")}
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        **diagnostics,
        "remaining_input_rows_after_filters": len(rows),
        "aips_input": str(aips_path),
        "aips_records_loaded": len(records),
        "aips_records_with_sections": len(usable_records),
        "section_rows": len(section_rows),
        "row_ids_with_swissmedic_sections": len(covered_ids),
        "source_row_counts": dict(source_counts),
        "status_counts": dict(status_counts),
        "outputs": {"sections": args.output, "query_status": args.query_status_output, "summary": args.summary},
        "notes": [
            "Swissmedic AIPS is a foreign official source. Treat matches as B-level only for same product/MAH/strength/form/route; otherwise C-level same-DCI fallback.",
            "For exact Tunisia A-level evidence, still request DPM/ANMPS or MAH-approved RCP/notice documents.",
        ],
    }
    Path(args.summary).write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
