# Script Finale Scraping - TN Med Data Collection

Ce dossier regroupe les scripts principaux utilises pour collecter, enrichir, extraire et consolider les donnees medicamenteuses du projet TN Med.

L'objectif est de garder une version propre des fichiers utiles, sans les anciennes variantes intermediaires de test.

## Structure

```text
script_finale_scraping/
  01_dpm_scraping/
  02_pdf_text_extraction/
  03_external_fallback_sources/
  04_release_build_and_correction/
  config/
```

## 1. DPM Scraping

Scripts principaux pour collecter les medicaments depuis la DPM Tunisie, recuperer les details, les liens RCP/notice et verifier la couverture documentaire.

| Script | Role |
|---|---|
| `dpm_tn_live_mapper_and_rcp_tool_v4_final_hardened.py` | Script principal DPM live : mapping medicament, details, RCP/notice, logique durcie. |
| `dpm_tn_from_catalog_csv_enricher.py` | Enrichit un catalogue existant avec les informations DPM. |
| `dpm_tn_rcp_resume_only.py` | Reprise/resume du traitement RCP sans relancer toute la collecte. |
| `verify_medicine_document_availability.py` | Verifie la disponibilite des documents RCP/notice/PDF. |
| `summarize_local_coverage.py` | Produit un resume de couverture locale. |
| `dpm_tn_live_tools_README.md` | Documentation des outils DPM live. |

## 2. PDF / Text Extraction

Scripts utilises pour transformer les documents collectes en texte exploitable.

| Script | Role |
|---|---|
| `extract_rcp_sections_from_pdfs.py` | Extrait les sections cliniques depuis les PDF RCP/notices. |
| `ocr_local_document_queue.py` | Prepare/traite les documents locaux qui necessitent OCR ou retraitement. |
| `build_rcp_fallback_reextraction_manifest.php` | Genere un manifest de re-extraction fallback RCP. |

## 3. External Fallback Sources

Scripts utilises pour completer la couverture quand les sources locales ne suffisent pas.

| Script | Role |
|---|---|
| `prepare_external_fallback_queues.py` | Prepare les files de traitement fallback externe. |
| `guaranteed_source_hunter.py` | Cherche des sources documentaires alternatives pour les medicaments non couverts. |
| `fetch_us_label_fallback.py` | Recupere des labels US de fallback. |
| `fetch_us_live_fallback.py` | Recupere des donnees US live. |
| `fetch_eu_uk_live_fallback.py` | Recupere des sources Europe/UK. |
| `fetch_bdpm_api_fallback.py` | Recupere des donnees BDPM. |
| `fetch_global_regulatory_fallback.py` | Recupere des sources reglementaires internationales. |
| `global_regulatory_fallback_enriched_v3.py` | Version enrichie du fallback reglementaire global. |
| `normalize_cache_fallback_data.py` | Normalise les donnees fallback cachees. |
| `swissmedic_aips_adapter_v2_fixed.py` | Adapteur Swissmedic/AIPS. |
| `swissmedic_aips_candidate_resolver_fast_v2.py` | Resolution rapide des candidats Swissmedic. |
| `who_vaccine_document_adapter_v2.py` | Adapteur documents vaccin WHO. |

## 4. Release Build And Correction

Scripts pour assembler la base finale, produire les fichiers de release et appliquer la correction qualite.

| Script | Role |
|---|---|
| `build_remaining_medicine_details.py` | Complete les details manquants des medicaments. |
| `build_final_data_release.py` | Construit la release finale initiale. |
| `create_corrected_final_release.py` | Cree la release corrigee finale apres audit qualite. |
| `build_automatic_prescription_master_db.py` | Construction d'une base master pour prescription/CDSS. |
| `enhance_automatic_prescription_master_db.ps1` | Enrichissement PowerShell de la base master. |

## 5. Config

| Fichier | Role |
|---|---|
| `dpm_tn_class_map.json` | Mapping de classes DPM/TN utilise pour l'enrichissement. |

## Scripts volontairement non inclus

Les anciennes versions de test n'ont pas ete copiees dans ce dossier afin de garder une structure propre :

- `dpm_tn_live_mapper_and_rcp_tool.py`
- `dpm_tn_live_mapper_and_rcp_tool_v2.py`
- `dpm_tn_live_mapper_and_rcp_tool_v2b_parserfix.py`
- `dpm_tn_live_mapper_and_rcp_tool_v3_rcp_extract.py`
- `dpm_tn_live_mapper_and_rcp_tool_v4_1_resume_patch.py`
- `dpm_tn_fiche_rcp_smoketest*.py`
- anciennes versions `global_regulatory_fallback_enriched.py`, `v2`
- anciennes versions Swissmedic/WHO non fixees

Ces fichiers restent dans le dossier racine si un historique technique est necessaire.

## Ordre logique du pipeline

```text
1. Scraping DPM
   -> dpm_tn_live_mapper_and_rcp_tool_v4_final_hardened.py
   -> dpm_tn_from_catalog_csv_enricher.py

2. Verification/reprise documentaire
   -> verify_medicine_document_availability.py
   -> dpm_tn_rcp_resume_only.py
   -> summarize_local_coverage.py

3. Extraction PDF / OCR
   -> extract_rcp_sections_from_pdfs.py
   -> ocr_local_document_queue.py

4. Fallback externe si couverture insuffisante
   -> prepare_external_fallback_queues.py
   -> fetch_*_fallback.py
   -> swissmedic_* / who_* adapters
   -> normalize_cache_fallback_data.py

5. Construction release
   -> build_remaining_medicine_details.py
   -> build_final_data_release.py

6. Correction qualite finale
   -> create_corrected_final_release.py
```
