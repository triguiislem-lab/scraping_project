#!/usr/bin/env python3
"""
Build a guaranteed/official-source recovery plan for medicines still missing RCP/notice-style details.

What this script does:
  1. Reads the remaining-medicines CSV exported by your remediation pipeline.
  2. Optionally removes row_ids already recovered by an extra sections CSV, e.g. CECMED v3.
  3. Classifies each medicine by country and product type.
  4. Emits ranked official-source candidates that are realistic for RCP/SmPC/PIL/notice recovery.
  5. Optionally runs lightweight live HTTP checks for URLs when internet is available.

It does NOT pretend dynamic/captcha/WebForms portals are machine-readable. Those are marked as
"official_source_manual_or_adapter_needed" and the next action explains what to implement.

Example:
  python guaranteed_source_hunter.py \
    --remaining remaining_382_medicines_all_available_details.csv \
    --covered-sections global_regulatory_fallback_sections_cecmed_v3.csv \
    --output-plan dpm_live_out/guaranteed_source_plan.csv \
    --output-medicine-summary dpm_live_out/guaranteed_source_medicine_summary.csv \
    --output-summary dpm_live_out/guaranteed_source_summary.json \
    --examples "HEBERBIOVAC HB,GC FLU,COMINARTY,ZOLEDRONIC ACID HIKMA,ABUFENE,ALBUMINATIV"
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

OUTPUT_FIELDS = [
    "row_id",
    "amm",
    "nom",
    "nom_generique",
    "dosage",
    "forme",
    "labo",
    "pays",
    "already_covered_by_extra_sections",
    "product_type",
    "priority_rank",
    "source_name",
    "source_country_or_scope",
    "source_kind",
    "evidence_level",
    "guarantee_level",
    "expected_document",
    "adapter_status",
    "base_url",
    "query_url",
    "match_query",
    "reason",
    "next_action",
    "live_http_status",
    "live_content_type",
    "live_note",
]

SUMMARY_FIELDS = [
    "row_id",
    "amm",
    "nom",
    "nom_generique",
    "dosage",
    "forme",
    "labo",
    "pays",
    "already_covered_by_extra_sections",
    "product_type",
    "top_source_name",
    "top_source_kind",
    "top_evidence_level",
    "top_guarantee_level",
    "top_query_url",
    "top_next_action",
    "source_count",
]

# Source kinds:
# - official_exact_tunisia: exact Tunisian AMM/RCP/notice source if public or requested from MAH/DPM.
# - official_foreign_exact_if_matched: official foreign RCP/SmPC/PIL source; only exact if same product/presentation/MAH.
# - official_global_product_info: authoritative global vaccine/product info; fallback, not Tunisia-exact.
# - official_manual_request: guaranteed only when regulator/MAH sends the approved latest documents.
# - manufacturer_ifu: device/borderline product information, not standard RCP.

SOURCE_DEFS: Dict[str, Dict[str, str]] = {
    "tunisia_dpm_public": {
        "source_name": "Tunisia DPM public AMM page",
        "scope": "Tunisia",
        "kind": "official_exact_tunisia",
        "evidence": "A_if_rcp_notice_pdf_present_else_metadata_only",
        "guarantee": "exact_tunisia_when_rcp_notice_is_public",
        "expected": "Tunisian RCP and/or notice PDF when published; otherwise AMM metadata",
        "adapter": "existing_public_metadata_plus_pdf_url_check",
        "base": "https://dpm.tn/medicament/humain/liste-des-medicaments",
        "query_template": "https://dpm.tn/medicament/humain/liste-des-medicaments?search={q}",
        "next": "Use DPM detail URL and existing catalog_rcp_url/catalog_notice_url; if absent, trigger MAH/DPM document request.",
    },
    "tunisia_mah_request": {
        "source_name": "Tunisian MAH / DPM approved dossier request",
        "scope": "Tunisia",
        "kind": "official_manual_request",
        "evidence": "A",
        "guarantee": "guaranteed_exact_if_document_obtained",
        "expected": "Latest approved Tunisian RCP and notice annexed to the AMM",
        "adapter": "manual_request_queue",
        "base": "https://dpm.tn/",
        "query_template": "mailto_or_manual_request",
        "next": "Send AMM-specific request to MAH/laboratory or DPM/ANMPS for latest approved RCP + notice.",
    },
    "france_bdpm_ansm": {
        "source_name": "France BDPM / ANSM / eCodex",
        "scope": "France",
        "kind": "official_foreign_exact_if_matched",
        "evidence": "B_exact_product_else_C_same_dci",
        "guarantee": "official_french_rcp_notice_if_product_found",
        "expected": "RCP, notice, composition, presentations, conditions de prescription/delivrance",
        "adapter": "bdpm_ecodex_adapter_or_manual_search",
        "base": "https://base-donnees-publique.medicaments.gouv.fr/",
        "query_template": "https://base-donnees-publique.medicaments.gouv.fr/recherche?query={q}",
        "next": "Search by brand first, then DCI; if no public RCP/notice, request MAH document.",
    },
    "spain_cima": {
        "source_name": "Spain AEMPS CIMA",
        "scope": "Spain",
        "kind": "official_foreign_exact_if_matched",
        "evidence": "B_exact_product_else_C_same_dci",
        "guarantee": "official_spanish_ficha_tecnica_prospecto_if_product_found",
        "expected": "Ficha tecnica / prospecto with sections 4.1-4.9, 5.1-5.2, storage",
        "adapter": "machine_adapter_available",
        "base": "https://cima.aemps.es/cima/rest/medicamentos",
        "query_template": "https://cima.aemps.es/cima/rest/medicamentos?nombre={q}",
        "next": "Use CIMA REST by brand/DCI, then dochtml ft/p document extraction.",
    },
    "italy_aifa": {
        "source_name": "Italy AIFA Medicinali",
        "scope": "Italy",
        "kind": "official_foreign_exact_if_matched",
        "evidence": "B_exact_product_else_C_same_dci",
        "guarantee": "official_italian_rcp_fi_if_product_found",
        "expected": "RCP PDF and Foglio Illustrativo PDF",
        "adapter": "machine_adapter_available",
        "base": "https://medicinali.aifa.gov.it/",
        "query_template": "https://medicinali.aifa.gov.it/it/#/it/risultati?query={q}",
        "next": "Use AIFA query, resolve codiceSis/aic6, download ts=RCP and ts=FI PDFs.",
    },
    "germany_pharmnet": {
        "source_name": "Germany PharmNet.Bund / BfArM",
        "scope": "Germany",
        "kind": "official_foreign_exact_if_matched",
        "evidence": "B_exact_product_else_C_same_dci",
        "guarantee": "official_german_smpc_leaflet_if_product_found",
        "expected": "Fachinformation / Gebrauchsinformation",
        "adapter": "dynamic_portal_adapter_needed",
        "base": "https://www.pharmnet-bund.de/",
        "query_template": "https://www.pharmnet-bund.de/dynamic/de/arzneimittel-informationssystem/index.html?query={q}",
        "next": "Resolve product in PharmNet/BfArM; if dynamic portal blocks automation, add manual download queue.",
    },
    "swissmedic_aips": {
        "source_name": "Swissmedic AIPS XML",
        "scope": "Switzerland",
        "kind": "official_foreign_exact_if_matched",
        "evidence": "B_exact_product_else_C_same_dci",
        "guarantee": "official_swiss_professional_patient_info_if_product_found",
        "expected": "Fachinformation / Information professionnelle and patient information in XML/HTML/PDF",
        "adapter": "bulk_xml_import_recommended",
        "base": "https://download.swissmedicinfo.ch/",
        "query_template": "https://www.swissmedicinfo.ch/?Lang=EN&searchText={q}",
        "next": "Download AIPS XML bundle, index by product/active substance, extract professional/patient info.",
    },
    "austria_basg": {
        "source_name": "Austria BASG Arzneispezialitaetenregister",
        "scope": "Austria",
        "kind": "official_foreign_exact_if_matched",
        "evidence": "B_exact_product_else_C_same_dci",
        "guarantee": "official_austrian_smpc_leaflet_if_product_found",
        "expected": "Fachinformation, Gebrauchsinformation, assessment report",
        "adapter": "dynamic_api_adapter_needed",
        "base": "https://medikamente.basg.gv.at/",
        "query_template": "https://medikamente.basg.gv.at/medikamente/?q={q}",
        "next": "Resolve BASG product and download current Fachinformation/Gebrauchsinformation.",
    },
    "belgium_famhp": {
        "source_name": "Belgium FAMHP medicines database",
        "scope": "Belgium",
        "kind": "official_foreign_exact_if_matched",
        "evidence": "B_exact_product_else_C_same_dci",
        "guarantee": "official_belgian_spc_leaflet_if_product_found",
        "expected": "SPC/RCP and patient leaflet",
        "adapter": "dynamic_api_or_manual_adapter_needed",
        "base": "https://medicinesdatabase.be/",
        "query_template": "https://medicinesdatabase.be/human-use?search={q}",
        "next": "Search FAMHP database by brand/DCI, download SPC/leaflet links.",
    },
    "denmark_dkma": {
        "source_name": "Denmark DKMA product summary / leaflet",
        "scope": "Denmark",
        "kind": "official_foreign_exact_if_matched",
        "evidence": "B_exact_product_else_C_same_dci",
        "guarantee": "official_danish_smpc_leaflet_if_product_found",
        "expected": "Produktresume and indlaegsseddel",
        "adapter": "public_portal_adapter_needed",
        "base": "https://produktresume.dk/",
        "query_template": "https://produktresume.dk/AppBuilder/search?query={q}",
        "next": "Search produktresume.dk for SmPC and xnet.dkma.dk for leaflet.",
    },
    "portugal_infarmed": {
        "source_name": "Portugal Infarmed / Infomed",
        "scope": "Portugal",
        "kind": "official_foreign_exact_if_matched",
        "evidence": "B_exact_product_else_C_same_dci",
        "guarantee": "official_portuguese_rcm_fi_if_product_found",
        "expected": "RCM and Folheto Informativo",
        "adapter": "dynamic_download_adapter_needed",
        "base": "https://www.infarmed.pt/web/infarmed/servicos-on-line/pesquisa-do-medicamento",
        "query_template": "https://www.infarmed.pt/web/infarmed/servicos-on-line/pesquisa-do-medicamento?query={q}",
        "next": "Resolve med_guid in Infomed, download tipo_doc=RCM and tipo_doc=FI.",
    },
    "uk_mhra_emc": {
        "source_name": "UK MHRA Products / emc Datapharm",
        "scope": "United Kingdom",
        "kind": "official_foreign_exact_if_matched",
        "evidence": "B_exact_product_else_C_same_dci",
        "guarantee": "official_uk_smpc_pil_if_product_found",
        "expected": "SmPC and PIL",
        "adapter": "machine_adapter_available_for_emc_partial_mhra_dynamic",
        "base": "https://products.mhra.gov.uk/",
        "query_template": "https://products.mhra.gov.uk/search/?search={q}",
        "next": "Use MHRA Products and emc Documents API; validate product/MAH/strength/form.",
    },
    "canada_dpd": {
        "source_name": "Health Canada Drug Product Database",
        "scope": "Canada",
        "kind": "official_foreign_exact_if_matched",
        "evidence": "B_exact_product_metadata_else_C_same_dci",
        "guarantee": "official_canadian_dpd_metadata_if_product_found",
        "expected": "DPD product identity, active ingredients, dosage form, route, schedule, status, therapeutic class",
        "adapter": "machine_adapter_metadata_available",
        "base": "https://health-products.canada.ca/api/drug/",
        "query_template": "https://health-products.canada.ca/api/drug/drugproduct/?lang=en&type=json&brandname={q}",
        "next": "Use Health Canada DPD brand search, then per-drug activeingredient/form/route/status/class endpoints.",
    },
    "turkey_titck": {
        "source_name": "Turkey TITCK KUB/KT",
        "scope": "Turkey",
        "kind": "official_foreign_exact_if_matched",
        "evidence": "B_exact_product_else_C_same_dci",
        "guarantee": "official_turkish_kub_kt_if_product_found",
        "expected": "KUB short product information and KT patient leaflet PDFs",
        "adapter": "machine_adapter_available",
        "base": "https://www.titck.gov.tr/kubkt",
        "query_template": "https://www.titck.gov.tr/kubkt",
        "next": "Use TITCK KUB/KT DataTables adapter by brand/DCI; download KUB first, then KT if KUB is unavailable.",
    },
    "jordan_jfda": {
        "source_name": "Jordan JFDA eLeaflet",
        "scope": "Jordan",
        "kind": "official_foreign_exact_if_matched",
        "evidence": "B_exact_product_else_C_same_dci",
        "guarantee": "official_jordan_leaflet_if_product_found",
        "expected": "Official leaflet/eLeaflet; often PIL/SPC-like details",
        "adapter": "aspnet_webforms_adapter_needed",
        "base": "https://services.jfda.jo/JFDA/registration/leafletdrugssearch.aspx",
        "query_template": "https://services.jfda.jo/JFDA/registration/leafletdrugssearch.aspx",
        "next": "Implement ASP.NET WebForms postback search by drug name/active substance/manufacturer; download leaflet link.",
    },
    "saudi_sfda_sdi": {
        "source_name": "Saudi SFDA SDI",
        "scope": "Saudi Arabia / Gulf fallback",
        "kind": "official_foreign_exact_if_matched",
        "evidence": "B_exact_product_else_C_same_dci",
        "guarantee": "official_saudi_spc_pil_if_product_found",
        "expected": "PIL and SPC uploaded by company/agent",
        "adapter": "session_portal_adapter_needed",
        "base": "https://sdi.sfda.gov.sa/",
        "query_template": "https://sdi.sfda.gov.sa/",
        "next": "Search SDI by brand/manufacturer; capture SPC/PIL document URLs manually or via session adapter.",
    },
    "uae_ede_mohap": {
        "source_name": "UAE EDE / MOHAP drug directory",
        "scope": "United Arab Emirates",
        "kind": "official_foreign_exact_if_matched",
        "evidence": "B_exact_product_else_C_same_dci",
        "guarantee": "official_uae_registered_product_if_found; document availability varies",
        "expected": "Registered product details and sometimes leaflet/documents",
        "adapter": "dynamic_or_captcha_manual_adapter_needed",
        "base": "https://www.ede.gov.ae/en/drug-directory",
        "query_template": "https://www.ede.gov.ae/en/drug-directory?search={q}",
        "next": "Search EDE/MOHAP by brand/manufacturer; request MAH leaflet if documents are not public.",
    },
    "korea_mfds_nedrug": {
        "source_name": "Korea MFDS / NEDRUG",
        "scope": "South Korea",
        "kind": "official_foreign_exact_if_matched",
        "evidence": "B_exact_product_else_C_same_dci",
        "guarantee": "official_korean_label_sections_if_itemseq_found",
        "expected": "NEDRUG EE/NB/UD sections: efficacy, precautions, dosage/use",
        "adapter": "itemseq_resolver_needed_then_xml_html_adapter",
        "base": "https://nedrug.mfds.go.kr/eng/index",
        "query_template": "https://nedrug.mfds.go.kr/searchDrug?sort=&page=1&searchYn=true&itemName={q}",
        "next": "Resolve itemSeq by brand; fetch /pbp/cmn/html/drb/{itemSeq}/EE, NB, UD or XML equivalents.",
    },
    "sweden_fass": {
        "source_name": "Sweden FASS product information",
        "scope": "Sweden",
        "kind": "official_foreign_exact_if_matched",
        "evidence": "B_exact_product_else_C_same_dci",
        "guarantee": "official_swedish_product_information_if_product_found",
        "expected": "FASS product information and patient/professional document references",
        "adapter": "api_access_or_portal_adapter_needed",
        "base": "https://www.fass.se/",
        "query_template": "https://www.fass.se/LIF/result?query={q}",
        "next": "Resolve product in FASS by brand/DCI; use API if access is available, otherwise add manual document queue.",
    },
    "who_vaccines": {
        "source_name": "WHO Prequalified/EUL vaccine documents",
        "scope": "WHO global vaccine product info",
        "kind": "official_global_product_info",
        "evidence": "B_for_WHO_EUL_or_PQ_product_info_C_if_not_exact_presentation",
        "guarantee": "official_global_product_info_if_vaccine_document_found",
        "expected": "WHO product information, package leaflet, product characteristics, recommendation/EUL/PQ documents",
        "adapter": "documents_index_parser_recommended",
        "base": "https://extranet.who.int/prequal/vaccines/prequalified-vaccines",
        "query_template": "https://extranet.who.int/prequal/key-resources/documents/vaccines/w?title={q}",
        "next": "Search WHO vaccine document index/product page; extract product information/package leaflet PDF sections.",
    },
    "ema": {
        "source_name": "EMA EPAR / medicine finder",
        "scope": "EU centrally authorised",
        "kind": "official_foreign_exact_if_matched",
        "evidence": "B_exact_product_else_C_same_dci",
        "guarantee": "official_ema_smpc_leaflet_if_centrally_authorised_product_found",
        "expected": "EU product information: SmPC/RCP, labelling, package leaflet",
        "adapter": "machine_adapter_available_or_manual_finder",
        "base": "https://www.ema.europa.eu/en/medicines",
        "query_template": "https://www.ema.europa.eu/en/medicines?search_api_fulltext={q}",
        "next": "Use EMA finder for centrally authorised products, especially vaccines/biologics/rare disease products.",
    },
    "cecmed_cuba": {
        "source_name": "Cuba CECMED RCP medicines/biologics",
        "scope": "Cuba",
        "kind": "official_foreign_exact_if_matched",
        "evidence": "B_exact_product",
        "guarantee": "official_cuban_rcp_if_product_found",
        "expected": "RCP PDF for medicines/biologics",
        "adapter": "machine_adapter_available_v3",
        "base": "https://www.cecmed.cu/registro/rcp/biologicos",
        "query_template": "https://www.cecmed.cu/registro/rcp/biologicos?title={q}",
        "next": "Use existing CECMED v3 adapter; already recovered Heberbiovac/Ior Epocim rows.",
    },
    "manufacturer_ifu": {
        "source_name": "Manufacturer IFU / technical leaflet",
        "scope": "Manufacturer / device or borderline product",
        "kind": "manufacturer_ifu",
        "evidence": "B_for_device_IFU_not_RCP",
        "guarantee": "best_available_for_device_or_borderline_product_if_official_manufacturer_document",
        "expected": "Instructions for Use, device leaflet, product monograph, technical notice",
        "adapter": "manual_or_manufacturer_site_scrape",
        "base": "manufacturer_site_or_contact",
        "query_template": "manufacturer_site_or_contact",
        "next": "Do not expect standard RCP; request IFU/technical leaflet from manufacturer/MAH and mark product as device/borderline.",
    },
}

COUNTRY_TO_SOURCES: Dict[str, List[str]] = {
    "FRANCE": ["france_bdpm_ansm", "ema", "tunisia_mah_request"],
    "TUNISIE": ["tunisia_dpm_public", "tunisia_mah_request"],
    "ITALIE": ["italy_aifa", "ema", "tunisia_mah_request"],
    "ITALY": ["italy_aifa", "ema", "tunisia_mah_request"],
    "ALLEMAGNE": ["germany_pharmnet", "ema", "tunisia_mah_request"],
    "GERMANY": ["germany_pharmnet", "ema", "tunisia_mah_request"],
    "SUISSE": ["swissmedic_aips", "ema", "tunisia_mah_request"],
    "SWITZERLAND": ["swissmedic_aips", "ema", "tunisia_mah_request"],
    "AUTRICHE": ["austria_basg", "ema", "tunisia_mah_request"],
    "AUSTRIA": ["austria_basg", "ema", "tunisia_mah_request"],
    "BELGIQUE": ["belgium_famhp", "ema", "tunisia_mah_request"],
    "BELGIUM": ["belgium_famhp", "ema", "tunisia_mah_request"],
    "DANEMARK": ["denmark_dkma", "ema", "tunisia_mah_request"],
    "DENMARK": ["denmark_dkma", "ema", "tunisia_mah_request"],
    "SUEDE": ["sweden_fass", "denmark_dkma", "ema", "tunisia_mah_request"],
    "SWEDEN": ["sweden_fass", "denmark_dkma", "ema", "tunisia_mah_request"],
    "PORTUGAL": ["portugal_infarmed", "ema", "tunisia_mah_request"],
    "ESPAGNE": ["spain_cima", "ema", "tunisia_mah_request"],
    "SPAIN": ["spain_cima", "ema", "tunisia_mah_request"],
    "RAYAUME UNI": ["uk_mhra_emc", "ema", "tunisia_mah_request"],
    "UNITED KINGDOM": ["uk_mhra_emc", "ema", "tunisia_mah_request"],
    "CANADA": ["canada_dpd", "ema", "tunisia_mah_request"],
    "JORDANIE": ["jordan_jfda", "saudi_sfda_sdi", "tunisia_mah_request"],
    "JORDAN": ["jordan_jfda", "saudi_sfda_sdi", "tunisia_mah_request"],
    "ARABIE SAOUDITE": ["saudi_sfda_sdi", "tunisia_mah_request"],
    "SAUDI": ["saudi_sfda_sdi", "tunisia_mah_request"],
    "EMIRATS ARABES UNIS": ["uae_ede_mohap", "saudi_sfda_sdi", "tunisia_mah_request"],
    "UNITED ARAB EMIRATES": ["uae_ede_mohap", "saudi_sfda_sdi", "tunisia_mah_request"],
    "COREE": ["korea_mfds_nedrug", "who_vaccines", "ema", "tunisia_mah_request"],
    "KOREA": ["korea_mfds_nedrug", "who_vaccines", "ema", "tunisia_mah_request"],
    "CUBA": ["cecmed_cuba", "who_vaccines", "tunisia_mah_request"],
    "TURQUIE": ["turkey_titck", "ema", "tunisia_mah_request"],
    "TURKEY": ["turkey_titck", "ema", "tunisia_mah_request"],
}

VACCINE_TERMS = [
    "VACCIN", "VACCINE", "COMIR", "COMIN", "COVID", "CORONAVAC", "SPUTNIK", "GAM-COVID",
    "HEPAVAX", "HEBERBIOVAC", "EUVAX", "GC FLU", "GCFLU", "PENTAXIM", "VAXIGRIP", "POLIO",
    "ROUGEOLE", "RUB", "DTP", "DTC", "BCG", "AGRIPPAL", "EPAXAL", "nVPO", "NOPV",
]
BIOLOGIC_TERMS = [
    "ALBUMIN", "IMMUNOGLOBUL", "FACTOR", "FACTEUR", "HAEMATE", "HAEMOCTIN", "IMMUNATE", "NOVO EIGHT",
    "ERYTHROPOIET", "EPOCIM", "EPOTIN", "INTERFERON", "LYMPHOGLOBULINE", "RHESONATIV", "FOVEPTA",
]
DEVICE_TERMS = [
    "HANSAPLAST", "STRIPS", "BANDE", "VISCOAT", "HEALON", "PROVISC", "DUOVISC", "AMVISC", "SINOVIAL",
    "SYNOCROM", "SUPLASYN", "EYEFILL", "STRUCTOVIAL", "HYALOSILVER", "VISIODIS", "SCLERA", "UNICROM",
    "GONIOSOL", "LACRINORM", "LACRIFLUID", "DULCIPHAK",
]
DIALYSIS_NUTRITION_TERMS = [
    "DIALYSE", "DPCA", "DP AUTOMATISEE", "OLICLINOMEL", "OLIMEL", "PERIKABIVEN", "INTRALIPIDE", "VITALIPIDE",
    "MEDIALIPIDE", "POLYONIQUE",
]
HOMEOPATHY_TERMS = ["HOMEOGENE", "SEDATIF PC", "ZENALIA", "ACTHEANE", "COCYNTAL", "PARAGRIPPE", "P.H.U", "PRODUITS HOMEOPATHIQUES", "VERRULIA", "AVENOC"]
ALLERGEN_TERMS = ["ALYOSTAL", "ALUSTAL", "DIATER", "PRICK", "ALLERGEN"]
RADIO_TERMS = ["FDG", "18F", "PSMA", "SISORA", "RADIO"]

LAB_HINTS: List[Tuple[str, List[str]]] = [
    ("HIKMA", ["jordan_jfda", "saudi_sfda_sdi", "tunisia_mah_request"]),
    ("JULPHAR", ["uae_ede_mohap", "saudi_sfda_sdi", "tunisia_mah_request"]),
    ("GC", ["korea_mfds_nedrug", "who_vaccines", "tunisia_mah_request"]),
    ("GREEN CROSS", ["korea_mfds_nedrug", "who_vaccines", "tunisia_mah_request"]),
    ("LABESFAL", ["portugal_infarmed", "tunisia_mah_request"]),
    ("OCTAPHARMA", ["swissmedic_aips", "denmark_dkma", "ema", "tunisia_mah_request"]),
    ("BOIRON", ["france_bdpm_ansm", "tunisia_mah_request"]),
    ("LEHNING", ["france_bdpm_ansm", "tunisia_mah_request"]),
    ("STALLERGENES", ["france_bdpm_ansm", "tunisia_mah_request"]),
    ("DIATER", ["spain_cima", "tunisia_mah_request"]),
    ("BAXTER", ["uk_mhra_emc", "italy_aifa", "ema", "tunisia_mah_request"]),
    ("FRESENIUS", ["germany_pharmnet", "italy_aifa", "ema", "tunisia_mah_request"]),
    ("B. BRAUN", ["germany_pharmnet", "ema", "tunisia_mah_request"]),
    ("CSL BEHRING", ["germany_pharmnet", "swissmedic_aips", "ema", "tunisia_mah_request"]),
    ("TAKEDA", ["austria_basg", "swissmedic_aips", "ema", "tunisia_mah_request"]),
]


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\ufeff", "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def norm(value: Any) -> str:
    text = unicodedata.normalize("NFKD", clean(value).upper())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def build_path_candidates(raw_path: str, search_roots: Sequence[Path]) -> List[Path]:
    text = clean(raw_path)
    if not text:
        return []

    path = Path(text)
    if path.is_absolute():
        return [path]

    seen: Set[str] = set()
    out: List[Path] = []
    for root in search_roots:
        for candidate in (root / path, root / "dpm_live_out" / path.name if len(path.parts) == 1 else None):
            if candidate is None:
                continue
            key = str(candidate)
            if key not in seen:
                seen.add(key)
                out.append(candidate)
    return out


def resolve_input_path(raw_path: str, search_roots: Sequence[Path]) -> Path:
    candidates = build_path_candidates(raw_path, search_roots)
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()

    if not candidates:
        raise FileNotFoundError("Input file path is empty")

    shown = candidates[:8]
    lines = "\n".join(f"  - {p}" for p in shown)
    if len(candidates) > len(shown):
        lines += f"\n  - ... ({len(candidates) - len(shown)} more candidate paths)"
    raise FileNotFoundError(f"Input file not found: {raw_path}\nSearched in:\n{lines}")


def resolve_optional_input_path(raw_path: str, search_roots: Sequence[Path]) -> Optional[Path]:
    for candidate in build_path_candidates(raw_path, search_roots):
        if candidate.exists():
            return candidate.resolve()
    return None


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as handle:
        reader = csv.DictReader(handle)
        return [{clean(k): clean(v) for k, v in row.items()} for row in reader]


def write_csv(path: Path, fields: Sequence[str], rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: clean(row.get(field, "")) for field in fields})


def load_covered_row_ids(paths: Sequence[Path]) -> Set[str]:
    covered: Set[str] = set()
    for path in paths:
        if not path or not path.exists():
            continue
        for row in read_csv(path):
            row_id = clean(row.get("row_id"))
            text = clean(row.get("section_text"))
            if row_id and len(text) >= 40:
                covered.add(row_id)
    return covered


def best_query(row: Dict[str, str]) -> str:
    for key in ("query_brand", "nom", "query_primary", "query_generic", "nom_generique"):
        value = clean(row.get(key))
        if value:
            # Remove presentation noise but keep meaningful brand tokens.
            value = re.split(r"\s+B/|\s+FL/|\s+BT/|\s+\d+\s*(?:MG|ML|UI|UG|µG)", value, maxsplit=1, flags=re.I)[0].strip()
            return value or clean(row.get(key))
    return clean(row.get("nom") or row.get("nom_generique") or row.get("amm"))


def classify_product(row: Dict[str, str]) -> str:
    hay = norm(" ".join([row.get("nom", ""), row.get("nom_generique", ""), row.get("forme", ""), row.get("presentation", ""), row.get("labo", "")]))
    if any(term in hay for term in [norm(t) for t in RADIO_TERMS]):
        return "radiopharmaceutical_or_diagnostic"
    if any(term in hay for term in [norm(t) for t in HOMEOPATHY_TERMS]):
        return "homeopathic_or_low_rcp_expected"
    if any(term in hay for term in [norm(t) for t in ALLERGEN_TERMS]):
        return "allergen_extract_specialist"
    if any(term in hay for term in [norm(t) for t in DIALYSIS_NUTRITION_TERMS]):
        return "dialysis_or_parenteral_nutrition"
    if any(term in hay for term in [norm(t) for t in DEVICE_TERMS]):
        return "device_or_borderline_ifu"
    if any(term in hay for term in [norm(t) for t in VACCINE_TERMS]):
        return "vaccine"
    if any(term in hay for term in [norm(t) for t in BIOLOGIC_TERMS]):
        return "biologic_or_blood_product"
    return "standard_medicine"


def country_sources(country: str) -> List[str]:
    cn = norm(country)
    out: List[str] = []
    # Longest keys first prevents "SUEDE" inside other strings being ignored.
    for key in sorted(COUNTRY_TO_SOURCES, key=len, reverse=True):
        if norm(key) in cn:
            out.extend(COUNTRY_TO_SOURCES[key])
            break
    return out


def lab_sources(row: Dict[str, str]) -> List[str]:
    hay = norm(" ".join([row.get("labo", ""), row.get("nom", "")]))
    out: List[str] = []
    for lab, sources in LAB_HINTS:
        if norm(lab) in hay:
            out.extend(sources)
    return out


def product_type_sources(product_type: str) -> List[str]:
    if product_type == "vaccine":
        return ["who_vaccines", "ema", "tunisia_mah_request"]
    if product_type == "biologic_or_blood_product":
        return ["ema", "swissmedic_aips", "germany_pharmnet", "tunisia_mah_request"]
    if product_type in {"device_or_borderline_ifu", "dialysis_or_parenteral_nutrition", "allergen_extract_specialist", "radiopharmaceutical_or_diagnostic"}:
        return ["manufacturer_ifu", "tunisia_mah_request"]
    if product_type == "homeopathic_or_low_rcp_expected":
        return ["france_bdpm_ansm", "manufacturer_ifu", "tunisia_mah_request"]
    return ["tunisia_mah_request"]


def dedupe(items: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for item in items:
        if item in SOURCE_DEFS and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def build_query_url(src: Dict[str, str], query: str) -> str:
    template = src.get("query_template", "")
    if not template:
        return src.get("base", "")
    if "{q}" not in template:
        return template
    return template.replace("{q}", urllib.parse.quote(query))


def source_rows_for_medicine(row: Dict[str, str], covered: Set[str]) -> List[Dict[str, Any]]:
    row_id = clean(row.get("row_id"))
    product_type = classify_product(row)
    q = best_query(row)
    src_keys: List[str] = []

    # Exact Tunisia path is always relevant, but avoid making it the only thing for imported products.
    src_keys.extend(country_sources(row.get("pays") or row.get("catalog_pays") or row.get("list_amm_pays") or ""))
    src_keys.extend(lab_sources(row))
    src_keys.extend(product_type_sources(product_type))

    # Always include the exact Tunisia/manual path at the end if not already present.
    src_keys.append("tunisia_mah_request")
    src_keys = dedupe(src_keys)

    # If nothing matched, still provide exact Tunisia/manual plus DPM public.
    if not src_keys:
        src_keys = ["tunisia_dpm_public", "tunisia_mah_request"]

    rows: List[Dict[str, Any]] = []
    for idx, key in enumerate(src_keys, start=1):
        src = SOURCE_DEFS[key]
        reason_bits = []
        if key in country_sources(row.get("pays", "")):
            reason_bits.append(f"country={row.get('pays', '')}")
        if key in lab_sources(row):
            reason_bits.append(f"lab/brand hint={row.get('labo') or row.get('nom')}")
        if key in product_type_sources(product_type):
            reason_bits.append(f"product_type={product_type}")
        if key == "tunisia_mah_request":
            reason_bits.append("only universal exact source when public RCP/notice is absent")
        if key == "manufacturer_ifu":
            reason_bits.append("standard RCP may not exist; IFU/technical leaflet is expected")
        reason = "; ".join(dict.fromkeys(reason_bits))
        rows.append(
            {
                "row_id": row_id,
                "amm": row.get("amm", ""),
                "nom": row.get("nom", ""),
                "nom_generique": row.get("nom_generique", ""),
                "dosage": row.get("dosage", ""),
                "forme": row.get("forme", ""),
                "labo": row.get("labo", ""),
                "pays": row.get("pays", ""),
                "already_covered_by_extra_sections": "yes" if row_id in covered else "no",
                "product_type": product_type,
                "priority_rank": idx,
                "source_name": src["source_name"],
                "source_country_or_scope": src["scope"],
                "source_kind": src["kind"],
                "evidence_level": src["evidence"],
                "guarantee_level": src["guarantee"],
                "expected_document": src["expected"],
                "adapter_status": src["adapter"],
                "base_url": src["base"],
                "query_url": build_query_url(src, q),
                "match_query": q,
                "reason": reason,
                "next_action": src["next"],
                "live_http_status": "",
                "live_content_type": "",
                "live_note": "",
            }
        )
    return rows


def live_check_url(url: str, timeout: int = 15) -> Tuple[str, str, str]:
    if not url or url in {"mailto_or_manual_request", "manufacturer_site_or_contact"}:
        return "not_checked", "", "no public HTTP URL"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "tunisia-cdss-source-hunter/1.0 (+official document recovery)",
                "Accept": "text/html,application/pdf,application/json,*/*",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            # Read a tiny sample so servers complete the response.
            response.read(256)
            return str(getattr(response, "status", "ok")), clean(response.headers.get("content-type", "")), "reachable"
    except Exception as exc:
        return "error", "", clean(exc)[:300]


def choose_top_summary(plan_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    first = plan_rows[0]
    top = plan_rows[0]
    # Prefer machine adapters and official foreign/exact before manual, unless product is already covered.
    score_map = {
        "machine_adapter_available_v3": 100,
        "machine_adapter_available": 95,
        "machine_adapter_metadata_available": 88,
        "bulk_xml_import_recommended": 85,
        "bdpm_ecodex_adapter_or_manual_search": 82,
        "existing_public_metadata_plus_pdf_url_check": 82,
        "documents_index_parser_recommended": 80,
        "machine_adapter_available_or_manual_finder": 78,
        "itemseq_resolver_needed_then_xml_html_adapter": 75,
        "aspnet_webforms_adapter_needed": 70,
        "dynamic_download_adapter_needed": 65,
        "dynamic_api_adapter_needed": 60,
        "session_portal_adapter_needed": 58,
        "dynamic_portal_adapter_needed": 55,
        "api_access_or_portal_adapter_needed": 55,
        "manual_request_queue": 50,
        "dynamic_or_captcha_manual_adapter_needed": 48,
        "manual_or_manufacturer_site_scrape": 45,
    }
    def score(row: Dict[str, Any]) -> Tuple[int, int, int]:
        adapter = row.get("adapter_status", "")
        priority_rank = int(row.get("priority_rank") or 999)
        adapter_score = score_map.get(adapter, 40)
        # The plan order encodes source relevance (country/lab/product type).
        # Adapter maturity should help, but not routinely leapfrog a more
        # relevant national source.
        priority_bonus = max(0, 100 - 20 * priority_rank)
        return (adapter_score + priority_bonus, adapter_score, -priority_rank)
    top = max(plan_rows, key=score)
    return {
        "row_id": first.get("row_id", ""),
        "amm": first.get("amm", ""),
        "nom": first.get("nom", ""),
        "nom_generique": first.get("nom_generique", ""),
        "dosage": first.get("dosage", ""),
        "forme": first.get("forme", ""),
        "labo": first.get("labo", ""),
        "pays": first.get("pays", ""),
        "already_covered_by_extra_sections": first.get("already_covered_by_extra_sections", ""),
        "product_type": first.get("product_type", ""),
        "top_source_name": top.get("source_name", ""),
        "top_source_kind": top.get("source_kind", ""),
        "top_evidence_level": top.get("evidence_level", ""),
        "top_guarantee_level": top.get("guarantee_level", ""),
        "top_query_url": top.get("query_url", ""),
        "top_next_action": top.get("next_action", ""),
        "source_count": len(plan_rows),
    }


def filter_examples(rows: List[Dict[str, str]], examples: str) -> List[Dict[str, str]]:
    if not examples:
        return rows
    needles = [norm(x) for x in examples.split(",") if norm(x)]
    out = []
    for row in rows:
        hay = norm(" ".join([row.get("row_id", ""), row.get("amm", ""), row.get("nom", ""), row.get("nom_generique", ""), row.get("labo", "")]))
        if any(needle in hay for needle in needles):
            out.append(row)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remaining", default="dpm_live_out/remaining_382_medicines_all_available_details.csv", help="Remaining medicines CSV")
    parser.add_argument("--covered-sections", action="append", default=[], help="Sections CSV(s) already recovered, e.g. CECMED v3. Can be passed multiple times.")
    parser.add_argument("--output-plan", default="dpm_live_out/guaranteed_source_plan.csv", help="Output one row per medicine-source candidate")
    parser.add_argument("--output-medicine-summary", default="dpm_live_out/guaranteed_source_medicine_summary.csv", help="Output one top-source row per medicine")
    parser.add_argument("--output-summary", default="dpm_live_out/guaranteed_source_summary.json", help="Summary JSON")
    parser.add_argument("--examples", default="", help="Comma-separated medicine names/row ids to process for testing")
    parser.add_argument("--limit", type=int, default=0, help="Limit rows after filtering")
    parser.add_argument("--exclude-covered", action="store_true", default=False, help="Exclude row_ids found in --covered-sections from output")
    parser.add_argument("--live-check", action="store_true", default=False, help="Check HTTP reachability of top source URLs. Requires internet.")
    parser.add_argument("--live-timeout", type=int, default=12)
    parser.add_argument("--sleep", type=float, default=0.2)
    args = parser.parse_args()

    search_roots = [Path.cwd(), Path(__file__).resolve().parent]
    remaining_path = resolve_input_path(args.remaining, search_roots)

    covered_paths: List[Path] = []
    missing_covered: List[str] = []
    for raw_path in args.covered_sections:
        resolved = resolve_optional_input_path(raw_path, search_roots)
        if resolved is None:
            missing_covered.append(raw_path)
            covered_paths.append(Path(raw_path))
        else:
            covered_paths.append(resolved)
    if missing_covered:
        missing = ", ".join(missing_covered)
        print(f"Warning: covered-sections path(s) not found and ignored: {missing}", file=sys.stderr)

    rows = read_csv(remaining_path)
    covered = load_covered_row_ids(covered_paths)
    if args.exclude_covered:
        rows = [row for row in rows if clean(row.get("row_id")) not in covered]
    rows = filter_examples(rows, args.examples)
    if args.limit > 0:
        rows = rows[: args.limit]

    plan_rows: List[Dict[str, Any]] = []
    summary_rows: List[Dict[str, Any]] = []
    grouped_sources: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        candidates = source_rows_for_medicine(row, covered)
        if args.live_check and candidates:
            # Only live-check the top two HTTP URLs per medicine to avoid hammering portals.
            for cand in candidates[:2]:
                status, content_type, note = live_check_url(cand.get("query_url", ""), args.live_timeout)
                cand["live_http_status"] = status
                cand["live_content_type"] = content_type
                cand["live_note"] = note
                if args.sleep > 0:
                    time.sleep(args.sleep)
        plan_rows.extend(candidates)
        grouped_sources[clean(row.get("row_id"))] = candidates
        summary_rows.append(choose_top_summary(candidates))

    write_csv(Path(args.output_plan), OUTPUT_FIELDS, plan_rows)
    write_csv(Path(args.output_medicine_summary), SUMMARY_FIELDS, summary_rows)

    source_counter = Counter(row["source_name"] for row in plan_rows)
    top_counter = Counter(row["top_source_name"] for row in summary_rows)
    product_counter = Counter(row["product_type"] for row in summary_rows)
    country_counter = Counter(row.get("pays", "") for row in summary_rows)
    adapter_counter = Counter(row["adapter_status"] for row in plan_rows)
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "remaining_input_rows": len(read_csv(remaining_path)),
        "covered_row_ids_from_extra_sections": len(covered),
        "rows_processed": len(rows),
        "exclude_covered": bool(args.exclude_covered),
        "effective_remaining_after_excluding_covered": len(rows),
        "plan_rows": len(plan_rows),
        "medicine_summary_rows": len(summary_rows),
        "top_source_counts": dict(top_counter.most_common()),
        "all_source_counts": dict(source_counter.most_common()),
        "product_type_counts": dict(product_counter.most_common()),
        "country_counts_top_30": dict(country_counter.most_common(30)),
        "adapter_status_counts": dict(adapter_counter.most_common()),
        "outputs": {
            "plan": args.output_plan,
            "medicine_summary": args.output_medicine_summary,
            "summary": args.output_summary,
        },
        "notes": [
            "A-level exact evidence for Tunisia requires DPM public RCP/notice or MAH/DPM approved dossier documents.",
            "Foreign official registries are B-level only when same product/MAH/strength/form/route match, otherwise C-level same-DCI fallback.",
            "Device/borderline/homeopathy/allergen rows often need IFU/technical leaflet rather than standard RCP.",
        ],
    }
    Path(args.output_summary).write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
