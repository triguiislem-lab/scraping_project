#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path


CDSS_TABLES = [
    "product_catalog",
    "ingredient_reference",
    "product_ingredient_link",
    "terminology_bridge",
    "formulary_status",
    "indication_rule",
    "contraindication_rule",
    "dosage_rule",
    "renal_hepatic_adjustment",
    "drug_interaction_rule",
    "adverse_effect_summary",
    "administration_rule",
    "special_population_rule",
    "substitution_rule",
    "guideline_document",
    "guideline_chunk",
    "regulatory_alert",
    "care_pathway_node",
    "care_pathway_edge",
    "pathway_recommendation_rule",
    "prescription_checklist_rule",
    "source_provenance",
    "ingestion_run",
    "review_queue",
    "data_quality_issue",
]
DOC_ROOTS = ["medis", "opalia", "teriak", "unimed", "dpm_live_out"]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def text(value: object) -> str:
    return "" if value is None else str(value).strip()


def strip_accents(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))


def norm(value: object) -> str:
    value = strip_accents(text(value)).upper()
    value = re.sub(r"[^A-Z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def norm_amm(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", text(value).upper())


def key(*values: object) -> str:
    return "|".join(norm(value) for value in values)


def truthy(value: object) -> int:
    return 1 if text(value).lower() in {"1", "true", "yes", "y", "oui"} else 0


