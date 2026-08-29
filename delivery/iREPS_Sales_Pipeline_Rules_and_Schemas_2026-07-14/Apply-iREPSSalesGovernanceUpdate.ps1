param(
    [string]$PipelineRoot = "C:\dev\ireps-pipeline-sales",
    [string]$SchemasRoot = "C:\dev\ireps-schemas"
)

$ErrorActionPreference = "Stop"
$Timestamp = Get-Date -Format "yyyyMMddTHHmmss"
$RulesPath = Join-Path $PipelineRoot "rules\SALES_PIPELINE_RULES.md"
$PackageSchemas = Join-Path $PSScriptRoot "ireps-schemas"

function Replace-Section {
    param(
        [string]$Text,
        [string]$StartHeading,
        [string]$EndHeading,
        [string]$NewSection
    )

    $start = $Text.IndexOf($StartHeading, [System.StringComparison]::Ordinal)
    if ($start -lt 0) { throw "Start heading not found: $StartHeading" }

    $end = $Text.IndexOf($EndHeading, $start, [System.StringComparison]::Ordinal)
    if ($end -lt 0) { throw "End heading not found: $EndHeading" }

    return $Text.Substring(0, $start) + $NewSection.TrimEnd() + "`r`n`r`n---`r`n`r`n" + $Text.Substring($end)
}

function Replace-Range {
    param(
        [string]$Text,
        [string]$StartHeading,
        [string]$EndHeading,
        [string]$Replacement
    )

    $start = $Text.IndexOf($StartHeading, [System.StringComparison]::Ordinal)
    if ($start -lt 0) { throw "Range start not found: $StartHeading" }

    $end = $Text.IndexOf($EndHeading, $start, [System.StringComparison]::Ordinal)
    if ($end -lt 0) { throw "Range end not found: $EndHeading" }

    return $Text.Substring(0, $start) + $Replacement.TrimEnd() + "`r`n`r`n" + $Text.Substring($end)
}

if (-not (Test-Path $RulesPath)) {
    throw "Rules file not found: $RulesPath"
}
if (-not (Test-Path $PackageSchemas)) {
    throw "Packaged schemas not found: $PackageSchemas"
}

$RulesBackup = "$RulesPath.before-20260714-$Timestamp.bak"
Copy-Item $RulesPath $RulesBackup -Force

$rules = Get-Content $RulesPath -Raw -Encoding UTF8
$rules = $rules.Replace('C:\dev\ireps\schemas', 'C:\dev\ireps-schemas')
$rules = [regex]::Replace($rules, '\*\*Version:\*\*\s*[^\r\n]+', '**Version:** 1.7', 1)
$rules = [regex]::Replace($rules, '\*\*Effective date:\*\*\s*[^\r\n]+', '**Effective date:** 2026-07-14', 1)
$rules = [regex]::Replace($rules, '\*\*Current phase:\*\*\s*[^\r\n]+', '**Current phase:** `ireps-test` Sales Pipeline baseline complete; governance close-out', 1)

$schemaRepoRule = @'
### 6.1 Authoritative schema repository

The authoritative iREPS collection-schema repository is:

```text
C:\dev\ireps-schemas
```

The Sales Pipeline schemas maintained there are:

```text
conlog-sales-atomic
conlog-sales-monthly
conlog-sales-monthly-lm
conlog-sales-monthly-lm-groups
meter-master
sales-all-meters
```

This rules file governs pipeline execution and safety. The schema repository governs canonical Firestore document identity, shape, field type and field ownership.

A code change that changes a Firestore shape or identity must update the corresponding schema in the same governed change. If code, rules and schemas disagree, the change or upload must stop until the conflict is resolved.

---

'@
if (-not $rules.Contains('### 6.1 Authoritative schema repository')) {
    $rules = $rules.Replace('## 7. Script path rule', $schemaRepoRule + '## 7. Script path rule')
}

$section12 = @'
## 12. Current generated-data and Firestore status

The Lesedi Conlog baseline for `ireps-test` is complete for:

```text
LM / workbase: ZA7423
Provider:      conlog
Period:        2025-09 through 2026-06
Months:        10 continuous months
Completed:     2026-07-14
```

