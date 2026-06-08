param(
    [string]$DbPath = "dpm_live_out\automatic_prescription_master.db",
    [string]$OutputDir = "dpm_live_out"
)

$ErrorActionPreference = "Stop"

function Resolve-PathValue([string]$PathValue) {
    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return [System.IO.Path]::GetFullPath($PathValue)
    }
    return [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $PathValue))
}

$DbFullPath = Resolve-PathValue $DbPath
$OutFullPath = Resolve-PathValue $OutputDir
New-Item -ItemType Directory -Force -Path $OutFullPath | Out-Null

if (-not (Test-Path $DbFullPath)) {
    throw "Master DB not found: $DbFullPath"
}

$Sql = @"
DROP VIEW IF EXISTS v_medicine_profile;
DROP VIEW IF EXISTS v_prescription_ready_high;
DROP VIEW IF EXISTS v_safety_gap_queue;
DROP VIEW IF EXISTS v_local_evidence_priority;
DROP VIEW IF EXISTS v_cdss_unmatched_medicines;
DROP VIEW IF EXISTS v_prescription_module_coverage;
DROP TABLE IF EXISTS medicine_profile;

CREATE TABLE medicine_profile AS
SELECT
  CAST(mm.row_id AS INTEGER) AS row_id,
  mm.amm,
  mm.nom,
  mm.dosage,
  mm.forme,
  mm.presentation,
  mm.nom_generique,
  mm.labo,
  mm.pays,
  mm.date_amm,
  mm.generic_princeps_biosimilar,
  mcm.cdss_product_id,
  mcm.cdss_product_label,
  mcm.cdss_dci_api,
  mcm.cdss_therapeutic_class,
  mcm.cdss_therapeutic_subclass,
  mcm.cdss_reimbursement_category,
  mcm.match_type AS cdss_match_type,
  CAST(mcm.match_score AS REAL) AS cdss_match_score,
  CAST(mcc.local_any_document_available AS INTEGER) AS local_any_document_available,
  CAST(mcc.local_rcp_available AS INTEGER) AS local_rcp_available,
  CAST(mcc.local_notice_available AS INTEGER) AS local_notice_available,
  CAST(mcc.dpm_rcp_text_available AS INTEGER) AS dpm_rcp_text_available,
  CAST(mcc.cdss_product_available AS INTEGER) AS cdss_product_available,
  CAST(mcc.ingredient_link_count AS INTEGER) AS ingredient_link_count,
  CAST(mcc.indication_rule_count AS INTEGER) AS indication_rule_count,
  CAST(mcc.dosage_rule_count AS INTEGER) AS dosage_rule_count,
  CAST(mcc.contraindication_rule_count AS INTEGER) AS contraindication_rule_count,
  CAST(mcc.interaction_rule_count AS INTEGER) AS interaction_rule_count,
  CAST(mcc.adverse_effect_count AS INTEGER) AS adverse_effect_count,
  CAST(mcc.renal_hepatic_adjustment_count AS INTEGER) AS renal_hepatic_adjustment_count,
  CAST(mcc.special_population_rule_count AS INTEGER) AS special_population_rule_count,
  CAST(mcc.administration_rule_count AS INTEGER) AS administration_rule_count,
  CAST(mcc.substitution_rule_count AS INTEGER) AS substitution_rule_count,
  mcc.automatic_prescription_readiness,
  TRIM(
    CASE WHEN CAST(mcc.cdss_product_available AS INTEGER)=0 THEN 'missing_cdss_match; ' ELSE '' END ||
    CASE WHEN CAST(mcc.local_any_document_available AS INTEGER)=0 THEN 'missing_local_document; ' ELSE '' END ||
    CASE WHEN CAST(mcc.ingredient_link_count AS INTEGER)=0 THEN 'missing_ingredient_link; ' ELSE '' END ||
    CASE WHEN CAST(mcc.indication_rule_count AS INTEGER)=0 THEN 'missing_indication; ' ELSE '' END ||
    CASE WHEN CAST(mcc.dosage_rule_count AS INTEGER)=0 THEN 'missing_dosage; ' ELSE '' END ||
    CASE WHEN CAST(mcc.contraindication_rule_count AS INTEGER)=0 THEN 'missing_contraindication; ' ELSE '' END ||
    CASE WHEN CAST(mcc.interaction_rule_count AS INTEGER)=0 THEN 'missing_interaction; ' ELSE '' END ||
    CASE WHEN CAST(mcc.special_population_rule_count AS INTEGER)=0 THEN 'missing_special_population; ' ELSE '' END
  ) AS gap_flags
