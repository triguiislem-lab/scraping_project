#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import shutil
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


SRC_DIR = Path("final_data_release")
OUT_DIR = Path("final_data_release_corrected_v2")
SRC_DB = SRC_DIR / "final_data_release.db"
OUT_DB = OUT_DIR / "final_data_release_corrected.db"

METADATA_KINDS = {"identity", "presentation", "generic_group", "document_reference"}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_out_dir() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)


def copy_release_db() -> None:
    ensure_out_dir()
    for leftover in [
        OUT_DB,
        OUT_DIR / f"{OUT_DB.name}-journal",
        OUT_DIR / f"{OUT_DB.name}-wal",
        OUT_DIR / f"{OUT_DB.name}-shm",
    ]:
        if leftover.exists():
            leftover.unlink()
    shutil.copy2(SRC_DB, OUT_DB)


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(OUT_DB)
    con.row_factory = sqlite3.Row
    return con


def append_flag_sql(flag: str) -> str:
    return (
        "CASE "
        "WHEN quality_flags IS NULL OR quality_flags='' OR quality_flags='ok' THEN ? "
        "WHEN instr(';' || quality_flags || ';', ';' || ? || ';') = 0 THEN quality_flags || ';' || ? "
        "ELSE quality_flags END"
    )


def apply_corrections(con: sqlite3.Connection) -> dict[str, int]:
    before = dict(
        total_evidence=con.execute("SELECT COUNT(*) FROM evidence_sections").fetchone()[0],
        accepted_evidence=con.execute(
            "SELECT COUNT(*) FROM evidence_sections WHERE accepted_for_clinical_use='1'"
        ).fetchone()[0],
        accepted_medicines=con.execute(
            "SELECT COUNT(DISTINCT row_id) FROM evidence_sections WHERE accepted_for_clinical_use='1'"
        ).fetchone()[0],
    )

    con.execute("DROP TABLE IF EXISTS correction_action_log")
    con.execute(
        """
        CREATE TABLE correction_action_log (
            action_name TEXT PRIMARY KEY,
            affected_rows INTEGER NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )

    actions: list[tuple[str, str, tuple[object, ...]]] = [
        (
            "reject_support_only_sources",
            """
            UPDATE evidence_sections
            SET accepted_for_clinical_use='0',
                quality_flags = {flag_expr}
            WHERE accepted_for_clinical_use='1'
              AND (
                    quality_tier LIKE 'D_%'
                 OR quality_flags LIKE '%support_only_not_full_rcp%'
              )
            """.format(flag_expr=append_flag_sql("support_only_not_clinical_evidence")),
            ("support_only_not_clinical_evidence", "support_only_not_clinical_evidence", "support_only_not_clinical_evidence"),
        ),
        (
            "reject_nonclinical_metadata_sections",
            """
            UPDATE evidence_sections
            SET accepted_for_clinical_use='0',
                quality_flags = {flag_expr}
            WHERE accepted_for_clinical_use='1'
              AND (
                    quality_flags LIKE '%non_clinical_or_metadata_section%'
                 OR section_kind IN ('identity', 'presentation', 'generic_group', 'document_reference')
              )
            """.format(flag_expr=append_flag_sql("metadata_not_clinical_evidence")),
            ("metadata_not_clinical_evidence", "metadata_not_clinical_evidence", "metadata_not_clinical_evidence"),
        ),
        (
            "reject_lab_document_amm_mismatches",
            """
            UPDATE evidence_sections
            SET accepted_for_clinical_use='0',
                quality_flags = {flag_expr}
            WHERE accepted_for_clinical_use='1'
              AND source_system='tunisia_lab_local_document'
              AND COALESCE(amm,'') <> ''
              AND EXISTS (
                SELECT 1
                FROM medicines m
                WHERE m.row_id=evidence_sections.row_id
                  AND COALESCE(m.amm,'') <> ''
                  AND UPPER(REPLACE(REPLACE(m.amm,' ',''),'-',''))
                      <> UPPER(REPLACE(REPLACE(evidence_sections.amm,' ',''),'-',''))
              )
            """.format(flag_expr=append_flag_sql("amm_mismatch_needs_review")),
            ("amm_mismatch_needs_review", "amm_mismatch_needs_review", "amm_mismatch_needs_review"),
        ),
    ]

    counts: dict[str, int] = {}
    for name, sql, params in actions:
        con.execute(sql, params)
        affected = con.execute("SELECT changes()").fetchone()[0]
        counts[name] = int(affected)
        con.execute(
            "INSERT INTO correction_action_log VALUES (?, ?, ?)",
            (name, int(affected), now_utc()),
        )

    con.execute(
        """
        UPDATE evidence_sections
        SET section_id = 'ev_' || substr(content_hash, 1, 24)
        WHERE COALESCE(content_hash, '') <> ''
        """
    )
    section_id_updates = int(con.execute("SELECT changes()").fetchone()[0])
    counts["regenerate_section_ids_from_content_hash"] = section_id_updates
    con.execute(
        "INSERT INTO correction_action_log VALUES (?, ?, ?)",
        ("regenerate_section_ids_from_content_hash", section_id_updates, now_utc()),
    )

    con.execute("DROP TABLE IF EXISTS medicine_corrected_rollup")
    con.execute(
        """
        CREATE TEMP TABLE medicine_corrected_rollup AS
        SELECT
          m.row_id,
          COUNT(e.section_id) AS total_sections,
          SUM(CASE WHEN e.accepted_for_clinical_use='1' THEN 1 ELSE 0 END) AS accepted_sections,
          MAX(CAST(CASE WHEN e.accepted_for_clinical_use='1' THEN e.evidence_rank ELSE '0' END AS INTEGER)) AS best_rank
        FROM medicines m
        LEFT JOIN evidence_sections e ON e.row_id=m.row_id
        GROUP BY m.row_id
        """
    )

    con.execute(
        """
        UPDATE medicines
        SET
          final_evidence_section_rows = (
            SELECT CAST(total_sections AS TEXT)
            FROM medicine_corrected_rollup r
            WHERE r.row_id=medicines.row_id
          ),
          final_accepted_section_rows = (
            SELECT CAST(COALESCE(accepted_sections,0) AS TEXT)
            FROM medicine_corrected_rollup r
            WHERE r.row_id=medicines.row_id
          ),
          final_best_evidence_rank = (
            SELECT CAST(COALESCE(best_rank,0) AS TEXT)
            FROM medicine_corrected_rollup r
            WHERE r.row_id=medicines.row_id
          ),
          final_best_source_system = COALESCE((
            SELECT e.source_system
            FROM evidence_sections e
            WHERE e.row_id=medicines.row_id
              AND e.accepted_for_clinical_use='1'
            ORDER BY CAST(e.evidence_rank AS INTEGER) DESC, e.source_system
            LIMIT 1
          ), ''),
          final_evidence_status = CASE
            WHEN (
              SELECT COALESCE(accepted_sections,0)
              FROM medicine_corrected_rollup r
              WHERE r.row_id=medicines.row_id
            ) > 0 THEN 'covered_by_accepted_clinical_evidence'
            ELSE 'uncovered_no_accepted_clinical_evidence'
          END
        """
    )
    counts["recompute_medicine_coverage_fields"] = int(con.execute("SELECT changes()").fetchone()[0])
    con.execute(
        "INSERT INTO correction_action_log VALUES (?, ?, ?)",
        ("recompute_medicine_coverage_fields", counts["recompute_medicine_coverage_fields"], now_utc()),
    )

    con.execute("DROP VIEW IF EXISTS v_medicine_evidence_summary")
    con.execute("DROP VIEW IF EXISTS v_uncovered_medicines")
    con.execute("DROP VIEW IF EXISTS v_source_counts")
    con.executescript(
        """
        CREATE VIEW v_medicine_evidence_summary AS
        SELECT
          m.row_id,
          m.amm,
          m.nom,
          m.nom_generique,
          COUNT(e.section_id) AS evidence_section_rows,
          SUM(CASE WHEN e.accepted_for_clinical_use='1' THEN 1 ELSE 0 END) AS accepted_section_rows,
          MAX(CAST(CASE WHEN e.accepted_for_clinical_use='1' THEN e.evidence_rank ELSE '0' END AS INTEGER)) AS best_evidence_rank,
          GROUP_CONCAT(DISTINCT CASE WHEN e.accepted_for_clinical_use='1' THEN e.source_system END) AS accepted_sources
        FROM medicines m
        LEFT JOIN evidence_sections e ON e.row_id=m.row_id
        GROUP BY m.row_id;

        CREATE VIEW v_uncovered_medicines AS
        SELECT * FROM v_medicine_evidence_summary
        WHERE COALESCE(accepted_section_rows,0)=0
        ORDER BY nom;

        CREATE VIEW v_source_counts AS
        SELECT source_system, quality_tier, accepted_for_clinical_use,
               COUNT(*) AS section_rows, COUNT(DISTINCT row_id) AS medicine_rows
        FROM evidence_sections
        GROUP BY source_system, quality_tier, accepted_for_clinical_use
        ORDER BY accepted_for_clinical_use DESC, medicine_rows DESC, section_rows DESC;
        """
    )

    con.commit()

    after = dict(
        total_evidence=con.execute("SELECT COUNT(*) FROM evidence_sections").fetchone()[0],
        accepted_evidence=con.execute(
            "SELECT COUNT(*) FROM evidence_sections WHERE accepted_for_clinical_use='1'"
        ).fetchone()[0],
        accepted_medicines=con.execute(
            "SELECT COUNT(DISTINCT row_id) FROM evidence_sections WHERE accepted_for_clinical_use='1'"
        ).fetchone()[0],
    )
    return {**{f"before_{k}": v for k, v in before.items()}, **counts, **{f"after_{k}": v for k, v in after.items()}}


def write_query_csv(con: sqlite3.Connection, path: Path, sql: str) -> int:
    cur = con.execute(sql)
    rows_written = 0
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([desc[0] for desc in cur.description])
        while True:
            rows = cur.fetchmany(5000)
            if not rows:
                break
            writer.writerows(rows)
            rows_written += len(rows)
    return rows_written


def export_release_files(con: sqlite3.Connection) -> dict[str, int]:
    counts = {
        "final_medicines_corrected.csv": write_query_csv(
            con, OUT_DIR / "final_medicines_corrected.csv", "SELECT * FROM medicines ORDER BY CAST(row_id AS INTEGER)"
        ),
        "final_evidence_sections_corrected.csv": write_query_csv(
            con,
            OUT_DIR / "final_evidence_sections_corrected.csv",
            "SELECT * FROM evidence_sections ORDER BY CAST(row_id AS INTEGER), source_system, section_kind, section_id",
        ),
        "final_uncovered_medicines_corrected.csv": write_query_csv(
            con,
            OUT_DIR / "final_uncovered_medicines_corrected.csv",
            "SELECT * FROM medicines WHERE final_evidence_status='uncovered_no_accepted_clinical_evidence' ORDER BY nom, dosage",
        ),
        "correction_action_log.csv": write_query_csv(
            con, OUT_DIR / "correction_action_log.csv", "SELECT * FROM correction_action_log ORDER BY action_name"
        ),
    }
    return counts


def validate(con: sqlite3.Connection) -> dict[str, object]:
    out: dict[str, object] = {}
    scalar_queries = {
        "integrity_check": "PRAGMA integrity_check",
        "medicine_rows": "SELECT COUNT(*) FROM medicines",
        "evidence_rows": "SELECT COUNT(*) FROM evidence_sections",
        "accepted_evidence_rows": "SELECT COUNT(*) FROM evidence_sections WHERE accepted_for_clinical_use='1'",
        "accepted_medicine_rows": "SELECT COUNT(DISTINCT row_id) FROM evidence_sections WHERE accepted_for_clinical_use='1'",
        "uncovered_medicine_rows": "SELECT COUNT(*) FROM medicines WHERE final_evidence_status='uncovered_no_accepted_clinical_evidence'",
        "duplicate_medicine_row_ids": "SELECT COUNT(*) FROM (SELECT row_id FROM medicines GROUP BY row_id HAVING COUNT(*)>1)",
        "orphan_evidence_rows": "SELECT COUNT(*) FROM evidence_sections e LEFT JOIN medicines m ON m.row_id=e.row_id WHERE m.row_id IS NULL",
        "duplicate_section_ids": "SELECT COUNT(*) FROM (SELECT section_id FROM evidence_sections GROUP BY section_id HAVING COUNT(*)>1)",
        "duplicate_content_hashes": "SELECT COUNT(*) FROM (SELECT content_hash FROM evidence_sections WHERE COALESCE(content_hash,'')<>'' GROUP BY content_hash HAVING COUNT(*)>1)",
        "accepted_support_only_rows": "SELECT COUNT(*) FROM evidence_sections WHERE accepted_for_clinical_use='1' AND (quality_tier LIKE 'D_%' OR quality_flags LIKE '%support_only_not_full_rcp%')",
        "accepted_nonclinical_metadata_rows": "SELECT COUNT(*) FROM evidence_sections WHERE accepted_for_clinical_use='1' AND (quality_flags LIKE '%non_clinical_or_metadata_section%' OR section_kind IN ('identity','presentation','generic_group','document_reference'))",
        "accepted_lab_amm_mismatch_rows": """
            SELECT COUNT(*)
            FROM evidence_sections e JOIN medicines m ON m.row_id=e.row_id
            WHERE e.accepted_for_clinical_use='1'
              AND e.source_system='tunisia_lab_local_document'
              AND COALESCE(e.amm,'') <> ''
              AND COALESCE(m.amm,'') <> ''
              AND UPPER(REPLACE(REPLACE(e.amm,' ',''),'-',''))
                  <> UPPER(REPLACE(REPLACE(m.amm,' ',''),'-',''))
        """,
        "covered_zero_accepted_medicines": """
            SELECT COUNT(*)
            FROM medicines
            WHERE final_evidence_status LIKE 'covered%'
              AND CAST(final_accepted_section_rows AS INTEGER)=0
        """,
        "uncovered_with_accepted_medicines": """
            SELECT COUNT(*)
            FROM medicines
            WHERE final_evidence_status LIKE 'uncovered%'
              AND CAST(final_accepted_section_rows AS INTEGER)>0
        """,
    }
    for key, sql in scalar_queries.items():
        out[key] = con.execute(sql).fetchone()[0]

    out["source_counts"] = [
        dict(row)
        for row in con.execute(
            """
            SELECT source_system, quality_tier, accepted_for_clinical_use,
                   COUNT(*) AS rows, COUNT(DISTINCT row_id) AS medicines
            FROM evidence_sections
            GROUP BY 1,2,3
            ORDER BY accepted_for_clinical_use DESC, rows DESC
            """
        )
    ]

    out["medicine_quality_buckets"] = [
        dict(row)
        for row in con.execute(
            """
            WITH flags AS (
             SELECT m.row_id,
                    MAX(CASE WHEN e.accepted_for_clinical_use='1' AND e.quality_tier LIKE 'A_%' THEN 1 ELSE 0 END) has_a,
                    MAX(CASE WHEN e.accepted_for_clinical_use='1' AND e.quality_tier LIKE 'B_%' THEN 1 ELSE 0 END) has_b,
                    MAX(CASE WHEN e.accepted_for_clinical_use='1' AND e.quality_tier LIKE 'C_%' THEN 1 ELSE 0 END) has_c,
                    MAX(CASE WHEN e.accepted_for_clinical_use='1' AND e.quality_tier LIKE 'D_%' THEN 1 ELSE 0 END) has_d,
                    MAX(CASE WHEN e.accepted_for_clinical_use='1' THEN 1 ELSE 0 END) has_any
             FROM medicines m LEFT JOIN evidence_sections e ON e.row_id=m.row_id
             GROUP BY m.row_id
            )
            SELECT CASE
              WHEN has_a=1 THEN 'A local Tunisia clinical evidence'
              WHEN has_b=1 THEN 'B foreign official clinical evidence only'
              WHEN has_c=1 THEN 'C cached fallback clinical evidence only'
              WHEN has_d=1 THEN 'D support-only evidence only'
              ELSE 'No accepted clinical evidence'
            END AS bucket,
            COUNT(*) AS medicines
            FROM flags
            GROUP BY 1
            ORDER BY medicines DESC
            """
        )
    ]
    return out


def main() -> int:
    if not SRC_DB.exists():
        raise SystemExit(f"Missing source database: {SRC_DB}")
    copy_release_db()
    con = connect()
    try:
        corrections = apply_corrections(con)
        exports = export_release_files(con)
        validation = validate(con)
        summary = {
            "created_at": now_utc(),
            "source_database": str(SRC_DB),
            "corrected_database": str(OUT_DB),
            "corrections": corrections,
            "exports": exports,
            "validation": validation,
        }
        (OUT_DIR / "final_release_corrected_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (OUT_DIR / "README_CORRECTED_DATA_FILES.md").write_text(
            "\n".join(
                [
                    "# Corrected Final Data Release",
                    "",
                    "This folder was generated from `final_data_release/final_data_release.db` by `create_corrected_final_release.py`.",
                    "",
                    "The original release is left untouched.",
                    "",
                    "Corrections applied:",
                    "- support-only D-tier sources are retained for audit but no longer accepted for clinical use.",
                    "- non-clinical metadata sections are retained for audit but no longer accepted for clinical use.",
                    "- Tunisia lab-document rows whose extracted AMM disagrees with the target medicine AMM are retained for audit but no longer accepted.",
                    "- section IDs are regenerated from `content_hash` so they are unique and stable.",
                    "- medicine coverage fields are recalculated from accepted clinical evidence only.",
                    "",
                    "Primary files:",
                    "- `final_data_release_corrected.db`",
                    "- `final_medicines_corrected.csv`",
                    "- `final_evidence_sections_corrected.csv`",
                    "- `final_uncovered_medicines_corrected.csv`",
                    "- `correction_action_log.csv`",
                    "- `final_release_corrected_summary.json`",
                ]
            ),
            encoding="utf-8",
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