def sid(*values: object) -> str:
    payload = "|".join(text(value) for value in values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def qident(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value)
    if not cleaned:
        raise ValueError("empty identifier")
    return cleaned


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def import_raw_csv(con: sqlite3.Connection, table: str, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for column in row:
            safe = qident(column)
            if safe not in seen:
                seen.add(safe)
                columns.append(safe)
    con.execute(f"DROP TABLE IF EXISTS {qident(table)}")
    con.execute(f"CREATE TABLE {qident(table)} ({', '.join(c + ' TEXT' for c in columns)})")
    placeholders = ", ".join("?" for _ in columns)
    con.executemany(
        f"INSERT INTO {qident(table)} ({', '.join(columns)}) VALUES ({placeholders})",
        ([text(row.get(column, "")) for column in columns] for row in rows),
    )


def load_dpm(path: Path) -> list[dict[str, str]]:
    rows = read_csv(path)
    for idx, row in enumerate(rows, 1):
        row["row_id"] = str(idx)
        row["normalized_amm"] = norm_amm(row.get("amm"))
        row["medicine_key"] = key(
            row.get("nom"),
            row.get("dosage"),
            row.get("forme"),
            row.get("presentation"),
            row.get("nom_generique"),
            row.get("labo"),
        )
    return rows


def cdss_indexes(cdss_db: Path) -> tuple[dict[str, list[sqlite3.Row]], dict[str, list[sqlite3.Row]]]:
    if not cdss_db.exists() or cdss_db.stat().st_size == 0:
        return {}, {}
    src = sqlite3.connect(cdss_db)
    src.row_factory = sqlite3.Row
    try:
        rows = src.execute(
            """
            SELECT product_id, canonical_product_id, amm, product_label, brand_name,
                   dosage_text, form_raw, presentation, dci_api, laboratory,
                   therapeutic_class, therapeutic_subclass, reimbursement_category,
                   price_public_tnd, source_dataset, source_reference
            FROM product_catalog
            """
        ).fetchall()
    finally:
        src.close()
    by_amm: dict[str, list[sqlite3.Row]] = {}
    by_key: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        amm = norm_amm(row["amm"])
        if amm:
            by_amm.setdefault(amm, []).append(row)
        product_key = key(
            row["brand_name"] or row["product_label"],
            row["dosage_text"],
            row["form_raw"],
            row["presentation"],
            row["dci_api"],
            row["laboratory"],
        )
        by_key.setdefault(product_key, []).append(row)
    return by_amm, by_key


def best_match(row: dict[str, str], by_amm: dict[str, list[sqlite3.Row]], by_key: dict[str, list[sqlite3.Row]]):
    amm = row["normalized_amm"]
    if amm and amm in by_amm:
        candidates = by_amm[amm]
        if len(candidates) == 1:
            return "amm_exact", candidates[0], 1.0
        for candidate in candidates:
            candidate_key = key(
                candidate["brand_name"] or candidate["product_label"],
                candidate["dosage_text"],
                candidate["form_raw"],
                candidate["presentation"],
                candidate["dci_api"],
                candidate["laboratory"],
            )
            if candidate_key == row["medicine_key"]:
                return "amm_and_composite_exact", candidate, 1.0
        return "amm_exact_multi", candidates[0], 0.98
    if row["medicine_key"] in by_key:
        return "composite_exact", by_key[row["medicine_key"]][0], 0.94
    return "missing", None, 0.0


def init_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE source_manifest (
            source_id TEXT PRIMARY KEY,
            source_name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_path TEXT NOT NULL,
            exists_flag INTEGER NOT NULL,
            size_bytes INTEGER,
            row_count INTEGER,
            imported_at TEXT NOT NULL
        );

        CREATE TABLE medicine_master (
            row_id INTEGER PRIMARY KEY,
            medicine_key TEXT NOT NULL,
            normalized_amm TEXT,
            nom TEXT,
            dosage TEXT,
            forme TEXT,
            presentation TEXT,
            nom_generique TEXT,
            labo TEXT,
            pays TEXT,
            amm TEXT,
            date_amm TEXT,
            generic_princeps_biosimilar TEXT,
            detail_url TEXT,
            rcp_url TEXT,
            notice_url TEXT,
            has_detail INTEGER NOT NULL,
            has_rcp INTEGER NOT NULL,
            has_notice INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE medicine_cdss_match (
            row_id INTEGER PRIMARY KEY REFERENCES medicine_master(row_id),
            cdss_product_id TEXT,
            cdss_canonical_product_id TEXT,
            match_type TEXT NOT NULL,
            match_score REAL NOT NULL,
            cdss_product_label TEXT,
            cdss_dci_api TEXT,
            cdss_therapeutic_class TEXT,
            cdss_therapeutic_subclass TEXT,
            cdss_source_dataset TEXT,
            cdss_source_reference TEXT,
            cdss_reimbursement_category TEXT,
            cdss_price_public_tnd REAL
        );

        CREATE TABLE local_document_file (
            document_id TEXT PRIMARY KEY,
            source_root TEXT,
            document_kind TEXT,
            file_path TEXT NOT NULL,
            file_extension TEXT,
            size_bytes INTEGER,
            matched_row_ids TEXT,
            matched_amms TEXT,
            match_method TEXT,
            match_score REAL
        );

        CREATE TABLE rcp_document_section (
            section_id TEXT PRIMARY KEY,
            row_id INTEGER NOT NULL REFERENCES medicine_master(row_id),
            amm TEXT,
            nom TEXT,
            section_kind TEXT NOT NULL,
            section_title TEXT,
            section_text TEXT NOT NULL,
            source_path TEXT,
            text_chars INTEGER NOT NULL
        );

        CREATE INDEX idx_medicine_amm ON medicine_master(normalized_amm);
        CREATE INDEX idx_match_product ON medicine_cdss_match(cdss_product_id);
        """
    )


def copy_cdss(con: sqlite3.Connection, cdss_db: Path) -> dict[str, int]:
    if not cdss_db.exists() or cdss_db.stat().st_size == 0:
        return {}
    counts: dict[str, int] = {}
    con.execute("ATTACH DATABASE ? AS cdss", (str(cdss_db),))
    try:
        for table in CDSS_TABLES:
            exists = con.execute(
                "SELECT 1 FROM cdss.sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            target = qident(f"cdss_{table}")
            con.execute(f"CREATE TABLE {target} AS SELECT * FROM cdss.{qident(table)}")
            counts[target] = con.execute(f"SELECT COUNT(*) FROM {target}").fetchone()[0]
    finally:
        con.execute("DETACH DATABASE cdss")
    for sql in [
        "CREATE INDEX idx_cdss_pc_pid ON cdss_product_catalog(product_id)",
        "CREATE INDEX idx_cdss_pil_pid ON cdss_product_ingredient_link(product_id)",
        "CREATE INDEX idx_cdss_pil_iid ON cdss_product_ingredient_link(ingredient_id)",
        "CREATE INDEX idx_cdss_ind_pid ON cdss_indication_rule(product_id)",
        "CREATE INDEX idx_cdss_ind_iid ON cdss_indication_rule(ingredient_id)",
        "CREATE INDEX idx_cdss_dose_pid ON cdss_dosage_rule(product_id)",
        "CREATE INDEX idx_cdss_dose_iid ON cdss_dosage_rule(ingredient_id)",
        "CREATE INDEX idx_cdss_contra_pid ON cdss_contraindication_rule(product_id)",
        "CREATE INDEX idx_cdss_contra_iid ON cdss_contraindication_rule(ingredient_id)",
        "CREATE INDEX idx_cdss_ddi_i1 ON cdss_drug_interaction_rule(ingredient_1_id)",
        "CREATE INDEX idx_cdss_ddi_i2 ON cdss_drug_interaction_rule(ingredient_2_id)",
        "CREATE INDEX idx_cdss_ae_iid ON cdss_adverse_effect_summary(ingredient_id)",
        "CREATE INDEX idx_cdss_adj_iid ON cdss_renal_hepatic_adjustment(ingredient_id)",
        "CREATE INDEX idx_cdss_pop_iid ON cdss_special_population_rule(ingredient_id)",
        "CREATE INDEX idx_cdss_admin_iid ON cdss_administration_rule(ingredient_id)",
        "CREATE INDEX idx_cdss_sub_src ON cdss_substitution_rule(source_product_id)",
        "CREATE INDEX idx_cdss_sub_tgt ON cdss_substitution_rule(target_product_id)",
    ]:
        try:
            con.execute(sql)
        except sqlite3.OperationalError:
            pass
    return counts


def insert_catalog(con: sqlite3.Connection, dpm_rows: list[dict[str, str]], matches) -> None:
    for row in dpm_rows:
        row_id = int(row["row_id"])
        con.execute(
            """
            INSERT INTO medicine_master VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                row_id,
                row["medicine_key"],
                row["normalized_amm"],
                text(row.get("nom")),
                text(row.get("dosage")),
                text(row.get("forme")),
                text(row.get("presentation")),
                text(row.get("nom_generique")),
                text(row.get("labo")),
                text(row.get("pays")),
                text(row.get("amm")),
                text(row.get("date_amm")),
                text(row.get("g_p")),
                text(row.get("detail_url")),
                text(row.get("rcp_url")),
                text(row.get("notice_url")),
                truthy(row.get("has_detail")),
                truthy(row.get("has_rcp")),
                truthy(row.get("has_notice")),
                now_utc(),
            ),
        )
        match_type, match, score = matches[row_id]
        con.execute(
            """
            INSERT INTO medicine_cdss_match VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                match["product_id"] if match else None,
                match["canonical_product_id"] if match else None,
                match_type,
                score,
                match["product_label"] if match else None,
                match["dci_api"] if match else None,
                match["therapeutic_class"] if match else None,
                match["therapeutic_subclass"] if match else None,
                match["source_dataset"] if match else None,
                match["source_reference"] if match else None,
                match["reimbursement_category"] if match else None,
                match["price_public_tnd"] if match else None,
            ),
        )


def import_documents(con: sqlite3.Connection, root: Path, scan_rows: list[dict[str, str]]) -> None:
    seen: set[str] = set()
    for row in scan_rows:
        file_path = text(row.get("pdf_path"))
        if not file_path:
            continue
        path = Path(file_path)
        seen.add(str(path.resolve()) if path.exists() else file_path)
        con.execute(
            "INSERT OR REPLACE INTO local_document_file VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid("scan", file_path),
                text(row.get("source")),
                text(row.get("doc_kind")),
                file_path,
                path.suffix.lower(),
                path.stat().st_size if path.exists() else None,
                text(row.get("mapped_row_ids")),
                text(row.get("amm_matches")),
                text(row.get("name_match_method")),
                text(row.get("name_match_score")) or None,
            ),
        )
    for root_name in DOC_ROOTS:
        folder = root / root_name
        if not folder.exists():
            continue
        for path in folder.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".pdf", ".txt"}:
                continue
            resolved = str(path.resolve())
            if resolved in seen:
                continue
            con.execute(
                "INSERT OR IGNORE INTO local_document_file VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sid("file", resolved),
                    root_name,
                    None,
                    str(path),
                    path.suffix.lower(),
                    path.stat().st_size,
                    "",
                    "",
                    "file_index_only",
                    None,
                ),
            )


def import_rcp_sections(con: sqlite3.Connection, rows: list[dict[str, str]]) -> None:
    sections = [
        ("indication", "RCP_INDICATIONS_TITLE", "RCP_INDICATIONS_TEXT"),
        ("dosage", "RCP_POSOLOGIE_TITLE", "RCP_POSOLOGIE_TEXT"),
        ("contraindication", "RCP_CONTRE_INDICATIONS_TITLE", "RCP_CONTRE_INDICATIONS_TEXT"),
        ("full_text", "RCP_SECTION_TITLES", "RCP_FULL_TEXT"),
    ]
    for row in rows:
        row_id = text(row.get("ROW_ID"))
        if not row_id.isdigit():
            continue
        source = text(row.get("RCP_TEXT_PATH")) or text(row.get("DOWNLOADED_RCP_FILE"))
        for section, title_col, text_col in sections:
            body = text(row.get(text_col))
            if not body:
                continue
            con.execute(
                "INSERT OR REPLACE INTO rcp_document_section VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sid(row_id, section, source, body[:100]),
                    int(row_id),
                    text(row.get("AMM")),
                    text(row.get("NOM")),
                    section,
                    text(row.get(title_col)),
                    body,
                    source,
                    len(body),
                ),
            )


def create_coverage(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE medicine_ingredient AS
        SELECT m.row_id, pil.ingredient_id
        FROM medicine_cdss_match m
        JOIN cdss_product_ingredient_link pil ON pil.product_id = m.cdss_product_id;
        CREATE INDEX idx_mi_row ON medicine_ingredient(row_id);
        CREATE INDEX idx_mi_ing ON medicine_ingredient(ingredient_id);

        CREATE TABLE medicine_clinical_coverage AS
        SELECT
            mm.row_id,
            COALESCE(CAST(lc.local_any_document_available AS INTEGER), 0) AS local_any_document_available,
            COALESCE(CAST(lc.local_rcp_available AS INTEGER), 0) AS local_rcp_available,
            COALESCE(CAST(lc.local_notice_available AS INTEGER), 0) AS local_notice_available,
            CASE WHEN EXISTS (
                SELECT 1 FROM rcp_document_section s
                WHERE s.row_id = mm.row_id AND s.section_kind = 'full_text'
            ) THEN 1 ELSE 0 END AS dpm_rcp_text_available,
            CASE WHEN m.cdss_product_id IS NOT NULL THEN 1 ELSE 0 END AS cdss_product_available,
            (SELECT COUNT(*) FROM medicine_ingredient mi WHERE mi.row_id = mm.row_id) AS ingredient_link_count,
            (SELECT COUNT(*) FROM cdss_indication_rule r
             WHERE r.product_id = m.cdss_product_id
                OR r.ingredient_id IN (SELECT ingredient_id FROM medicine_ingredient WHERE row_id = mm.row_id)) AS indication_rule_count,
            (SELECT COUNT(*) FROM cdss_dosage_rule r
             WHERE r.product_id = m.cdss_product_id
                OR r.ingredient_id IN (SELECT ingredient_id FROM medicine_ingredient WHERE row_id = mm.row_id)) AS dosage_rule_count,
            (SELECT COUNT(*) FROM cdss_contraindication_rule r
             WHERE r.product_id = m.cdss_product_id
                OR r.ingredient_id IN (SELECT ingredient_id FROM medicine_ingredient WHERE row_id = mm.row_id)) AS contraindication_rule_count,
            (SELECT COUNT(*) FROM cdss_drug_interaction_rule r
             WHERE r.ingredient_1_id IN (SELECT ingredient_id FROM medicine_ingredient WHERE row_id = mm.row_id)
                OR r.ingredient_2_id IN (SELECT ingredient_id FROM medicine_ingredient WHERE row_id = mm.row_id)) AS interaction_rule_count,
            (SELECT COUNT(*) FROM cdss_adverse_effect_summary r
             WHERE r.ingredient_id IN (SELECT ingredient_id FROM medicine_ingredient WHERE row_id = mm.row_id)) AS adverse_effect_count,
            (SELECT COUNT(*) FROM cdss_renal_hepatic_adjustment r
             WHERE r.product_id = m.cdss_product_id
                OR r.ingredient_id IN (SELECT ingredient_id FROM medicine_ingredient WHERE row_id = mm.row_id)) AS renal_hepatic_adjustment_count,
            (SELECT COUNT(*) FROM cdss_special_population_rule r
             WHERE r.product_id = m.cdss_product_id
                OR r.ingredient_id IN (SELECT ingredient_id FROM medicine_ingredient WHERE row_id = mm.row_id)) AS special_population_rule_count,
            (SELECT COUNT(*) FROM cdss_administration_rule r
             WHERE r.product_id = m.cdss_product_id
                OR r.ingredient_id IN (SELECT ingredient_id FROM medicine_ingredient WHERE row_id = mm.row_id)) AS administration_rule_count,
            (SELECT COUNT(*) FROM cdss_substitution_rule r
             WHERE r.source_product_id = m.cdss_product_id OR r.target_product_id = m.cdss_product_id) AS substitution_rule_count
        FROM medicine_master mm
        LEFT JOIN medicine_cdss_match m ON m.row_id = mm.row_id
        LEFT JOIN raw_local_document_coverage lc ON CAST(lc.row_id AS INTEGER) = mm.row_id;

        ALTER TABLE medicine_clinical_coverage ADD COLUMN automatic_prescription_readiness TEXT;
        UPDATE medicine_clinical_coverage
        SET automatic_prescription_readiness = CASE
            WHEN local_any_document_available = 1
             AND cdss_product_available = 1
             AND indication_rule_count > 0
             AND dosage_rule_count > 0
             AND contraindication_rule_count > 0
             AND interaction_rule_count > 0 THEN 'high'
            WHEN cdss_product_available = 1
             AND (indication_rule_count > 0 OR dosage_rule_count > 0)
             AND (contraindication_rule_count > 0 OR interaction_rule_count > 0) THEN 'medium'
            ELSE 'low'
        END;
        CREATE INDEX idx_mcc_ready ON medicine_clinical_coverage(automatic_prescription_readiness);
        """
    )


def manifest(con: sqlite3.Connection, name: str, typ: str, path: Path, rows: int | None) -> None:
    con.execute(
        "INSERT OR REPLACE INTO source_manifest VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sid(name, path),
            name,
            typ,
            str(path),
            1 if path.exists() else 0,
            path.stat().st_size if path.exists() else None,
            rows,
            now_utc(),
        ),
    )