### 12.1 Verified collection counts

```text
conlog_sales_atomic             822,527 documents
conlog_sales_monthly            157,940 documents
conlog_sales_monthly_lm              10 documents
conlog_sales_monthly_lm_groups       50 documents
meter_master                     35,295 documents
sales-all-meters                 35,295 documents
```

### 12.2 Meter Master baseline

```text
Monthly-backed meters:            19,904
Customer-only seeded meters:       3,987
NPR-only seeded meters:           11,404
Total rows/documents:             35,295
CSV SHA-256: 44a604c0ca06e0f4bb624be69ad20155d07fbb6c903d911d4d19fa3fe084108d
```

Approved duplicate outcomes:

```text
Customer placeholder duplicates:    124
Customer Active-status duplicates:    10
Customer latest-purchase duplicates:   1
NPR placeholder duplicates:           13
NPR latest-purchase duplicates:         0
```

### 12.3 Sales All Meters baseline

```text
Meters with sales:                19,904
Meters without sales:             15,391
Total rows/documents:             35,295
Total amount cents:        9,728,029,408
CSV SHA-256: 139e1775ed4404696077ccf5df4355288eabcb0357fbb7ddeebe578d69179087
```

All current Sales All Meters records are `INVISIBLE` because the approved staging Meter Master CSV contained blank `astId` values. This is a derived staging result, not proof that no corresponding operational AST exists anywhere in iREPS.

Earlier Meter Master or Sales All Meters files ending at February 2026 are historical and are not the approved TEST baseline.
'@
$rules = Replace-Section $rules '## 12. Current generated-data status' '## 13. Pipeline dependency and execution order' $section12

$section13 = @'
## 13. Pipeline dependency and execution order

The mandatory data dependency is:

```text
RAW provider download
    -> RAW STAGING
    -> Atomic Sales
    -> Monthly Sales
    -> Meter Master
    -> Sales All Meters
```

### 13.1 Stage operating grains

Stages 00 to 04 operate at one LM and one month per execution:

```text
00_prepare_conlog_raw_sales.py
01_prepare_conlog_sales.py
02_upload_conlog_atomic_v2.py
03_aggregate_monthly_from_atomic_outputs.py
04_upload_conlog_monthly_v3.py
```

Historical Atomic and Monthly loads repeat those monthly commands chronologically.

Stages 05 and 06 are controlled downstream full-period builders. They require one LM plus an explicit continuous `--from-month` and `--to-month` range:

```text
05_build_meter_master_v3.py
06_build_sales_all_meters.py
```

They must dynamically discover and validate every required monthly meter-level file inside that explicit range. They must not contain a fixed historical month list.

Stages 07 and 08 upload one frozen approved full-period CSV to one explicitly selected Firebase project:

```text
07_upload_meter_master_v3.py
08_upload_sales_all_meters.py
```

### 13.2 Firestore upload order

```text
1. conlog_sales_atomic
2. conlog_sales_monthly, conlog_sales_monthly_lm, conlog_sales_monthly_lm_groups
3. meter_master
4. sales-all-meters
```

A downstream build or upload is blocked until every required upstream month is complete, reconciled and verified.

### 13.3 Environment contract

Local builders remain environment-neutral. Every uploader requires explicit project ID, matching project confirmation and a service account whose `project_id` matches the selected project.

### 13.4 Architecture diagram

The editable architecture diagram remains:

```text
docs/architecture/SALES_PIPELINE_FLOW.drawio
```

The diagram is explanatory. This rules file remains the operating authority.
'@
$rules = Replace-Section $rules '## 13. Pipeline dependency and execution order' '## 14. Month-by-month progression rule' $section13

$section14 = @'
## 14. Monthly progression and downstream rebuild rule

New provider sales data is introduced month by month through Stages 00 to 04 without changing Python code.

For each new month:

1. prepare RAW STAGING for one LM/month;
2. build and upload Atomic Sales for that LM/month;
3. build and upload the three Monthly Sales datasets for that LM/month;
4. confirm reconciliation and upload verification.

