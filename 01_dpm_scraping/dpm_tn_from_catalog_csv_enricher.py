#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import urllib3
from bs4 import BeautifulSoup

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

BASE_COLUMNS = [
    "nom", "dosage", "forme", "presentation", "nom_generique",
    "labo", "pays", "amm", "date_amm", "g_p",
    "detail_link_raw", "rcp_link_raw", "notice_link_raw",
    "detail_url", "rcp_url", "notice_url",
    "has_detail", "has_rcp", "has_notice",
]

DETAIL_COLUMNS = [
    "specialite_page", "dosage_page", "forme_page", "presentation_page",
    "conditionnement_primaire", "specification", "dci_page",
    "classement_veic", "classe_therapeutique", "sous_classe",
    "laboratoire_page", "tableau", "duree_conservation",
    "indication", "detail_fetch_status", "detail_http_status",
]

RCP_COLUMNS = [
    "rcp_verify_status", "rcp_http_status", "rcp_source", "verified_rcp_url",
    "downloaded_rcp_file", "rcp_sha256", "rcp_bytes",
    "rcp_text_path", "rcp_text_chars", "rcp_extract_status",
    "rcp_section_titles", "rcp_indications_title", "rcp_posologie_title",
    "rcp_contre_indications_title",
]

NOTICE_COLUMNS = [
    "notice_verify_status", "notice_http_status", "notice_source",
    "verified_notice_url", "downloaded_notice_file", "notice_sha256", "notice_bytes",
]

META_COLUMNS = ["last_check_utc"]

FIELD_ALIASES = {
    "specialite": "specialite_page",
    "spécialité": "specialite_page",
    "dosage": "dosage_page",
    "forme": "forme_page",
    "presentation": "presentation_page",
    "présentation": "presentation_page",
    "conditionnement primaire": "conditionnement_primaire",
    "specification": "specification",
    "spécification": "specification",
    "dci": "dci_page",
    "classement veic": "classement_veic",
    "classe therapeutique": "classe_therapeutique",
    "classe thérapeutique": "classe_therapeutique",
    "sous classe": "sous_classe",
    "sous-classe": "sous_classe",
    "laboratoire": "laboratoire_page",
    "tableau": "tableau",
    "duree de conservation": "duree_conservation",
    "durée de conservation": "duree_conservation",
    "indication": "indication",
    "amm": "amm",
    "date amm": "date_amm",
}

SECTION_VARIANTS = {
    "indications": [
        r"^indications?( therapeutiques?)?$",
        r"^4\.1 indications? therapeutiques?$",
    ],
    "posologie": [
        r"^posologie( et mode d administration)?$",
        r"^mode d administration$",
        r"^4\.2 posologie( et mode d administration)?$",
    ],
    "contre_indications": [
        r"^contre indications?$",
        r"^4\.3 contre indications?$",
    ],
}

OTHER_HEADING_VARIANTS = [
    r"^composition.*$",
    r"^forme pharmaceutique.*$",
    r"^mises? en garde.*$",
    r"^precautions? d emploi.*$",
    r"^interactions?.*$",
    r"^grossesse.*$",
    r"^allaitement.*$",
    r"^effets? indesirables?.*$",
    r"^surdosage.*$",
    r"^proprietes? pharmacologiques?.*$",
    r"^donnees? pharmaceutiques?.*$",
    r"^titulaire.*$",
    r"^date de .*revis.*$",
    r"^4\.[4-9].*$",
    r"^5\..*$",
    r"^6\..*$",
    r"^7\..*$",
    r"^8\..*$",
    r"^9\..*$",
]


def normalize_text(value: object) -> str:
    if value is None:
        return ""
    s = str(value).replace("\xa0", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def strip_accents(value: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(c)
    )


def slugify(value: object) -> str:
    s = normalize_text(value)
    s = strip_accents(s).lower().replace("µ", "u")
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def dosage_slug(value: object) -> str:
    s = normalize_text(value)
    s = strip_accents(s).lower().replace("µ", "u")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def now_utc() -> str:
    return pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_session(insecure: bool = False) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr,en;q=0.9",
        "Connection": "keep-alive",
    })
    s.verify = not insecure
    if insecure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return s


