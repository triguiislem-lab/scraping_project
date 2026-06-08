#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

import pandas as pd
import requests
import urllib3

try:
    from pypdf import PdfReader
except Exception:  # pragma: no cover
    PdfReader = None


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

PDF_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/pdf,*/*;q=0.8",
    "Accept-Language": "fr,en;q=0.9",
    "Connection": "keep-alive",
}

AMM_RE = re.compile(r"\b\d{6,8}[A-Za-z]?\b")


def normalize_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_amm(value: object) -> str:
    s = normalize_text(value).upper()
    return re.sub(r"[^A-Z0-9]", "", s)


def is_verified(status: object) -> bool:
    return normalize_text(status).lower() == "verified"


def verify_pdf_url(url: str, timeout: int, insecure: bool) -> Dict[str, str]:
    result = {
        "verify_status": "missing",
        "http_status": "",
        "verified_url": "",
        "content_type": "",
        "error": "",
    }
    url = normalize_text(url)
    if not url:
        return result

    verify_ssl = not insecure

    try:
        head = requests.head(
            url,
            headers=PDF_HEADERS,
            allow_redirects=True,
            timeout=timeout,
            verify=verify_ssl,
        )
        result["http_status"] = str(head.status_code)
        result["verified_url"] = normalize_text(head.url)
        result["content_type"] = normalize_text(head.headers.get("Content-Type", "")).lower()

        if head.status_code == 200:
            if ("pdf" in result["content_type"]) or result["verified_url"].lower().endswith(".pdf"):
                result["verify_status"] = "verified"
                return result
    except requests.RequestException as exc:
        result["error"] = f"HEAD:{type(exc).__name__}"

    try:
        get = requests.get(
            url,
            headers=PDF_HEADERS,
            allow_redirects=True,
            stream=True,
            timeout=timeout,
            verify=verify_ssl,
        )
        result["http_status"] = str(get.status_code)
        result["verified_url"] = normalize_text(get.url)
        result["content_type"] = normalize_text(get.headers.get("Content-Type", "")).lower()

        if get.status_code != 200:
            return result

        first = next(get.iter_content(128), b"")
        if first.startswith(b"%PDF") or ("pdf" in result["content_type"]):
            result["verify_status"] = "verified"
    except requests.RequestException as exc:
        if not result["error"]:
            result["error"] = f"GET:{type(exc).__name__}"

    return result


def verify_unique_urls(urls: Iterable[str], timeout: int, insecure: bool, max_workers: int) -> Dict[str, Dict[str, str]]:
    uniq = sorted({normalize_text(u) for u in urls if normalize_text(u)})
    out: Dict[str, Dict[str, str]] = {}
    if not uniq:
        return out

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(verify_pdf_url, u, timeout, insecure): u
            for u in uniq
        }
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                out[url] = fut.result()
            except Exception as exc:  # pragma: no cover
                out[url] = {
                    "verify_status": "missing",
                    "http_status": "",
                    "verified_url": "",
                    "content_type": "",
                    "error": f"EXEC:{type(exc).__name__}",
                }
    return out


def extract_amms_from_pdf(pdf_path: Path, pages: int) -> Set[str]:
    if PdfReader is None:
        return set()

    reader = PdfReader(str(pdf_path))
    max_pages = min(max(1, pages), len(reader.pages))
    content = []
    for i in range(max_pages):
        content.append(reader.pages[i].extract_text() or "")
    text = "\n".join(content)
    return {normalize_amm(x) for x in AMM_RE.findall(text) if normalize_amm(x)}


def scan_single_lab_folder(folder_path: Path, folder_name: str, pages: int, amm_to_sources: Dict[str, Set[str]]) -> Dict[str, int]:
    files = list(folder_path.glob("*.pdf")) if folder_path.exists() else []
    with_amm = 0
    errors = 0
    local_amm: Set[str] = set()

    for pdf_path in files:
        try:
            found = extract_amms_from_pdf(pdf_path, pages)
            if not found:
                continue

            with_amm += 1
            local_amm.update(found)
            for amm in found:
                amm_to_sources.setdefault(amm, set()).add(folder_name)
        except Exception:
            errors += 1

    return {
        "files": len(files),
        "with_amm": with_amm,
        "unique_amm": len(local_amm),
        "errors": errors,
    }


def read_catalog(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str).fillna("")
    if "row_id" not in df.columns:
        df.insert(0, "row_id", range(1, len(df) + 1))
    df["row_id"] = pd.to_numeric(df["row_id"], errors="coerce").fillna(0).astype(int)
    for col in ["amm", "nom", "labo", "pays", "rcp_url", "notice_url"]:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].map(normalize_text)
    return df


def read_mapped(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str).fillna("")
    if "ROW_ID" not in df.columns:
        df.insert(0, "ROW_ID", range(1, len(df) + 1))
    df["ROW_ID"] = pd.to_numeric(df["ROW_ID"], errors="coerce").fillna(0).astype(int)

    cols = {
        "ROW_ID": "row_id",
        "AMM": "mapped_amm",
        "NOM": "mapped_nom",
        "RCP_VERIFY_STATUS": "mapped_rcp_verify_status",
        "RCP_HTTP_STATUS": "mapped_rcp_http_status",
        "VERIFIED_RCP_URL": "mapped_verified_rcp_url",
        "RCP_SOURCE": "mapped_rcp_source",
    }

    keep = [k for k in cols if k in df.columns]
    df = df[keep].rename(columns=cols)
    for col in [
        "mapped_amm",
        "mapped_nom",
        "mapped_rcp_verify_status",
        "mapped_rcp_http_status",
        "mapped_verified_rcp_url",
        "mapped_rcp_source",
    ]:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].map(normalize_text)

    return df


def scan_lab_pdfs(base_dir: Path, folders: List[str], pages: int) -> Tuple[Dict[str, Set[str]], Dict[str, Dict[str, int]]]:
    amm_to_sources: Dict[str, Set[str]] = {}
    stats: Dict[str, Dict[str, int]] = {}

    if PdfReader is None:
        for folder in folders:
            stats[folder] = {
                "files": 0,
                "with_amm": 0,
                "unique_amm": 0,
                "errors": 0,
            }
        return amm_to_sources, stats

    logging.getLogger("pypdf").setLevel(logging.ERROR)
    logging.getLogger("pypdf._reader").setLevel(logging.ERROR)

    for folder in folders:
        folder_path = base_dir / folder
        stats[folder] = scan_single_lab_folder(folder_path, folder, pages, amm_to_sources)

    return amm_to_sources, stats


def build_output(
    merged: pd.DataFrame,
    rcp_status_by_url: Dict[str, Dict[str, str]],
    notice_status_by_url: Dict[str, Dict[str, str]],
    amm_to_sources: Dict[str, Set[str]],
) -> pd.DataFrame:
    rows: List[Dict[str, object]] = []

    for _, row in merged.iterrows():
        catalog_rcp_url = normalize_text(row.get("rcp_url", ""))
        catalog_notice_url = normalize_text(row.get("notice_url", ""))

        rcp_status = rcp_status_by_url.get(
            catalog_rcp_url,
            {"verify_status": "missing", "http_status": "", "verified_url": "", "error": ""},
        )
        notice_status = notice_status_by_url.get(
            catalog_notice_url,
            {"verify_status": "missing", "http_status": "", "verified_url": "", "error": ""},
        )

        mapped_rcp_verified = is_verified(row.get("mapped_rcp_verify_status", ""))
        catalog_rcp_verified = is_verified(rcp_status.get("verify_status", ""))
        catalog_notice_verified = is_verified(notice_status.get("verify_status", ""))

        rcp_actual_available = mapped_rcp_verified or catalog_rcp_verified
        notice_actual_available = catalog_notice_verified
        actual_detail_available = rcp_actual_available or notice_actual_available

        amm_norm = normalize_amm(row.get("amm", "") or row.get("mapped_amm", ""))
        lab_sources = sorted(amm_to_sources.get(amm_norm, set()))
        lab_detected = bool(lab_sources)

        rcp_link_provided = bool(catalog_rcp_url)
        notice_link_provided = bool(catalog_notice_url)
        rcp_link_404 = rcp_link_provided and normalize_text(rcp_status.get("http_status", "")) == "404"
        notice_link_404 = notice_link_provided and normalize_text(notice_status.get("http_status", "")) == "404"

        rows.append(
            {
                "row_id": int(row.get("row_id", 0) or 0),
                "amm": normalize_text(row.get("amm", "") or row.get("mapped_amm", "")),
                "nom": normalize_text(row.get("nom", "") or row.get("mapped_nom", "")),
                "labo": normalize_text(row.get("labo", "")),
                "pays": normalize_text(row.get("pays", "")),
                "mapped_rcp_verify_status": normalize_text(row.get("mapped_rcp_verify_status", "")),
                "mapped_rcp_http_status": normalize_text(row.get("mapped_rcp_http_status", "")),
                "mapped_verified_rcp_url": normalize_text(row.get("mapped_verified_rcp_url", "")),
                "mapped_rcp_source": normalize_text(row.get("mapped_rcp_source", "")),
                "catalog_rcp_url": catalog_rcp_url,
                "catalog_rcp_verify_status": normalize_text(rcp_status.get("verify_status", "")),
                "catalog_rcp_http_status": normalize_text(rcp_status.get("http_status", "")),
                "catalog_verified_rcp_url": normalize_text(rcp_status.get("verified_url", "")),
                "catalog_rcp_verify_error": normalize_text(rcp_status.get("error", "")),
                "catalog_notice_url": catalog_notice_url,
                "catalog_notice_verify_status": normalize_text(notice_status.get("verify_status", "")),
                "catalog_notice_http_status": normalize_text(notice_status.get("http_status", "")),
                "catalog_verified_notice_url": normalize_text(notice_status.get("verified_url", "")),
                "catalog_notice_verify_error": normalize_text(notice_status.get("error", "")),
                "rcp_actual_available": bool(rcp_actual_available),
                "notice_actual_available": bool(notice_actual_available),
                "actual_detail_available": bool(actual_detail_available),
                "lab_document_detected_by_amm": bool(lab_detected),
                "lab_document_sources": ";".join(lab_sources),
                "actual_or_lab_available": bool(actual_detail_available or lab_detected),
                "rcp_link_provided": bool(rcp_link_provided),
                "notice_link_provided": bool(notice_link_provided),
                "rcp_link_404": bool(rcp_link_404),
                "notice_link_404": bool(notice_link_404),
                "any_link_404": bool(rcp_link_404 or notice_link_404),
                "rcp_link_provided_but_not_verified": bool(rcp_link_provided and not catalog_rcp_verified),
                "notice_link_provided_but_not_verified": bool(notice_link_provided and not catalog_notice_verified),
            }
        )

    out = pd.DataFrame(rows)
    out = out.sort_values(["actual_detail_available", "nom", "amm"], ascending=[False, True, True])
    return out


def compute_summary(out_df: pd.DataFrame, lab_stats: Dict[str, Dict[str, int]]) -> Dict[str, object]:
    summary = {
        "total_medicines": int(len(out_df)),
        "rcp_actual_available": int(out_df["rcp_actual_available"].sum()),
        "notice_actual_available": int(out_df["notice_actual_available"].sum()),
        "actual_detail_available": int(out_df["actual_detail_available"].sum()),
        "actual_or_lab_available": int(out_df["actual_or_lab_available"].sum()),
        "missing_actual_detail": int((~out_df["actual_detail_available"]).sum()),
        "with_any_404_link": int(out_df["any_link_404"].sum()),
        "with_rcp_404_link": int(out_df["rcp_link_404"].sum()),
        "with_notice_404_link": int(out_df["notice_link_404"].sum()),
        "mapped_in_lab_folders_by_amm": int(out_df["lab_document_detected_by_amm"].sum()),
        "lab_folder_stats": lab_stats,
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify actual RCP/notice availability for all medicines")
    parser.add_argument("--catalog-csv", default="medicaments_all_data.csv")
    parser.add_argument("--mapped-csv", default="dpm_live_out/medicines_exact_mapped.csv")
    parser.add_argument("--output-csv", default="dpm_live_out/medicine_document_availability.csv")
    parser.add_argument("--summary-json", default="dpm_live_out/medicine_document_availability_summary.json")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--max-workers", type=int, default=12)
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--lab-folders", nargs="*", default=["medis", "teriak", "unimed"])
    parser.add_argument("--lab-pages", type=int, default=3)
    args = parser.parse_args()

    base_dir = Path.cwd()
    catalog_path = (base_dir / args.catalog_csv).resolve()
    mapped_path = (base_dir / args.mapped_csv).resolve()
    output_csv = (base_dir / args.output_csv).resolve()
    summary_json = (base_dir / args.summary_json).resolve()

    if args.insecure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    catalog = read_catalog(catalog_path)
    mapped = read_mapped(mapped_path)
    merged = catalog.merge(mapped, on="row_id", how="left")

    print(f"[INFO] Rows to evaluate: {len(merged)}")

    rcp_urls = merged["rcp_url"].tolist()
    notice_urls = merged["notice_url"].tolist()

    print(f"[INFO] Unique catalog RCP URLs: {len({normalize_text(u) for u in rcp_urls if normalize_text(u)})}")
    print(f"[INFO] Unique catalog notice URLs: {len({normalize_text(u) for u in notice_urls if normalize_text(u)})}")

    rcp_status_by_url = verify_unique_urls(
        rcp_urls,
        timeout=args.timeout,
        insecure=args.insecure,
        max_workers=args.max_workers,
    )
    notice_status_by_url = verify_unique_urls(
        notice_urls,
        timeout=args.timeout,
        insecure=args.insecure,
        max_workers=args.max_workers,
    )

    amm_to_sources, lab_stats = scan_lab_pdfs(base_dir, args.lab_folders, args.lab_pages)

    out_df = build_output(
        merged=merged,
        rcp_status_by_url=rcp_status_by_url,
        notice_status_by_url=notice_status_by_url,
        amm_to_sources=amm_to_sources,
    )

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(output_csv, index=False, encoding="utf-8-sig")

    available_csv = output_csv.with_name("medicine_document_available_only.csv")
    missing_csv = output_csv.with_name("medicine_document_missing_only.csv")
    links404_csv = output_csv.with_name("medicine_document_links_404.csv")

    out_df[out_df["actual_detail_available"]].to_csv(available_csv, index=False, encoding="utf-8-sig")
    out_df[~out_df["actual_detail_available"]].to_csv(missing_csv, index=False, encoding="utf-8-sig")
    out_df[out_df["any_link_404"]].to_csv(links404_csv, index=False, encoding="utf-8-sig")

    summary = compute_summary(out_df, lab_stats)
    summary["output_files"] = {
        "all": str(output_csv),
        "available_only": str(available_csv),
        "missing_only": str(missing_csv),
        "links_404": str(links404_csv),
    }

    summary_json.parent.mkdir(parents=True, exist_ok=True)
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[DONE] Wrote: {output_csv}")
    print(f"[DONE] Wrote: {available_csv}")
    print(f"[DONE] Wrote: {missing_csv}")
    print(f"[DONE] Wrote: {links404_csv}")
    print(f"[DONE] Wrote: {summary_json}")


if __name__ == "__main__":
    main()