After the approved monthly source range changes, Meter Master and Sales All Meters are rebuilt from one explicit continuous range. Their builders dynamically discover every month in that range.

The current approved historical baseline is:

```text
2025-09 through 2026-06
```

A future July 2026 refresh will first process July through Stages 00 to 04, then create a new governed downstream range ending at 2026-07. Replacing established downstream Firestore collections requires a separate reviewed refresh or migration plan; `resume` is not a general update mode.
'@
$rules = Replace-Section $rules '## 14. Month-by-month progression rule' '## 15. Raw, raw-staging, and Atomic Sales rule' $section14

$duplicateRule = @'
### 19.3.1 Governed duplicate-resolution rule

The Meter Master builder may use only these Customer Details fields when resolving duplicate meter identities:

```text
MeterNumber
CustomerNo
AccountNo
AccountStatus
LastPurchaseDate
```

Customer name, ERF, physical address, postal address and other premise information are not duplicate-resolution inputs.

For Customer Details duplicates:

1. `CustomerNo = AccountNo = MeterNumber` is a weak placeholder;
2. `CustomerNo = AccountNo` with a value different from `MeterNumber` is the dominant normal pattern and may replace the placeholder;
3. a controlled Active-status rule may select an otherwise supportable `Active` row over a `Block Purchases` row;
4. remaining competing non-placeholder identities may be resolved only by the latest valid `LastPurchaseDate`;
5. tied dates, missing dates or remaining conflicts stop the build.

For 90 Days No Purchase duplicates, a customer number equal to the meter identifier is a weak placeholder. A different non-empty customer number may replace it. Remaining competing non-placeholder customer numbers may be resolved only by the latest valid purchase date; unresolved conflicts stop the build.

The builder must report the count resolved by each approved rule.

'@
if (-not $rules.Contains('### 19.3.1 Governed duplicate-resolution rule')) {
    $rules = $rules.Replace('### 19.4 Firestore document identity', $duplicateRule + '### 19.4 Firestore document identity')
}

$section20 = @'
## 20. Sales All Meters rule

The Firestore collection is:

```text
sales-all-meters
```

Sales All Meters is the governed supporting sales-awareness projection for every approved Meter Master identity, including meters with no sales.

### 20.1 Approved scripts and inputs

```text
Builder:  scripts/06_build_sales_all_meters.py
Uploader: scripts/08_upload_sales_all_meters.py
```

The builder combines one approved Meter Master CSV with every valid monthly meter-level file in an explicit continuous `--from-month` to `--to-month` range.

### 20.2 Staging contract

Fixed columns:

```text
masterId
visibility
meterNo
meterNoNormalized
provider
customerNo
accountNo
totalAmountC
lastPurchaseAtISO
daysSinceLastPurchase
```

They are followed by one dynamic integer-cent column per included month:

```text
amount_YYYY_MM_C
```

`totalAmountC` must equal the sum of all included monthly amount columns. `lastPurchaseAtISO` is the latest valid purchase across the included range. `daysSinceLastPurchase` is calculated from the explicit build as-of date when reproducibility is required.

Every Meter Master identity remains present. A meter with no sales has zero totals and blank CSV last-purchase fields.

### 20.3 Visibility projection

```text
astId populated -> VISIBLE
astId blank     -> INVISIBLE
```

This is a supporting projection and must not be written back into Meter Master.

### 20.4 Firestore shape

```json
{
  "master": {
    "id": "04085345850",
    "visibility": "INVISIBLE"
  },
  "meterNo": "04085345850",
  "meterNoNormalized": "04085345850",
  "provider": "conlog",
  "customerNo": "101517546",
  "accountNo": "101517546",
  "totalAmountC": 125000,
  "monthlyTotalsC": {
    "2025-09": 10000,
    "2025-10": 15000,
    "2025-11": 0,
    "2025-12": 20000,
    "2026-01": 10000,
    "2026-02": 15000,
    "2026-03": 10000,
    "2026-04": 15000,
    "2026-05": 10000,
    "2026-06": 20000
  },
  "lastPurchaseAtISO": "2026-06-27T10:35:00Z",
  "daysSinceLastPurchase": 17
}
```