def try_request(session: requests.Session, method: str, url: str, verbose: bool = False, **kwargs):
    timeout = kwargs.pop("timeout", 30)
    for attempt in range(3):
        try:
            resp = session.request(method, url, timeout=timeout, **kwargs)
            if verbose:
                print(f"[NET] {method.upper()} {url} -> {resp.status_code} ({resp.url})")
            return resp
        except requests.RequestException as exc:
            if verbose:
                print(f"[NET] {method.upper()} {url} -> EXC {type(exc).__name__}: {exc}")
            if attempt == 2:
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def load_catalog(path: Path) -> pd.DataFrame:
    sep = "\t" if path.suffix.lower() in {".tsv", ".txt"} else ","
    if path.suffix.lower() == ".xlsx":
        df = pd.read_excel(path, dtype=str)
    else:
        df = pd.read_csv(path, sep=sep, dtype=str)
    df = df.copy()
    if "row_id" not in df.columns:
        df.insert(0, "row_id", range(1, len(df) + 1))
    else:
        df["row_id"] = pd.to_numeric(df["row_id"], errors="coerce").fillna(0).astype(int)

    for c in BASE_COLUMNS + DETAIL_COLUMNS + RCP_COLUMNS + NOTICE_COLUMNS + META_COLUMNS:
        if c not in df.columns:
            df[c] = ""

    for c in BASE_COLUMNS + DETAIL_COLUMNS + RCP_COLUMNS + NOTICE_COLUMNS + META_COLUMNS:
        df[c] = df[c].fillna("").map(normalize_text)

    return df


