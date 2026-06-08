#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pandas as pd

try:
    from pypdf import PdfReader
except Exception:  # pragma: no cover
    PdfReader = None


AMM_RE = re.compile(r"\b\d{6,8}[A-Za-z]?\b")

# Common non-name tokens to avoid noisy fuzzy matching.
NAME_STOPWORDS = {
    "a", "au", "aux", "avec", "b", "boite", "boitee", "ce", "cette", "collyre",
    "comprime", "comprimes", "composition", "conseil", "contenu", "contre", "de",
    "des", "details", "dosage", "du", "effets", "emploi", "en", "et", "forme",
    "g", "gelule", "gelules", "gr", "gouttes", "h", "ii", "iii", "im", "indication",
    "info", "informations", "injectable", "iv", "la", "le", "les", "lisez", "mg",
    "ml", "mode", "notice", "nourrissons", "ou", "par", "pharmaceutique", "pour",
    "posologie", "precautions", "presentation", "produit", "qd", "rcp", "resume", "sac",
    "si", "solution", "sous", "specialite", "suspension", "sur", "traitement", "un",
    "une", "veuillez", "votre", "voie",
}

LINE_PREFIX_BLACKLIST = (
    "veuillez lire", "lisez attentivement", "gardez cette notice", "si vous avez",
    "n utilisez", "n'utilisez", "demandez conseil", "consultez", "composition",
    "forme et presentation", "formes et presentations", "excipients", "titulaire",
)


def normalize_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_amm(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", normalize_text(value).upper())


def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(c)
    )


def normalize_name(value: object) -> str:
    s = strip_accents(normalize_text(value)).lower()
    s = s.replace("\u00ae", " ").replace("\u2122", " ")
    s = re.sub(r"[^a-z0-9+\-\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def tokenize_name(value: str) -> Set[str]:
    tokens = set(re.findall(r"[a-z0-9]+", value))
    out: Set[str] = set()
    for token in tokens:
        if token in NAME_STOPWORDS:
            continue
        if len(token) < 2:
            continue
        if token.isdigit():
            continue
        out.add(token)
    return out


def detect_doc_kind(text: str) -> str:
    t = strip_accents(normalize_text(text)).lower()
    has_notice = "notice" in t
    has_rcp = bool(re.search(r"\brcp\b", t)) or ("resume des caracteristiques du produit" in t)

    if has_notice and has_rcp:
        return "both"
    if has_notice:
        return "notice"
    if has_rcp:
        return "rcp"
    return "unknown"


def read_catalog(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str).fillna("")
    if "row_id" not in df.columns:
        df.insert(0, "row_id", range(1, len(df) + 1))
    df["row_id"] = pd.to_numeric(df["row_id"], errors="coerce").fillna(0).astype(int)

    for col in ["amm", "nom", "nom_generique", "labo", "pays"]:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].map(normalize_text)

    return df


def read_mapped(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str).fillna("")
    if "ROW_ID" not in df.columns:
        df.insert(0, "ROW_ID", range(1, len(df) + 1))
    df["ROW_ID"] = pd.to_numeric(df["ROW_ID"], errors="coerce").fillna(0).astype(int)

    keep = {
        "ROW_ID": "row_id",
        "AMM": "mapped_amm",
        "NOM": "mapped_nom",
        "DOWNLOADED_RCP_FILE": "mapped_downloaded_rcp_file",
        "RCP_TEXT_PATH": "mapped_rcp_text_path",
        "RCP_VERIFY_STATUS": "mapped_rcp_verify_status",
    }
    cols = [c for c in keep if c in df.columns]
    out = df[cols].rename(columns=keep)

    for col in [
        "mapped_amm",
        "mapped_nom",
        "mapped_downloaded_rcp_file",
        "mapped_rcp_text_path",
        "mapped_rcp_verify_status",
    ]:
        if col not in out.columns:
            out[col] = ""
        out[col] = out[col].map(normalize_text)

    return out


def add_name_index(
    row_id: int,
    raw_name: object,
    key_to_rows: Dict[str, Set[int]],
    token_to_keys: Dict[str, Set[str]],
) -> None:
    key = normalize_name(raw_name)
    if not key:
        return

    key_to_rows.setdefault(key, set()).add(row_id)
    for token in tokenize_name(key):
        token_to_keys.setdefault(token, set()).add(key)