The `monthlyTotalsC` keys are dynamic. The current TEST schema does not add metadata to Sales All Meters.

### 20.5 Upload safety

The uploader requires explicit `--project-id`, `--confirm-project`, `--service-account`, `--input` and `--mode` values.

Normal mode is `create-only` against an empty collection. `resume` is restricted to recovery from a verified partial upload of the same frozen CSV. Missing documents may be created, exact matches skipped, and conflicts or unexpected documents must stop the upload.

Firestore create operations are required. Broad merge writes are prohibited. Every run records the CSV SHA-256, writes a JSON report and verifies the final collection count.

### 20.6 Completed TEST baseline

```text
LM:                    ZA7423
Period:                2025-09 through 2026-06
Documents:             35,295
Meters with sales:     19,904
Meters without sales:  15,391
Total amount cents:    9,728,029,408
Upload result:         PASS
```
'@
$rules = Replace-Section $rules '## 20. Sales All Meters rule' '## 21. Visibility rule' $section20

$section28 = @'
## 28. Current implementation status and next governance actions

The Lesedi `ireps-test` Sales Pipeline baseline is complete:

1. RAW and RAW STAGING prepared through June 2026.
2. Atomic Sales built, uploaded and verified for ten months.
3. Monthly meter, LM and group datasets built, reconciled and uploaded.
4. Meter Master rebuilt, validated and uploaded with 35,295 documents.
5. Sales All Meters rebuilt, reconciled and uploaded with 35,295 documents.
6. CSV fingerprints and JSON upload reports recorded.
7. Governed script changes committed and pushed.

Immediate governance close-out actions are:

1. keep this rules file current;
2. maintain all six collection schemas under `C:\dev\ireps-schemas`;
3. update the project README and iREPS Master Dictionary where required;
4. commit rules and schema changes in their respective repositories;
5. keep operational CSVs, credentials and upload reports outside Git.
'@
$rules = Replace-Section $rules '## 28. Current implementation sequence' '## 29. Current non-goals' $section28

$section30 = @'
## 30. Definition of done and completion record for the TEST sales sprint

The Lesedi TEST sales-data baseline satisfies the implementation definition of done:

- Atomic and Monthly data cover September 2025 through June 2026;
- the ten months are continuous and reconciled;
- Meter Master and Sales All Meters use the complete approved range;
- all uploaders require explicit environment selection;
- Firestore uploads followed the mandatory dependency order;
- frozen CSV fingerprints and JSON reports were recorded;
- all six target collections were count-verified in `ireps-test`;
- governed script changes were committed and pushed.

```text
Lesedi Conlog Sales Pipeline
ZA7423
2025-09 through 2026-06
ireps-test
DATA AND FIRESTORE BASELINE COMPLETE
```

Governance close-out is complete after this rules update and the authoritative schemas under `C:\dev\ireps-schemas` are reviewed and committed, and Git status confirms no municipal source data or credentials are staged.
'@
$rules = Replace-Section $rules '## 30. Definition of done for the current TEST sales sprint' '## 31. Decision history' $section30

$dynamicHistory = @'
### 2026-07-13 — Dynamic full-period downstream builders confirmed 2026-07-14

Atomic and Monthly stages operate one LM and one month at a time. Meter Master and Sales All Meters are controlled downstream builders that use one LM and one explicit continuous full-period range.

The fixed September 2025 to February 2026 configuration was replaced by dynamic discovery and validation. The governed builders were proven for September 2025 through June 2026.
'@
$historyStart = '### 2026-07-13 — Dynamic full-period builders — SUPERSEDED 2026-07-14'
$historyEnd = '### 2026-07-13 — Environment-neutral builds'
if ($rules.Contains($historyStart)) {
    $rules = Replace-Range $rules $historyStart $historyEnd $dynamicHistory
}

$history20260714 = @'
### 2026-07-14 — Lesedi TEST Sales Pipeline baseline completed

The complete Conlog Sales Pipeline baseline for Lesedi `ZA7423`, covering September 2025 through June 2026, was built, reconciled and uploaded to `ireps-test`.

