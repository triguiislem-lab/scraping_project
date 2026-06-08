# DPM Tunisia live mapper + RCP tools

For the complete data inventory, scripts used, and automatic-prescription oriented outputs, see:
- [AUTOMATIC_PRESCRIPTION_DATA_DETAILS.md](AUTOMATIC_PRESCRIPTION_DATA_DETAILS.md)

This package gives you a **live exact mapper** and an **RCP verifier/downloader** for the DPM Tunisia registry.

## Files
- `dpm_tn_live_mapper_and_rcp_tool.py` → main script
- `dpm_tn_class_map.json` → therapeutic class/subclass reference built from your provided DPM HTML
- `dpm_tn_live_template.xlsx` → workbook template with exact-mapping and RCP result columns already added

## What the script does
1. Reads the medicine registry workbook.
2. Scrapes DPM therapeutic subclass pages live.
3. Matches each live DPM listing/detail record back to the registry row.
4. Writes exact:
   - class code
   - class name
   - subclass code
   - subclass name
5. Parses the detail page for a direct RCP link when available.
6. Verifies guessed or direct RCP URLs.
7. Optionally downloads verified PDFs.

## Install
```bash
pip install requests beautifulsoup4 pandas openpyxl
```

## Run full pipeline
```bash
python dpm_tn_live_mapper_and_rcp_tool.py \
  --input-xlsx dpm_tn_live_template.xlsx \
  --class-map dpm_tn_class_map.json \
  --output-dir dpm_live_out \
  --mode all \
  --download-rcp
```

## Run mapping only
```bash
python dpm_tn_live_mapper_and_rcp_tool.py \
  --input-xlsx dpm_tn_live_template.xlsx \
  --class-map dpm_tn_class_map.json \
  --output-dir dpm_live_out \
  --mode mapping
```

## Run RCP verification only
```bash
python dpm_tn_live_mapper_and_rcp_tool.py \
  --input-xlsx dpm_tn_live_template.xlsx \
  --class-map dpm_tn_class_map.json \
  --output-dir dpm_live_out \
  --mode rcp \
  --download-rcp
```

## Outputs
Inside `dpm_live_out/` you should get:
- `medicines_exact_mapped.csv`
- `subclass_scrape_log.csv`
- `rcp_manifest.csv`
- `dpm_tn_live_verified.xlsx`
- `rcp_pdfs/` with downloaded PDFs

## Important
This script needs a machine with normal internet access to `dpm.tn`.  
I prepared the tool here, but I could not execute the live verification/download step from this environment.
