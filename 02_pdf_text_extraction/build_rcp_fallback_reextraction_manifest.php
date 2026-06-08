<?php
declare(strict_types=1);

/*
 * Build a targeted manifest for re-extracting Tunisian DPM RCP PDFs that were
 * previously parsed through keyword fallback windows.
 *
 * The generated CSV is compatible with extract_rcp_sections_from_pdfs.py.
 */

function arg_value(array $argv, string $name, string $default): string
{
    foreach ($argv as $idx => $arg) {
        if ($arg === $name && isset($argv[$idx + 1])) {
            return $argv[$idx + 1];
        }
        if (str_starts_with($arg, $name . '=')) {
            return substr($arg, strlen($name) + 1);
        }
    }
    return $default;
}

function norm_key(string $value): string
{
    return strtolower((string) preg_replace('/[^a-z0-9]+/i', '', $value));
}

function workspace_path(string $path): string
{
    if (preg_match('/^[A-Za-z]:[\\\\\\/]/', $path) || str_starts_with($path, '/')) {
        return $path;
    }
    return getcwd() . DIRECTORY_SEPARATOR . $path;
}

function rel_path(string $path): string
{
    $cwd = rtrim(str_replace('\\', '/', getcwd()), '/');
    $normalized = str_replace('\\', '/', $path);
    if (str_starts_with($normalized, $cwd . '/')) {
        return substr($normalized, strlen($cwd) + 1);
    }
    return $normalized;
}

$dbPath = workspace_path(arg_value($argv, '--db', 'final_data_release/final_data_release.db'));
$outputPath = workspace_path(arg_value($argv, '--output', 'dpm_live_out/rcp_fallback_reextraction_manifest.csv'));
$pdfRootsArg = arg_value($argv, '--pdf-roots', 'rcp_pdfs,opalia,teriak,unimed,medis');
$pdfRoots = array_values(array_filter(array_map('trim', explode(',', $pdfRootsArg))));

if (!is_file($dbPath)) {
    fwrite(STDERR, "Database not found: {$dbPath}\n");
    exit(1);
}

$pdfIndex = [];
$pdfRootByKey = [];
foreach ($pdfRoots as $root) {
    $rootPath = workspace_path($root);
    if (!is_dir($rootPath)) {
        continue;
    }
    $iterator = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($rootPath, FilesystemIterator::SKIP_DOTS));
    foreach ($iterator as $fileInfo) {
        if (!$fileInfo->isFile() || strtolower($fileInfo->getExtension()) !== 'pdf') {
            continue;
        }
        $key = norm_key($fileInfo->getBasename('.pdf'));
        if ($key === '') {
            continue;
        }
        if (!isset($pdfIndex[$key])) {
            $pdfIndex[$key] = rel_path($fileInfo->getPathname());
            $pdfRootByKey[$key] = $root;
        }
    }
}

$pdo = new PDO('sqlite:' . $dbPath, null, null, [
    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
]);

$sql = <<<SQL
SELECT
    MIN(row_id) AS row_id,
    amm,
    MIN(nom) AS nom,
    COUNT(*) AS fallback_rows,
    SUM(CASE WHEN LENGTH(section_text) >= 4800 THEN 1 ELSE 0 END) AS near_cap_rows,
    SUM(CASE WHEN LENGTH(section_text) = 5000 THEN 1 ELSE 0 END) AS exact_cap_rows,
    MAX(LENGTH(section_text)) AS max_section_chars
FROM evidence_sections
WHERE source_system = 'tunisia_dpm_local_rcp_pdf'
  AND section_id LIKE '%fallback%'
  AND CAST(accepted_for_clinical_use AS TEXT) IN ('1', 'true', 'True', 'TRUE')
GROUP BY amm
ORDER BY exact_cap_rows DESC, near_cap_rows DESC, fallback_rows DESC, amm
SQL;

$rows = $pdo->query($sql)->fetchAll();

$outputDir = dirname($outputPath);
if (!is_dir($outputDir) && !mkdir($outputDir, 0777, true) && !is_dir($outputDir)) {
    fwrite(STDERR, "Cannot create output directory: {$outputDir}\n");
    exit(1);
}

$handle = fopen($outputPath, 'wb');
if (!$handle) {
    fwrite(STDERR, "Cannot write output: {$outputPath}\n");
    exit(1);
}

$fields = [
    'ROW_ID',
    'AMM',
    'NOM',
    'RCP_VERIFY_STATUS',
    'DOWNLOADED_RCP_FILE',
    'FALLBACK_ROWS',
    'NEAR_CAP_ROWS',
    'EXACT_CAP_ROWS',
    'MAX_SECTION_CHARS',
    'MATCHED_PDF_ROOT',
    'REEXTRACTION_REASON',
];
fputcsv($handle, $fields);

$matched = 0;
$unmatched = 0;
$nearCapMatched = 0;
$exactCapMatched = 0;
$unmatchedAmms = [];

foreach ($rows as $row) {
    $amm = (string) ($row['amm'] ?? '');
    $key = norm_key($amm);
    $pdfPath = $pdfIndex[$key] ?? '';
    $root = $pdfRootByKey[$key] ?? '';
    if ($pdfPath !== '') {
        $matched++;
        if ((int) $row['near_cap_rows'] > 0) {
            $nearCapMatched++;
        }
        if ((int) $row['exact_cap_rows'] > 0) {
            $exactCapMatched++;
        }
    } else {
        $unmatched++;
        $unmatchedAmms[] = $amm;
    }
    fputcsv($handle, [
        $row['row_id'] ?? '',
        $amm,
        $row['nom'] ?? '',
        $pdfPath !== '' ? 'verified' : 'missing_pdf',
        $pdfPath,
        $row['fallback_rows'] ?? '0',
        $row['near_cap_rows'] ?? '0',
        $row['exact_cap_rows'] ?? '0',
        $row['max_section_chars'] ?? '0',
        $root,
        'previous_keyword_fallback_or_near_5000_char_window',
    ]);
}
fclose($handle);

$summary = [
    'database' => rel_path($dbPath),
    'output' => rel_path($outputPath),
    'pdf_roots' => $pdfRoots,
    'fallback_amms' => count($rows),
    'matched_pdfs' => $matched,
    'unmatched_pdfs' => $unmatched,
    'matched_with_near_cap_rows' => $nearCapMatched,
    'matched_with_exact_cap_rows' => $exactCapMatched,
    'unmatched_amms_sample' => array_slice($unmatchedAmms, 0, 20),
];

echo json_encode($summary, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) . PHP_EOL;