```text
conlog_sales_atomic             822,527
conlog_sales_monthly            157,940
conlog_sales_monthly_lm              10
conlog_sales_monthly_lm_groups       50
meter_master                     35,295
sales-all-meters                 35,295
```

### 2026-07-14 — Meter Master duplicate-resolution contract proven

Customer Details and 90 Days No Purchase duplicate rows are resolved only through the governed identity, account-status and latest-purchase rules. Customer name, ERF and address are not resolution inputs.

### 2026-07-14 — Governed Sales All Meters scripts approved

`scripts/06_build_sales_all_meters.py` now uses an explicit LM, continuous dynamic range, approved Meter Master input, reproducible as-of date and full reconciliation.

`scripts/08_upload_sales_all_meters.py` requires explicit project selection and controlled `create-only` or `resume` mode. Broad merge behaviour is prohibited.

```text
66f06fb fix: govern sales all meters build and upload
```

### 2026-07-14 — Authoritative schema repository confirmed

```text
C:\dev\ireps-schemas
```

All six completed Sales Pipeline collections require active canonical schema documents there.

'@
if (-not $rules.Contains('### 2026-07-14 — Lesedi TEST Sales Pipeline baseline completed')) {
    $rules = $rules.Replace('## 32. Rule amendment', $history20260714 + "---`r`n`r`n## 32. Rule amendment")
}

Set-Content -Path $RulesPath -Value $rules -Encoding UTF8

# Back up the current schema repository outside the active repository, then replace active schema documents.
if (Test-Path $SchemasRoot) {
    $SchemasBackup = "$SchemasRoot-backup-before-sales-v1-$Timestamp"
    Copy-Item $SchemasRoot $SchemasBackup -Recurse -Force
} else {
    New-Item -Path $SchemasRoot -ItemType Directory -Force | Out-Null
    $SchemasBackup = "not required — repository did not exist"
}

$folders = @(
    'conlog-sales-atomic',
    'conlog-sales-monthly',
    'conlog-sales-monthly-lm',
    'conlog-sales-monthly-lm-groups',
    'meter-master',
    'sales-all-meters'
)

foreach ($folder in $folders) {
    $targetFolder = Join-Path $SchemasRoot $folder
    New-Item -Path $targetFolder -ItemType Directory -Force | Out-Null

    # Remove only active root-level schema Markdown files after the repository backup.
    Get-ChildItem -Path $targetFolder -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '\.schema(\.v[^.]*)?\.md$' -or $_.Name -like '*.schema.md' } |
        Remove-Item -Force
}

Copy-Item (Join-Path $PackageSchemas '*') $SchemasRoot -Recurse -Force

$expected = @(
    (Join-Path $SchemasRoot 'README.md'),
    (Join-Path $SchemasRoot 'conlog-sales-atomic\conlog_sales_atomic.schema.md'),
    (Join-Path $SchemasRoot 'conlog-sales-monthly\conlog_sales_monthly.schema.md'),
    (Join-Path $SchemasRoot 'conlog-sales-monthly-lm\conlog_sales_monthly_lm.schema.md'),
    (Join-Path $SchemasRoot 'conlog-sales-monthly-lm-groups\conlog_sales_monthly_lm_groups.schema.md'),
    (Join-Path $SchemasRoot 'meter-master\meter_master.schema.md'),
    (Join-Path $SchemasRoot 'sales-all-meters\sales_all_meters.schema.md')
)

$missing = $expected | Where-Object { -not (Test-Path $_) }
if ($missing.Count -gt 0) {
    throw "Schema validation failed. Missing: $($missing -join ', ')"
}

Write-Host ""
Write-Host "=== iREPS SALES GOVERNANCE UPDATE COMPLETE ==="
Write-Host "Rules updated:     $RulesPath"
Write-Host "Rules backup:      $RulesBackup"
Write-Host "Schemas updated:   $SchemasRoot"
Write-Host "Schemas backup:    $SchemasBackup"
Write-Host "Schema documents:  6"
Write-Host "Validation:        PASS"
Write-Host ""
Write-Host "No Firebase connection was made. No operational CSV was changed."
