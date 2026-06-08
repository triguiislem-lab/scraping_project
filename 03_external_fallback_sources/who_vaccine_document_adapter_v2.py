#!/usr/bin/env python3
"""WHO vaccine document fallback adapter.

This script resolves WHO Prequalification / EUL vaccine document references for
remaining medicines and optionally downloads/parses official document text.

It emits the same row-level section contract used by the other fallback scripts.
When run without --fetch-docs, it emits document_reference rows only if
--emit-reference-sections is set. Those reference rows are useful for review but
should not be counted as parsed clinical sections.

Typical use:
  python -S who_vaccine_document_adapter.py \
    --medicine-summary dpm_live_out/guaranteed_source_medicine_summary.csv \
    --covered-sections dpm_live_out/global_regulatory_fallback_sections_cecmed_v3.csv \
    --covered-sections dpm_live_out/swissmedic_aips_fallback_sections_v2_clean.csv \
    --exclude-covered \
    --fetch-index --fetch-docs \
    --output dpm_live_out/who_vaccine_fallback_sections.csv \
    --query-status-output dpm_live_out/who_vaccine_query_status.csv \
    --summary dpm_live_out/who_vaccine_summary.json
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
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

WHO_DOC_INDEX_PAGES = [
    "https://extranet.who.int/prequal/key-resources/documents/vaccines/all",
    "https://extranet.who.int/prequal/key-resources/documents/vaccines/all?page=1",
    "https://extranet.who.int/prequal/key-resources/documents/vaccines/w",
]

OUTPUT_FIELDS = [
    "row_id", "amm", "nom", "nom_generique", "source_system", "source_file", "source_record_id",
    "match_query", "match_score", "section_kind", "section_title", "section_text", "language",
    "authority_level", "confidence", "evidence_rank", "retrieved_at", "content_hash",
]

STATUS_FIELDS = [
    "row_id", "amm", "nom", "nom_generique", "dosage", "forme", "labo", "pays", "product_type",
    "status", "matched_title", "matched_url", "resolved_document_url", "match_score", "sections",
    "attempted_queries", "notes",
]

REFERENCE_KINDS = {"document_reference", "product_information_reference"}

DOC_TYPE_WEIGHT = {
    "summary_product_characteristics": 0.18,
    "product_characteristics": 0.16,
    "product_information": 0.14,
    "package_insert": 0.12,
    "package_leaflet": 0.10,
    "recommendation": 0.02,
    "report": 0.00,
}

SECTION_RULES = [
    ("indication", r"\b(?:4\.1\s+)?(?:Therapeutic\s+indications|What\s+.*(?:is|are)\s+and\s+what\s+.*(?:used|given)\s+for|What\s+.*is\s+used\s+for)\b"),
    ("dosage", r"\b(?:4\.2\s+)?(?:Posology\s+and\s+method\s+of\s+administration|How\s+.*(?:is|are)\s+given|How\s+.*(?:use|receive)|Dose|Dosage)\b"),
    ("contraindication", r"\b(?:4\.3\s+)?Contraindications\b|\bDo\s+not\s+(?:use|receive|give)\b"),
    ("warning", r"\b(?:4\.4\s+)?(?:Special\s+warnings\s+and\s+precautions|Warnings\s+and\s+precautions|What\s+you\s+need\s+to\s+know\s+before)\b"),
    ("interaction", r"\b(?:4\.5\s+)?(?:Interaction\s+with\s+other\s+medicinal\s+products|Other\s+medicines\s+and|Other\s+vaccines|Interactions)\b"),
    ("special_population", r"\b(?:4\.6\s+)?(?:Fertility,?\s+pregnancy\s+and\s+lactation|Pregnancy\s+and\s+breast-?feeding|Pregnancy|Breast-?feeding|Fertility)\b"),
    ("adverse_effect", r"\b(?:4\.8\s+)?(?:Undesirable\s+effects|Possible\s+side\s+effects|Side\s+effects|Adverse\s+reactions)\b"),
    ("overdose", r"\b(?:4\.9\s+)?Overdose\b"),
    ("pharmacology", r"\b(?:5\.1\s+)?(?:Pharmacodynamic\s+properties|Pharmacological\s+properties)\b"),
    ("pharmacology", r"\b(?:5\.2\s+)?Pharmacokinetic\s+properties\b"),
    ("storage", r"\b(?:6\.4\s+)?(?:Special\s+precautions\s+for\s+storage|How\s+to\s+store|Storage)\b"),
    ("composition", r"\b(?:2\.?\s+)?(?:Qualitative\s+and\s+quantitative\s+composition|What\s+.*contains|Composition)\b"),
]

PRODUCT_SYNONYMS = {
    "COMINARTY": ["COMIRNATY", "BIONTECH", "PFIZER", "TOZINAMERAN", "COVID 19 MRNA"],
    "COMIRNATY": ["COMINARTY", "BIONTECH", "PFIZER", "TOZINAMERAN", "COVID 19 MRNA"],
    "COVID 19 VACCINE MODERNA": ["MODERNA", "MRNA", "NUCLEOSIDE MODIFIED"],
    "COVID 19 VACCINE ASTRAZENECA": ["ASTRAZENECA", "CHADOX1", "AZD1222", "COVISHIELD"],
    "CORONAVAC": ["CORONAVAC", "SINOVAC"],
    "GAM COVID VAC": ["GAM COVID VAC", "GAMALEYA", "SPUTNIK"],
    "SPUTNIK LIGHT": ["SPUTNIK", "GAMALEYA", "ADENOVIRUS"],
    "GC FLU": ["GCFLU", "GC FLU", "GREEN CROSS", "INFLUENZA"],
    "GCFLU QUADRIVALENT": ["GCFLU", "GC FLU", "GREEN CROSS", "INFLUENZA", "QUADRIVALENT"],
    "HEPAVAX GENE": ["HEPAVAX", "HEPATITIS B", "HEP B"],
    "EUVAX B": ["EUVAX", "HEPATITIS B", "LG"],
    "ORAL POLIO VACCINE": ["ORAL POLIO", "POLIOMYELITIS", "BIO FARMA", "BIOFARMA"],
    "NOVEL ORAL POLIOMYELITIS VACCINE TYPE 2 NVPO2": ["NOPV2", "NOVEL ORAL POLIO", "BIOFARMA", "BIO FARMA"],
    "PENTAXIM": ["PENTAXIM", "SANOFI", "DTP", "HIB", "POLIO"],
    "VAXIGRIPTETRA": ["VAXIGRIP", "SANOFI", "INFLUENZA", "QUADRIVALENT"],
    "VACCIN DTP DTC ADSORBE": ["DTP", "DIPHTHERIA", "TETANUS", "PERTUSSIS", "SERUM INSTITUTE"],
    "VACCIN DU VIRUS VIVANT DE LA ROUGEOLE ET DE LA RUBEOLE USP": ["MEASLES", "RUBELLA", "SERUM INSTITUTE"],
}


def clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = " ".join(clean(v) for v in value)
    text = html.unescape(str(value))
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def norm(value: Any) -> str:
    text = unicodedata.normalize("NFKD", clean(value).upper())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("µ", "U").replace("μ", "U")
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def sha(*parts: Any) -> str:
    return hashlib.sha1("|".join(clean(p) for p in parts).encode("utf-8", "ignore")).hexdigest()


def read_csv(path: str | Path) -> List[Dict[str, str]]:
    p = Path(path)
    if not path or not p.exists():
        return []
    with p.open("r", encoding="utf-8-sig", newline="") as handle:
        return [{clean(k): clean(v) for k, v in row.items()} for row in csv.DictReader(handle)]


def write_csv(path: str | Path, fields: Sequence[str], rows: Iterable[Dict[str, Any]]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({f: clean(row.get(f, "")) for f in fields})


def row_key(row: Dict[str, str]) -> str:
    return clean(row.get("row_id") or row.get("id") or "")


def load_covered(paths: Sequence[str]) -> set[str]:
    out: set[str] = set()
    for raw in paths:
        for part in str(raw).split(","):
            part = part.strip()
            if not part:
                continue
            for row in read_csv(part):
                rid = row_key(row)
                kind = clean(row.get("section_kind"))
                source = clean(row.get("source_system"))
                # Count only parsed clinical/reference recovery rows, not empty status rows.
                if rid and clean(row.get("section_text")) and kind not in REFERENCE_KINDS:
                    out.add(rid)
    return out


def is_vaccine_row(row: Dict[str, str]) -> bool:
    text = norm(" ".join([row.get("product_type", ""), row.get("nom", ""), row.get("nom_generique", ""), row.get("top_source_name", "")]))
    return any(token in text for token in ["VACCINE", "VACCIN", "COVID", "COMIRNATY", "COMINARTY", "CORONAVAC", "POLIO", "POLIOMYELIT", "FLU", "GRIPPE", "HEPATITIS", "HEPATITE", "BCG"])


def load_rows(remaining: str, medicine_summary: str, examples: str, covered_paths: Sequence[str], exclude_covered: bool) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    rows = read_csv(medicine_summary) or read_csv(remaining)
    stats = {"input_rows": len(rows), "after_examples": 0, "covered_row_ids": 0, "after_exclude_covered": 0, "after_vaccine_filter": 0}
    if examples:
        wants = [norm(x) for x in examples.split(",") if norm(x)]
        rows = [r for r in rows if any(w in norm(r.get("nom")) or norm(r.get("nom")) in w for w in wants)]
    stats["after_examples"] = len(rows)
    covered = load_covered(covered_paths)
    stats["covered_row_ids"] = len(covered)
    if exclude_covered:
        rows = [r for r in rows if row_key(r) not in covered]
    stats["after_exclude_covered"] = len(rows)
    rows = [r for r in rows if is_vaccine_row(r)]
    stats["after_vaccine_filter"] = len(rows)
    return rows, stats


@dataclass
class WhoDoc:
    title: str
    url: str
    doc_type: str = ""


def classify_doc_type(title: str) -> str:
    t = norm(title)
    if "SUMMARY OF PRODUCT CHARACTERISTICS" in t or "SMPC" in t:
        return "summary_product_characteristics"
    if "PRODUCT CHARACTERISTICS" in t:
        return "product_characteristics"
    if "PRODUCT INFORMATION" in t:
        return "product_information"
    if "PACKAGE INSERT" in t or "INSERT" in t:
        return "package_insert"
    if "PACKAGE LEAFLET" in t or "LEAFLET" in t or "PIL" in t:
        return "package_leaflet"
    if "RECOMMENDATION" in t or "EUL" in t:
        return "recommendation"
    if "REPORT" in t or "TAG" in t:
        return "report"
    return "other"


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def cached_index_base_url(path: Path) -> str:
    for url in WHO_DOC_INDEX_PAGES:
        if path.name == "who_index_" + sha(url)[:12] + ".html":
            return url
    return path.resolve().as_uri()


def fetch_url(url: str, timeout: int = 30) -> Tuple[str, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "tunisia-cdss-who-vaccine-adapter/1.0", "Accept": "text/html,application/pdf,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        final = resp.geturl()
        return final, resp.read()


def parse_links_from_html(html_text: str, base_url: str) -> List[WhoDoc]:
    docs: List[WhoDoc] = []
    # Basic anchor extraction; WHO pages are simple enough for this.
    for m in re.finditer(r"(?is)<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html_text):
        href = html.unescape(m.group(1))
        title = clean(re.sub(r"<[^>]+>", " ", m.group(2)))
        if not title or len(title) < 4:
            continue
        if not any(tok in norm(title) for tok in ["VACCINE", "VACCIN", "COVID", "COMIRNATY", "TOZINAMERAN", "CORONAVAC", "NOPV2", "POLIO", "INFLUENZA", "PACKAGE", "PRODUCT", "PIL", "SMPC"]):
            continue
        url = urllib.parse.urljoin(base_url, href)
        docs.append(WhoDoc(title=title, url=url, doc_type=classify_doc_type(title)))
    # de-dupe
    seen = set()
    unique: List[WhoDoc] = []
    for doc in docs:
        key = (norm(doc.title), doc.url)
        if key not in seen:
            seen.add(key)
            unique.append(doc)
    return unique


def load_who_index(index_inputs: Sequence[str], fetch_index: bool, raw_dir: Path, timeout: int) -> Tuple[List[WhoDoc], Dict[str, Any]]:
    docs: List[WhoDoc] = []
    stats: Dict[str, Any] = {"index_inputs": [], "fetched_index_pages": 0, "index_errors": []}
    inputs = list(index_inputs)
    if not inputs:
        for cache_root in (raw_dir, Path("who_vaccine_cache")):
            if cache_root.exists():
                inputs.extend(str(path) for path in sorted(cache_root.glob("who_index_*.html")))
    if fetch_index:
        inputs.extend(WHO_DOC_INDEX_PAGES)
    for source in inputs:
        if not source:
            continue
        stats["index_inputs"].append(source)
        try:
            if source.startswith("http://") or source.startswith("https://"):
                cache = raw_dir / ("who_index_" + sha(source)[:12] + ".html")
                if cache.exists() and cache.stat().st_size > 0:
                    text = read_text_file(cache)
                elif fetch_index:
                    final, body = fetch_url(source, timeout)
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_bytes(body)
                    text = body.decode("utf-8", "ignore")
                    stats["fetched_index_pages"] += 1
                else:
                    stats["index_errors"].append(f"missing_cache:{source}")
                    continue
                docs.extend(parse_links_from_html(text, source))
            else:
                p = Path(source)
                if p.is_dir():
                    for child in sorted(p.glob("*.html")):
                        docs.extend(parse_links_from_html(read_text_file(child), cached_index_base_url(child)))
                elif p.exists():
                    docs.extend(parse_links_from_html(read_text_file(p), cached_index_base_url(p)))
                else:
                    stats["index_errors"].append(f"missing:{source}")
        except Exception as exc:
            stats["index_errors"].append(f"{source}:{type(exc).__name__}:{clean(exc)}")
    seen = set()
    unique: List[WhoDoc] = []
    for doc in docs:
        key = (norm(doc.title), doc.url)
        if key not in seen:
            seen.add(key)
            unique.append(doc)
    return unique, stats


def base_product_name(row: Dict[str, str]) -> str:
    name = norm(row.get("nom"))
    # remove common dosage/form bits
    name = re.sub(r"\b\d+(?:MG|MCG|UG|ML|UI|U|DOSE|DOSES)\b", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def query_terms_for_row(row: Dict[str, str]) -> List[str]:
    terms: List[str] = []
    for field in ("nom", "nom_generique", "labo"):
        val = norm(row.get(field))
        if val:
            terms.append(val)
    base = base_product_name(row)
    if base:
        terms.append(base)
    for key, syns in PRODUCT_SYNONYMS.items():
        if key in base or base in key or key in norm(row.get("nom")):
            terms.extend(syns)
    # generic derived terms
    text = norm(" ".join([row.get("nom"), row.get("nom_generique"), row.get("labo")]))
    if "INFLUENZA" in text or "ANTIGRIPPE" in text or "GRIPPE" in text:
        terms.append("INFLUENZA")
    if "HEPATITE B" in text or "HEPATITIS B" in text:
        terms.append("HEPATITIS B")
    if "COVID" in text:
        terms.append("COVID 19")
    if "POLIO" in text or "POLIOMYEL" in text:
        terms.extend(["POLIO", "POLIOMYELITIS"])
    # clean and de-dupe, avoid ultra generic alone
    out: List[str] = []
    seen = set()
    for t in terms:
        t = norm(t)
        if len(t) < 3:
            continue
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def token_set(text: str) -> set[str]:
    stop = {"VACCINE", "VACCIN", "COVID", "PRODUCT", "INFORMATION", "PACKAGE", "LEAFLET", "INSERT", "WHO", "SUMMARY", "CHARACTERISTICS", "SUSPENSION", "INJECTION", "SOLUTION", "FOR", "THE", "AND", "OF", "DE", "LA", "LE", "A", "B"}
    return {t for t in norm(text).split() if len(t) >= 3 and t not in stop}


def score_match(row: Dict[str, str], doc: WhoDoc) -> Tuple[float, str]:
    title_norm = norm(doc.title)
    name = norm(row.get("nom"))
    generic = norm(row.get("nom_generique"))
    labo = norm(row.get("labo"))
    base = base_product_name(row)
    terms = query_terms_for_row(row)
    reasons: List[str] = []
    score = 0.0

    # strong product/synonym matching
    if name and name in title_norm:
        score += 0.60; reasons.append("name_in_title")
    elif base and base in title_norm:
        score += 0.55; reasons.append("base_name_in_title")

    for t in terms:
        if len(t) >= 5 and t in title_norm:
            # product names / specific synonyms get useful boost.
            if t in {"COVID 19", "INFLUENZA", "POLIO", "POLIOMYELITIS", "HEPATITIS B"}:
                score += 0.08; reasons.append(f"generic:{t}")
            else:
                score += 0.20; reasons.append(f"term:{t}")

    # token overlap for noisy titles.
    product_tokens = token_set(" ".join([name, generic, labo] + terms))
    title_tokens = token_set(title_norm)
    inter = product_tokens & title_tokens
    if inter:
        overlap = len(inter) / max(1, min(len(product_tokens), 8))
        score += min(0.25, overlap * 0.25)
        reasons.append("tokens:" + "/".join(sorted(list(inter))[:6]))

    # manufacturer boosts.
    if any(tok in title_norm for tok in ["BIONTECH", "PFIZER"]) and any(tok in name + " " + labo for tok in ["BIONTECH", "PFIZER"]):
        score += 0.20; reasons.append("manufacturer_biontech")
    if "MODERNA" in title_norm and "MODERNA" in name + " " + labo:
        score += 0.22; reasons.append("manufacturer_moderna")
    if "ASTRAZENECA" in title_norm and "ASTRAZENECA" in name + " " + labo:
        score += 0.22; reasons.append("manufacturer_astrazeneca")
    if "SINOVAC" in title_norm and "SINOVAC" in labo + " " + name:
        score += 0.22; reasons.append("manufacturer_sinovac")
    if "BIOFARMA" in title_norm.replace(" ", "") and ("BIO FARMA" in labo or "BIOFARMA" in labo.replace(" ", "")):
        score += 0.22; reasons.append("manufacturer_biofarma")
    if "SANOFI" in title_norm and "SANOFI" in labo + " " + name:
        score += 0.18; reasons.append("manufacturer_sanofi")

    # doc type priority.
    dtype = doc.doc_type or classify_doc_type(doc.title)
    score += DOC_TYPE_WEIGHT.get(dtype, 0.0)
    if dtype:
        reasons.append("doc_type:" + dtype)

    # Guardrails: avoid very generic cross-vaccine matches.
    if score < 0.50:
        # allow exact COVID manufacturer docs, otherwise reject.
        pass
    if base and len(base.split()) == 1 and base not in title_norm:
        # one-token brand should not match only generic vaccine words.
        if not any(s in title_norm for s in PRODUCT_SYNONYMS.get(base, [])):
            score -= 0.15; reasons.append("single_token_guard")
    if "PENTAXIM" in name and "HEXAXIM" in title_norm:
        score -= 0.40; reasons.append("reject_hexaxim_for_pentaxim")
    if "SPUTNIK" in name or "GAM COVID VAC" in name:
        if not any(tok in title_norm for tok in ["SPUTNIK", "GAMALEYA", "GAM COVID"]):
            score -= 0.35; reasons.append("sputnik_guard")
    if "CORONAVAC" in name and not any(tok in title_norm for tok in ["CORONAVAC", "SINOVAC"]):
        score -= 0.35; reasons.append("coronavac_guard")

    return max(0.0, min(score, 0.98)), ";".join(reasons)


def choose_best_doc(row: Dict[str, str], docs: Sequence[WhoDoc], min_score: float) -> Tuple[Optional[WhoDoc], float, str]:
    scored = []
    for doc in docs:
        score, reasons = score_match(row, doc)
        if score >= min_score:
            scored.append((score, doc, reasons))
    if not scored:
        return None, 0.0, ""
    scored.sort(key=lambda x: (x[0], DOC_TYPE_WEIGHT.get(x[1].doc_type, 0.0)), reverse=True)
    return scored[0][1], scored[0][0], scored[0][2]


def strip_html(text: str) -> str:
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</tr>|</h[1-6]>|</div>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return clean(text)


def resolve_document_url(doc: WhoDoc, raw_dir: Path, timeout: int) -> Tuple[str, str, bytes, str]:
    """Return (status, final_url, body, content_type_hint)."""
    url = doc.url
    if url.startswith("file://"):
        p = Path(urllib.parse.urlparse(url).path)
        if not p.exists():
            return "file_missing", url, b"", ""
        return "ok", url, p.read_bytes(), p.suffix.lower().lstrip(".")
    cache = raw_dir / ("who_doc_" + sha(url)[:12])
    try:
        final, body = fetch_url(url, timeout)
        # if a WHO node page, find a pdf link.
        if not final.lower().endswith(".pdf") and b"<html" in body[:500].lower():
            text = body.decode("utf-8", "ignore")
            pdfs = re.findall(r"(?is)href=[\"']([^\"']+\.pdf(?:\?[^\"']*)?)[\"']", text)
            if pdfs:
                pdf_url = urllib.parse.urljoin(final, html.unescape(pdfs[0]))
                final, body = fetch_url(pdf_url, timeout)
        return "ok", final, body, "pdf" if final.lower().split("?")[0].endswith(".pdf") else "html"
    except Exception as exc:
        return f"fetch_error:{type(exc).__name__}:{clean(exc)}", url, b"", ""


def text_looks_readable(text: str) -> bool:
    """Reject raw/compressed PDF byte dumps masquerading as text."""
    if not text or len(text.strip()) < 80:
        return False
    sample = text[:20000]
    raw_markers = sum(sample.count(marker) for marker in ("%PDF", " obj", "endobj", "stream", "/Type", "/Font", "/Length", "/Filter"))
    letters = sum(1 for ch in sample if ch.isalpha())
    spaces = sum(1 for ch in sample if ch.isspace())
    # A real pdftotext/pypdf extraction has natural language, whitespace, and few PDF structural markers.
    if raw_markers >= 6 and spaces / max(len(sample), 1) < 0.08:
        return False
    if letters / max(len(sample), 1) < 0.18:
        return False
    return True


def pdf_bytes_to_text_with_status(body: bytes) -> Tuple[str, str]:
    """Extract PDF text using available tools. Returns (text, status)."""
    pdftotext = shutil.which("pdftotext")
    if pdftotext:
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "in.pdf"
            out = Path(td) / "out.txt"
            inp.write_bytes(body)
            try:
                subprocess.run(
                    [pdftotext, "-layout", "-enc", "UTF-8", str(inp), str(out)],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=90,
                )
                if out.exists():
                    text = out.read_text(encoding="utf-8", errors="ignore")
                    if text_looks_readable(text):
                        return text, "pdftotext_ok"
                    return text, "pdftotext_unreadable"
            except Exception as exc:
                return "", f"pdftotext_error:{type(exc).__name__}:{clean(exc)}"

    # Optional Python-library fallbacks. These will not work under `python -S`
    # unless the libraries are on sys.path, but they make the script portable.
    try:
        import fitz  # type: ignore
        doc = fitz.open(stream=body, filetype="pdf")
        text = "\n".join(page.get_text("text") for page in doc)
        if text_looks_readable(text):
            return text, "pymupdf_ok"
    except Exception:
        pass

    try:
        import pypdf  # type: ignore
        reader = pypdf.PdfReader(io.BytesIO(body))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if text_looks_readable(text):
            return text, "pypdf_ok"
    except Exception:
        pass

    # Do not return latin-1 decoded raw PDF bytes. That caused false "text_len"
    # values and no sections. Surface the missing extractor clearly instead.
    if not pdftotext:
        return "", "pdf_text_extractor_missing_install_poppler_utils_or_run_without_python_S_with_pypdf"
    return "", "pdf_text_extraction_failed"


def document_body_to_text_with_status(body: bytes, hint: str) -> Tuple[str, str]:
    if hint == "pdf" or body[:4] == b"%PDF":
        return pdf_bytes_to_text_with_status(body)
    text = body.decode("utf-8", "ignore")
    if "<html" in text[:1000].lower() or "<body" in text[:1000].lower():
        stripped = strip_html(text)
        return stripped, "html_ok" if text_looks_readable(stripped) else "html_unreadable"
    cleaned = clean(text)
    return cleaned, "text_ok" if text_looks_readable(cleaned) else "text_unreadable"


# Backward-compatible wrapper used by older tests/imports.
def pdf_bytes_to_text(body: bytes) -> str:
    return pdf_bytes_to_text_with_status(body)[0]


def document_body_to_text(body: bytes, hint: str) -> str:
    return document_body_to_text_with_status(body, hint)[0]


def section_title_from_start(text: str, start: int) -> str:
    snippet = clean(text[start:start+220])
    line = re.split(r"\n|(?<=\.)\s+", snippet, maxsplit=1)[0]
    return clean(line[:180]) or clean(snippet[:180])


def extract_sections(text: str) -> List[Dict[str, str]]:
    # Preserve some line breaks for pdftotext, but normalize spaces within sections.
    t = re.sub(r"\r", "\n", text)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    flat = clean(t)
    matches: List[Tuple[int, str, str]] = []
    for kind, pattern in SECTION_RULES:
        for m in re.finditer(pattern, flat, flags=re.I):
            matches.append((m.start(), kind, section_title_from_start(flat, m.start()) or m.group(0)))
    # Numeric fallback for product-info docs.
    for m in re.finditer(r"\b(?:4\.[12345689]|5\.[12]|6\.4|[1-6]\.)\s+[A-Z][A-Za-z ,()\-/]{4,100}", flat):
        title = section_title_from_start(flat, m.start())
        title_norm = norm(title)
        kind = ""
        if "INDICATION" in title_norm or "USED FOR" in title_norm: kind = "indication"
        elif "POSOLOGY" in title_norm or "METHOD OF ADMINISTRATION" in title_norm or "DOS" in title_norm: kind = "dosage"
        elif "CONTRAINDICATION" in title_norm: kind = "contraindication"
        elif "WARNING" in title_norm or "PRECAUTION" in title_norm: kind = "warning"
        elif "INTERACTION" in title_norm: kind = "interaction"
        elif "PREGNANCY" in title_norm or "LACTATION" in title_norm or "FERTILITY" in title_norm: kind = "special_population"
        elif "UNDESIRABLE" in title_norm or "ADVERSE" in title_norm or "SIDE EFFECT" in title_norm: kind = "adverse_effect"
        elif "OVERDOSE" in title_norm: kind = "overdose"
        elif "PHARMACODYNAMIC" in title_norm or "PHARMACOKINETIC" in title_norm: kind = "pharmacology"
        elif "STORAGE" in title_norm: kind = "storage"
        elif "COMPOSITION" in title_norm: kind = "composition"
        if kind:
            matches.append((m.start(), kind, title))
    # de-dupe close positions
    dedup: List[Tuple[int, str, str]] = []
    for pos, kind, title in sorted(matches, key=lambda x: x[0]):
        if any(abs(pos - p) < 30 for p, _, _ in dedup):
            continue
        dedup.append((pos, kind, title))
    rows: List[Dict[str, str]] = []
    seen = set()
    for i, (start, kind, title) in enumerate(dedup):
        end = dedup[i+1][0] if i+1 < len(dedup) else len(flat)
        sec = clean(flat[start:min(end, start+10000)])
        if len(sec) < 80:
            continue
        key = (kind, sec[:400])
        if key in seen:
            continue
        seen.add(key)
        rows.append({"section_kind": kind, "section_title": title, "section_text": sec})
    return rows


def apply_sections(sections: List[Dict[str, str]], row: Dict[str, str], doc: WhoDoc, final_url: str, match_score: float, reference_only: bool = False) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    source_system = "who_vaccine_document_reference" if reference_only else "who_vaccine_product_information"
    confidence = "0.62" if reference_only else f"{max(0.66, min(match_score, 0.90)):.2f}"
    rank = "61" if reference_only else "68"
    out = []
    for sec in sections:
        section_text = sec.get("section_text", "")
        r = {
            "row_id": row.get("row_id", ""),
            "amm": row.get("amm", ""),
            "nom": row.get("nom", ""),
            "nom_generique": row.get("nom_generique", ""),
            "source_system": source_system,
            "source_file": final_url or doc.url,
            "source_record_id": sha(doc.title, doc.url)[:16],
            "match_query": "; ".join(query_terms_for_row(row)[:8]),
            "match_score": f"{match_score:.2f}",
            "section_kind": sec.get("section_kind", ""),
            "section_title": sec.get("section_title", ""),
            "section_text": section_text,
            "language": "en",
            "authority_level": "fallback_who_global_vaccine_product_info",
            "confidence": confidence,
            "evidence_rank": rank,
            "retrieved_at": now,
        }
        r["content_hash"] = sha(r["row_id"], r["source_system"], r["section_kind"], r["section_text"][:500])
        out.append(r)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remaining", default="dpm_live_out/remaining_382_medicines_all_available_details.csv")
    parser.add_argument("--medicine-summary", default="dpm_live_out/guaranteed_source_medicine_summary.csv")
    parser.add_argument("--covered-sections", action="append", default=[], help="CSV of already recovered clinical sections. Can be repeated or comma-separated.")
    parser.add_argument("--exclude-covered", action="store_true")
    parser.add_argument("--examples", default="", help="Comma-separated product names for quick tests.")
    parser.add_argument("--who-index-input", action="append", default=[], help="WHO documents index HTML file/dir/URL. Can be repeated.")
    parser.add_argument("--fetch-index", action="store_true", help="Fetch WHO index pages from extranet.who.int/prequal.")
    parser.add_argument("--fetch-docs", action="store_true", help="Download matched WHO PDFs/HTML and extract sections.")
    parser.add_argument("--emit-reference-sections", action="store_true", help="Emit document_reference rows without fetching/parsing docs.")
    parser.add_argument("--emit-reference-on-no-sections", action="store_true", help="When --fetch-docs matches a document but section extraction fails, emit a document_reference row for manual review.")
    parser.add_argument("--raw-dir", default="dpm_live_out/api_cache/who_vaccine")
    parser.add_argument("--output", default="dpm_live_out/who_vaccine_fallback_sections.csv")
    parser.add_argument("--query-status-output", default="dpm_live_out/who_vaccine_query_status.csv")
    parser.add_argument("--summary", default="dpm_live_out/who_vaccine_summary.json")
    parser.add_argument("--min-match-score", type=float, default=0.55)
    parser.add_argument("--timeout", type=int, default=35)
    args = parser.parse_args()

    started = time.time()
    raw_dir = Path(args.raw_dir)
    rows, filter_stats = load_rows(args.remaining, args.medicine_summary, args.examples, args.covered_sections, args.exclude_covered)
    docs, index_stats = load_who_index(args.who_index_input, args.fetch_index, raw_dir, args.timeout)

    section_rows: List[Dict[str, Any]] = []
    status_rows: List[Dict[str, Any]] = []
    status_counts: Dict[str, int] = {}
    source_counts: Dict[str, int] = {}

    for row in rows:
        doc, score, reasons = choose_best_doc(row, docs, args.min_match_score)
        sections: List[Dict[str, str]] = []
        status = "no_who_document_match"
        final_url = ""
        note = reasons
        if doc is None:
            status = "no_who_document_match"
        elif args.fetch_docs:
            st, final_url, body, hint = resolve_document_url(doc, raw_dir, args.timeout)
            if st != "ok":
                status = st
            else:
                text, text_status = document_body_to_text_with_status(body, hint)
                sections = extract_sections(text) if text else []
                if sections:
                    status = "ok_document_sections"
                    applied = apply_sections(sections, row, doc, final_url, score, reference_only=False)
                    section_rows.extend(applied)
                    source_counts["who_vaccine_product_information"] = source_counts.get("who_vaccine_product_information", 0) + 1
                else:
                    status = "matched_doc_no_sections"
                    note += f";text_status={text_status};text_len={len(text)};hint={hint}"
                    if args.emit_reference_on_no_sections:
                        ref_sections = [{"section_kind": "document_reference", "section_title": doc.title, "section_text": f"WHO vaccine document reference after failed extraction: {doc.title}. URL: {final_url or doc.url}. Text status: {text_status}."}]
                        section_rows.extend(apply_sections(ref_sections, row, doc, final_url or doc.url, score, reference_only=True))
                        source_counts["who_vaccine_document_reference"] = source_counts.get("who_vaccine_document_reference", 0) + 1
                        sections = ref_sections
                        status = "matched_doc_reference_emitted_no_sections"
        elif args.emit_reference_sections:
            final_url = doc.url
            sections = [{"section_kind": "document_reference", "section_title": doc.title, "section_text": f"WHO vaccine document reference: {doc.title}. URL: {doc.url}"}]
            status = "matched_reference"
            section_rows.extend(apply_sections(sections, row, doc, final_url, score, reference_only=True))
            source_counts["who_vaccine_document_reference"] = source_counts.get("who_vaccine_document_reference", 0) + 1
        else:
            status = "matched_reference_not_emitted"
            final_url = doc.url if doc else ""

        status_counts[status.split(":")[0]] = status_counts.get(status.split(":")[0], 0) + 1
        status_rows.append({
            "row_id": row.get("row_id", ""), "amm": row.get("amm", ""), "nom": row.get("nom", ""), "nom_generique": row.get("nom_generique", ""),
            "dosage": row.get("dosage", ""), "forme": row.get("forme", ""), "labo": row.get("labo", ""), "pays": row.get("pays", ""), "product_type": row.get("product_type", ""),
            "status": status, "matched_title": doc.title if doc else "", "matched_url": doc.url if doc else "", "resolved_document_url": final_url,
            "match_score": f"{score:.2f}" if doc else "", "sections": str(len(sections)) if sections else "0",
            "attempted_queries": "; ".join(query_terms_for_row(row)[:12]), "notes": note,
        })

    write_csv(args.output, OUTPUT_FIELDS, section_rows)
    write_csv(args.query_status_output, STATUS_FIELDS, status_rows)
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "filter_stats": filter_stats,
        "vaccine_rows_processed": len(rows),
        "who_documents_loaded": len(docs),
        "section_rows": len(section_rows),
        "row_ids_with_who_sections": len({r["row_id"] for r in section_rows if r.get("section_kind") != "document_reference"}),
        "row_ids_with_reference_sections": len({r["row_id"] for r in section_rows if r.get("section_kind") == "document_reference"}),
        "status_counts": status_counts,
        "source_row_counts": source_counts,
        "index_stats": index_stats,
        "elapsed_seconds": round(time.time() - started, 2),
        "outputs": {"sections": args.output, "query_status": args.query_status_output, "summary": args.summary},
        "notes": [
            "WHO vaccine documents are global product information/EUL/PQ fallback evidence, not exact Tunisian RCP unless the product/MAH/presentation match.",
            "document_reference rows are for review/routing and should not be counted as parsed clinical sections.",
            "Use --fetch-docs with pdftotext installed to extract PDF clinical sections.",
            "If PDF extraction returns 0 sections, install poppler-utils/pdftotext or run without -S after installing pypdf/PyMuPDF.",
            "Use --emit-reference-on-no-sections to keep matched WHO document URLs when PDF parsing fails.",
        ],
    }
    Path(args.summary).write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