FROM medicine_master mm
JOIN medicine_cdss_match mcm ON CAST(mcm.row_id AS INTEGER)=CAST(mm.row_id AS INTEGER)
JOIN medicine_clinical_coverage mcc ON CAST(mcc.row_id AS INTEGER)=CAST(mm.row_id AS INTEGER);

CREATE INDEX idx_profile_row ON medicine_profile(row_id);
CREATE INDEX idx_profile_ready ON medicine_profile(automatic_prescription_readiness);
CREATE INDEX idx_profile_cdss_available ON medicine_profile(cdss_product_available);
CREATE INDEX idx_profile_local_doc ON medicine_profile(local_any_document_available);

CREATE VIEW v_medicine_profile AS
SELECT * FROM medicine_profile;

CREATE VIEW v_prescription_ready_high AS
SELECT *
FROM v_medicine_profile
WHERE automatic_prescription_readiness='high'
ORDER BY nom, dosage, forme, presentation;

CREATE VIEW v_safety_gap_queue AS
SELECT
  row_id,
  amm,
  nom,
  dosage,
  forme,
  presentation,
  nom_generique,
  labo,
  automatic_prescription_readiness,
  gap_flags,
  CASE
    WHEN cdss_product_available=0 THEN 'P0 map medicine to CDSS/product identity'
    WHEN ingredient_link_count=0 THEN 'P0 resolve ingredient/DCI link'
    WHEN interaction_rule_count=0 THEN 'P0 add interaction safety'
    WHEN contraindication_rule_count=0 THEN 'P0 add contraindication safety'
    WHEN dosage_rule_count=0 THEN 'P1 add dosage'
    WHEN local_any_document_available=0 THEN 'P1 acquire local RCP/notice'
    WHEN indication_rule_count=0 THEN 'P1 add indication'
    WHEN special_population_rule_count=0 THEN 'P2 add pregnancy/pediatric/geriatric/lactation'
    ELSE 'P3 enrich evidence'
  END AS next_action,
  cdss_product_id,
  cdss_match_type,
  local_any_document_available,
  ingredient_link_count,
  indication_rule_count,
  dosage_rule_count,
  contraindication_rule_count,
  interaction_rule_count,
  adverse_effect_count,
  renal_hepatic_adjustment_count,
  special_population_rule_count,
  substitution_rule_count
FROM v_medicine_profile
WHERE automatic_prescription_readiness<>'high'
ORDER BY
  CASE
    WHEN cdss_product_available=0 THEN 0
    WHEN ingredient_link_count=0 THEN 1
    WHEN interaction_rule_count=0 THEN 2
    WHEN contraindication_rule_count=0 THEN 3
    WHEN dosage_rule_count=0 THEN 4
    WHEN local_any_document_available=0 THEN 5
    ELSE 6
  END,
  nom, dosage;

CREATE VIEW v_local_evidence_priority AS
SELECT
  row_id,
  amm,
  nom,
  dosage,
  forme,
  presentation,
  nom_generique,
  labo,
  local_rcp_available,
  local_notice_available,
  dpm_rcp_text_available,
  indication_rule_count,
  dosage_rule_count,
  contraindication_rule_count,
  interaction_rule_count,
  automatic_prescription_readiness
FROM v_medicine_profile
WHERE local_any_document_available=1
  AND dpm_rcp_text_available=0
ORDER BY
  CASE WHEN automatic_prescription_readiness='high' THEN 2 WHEN automatic_prescription_readiness='medium' THEN 1 ELSE 0 END DESC,
  local_rcp_available DESC,
  nom, dosage;

