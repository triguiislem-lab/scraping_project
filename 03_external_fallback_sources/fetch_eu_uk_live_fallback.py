#!/usr/bin/env python3
"""
Fetch EU/UK regulatory fallback sections for medicines still missing evidence.

Order by default:
1. EMA ePI API (FHIR ePI pilot, regulator source).
2. MHRA products pages (UK regulator source).
3. EMC SmPC HTML pages (regulated UK medicine information, private host).

The output follows the same row-level section contract as the other fallback
normalizers so it can be merged into the Tunisia CDSS evidence layer.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


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

SECTION_RULES = [
    ("indication", r"4\.1\s+Therapeutic indications?"),
    ("dosage", r"4\.2\s+Posology and method of administration"),
    ("contraindication", r"4\.3\s+Contraindications?"),
    ("warning", r"4\.4\s+Special warnings and precautions for use"),
    ("interaction", r"4\.5\s+Interaction with other medicinal products(?: and other forms of interaction)?"),
    ("special_population", r"4\.6\s+Fertility, pregnancy and lactation"),
    ("adverse_effect", r"4\.8\s+Undesirable effects?"),
    ("overdose", r"4\.9\s+Overdose"),
    ("pharmacology", r"5\.1\s+Pharmacodynamic properties"),
    ("pharmacology", r"5\.2\s+Pharmacokinetic properties"),
]
SMPC_SECTION_RE = re.compile(r"^(4\.[12345689]|5\.[12])\b")

MHRA_SUBSTANCE_INDEX_URL = "https://products.mhra.gov.uk/substance/index.json"
_MHRA_INDEX_CACHE: Dict[str, str] = {}


def clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = "\n".join(clean(item) for item in value)
    text = str(value).encode("utf-8", "ignore").decode("utf-8", "ignore")
    text = html.unescape(text)
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
    slug = re.sub(r"[^A-Za-z0-9]+", "_", clean(value)).strip("_")[:90]
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


def fetch_text(url: str, raw_path: Path, timeout: int, resume: bool) -> Tuple[str, str, str, bool]:
    if resume and raw_path.exists() and raw_path.stat().st_size > 0:
        return "cached", "", raw_path.read_text(encoding="utf-8-sig", errors="ignore"), True
    status, message, body = request_bytes(url, timeout)
    if status == "ok":
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(body)
        return status, message, body.decode("utf-8", "ignore"), False
    return status, message, "", False


def fetch_json(url: str, raw_path: Path, timeout: int, resume: bool) -> Tuple[str, str, Any, bool]:
    text_status, message, text, cached = fetch_text(url, raw_path, timeout, resume)
    if text_status in {"ok", "cached"} and text:
        try:
            return text_status, message, json.loads(text), cached
        except Exception as exc:
            return "parse_error", clean(exc), {}, cached
    return text_status, message, {}, cached


def strip_html(value: str) -> str:
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", value)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</tr>|</h[1-6]>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return clean(text)


def classify_title(title: str) -> str:
    title_clean = clean(title)
    if re.match(r"^\d+\.\d+\b", title_clean) and not SMPC_SECTION_RE.match(title_clean):
        return ""
    title_norm = norm(title)
    for kind, pattern in SECTION_RULES:
        if re.search(pattern, title, flags=re.I):
            return kind
    if "INDICATION" in title_norm:
        return "indication"
    if "POSOLOGY" in title_norm or "DOSAGE" in title_norm:
        return "dosage"
    if "CONTRAINDICATION" in title_norm:
        return "contraindication"
    if "INTERACTION" in title_norm:
        return "interaction"
    if "UNDESIRABLE" in title_norm or "ADVERSE" in title_norm:
        return "adverse_effect"
    if "PREGNANCY" in title_norm or "LACTATION" in title_norm:
        return "special_population"
    if "WARNING" in title_norm or "PRECAUTION" in title_norm:
        return "warning"
    if "THERAPEUTIC USE" in title_norm or "CLINICAL USE" in title_norm:
        return "indication"
    if "MECHANISM" in title_norm or "PHARMACOLOGY" in title_norm or "PHARMACODYNAMIC" in title_norm:
        return "pharmacology"
    if "TOXICITY" in title_norm or "SAFETY" in title_norm:
        return "warning"
    return ""


def extract_smpc_sections(text: str) -> List[Dict[str, str]]:
    normalized = clean(text)
    matches: List[Tuple[int, str, str]] = []
    for kind, pattern in SECTION_RULES:
        for match in re.finditer(pattern, normalized, flags=re.I):
            if not SMPC_SECTION_RE.match(match.group(0)):
                continue
            matches.append((match.start(), kind, match.group(0)))
    matches.sort(key=lambda item: item[0])

    deduped: List[Tuple[int, str, str]] = []
    last_pos = -1
    for pos, kind, title in matches:
        if pos > last_pos + 10:
            deduped.append((pos, kind, title))
            last_pos = pos

    rows: List[Dict[str, str]] = []
    seen = set()
    for idx, (start, kind, title) in enumerate(deduped):
        next_start = deduped[idx + 1][0] if idx + 1 < len(deduped) else len(normalized)
        section_text = clean(normalized[start : min(next_start, start + 8000)])
        if len(section_text) < 80:
            continue
        key = (kind, section_text[:300])
        if key in seen:
            continue
        seen.add(key)
        rows.append({"section_kind": kind, "section_title": clean(title), "section_text": section_text})
    return rows


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        import io
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        pass
    try:
        text = pdf_bytes.decode("latin-1", errors="ignore")
        return re.sub(r"[\x00-\x08\x0e-\x1f]", " ", text)
    except Exception:
        return ""


def _recursive_urls(value: Any) -> List[str]:
    urls: List[str] = []
    if isinstance(value, dict):
        for item in value.values():
            urls.extend(_recursive_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(_recursive_urls(item))
    elif isinstance(value, str) and ("/en/medicines/" in value or "/en/documents/" in value or value.startswith("http")):
        urls.append(value)
    return list(dict.fromkeys(clean(url) for url in urls if clean(url)))


def extract_emc_html_sections(page: str) -> List[Dict[str, str]]:
    """Extract EMC SmPC sections from the native <details>/<summary> structure."""
    rows: List[Dict[str, str]] = []
    seen = set()
    for match in re.finditer(r"(?is)<details[^>]*>\s*<summary[^>]*>(.*?)</summary>(.*?)</details>", page):
        title = strip_html(match.group(1))
        kind = classify_title(title)
        if not kind:
            continue
        body = strip_html(match.group(2))
        body = re.sub(r"^sectionWrapper\s+", "", body, flags=re.I)
        if len(body) < 80:
            continue
        section_text = clean(f"{title} {body}")
        key = (kind, title, section_text[:300])
        if key in seen:
            continue
        seen.add(key)
        rows.append({"section_kind": kind, "section_title": title, "section_text": section_text})
    return rows


def recursive_sections(value: Any) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    if isinstance(value, dict):
        title = clean(value.get("title"))
        text_value = ""
        text_obj = value.get("text")
        if isinstance(text_obj, dict):
            text_value = strip_html(clean(text_obj.get("div") or text_obj.get("text") or ""))
        if title and text_value:
            kind = classify_title(title)
            if kind and len(text_value) >= 80:
                rows.append({"section_kind": kind, "section_title": title, "section_text": text_value})
        for item in value.values():
            rows.extend(recursive_sections(item))
    elif isinstance(value, list):
        for item in value:
            rows.extend(recursive_sections(item))
    return rows


def recursive_ids(value: Any, needle: str) -> List[str]:
    ids: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and "id" in key.lower() and needle in key.lower():
                ids.append(item)
            ids.extend(recursive_ids(item, needle))
    elif isinstance(value, list):
        for item in value:
            ids.extend(recursive_ids(item, needle))
    return list(dict.fromkeys(clean(item) for item in ids if clean(item)))


def ema_sections(query: str, raw_dir: Path, timeout: int, resume: bool, max_docs: int) -> Tuple[str, str, List[Dict[str, str]]]:
    encoded = urllib.parse.quote(query)
    search_url = f"https://www.ema.europa.eu/en/medicines/find-medicine?search_api_fulltext={encoded}&_format=json"
    raw = cache_path(raw_dir, "ema_search", query, "json")
    status, _message, data, _ = fetch_json(search_url, raw, timeout, resume)
    if status not in {"ok", "cached"} or not data:
        return _ema_epar_fallback(query, raw_dir, timeout, resume, max_docs)

    product_urls: List[str] = []
    items = data if isinstance(data, list) else data.get("results", []) if isinstance(data, dict) else []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("url", "path", "view_node", "field_ema_web_product_number"):
                url = clean(item.get(key))
                if url:
                    product_urls.append(urllib.parse.urljoin("https://www.ema.europa.eu", url))
            product_urls.extend(urllib.parse.urljoin("https://www.ema.europa.eu", url) for url in _recursive_urls(item))
    product_urls = list(dict.fromkeys(url for url in product_urls if "/en/medicines/" in url or "/en/documents/" in url))

    for product_url in product_urls[:max_docs]:
        raw_page = raw_dir / "ema_product" / f"{sha(product_url)[:16]}_{safe_filename(query)}.html"
        page_status, _, page_html, _ = fetch_text(product_url, raw_page, timeout, resume)
        if page_status not in {"ok", "cached"}:
            continue
        smpc_links = re.findall(
            r'href=["\']([^"\']*(?:SmPC|summary-of-product-characteristics|product-information)[^"\']*\.pdf(?:\?[^"\']*)?)["\']',
            page_html,
            flags=re.I,
        )
        for smpc_link in list(dict.fromkeys(smpc_links))[:max_docs]:
            smpc_url = urllib.parse.urljoin("https://www.ema.europa.eu", smpc_link)
            raw_pdf_txt = raw_dir / "ema_smpc_txt" / f"{sha(smpc_url)[:16]}_{safe_filename(query)}.txt"
            if resume and raw_pdf_txt.exists() and raw_pdf_txt.stat().st_size > 0:
                text = raw_pdf_txt.read_text(encoding="utf-8-sig", errors="ignore")
            else:
                pdf_status, _, pdf_bytes = request_bytes(smpc_url, timeout)
                if pdf_status != "ok" or not pdf_bytes:
                    continue
                text = _extract_pdf_text(pdf_bytes)
                raw_pdf_txt.parent.mkdir(parents=True, exist_ok=True)
                raw_pdf_txt.write_text(text, encoding="utf-8", errors="ignore")
            sections = extract_smpc_sections(text)
            if sections:
                return "ok", smpc_url, sections
    return _ema_epar_fallback(query, raw_dir, timeout, resume, max_docs)


def _ema_epar_fallback(query: str, raw_dir: Path, timeout: int, resume: bool, max_docs: int) -> Tuple[str, str, List[Dict[str, str]]]:
    encoded = urllib.parse.quote(query)
    search_url = f"https://www.ema.europa.eu/en/search?search_api_fulltext={encoded}"
    raw = cache_path(raw_dir, "ema_site_search", query, "html")
    status, _, body, _ = fetch_text(search_url, raw, timeout, resume)
    if status not in {"ok", "cached"} or not body:
        return f"ema_site_search_{status}", "", []

    links = re.findall(r'href=["\']([^"\']+)["\']', body, flags=re.I)
    urls = [
        urllib.parse.urljoin("https://www.ema.europa.eu", link)
        for link in links
        if "/en/medicines/" in link or "/en/documents/" in link
    ]
    urls = list(dict.fromkeys(urls))

    product_urls = [url for url in urls if "/en/medicines/" in url][:max_docs]
    document_urls = [
        url
        for url in urls
        if "/en/documents/" in url and re.search(r"(product-information|summary-product-characteristics|smpc)", url, flags=re.I)
    ][:max_docs]

    for product_url in product_urls:
        raw_page = raw_dir / "ema_product" / f"{sha(product_url)[:16]}_{safe_filename(query)}.html"
        page_status, _, page_html, _ = fetch_text(product_url, raw_page, timeout, resume)
        if page_status not in {"ok", "cached"}:
            continue
        product_links = re.findall(
            r'href=["\']([^"\']*(?:SmPC|summary-of-product-characteristics|product-information)[^"\']*\.pdf(?:\?[^"\']*)?)["\']',
            page_html,
            flags=re.I,
        )
        document_urls.extend(urllib.parse.urljoin("https://www.ema.europa.eu", link) for link in product_links)

    for document_url in list(dict.fromkeys(document_urls))[:max_docs]:
        raw_txt = raw_dir / "ema_smpc_txt" / f"{sha(document_url)[:16]}_{safe_filename(query)}.txt"
        if resume and raw_txt.exists() and raw_txt.stat().st_size > 0:
            text = raw_txt.read_text(encoding="utf-8-sig", errors="ignore")
        else:
            pdf_status, _, pdf_bytes = request_bytes(document_url, timeout)
            if pdf_status != "ok" or not pdf_bytes:
                continue
            text = _extract_pdf_text(pdf_bytes)
            raw_txt.parent.mkdir(parents=True, exist_ok=True)
            raw_txt.write_text(text, encoding="utf-8", errors="ignore")
        sections = extract_smpc_sections(text)
        if sections:
            return "ema_site_search", document_url, sections

    return "ema_no_match", "", []


def emc_sections(query: str, raw_dir: Path, timeout: int, resume: bool, max_docs: int) -> Tuple[str, str, List[Dict[str, str]]]:
    search_url = f"https://www.medicines.org.uk/emc/search?q={urllib.parse.quote(query)}"
    search_raw = cache_path(raw_dir, "emc_search", query, "html")
    status, message, body, _ = fetch_text(search_url, search_raw, timeout, resume)
    if status not in {"ok", "cached"}:
        return status or message, "", []
    links = re.findall(r'href=["\']([^"\']*/emc/(?:product|medicine)/\d+/(?:smpc|spc)[^"\']*)["\']', body, flags=re.I)
    links = list(dict.fromkeys(links))
    for link in links[:max_docs]:
        url = urllib.parse.urljoin("https://www.medicines.org.uk", link)
        raw = raw_dir / "emc_smpc" / f"{sha(url)[:16]}_{safe_filename(query)}.html"
        page_status, _, page, _ = fetch_text(url, raw, timeout, resume)
        if page_status in {"ok", "cached"}:
            sections = extract_emc_html_sections(page) or extract_smpc_sections(strip_html(page))
            if sections:
                return status, url, sections
    return status, "", []


def _load_mhra_index(raw_dir: Path, timeout: int, resume: bool) -> Dict[str, str]:
    raw = raw_dir / "mhra_index" / "substance_index.json"
    status, _, data, _ = fetch_json(MHRA_SUBSTANCE_INDEX_URL, raw, timeout, resume)
    if status not in {"ok", "cached"} or not isinstance(data, (list, dict)):
        return {}
    index: Dict[str, str] = {}
    items = data if isinstance(data, list) else data.get("substances", [])
    if not isinstance(items, list):
        items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = norm(item.get("name") or item.get("substance") or item.get("title") or "")
        url = clean(item.get("url") or item.get("href") or item.get("path") or "")
        if name and url:
            index[name] = url
    return index


def mhra_sections(query: str, raw_dir: Path, timeout: int, resume: bool, max_docs: int) -> Tuple[str, str, List[Dict[str, str]]]:
    global _MHRA_INDEX_CACHE
    if not _MHRA_INDEX_CACHE:
        _MHRA_INDEX_CACHE = _load_mhra_index(raw_dir, timeout, resume)
    if not _MHRA_INDEX_CACHE:
        return "mhra_index_unavailable", "", []

    norm_query = norm(query)
    product_url = _MHRA_INDEX_CACHE.get(norm_query, "")
    if not product_url:
        tokens = [token for token in norm_query.split() if len(token) > 2]
        product_url = next(
            (url for key, url in _MHRA_INDEX_CACHE.items() if tokens and all(token in key for token in tokens)),
            "",
        )
    if not product_url:
        return "mhra_not_in_index", "", []

    full_url = urllib.parse.urljoin("https://products.mhra.gov.uk", product_url)
    raw_page = raw_dir / "mhra_product" / f"{sha(full_url)[:16]}_{safe_filename(query)}.html"
    page_status, _, page_html, _ = fetch_text(full_url, raw_page, timeout, resume)
    if page_status not in {"ok", "cached"}:
        return page_status, "", []

    spc_links = re.findall(r'href=["\']([^"\']*(?:spc|smpc|summary-of-product)[^"\']*)["\']', page_html, flags=re.I)
    for link in list(dict.fromkeys(spc_links))[:max_docs]:
        spc_url = urllib.parse.urljoin("https://products.mhra.gov.uk", link)
        raw_spc = raw_dir / "mhra_spc" / f"{sha(spc_url)[:16]}_{safe_filename(query)}.html"
        spc_status, _, spc_html, _ = fetch_text(spc_url, raw_spc, timeout, resume)
        if spc_status in {"ok", "cached"}:
            sections = extract_smpc_sections(strip_html(spc_html))
            if sections:
                return "ok", spc_url, sections
    return "mhra_no_spc", "", []


def chembl_sections(query: str, raw_dir: Path, timeout: int, resume: bool, max_docs: int) -> Tuple[str, str, List[Dict[str, str]]]:
    encoded = urllib.parse.quote(query)
    mol_url = f"https://www.ebi.ac.uk/chembl/api/data/molecule?pref_name__iexact={encoded}&format=json&limit=1"
    mol_raw = cache_path(raw_dir, "chembl_mol", query, "json")
    status, _, mol_data, _ = fetch_json(mol_url, mol_raw, timeout, resume)
    if status not in {"ok", "cached"} or not isinstance(mol_data, dict):
        return status, "", []
    mols = mol_data.get("molecules", [])
    if not isinstance(mols, list) or not mols:
        return "chembl_not_found", "", []
    chembl_id = clean(mols[0].get("molecule_chembl_id", ""))
    if not chembl_id:
        return "chembl_no_id", "", []

    sections: List[Dict[str, str]] = []
    ind_url = f"https://www.ebi.ac.uk/chembl/api/data/drug_indication?molecule_chembl_id={urllib.parse.quote(chembl_id)}&format=json&limit=20"
    ind_raw = cache_path(raw_dir, "chembl_ind", chembl_id, "json")
    _, _, ind_data, _ = fetch_json(ind_url, ind_raw, timeout, resume)
    indications: List[str] = []
    if isinstance(ind_data, dict):
        for item in ind_data.get("drug_indications", []) or []:
            if not isinstance(item, dict):
                continue
            value = clean(item.get("efo_term") or item.get("mesh_heading") or "")
            if value and value not in indications:
                indications.append(value)
    if indications:
        sections.append(
            {
                "section_kind": "indication",
                "section_title": "Indications (ChEMBL)",
                "section_text": f"Indicated or investigated for: {'; '.join(indications[:15])}.",
            }
        )

    mech_url = f"https://www.ebi.ac.uk/chembl/api/data/mechanism?molecule_chembl_id={urllib.parse.quote(chembl_id)}&format=json&limit=10"
    mech_raw = cache_path(raw_dir, "chembl_mech", chembl_id, "json")
    _, _, mech_data, _ = fetch_json(mech_url, mech_raw, timeout, resume)
    mechanisms: List[str] = []
    if isinstance(mech_data, dict):
        for item in mech_data.get("mechanisms", []) or []:
            if not isinstance(item, dict):
                continue
            mechanism = clean(item.get("mechanism_of_action", ""))
            target = clean(item.get("target_name", ""))
            if mechanism:
                mechanisms.append(f"{mechanism}" + (f" (target: {target})" if target else ""))
    if mechanisms:
        sections.append(
            {
                "section_kind": "pharmacology",
                "section_title": "Mechanism of action (ChEMBL)",
                "section_text": " | ".join(mechanisms[:5]),
            }
        )
    return ("ok" if sections else "chembl_no_sections"), chembl_id, sections


def pubchem_sections(query: str, raw_dir: Path, timeout: int, resume: bool, max_docs: int) -> Tuple[str, str, List[Dict[str, str]]]:
    encoded = urllib.parse.quote(query)
    cid_url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{encoded}/cids/JSON"
    cid_raw = cache_path(raw_dir, "pubchem_cid", query, "json")
    status, _, cid_data, _ = fetch_json(cid_url, cid_raw, timeout, resume)
    if status not in {"ok", "cached"} or not isinstance(cid_data, dict):
        return status, "", []
    cids = cid_data.get("IdentifierList", {}).get("CID", [])
    if not isinstance(cids, list) or not cids:
        return "pubchem_not_found", "", []
    cid = str(cids[0])

    ann_url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug_view/data/compound/{urllib.parse.quote(cid)}/JSON?heading=Drug+and+Medication+Information"
    ann_raw = cache_path(raw_dir, "pubchem_ann", cid, "json")
    ann_status, _, ann_data, _ = fetch_json(ann_url, ann_raw, timeout, resume)
    if ann_status not in {"ok", "cached"} or not isinstance(ann_data, dict):
        return ann_status, cid, []

    sections: List[Dict[str, str]] = []
    seen = set()

    def text_from_information(info_list: Any) -> str:
        parts: List[str] = []
        if not isinstance(info_list, list):
            return ""
        for info in info_list:
            if not isinstance(info, dict):
                continue
            value = info.get("Value", {})
            markup = value.get("StringWithMarkup", []) if isinstance(value, dict) else []
            if not isinstance(markup, list):
                continue
            for item in markup:
                if isinstance(item, dict):
                    text = clean(item.get("String", ""))
                    if len(text) > 20:
                        parts.append(text)
        return clean(" ".join(parts))

    def walk(node: Any, depth: int = 0) -> None:
        if not isinstance(node, dict) or depth > 7:
            return
        heading = clean(node.get("TOCHeading", ""))
        kind = classify_title(heading)
        text = text_from_information(node.get("Information", []))
        if kind and heading and len(text) >= 80:
            key = (kind, heading, text[:300])
            if key not in seen:
                seen.add(key)
                sections.append(
                    {
                        "section_kind": kind,
                        "section_title": f"{heading} (PubChem)",
                        "section_text": text[:4000],
                    }
                )
        for child in node.get("Section", []) or []:
            walk(child, depth + 1)

    walk(ann_data.get("Record", ann_data))
    return ("ok" if sections else "pubchem_no_sections"), cid, sections


def load_queue(missing_path: Path, details_path: Path, bdpm_queue_path: Path) -> List[Dict[str, str]]:
    if not missing_path.exists():
        raise FileNotFoundError(
            f"Missing queue file not found: {missing_path}. Run the missing-evidence detection step first."
        )
    missing = read_csv(missing_path)
    if details_path.exists():
        details_by_id = {row.get("row_id", ""): row for row in read_csv(details_path)}
    else:
        log(f"Optional file not found (skipping): {details_path}")
        details_by_id = {}
    if bdpm_queue_path.exists():
        queue_by_id = {row.get("row_id", ""): row for row in read_csv(bdpm_queue_path)}
    else:
        log(f"Optional file not found (skipping): {bdpm_queue_path}")
        queue_by_id = {}
    rows: List[Dict[str, str]] = []
    for row in missing:
        row_id = row.get("row_id", "")
        merged = dict(row)
        for source in (details_by_id.get(row_id, {}), queue_by_id.get(row_id, {})):
            for key, value in source.items():
                if value and not merged.get(key):
                    merged[key] = value
        rows.append(merged)
    return rows


def query_candidates(row: Dict[str, str]) -> List[str]:
    candidates = [
        row.get("query_generic", ""),
        row.get("nom_generique", ""),
        row.get("query_primary", ""),
        row.get("nom", ""),
        row.get("query_brand", ""),
    ]
    nom = clean(row.get("nom", ""))
    if nom:
        candidates.append(re.split(r"\s+\d|,|\(", nom)[0])
    out: List[str] = []
    for candidate in candidates:
        candidate = clean(candidate)
        if len(candidate) >= 3 and norm(candidate) not in {norm(item) for item in out}:
            out.append(candidate)
    return out


def apply_sections(sections: List[Dict[str, str]], row: Dict[str, str], source_system: str, record_id: str, query: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()
    confidence_by_source = {
        "ema_medicine_finder": "0.70",
        "ema_epi_fhir": "0.70",
        "mhra_products_smpc": "0.66",
        "emc_smpc_html": "0.62",
        "chembl_ebi_api": "0.55",
        "pubchem_annotations": "0.52",
    }
    rank_by_source = {
        "ema_medicine_finder": "72",
        "ema_epi_fhir": "72",
        "mhra_products_smpc": "69",
        "emc_smpc_html": "63",
        "chembl_ebi_api": "56",
        "pubchem_annotations": "53",
    }
    for section in sections:
        section_row = {
            "row_id": row.get("row_id", ""),
            "amm": row.get("amm", ""),
            "nom": row.get("nom", ""),
            "nom_generique": row.get("nom_generique", ""),
            "source_system": source_system,
            "source_file": record_id,
            "source_record_id": record_id,
            "match_query": query,
            "section_kind": section.get("section_kind", ""),
            "section_title": section.get("section_title", ""),
            "section_text": section.get("section_text", ""),
            "language": "en",
            "authority_level": f"fallback_{source_system}",
            "confidence": confidence_by_source.get(source_system, "0.50"),
            "evidence_rank": rank_by_source.get(source_system, "50"),
            "retrieved_at": now,
        }
        section_row["content_hash"] = sha(section_row["row_id"], source_system, section_row["section_kind"], section_row["section_text"][:500])
        out.append(section_row)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--missing", default="dpm_live_out/treated_medicines_missing_all_local_evidence.csv")
    parser.add_argument("--details", default="dpm_live_out/automatic_prescription_missing_details.csv")
    parser.add_argument("--bdpm-queue", default="dpm_live_out/bdpm_live_query_queue.csv")
    parser.add_argument("--raw-dir", default="dpm_live_out/api_cache/eu_uk_live")
    parser.add_argument("--output", default="dpm_live_out/eu_uk_live_fallback_sections.csv")
    parser.add_argument("--query-status-output", default="dpm_live_out/eu_uk_live_fallback_query_status.csv")
    parser.add_argument("--summary", default="dpm_live_out/eu_uk_live_fallback_summary.json")
    parser.add_argument("--sources", default="emc,chembl,pubchem,ema,mhra")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.5)
    parser.add_argument("--timeout", type=int, default=35)
    parser.add_argument("--max-docs", type=int, default=2)
    parser.add_argument("--resume", action="store_true", default=True)
    parser.add_argument("--progress-every", type=int, default=10)
    args = parser.parse_args()

    queue_rows = load_queue(Path(args.missing), Path(args.details), Path(args.bdpm_queue))
    if args.limit > 0:
        queue_rows = queue_rows[: args.limit]
    sources = [source.strip().lower() for source in args.sources.split(",") if source.strip()]
    raw_dir = Path(args.raw_dir)
    section_rows: List[Dict[str, Any]] = []
    status_rows: List[Dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    started = time.time()

    log(f"Starting EU/UK fallback: {len(queue_rows)} rows, sources={sources}, sleep={args.sleep}s")
    for idx, row in enumerate(queue_rows, start=1):
        chosen_source = ""
        chosen_status = ""
        chosen_record = ""
        chosen_query = ""
        chosen_sections: List[Dict[str, str]] = []
        for query in query_candidates(row):
            for source in sources:
                if source == "ema":
                    status, record, sections = ema_sections(query, raw_dir, args.timeout, args.resume, args.max_docs)
                    source_system = "ema_medicine_finder"
                elif source == "mhra":
                    status, record, sections = mhra_sections(query, raw_dir, args.timeout, args.resume, args.max_docs)
                    source_system = "mhra_products_smpc"
                elif source == "emc":
                    status, record, sections = emc_sections(query, raw_dir, args.timeout, args.resume, args.max_docs)
                    source_system = "emc_smpc_html"
                elif source == "chembl":
                    status, record, sections = chembl_sections(query, raw_dir, args.timeout, args.resume, args.max_docs)
                    source_system = "chembl_ebi_api"
                elif source == "pubchem":
                    status, record, sections = pubchem_sections(query, raw_dir, args.timeout, args.resume, args.max_docs)
                    source_system = "pubchem_annotations"
                else:
                    continue
                status_counts[f"{source}_{status.split(',')[0]}"] += 1
                if sections:
                    chosen_source = source_system
                    chosen_status = status
                    chosen_record = record
                    chosen_query = query
                    chosen_sections = sections
                    break
            if chosen_sections:
                break
        if chosen_sections:
            source_counts[chosen_source] += 1
            section_rows.extend(apply_sections(chosen_sections, row, chosen_source, chosen_record, chosen_query))
        status_rows.append(
            {
                "row_id": row.get("row_id", ""),
                "amm": row.get("amm", ""),
                "nom": row.get("nom", ""),
                "nom_generique": row.get("nom_generique", ""),
                "chosen_source": chosen_source,
                "chosen_query": chosen_query,
                "chosen_record": chosen_record,
                "sections": len(chosen_sections),
                "status": chosen_status,
            }
        )
        if idx == 1 or idx % args.progress_every == 0 or idx == len(queue_rows):
            elapsed = time.time() - started
            rate = idx / elapsed if elapsed > 0 else 0
            eta = (len(queue_rows) - idx) / rate if rate > 0 else 0
            log(
                f"{idx}/{len(queue_rows)} | {clean(row.get('nom'))[:50]} | source={chosen_source or 'none'} | "
                f"sections={len(chosen_sections)} | covered_rows={len({r['row_id'] for r in section_rows})} | "
                f"elapsed={elapsed/60:.1f}m | eta={eta/60:.1f}m"
            )
        if args.sleep > 0:
            time.sleep(args.sleep)

    write_csv(Path(args.output), OUTPUT_FIELDS, section_rows)
    write_csv(
        Path(args.query_status_output),
        ["row_id", "amm", "nom", "nom_generique", "chosen_source", "chosen_query", "chosen_record", "sections", "status"],
        status_rows,
    )
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "queue_rows_processed": len(queue_rows),
        "section_rows": len(section_rows),
        "row_ids_with_eu_uk_sections": len({row["row_id"] for row in section_rows}),
        "source_row_counts": dict(source_counts),
        "status_counts": dict(status_counts),
        "outputs": {
            "sections": args.output,
            "query_status": args.query_status_output,
            "raw_dir": args.raw_dir,
            "summary": args.summary,
        },
    }
    Path(args.summary).write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