def save_outputs(df: pd.DataFrame, output_dir: Path, detail_rows: List[dict], rcp_rows: List[dict], notice_rows: List[dict]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_dir / "catalog_enriched.csv", index=False, encoding="utf-8-sig")
    df.to_csv(output_dir / "checkpoint_catalog_enriched.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(detail_rows).to_csv(output_dir / "detail_manifest.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(rcp_rows).to_csv(output_dir / "rcp_manifest.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(notice_rows).to_csv(output_dir / "notice_manifest.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(detail_rows).to_csv(output_dir / "checkpoint_detail_manifest.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(rcp_rows).to_csv(output_dir / "checkpoint_rcp_manifest.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(notice_rows).to_csv(output_dir / "checkpoint_notice_manifest.csv", index=False, encoding="utf-8-sig")


def sha256_of_path(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_probably_pdf_path(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            first = f.read(8)
        return first.startswith(b"%PDF")
    except Exception:
        return False


def parse_detail_page(url: str, html: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    fields: Dict[str, str] = {}

    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        label = normalize_text(cells[0].get_text(" ", strip=True)).rstrip(":")
        value = normalize_text(cells[-1].get_text(" ", strip=True))
        alias = FIELD_ALIASES.get(strip_accents(label).lower())
        if alias and value:
            fields[alias] = value

    full_text = soup.get_text("\n", strip=True)
    patterns = {
        "specialite_page": [r"specialit[eé][:\s]+(.+?)\n", r"nom(?: du)? medicament[:\s]+(.+?)\n"],
        "dosage_page": [r"dosage[:\s]+(.+?)\n"],
        "forme_page": [r"forme[:\s]+(.+?)\n"],
        "presentation_page": [r"pr[ée]sentation[:\s]+(.+?)\n"],
        "conditionnement_primaire": [r"conditionnement primaire[:\s]+(.+?)\n"],
        "specification": [r"sp[ée]cification[:\s]+(.+?)\n"],
        "dci_page": [r"dci[:\s]+(.+?)\n", r"nom g[ée]n[ée]rique[:\s]+(.+?)\n"],
        "classement_veic": [r"classement veic[:\s]+(.+?)\n"],
        "classe_therapeutique": [r"classe th[ée]rapeutique[:\s]+(.+?)\n"],
        "sous_classe": [r"sous[- ]classe[:\s]+(.+?)\n"],
        "laboratoire_page": [r"laboratoire[:\s]+(.+?)\n"],
        "tableau": [r"tableau[:\s]+(.+?)\n"],
        "duree_conservation": [r"dur[ée]e de conservation[:\s]+(.+?)\n"],
        "indication": [r"indication[:\s]+(.+?)\n"],
        "amm": [r"amm[:\s]+([A-Z0-9]+)\n"],
        "date_amm": [r"date amm[:\s]+(.+?)\n"],
    }
    for field, plist in patterns.items():
        if fields.get(field):
            continue
        for pat in plist:
            m = re.search(pat, full_text, flags=re.I)
            if m:
                fields[field] = normalize_text(m.group(1))
                break

    return fields


def build_rcp_candidates(row: pd.Series) -> List[Tuple[str, str]]:
    candidates: List[Tuple[str, str]] = []

    def add(source: str, url: str) -> None:
        url = normalize_text(url)
        if url and url.lower().startswith("http") and url not in [u for _, u in candidates]:
            candidates.append((source, url))

    add("catalog_rcp_url", row.get("rcp_url", ""))
    add("catalog_rcp_link_raw", row.get("rcp_link_raw", ""))

    generic_slug = slugify(row.get("nom_generique", "") or row.get("dci_page", ""))
    nom_slug = slugify(row.get("nom", "") or row.get("specialite_page", ""))
    dose_slug = dosage_slug(row.get("dosage", "") or row.get("dosage_page", ""))

    if generic_slug and dose_slug:
        add("generic_dose_guess", f"https://dpm.tn/images/rcp/{generic_slug}_{dose_slug}_rcp.pdf")
    if nom_slug and dose_slug:
        add("nom_dose_guess", f"https://dpm.tn/images/rcp/{nom_slug}_{dose_slug}_rcp.pdf")
    if generic_slug:
        add("generic_guess", f"https://dpm.tn/images/rcp/{generic_slug}_rcp.pdf")
    if nom_slug:
        add("nom_guess", f"https://dpm.tn/images/rcp/{nom_slug}_rcp.pdf")

    return candidates


def build_notice_candidates(row: pd.Series) -> List[Tuple[str, str]]:
    candidates: List[Tuple[str, str]] = []

    def add(source: str, url: str) -> None:
        url = normalize_text(url)
        if url and url.lower().startswith("http") and url not in [u for _, u in candidates]:
            candidates.append((source, url))

    add("catalog_notice_url", row.get("notice_url", ""))
    add("catalog_notice_link_raw", row.get("notice_link_raw", ""))
    return candidates


def verify_pdf_url(session: requests.Session, url: str, out_dir: Path, filename_stem: str, download: bool, verbose: bool = False) -> Dict[str, str]:
    result = {
        "verify_status": "missing",
        "http_status": "",
        "verified_url": "",
        "downloaded_file": "",
        "sha256": "",
        "bytes": "",
    }

    head = try_request(session, "HEAD", url, allow_redirects=True, verbose=verbose)
    if head is not None:
        result["http_status"] = str(head.status_code)

    ok = False
    final_url = url
    get = None

    if head is not None and head.status_code == 200:
        final_url = head.url
        ctype = (head.headers.get("Content-Type") or "").lower()
        ok = ("pdf" in ctype) or final_url.lower().endswith(".pdf")

    if not ok:
        get = try_request(session, "GET", url, allow_redirects=True, stream=True, verbose=verbose)
        if get is None:
            return result
        result["http_status"] = str(get.status_code)
        final_url = get.url
        if get.status_code != 200:
            return result
        first = next(get.iter_content(64), b"")
        ctype = (get.headers.get("Content-Type") or "").lower()
        ok = first.startswith(b"%PDF") or ("pdf" in ctype)
        if not ok:
            return result

    if download:
        if get is None:
            get = try_request(session, "GET", final_url, allow_redirects=True, stream=True, verbose=verbose)
            if get is None or get.status_code != 200:
                return result
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{filename_stem}.pdf"
        with open(path, "wb") as f:
            first_written = False
            for chunk in get.iter_content(1024 * 1024):
                if chunk:
                    if not first_written:
                        if not chunk.startswith(b"%PDF") and b"%PDF" not in chunk[:1024]:
                            f.close()
                            try:
                                path.unlink(missing_ok=True)
                            except Exception:
                                pass
                            return result
                        first_written = True
                    f.write(chunk)
        if not is_probably_pdf_path(path):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            return result
        result["downloaded_file"] = str(path)
        result["bytes"] = str(path.stat().st_size)
        result["sha256"] = sha256_of_path(path)

    result["verify_status"] = "verified"
    result["verified_url"] = final_url
    return result


def extract_pdf_text(pdf_path: Path) -> Tuple[str, str]:
    if PdfReader is None:
        return "", "pypdf_not_installed"
    try:
        reader = PdfReader(str(pdf_path))
        texts = []
        for page in reader.pages:
            try:
                texts.append(page.extract_text() or "")
            except Exception:
                texts.append("")
        full = "\n\n".join(texts)
        full = full.replace("\x00", " ")
        full = re.sub(r"[ \t]+", " ", full)
        full = re.sub(r"\n{3,}", "\n\n", full)
        full = full.encode("utf-8", "ignore").decode("utf-8", "ignore")
        return full.strip(), "ok"
    except Exception as e:
        return "", f"extract_error:{type(e).__name__}"


def _normalize_heading_line(line: str) -> str:
    s = strip_accents(normalize_text(line)).lower()
    s = s.replace("'", " ")
    s = re.sub(r"[^a-z0-9. ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _line_matches_any(line: str, patterns: List[str]) -> bool:
    return any(re.match(p, line, flags=re.I) for p in patterns)


def find_rcp_sections(full_text: str) -> Dict[str, str]:
    lines = [normalize_text(x) for x in full_text.splitlines()]
    norm_lines = [_normalize_heading_line(x) for x in lines]
    result = {
        "rcp_section_titles": "",
        "rcp_indications_title": "",
        "rcp_posologie_title": "",
        "rcp_contre_indications_title": "",
    }
    found_titles = []

    all_heading_patterns = []
    for pats in SECTION_VARIANTS.values():
        all_heading_patterns.extend(pats)
    all_heading_patterns.extend(OTHER_HEADING_VARIANTS)

    extracted = {
        "indications": "",
        "posologie": "",
        "contre_indications": "",
    }

    for key, pats in SECTION_VARIANTS.items():
        start_idx = None
        for i, nline in enumerate(norm_lines):
            if _line_matches_any(nline, pats):
                start_idx = i
                break
        if start_idx is None:
            continue
        title = lines[start_idx]
        found_titles.append(title)
        end_idx = len(lines)
        for j in range(start_idx + 1, len(lines)):
            nline = norm_lines[j]
            if not nline:
                continue
            if _line_matches_any(nline, all_heading_patterns):
                end_idx = j
                break
        body = "\n".join(x for x in lines[start_idx + 1:end_idx] if normalize_text(x)).strip()
        extracted[key] = body
        if key == "indications":
            result["rcp_indications_title"] = title
        elif key == "posologie":
            result["rcp_posologie_title"] = title
        elif key == "contre_indications":
            result["rcp_contre_indications_title"] = title

    result["rcp_section_titles"] = " | ".join(found_titles)
    return result


def extract_and_save_rcp_text(pdf_path: Path, text_dir: Path, file_stem: str) -> Dict[str, str]:
    out = {
        "rcp_text_path": "",
        "rcp_text_chars": "",
        "rcp_extract_status": "not_extracted",
        "rcp_section_titles": "",
        "rcp_indications_title": "",
        "rcp_posologie_title": "",
        "rcp_contre_indications_title": "",
    }

    text, status = extract_pdf_text(pdf_path)
    out["rcp_extract_status"] = status
    if status != "ok":
        return out

    text = text.encode("utf-8", "ignore").decode("utf-8", "ignore")

    text_dir.mkdir(parents=True, exist_ok=True)
    txt_path = text_dir / f"{file_stem}.txt"
    txt_path.write_text(text, encoding="utf-8", errors="ignore")

    out["rcp_text_path"] = str(txt_path)
    out["rcp_text_chars"] = str(len(text))
    out.update(find_rcp_sections(text))
    return out


def should_skip_detail(row: pd.Series, resume: bool) -> bool:
    return resume and normalize_text(row.get("detail_fetch_status", "")) in {"ok", "missing", "http_error", "fetch_failed"}


def should_skip_asset(row: pd.Series, status_col: str, resume: bool) -> bool:
    return resume and normalize_text(row.get(status_col, "")) in {"verified", "missing", "http_error", "fetch_failed"}


def main() -> None:
    p = argparse.ArgumentParser(description="Direct DPM catalog enricher based on medicaments_all_data.csv")
    p.add_argument("--input-csv", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--download-rcp", action="store_true")
    p.add_argument("--download-notice", action="store_true")
    p.add_argument("--extract-rcp-text", action="store_true")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--delay", type=float, default=2.0)
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--verbose-net", action="store_true")
    p.add_argument("--checkpoint-every", type=int, default=50)
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    input_path = Path(args.input_csv)
    if args.resume and (output_dir / "checkpoint_catalog_enriched.csv").exists():
        input_path = output_dir / "checkpoint_catalog_enriched.csv"

    df = load_catalog(input_path)
    session = ensure_session(insecure=args.insecure)

    detail_rows: List[dict] = []
    rcp_rows: List[dict] = []
    notice_rows: List[dict] = []

    if args.resume:
        for name, target in [
            ("checkpoint_detail_manifest.csv", detail_rows),
            ("checkpoint_rcp_manifest.csv", rcp_rows),
            ("checkpoint_notice_manifest.csv", notice_rows),
        ]:
            pth = output_dir / name
            if pth.exists():
                try:
                    target.extend(pd.read_csv(pth, dtype=str).fillna("").to_dict("records"))
                except Exception:
                    pass

    processed = 0
    rcp_dir = output_dir / "rcp_pdfs"
    notice_dir = output_dir / "notice_pdfs"
    text_dir = output_dir / "rcp_texts"

    for idx, row in df.iterrows():
        if args.limit and processed >= args.limit:
            break

        row_id = int(row.get("row_id", 0))

        if not should_skip_detail(row, args.resume):
            detail_url = normalize_text(row.get("detail_url", ""))
            if detail_url:
                resp = try_request(session, "GET", detail_url, verbose=args.verbose_net)
                if resp is not None:
                    df.at[idx, "detail_http_status"] = str(resp.status_code)
                if resp is None:
                    df.at[idx, "detail_fetch_status"] = "fetch_failed"
                elif resp.status_code != 200 or not resp.text:
                    df.at[idx, "detail_fetch_status"] = "http_error"
                else:
                    fields = parse_detail_page(detail_url, resp.text)
                    for k, v in fields.items():
                        if k in df.columns and v:
                            df.at[idx, k] = v
                    df.at[idx, "detail_fetch_status"] = "ok"
                df.at[idx, "last_check_utc"] = now_utc()
                detail_rows.append({
                    "row_id": row_id,
                    "amm": df.at[idx, "amm"],
                    "nom": df.at[idx, "nom"],
                    "detail_url": detail_url,
                    "detail_http_status": df.at[idx, "detail_http_status"],
                    "detail_fetch_status": df.at[idx, "detail_fetch_status"],
                })
                time.sleep(max(args.delay / 2, 0.1))

        if not should_skip_asset(df.loc[idx], "rcp_verify_status", args.resume):
            candidates = build_rcp_candidates(df.loc[idx])
            verified = None
            chosen_source = ""
            file_stem = re.sub(r"[^a-z0-9_]+", "_", slugify(df.at[idx, "amm"] or df.at[idx, "nom"] or row_id))[:120]
            for source, url in candidates:
                res = verify_pdf_url(session, url, rcp_dir, file_stem, download=args.download_rcp, verbose=args.verbose_net)
                if res["verify_status"] == "verified":
                    verified = res
                    chosen_source = source
                    break
                time.sleep(max(args.delay / 2, 0.1))
            if verified:
                df.at[idx, "rcp_verify_status"] = verified["verify_status"]
                df.at[idx, "rcp_http_status"] = verified["http_status"]
                df.at[idx, "rcp_source"] = chosen_source
                df.at[idx, "verified_rcp_url"] = verified["verified_url"]
                df.at[idx, "downloaded_rcp_file"] = verified["downloaded_file"]
                df.at[idx, "rcp_sha256"] = verified["sha256"]
                df.at[idx, "rcp_bytes"] = verified["bytes"]
                if args.extract_rcp_text and verified.get("downloaded_file"):
                    try:
                        extracted = extract_and_save_rcp_text(Path(verified["downloaded_file"]), text_dir, file_stem)
                        for k, v in extracted.items():
                            if k in df.columns:
                                df.at[idx, k] = v
                    except Exception as e:
                        df.at[idx, "rcp_extract_status"] = f"write_error:{type(e).__name__}"
            else:
                df.at[idx, "rcp_verify_status"] = "missing"
            df.at[idx, "last_check_utc"] = now_utc()
            rcp_rows.append({
                "row_id": row_id,
                "amm": df.at[idx, "amm"],
                "nom": df.at[idx, "nom"],
                "rcp_source": chosen_source,
                "rcp_verify_status": df.at[idx, "rcp_verify_status"],
                "rcp_http_status": df.at[idx, "rcp_http_status"],
                "verified_rcp_url": df.at[idx, "verified_rcp_url"],
                "downloaded_rcp_file": df.at[idx, "downloaded_rcp_file"],
                "rcp_sha256": df.at[idx, "rcp_sha256"],
                "rcp_bytes": df.at[idx, "rcp_bytes"],
            })
            time.sleep(max(args.delay / 2, 0.1))

        if not should_skip_asset(df.loc[idx], "notice_verify_status", args.resume):
            candidates = build_notice_candidates(df.loc[idx])
            verified = None
            chosen_source = ""
            file_stem = re.sub(r"[^a-z0-9_]+", "_", slugify((df.at[idx, "amm"] or df.at[idx, "nom"] or row_id))[:120]) + "_notice"
            for source, url in candidates:
                res = verify_pdf_url(session, url, notice_dir, file_stem, download=args.download_notice, verbose=args.verbose_net)
                if res["verify_status"] == "verified":
                    verified = res
                    chosen_source = source
                    break
                time.sleep(max(args.delay / 2, 0.1))
            if verified:
                df.at[idx, "notice_verify_status"] = verified["verify_status"]
                df.at[idx, "notice_http_status"] = verified["http_status"]
                df.at[idx, "notice_source"] = chosen_source
                df.at[idx, "verified_notice_url"] = verified["verified_url"]
                df.at[idx, "downloaded_notice_file"] = verified["downloaded_file"]
                df.at[idx, "notice_sha256"] = verified["sha256"]
                df.at[idx, "notice_bytes"] = verified["bytes"]
            else:
                df.at[idx, "notice_verify_status"] = "missing"
            df.at[idx, "last_check_utc"] = now_utc()
            notice_rows.append({
                "row_id": row_id,
                "amm": df.at[idx, "amm"],
                "nom": df.at[idx, "nom"],
                "notice_source": chosen_source,
                "notice_verify_status": df.at[idx, "notice_verify_status"],
                "notice_http_status": df.at[idx, "notice_http_status"],
                "verified_notice_url": df.at[idx, "verified_notice_url"],
                "downloaded_notice_file": df.at[idx, "downloaded_notice_file"],
                "notice_sha256": df.at[idx, "notice_sha256"],
                "notice_bytes": df.at[idx, "notice_bytes"],
            })
            time.sleep(max(args.delay / 2, 0.1))

        processed += 1
        if args.checkpoint_every and processed % args.checkpoint_every == 0:
            save_outputs(df, output_dir, detail_rows, rcp_rows, notice_rows)
            print(f"[CHECKPOINT] Saved after {processed} rows")

    save_outputs(df, output_dir, detail_rows, rcp_rows, notice_rows)
    print(f"[DONE] Outputs written to: {output_dir}")


if __name__ == "__main__":
    main()