def build_catalog_indexes(catalog_df: pd.DataFrame) -> Dict[str, Dict]:
    amm_to_rows: Dict[str, Set[int]] = {}
    nom_to_rows: Dict[str, Set[int]] = {}
    generic_to_rows: Dict[str, Set[int]] = {}
    token_to_nom: Dict[str, Set[str]] = {}
    token_to_generic: Dict[str, Set[str]] = {}

    for _, row in catalog_df.iterrows():
        row_id = int(row.get("row_id", 0) or 0)
        if row_id <= 0:
            continue

        amm_norm = normalize_amm(row.get("amm", ""))
        if amm_norm:
            amm_to_rows.setdefault(amm_norm, set()).add(row_id)

        add_name_index(row_id, row.get("nom", ""), nom_to_rows, token_to_nom)
        add_name_index(row_id, row.get("nom_generique", ""), generic_to_rows, token_to_generic)

    return {
        "amm_to_rows": amm_to_rows,
        "nom_to_rows": nom_to_rows,
        "generic_to_rows": generic_to_rows,
        "token_to_nom": token_to_nom,
        "token_to_generic": token_to_generic,
    }


def add_unique_candidate(value: str, candidates: List[str], seen: Set[str]) -> None:
    val = normalize_text(value)
    if not val:
        return
    val = re.sub(r"\s+", " ", val).strip()
    if len(val) < 3 or val in seen:
        return
    seen.add(val)
    candidates.append(val[:180])


