#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set

import pandas as pd
import requests
import urllib3

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

DPM_BASE = "https://dpm.tn"

EXACT_COLUMNS = [
    "DIRECT_RCP_URL", "RCP_VERIFY_STATUS", "RCP_HTTP_STATUS", "RCP_SOURCE",
    "VERIFIED_RCP_URL", "DOWNLOADED_RCP_FILE", "RCP_SHA256", "RCP_BYTES",
    "RCP_TEXT_PATH", "RCP_TEXT_CHARS", "RCP_EXTRACT_STATUS", "RCP_SECTION_TITLES",
    "RCP_INDICATIONS_TITLE", "RCP_POSOLOGIE_TITLE", "RCP_CONTRE_INDICATIONS_TITLE",
    "LAST_CHECK_UTC"
]


def normalize_text(value: object) -> str:
    if value is None:
        return ""
    s = str(value).strip().replace("\xa0", " ")
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


def now_utc() -> str:
    return pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def absolutize_dpm_url(value: object) -> str:
    s = normalize_text(value)
    if not s:
        return ""

    s = s.replace("\\", "/")

    if s.startswith("http://") or s.startswith("https://"):
        return s

    if s.startswith("../../"):
        return f"{DPM_BASE}/{s[6:]}"
    if s.startswith("../"):
        return f"{DPM_BASE}/{s[3:]}"
    if s.startswith("./"):
        return f"{DPM_BASE}/{s[2:]}"
    if s.startswith("/"):
        return f"{DPM_BASE}{s}"

    return f"{DPM_BASE}/{s.lstrip('/')}"


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
                print(f"[NET] {method.upper()} {url} -> {resp.status_code} ({resp.url})", flush=True)
            return resp
        except requests.RequestException as exc:
            if verbose:
                print(f"[NET] {method.upper()} {url} -> EXC {type(exc).__name__}: {exc}", flush=True)
            if attempt == 2:
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def load_registry(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".xlsx":
        df = pd.read_excel(path, sheet_name=0, dtype=str)
    else:
        sep = "\t" if path.suffix.lower() in {".tsv", ".txt"} else ","
        df = pd.read_csv(path, sep=sep, dtype=str)

    df = df.copy()

    if "ROW_ID" not in df.columns:
        df.insert(0, "ROW_ID", range(1, len(df) + 1))
    else:
        df["ROW_ID"] = pd.to_numeric(df["ROW_ID"], errors="coerce").fillna(0).astype(int)

    for col in ["NOM", "DOSAGE", "FORME", "PRESENTATION", "NOM GENERIQUE", "AMM"]:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").map(normalize_text)

    for c in EXACT_COLUMNS:
        if c not in df.columns:
            df[c] = ""
        df[c] = df[c].fillna("").map(normalize_text)

    if "RCP_URL_GUESS_GENERIC" not in df.columns:
        df["RCP_URL_GUESS_GENERIC"] = ""
    else:
        df["RCP_URL_GUESS_GENERIC"] = df["RCP_URL_GUESS_GENERIC"].fillna("").map(normalize_text)

    if "RCP_URL_GUESS_NOM" not in df.columns:
        df["RCP_URL_GUESS_NOM"] = ""
    else:
        df["RCP_URL_GUESS_NOM"] = df["RCP_URL_GUESS_NOM"].fillna("").map(normalize_text)

    return df


def load_processed_row_ids(output_dir: Path, skip_all_processed: bool = False) -> Set[int]:
    """
    By default, skip only rows already VERIFIED or already DOWNLOADED.
    This allows previously missing rows to be retried after patching DIRECT_RCP_URL.
    If skip_all_processed=True, old behavior is restored: skip every row seen in manifest.
    """
    processed: Set[int] = set()

    for name in ["checkpoint_rcp_manifest.csv", "rcp_manifest.csv"]:
        path = output_dir / name
        if not path.exists():
            continue

        try:
            df = pd.read_csv(path, dtype=str).fillna("")
        except Exception:
            continue

        if "ROW_ID" not in df.columns:
            continue

        df["ROW_ID"] = pd.to_numeric(df["ROW_ID"], errors="coerce")
        df = df[df["ROW_ID"].notna()].copy()
        df["ROW_ID"] = df["ROW_ID"].astype(int)

        if skip_all_processed:
            processed.update(df["ROW_ID"].tolist())
            continue

        status_col = df["RCP_VERIFY_STATUS"] if "RCP_VERIFY_STATUS" in df.columns else ""
        file_col = df["DOWNLOADED_RCP_FILE"] if "DOWNLOADED_RCP_FILE" in df.columns else ""
        verified_url_col = df["VERIFIED_RCP_URL"] if "VERIFIED_RCP_URL" in df.columns else ""

        keep_mask = (
            status_col.astype(str).str.lower().eq("verified")
            | file_col.astype(str).str.strip().ne("")
            | verified_url_col.astype(str).str.strip().ne("")
        )
        processed.update(df.loc[keep_mask, "ROW_ID"].tolist())

    return processed


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


def build_rcp_candidates(row: pd.Series) -> List[Tuple[str, str]]:
    candidates: List[Tuple[str, str]] = []
    seen: Set[str] = set()

    def add(source: str, url: object) -> None:
        abs_url = absolutize_dpm_url(url)
        if abs_url and abs_url not in seen:
            seen.add(abs_url)
            candidates.append((source, abs_url))

    # 1) Best source: exact direct listing link propagated into working file
    add("listing_direct_rcp", row.get("DIRECT_RCP_URL", ""))

    # 2) Existing guessed/derived URLs already in dataset
    add("generic_guess", row.get("RCP_URL_GUESS_GENERIC", ""))
    add("nom_guess", row.get("RCP_URL_GUESS_NOM", ""))

    # 3) Fallback guesses
    generic_slug = slugify(row.get("NOM GENERIQUE", ""))
    nom_slug = slugify(row.get("NOM", ""))
    dose_slug = re.sub(
        r"[^a-z0-9]+",
        "",
        strip_accents(normalize_text(row.get("DOSAGE", ""))).lower().replace("µ", "u")
    )

    if generic_slug and dose_slug:
        add("generic_guess_fallback", f"/images/rcp/{generic_slug}_{dose_slug}_rcp.pdf")
    if nom_slug and dose_slug:
        add("nom_guess_fallback", f"/images/rcp/{nom_slug}_{dose_slug}_rcp.pdf")
    if generic_slug:
        add("generic_no_dose", f"/images/rcp/{generic_slug}_rcp.pdf")
    if nom_slug:
        add("nom_no_dose", f"/images/rcp/{nom_slug}_rcp.pdf")

    return candidates


def verify_pdf_url(
    session: requests.Session,
    url: str,
    out_dir: Path,
    filename_stem: str,
    download: bool,
    verbose: bool = False
) -> Dict[str, str]:
    result = {
        "RCP_VERIFY_STATUS": "missing",
        "RCP_HTTP_STATUS": "",
        "VERIFIED_RCP_URL": "",
        "DOWNLOADED_RCP_FILE": "",
        "RCP_SHA256": "",
        "RCP_BYTES": "",
    }

    head = try_request(session, "HEAD", url, allow_redirects=True, verbose=verbose)
    if head is not None:
        result["RCP_HTTP_STATUS"] = str(head.status_code)

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

        result["RCP_HTTP_STATUS"] = str(get.status_code)
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
                if not chunk:
                    continue

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

        result["DOWNLOADED_RCP_FILE"] = str(path)
        result["RCP_BYTES"] = str(path.stat().st_size)
        result["RCP_SHA256"] = sha256_of_path(path)

    result["RCP_VERIFY_STATUS"] = "verified"
    result["VERIFIED_RCP_URL"] = final_url
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


def extract_and_save_rcp_text(pdf_path: Path, text_dir: Path, file_stem: str) -> Dict[str, str]:
    out = {
        "RCP_TEXT_PATH": "",
        "RCP_TEXT_CHARS": "",
        "RCP_EXTRACT_STATUS": "not_extracted",
    }

    text, status = extract_pdf_text(pdf_path)
    out["RCP_EXTRACT_STATUS"] = status

    if status != "ok":
        return out

    text = text.encode("utf-8", "ignore").decode("utf-8", "ignore")

    text_dir.mkdir(parents=True, exist_ok=True)
    txt_path = text_dir / f"{file_stem}.txt"
    txt_path.write_text(text, encoding="utf-8", errors="ignore")

    out["RCP_TEXT_PATH"] = str(txt_path)
    out["RCP_TEXT_CHARS"] = str(len(text))
    return out


def save_checkpoint(output_dir: Path, registry: pd.DataFrame, rcp_rows: List[dict]):
    registry.to_csv(output_dir / "checkpoint_medicines_exact_mapped.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(rcp_rows).to_csv(output_dir / "checkpoint_rcp_manifest.csv", index=False, encoding="utf-8-sig")


def main():
    p = argparse.ArgumentParser(description="Resume/fill RCP verification and download from working CSV")
    p.add_argument("--input-xlsx", required=True, help="checkpoint_medicines_exact_mapped.csv or equivalent")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--download-rcp", action="store_true")
    p.add_argument("--extract-text", action="store_true")
    p.add_argument("--delay", type=float, default=2.5)
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--verbose-net", action="store_true")
    p.add_argument("--resume-rcp", action="store_true")
    p.add_argument("--checkpoint-every-rcp", type=int, default=25)
    p.add_argument(
        "--skip-all-processed",
        action="store_true",
        help="Old behavior: when resuming, skip every ROW_ID already present in manifest. "
             "Default behavior retries rows previously marked missing."
    )
    args = p.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    registry = load_registry(Path(args.input_xlsx))
    processed = (
        load_processed_row_ids(output_dir, skip_all_processed=args.skip_all_processed)
        if args.resume_rcp else set()
    )

    session = ensure_session(insecure=args.insecure)

    rcp_rows: List[dict] = []
    existing_manifest = output_dir / "checkpoint_rcp_manifest.csv"
    if existing_manifest.exists() and args.resume_rcp:
        try:
            rcp_rows = pd.read_csv(existing_manifest, dtype=str).fillna("").to_dict("records")
        except Exception:
            rcp_rows = []

    pdf_dir = output_dir / "rcp_pdfs"
    text_dir = output_dir / "rcp_texts"

    done_count = 0
    attempted_count = 0

    for idx, row in registry.iterrows():
        row_id = int(row.get("ROW_ID", 0))

        if row_id in processed:
            if args.verbose_net:
                print(f"[SKIP] ROW_ID={row_id} already verified/downloaded in manifest", flush=True)
            continue

        candidates = build_rcp_candidates(row)
        if not candidates:
            if args.verbose_net:
                print(f"[SKIP] ROW_ID={row_id} no RCP candidate URLs", flush=True)
            continue

        attempted_count += 1
        verified = None
        chosen_source = ""

        file_stem_base = row.get("AMM") or row.get("NOM") or str(row_id)
        file_stem = re.sub(r"[^a-z0-9_]+", "_", slugify(file_stem_base))[:120]
        if not file_stem:
            file_stem = f"row_{row_id}"

        if args.verbose_net:
            print(
                f"[ROW] ROW_ID={row_id} NOM={row.get('NOM', '')} "
                f"AMM={row.get('AMM', '')} CANDIDATES={len(candidates)}",
                flush=True
            )

        for source, url in candidates:
            if args.verbose_net:
                print(f"[TRY] ROW_ID={row_id} source={source} url={url}", flush=True)

            result = verify_pdf_url(
                session=session,
                url=url,
                out_dir=pdf_dir,
                filename_stem=file_stem,
                download=args.download_rcp,
                verbose=args.verbose_net
            )

            if result["RCP_VERIFY_STATUS"] == "verified":
                verified = result
                chosen_source = source
                break

            time.sleep(max(args.delay / 2, 0.1))

        manifest_row = {
            "ROW_ID": row_id,
            "NOM": row.get("NOM", ""),
            "AMM": row.get("AMM", ""),
            "RCP_SOURCE": chosen_source,
        }

        if verified:
            registry.at[idx, "RCP_VERIFY_STATUS"] = verified["RCP_VERIFY_STATUS"]
            registry.at[idx, "RCP_HTTP_STATUS"] = verified["RCP_HTTP_STATUS"]
            registry.at[idx, "VERIFIED_RCP_URL"] = verified["VERIFIED_RCP_URL"]
            registry.at[idx, "DOWNLOADED_RCP_FILE"] = verified["DOWNLOADED_RCP_FILE"]
            registry.at[idx, "RCP_SHA256"] = verified["RCP_SHA256"]
            registry.at[idx, "RCP_BYTES"] = verified["RCP_BYTES"]
            registry.at[idx, "RCP_SOURCE"] = chosen_source

            if args.extract_text and verified.get("DOWNLOADED_RCP_FILE"):
                extracted = extract_and_save_rcp_text(
                    Path(verified["DOWNLOADED_RCP_FILE"]),
                    text_dir,
                    file_stem
                )
                registry.at[idx, "RCP_TEXT_PATH"] = extracted["RCP_TEXT_PATH"]
                registry.at[idx, "RCP_TEXT_CHARS"] = extracted["RCP_TEXT_CHARS"]
                registry.at[idx, "RCP_EXTRACT_STATUS"] = extracted["RCP_EXTRACT_STATUS"]

            registry.at[idx, "LAST_CHECK_UTC"] = now_utc()
            manifest_row.update(verified)

            if args.verbose_net:
                print(
                    f"[OK] ROW_ID={row_id} source={chosen_source} "
                    f"url={verified['VERIFIED_RCP_URL']}",
                    flush=True
                )
        else:
            registry.at[idx, "RCP_VERIFY_STATUS"] = "missing"
            registry.at[idx, "LAST_CHECK_UTC"] = now_utc()
            manifest_row.update({
                "RCP_VERIFY_STATUS": "missing",
                "RCP_HTTP_STATUS": "",
                "VERIFIED_RCP_URL": "",
                "DOWNLOADED_RCP_FILE": "",
                "RCP_SHA256": "",
                "RCP_BYTES": "",
            })

            if args.verbose_net:
                print(f"[MISS] ROW_ID={row_id} no valid PDF found", flush=True)

        rcp_rows.append(manifest_row)
        done_count += 1

        if args.checkpoint_every_rcp and done_count % args.checkpoint_every_rcp == 0:
            save_checkpoint(output_dir, registry, rcp_rows)
            print(f"[CHECKPOINT] Saved partial RCP data after {done_count} new rows", flush=True)

        time.sleep(max(args.delay / 2, 0.1))

    save_checkpoint(output_dir, registry, rcp_rows)
    registry.to_csv(output_dir / "medicines_exact_mapped.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(rcp_rows).to_csv(output_dir / "rcp_manifest.csv", index=False, encoding="utf-8-sig")

    print(
        f"[DONE] RCP resume completed. Output dir: {output_dir} | attempted_rows={attempted_count} | new_rows_written={done_count}",
        flush=True
    )


if __name__ == "__main__":
    main()