def scalar(con: sqlite3.Connection, sql: str) -> int:
    return int(con.execute(sql).fetchone()[0])


def summary(con: sqlite3.Connection, output_db: Path, cdss_counts: dict[str, int]) -> dict[str, object]:
    readiness = dict(con.execute(
        "SELECT automatic_prescription_readiness, COUNT(*) FROM medicine_clinical_coverage GROUP BY 1"
    ).fetchall())
    matches = dict(con.execute("SELECT match_type, COUNT(*) FROM medicine_cdss_match GROUP BY 1").fetchall())
    return {
        "generated_at": now_utc(),
        "output_db": str(output_db),
        "output_db_bytes": output_db.stat().st_size if output_db.exists() else 0,
        "medicine_rows": scalar(con, "SELECT COUNT(*) FROM medicine_master"),
        "cdss_matched_rows": scalar(con, "SELECT COUNT(*) FROM medicine_cdss_match WHERE cdss_product_id IS NOT NULL"),
        "local_document_files": scalar(con, "SELECT COUNT(*) FROM local_document_file"),
        "rcp_document_sections": scalar(con, "SELECT COUNT(*) FROM rcp_document_section"),
        "match_distribution": {k: int(v) for k, v in matches.items()},
        "readiness_distribution": {k: int(v) for k, v in readiness.items()},
        "coverage": {
            "local_any_document_rows": scalar(con, "SELECT COUNT(*) FROM medicine_clinical_coverage WHERE local_any_document_available = 1"),
            "dpm_rcp_text_rows": scalar(con, "SELECT COUNT(*) FROM medicine_clinical_coverage WHERE dpm_rcp_text_available = 1"),
            "indication_rows": scalar(con, "SELECT COUNT(*) FROM medicine_clinical_coverage WHERE indication_rule_count > 0"),
            "dosage_rows": scalar(con, "SELECT COUNT(*) FROM medicine_clinical_coverage WHERE dosage_rule_count > 0"),
            "contraindication_rows": scalar(con, "SELECT COUNT(*) FROM medicine_clinical_coverage WHERE contraindication_rule_count > 0"),
            "interaction_rows": scalar(con, "SELECT COUNT(*) FROM medicine_clinical_coverage WHERE interaction_rule_count > 0"),
            "adverse_effect_rows": scalar(con, "SELECT COUNT(*) FROM medicine_clinical_coverage WHERE adverse_effect_count > 0"),
            "renal_hepatic_rows": scalar(con, "SELECT COUNT(*) FROM medicine_clinical_coverage WHERE renal_hepatic_adjustment_count > 0"),
            "special_population_rows": scalar(con, "SELECT COUNT(*) FROM medicine_clinical_coverage WHERE special_population_rule_count > 0"),
            "substitution_rows": scalar(con, "SELECT COUNT(*) FROM medicine_clinical_coverage WHERE substitution_rule_count > 0"),
        },
        "copied_cdss_tables": cdss_counts,
    }