CREATE VIEW v_cdss_unmatched_medicines AS
SELECT *
FROM v_medicine_profile
WHERE cdss_product_available=0
ORDER BY nom, dosage, forme, presentation;

CREATE VIEW v_prescription_module_coverage AS
SELECT 'medicine_total' AS module, COUNT(*) AS rows FROM v_medicine_profile
UNION ALL SELECT 'cdss_product', COUNT(*) FROM v_medicine_profile WHERE cdss_product_available=1
UNION ALL SELECT 'local_document', COUNT(*) FROM v_medicine_profile WHERE local_any_document_available=1
UNION ALL SELECT 'dpm_rcp_text', COUNT(*) FROM v_medicine_profile WHERE dpm_rcp_text_available=1
UNION ALL SELECT 'ingredient_link', COUNT(*) FROM v_medicine_profile WHERE ingredient_link_count>0
UNION ALL SELECT 'indication', COUNT(*) FROM v_medicine_profile WHERE indication_rule_count>0
UNION ALL SELECT 'dosage', COUNT(*) FROM v_medicine_profile WHERE dosage_rule_count>0
UNION ALL SELECT 'contraindication', COUNT(*) FROM v_medicine_profile WHERE contraindication_rule_count>0
UNION ALL SELECT 'interaction', COUNT(*) FROM v_medicine_profile WHERE interaction_rule_count>0
UNION ALL SELECT 'adverse_effect', COUNT(*) FROM v_medicine_profile WHERE adverse_effect_count>0
UNION ALL SELECT 'renal_hepatic', COUNT(*) FROM v_medicine_profile WHERE renal_hepatic_adjustment_count>0
UNION ALL SELECT 'special_population', COUNT(*) FROM v_medicine_profile WHERE special_population_rule_count>0
UNION ALL SELECT 'administration', COUNT(*) FROM v_medicine_profile WHERE administration_rule_count>0
UNION ALL SELECT 'substitution', COUNT(*) FROM v_medicine_profile WHERE substitution_rule_count>0
UNION ALL SELECT 'readiness_high', COUNT(*) FROM v_medicine_profile WHERE automatic_prescription_readiness='high'
UNION ALL SELECT 'readiness_medium', COUNT(*) FROM v_medicine_profile WHERE automatic_prescription_readiness='medium'
UNION ALL SELECT 'readiness_low', COUNT(*) FROM v_medicine_profile WHERE automatic_prescription_readiness='low';
"@

$Sql | sqlite3 $DbFullPath

$Exports = @{
    "automatic_prescription_ready_high.csv" = "SELECT * FROM v_prescription_ready_high;"
    "automatic_prescription_safety_gap_queue.csv" = "SELECT * FROM v_safety_gap_queue;"
    "automatic_prescription_local_evidence_priority.csv" = "SELECT * FROM v_local_evidence_priority;"
    "automatic_prescription_cdss_unmatched.csv" = "SELECT * FROM v_cdss_unmatched_medicines;"
    "automatic_prescription_module_coverage.csv" = "SELECT * FROM v_prescription_module_coverage;"
}

foreach ($entry in $Exports.GetEnumerator()) {
    $target = Join-Path $OutFullPath $entry.Key
    & sqlite3 -header -csv $DbFullPath $entry.Value | Set-Content -Encoding UTF8 $target
}

$coverage = & sqlite3 -header -csv $DbFullPath "SELECT * FROM v_prescription_module_coverage;" | ConvertFrom-Csv
$summary = [ordered]@{
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    db_path = $DbFullPath
    views_created = @(
        "v_medicine_profile",
        "v_prescription_ready_high",
        "v_safety_gap_queue",
        "v_local_evidence_priority",
        "v_cdss_unmatched_medicines",
        "v_prescription_module_coverage"
    )
    exports = [ordered]@{}
    module_coverage = $coverage
}
foreach ($entry in $Exports.GetEnumerator()) {
    $target = Join-Path $OutFullPath $entry.Key
    $summary.exports[$entry.Key] = $target
}
$summaryPath = Join-Path $OutFullPath "automatic_prescription_view_summary.json"
$summary | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 $summaryPath
$summary | ConvertTo-Json -Depth 6