def extract_labeled_candidates(ascii_text: str, candidates: List[str], seen: Set[str]) -> None:
    patterns = [
        r"nom\s+du\s+medicament\s*[:\-]?\s*([^\n\r]+)",
        r"specialite\s*[:\-]?\s*([^\n\r]+)",
        r"specialite\s+pharmaceutique\s*[:\-]?\s*([^\n\r]+)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, ascii_text, flags=re.I):
            add_unique_candidate(match.group(1), candidates, seen)


def should_skip_candidate_line(line_ascii: str, token_count: int) -> bool:
    if any(line_ascii.startswith(prefix) for prefix in LINE_PREFIX_BLACKLIST):
        return True
    if re.fullmatch(r"[0-9\W_]+", line_ascii):
        return True
    if token_count == 0:
        return True
    if token_count > 14 and "nom du medicament" not in line_ascii and "specialite" not in line_ascii:
        return True
    return False


def extract_name_candidates(text: str, max_lines: int = 80) -> List[str]:
    lines = [normalize_text(x) for x in text.splitlines()]
    candidates: List[str] = []
    seen: Set[str] = set()

    ascii_text = strip_accents(text)
    extract_labeled_candidates(ascii_text, candidates, seen)

    for i, line in enumerate(lines[:max_lines]):
        if not line:
            continue

        line_ascii = strip_accents(line).lower()
        token_count = len(re.findall(r"[A-Za-z0-9]+", line_ascii))
        if should_skip_candidate_line(line_ascii, token_count):
            continue

        add_unique_candidate(line, candidates, seen)

        if ("nom du medicament" in line_ascii or "specialite" in line_ascii) and i + 1 < len(lines):
            add_unique_candidate(lines[i + 1], candidates, seen)

    return candidates[:80]


def compute_name_score(line_norm: str, line_tokens: Set[str], key_norm: str) -> Tuple[float, int]:
    key_tokens = tokenize_name(key_norm)
    shared = len(line_tokens & key_tokens)

    if key_norm and key_norm in line_norm:
        return 1.0, shared

    if line_norm and len(line_norm) >= 8 and line_norm in key_norm:
        return 0.95, shared

    seq_ratio = SequenceMatcher(None, line_norm, key_norm).ratio()
    key_overlap = shared / max(1, len(key_tokens))
    line_coverage = shared / max(1, len(line_tokens))

    score = 0.58 * seq_ratio + 0.27 * key_overlap + 0.15 * line_coverage
    return score, shared


def is_nom_match_acceptable(score: float, shared_tokens: int) -> bool:
    if score >= 0.97:
        return True
    if score >= 0.90 and shared_tokens >= 1:
        return True
    if score >= 0.84 and shared_tokens >= 2:
        return True
    return False


def is_nom_match_acceptable_aggressive(score: float, shared_tokens: int) -> bool:
    if score >= 0.95:
        return True
    if score >= 0.84 and shared_tokens >= 1:
        return True
    if score >= 0.78 and shared_tokens >= 2:
        return True
    if score >= 0.74 and shared_tokens >= 3:
        return True
    return False


def is_generic_match_acceptable(score: float, shared_tokens: int) -> bool:
    if score >= 0.96:
        return True
    if score >= 0.89 and shared_tokens >= 2:
        return True
    return False


def is_generic_match_acceptable_aggressive(score: float, shared_tokens: int) -> bool:
    if score >= 0.92:
        return True
    if score >= 0.84 and shared_tokens >= 1:
        return True
    if score >= 0.80 and shared_tokens >= 2:
        return True
    return False


def filter_row_ids_by_preference(
    row_ids: Set[int],
    preferred_rows: Set[int],
    enforce_preferred_rows: bool,
) -> Set[int]:
    if not row_ids:
        return set()
    if not preferred_rows:
        return set(row_ids)

    narrowed = row_ids & preferred_rows
    if narrowed:
        return narrowed
    if enforce_preferred_rows:
        return set()
    return set(row_ids)


def collect_candidate_keys(
    line_norm: str,
    line_tokens: Set[str],
    indexes: Dict[str, Dict],
) -> Tuple[Set[str], Set[str]]:
    nom_keys: Set[str] = set()
    generic_keys: Set[str] = set()

    for token in line_tokens:
        nom_keys.update(indexes["token_to_nom"].get(token, set()))
        generic_keys.update(indexes["token_to_generic"].get(token, set()))

    if line_norm in indexes["nom_to_rows"]:
        nom_keys.add(line_norm)
    if line_norm in indexes["generic_to_rows"]:
        generic_keys.add(line_norm)

    return nom_keys, generic_keys


def update_best_match(
    best: Dict[str, object],
    score: float,
    row_ids: Set[int],
    method: str,
    key: str,
    raw_line: str,
) -> Dict[str, object]:
    if not row_ids:
        return best
    if score <= float(best["match_score"]):
        return best

    return {
        "row_ids": set(row_ids),
        "match_method": method,
        "match_key": key,
        "match_score": float(score),
        "match_line": raw_line,
    }


def evaluate_nom_matches(
    raw_line: str,
    line_norm: str,
    line_tokens: Set[str],
    nom_keys: Set[str],
    indexes: Dict[str, Dict],
    preferred_rows: Set[int],
    enforce_preferred_rows: bool,
    aggressive: bool,
    best: Dict[str, object],
) -> Dict[str, object]:
    for key in nom_keys:
        score, shared = compute_name_score(line_norm, line_tokens, key)
        ok = is_nom_match_acceptable_aggressive(score, shared) if aggressive else is_nom_match_acceptable(score, shared)
        if not ok:
            continue
        row_ids = indexes["nom_to_rows"].get(key, set())
        row_ids = filter_row_ids_by_preference(row_ids, preferred_rows, enforce_preferred_rows)
        best = update_best_match(best, score, row_ids, "name_nom", key, raw_line)
    return best


def evaluate_generic_matches(
    raw_line: str,
    line_norm: str,
    line_tokens: Set[str],
    generic_keys: Set[str],
    indexes: Dict[str, Dict],
    max_rows_per_generic: int,
    preferred_rows: Set[int],
    enforce_preferred_rows: bool,
    aggressive: bool,
    best: Dict[str, object],
) -> Dict[str, object]:
    generic_limit = max_rows_per_generic
    if aggressive:
        generic_limit = max(max_rows_per_generic * 3, 20)

    for key in generic_keys:
        row_ids = indexes["generic_to_rows"].get(key, set())
        row_ids = filter_row_ids_by_preference(row_ids, preferred_rows, enforce_preferred_rows)
        if not row_ids or len(row_ids) > generic_limit:
            continue

        score, shared = compute_name_score(line_norm, line_tokens, key)
        score -= 0.04
        ok = is_generic_match_acceptable_aggressive(score, shared) if aggressive else is_generic_match_acceptable(score, shared)
        if not ok:
            continue

        best = update_best_match(best, score, row_ids, "name_generic", key, raw_line)
    return best


def match_rows_by_name(
    text: str,
    indexes: Dict[str, Dict],
    max_rows_per_generic: int,
    preferred_rows: Set[int],
    enforce_preferred_rows: bool,
    aggressive: bool,
) -> Dict[str, object]:
    candidates = extract_name_candidates(text)

    best: Dict[str, object] = {
        "row_ids": set(),
        "match_method": "",
        "match_key": "",
        "match_score": 0.0,
        "match_line": "",
    }

    for raw_line in candidates:
        line_norm = normalize_name(raw_line)
        if len(line_norm) < 4:
            continue

        line_tokens = tokenize_name(line_norm)
        if not line_tokens:
            continue

        nom_keys, generic_keys = collect_candidate_keys(line_norm, line_tokens, indexes)
        best = evaluate_nom_matches(
            raw_line,
            line_norm,
            line_tokens,
            nom_keys,
            indexes,
            preferred_rows,
            enforce_preferred_rows,
            aggressive,
            best,
        )
        best = evaluate_generic_matches(
            raw_line,
            line_norm,
            line_tokens,
            generic_keys,
            indexes,
            max_rows_per_generic,
            preferred_rows,
            enforce_preferred_rows,
            aggressive,
            best,
        )

    return best


def extract_pdf_signals(pdf_path: Path, max_pages: int) -> Tuple[Set[str], str, str]:
    if PdfReader is None:
        return set(), "unknown", ""

    reader = PdfReader(str(pdf_path))
    pages_to_read = min(max(1, max_pages), len(reader.pages))
    chunks: List[str] = []

    for i in range(pages_to_read):
        chunks.append(reader.pages[i].extract_text() or "")

    text = "\n".join(chunks)
    amms = {normalize_amm(x) for x in AMM_RE.findall(text) if normalize_amm(x)}
    kind = detect_doc_kind(text)
    return amms, kind, text


def new_row_local_info() -> Dict[str, Set[str]]:
    return {
        "sources": set(),
        "kinds": set(),
        "files": set(),
        "methods": set(),
    }


def process_pdf_for_mapping(
    pdf_path: Path,
    source_name: str,
    max_pages: int,
    indexes: Dict[str, Dict],
    max_rows_per_generic: int,
    preferred_rows: Set[int],
    enforce_preferred_rows: bool,
    aggressive: bool,
) -> Tuple[dict, Set[int], Set[str], bool, bool, bool]:
    amms, kind, text = extract_pdf_signals(pdf_path, max_pages=max_pages)

    matched_rows: Set[int] = set()
    methods: Set[str] = set()

    for amm in amms:
        exact_rows = indexes["amm_to_rows"].get(amm, set())
        if exact_rows:
            matched_rows.update(exact_rows)
            methods.add("amm_exact")

    name_match = match_rows_by_name(
        text=text,
        indexes=indexes,
        max_rows_per_generic=max_rows_per_generic,
        preferred_rows=preferred_rows,
        enforce_preferred_rows=enforce_preferred_rows,
        aggressive=aggressive,
    )

    name_row_ids = set(name_match.get("row_ids", set()))
    if name_row_ids:
        matched_rows.update(name_row_ids)
        methods.add(str(name_match.get("match_method", "name")))

    row_entry = {
        "source": source_name,
        "pdf_path": str(pdf_path),
        "doc_kind": kind,
        "amm_matches": ";".join(sorted(amms)),
        "amm_count": len(amms),
        "mapped_row_count": len(matched_rows),
        "mapped_row_ids": ";".join(str(x) for x in sorted(matched_rows)),
        "name_matched_row_ids": ";".join(str(x) for x in sorted(name_row_ids)),
        "mapped_by_amm": "amm_exact" in methods,
        "mapped_by_name": bool(name_row_ids),
        "name_match_method": normalize_text(name_match.get("match_method", "")),
        "name_match_key": normalize_text(name_match.get("match_key", "")),
        "name_match_score": round(float(name_match.get("match_score", 0.0)), 4),
        "name_match_line": normalize_text(name_match.get("match_line", "")),
    }

    has_amm = bool(amms)
    is_notice = kind in {"notice", "both"}
    is_rcp = kind in {"rcp", "both"}

    return row_entry, matched_rows, methods, has_amm, is_notice, is_rcp


def init_scan_stats(file_count: int) -> Dict[str, int]:
    return {
        "files": file_count,
        "with_amm": 0,
        "with_notice": 0,
        "with_rcp": 0,
        "mapped_by_amm_files": 0,
        "mapped_by_name_files": 0,
        "mapped_rows": 0,
        "errors": 0,
    }


def make_error_row(source_name: str, pdf_path: Path) -> dict:
    return {
        "source": source_name,
        "pdf_path": str(pdf_path),
        "doc_kind": "error",
        "amm_matches": "",
        "amm_count": 0,
        "mapped_row_count": 0,
        "mapped_row_ids": "",
        "name_matched_row_ids": "",
        "mapped_by_amm": False,
        "mapped_by_name": False,
        "name_match_method": "",
        "name_match_key": "",
        "name_match_score": 0.0,
        "name_match_line": "",
    }


def update_scan_stats(
    stats: Dict[str, int],
    *,
    has_amm: bool = False,
    is_notice: bool = False,
    is_rcp: bool = False,
    mapped_by_amm: bool = False,
    mapped_by_name: bool = False,
    is_error: bool = False,
) -> None:
    if is_error:
        stats["errors"] += 1
        return

    stats["with_amm"] += int(has_amm)
    stats["with_notice"] += int(is_notice)
    stats["with_rcp"] += int(is_rcp)
    stats["mapped_by_amm_files"] += int(mapped_by_amm)
    stats["mapped_by_name_files"] += int(mapped_by_name)


def register_row_matches(
    row_map: Dict[int, dict],
    matched_rows: Set[int],
    source_name: str,
    doc_kind: str,
    pdf_path: Path,
    methods: Set[str],
) -> None:
    for row_id in matched_rows:
        info = row_map.setdefault(row_id, new_row_local_info())
        info["sources"].add(source_name)
        info["kinds"].add(doc_kind)
        info["files"].add(str(pdf_path))
        info["methods"].update(methods)


def scan_pdf_folder_by_content(
    folder_path: Path,
    source_name: str,
    max_pages: int,
    indexes: Dict[str, Dict],
    max_rows_per_generic: int,
    preferred_rows: Set[int],
    enforce_preferred_rows: bool,
    aggressive: bool,
) -> Tuple[List[dict], Dict[int, dict], Dict[str, int]]:
    rows: List[dict] = []
    row_map: Dict[int, dict] = {}

    if not folder_path.exists():
        return rows, row_map, init_scan_stats(0)

    pdf_files = sorted(folder_path.glob("*.pdf"))
    stats = init_scan_stats(len(pdf_files))

    for pdf_path in pdf_files:
        try:
            row_entry, matched_rows, methods, has_amm, is_notice, is_rcp = process_pdf_for_mapping(
                pdf_path=pdf_path,
                source_name=source_name,
                max_pages=max_pages,
                indexes=indexes,
                max_rows_per_generic=max_rows_per_generic,
                preferred_rows=preferred_rows,
                enforce_preferred_rows=enforce_preferred_rows,
                aggressive=aggressive,
            )

            update_scan_stats(
                stats,
                has_amm=has_amm,
                is_notice=is_notice,
                is_rcp=is_rcp,
                mapped_by_amm=bool(row_entry["mapped_by_amm"]),
                mapped_by_name=bool(row_entry["mapped_by_name"]),
            )

            rows.append(row_entry)
            register_row_matches(
                row_map=row_map,
                matched_rows=matched_rows,
                source_name=source_name,
                doc_kind=str(row_entry["doc_kind"]),
                pdf_path=pdf_path,
                methods=methods,
            )

        except Exception:
            update_scan_stats(stats, is_error=True)
            rows.append(make_error_row(source_name, pdf_path))

    stats["mapped_rows"] = len(row_map)
    return rows, row_map, stats


def scan_rcp_files_by_stem(rcp_dir: Path) -> Set[str]:
    if not rcp_dir.exists():
        return set()
    out: Set[str] = set()
    for pdf in rcp_dir.glob("*.pdf"):
        out.add(normalize_amm(pdf.stem))
    return {x for x in out if x}


def path_exists_from_workspace(value: str, workspace_dir: Path) -> bool:
    p = normalize_text(value)
    if not p:
        return False

    p_norm = p.replace("\\", "/")
    p_obj = Path(p_norm)
    if p_obj.is_absolute():
        return p_obj.exists()

    return (workspace_dir / p_obj).exists()


def merge_row_maps(parts: List[Dict[int, dict]]) -> Dict[int, dict]:
    merged: Dict[int, dict] = {}
    for chunk in parts:
        for row_id, info in chunk.items():
            slot = merged.setdefault(row_id, new_row_local_info())
            slot["sources"].update(info.get("sources", set()))
            slot["kinds"].update(info.get("kinds", set()))
            slot["files"].update(info.get("files", set()))
            slot["methods"].update(info.get("methods", set()))
    return merged


def build_preferred_rows_by_source(catalog_df: pd.DataFrame, sources: List[str]) -> Dict[str, Set[int]]:
    out: Dict[str, Set[int]] = {}
    labo_norm = catalog_df["labo"].map(normalize_name)

    for source in sources:
        source_norm = normalize_name(source)
        if not source_norm:
            out[source] = set()
            continue

        mask = labo_norm.str.contains(re.escape(source_norm), regex=True)
        ids = set(catalog_df.loc[mask, "row_id"].astype(int).tolist())
        out[source] = ids

    return out


def parse_row_ids(raw: object) -> List[int]:
    text = normalize_text(raw)
    if not text:
        return []

    out: List[int] = []
    for token in text.split(";"):
        token = token.strip()
        if not token:
            continue
        try:
            out.append(int(token))
        except Exception:
            continue
    return out


def collect_review_reasons(
    *,
    score: float,
    mapped_count: int,
    method: str,
    weak_score_threshold: float,
) -> List[str]:
    reasons: List[str] = []
    if score < weak_score_threshold:
        reasons.append("low_score")
    if mapped_count > 1:
        reasons.append("multiple_candidates")
    if method == "name_generic":
        reasons.append("generic_match")
    return reasons


def extract_review_core_fields(row: pd.Series) -> Tuple[bool, float, int, str, List[str]]:
    if str(row.get("mapped_by_name", "")).lower() != "true":
        return False, 0.0, 0, "", []

    score = float(row.get("name_match_score", 0.0) or 0.0)
    mapped_count = int(float(row.get("mapped_row_count", 0) or 0))
    method = normalize_text(row.get("name_match_method", ""))
    return True, score, mapped_count, method, []


def build_review_rows_for_pdf(
    row: pd.Series,
    row_lookup: Dict[int, Dict[str, object]],
    score: float,
    mapped_count: int,
    method: str,
    reasons: List[str],
) -> List[dict]:
    out: List[dict] = []
    candidate_row_ids = parse_row_ids(row.get("mapped_row_ids", ""))
    if not candidate_row_ids:
        candidate_row_ids = [0]

    for rid in candidate_row_ids:
        meta = row_lookup.get(rid, {"amm": "", "nom": "", "labo": ""})
        out.append(
            {
                "source": normalize_text(row.get("source", "")),
                "pdf_path": normalize_text(row.get("pdf_path", "")),
                "doc_kind": normalize_text(row.get("doc_kind", "")),
                "name_match_method": method,
                "name_match_score": round(score, 4),
                "mapped_row_count": mapped_count,
                "review_reasons": ";".join(reasons),
                "name_match_line": normalize_text(row.get("name_match_line", "")),
                "proposed_row_id": rid if rid else "",
                "proposed_amm": normalize_text(meta.get("amm", "")),
                "proposed_nom": normalize_text(meta.get("nom", "")),
                "proposed_labo": normalize_text(meta.get("labo", "")),
            }
        )

    return out


def build_review_queue(
    scan_df: pd.DataFrame,
    catalog_df: pd.DataFrame,
    weak_score_threshold: float,
) -> pd.DataFrame:
    row_lookup = catalog_df.set_index("row_id")[["amm", "nom", "labo"]].to_dict("index")

    review_rows: List[dict] = []
    for _, row in scan_df.iterrows():
        is_name_match, score, mapped_count, method, _ = extract_review_core_fields(row)
        if not is_name_match:
            continue

        reasons = collect_review_reasons(
            score=score,
            mapped_count=mapped_count,
            method=method,
            weak_score_threshold=weak_score_threshold,
        )
        if not reasons:
            continue

        review_rows.extend(
            build_review_rows_for_pdf(
                row=row,
                row_lookup=row_lookup,
                score=score,
                mapped_count=mapped_count,
                method=method,
                reasons=reasons,
            )
        )

    out_df = pd.DataFrame(review_rows)
    if out_df.empty:
        return out_df

    out_df = out_df.sort_values(
        ["name_match_score", "mapped_row_count", "source", "proposed_nom"],
        ascending=[True, False, True, True],
    )
    return out_df


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize medicine coverage from local files only")
    parser.add_argument("--catalog-csv", default="medicaments_all_data.csv")
    parser.add_argument("--mapped-csv", default="dpm_live_out/medicines_exact_mapped.csv")
    parser.add_argument("--rcp-dir", default="dpm_live_out/rcp_pdfs")
    parser.add_argument("--notice-dir", default="dpm_live_out/notice_pdfs")
    parser.add_argument("--lab-folders", nargs="*", default=["medis", "teriak", "unimed"])
    parser.add_argument("--scan-pages", type=int, default=3)
    parser.add_argument("--max-rows-per-generic", type=int, default=8)
    parser.add_argument("--aggressive-sources", nargs="*", default=["teriak"])
    parser.add_argument("--strict-source-lab-filter", nargs="*", default=["teriak"])
    parser.add_argument("--review-weak-score", type=float, default=0.93)
    parser.add_argument("--output-csv", default="dpm_live_out/local_document_coverage.csv")
    parser.add_argument("--summary-json", default="dpm_live_out/local_document_coverage_summary.json")
    parser.add_argument("--scan-report-csv", default="dpm_live_out/local_pdf_scan_report.csv")
    parser.add_argument("--review-queue-csv", default="dpm_live_out/local_mapping_review_queue.csv")
    args = parser.parse_args()

    workspace = Path.cwd()
    catalog_path = (workspace / args.catalog_csv).resolve()
    mapped_path = (workspace / args.mapped_csv).resolve()
    rcp_dir = (workspace / args.rcp_dir).resolve()
    notice_dir = (workspace / args.notice_dir).resolve()
    output_csv = (workspace / args.output_csv).resolve()
    summary_json = (workspace / args.summary_json).resolve()
    scan_report_csv = (workspace / args.scan_report_csv).resolve()
    review_queue_csv = (workspace / args.review_queue_csv).resolve()

    if PdfReader is not None:
        logging.getLogger("pypdf").setLevel(logging.ERROR)
        logging.getLogger("pypdf._reader").setLevel(logging.ERROR)

    catalog_df = read_catalog(catalog_path)
    mapped_df = read_mapped(mapped_path)
    merged_df = catalog_df.merge(mapped_df, on="row_id", how="left")
    indexes = build_catalog_indexes(catalog_df)

    aggressive_sources = {normalize_name(x) for x in args.aggressive_sources}
    strict_sources = {normalize_name(x) for x in args.strict_source_lab_filter}
    preferred_rows_by_source = build_preferred_rows_by_source(catalog_df, args.lab_folders + ["dpm_notice"])

    dpm_rcp_amms = scan_rcp_files_by_stem(rcp_dir)

    scan_rows: List[dict] = []
    row_maps: List[Dict[int, dict]] = []
    folder_stats: Dict[str, Dict[str, int]] = {}

    for folder in args.lab_folders:
        folder_path = (workspace / folder).resolve()
        source_norm = normalize_name(folder)
        preferred_rows = preferred_rows_by_source.get(folder, set())
        rows, row_map, stats = scan_pdf_folder_by_content(
            folder_path=folder_path,
            source_name=folder,
            max_pages=args.scan_pages,
            indexes=indexes,
            max_rows_per_generic=args.max_rows_per_generic,
            preferred_rows=preferred_rows,
            enforce_preferred_rows=source_norm in strict_sources,
            aggressive=source_norm in aggressive_sources,
        )
        scan_rows.extend(rows)
        row_maps.append(row_map)
        folder_stats[folder] = stats

    notice_rows, notice_row_map, notice_stats = scan_pdf_folder_by_content(
        folder_path=notice_dir,
        source_name="dpm_notice",
        max_pages=args.scan_pages,
        indexes=indexes,
        max_rows_per_generic=args.max_rows_per_generic,
        preferred_rows=preferred_rows_by_source.get("dpm_notice", set()),
        enforce_preferred_rows=False,
        aggressive=False,
    )
    scan_rows.extend(notice_rows)
    row_maps.append(notice_row_map)
    folder_stats["dpm_notice"] = notice_stats

    local_row_map = merge_row_maps(row_maps)

    out_rows: List[dict] = []
    for _, row in merged_df.iterrows():
        row_id = int(row.get("row_id", 0) or 0)
        amm = normalize_text(row.get("amm", "") or row.get("mapped_amm", ""))
        amm_norm = normalize_amm(amm)
        nom = normalize_text(row.get("nom", "") or row.get("mapped_nom", ""))

        mapped_download_exists = path_exists_from_workspace(row.get("mapped_downloaded_rcp_file", ""), workspace)
        mapped_text_exists = path_exists_from_workspace(row.get("mapped_rcp_text_path", ""), workspace)

        dpm_rcp_by_stem = amm_norm in dpm_rcp_amms
        local_info = local_row_map.get(row_id, new_row_local_info())

        local_sources = set(local_info.get("sources", set()))
        local_kinds = set(local_info.get("kinds", set()))
        local_methods = set(local_info.get("methods", set()))

        lab_rcp = ("rcp" in local_kinds) or ("both" in local_kinds)
        lab_notice = ("notice" in local_kinds) or ("both" in local_kinds)
        lab_unknown = bool(local_sources) and not (lab_rcp or lab_notice)

        local_rcp = bool(dpm_rcp_by_stem or mapped_download_exists or mapped_text_exists or lab_rcp)
        local_notice = bool(lab_notice)
        local_any = bool(local_rcp or local_notice or lab_unknown)

        out_rows.append({
            "row_id": row_id,
            "amm": amm,
            "nom": nom,
            "labo": normalize_text(row.get("labo", "")),
            "pays": normalize_text(row.get("pays", "")),
            "local_rcp_available": local_rcp,
            "local_notice_available": local_notice,
            "local_unknown_doc_available": lab_unknown,
            "local_any_document_available": local_any,
            "local_dpm_rcp_by_filename": bool(dpm_rcp_by_stem),
            "local_mapped_downloaded_rcp_exists": bool(mapped_download_exists),
            "local_mapped_rcp_text_exists": bool(mapped_text_exists),
            "local_lab_sources": ";".join(sorted(local_sources)),
            "local_lab_doc_kinds": ";".join(sorted(local_kinds)),
            "local_lab_match_methods": ";".join(sorted(local_methods)),
            "local_lab_file_count_for_row": len(local_info.get("files", set())),
            "mapped_rcp_verify_status": normalize_text(row.get("mapped_rcp_verify_status", "")),
        })

    out_df = pd.DataFrame(out_rows).sort_values(
        ["local_any_document_available", "nom", "amm"],
        ascending=[False, True, True],
    )

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(output_csv, index=False, encoding="utf-8-sig")

    covered_csv = output_csv.with_name("local_document_covered_only.csv")
    missing_csv = output_csv.with_name("local_document_missing_only.csv")

    out_df[out_df["local_any_document_available"]].to_csv(
        covered_csv,
        index=False,
        encoding="utf-8-sig",
    )
    out_df[~out_df["local_any_document_available"]].to_csv(
        missing_csv,
        index=False,
        encoding="utf-8-sig",
    )

    scan_df = pd.DataFrame(scan_rows)
    scan_df.to_csv(scan_report_csv, index=False, encoding="utf-8-sig")

    review_df = build_review_queue(
        scan_df=scan_df,
        catalog_df=catalog_df,
        weak_score_threshold=float(args.review_weak_score),
    )
    review_df.to_csv(review_queue_csv, index=False, encoding="utf-8-sig")

    summary = {
        "total_medicines": int(len(out_df)),
        "covered_local_any": int(out_df["local_any_document_available"].sum()),
        "covered_local_rcp": int(out_df["local_rcp_available"].sum()),
        "covered_local_notice": int(out_df["local_notice_available"].sum()),
        "covered_local_unknown_docs": int(out_df["local_unknown_doc_available"].sum()),
        "missing_local_all": int((~out_df["local_any_document_available"]).sum()),
        "coverage_any_percent": round(100.0 * float(out_df["local_any_document_available"].sum()) / max(1, len(out_df)), 2),
        "coverage_rcp_percent": round(100.0 * float(out_df["local_rcp_available"].sum()) / max(1, len(out_df)), 2),
        "coverage_notice_percent": round(100.0 * float(out_df["local_notice_available"].sum()) / max(1, len(out_df)), 2),
        "coverage_unknown_percent": round(100.0 * float(out_df["local_unknown_doc_available"].sum()) / max(1, len(out_df)), 2),
        "unique_amm_from_dpm_rcp_filenames": int(len(dpm_rcp_amms)),
        "mapped_rows_from_scanned_pdfs": int(len(local_row_map)),
        "review_queue_rows": int(len(review_df)),
        "scan_pages": int(args.scan_pages),
        "max_rows_per_generic": int(args.max_rows_per_generic),
        "review_weak_score": float(args.review_weak_score),
        "aggressive_sources": sorted(x for x in aggressive_sources if x),
        "strict_source_lab_filter": sorted(x for x in strict_sources if x),
        "folder_stats": folder_stats,
        "output_files": {
            "coverage": str(output_csv),
            "covered_only": str(covered_csv),
            "missing_only": str(missing_csv),
            "scan_report": str(scan_report_csv),
            "review_queue": str(review_queue_csv),
        },
    }

    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[DONE] Wrote: {output_csv}")
    print(f"[DONE] Wrote: {covered_csv}")
    print(f"[DONE] Wrote: {missing_csv}")
    print(f"[DONE] Wrote: {scan_report_csv}")
    print(f"[DONE] Wrote: {review_queue_csv}")
    print(f"[DONE] Wrote: {summary_json}")


if __name__ == "__main__":
    main()