def build(args: argparse.Namespace) -> dict[str, object]:
    root = Path(args.repo_root).resolve()
    output_db = (root / args.output_db).resolve()
    output_summary = (root / args.output_summary).resolve()
    output_db.parent.mkdir(parents=True, exist_ok=True)
    if output_db.exists():
        output_db.unlink()

    paths = {
        "dpm": (root / args.dpm_csv).resolve(),
        "coverage": (root / args.local_coverage_csv).resolve(),
        "scan": (root / args.local_pdf_scan_csv).resolve(),
        "manifest": (root / args.rcp_manifest_csv).resolve(),
        "texts": (root / args.rcp_text_csv).resolve(),
        "cdss": (root / args.cdss_db).resolve(),
    }
    dpm_rows = load_dpm(paths["dpm"])
    coverage_rows = read_csv(paths["coverage"])
    scan_rows = read_csv(paths["scan"])
    manifest_rows = read_csv(paths["manifest"])
    text_rows = read_csv(paths["texts"])
    by_amm, by_key = cdss_indexes(paths["cdss"])
    matches = {int(row["row_id"]): best_match(row, by_amm, by_key) for row in dpm_rows}

    con = sqlite3.connect(output_db)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        init_schema(con)
        import_raw_csv(con, "raw_medicaments_all_data", dpm_rows)
        import_raw_csv(con, "raw_local_document_coverage", coverage_rows)
        import_raw_csv(con, "raw_local_pdf_scan_report", scan_rows)
        import_raw_csv(con, "raw_dpm_rcp_manifest", manifest_rows)
        import_raw_csv(con, "raw_dpm_rcp_text_extracts", text_rows)
        insert_catalog(con, dpm_rows, matches)
        cdss_counts = copy_cdss(con, paths["cdss"])
        import_documents(con, root, scan_rows)
        import_rcp_sections(con, text_rows)
        create_coverage(con)
        for name, typ, path, rows in [
            ("DPM medicines CSV", "catalog", paths["dpm"], len(dpm_rows)),
            ("Local document coverage", "coverage", paths["coverage"], len(coverage_rows)),
            ("Local PDF scan report", "coverage", paths["scan"], len(scan_rows)),
            ("DPM RCP manifest", "document_manifest", paths["manifest"], len(manifest_rows)),
            ("DPM RCP text extracts", "document_text", paths["texts"], len(text_rows)),
            ("CDSS production database", "clinical_database", paths["cdss"], sum(cdss_counts.values())),
        ]:
            manifest(con, name, typ, path, rows)
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        payload = summary(con, output_db, cdss_counts)
    finally:
        con.close()
    output_summary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the automatic prescription master SQLite database.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--dpm-csv", default="medicaments_all_data.csv")
    parser.add_argument("--local-coverage-csv", default="dpm_live_out/local_document_coverage.csv")
    parser.add_argument("--local-pdf-scan-csv", default="dpm_live_out/local_pdf_scan_report.csv")
    parser.add_argument("--rcp-manifest-csv", default="dpm_live_out/checkpoint_rcp_manifest.csv")
    parser.add_argument("--rcp-text-csv", default="dpm_live_out/checkpoint_rcp_text_extracts.csv")
    parser.add_argument("--cdss-db", default="cdss_tn_prod/artifacts/cdss_tn.db")
    parser.add_argument("--output-db", default="dpm_live_out/automatic_prescription_master.db")
    parser.add_argument("--output-summary", default="dpm_live_out/automatic_prescription_master_summary.json")
    return parser.parse_args()


def main() -> None:
    print(json.dumps(build(parse_args()), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
