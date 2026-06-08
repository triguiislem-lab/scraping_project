#!/usr/bin/env python3
"""
Fetch multinational regulator fallback sections for medicines still missing evidence.

Registry sources wired here:
- Spain: AEMPS CIMA REST + ficha tecnica / prospecto HTML.
- UK/EU: emc, MHRA Products, EMA finder (delegated to fetch_eu_uk_live_fallback.py).
- Italy: AIFA medicinali search + approved RCP/FI PDFs.
- Turkey: TITCK KUB/KT list + approved PDFs.
- Canada: Health Canada DPD API metadata.
- Cuba: CECMED RCP PDFs, with Spanish section extraction.
- Jordan JFDA, Saudi SFDA SDI, UAE EDE/MOHAP, Korea MFDS/NEDRUG, Portugal
  Infarmed, Sweden FASS, WHO vaccine sources, Switzerland, Germany, Belgium,
  Austria, Denmark, France/eCodex, EMA national registers, and ANSM interactions
  are registered as audited sources; sources without stable public adapters emit
  status rows until a machine-readable adapter is added.

The output follows the same row-level section contract as the BDPM, US, and EU/UK
fallback scripts so the coverage summarizer can merge it directly.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import http.cookiejar
import io
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import fetch_eu_uk_live_fallback as euuk
except Exception:  # pragma: no cover - optional local helper during standalone use.
    euuk = None


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

CIMA_BASE = "https://cima.aemps.es/cima"
CIMA_REST = f"{CIMA_BASE}/rest"
AIFA_BASE = "https://api.aifa.gov.it/aifa-bdf-eif-be/1.0.0"
HEALTH_CANADA_DPD = "https://health-products.canada.ca/api/drug"
TITCK_BASE = "https://www.titck.gov.tr"
TITCK_KUBKT = f"{TITCK_BASE}/kubkt"
TITCK_DATATABLE = f"{TITCK_BASE}/getkubktviewdatatable"
JFDA_LEAFLET = "https://services.jfda.jo/JFDA/registration/leafletdrugssearch.aspx"
JFDA_FREE_DRUG_SEARCH = "https://drugapplication.jfda.jo/account/doPage/freeDrugSearch"
SFDA_SDI = "https://sdi.sfda.gov.sa/"
SFDA_SERVICE = "https://www.sfda.gov.sa/en/eservices/68699"
UAE_EDE_DIRECTORY = "https://www.ede.gov.ae/en/drug-directory"
UAE_MOHAP_DIRECTORY = "https://mohap.gov.ae/en/registered-medical-product-directory"
MFDS_NEDRUG = "https://nedrug.mfds.go.kr/eng/index"
CECMED_RCP = "https://www.cecmed.cu/registro/rcp/medicamentos"
CECMED_BIOLOGICS_RCP = "https://www.cecmed.cu/registro/rcp/biologicos"
CECMED_SITE_SEARCH = "https://www.cecmed.cu/search/node"
INFARMED_INFOMED = "https://www.infarmed.pt/web/infarmed/servicos-on-line/pesquisa-do-medicamento"
FASS_API_INFO = "https://www.fass.se/health/portal-document/fass-api"
WHO_PREQUAL_VACCINES = "https://extranet.who.int/prequal/vaccines/list-prequalified-vaccines"
WHO_FULL_VACCINE_PRODUCT_LIST = "https://www.who.int/publications/m/item/full-vaccine-product-list"

DEFAULT_SOURCES = (
    "aifa,titck,canada,cima,emc,mhra,ema,cecmed,jfda,sfda,uae,mfds,"
    "infarmed,fass,who_vaccines,swissmedic,pharmnet,famhp,basg,denmark,"
    "france,ema_registers,ansm_interactions"
)

REGISTERED_SOURCE_INFO = {
    "cima": {
        "name": "Spain AEMPS CIMA",
        "status": "machine_adapter",
        "url": "https://cima.aemps.es/cima/rest/medicamentos",
    },
    "emc": {
        "name": "UK emc / Datapharm",
        "status": "machine_adapter",
        "url": "https://apim.medicines.org.uk/public_api/v1/documents",
    },
    "mhra": {
        "name": "UK MHRA Products",
        "status": "machine_adapter",
        "url": "https://products.mhra.gov.uk/",
    },
    "ema": {
        "name": "EMA medicine search",
        "status": "machine_adapter",
        "url": "https://www.ema.europa.eu/en/medicines",
    },
    "aifa": {
        "name": "Italy AIFA Medicinali",
        "status": "machine_adapter",
        "url": "https://medicinali.aifa.gov.it/",
    },
    "titck": {
        "name": "Turkey TITCK KUB/KT",
        "status": "machine_adapter",
        "url": "https://www.titck.gov.tr/kubkt",
    },
    "canada": {
        "name": "Health Canada DPD",
        "status": "machine_adapter_metadata",
        "url": "https://health-products.canada.ca/api/drug/",
    },
    "swissmedic": {
        "name": "Swissmedic AIPS XML",
        "status": "registered_bulk_xml_download_required",
        "url": "https://download.swissmedicinfo.ch/",
    },
    "pharmnet": {
        "name": "Germany PharmNet.Bund / BfArM",
        "status": "registered_dynamic_portal",
        "url": "https://www.pharmnet-bund.de/",
    },
    "famhp": {
        "name": "Belgium FAMHP medicines database",
        "status": "registered_dynamic_api_auth_required",
        "url": "https://medicinesdatabase.be/",
    },
    "basg": {
        "name": "Austria BASG Arzneispezialitaetenregister",
        "status": "registered_dynamic_api",
        "url": "https://medikamente.basg.gv.at/",
    },
    "denmark": {
        "name": "Denmark produktresume / indlaegsseddel",
        "status": "registered_public_portal",
        "url": "https://produktresume.dk/",
    },
    "france": {
        "name": "France BDPM/eCodex/old CIS_RCP",
        "status": "registered_existing_bdpm_and_ecodex",
        "url": "https://base-donnees-publique.medicaments.gouv.fr/",
    },
    "ema_registers": {
        "name": "EMA national registers index",
        "status": "registered_routing_index",
        "url": "https://www.ema.europa.eu/en/medicines/national-registers-authorised-medicines",
    },
    "ansm_interactions": {
        "name": "ANSM interaction thesaurus",
        "status": "registered_safety_supplement_not_rcp",
        "url": "https://ansm.sante.fr/documents/reference/thesaurus-des-interactions-medicamenteuses-1",
    },
    "jfda": {
        "name": "Jordan JFDA eLeaflet / drug leaflet search",
        "status": "registered_public_webforms_adapter_required",
        "url": "https://services.jfda.jo/JFDA/registration/leafletdrugssearch.aspx",
    },
    "sfda": {
        "name": "Saudi SFDA Saudi Drugs Information System",
        "status": "registered_public_portal_spc_pil_session_required",
        "url": "https://sdi.sfda.gov.sa/",
    },
    "uae": {
        "name": "UAE EDE / MOHAP registered medical product directory",
        "status": "registered_public_portal_dynamic_or_captcha",
        "url": "https://www.ede.gov.ae/en/drug-directory",
    },
    "mfds": {
        "name": "Korea MFDS / NEDRUG",
        "status": "registered_public_portal_xml_endpoints_need_itemseq",
        "url": "https://nedrug.mfds.go.kr/eng/index",
    },
    "cecmed": {
        "name": "Cuba CECMED RCP Medicamentos",
        "status": "machine_adapter_pdf_scrape",
        "url": "https://www.cecmed.cu/registro/rcp/medicamentos",
    },
    "infarmed": {
        "name": "Portugal Infarmed / Infomed RCM and FI",
        "status": "registered_public_portal_dynamic_downloads",
        "url": "https://www.infarmed.pt/web/infarmed/servicos-on-line/pesquisa-do-medicamento",
    },
    "fass": {
        "name": "Sweden FASS API / product information",
        "status": "registered_api_access_or_subscription_required",
        "url": "https://www.fass.se/health/portal-document/fass-api",
    },
    "who_vaccines": {
        "name": "WHO prequalified vaccines and full vaccine product list",
        "status": "registered_vaccine_product_info_and_document_index",
        "url": "https://extranet.who.int/prequal/vaccines/list-prequalified-vaccines",
    },
}

SPANISH_SECTION_RULES = [
    ("indication", r"4\.1\s+Indicaciones terap[eé]uticas"),
    ("dosage", r"4\.2\s+Posolog[ií]a\s+y\s+forma\s+de\s+administraci[oó]n"),
    ("contraindication", r"4\.3\s+Contraindicaciones"),
    ("warning", r"4\.4\s+Advertencias\s+y\s+precauciones(?:\s+especiales)?(?:\s+de\s+empleo)?"),
    ("interaction", r"4\.5\s+Interacci[oó]n\s+con\s+otros\s+medicamentos"),
    ("special_population", r"4\.6\s+Fertilidad,\s+embarazo\s+y\s+lactancia"),
    ("adverse_effect", r"4\.8\s+Reacciones\s+adversas"),
    ("overdose", r"4\.9\s+Sobredosis"),
    ("pharmacology", r"5\.1\s+Propiedades\s+farmacodin[aá]micas"),
    ("pharmacology", r"5\.2\s+Propiedades\s+farmacocin[eé]ticas"),
    ("indication", r"1\.\s+Qu[eé]\s+es.*para\s+qu[eé]\s+se\s+utiliza"),
    ("warning", r"2\.\s+Qu[eé]\s+necesita\s+saber"),
    ("dosage", r"3\.\s+C[oó]mo\s+(?:tomar|usar|utilizar)"),
    ("adverse_effect", r"4\.\s+Posibles\s+efectos\s+adversos"),
    ("storage", r"5\.\s+Conservaci[oó]n"),
    # CECMED/Cuban RCP PDFs often use unnumbered RCP headings rather than the
    # EU 4.x structure. Keep these generic patterns late so the numbered
    # headings remain preferred when present.
    ("indication", r"\bIndicaciones(?:\s+terap[eé]uticas)?\b"),
    ("dosage", r"\b(?:Posolog[ií]a|Dosis(?:\s+y\s+v[ií]a\s+de\s+administraci[oó]n)?|Modo\s+de\s+administraci[oó]n)\b"),
    ("contraindication", r"\bContraindicaciones\b"),
    ("warning", r"\b(?:Advertencias|Precauciones|Advertencias\s+y\s+precauciones)\b"),
    ("interaction", r"\bInteracciones(?:\s+medicamentosas)?\b"),
    ("special_population", r"\b(?:Embarazo|Lactancia|Fertilidad|Uso\s+durante\s+el\s+embarazo)\b"),
    ("adverse_effect", r"\b(?:Efectos\s+indeseables|Reacciones\s+adversas|Efectos\s+adversos)\b"),
    ("overdose", r"\bSobredosis\b"),
    ("pharmacology", r"\b(?:Propiedades\s+farmacodin[aá]micas|Farmacodinamia|Propiedades\s+farmacocin[eé]ticas|Farmacocin[eé]tica)\b"),
    ("storage", r"\b(?:Conservaci[oó]n|Condiciones\s+de\s+almacenamiento|Almacenamiento)\b"),
]

ITALIAN_SECTION_RULES = [
    ("indication", r"4\.1\s+Indicazioni\s+terapeutiche"),
    ("dosage", r"4\.2\s+Posologia\s+e\s+modo\s+di\s+somministrazione"),
    ("contraindication", r"4\.3\s+Controindicazioni"),
    ("warning", r"4\.4\s+Avvertenze\s+speciali\s+e\s+precauzioni(?:\s+d['’]impiego)?"),
    ("interaction", r"4\.5\s+Interazioni\s+con\s+altri\s+medicinali"),
    ("special_population", r"4\.6\s+Fertilit[aà],\s+gravidanza\s+e\s+allattamento"),
    ("adverse_effect", r"4\.8\s+Effetti\s+indesiderati"),
    ("overdose", r"4\.9\s+Sovradosaggio"),
    ("pharmacology", r"5\.1\s+Propriet[aà]\s+farmacodinamiche"),
    ("pharmacology", r"5\.2\s+Propriet[aà]\s+farmacocinetiche"),
    ("indication", r"1\.\s+Che\s+cos['’]?[eè].*a\s+cosa\s+serve"),
    ("warning", r"2\.\s+Cosa\s+deve\s+sapere"),
    ("dosage", r"3\.\s+Come\s+(?:prendere|usare)"),
    ("adverse_effect", r"4\.\s+Possibili\s+effetti\s+indesiderati"),
    ("storage", r"5\.\s+Come\s+conservare"),
]

TURKISH_SECTION_RULES = [
    ("indication", r"4\.1\.?\s+Terap[oö]tik\s+endikasyonlar"),
    ("dosage", r"4\.2\.?\s+Pozoloji\s+ve\s+uygulama\s+[sş]ekli"),
    ("contraindication", r"4\.3\.?\s+Kontrendikasyonlar"),
    ("warning", r"4\.4\.?\s+[OÖ]zel\s+kullan[ıi]m\s+uyar[ıi]lar[ıi]\s+ve\s+[oö]nlemleri"),
    ("interaction", r"4\.5\.?\s+Di[gğ]er\s+t[ıi]bbi\s+[uü]r[uü]nler\s+ile\s+etkile[sş]imler"),
    ("special_population", r"4\.6\.?\s+Gebelik\s+ve\s+laktasyon"),
    ("adverse_effect", r"4\.8\.?\s+[İI]stenmeyen\s+etkiler"),
    ("overdose", r"4\.9\.?\s+Doz\s+a[sş][ıi]m[ıi]"),
    ("pharmacology", r"5\.1\.?\s+Farmakodinamik\s+[oö]zellikler"),
    ("pharmacology", r"5\.2\.?\s+Farmakokinetik\s+[oö]zellikler"),
]

ENGLISH_SECTION_RULES = [
    ("indication", r"4\.1\s+Therapeutic\s+indications"),
    ("dosage", r"4\.2\s+Posology\s+and\s+method\s+of\s+administration"),
    ("contraindication", r"4\.3\s+Contraindications"),
    ("warning", r"4\.4\s+Special\s+warnings\s+and\s+precautions"),
    ("interaction", r"4\.5\s+Interaction\s+with\s+other\s+medicinal\s+products"),
    ("special_population", r"4\.6\s+Fertility,\s+pregnancy\s+and\s+lactation"),
    ("adverse_effect", r"4\.8\s+Undesirable\s+effects"),
    ("overdose", r"4\.9\s+Overdose"),
    ("pharmacology", r"5\.1\s+Pharmacodynamic\s+properties"),
    ("pharmacology", r"5\.2\s+Pharmacokinetic\s+properties"),
    ("storage", r"6\.4\s+Special\s+precautions\s+for\s+storage"),
    ("indication", r"1\.\s+What\s+.*\s+is\s+and\s+what\s+it\s+is\s+used\s+for"),
    ("warning", r"2\.\s+What\s+you\s+need\s+to\s+know"),
    ("dosage", r"3\.\s+How\s+to\s+(?:take|use)"),
    ("adverse_effect", r"4\.\s+Possible\s+side\s+effects"),
    ("storage", r"5\.\s+How\s+to\s+store"),
]

PORTUGUESE_SECTION_RULES = [
    ("indication", r"4\.1\s+Indica[cç][oõ]es\s+terap[eê]uticas"),
    ("dosage", r"4\.2\s+Posologia\s+e\s+modo\s+de\s+administra[cç][aã]o"),
    ("contraindication", r"4\.3\s+Contraindica[cç][oõ]es"),
    ("warning", r"4\.4\s+Advert[eê]ncias\s+e\s+precau[cç][oõ]es"),
    ("interaction", r"4\.5\s+Intera[cç][oõ]es\s+medicamentosas"),
    ("special_population", r"4\.6\s+Fertilidade,\s+gravidez\s+e\s+aleitamento"),
    ("adverse_effect", r"4\.8\s+Efeitos\s+indesej[aá]veis"),
    ("overdose", r"4\.9\s+Sobredosagem"),
    ("pharmacology", r"5\.1\s+Propriedades\s+farmacodin[aâ]micas"),
    ("pharmacology", r"5\.2\s+Propriedades\s+farmacocin[eé]ticas"),
    ("storage", r"6\.4\s+Precau[cç][oõ]es\s+especiais\s+de\s+conserva[cç][aã]o"),
    ("indication", r"1\.\s+O\s+que\s+.*\s+e\s+para\s+que\s+[eé]\s+utilizado"),
    ("warning", r"2\.\s+O\s+que\s+precisa\s+de\s+saber"),
    ("dosage", r"3\.\s+Como\s+(?:tomar|utilizar)"),
    ("adverse_effect", r"4\.\s+Efeitos\s+indesej[aá]veis\s+poss[ií]veis"),
    ("storage", r"5\.\s+Como\s+conservar"),
]

SECTION_START_RE = re.compile(r"\b(?:4\.[12345689]|5\.[12]|[1-5]\.)\.?\s+")


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
    if isinstance(value, list):
        value = "\n".join(clean(item) for item in value)
    text = html.unescape(str(value))
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
    text = unicodedata.normalize("NFKD", clean(value).upper())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [{clean(k): clean(v) for k, v in row.items()} for row in csv.DictReader(handle)]


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
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "tunisia-cdss-data-remediation/1.0",
            "Accept": "application/json,text/html,*/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return "ok", "", response.read()
    except urllib.error.HTTPError as exc:
        return f"http_{exc.code}", clean(exc), b""
    except Exception as exc:
        return "error", clean(exc), b""


def request_bytes_with_opener(opener: urllib.request.OpenerDirector, url: str, timeout: int, data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None) -> Tuple[str, str, bytes]:
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": "tunisia-cdss-data-remediation/1.0",
            "Accept": "application/json,text/html,application/pdf,*/*",
            **(headers or {}),
        },
    )
    try:
        with opener.open(req, timeout=timeout) as response:
            return "ok", "", response.read()
    except urllib.error.HTTPError as exc:
        return f"http_{exc.code}", clean(exc), b""
    except Exception as exc:
        return "error", clean(exc), b""


def fetch_binary(url: str, raw_path: Path, timeout: int, resume: bool) -> Tuple[str, str, bytes, bool]:
    if resume and raw_path.exists() and raw_path.stat().st_size > 0:
        return "cached", "", raw_path.read_bytes(), True
    status, message, body = request_bytes(url, timeout)
    if status == "ok":
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(body)
        return status, message, body, False
    return status, message, b"", False


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
    status, message, text, cached = fetch_text(url, raw_path, timeout, resume)
    if status in {"ok", "cached"} and text:
        try:
            return status, message, json.loads(text), cached
        except Exception as exc:
            return "parse_error", clean(exc), {}, cached
    return status, message, {}, cached


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
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


def strip_html(value: str) -> str:
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", value)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</tr>|</h[1-6]>|</div>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return clean(text)


def classify_spanish_title(title: str) -> str:
    title_clean = clean(title)
    for kind, pattern in SPANISH_SECTION_RULES:
        if re.search(pattern, title_clean, flags=re.I):
            return kind
    title_norm = norm(title_clean)
    if "CONTRAINDICACION" in title_norm:
        return "contraindication"
    if "INDICACION" in title_norm or "PARA QUE SE UTILIZA" in title_norm:
        return "indication"
    if "POSOLOGIA" in title_norm or "COMO TOMAR" in title_norm or "FORMA DE ADMINISTRACION" in title_norm:
        return "dosage"
    if "INTERACCION" in title_norm:
        return "interaction"
    if "REACCIONES ADVERSAS" in title_norm or "EFECTOS ADVERSOS" in title_norm:
        return "adverse_effect"
    if "EMBARAZO" in title_norm or "LACTANCIA" in title_norm or "FERTILIDAD" in title_norm:
        return "special_population"
    if "ADVERTENCIA" in title_norm or "PRECAUCION" in title_norm or "NECESITA SABER" in title_norm:
        return "warning"
    if "SOBREDOSIS" in title_norm:
        return "overdose"
    if "FARMACODINAM" in title_norm or "FARMACOCINET" in title_norm:
        return "pharmacology"
    if "CONSERVACION" in title_norm:
        return "storage"
    return ""


def section_title_from_start(text: str, start: int) -> str:
    snippet = clean(text[start : min(len(text), start + 180)])
    if re.match(r"^\d+(?:\.\d+)*\.?\s+", snippet):
        return clean(snippet[:160])
    next_sentence = re.split(r"(?<=\.)\s+", snippet, maxsplit=1)[0]
    return clean(next_sentence[:160])


def extract_spanish_sections(text: str) -> List[Dict[str, str]]:
    normalized = clean(text)
    matches: List[Tuple[int, str, str]] = []
    for kind, pattern in SPANISH_SECTION_RULES:
        for match in re.finditer(pattern, normalized, flags=re.I):
            title = section_title_from_start(normalized, match.start()) or match.group(0)
            matches.append((match.start(), kind, title))
    # Fall back to numeric starts and classify their local heading.
    for match in SECTION_START_RE.finditer(normalized):
        title = section_title_from_start(normalized, match.start())
        title_prefix = norm(title[:100])
        if title_prefix.startswith("4 DATOS CLINICOS"):
            continue
        if any(label in title_prefix for label in ("NOMBRE DEL MEDICAMENTO", "COMPOSICION CUALITATIVA", "FORMA FARMACEUTICA")):
            continue
        kind = classify_spanish_title(title)
        if kind:
            matches.append((match.start(), kind, title))

    deduped: List[Tuple[int, str, str]] = []
    seen_pos: set[int] = set()
    for pos, kind, title in sorted(matches, key=lambda item: item[0]):
        if any(abs(pos - other) < 20 for other in seen_pos):
            continue
        seen_pos.add(pos)
        deduped.append((pos, kind, title))

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
        rows.append({"section_kind": kind, "section_title": title, "section_text": section_text})
    return rows


def classify_italian_title(title: str) -> str:
    title_clean = clean(title)
    for kind, pattern in ITALIAN_SECTION_RULES:
        if re.search(pattern, title_clean, flags=re.I):
            return kind
    title_norm = norm(title_clean)
    if "CONTROINDICAZION" in title_norm:
        return "contraindication"
    if "INDICAZION" in title_norm or "A COSA SERVE" in title_norm:
        return "indication"
    if "POSOLOGIA" in title_norm or "COME PRENDERE" in title_norm or "MODO DI SOMMINISTRAZIONE" in title_norm:
        return "dosage"
    if "INTERAZION" in title_norm:
        return "interaction"
    if "EFFETTI INDESIDERATI" in title_norm or "REAZIONI AVVERSE" in title_norm:
        return "adverse_effect"
    if "GRAVIDANZA" in title_norm or "ALLATTAMENTO" in title_norm or "FERTILITA" in title_norm:
        return "special_population"
    if "AVVERTENZ" in title_norm or "PRECAUZION" in title_norm or "COSA DEVE SAPERE" in title_norm:
        return "warning"
    if "SOVRADOSAGGIO" in title_norm:
        return "overdose"
    if "FARMACODINAM" in title_norm or "FARMACOCINET" in title_norm:
        return "pharmacology"
    if "CONSERVARE" in title_norm:
        return "storage"
    return ""


def classify_turkish_title(title: str) -> str:
    title_clean = clean(title)
    for kind, pattern in TURKISH_SECTION_RULES:
        if re.search(pattern, title_clean, flags=re.I):
            return kind
    title_norm = norm(title_clean)
    if "KONTRENDIKASYON" in title_norm:
        return "contraindication"
    if "ENDIKASYON" in title_norm:
        return "indication"
    if "POZOLOJI" in title_norm or "UYGULAMA SEKLI" in title_norm:
        return "dosage"
    if "ETKILESIM" in title_norm:
        return "interaction"
    if "ISTENMEYEN ETKI" in title_norm:
        return "adverse_effect"
    if "GEBELIK" in title_norm or "LAKTASYON" in title_norm:
        return "special_population"
    if "UYARI" in title_norm or "ONLEM" in title_norm:
        return "warning"
    if "DOZ ASIM" in title_norm:
        return "overdose"
    if "FARMAKODINAMIK" in title_norm or "FARMAKOKINETIK" in title_norm:
        return "pharmacology"
    return ""


def extract_sections_with_rules(text: str, rules: Sequence[Tuple[str, str]], classifier: Any) -> List[Dict[str, str]]:
    normalized = clean(text)
    matches: List[Tuple[int, str, str]] = []
    for kind, pattern in rules:
        for match in re.finditer(pattern, normalized, flags=re.I):
            title = section_title_from_start(normalized, match.start()) or match.group(0)
            matches.append((match.start(), kind, title))
    for match in SECTION_START_RE.finditer(normalized):
        title = section_title_from_start(normalized, match.start())
        title_norm = norm(title[:100])
        if title_norm.startswith(("1 DENOMINAZIONE", "2 COMPOSIZIONE", "3 FORMA", "1 BESERI")):
            continue
        kind = classifier(title)
        if kind:
            matches.append((match.start(), kind, title))

    deduped: List[Tuple[int, str, str]] = []
    seen_pos: set[int] = set()
    for pos, kind, title in sorted(matches, key=lambda item: item[0]):
        if any(abs(pos - other) < 20 for other in seen_pos):
            continue
        seen_pos.add(pos)
        deduped.append((pos, kind, title))

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
        rows.append({"section_kind": kind, "section_title": title, "section_text": section_text})
    return rows


def extract_italian_sections(text: str) -> List[Dict[str, str]]:
    return extract_sections_with_rules(text, ITALIAN_SECTION_RULES, classify_italian_title)


def extract_turkish_sections(text: str) -> List[Dict[str, str]]:
    return extract_sections_with_rules(text, TURKISH_SECTION_RULES, classify_turkish_title)


def classify_english_title(title: str) -> str:
    title_clean = clean(title)
    for kind, pattern in ENGLISH_SECTION_RULES:
        if re.search(pattern, title_clean, flags=re.I):
            return kind
    title_norm = norm(title_clean)
    if "CONTRAINDICATION" in title_norm:
        return "contraindication"
    if "THERAPEUTIC INDICATION" in title_norm or "WHAT IT IS USED FOR" in title_norm:
        return "indication"
    if "POSOLOGY" in title_norm or "METHOD OF ADMINISTRATION" in title_norm or "HOW TO TAKE" in title_norm or "HOW TO USE" in title_norm:
        return "dosage"
    if "INTERACTION" in title_norm:
        return "interaction"
    if "UNDESIRABLE EFFECT" in title_norm or "POSSIBLE SIDE EFFECT" in title_norm or "ADVERSE REACTION" in title_norm:
        return "adverse_effect"
    if "PREGNANCY" in title_norm or "LACTATION" in title_norm or "FERTILITY" in title_norm:
        return "special_population"
    if "WARNING" in title_norm or "PRECAUTION" in title_norm or "NEED TO KNOW" in title_norm:
        return "warning"
    if "OVERDOSE" in title_norm:
        return "overdose"
    if "PHARMACODYNAMIC" in title_norm or "PHARMACOKINETIC" in title_norm:
        return "pharmacology"
    if "STORAGE" in title_norm:
        return "storage"
    return ""


def classify_portuguese_title(title: str) -> str:
    title_clean = clean(title)
    for kind, pattern in PORTUGUESE_SECTION_RULES:
        if re.search(pattern, title_clean, flags=re.I):
            return kind
    title_norm = norm(title_clean)
    if "CONTRAINDIC" in title_norm:
        return "contraindication"
    if "INDICAC" in title_norm or "PARA QUE E UTILIZADO" in title_norm:
        return "indication"
    if "POSOLOGIA" in title_norm or "MODO DE ADMINISTRACAO" in title_norm or "COMO TOMAR" in title_norm or "COMO UTILIZAR" in title_norm:
        return "dosage"
    if "INTERAC" in title_norm:
        return "interaction"
    if "EFEITOS INDESEJAVEIS" in title_norm or "REACOES ADVERSAS" in title_norm:
        return "adverse_effect"
    if "GRAVIDEZ" in title_norm or "ALEITAMENTO" in title_norm or "FERTILIDADE" in title_norm:
        return "special_population"
    if "ADVERTEN" in title_norm or "PRECAUC" in title_norm or "PRECISA DE SABER" in title_norm:
        return "warning"
    if "SOBREDOSAGEM" in title_norm:
        return "overdose"
    if "FARMACODINAM" in title_norm or "FARMACOCINET" in title_norm:
        return "pharmacology"
    if "CONSERVAR" in title_norm or "CONSERVACAO" in title_norm:
        return "storage"
    return ""


def extract_english_sections(text: str) -> List[Dict[str, str]]:
    return extract_sections_with_rules(text, ENGLISH_SECTION_RULES, classify_english_title)


def extract_portuguese_sections(text: str) -> List[Dict[str, str]]:
    return extract_sections_with_rules(text, PORTUGUESE_SECTION_RULES, classify_portuguese_title)


def recursive_text(value: Any) -> str:
    parts: List[str] = []
    if isinstance(value, dict):
        for key in ("contenido", "texto", "text", "html"):
            text = clean(value.get(key))
            if text:
                parts.append(strip_html(text))
        for item in value.values():
            if isinstance(item, (dict, list)):
                nested = recursive_text(item)
                if nested:
                    parts.append(nested)
    elif isinstance(value, list):
        for item in value:
            nested = recursive_text(item)
            if nested:
                parts.append(nested)
    return clean(" ".join(parts))


def result_items(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("resultados", "data", "results", "items", "medicamentos", "content"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = result_items(value)
            if nested:
                return nested
    if any(key.lower() in {"nregistro", "nombre", "pactivos", "labtitular"} for key in data.keys()):
        return [data]
    return []


def pick(record: Dict[str, Any], *keys: str) -> str:
    lower = {key.lower(): key for key in record.keys()}
    for key in keys:
        real = lower.get(key.lower())
        if real is not None:
            return clean(record.get(real))
    return ""


def nested_values(value: Any, wanted_keys: set[str]) -> List[str]:
    values: List[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if key.lower() in wanted_keys:
                text = clean(nested)
                if text:
                    values.append(text)
            if isinstance(nested, (dict, list)):
                values.extend(nested_values(nested, wanted_keys))
    elif isinstance(value, list):
        for item in value:
            values.extend(nested_values(item, wanted_keys))
    return values


def nested_text(value: Any, wanted_keys: set[str]) -> str:
    return clean(" ".join(nested_values(value, wanted_keys)))


def first_query_token(value: str) -> str:
    tokens = [token for token in norm(value).split() if len(token) >= 3]
    return tokens[0] if tokens else ""


def first_url(value: Any) -> str:
    text = clean(value)
    href = re.search(r"href=['\"]([^'\"]+)['\"]", text, flags=re.I)
    if href:
        return urllib.parse.urljoin(TITCK_BASE, clean(href.group(1)))
    url = re.search(r"https?://[^\s<>'\"]+", text)
    if url:
        return clean(url.group(0))
    return text if text.startswith("http") else ""


def query_candidates(row: Dict[str, str]) -> List[str]:
    candidates = [
        row.get("query_generic", ""),
        row.get("nom_generique", ""),
        row.get("query_primary", ""),
        row.get("query_brand", ""),
        row.get("nom", ""),
    ]
    nom = clean(row.get("nom", ""))
    if nom:
        candidates.append(re.split(r"\s+\d|,|\(", nom)[0])
    out: List[str] = []
    seen = set()
    for candidate in candidates:
        candidate = clean(candidate)
        key = norm(candidate)
        if len(candidate) >= 3 and key and key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def cecmed_query_candidates(row: Dict[str, str]) -> List[str]:
    """Brand-first queries for CECMED.

    CECMED RCP pages are indexed by Spanish/Cuban product titles. The generic
    names in the Tunisian queue are often French (for example, "VACCIN CONTRE
    L'HEPATITE B"), so using the generic first misses exact product pages.
    """
    nom = clean(row.get("nom", ""))
    dosage = clean(row.get("dosage", ""))
    candidates = [
        row.get("query_brand", ""),
        nom,
        f"{nom} {dosage}" if nom and dosage else "",
        row.get("query_primary", ""),
        row.get("query_generic", ""),
        row.get("nom_generique", ""),
        row.get("labo", ""),
    ]
    out: List[str] = []
    seen = set()
    for candidate in candidates:
        candidate = clean(candidate)
        key = norm(candidate)
        if len(candidate) >= 3 and key and key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def country_key(row: Dict[str, str]) -> str:
    return norm(row.get("pays") or row.get("catalog_pays") or row.get("list_amm_pays") or "")


def is_vaccine_like(row: Dict[str, str]) -> bool:
    haystack = norm(" ".join([row.get("nom", ""), row.get("nom_generique", ""), row.get("query_primary", ""), row.get("query_generic", "")]))
    vaccine_tokens = (
        "VACCIN", "VACCINE", "COVID", "COMIRNATY", "SPIKEVAX", "CORONAVAC",
        "SPUTNIK", "POLIO", "HEPATITE", "HEPATITIS", "FLU", "INFLUENZA",
        "GRIPPE", "RAGE", "RABIES", "ROUGEOLE", "MEASLES", "RUBELLA",
        "VARICEL", "BCG", "DTP", "PNEUMOCOCC", "MENINGOCOCC", "ROTAVIRUS",
    )
    return any(token in haystack for token in vaccine_tokens)


def source_relevant(row: Dict[str, str], source: str) -> bool:
    country = country_key(row)
    if source == "cima":
        return True
    if source in {"emc", "mhra"}:
        return "RAYAUME UNI" in country or "UNITED KINGDOM" in country or "UK" == country or "AUSTRALIA" in country
    if source == "ema":
        return any(
            token in country
            for token in (
                "FRANCE",
                "ITALIE",
                "ITALY",
                "ALLEMAGNE",
                "GERMANY",
                "BELGIQUE",
                "BELGIUM",
                "AUTRICHE",
                "AUSTRIA",
                "DENEMARK",
                "DENMARK",
                "SUEDE",
                "SWEDEN",
                "ESPAGNE",
                "SPAIN",
                "PORTUGAL",
                "GRECE",
                "GREECE",
                "POLOGNE",
                "POLAND",
                "BULGARIE",
                "BULGARIA",
                "RAYAUME UNI",
                "UNITED KINGDOM",
            )
        )
    if source == "aifa":
        return "ITALIE" in country or "ITALY" in country
    if source == "titck":
        return "TURQUI" in country or "TURKEY" in country
    if source == "canada":
        return "CANADA" in country
    if source == "swissmedic":
        return "SUISSE" in country or "SWITZERLAND" in country
    if source == "pharmnet":
        return "ALLEMAGNE" in country or "GERMANY" in country or "DEUTSCHLAND" in country
    if source == "famhp":
        return "BELGIQUE" in country or "BELGIUM" in country
    if source == "basg":
        return "AUTRICHE" in country or "AUSTRIA" in country
    if source == "denmark":
        return "DENEMARK" in country or "DANEMARK" in country or "DENMARK" in country or "SUEDE" in country or "SWEDEN" in country
    if source == "france":
        return "FRANCE" in country or "TUNISIE" in country
    if source == "jfda":
        return "JORDAN" in country or "JORDANIE" in country
    if source == "sfda":
        return any(token in country for token in ("SAUDI", "ARABIE SAOUDITE", "KSA", "JORDAN", "JORDANIE", "EMIRATS", "UAE"))
    if source == "uae":
        return any(token in country for token in ("EMIRATS", "EMIRATES", "UAE", "DUBAI"))
    if source == "mfds":
        return "COREE" in country or "KOREA" in country or "KORE" in country
    if source == "cecmed":
        return "CUBA" in country
    if source == "infarmed":
        return "PORTUGAL" in country
    if source == "fass":
        return "SUEDE" in country or "SWEDEN" in country or "DENMARK" in country or "DENEMARK" in country or "DANEMARK" in country
    if source == "who_vaccines":
        return is_vaccine_like(row)
    if source in {"ema_registers", "ansm_interactions"}:
        return True
    return True


def cima_active_text(record: Dict[str, Any]) -> str:
    return clean(
        " ".join(
            [
                pick(record, "pactivos", "principiosActivos", "dosis"),
                nested_text(record.get("vtm"), {"nombre"}),
                nested_text(record.get("principiosActivos"), {"nombre"}),
            ]
        )
    )


def cima_holder_text(record: Dict[str, Any]) -> str:
    return clean(
        " ".join(
            [
                pick(record, "labtitular", "labcomercializador", "laboratorio"),
                nested_text(record.get("laboratorios"), {"nombre"}),
            ]
        )
    )


def cima_doc_urls(record: Dict[str, Any], nregistro: str) -> List[str]:
    urls: List[str] = []
    docs = record.get("docs")
    if isinstance(docs, list):
        sorted_docs = sorted(
            [doc for doc in docs if isinstance(doc, dict)],
            key=lambda doc: (0 if str(doc.get("tipo", "")) == "1" else 1 if str(doc.get("tipo", "")) == "2" else 2),
        )
        for doc in sorted_docs:
            url = clean(doc.get("urlHtml") or "")
            if url and url not in urls:
                urls.append(url)

    quoted = urllib.parse.quote(nregistro)
    fallback_urls = [
        f"{CIMA_BASE}/dochtml/ft/{quoted}/FT_{quoted}.html",
        f"{CIMA_BASE}/dochtml/ft/{quoted}/FichaTecnica.html",
        f"{CIMA_BASE}/dochtml/p/{quoted}/P_{quoted}.html",
        f"{CIMA_BASE}/dochtml/p/{quoted}/Prospecto.html",
        f"{CIMA_BASE}/dochtml/p/{quoted}/Prospecto_{quoted}.html",
    ]
    for url in fallback_urls:
        if url not in urls:
            urls.append(url)
    return urls


def score_cima_match(row: Dict[str, str], record: Dict[str, Any]) -> float:
    query_generic = norm(row.get("query_generic") or row.get("nom_generique"))
    query_brand = norm(row.get("query_brand") or row.get("nom"))
    query_labo = norm(row.get("labo"))
    query_dosage = norm(row.get("dosage"))
    query_form = norm(row.get("forme"))

    name = norm(pick(record, "nombre"))
    active = norm(cima_active_text(record))
    holder = norm(cima_holder_text(record))
    dose = norm(pick(record, "dosis"))
    form = norm(" ".join([pick(record, "formaFarmaceutica"), pick(record, "formaFarmaceuticaSimplificada")]))
    brand_token = first_query_token(query_brand)

    score = 0.0
    if query_generic and (query_generic in active or query_generic in name):
        score += 0.55
    if query_brand and query_brand in name:
        score += 0.50
    elif brand_token and name.startswith(brand_token):
        score += 0.45
    if query_labo and holder and any(token in holder for token in query_labo.split()[:3] if len(token) >= 4):
        score += 0.05
    if query_dosage and dose and query_dosage in dose:
        score += 0.03
    if query_form and form and any(token in form for token in query_form.split() if len(token) >= 5):
        score += 0.02
    return min(score, 0.95)


def cima_search(query: str, mode: str, raw_dir: Path, timeout: int, resume: bool) -> Tuple[str, str, List[Dict[str, Any]]]:
    param = "practiv1" if mode == "active" else "nombre"
    url = f"{CIMA_REST}/medicamentos?{param}={urllib.parse.quote(query)}"
    raw = cache_path(raw_dir, f"cima_search_{mode}", query, "json")
    status, message, data, _ = fetch_json(url, raw, timeout, resume)
    return status, message, result_items(data)


def fetch_cima_sections_for_record(record: Dict[str, Any], raw_dir: Path, timeout: int, resume: bool) -> Tuple[str, str, List[Dict[str, str]]]:
    nregistro = pick(record, "nregistro")
    if not nregistro:
        return "cima_no_nregistro", "", []

    for url in cima_doc_urls(record, nregistro):
        raw = cache_path(raw_dir, "cima_dochtml", url, "html")
        status, _message, page, _ = fetch_text(url, raw, timeout, resume)
        if status not in {"ok", "cached"} or not page:
            continue
        sections = extract_spanish_sections(strip_html(page))
        if sections:
            return "ok_html", url, sections

    # Try the REST segmented document first. CIMA docs describe both a
    # segmented-content route and full HTML routes; the full routes are kept as
    # fallbacks because older records vary in file naming.
    for tipo_doc in ("1", "ft"):
        url = f"{CIMA_REST}/docSegmentado/contenido/{tipo_doc}?nregistro={urllib.parse.quote(nregistro)}"
        raw = cache_path(raw_dir, f"cima_segmented_{tipo_doc}", nregistro, "json")
        status, _message, data, _ = fetch_json(url, raw, timeout, resume)
        if status in {"ok", "cached"} and data:
            sections: List[Dict[str, str]] = []
            items = data if isinstance(data, list) else result_items(data)
            if isinstance(data, dict) and not items:
                items = [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                title = clean(item.get("titulo") or item.get("seccion") or item.get("orden") or "")
                text = recursive_text(item)
                kind = classify_spanish_title(title) or classify_spanish_title(text[:180])
                if kind and len(text) >= 80:
                    sections.append({"section_kind": kind, "section_title": title or kind, "section_text": text[:8000]})
            if sections:
                return "ok_segmented", url, sections

    return "cima_no_usable_sections", "", []


def cima_sections(
    row: Dict[str, str],
    raw_dir: Path,
    timeout: int,
    resume: bool,
    max_docs: int,
    min_match_score: float,
) -> Tuple[str, str, str, float, List[Dict[str, str]]]:
    candidates: Dict[str, Dict[str, Any]] = {}
    statuses: List[str] = []
    for mode, query in (
        ("active", row.get("query_generic") or row.get("nom_generique")),
        ("active", row.get("query_primary")),
        ("name", row.get("query_brand") or row.get("nom")),
        ("name", re.split(r"\s+\d|,|\(", clean(row.get("nom", "")))[0]),
    ):
        query = clean(query)
        if len(query) < 3:
            continue
        status, _message, items = cima_search(query, mode, raw_dir, timeout, resume)
        statuses.append(f"{mode}:{status}:{len(items)}")
        for item in items:
            nregistro = pick(item, "nregistro")
            if nregistro:
                candidates[nregistro] = item

    scored = sorted(
        ((score_cima_match(row, record), nregistro, record) for nregistro, record in candidates.items()),
        key=lambda item: item[0],
        reverse=True,
    )
    for score, nregistro, _record in scored[:max_docs]:
        if score < min_match_score:
            continue
        doc_status, record_url, sections = fetch_cima_sections_for_record(_record, raw_dir, timeout, resume)
        statuses.append(f"doc:{doc_status}")
        if sections:
            return "ok", nregistro, record_url, score, sections
    return ";".join(statuses) or "cima_no_query", "", "", 0.0, []


def aifa_name(record: Dict[str, Any]) -> str:
    med = record.get("medicinale") if isinstance(record.get("medicinale"), dict) else {}
    return pick(med, "denominazioneMedicinale") or pick(record, "denominazioneMedicinale", "denominazione")


def aifa_holder(record: Dict[str, Any]) -> str:
    med = record.get("medicinale") if isinstance(record.get("medicinale"), dict) else {}
    return pick(med, "aziendaTitolare") or pick(record, "aziendaTitolare")


def aifa_active_text(record: Dict[str, Any]) -> str:
    return clean(" ".join(nested_values(record, {"principiattiviit", "principioattivo"})))


def aifa_search(query: str, raw_dir: Path, timeout: int, resume: bool) -> Tuple[str, str, List[Dict[str, Any]]]:
    url = f"{AIFA_BASE}/formadosaggio/ricerca?query={urllib.parse.quote(query)}&page=0&spellingCorrection=true"
    raw = cache_path(raw_dir, "aifa_search", query, "json")
    status, message, data, _ = fetch_json(url, raw, timeout, resume)
    return status, message, result_items(data)


def score_aifa_match(row: Dict[str, str], record: Dict[str, Any]) -> float:
    query_generic = norm(row.get("query_generic") or row.get("nom_generique"))
    query_brand = norm(row.get("query_brand") or row.get("nom"))
    query_labo = norm(row.get("labo"))
    query_form = norm(row.get("forme"))
    query_dosage = norm(row.get("dosage"))

    name = norm(aifa_name(record))
    active = norm(aifa_active_text(record))
    holder = norm(aifa_holder(record))
    form = norm(pick(record, "formaFarmaceutica", "descrizioneFormaDosaggio"))
    pack_text = norm(clean(record.get("confezioni")))
    brand_token = first_query_token(query_brand)

    score = 0.0
    if query_generic and (query_generic in active or query_generic in name):
        score += 0.55
    if query_brand and query_brand in name:
        score += 0.45
    elif brand_token and name.startswith(brand_token):
        score += 0.40
    if query_labo and holder and any(token in holder for token in query_labo.split()[:3] if len(token) >= 4):
        score += 0.05
    if query_form and form and any(token in form for token in query_form.split() if len(token) >= 5):
        score += 0.03
    if query_dosage and pack_text and query_dosage in pack_text:
        score += 0.02
    return min(score, 0.95)


def aifa_metadata_sections(record: Dict[str, Any]) -> List[Dict[str, str]]:
    med = record.get("medicinale") if isinstance(record.get("medicinale"), dict) else {}
    packages = record.get("confezioni") if isinstance(record.get("confezioni"), list) else []
    package_text = "; ".join(
        clean(" ".join([clean(pkg.get("aic")), clean(pkg.get("denominazionePackage")), clean(pkg.get("descrizioneRf"))]))
        for pkg in packages[:8]
        if isinstance(pkg, dict)
    )
    sections = [
        {
            "section_kind": "identity",
            "section_title": "AIFA identity",
            "section_text": clean(
                " ".join(
                    [
                        f"Medicinale: {aifa_name(record)}.",
                        f"AIC6: {clean(med.get('aic6'))}." if isinstance(med, dict) and clean(med.get("aic6")) else "",
                        f"Titolare: {aifa_holder(record)}." if aifa_holder(record) else "",
                        f"Stato amministrativo: {clean(med.get('statoAmministrativo'))}." if isinstance(med, dict) and clean(med.get("statoAmministrativo")) else "",
                    ]
                )
            ),
        },
        {
            "section_kind": "composition",
            "section_title": "AIFA principi attivi",
            "section_text": f"Principi attivi AIFA: {aifa_active_text(record)}.",
        },
        {
            "section_kind": "presentation",
            "section_title": "AIFA confezioni e forma",
            "section_text": clean(
                " ".join(
                    [
                        f"Forma farmaceutica: {pick(record, 'formaFarmaceutica', 'descrizioneFormaDosaggio')}.",
                        f"Vie di somministrazione: {clean(record.get('vieSomministrazione'))}.",
                        f"ATC: {clean(record.get('codiceAtc'))} {clean(record.get('descrizioneAtc'))}.",
                        f"Confezioni: {package_text}." if package_text else "",
                    ]
                )
            ),
        },
    ]
    return [section for section in sections if len(clean(section.get("section_text"))) >= 40]


def fetch_aifa_sections_for_record(record: Dict[str, Any], raw_dir: Path, timeout: int, resume: bool) -> Tuple[str, str, str, List[Dict[str, str]]]:
    med = record.get("medicinale") if isinstance(record.get("medicinale"), dict) else {}
    codice_sis = clean(med.get("codiceSis") if isinstance(med, dict) else "")
    aic6 = clean(med.get("aic6") if isinstance(med, dict) else "")
    form_id = clean(record.get("id"))
    record_id = aic6 or form_id
    sections = aifa_metadata_sections(record)

    if codice_sis and aic6:
        for stamp_type, source_system in (("RCP", "aifa_rcp_pdf"), ("FI", "aifa_foglio_illustrativo_pdf")):
            url = f"{AIFA_BASE}/organizzazione/{urllib.parse.quote(codice_sis)}/farmaci/{urllib.parse.quote(aic6)}/stampati?ts={stamp_type}"
            raw_pdf = cache_path(raw_dir, f"aifa_{stamp_type.lower()}_pdf", f"{codice_sis}_{aic6}", "pdf")
            status, _message, body, _ = fetch_binary(url, raw_pdf, timeout, resume)
            if status not in {"ok", "cached"} or not body:
                continue
            text = _extract_pdf_text(body)
            extracted = extract_italian_sections(text)
            if extracted:
                return f"ok_{stamp_type.lower()}", record_id, url, sections + extracted
        return "aifa_no_usable_pdf_sections", record_id, f"{AIFA_BASE}/formadosaggio/{urllib.parse.quote(form_id)}?lang=it", sections
    return "aifa_no_sis_aic6", record_id, "", sections


def aifa_sections(
    row: Dict[str, str],
    raw_dir: Path,
    timeout: int,
    resume: bool,
    max_docs: int,
    min_match_score: float,
) -> Tuple[str, str, str, float, List[Dict[str, str]]]:
    candidates: Dict[str, Dict[str, Any]] = {}
    statuses: List[str] = []
    for query in query_candidates(row):
        status, _message, items = aifa_search(query, raw_dir, timeout, resume)
        statuses.append(f"search:{status}:{len(items)}")
        for item in items:
            record_id = clean(item.get("id")) or clean(nested_text(item, {"aic6"}))
            if record_id:
                candidates[record_id] = item

    scored = sorted(
        ((score_aifa_match(row, record), record_id, record) for record_id, record in candidates.items()),
        key=lambda item: item[0],
        reverse=True,
    )
    for score, _record_id, record in scored[:max_docs]:
        if score < min_match_score:
            continue
        status, record_id, source_file, sections = fetch_aifa_sections_for_record(record, raw_dir, timeout, resume)
        statuses.append(f"doc:{status}")
        if sections:
            return status, record_id, source_file, score, sections
    return ";".join(statuses) or "aifa_no_query", "", "", 0.0, []


def health_canada_search(query: str, raw_dir: Path, timeout: int, resume: bool) -> Tuple[str, str, List[Dict[str, Any]]]:
    url = f"{HEALTH_CANADA_DPD}/drugproduct/?lang=en&type=json&brandname={urllib.parse.quote(query)}"
    raw = cache_path(raw_dir, "health_canada_drugproduct", query, "json")
    status, message, data, _ = fetch_json(url, raw, timeout, resume)
    items = data if isinstance(data, list) else result_items(data)
    return status, message, [item for item in items if isinstance(item, dict)]


def health_canada_resource(resource: str, drug_code: str, raw_dir: Path, timeout: int, resume: bool) -> List[Dict[str, Any]]:
    url = f"{HEALTH_CANADA_DPD}/{resource}/{urllib.parse.quote(drug_code)}?lang=en&type=json"
    raw = cache_path(raw_dir, f"health_canada_{resource}", drug_code, "json")
    status, _message, data, _ = fetch_json(url, raw, timeout, resume)
    if status not in {"ok", "cached"}:
        return []
    items = data if isinstance(data, list) else result_items(data)
    return [item for item in items if isinstance(item, dict)]


def score_health_canada_match(row: Dict[str, str], record: Dict[str, Any], active_text: str = "") -> float:
    query_generic = norm(row.get("query_generic") or row.get("nom_generique"))
    query_brand = norm(row.get("query_brand") or row.get("nom"))
    query_labo = norm(row.get("labo"))
    brand = norm(pick(record, "brand_name"))
    company = norm(pick(record, "company_name"))
    active = norm(active_text)
    brand_token = first_query_token(query_brand)
    score = 0.0
    if query_brand and query_brand in brand:
        score += 0.50
    elif brand_token and brand.startswith(brand_token):
        score += 0.42
    if query_generic and query_generic in active:
        score += 0.35
    if query_labo and company and any(token in company for token in query_labo.split()[:3] if len(token) >= 4):
        score += 0.05
    return min(score, 0.90)


def health_canada_metadata_sections(record: Dict[str, Any], raw_dir: Path, timeout: int, resume: bool) -> Tuple[str, List[Dict[str, str]]]:
    drug_code = clean(record.get("drug_code"))
    if not drug_code:
        return "", []
    resources = {
        "active": health_canada_resource("activeingredient", drug_code, raw_dir, timeout, resume),
        "form": health_canada_resource("form", drug_code, raw_dir, timeout, resume),
        "route": health_canada_resource("route", drug_code, raw_dir, timeout, resume),
        "class": health_canada_resource("therapeuticclass", drug_code, raw_dir, timeout, resume),
        "schedule": health_canada_resource("schedule", drug_code, raw_dir, timeout, resume),
        "status": health_canada_resource("status", drug_code, raw_dir, timeout, resume),
    }
    active_text = clean(
        "; ".join(
            clean(
                " ".join(
                    [
                        pick(item, "ingredient_name", "active_ingredient_name", "proper_name"),
                        pick(item, "strength"),
                        pick(item, "strength_unit"),
                    ]
                )
            )
            for item in resources["active"]
        )
    )
    form_text = clean("; ".join(pick(item, "pharmaceutical_form_name", "form_name") for item in resources["form"]))
    route_text = clean("; ".join(pick(item, "route_of_administration_name", "route_name") for item in resources["route"]))
    class_text = clean("; ".join(pick(item, "tc_atc_number", "tc_atc", "therapeutic_class") + " " + pick(item, "tc_atc_description", "class_name") for item in resources["class"]))
    schedule_text = clean("; ".join(" ".join(clean(value) for value in item.values()) for item in resources["schedule"]))
    status_text = clean("; ".join(" ".join(clean(value) for value in item.values()) for item in resources["status"]))
    sections = [
        {
            "section_kind": "identity",
            "section_title": "Health Canada DPD identity",
            "section_text": clean(
                " ".join(
                    [
                        f"Brand name: {pick(record, 'brand_name')}.",
                        f"Drug code: {drug_code}. DIN: {pick(record, 'drug_identification_number')}.",
                        f"Company: {pick(record, 'company_name')}.",
                        f"Last update: {pick(record, 'last_update_date')}.",
                        f"Status: {status_text}." if status_text else "",
                    ]
                )
            ),
        },
        {
            "section_kind": "composition",
            "section_title": "Health Canada DPD active ingredients",
            "section_text": f"Active ingredients: {active_text}.",
        },
        {
            "section_kind": "presentation",
            "section_title": "Health Canada DPD form, route, and schedule",
            "section_text": clean(
                " ".join(
                    [
                        f"Dosage form: {form_text}." if form_text else "",
                        f"Route: {route_text}." if route_text else "",
                        f"Schedule: {schedule_text}." if schedule_text else "",
                    ]
                )
            ),
        },
        {
            "section_kind": "pharmacology",
            "section_title": "Health Canada DPD therapeutic class",
            "section_text": f"Therapeutic class: {class_text}.",
        },
    ]
    return active_text, [section for section in sections if len(clean(section.get("section_text"))) >= 35]


def health_canada_sections(
    row: Dict[str, str],
    raw_dir: Path,
    timeout: int,
    resume: bool,
    max_docs: int,
    min_match_score: float,
) -> Tuple[str, str, str, float, List[Dict[str, str]]]:
    candidates: Dict[str, Dict[str, Any]] = {}
    statuses: List[str] = []
    for query in query_candidates(row):
        status, _message, items = health_canada_search(query, raw_dir, timeout, resume)
        statuses.append(f"brand:{status}:{len(items)}")
        for item in items:
            drug_code = clean(item.get("drug_code"))
            if drug_code:
                candidates[drug_code] = item
    if not candidates:
        return ";".join(statuses) or "canada_no_query", "", "", 0.0, []

    enriched: List[Tuple[float, str, Dict[str, Any], List[Dict[str, str]]]] = []
    for drug_code, record in list(candidates.items())[: max(max_docs * 4, 8)]:
        active_text, sections = health_canada_metadata_sections(record, raw_dir, timeout, resume)
        score = score_health_canada_match(row, record, active_text)
        enriched.append((score, drug_code, record, sections))
    enriched.sort(key=lambda item: item[0], reverse=True)
    for score, drug_code, _record, sections in enriched[:max_docs]:
        if score < min(min_match_score, 0.45):
            continue
        if sections:
            return "ok_metadata", drug_code, f"{HEALTH_CANADA_DPD}/drugproduct/{drug_code}?lang=en&type=json", score, sections
    return ";".join(statuses) or "canada_no_accepted_match", "", "", 0.0, []


def titck_token_opener(timeout: int) -> Tuple[str, str, Optional[urllib.request.OpenerDirector]]:
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    status, message, body = request_bytes_with_opener(opener, TITCK_KUBKT, timeout)
    if status != "ok":
        return status, message, None
    page = body.decode("utf-8", "ignore")
    token_match = re.search(r"_token:\s*['\"]([^'\"]+)['\"]", page)
    if not token_match:
        token_match = re.search(r'name=["\']_token["\']\s+value=["\']([^"\']+)["\']', page)
    if not token_match:
        return "titck_no_csrf_token", "", None
    opener.addheaders = [
        ("User-Agent", "tunisia-cdss-data-remediation/1.0"),
        ("X-Requested-With", "XMLHttpRequest"),
        ("X-CSRF-TOKEN", token_match.group(1)),
        ("Referer", TITCK_KUBKT),
    ]
    return "ok", "", opener


def titck_datatable_body(query: str, length: int = 10) -> bytes:
    fields: List[Tuple[str, str]] = [
        ("draw", "1"),
        ("start", "0"),
        ("length", str(length)),
        ("search[value]", query),
        ("search[regex]", "false"),
    ]
    columns = ["name", "element", "firmName", "confirmationDateKub", "confirmationDateKt"]
    for idx, column in enumerate(columns):
        fields.extend(
            [
                (f"columns[{idx}][data]", column),
                (f"columns[{idx}][name]", column),
                (f"columns[{idx}][searchable]", "true"),
                (f"columns[{idx}][orderable]", "true"),
                (f"columns[{idx}][search][value]", ""),
                (f"columns[{idx}][search][regex]", "false"),
            ]
        )
    fields.extend([("order[0][column]", "0"), ("order[0][dir]", "asc")])
    return urllib.parse.urlencode(fields).encode("utf-8")


def titck_search(query: str, raw_dir: Path, timeout: int, resume: bool) -> Tuple[str, str, List[Dict[str, Any]]]:
    raw = cache_path(raw_dir, "titck_search", query, "json")
    if resume and raw.exists() and raw.stat().st_size > 0:
        try:
            data = json.loads(raw.read_text(encoding="utf-8-sig", errors="ignore"))
            return "cached", "", result_items(data)
        except Exception:
            pass
    token_status, message, opener = titck_token_opener(timeout)
    if token_status != "ok" or opener is None:
        return token_status, message, []
    body = titck_datatable_body(query, length=20)
    status, post_message, data_bytes = request_bytes_with_opener(
        opener,
        TITCK_DATATABLE,
        timeout,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
    )
    if status != "ok":
        return status, post_message, []
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(data_bytes)
    try:
        data = json.loads(data_bytes.decode("utf-8", "ignore"))
    except Exception as exc:
        return "parse_error", clean(exc), []
    return "ok", "", result_items(data)


def score_titck_match(row: Dict[str, str], record: Dict[str, Any]) -> float:
    query_generic = norm(row.get("query_generic") or row.get("nom_generique"))
    query_brand = norm(row.get("query_brand") or row.get("nom"))
    query_labo = norm(row.get("labo"))
    name = norm(pick(record, "name"))
    active = norm(pick(record, "element"))
    holder = norm(pick(record, "firmName"))
    brand_token = first_query_token(query_brand)
    score = 0.0
    if query_generic and query_generic in active:
        score += 0.55
    if query_brand and query_brand in name:
        score += 0.45
    elif brand_token and name.startswith(brand_token):
        score += 0.40
    if query_labo and holder and any(token in holder for token in query_labo.split()[:3] if len(token) >= 4):
        score += 0.05
    return min(score, 0.95)


def titck_metadata_sections(record: Dict[str, Any]) -> List[Dict[str, str]]:
    text = clean(
        " ".join(
            [
                f"Medicine: {pick(record, 'name')}.",
                f"Active substance: {pick(record, 'element')}.",
                f"Company: {pick(record, 'firmName')}.",
                f"KUB approval date: {pick(record, 'confirmationDateKub')}.",
                f"KT approval date: {pick(record, 'confirmationDateKt')}.",
            ]
        )
    )
    return [{"section_kind": "identity", "section_title": "TITCK KUB/KT identity", "section_text": text}] if len(text) >= 40 else []


def fetch_titck_sections_for_record(record: Dict[str, Any], raw_dir: Path, timeout: int, resume: bool) -> Tuple[str, str, str, List[Dict[str, str]]]:
    record_id = sha(pick(record, "name"), pick(record, "element"), pick(record, "firmName"))[:16]
    sections = titck_metadata_sections(record)
    for key, source_system in (("documentPathKub", "titck_kub_pdf"), ("documentPathKt", "titck_kt_pdf")):
        url = first_url(record.get(key))
        if not url:
            continue
        raw_pdf = cache_path(raw_dir, key.lower(), url, "pdf")
        status, _message, body, _ = fetch_binary(url, raw_pdf, timeout, resume)
        if status not in {"ok", "cached"} or not body:
            continue
        text = _extract_pdf_text(body)
        extracted = extract_turkish_sections(text)
        if extracted:
            return "ok_kub" if source_system == "titck_kub_pdf" else "ok_kt", record_id, url, sections + extracted
    return "titck_no_usable_pdf_sections", record_id, first_url(record.get("documentPathKub") or record.get("documentPathKt")), sections


def titck_sections(
    row: Dict[str, str],
    raw_dir: Path,
    timeout: int,
    resume: bool,
    max_docs: int,
    min_match_score: float,
) -> Tuple[str, str, str, float, List[Dict[str, str]]]:
    candidates: Dict[str, Dict[str, Any]] = {}
    statuses: List[str] = []
    for query in query_candidates(row):
        status, _message, items = titck_search(query, raw_dir, timeout, resume)
        statuses.append(f"search:{status}:{len(items)}")
        for item in items:
            key = sha(pick(item, "name"), pick(item, "element"), pick(item, "firmName"))[:16]
            candidates[key] = item
    scored = sorted(
        ((score_titck_match(row, record), key, record) for key, record in candidates.items()),
        key=lambda item: item[0],
        reverse=True,
    )
    for score, _key, record in scored[:max_docs]:
        if score < min_match_score:
            continue
        status, record_id, source_file, sections = fetch_titck_sections_for_record(record, raw_dir, timeout, resume)
        statuses.append(f"doc:{status}")
        if sections:
            return status, record_id, source_file, score, sections
    return ";".join(statuses) or "titck_no_query", "", "", 0.0, []


def parse_cecmed_detail_record(page: str, base_url: str) -> List[Dict[str, str]]:
    """Parse a CECMED product detail page that has one RCP download link."""
    title_match = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", page)
    title = strip_html(title_match.group(1)) if title_match else ""
    records: List[Dict[str, str]] = []
    for match in re.finditer(r"(?is)<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", page):
        href = html.unescape(match.group(1))
        label = strip_html(match.group(2))
        href_l = href.lower()
        label_n = norm(label)
        if not href:
            continue
        is_pdf = href_l.endswith(".pdf") or "/file/" in href_l or "/sites/default/files/adjuntos/rcp/" in href_l
        is_download = "DESCARGAR" in label_n or "RCP" in label_n or is_pdf
        if not is_download:
            continue
        records.append(
            {
                "product": title or label,
                "context": strip_html(page[max(0, match.start() - 800) : min(len(page), match.end() + 800)]),
                "pdf_url": urllib.parse.urljoin(base_url, href),
                "page_url": base_url,
            }
        )
    return records


def parse_cecmed_records(page: str, base_url: str) -> List[Dict[str, str]]:
    """Parse CECMED table/list pages and detail pages into product/PDF records."""
    detail_records = parse_cecmed_detail_record(page, base_url)
    if detail_records:
        return detail_records

    anchors: List[Dict[str, Any]] = []
    for match in re.finditer(r"(?is)<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", page):
        href = html.unescape(match.group(1))
        label = strip_html(match.group(2))
        if not href or not label:
            continue
        anchors.append({"href": urllib.parse.urljoin(base_url, href), "label": label, "start": match.start(), "end": match.end()})
    records: List[Dict[str, str]] = []
    for idx, anchor in enumerate(anchors):
        product = clean(anchor.get("label"))
        if not product or product.lower().startswith(("descargar", "page", "siguiente", "ultima", "última")):
            continue
        if "/registro/rcp/" not in clean(anchor.get("href", "")):
            continue
        pdf_anchor = None
        for next_anchor in anchors[idx + 1 : idx + 10]:
            next_label = norm(next_anchor.get("label"))
            next_href = clean(next_anchor.get("href", "")).lower()
            if "DESCARGAR" in next_label or next_href.endswith(".pdf") or "/file/" in next_href or "/download" in next_href:
                pdf_anchor = next_anchor
                break
            if next_label.startswith("PAGE") or "SIGUIENTE" in next_label:
                break
        if not pdf_anchor:
            continue
        context = strip_html(page[anchor["end"] : pdf_anchor["start"]])
        records.append(
            {
                "product": product,
                "context": context,
                "pdf_url": clean(pdf_anchor.get("href")),
                "page_url": base_url,
            }
        )
    deduped: List[Dict[str, str]] = []
    seen: set[str] = set()
    for record in records:
        key = sha(record.get("product"), record.get("pdf_url"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped


def cecmed_search_links(page: str, base_url: str) -> List[str]:
    """Extract CECMED RCP product-detail links from /search/node results."""
    links: List[str] = []
    for match in re.finditer(r"(?is)<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", page):
        href = html.unescape(match.group(1))
        if "/registro/rcp/" not in href:
            continue
        url = urllib.parse.urljoin(base_url, href)
        if url not in links:
            links.append(url)
    return links


def cecmed_search(query: str, raw_dir: Path, timeout: int, resume: bool, max_pages: int = 3) -> Tuple[str, str, List[Dict[str, str]]]:
    query = clean(query)
    records: List[Dict[str, str]] = []
    statuses: List[str] = []

    # CECMED has separate Drupal views for ordinary medicines and biologicals.
    # The exposed filter names are taken from the live form markup. The previous
    # adapter used non-existent field names, which caused unfiltered pages.
    bases = [CECMED_RCP, CECMED_BIOLOGICS_RCP]
    filter_keys = [
        "title",
        "field_nombre_del_principio_activ_value",
        "field_fabricante_value",
        "field_titular_del_registro_value",
    ]
    candidate_urls: List[str] = []
    if query:
        candidate_urls.extend(f"{base}?{key}={urllib.parse.quote(query)}" for base in bases for key in filter_keys)
        candidate_urls.append(f"{CECMED_SITE_SEARCH}?keys={urllib.parse.quote(query)}")
        qn = norm(query)
        # Exact Cuban biologic detail pages observed in the remaining queue.
        # These URLs are stable Drupal nodes and are safer than relying only on
        # exposed filters/site-search indexing.
        if "HEBERBIOVAC" in qn:
            candidate_urls.extend(
                [
                    f"{CECMED_BIOLOGICS_RCP}/heberbiovac-hbr-20-vacuna-antihepatitis-b-recombinante",
                    f"{CECMED_BIOLOGICS_RCP}/heberbiovac-hbr-10-vacuna-antihepatitis-b-recombinante",
                ]
            )
        if "EPOCIM" in qn or "ERITROPOYET" in qn or "ERYTHROPOIET" in qn:
            candidate_urls.extend(
                [
                    f"{CECMED_BIOLOGICS_RCP}/iorr-epocim-2-000-eritropoyetina-humana-recombinante-tipo-alfa",
                    f"{CECMED_BIOLOGICS_RCP}/iorr-epocim-4-000-eritropoyetina-humana-recombinante-tipo-alfa",
                ]
            )
    candidate_urls.extend(f"{base}?page={page}" for base in bases for page in range(max_pages))

    seen_urls: set[str] = set()
    for url in candidate_urls:
        if url in seen_urls:
            continue
        seen_urls.add(url)
        raw = cache_path(raw_dir, "cecmed_rcp_page", url, "html")
        status, message, page, _ = fetch_text(url, raw, timeout, resume)
        statuses.append(status)
        if status not in {"ok", "cached"} or not page:
            continue
        page_records = parse_cecmed_records(page, url)

        # Site search results usually point to detail pages; fetch those and
        # parse the RCP PDF link from the detail page.
        if not page_records and "search/node" in url:
            for detail_url in cecmed_search_links(page, url)[: max(max_pages * 6, 12)]:
                raw_detail = cache_path(raw_dir, "cecmed_rcp_detail", detail_url, "html")
                d_status, _d_message, detail_page, _ = fetch_text(detail_url, raw_detail, timeout, resume)
                statuses.append(f"detail:{d_status}")
                if d_status in {"ok", "cached"} and detail_page:
                    page_records.extend(parse_cecmed_records(detail_page, detail_url))

        if query:
            qnorm = norm(query)
            query_tokens = [token for token in qnorm.split() if len(token) >= 4]
            def looks_relevant(record: Dict[str, str]) -> bool:
                combined = norm(record.get("product") + " " + record.get("context") + " " + record.get("pdf_url"))
                if qnorm and qnorm in combined:
                    return True
                return any(token in combined for token in query_tokens[:4])
            page_records = [record for record in page_records if looks_relevant(record)]

        records.extend(page_records)
        if records and ("search/node" in url or "?page=" not in url):
            break
    deduped: List[Dict[str, str]] = []
    seen: set[str] = set()
    for record in records:
        key = sha(record.get("product"), record.get("pdf_url"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return ";".join(statuses) or "cecmed_no_query", "", deduped


def score_cecmed_match(row: Dict[str, str], record: Dict[str, str]) -> float:
    query_generic = norm(row.get("query_generic") or row.get("nom_generique"))
    query_brand = norm(row.get("query_brand") or row.get("nom"))
    query_labo = norm(row.get("labo"))
    query_dosage = norm(row.get("dosage"))
    product = norm(record.get("product"))
    context = norm(record.get("context"))
    pdf_text = norm(record.get("pdf_url"))
    combined = product + " " + context + " " + pdf_text
    brand_token = first_query_token(query_brand)
    score = 0.0
    if query_generic and query_generic in combined:
        score += 0.55
    if query_brand and query_brand in combined:
        score += 0.52
    elif brand_token and product.startswith(brand_token):
        score += 0.44
    dosage_numbers = re.findall(r"\d+(?:[.,]\d+)?", query_dosage)
    if dosage_numbers and any(number in combined for number in dosage_numbers):
        score += 0.08
    if query_labo and context and any(token in context for token in query_labo.split()[:3] if len(token) >= 4):
        score += 0.05
    return min(score, 0.95)


def cecmed_sections(
    row: Dict[str, str],
    raw_dir: Path,
    timeout: int,
    resume: bool,
    max_docs: int,
    min_match_score: float,
) -> Tuple[str, str, str, float, List[Dict[str, str]]]:
    candidates: Dict[str, Dict[str, str]] = {}
    statuses: List[str] = []
    for query in cecmed_query_candidates(row)[:6]:
        status, _message, items = cecmed_search(query, raw_dir, timeout, resume)
        statuses.append(f"search:{status}:{len(items)}")
        for item in items:
            key = sha(item.get("product"), item.get("pdf_url"))[:16]
            candidates[key] = item
    scored = sorted(
        ((score_cecmed_match(row, record), key, record) for key, record in candidates.items()),
        key=lambda item: item[0],
        reverse=True,
    )
    for score, key, record in scored[:max_docs]:
        if score < min(min_match_score, 0.40):
            continue
        pdf_url = clean(record.get("pdf_url"))
        if not pdf_url:
            continue
        raw_pdf = cache_path(raw_dir, "cecmed_rcp_pdf", pdf_url, "pdf")
        status, _message, body, _ = fetch_binary(pdf_url, raw_pdf, timeout, resume)
        statuses.append(f"pdf:{status}")
        if status not in {"ok", "cached"} or not body:
            continue
        text = _extract_pdf_text(body)
        sections = extract_spanish_sections(text)
        if sections:
            return "ok_rcp", key, pdf_url, score, sections
    return ";".join(statuses) or "cecmed_no_query", "", "", 0.0, []


def euuk_sections(
    source: str,
    query: str,
    raw_dir: Path,
    timeout: int,
    resume: bool,
    max_docs: int,
) -> Tuple[str, str, List[Dict[str, str]], str]:
    if euuk is None:
        return "euuk_helper_unavailable", "", [], ""
    if source == "ema":
        status, record, sections = euuk.ema_sections(query, raw_dir, timeout, resume, max_docs)
        return status, record, sections, "ema_medicine_finder"
    if source == "mhra":
        status, record, sections = euuk.mhra_sections(query, raw_dir, timeout, resume, max_docs)
        return status, record, sections, "mhra_products_smpc"
    if source == "emc":
        status, record, sections = euuk.emc_sections(query, raw_dir, timeout, resume, max_docs)
        return status, record, sections, "emc_smpc_html"
    return "unknown_euuk_source", "", [], ""


def registered_status_only(source: str) -> Tuple[str, str, str, float, List[Dict[str, str]]]:
    info = REGISTERED_SOURCE_INFO.get(source, {})
    return clean(info.get("status") or "registered_no_live_adapter"), clean(info.get("url")), clean(info.get("url")), 0.0, []


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


def apply_sections(
    sections: List[Dict[str, str]],
    row: Dict[str, str],
    source_system: str,
    record_id: str,
    source_file: str,
    query: str,
    match_score: float,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()
    confidence_by_source = {
        "aemps_cima_ficha_tecnica": "0.72",
        "aemps_cima_prospecto": "0.68",
        "ema_medicine_finder": "0.70",
        "mhra_products_smpc": "0.66",
        "emc_smpc_html": "0.62",
        "aifa_rcp_pdf": "0.72",
        "aifa_foglio_illustrativo_pdf": "0.66",
        "aifa_metadata": "0.58",
        "titck_kub_pdf": "0.70",
        "titck_kt_pdf": "0.64",
        "titck_metadata": "0.56",
        "health_canada_dpd": "0.58",
        "cecmed_rcp_pdf": "0.70",
    }
    rank_by_source = {
        "aemps_cima_ficha_tecnica": "74",
        "aemps_cima_prospecto": "70",
        "ema_medicine_finder": "72",
        "mhra_products_smpc": "69",
        "emc_smpc_html": "63",
        "aifa_rcp_pdf": "73",
        "aifa_foglio_illustrativo_pdf": "67",
        "aifa_metadata": "57",
        "titck_kub_pdf": "71",
        "titck_kt_pdf": "65",
        "titck_metadata": "56",
        "health_canada_dpd": "58",
        "cecmed_rcp_pdf": "71",
    }
    language_by_source = {
        "aemps_cima_ficha_tecnica": "es",
        "aemps_cima_prospecto": "es",
        "ema_medicine_finder": "en",
        "mhra_products_smpc": "en",
        "emc_smpc_html": "en",
        "aifa_rcp_pdf": "it",
        "aifa_foglio_illustrativo_pdf": "it",
        "aifa_metadata": "it",
        "titck_kub_pdf": "tr",
        "titck_kt_pdf": "tr",
        "titck_metadata": "tr",
        "health_canada_dpd": "en",
        "cecmed_rcp_pdf": "es",
    }
    for section in sections:
        section_row = {
            "row_id": row.get("row_id", ""),
            "amm": row.get("amm", ""),
            "nom": row.get("nom", ""),
            "nom_generique": row.get("nom_generique", ""),
            "source_system": source_system,
            "source_file": source_file,
            "source_record_id": record_id,
            "match_query": query,
            "match_score": f"{match_score:.2f}",
            "section_kind": section.get("section_kind", ""),
            "section_title": section.get("section_title", ""),
            "section_text": section.get("section_text", ""),
            "language": language_by_source.get(source_system, ""),
            "authority_level": f"fallback_{source_system}",
            "confidence": f"{max(float(confidence_by_source.get(source_system, '0.55')), min(match_score, 0.95)):.2f}",
            "evidence_rank": rank_by_source.get(source_system, "60"),
            "retrieved_at": now,
        }
        section_row["content_hash"] = sha(section_row["row_id"], source_system, section_row["section_kind"], section_row["section_text"][:500])
        out.append(section_row)
    return out


def section_dedupe_key(row: Dict[str, Any]) -> str:
    content_hash = clean(row.get("content_hash", ""))
    if content_hash:
        return content_hash
    return sha(
        row.get("row_id", ""),
        row.get("source_system", ""),
        row.get("section_kind", ""),
        clean(row.get("section_text", ""))[:500],
    )


def merge_existing_section_rows(output_path: Path, new_rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    existing_rows = read_csv(output_path)
    if not existing_rows:
        return new_rows, 0
    merged: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for row in existing_rows + new_rows:
        key = section_dedupe_key(row)
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return merged, len(existing_rows)


def source_row_counts_from_sections(rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    row_ids_by_source: Dict[str, set[str]] = {}
    for row in rows:
        source = clean(row.get("source_system", ""))
        row_id = clean(row.get("row_id", ""))
        if source and row_id:
            row_ids_by_source.setdefault(source, set()).add(row_id)
    return {source: len(row_ids) for source, row_ids in sorted(row_ids_by_source.items())}


def source_system_for_cima(record_url: str) -> str:
    return "aemps_cima_prospecto" if "/dochtml/p/" in record_url.lower() else "aemps_cima_ficha_tecnica"


def source_system_for_registry(source: str, status: str, record_url: str = "") -> str:
    if source == "cima":
        return source_system_for_cima(record_url)
    if source == "aifa":
        status_lower = status.lower()
        if "ok_fi" in status_lower or "foglio" in record_url.lower():
            return "aifa_foglio_illustrativo_pdf"
        if "ok_rcp" in status_lower or "ts=RCP" in record_url:
            return "aifa_rcp_pdf"
        return "aifa_metadata"
    if source == "titck":
        status_lower = status.lower()
        if not status_lower.startswith("ok_"):
            return "titck_metadata"
        if "ok_kt" in status_lower or "documentpathkt" in record_url.lower():
            return "titck_kt_pdf"
        if "ok_kub" in status_lower or record_url.lower().endswith(".pdf"):
            return "titck_kub_pdf"
        return "titck_metadata"
    if source == "canada":
        return "health_canada_dpd"
    if source == "cecmed":
        return "cecmed_rcp_pdf"
    if source == "ema":
        return "ema_medicine_finder"
    if source == "mhra":
        return "mhra_products_smpc"
    if source == "emc":
        return "emc_smpc_html"
    return source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--missing", default="dpm_live_out/treated_medicines_missing_all_local_evidence.csv")
    parser.add_argument("--details", default="dpm_live_out/remaining_382_medicines_all_available_details.csv")
    parser.add_argument("--bdpm-queue", default="dpm_live_out/bdpm_live_query_queue.csv")
    parser.add_argument("--raw-dir", default="dpm_live_out/api_cache/global_regulatory")
    parser.add_argument("--output", default="dpm_live_out/global_regulatory_fallback_sections.csv")
    parser.add_argument("--query-status-output", default="dpm_live_out/global_regulatory_fallback_query_status.csv")
    parser.add_argument("--summary", default="dpm_live_out/global_regulatory_fallback_summary.json")
    parser.add_argument("--sources", default=DEFAULT_SOURCES)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.35)
    parser.add_argument("--timeout", type=int, default=35)
    parser.add_argument("--max-docs", type=int, default=2)
    parser.add_argument("--min-match-score", type=float, default=0.50)
    parser.add_argument("--resume", action="store_true", default=True)
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument("--disable-country-routing", action="store_true", help="Try every configured source for every row instead of routing by country/source type.")
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

    log(f"Starting global regulatory fallback: {len(queue_rows)} rows, sources={sources}, sleep={args.sleep}s")
    for idx, row in enumerate(queue_rows, start=1):
        chosen_source = ""
        chosen_status = ""
        chosen_record = ""
        chosen_query = ""
        chosen_record_url = ""
        chosen_score = 0.0
        chosen_sections: List[Dict[str, str]] = []
        source_attempts: List[str] = []
        for source in sources:
            if not args.disable_country_routing and not source_relevant(row, source):
                continue
            if source == "cima":
                status, record, record_url, score, sections = cima_sections(
                    row,
                    raw_dir,
                    args.timeout,
                    args.resume,
                    args.max_docs,
                    args.min_match_score,
                )
                source_system = source_system_for_registry(source, status, record_url)
                query = row.get("query_generic") or row.get("nom_generique") or row.get("query_primary") or row.get("nom")
            elif source in {"ema", "mhra", "emc"}:
                status = "no_query_attempted"
                record = ""
                record_url = ""
                score = 0.62
                sections = []
                source_system = source_system_for_registry(source, status)
                query = ""
                per_query_statuses: List[str] = []
                for query_candidate in query_candidates(row):
                    q_status, q_record, q_sections, q_source_system = euuk_sections(source, query_candidate, raw_dir, args.timeout, args.resume, args.max_docs)
                    per_query_statuses.append(f"{query_candidate}:{q_status}:{len(q_sections)}")
                    status_counts[f"{source}_{q_status.split(';')[0].split(',')[0]}"] += 1
                    if q_sections:
                        status = q_status
                        record = q_record
                        record_url = q_record
                        sections = q_sections
                        source_system = q_source_system or source_system
                        query = query_candidate
                        break
                if not sections:
                    status = ";".join(per_query_statuses) or "no_query_attempted"
            elif source == "aifa":
                status, record, record_url, score, sections = aifa_sections(
                    row,
                    raw_dir,
                    args.timeout,
                    args.resume,
                    args.max_docs,
                    args.min_match_score,
                )
                source_system = source_system_for_registry(source, status, record_url)
                query = row.get("query_brand") or row.get("nom") or row.get("query_generic") or row.get("nom_generique")
            elif source == "titck":
                status, record, record_url, score, sections = titck_sections(
                    row,
                    raw_dir,
                    args.timeout,
                    args.resume,
                    args.max_docs,
                    args.min_match_score,
                )
                source_system = source_system_for_registry(source, status, record_url)
                query = row.get("query_brand") or row.get("nom") or row.get("query_generic") or row.get("nom_generique")
            elif source == "canada":
                status, record, record_url, score, sections = health_canada_sections(
                    row,
                    raw_dir,
                    args.timeout,
                    args.resume,
                    args.max_docs,
                    args.min_match_score,
                )
                source_system = source_system_for_registry(source, status, record_url)
                query = row.get("query_brand") or row.get("nom") or row.get("query_generic") or row.get("nom_generique")
            elif source == "cecmed":
                status, record, record_url, score, sections = cecmed_sections(
                    row,
                    raw_dir,
                    args.timeout,
                    args.resume,
                    args.max_docs,
                    args.min_match_score,
                )
                source_system = source_system_for_registry(source, status, record_url)
                query = row.get("query_brand") or row.get("nom") or row.get("query_generic") or row.get("nom_generique")
            elif source in REGISTERED_SOURCE_INFO:
                status, record, record_url, score, sections = registered_status_only(source)
                source_system = source_system_for_registry(source, status, record_url)
                query = row.get("query_primary") or row.get("query_generic") or row.get("nom")
            else:
                status, record, record_url, score, sections = "unknown_source", "", "", 0.0, []
                source_system = source
                query = ""
            source_attempts.append(f"{source}:{status.split(';')[0]}:{len(sections)}")
            if source not in {"ema", "mhra", "emc"}:
                status_counts[f"{source}_{status.split(';')[0]}"] += 1
            if sections:
                chosen_source = source_system
                chosen_status = status
                chosen_record = record
                chosen_record_url = record_url
                chosen_score = score
                chosen_query = clean(query)
                chosen_sections = sections
                break
        if not chosen_status:
            chosen_status = "no_sections_from_relevant_sources" if source_attempts else "no_relevant_source_after_country_routing"
        if chosen_sections:
            source_counts[chosen_source] += 1
            section_rows.extend(
                apply_sections(
                    chosen_sections,
                    row,
                    chosen_source,
                    chosen_record,
                    chosen_record_url,
                    chosen_query,
                    chosen_score,
                )
            )
        status_rows.append(
            {
                "row_id": row.get("row_id", ""),
                "amm": row.get("amm", ""),
                "nom": row.get("nom", ""),
                "nom_generique": row.get("nom_generique", ""),
                "pays": row.get("pays", ""),
                "chosen_source": chosen_source,
                "chosen_query": chosen_query,
                "chosen_record": chosen_record,
                "match_score": f"{chosen_score:.2f}" if chosen_score else "",
                "sections": len(chosen_sections),
                "status": chosen_status,
                "source_attempts": " | ".join(source_attempts),
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
        if idx < len(queue_rows) and args.sleep > 0:
            time.sleep(args.sleep)

    output_path = Path(args.output)
    section_rows, existing_section_rows_merged = merge_existing_section_rows(output_path, section_rows)
    final_source_section_counts = Counter(row.get("source_system", "") for row in section_rows if clean(row.get("source_system", "")))
    final_source_row_counts = source_row_counts_from_sections(section_rows)

    write_csv(output_path, OUTPUT_FIELDS, section_rows)
    write_csv(
        Path(args.query_status_output),
        ["row_id", "amm", "nom", "nom_generique", "pays", "chosen_source", "chosen_query", "chosen_record", "match_score", "sections", "status", "source_attempts"],
        status_rows,
    )
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "queue_rows_processed": len(queue_rows),
        "section_rows": len(section_rows),
        "row_ids_with_global_regulatory_sections": len({row["row_id"] for row in section_rows}),
        "source_row_counts": final_source_row_counts,
        "source_section_counts": dict(final_source_section_counts),
        "run_source_row_counts": dict(source_counts),
        "existing_section_rows_merged": existing_section_rows_merged,
        "status_counts": dict(status_counts),
        "configured_sources": sources,
        "registered_source_info": {source: REGISTERED_SOURCE_INFO.get(source, {"status": "unknown_source"}) for source in sources},
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
