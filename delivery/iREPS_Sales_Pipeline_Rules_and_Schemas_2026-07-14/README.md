# iREPS Sales Pipeline Rules and Schemas Update

This package closes the Lesedi Conlog Sales Pipeline governance milestone completed on 2026-07-14.

## Completed baseline

```text
Environment:          ireps-test
LM / workbase:        Lesedi / ZA7423
Period:               2025-09 through 2026-06
Atomic documents:     822,527
Monthly meter docs:   157,940
Monthly LM docs:      10
Monthly group docs:   50
Meter Master docs:    35,295
Sales All Meters:     35,295
```

## Package contents

```text
Apply-iREPSSalesGovernanceUpdate.ps1
ireps-schemas/
  README.md
  conlog-sales-atomic/conlog_sales_atomic.schema.md
  conlog-sales-monthly/conlog_sales_monthly.schema.md
  conlog-sales-monthly-lm/conlog_sales_monthly_lm.schema.md
  conlog-sales-monthly-lm-groups/conlog_sales_monthly_lm_groups.schema.md
  meter-master/meter_master.schema.md
  sales-all-meters/sales_all_meters.schema.md
```

## What the installer does

1. Backs up `C:\dev\ireps-pipeline-sales\rules\SALES_PIPELINE_RULES.md`.
2. Updates the rules to record the completed TEST baseline.
3. Corrects the schema repository path to `C:\dev\ireps-schemas`.
4. Replaces the outdated one-month downstream rule with the tested explicit continuous full-period rule for Meter Master and Sales All Meters.
5. Records the governed duplicate-resolution contract.
6. Copies all six collection schemas into `C:\dev\ireps-schemas`.
7. Adds the missing `sales-all-meters` schema folder.
8. Validates the expected files after writing.

## Run

From the extracted package folder:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\Apply-iREPSSalesGovernanceUpdate.ps1
```

The installer changes documentation only. It does not connect to Firebase and does not touch operational CSV files.
