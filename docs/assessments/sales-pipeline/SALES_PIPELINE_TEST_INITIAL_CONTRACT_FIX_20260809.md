# Sales Pipeline TEST Initial Contract Fix — 2026-08-09

## Scope and safety

This fix was performed locally against the frozen ZA5241 Stage 05 and Stage 06 artifacts. No pipeline command was run, no Firebase client was initialized during validation, and no Firestore writes or deletes were performed.

The Stage 08 `initial-load` configuration is now explicitly hard-gated to `ireps-test`. The existing orchestrator service-account project check remains in place. Refresh behavior and the DEV pipeline dispatch were not changed.

## Contract trace

The current Stage 06 path is selected by `06_build_sales_all_meters.py --source-origin monthly_source`. It delegates to `sales_pipeline_monthly_source_support.build_stage06_monthly_source`, which writes a schema-version 2 manifest and an exact ordered rich CSV contract. That contract contains the base Sales All fields, the governed commercial/GPS/risk/ERF fields, chronological monthly amount fields, and matching chronological monthly unit fields. Visibility remains absent from the CSV and owned by operational writers.

Stage 08 refresh already consumed this exact contract through `sales_pipeline_sales_all_refresh.load_and_validate`. That loader verifies the manifest identity/status/fingerprint, CSV SHA, manifest column list and row count, the exact ordered current schema, month pairing, provider and LM identity, totals, units, recency semantics, typed commercial fields, and JSON fields. It also builds the canonical current Firestore document used by the successful DEV Sales load.

The Stage 08 non-refresh path predated the monthly-source contract. Its `load_and_validate_csv` function required the legacy atomic shape (`BASE_COLUMNS + amount_*`) and its `build_document` function constructed only the narrow legacy document. The optimized TEST initial-load orchestrator selected this non-refresh path, so it failed at the first local column comparison before Firestore was opened.

## Fix

Only Stage 08 `initial-load` routing changed:

- It reuses `sales_pipeline_sales_all_refresh.load_and_validate` for exact current Stage 06 validation and canonical rich document construction.
- It creates those prebuilt canonical documents with `batch.create` in 400-document waves.
- It retains the input-ID absence gate using `get_all` in 400-document waves.
- It verifies all input documents after creation using `get_all` in 400-document waves and exact canonical document equality.
- It does not call refresh, update, transaction, merge, or delete paths.
- Legacy `create-only` and `resume` semantics remain on their existing narrow contract path. Stage 08 refresh dispatch is unchanged.

## Stage 07 local contract validation

`meter_master__ZA5241__FULL__2023-12_to_2026-06.csv` and its manifest were passed through:

1. `07_upload_meter_master_v3.load_and_validate_csv`
2. `07_upload_meter_master_v3.validate_stage05_manifest`
3. `07_upload_meter_master_v3.dataframe_rows`
4. `07_upload_meter_master_v3.build_create_doc` for every row

Result: **PASS**, 10,216 rows and 10,216 constructed documents. No Stage 07 change was required.

## Full local pre-run validation

- Sales All artifact and manifest: current rich Stage 06 loader passed; 10,216 unique rows and 10,216 canonical documents constructed.
- Meter Master artifact and manifest: Stage 07 parser, manifest validator, row conversion, and document construction passed; 10,216 rows and 10,216 documents constructed.
- Sales provider: `contour`.
- LM: `ZA5241`.
- Stage 08 batch size: 400.
- Stage 07 batch size: 400.
- Stage 08 initial-load verification: full input scope, batched `get_all`.
- Changed-file `python -m py_compile`: PASS.
- `git diff --check`: PASS (Git emitted only existing line-ending conversion warnings).
- Existing offline unit suite: 12 tests passed and one pre-existing test errored because its `UploadConfig` fixture omits the already-required `preflight_only` argument. This is unrelated to the contract fix.

ROOT CAUSE:
The optimized Stage 08 TEST initial-load mode was routed through the legacy atomic Sales All validator and narrow document builder, while current Stage 06 monthly-source output and successful DEV refresh use the schema-version 2 rich governed loader/document builder.

SALES ALL CURRENT CONTRACT IDENTIFIED:
YES

STAGE 08 INITIAL-LOAD CONTRACT FIXED:
YES

SALES ALL 10,216 LOCAL VALIDATION:
PASS

METER MASTER 10,216 LOCAL VALIDATION:
PASS

REFRESH USED:
NO

BATCH READ SIZE:
400

BATCH WRITE SIZE:
400

PROJECT HARD GATE:
PASS

FILES MODIFIED:
scripts/08_upload_sales_all_meters.py
docs/assessments/sales-pipeline/SALES_PIPELINE_TEST_INITIAL_CONTRACT_FIX_20260809.md

READY TO RERUN ONE-COMMAND TEST PIPELINE:
YES
