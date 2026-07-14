# iREPS Sales Pipeline Rules

**File:** `rules/SALES_PIPELINE_RULES.md`  
**Project:** `C:\dev\ireps-pipeline-sales`  
**Status:** Governing project rules  
**Version:** 1.6  
**Effective date:** 2026-07-14  
**Current phase:** `ireps-test` stabilisation  
**Current provider:** Conlog  
**Current LM / workbase:** Lesedi — `ZA7423`

---

## 1. Purpose

This file is the governing architecture, implementation, data, safety, and operating contract for the iREPS Sales Pipeline.

It exists to preserve agreed decisions across developers, future maintainers, Codex and other AI agents, pipeline operators, documentation work, environment migrations, and future Trials and Production preparation.

The rules in this file must be read before analysing, designing, changing, running, or documenting this project.

---

## 2. Authority

The authority order for this project is:

1. Locked canonical collection schemas under `C:\dev\ireps\schemas` for Firestore document identity, shape, field type, and field ownership
2. `rules/SALES_PIPELINE_RULES.md` for pipeline architecture, implementation, safety, and operating behaviour
3. Approved iREPS architecture and data decisions
4. The iREPS Master Dictionary
5. Confirmed current code behaviour
6. Sprint instructions
7. Chat discussions
8. Assumptions

A pipeline rule must not invent a Firestore field that is absent from the applicable locked canonical schema.

A schema document must not silently redefine pipeline execution, environment safety, or upload controls governed by this rules file.

If code conflicts with this rules file, do not silently change either one.

The conflict must first be identified and classified as one of the following:

- the code is outdated or incorrect;
- the rules file is outdated;
- an agreed migration is incomplete;
- the implementation differs intentionally and needs documentation.

No developer or agent may override an agreed rule through an undocumented code change.

---

## 3. Mandatory read-first rule

Before working on this project:

1. Read this entire file.
2. Read the Sales Pipeline section in the iREPS Master Dictionary.
3. Inspect the current repository tree.
4. Inspect the current scripts involved in the requested change.
5. Confirm the target environment before any upload or destructive action.
6. Report any conflict between the rules and current code before editing.

Every Codex or AI-agent request must begin with an instruction equivalent to:

> Read `rules/SALES_PIPELINE_RULES.md` first. Treat it as the governing implementation contract. Do not make changes that contradict it. Report conflicts before editing.

---

## 4. Official terminology source

There is only one iREPS Master Dictionary.

The official dictionary is maintained in:

```text
C:\dev\ireps-academy\15-dictionary\
iREPS_Master_Dictionary_v1.3.md
```

The Master Dictionary is the single source of truth for iREPS terminology.

This repository must not create a separate module dictionary.

Sales Pipeline terms must be added to or refined in a dedicated section of the Master Dictionary:

```text
Sales Pipeline Concepts
```

New business, data, collection, architecture, or pipeline terms must not be introduced without an approved definition in the Master Dictionary.

This rules file defines how the pipeline must operate. The Master Dictionary defines what approved terms mean.

---

## 5. Naming rule

Use the generic word `sales` for new project, rules, architecture, and internal governance names.

Preferred examples:

```text
sales
sales_pipeline
sales_atomic
sales_monthly
sales_all_meters
SALES_PIPELINE_RULES.md
```

Avoid introducing `prepaid` or `PREPAID` into new generic project and governance filenames.

The word `prepaid` may still appear where it accurately describes the current Conlog source data, a source-system filename, an existing input filename, or a business explanation.

Existing filenames such as the following do not need to be renamed during the current sprint:

```text
conlog_prepaid_sales__ZA7423__2026-06.csv
```

---

## 6. Approved repository structure

The approved project structure is:

```text
C:\dev\ireps-pipeline-sales
│
│   README.md
│   .gitignore
│
├── rules
│   └── SALES_PIPELINE_RULES.md
│
├── scripts
│   ├── 00_prepare_conlog_raw_sales.py
│   ├── 01_prepare_conlog_sales.py
│   ├── 02_upload_conlog_atomic_v2.py
│   ├── 03_aggregate_monthly_from_atomic_outputs.py
│   ├── 04_upload_conlog_monthly_v3.py
│   ├── 05_build_meter_master_v3.py
│   ├── 06_build_sales_all_meters.py
│   ├── 07_upload_meter_master_v3.py
│   └── 08_upload_sales_all_meters.py
│
├── docs
│   ├── architecture
│   │   └── SALES_PIPELINE_FLOW.drawio
│   └── project-history
│
├── input
│   ├── conlog_sales
│   ├── raw-sales
│   └── reference
│
└── output
    ├── atomic
    ├── logs
    ├── meter_master
    ├── monthly
    ├── monthly_lm
    ├── monthly_lm_groups
    └── sales_all_meters
```

The root folder must remain clean.

Python scripts belong in `scripts`.

Project-history files belong in:

```text
docs/project-history
```

The already-cleaned `input` and `output` structures must not be reorganised without an agreed design change.

---

## 7. Script path rule

Scripts must not depend on being located in the project root.

Every script must resolve the repository root from its own file location.

The standard pattern is:

```python
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUT_DIR = PROJECT_ROOT / "input"
OUTPUT_DIR = PROJECT_ROOT / "output"
```

All input, output, reference, log, and staging paths must be derived from `PROJECT_ROOT`.

Scripts must work when launched from the repository root, for example:

```powershell
python .\scripts\01_prepare_conlog_sales.py
```

The moved scripts must not be run until their path handling has been inspected and updated.

---

## 8. Environment model

The iREPS environments are separate from the sales provider.

Current and planned environments are:

```text
ireps2           DEV
ireps-test       TEST
ireps-trials     Future Trials environment
ireps-production Future Production environment
```

Current sprint target:

```text
ireps-test
```

The local build scripts must remain environment-neutral.

Build scripts must not contain a silent Firebase project selection.

Upload scripts must require an explicit target project, such as:

```powershell
--project-id ireps-test
```

Future examples:

```powershell
--project-id ireps-trials
--project-id ireps-production
```

Production must never be selected by default.

If the target project is missing, unclear, or unsupported, the upload must stop.

---

## 9. Provider model for the current phase

The current provider is:

```text
conlog
```

The current Firestore collection family remains:

```text
conlog_sales_atomic
conlog_sales_monthly
conlog_sales_monthly_lm
conlog_sales_monthly_lm_groups
```

These names remain active during TEST stabilisation.

Do not rename them during the current sprint.

Do not place another vending provider's data into these collections during the current sprint.

The future provider-neutral architecture is deferred until Trials Readiness.

Possible future providers include Conlog, Landis+Gyr, and others.

The provider-neutral redesign must be handled as a separate controlled architecture and migration sprint.

---

## 10. Deferred provider-neutral reset

The long-term preferred architecture is expected to use generic sales collections with an explicit provider property.

That refactor is not part of the current TEST stabilisation work.

The provider-neutral reset is deferred until iREPS is stable and ready for `ireps-trials`.

The future reset is expected to include:

1. Codex full codebase inventory.
2. Search for every sales-related collection, field, API, rule, index, report, and UI reference.
3. Final provider-neutral schema.
4. Cadastral corrections.
5. Sales pipeline corrections.
6. Firestore rule and index corrections.
7. Web and mobile query updates.
8. Clean cadastral rebuild.
9. Clean sales rebuild.
10. Clean Meter Master rebuild.
11. Clean Sales All Meters rebuild.
12. Fresh loading of DEV and TEST.
13. Creation of Trials from the corrected architecture.
14. Creation of Production from the approved Trials architecture.

The future reset is intended to be a clean reload, not an uncontrolled mixed backfill.

Until that sprint is formally opened, continue using `conlog_sales_xxx`.

---

## 11. Current source-data scope

Current LM / workbase:

```text
Lesedi
ZA7423
```

The current Conlog source-data path has two separate pre-Atomic layers:

```text
RAW provider download
input/raw-sales

RAW STAGING
input/conlog_sales
```

`input/raw-sales` contains the original CSV files downloaded from the Conlog sales portal. These files are source evidence and must remain unchanged.

`input/conlog_sales` contains generated, standardised Conlog raw-staging files. It is not the Atomic layer.

Current raw-staging coverage is:

```text
2025-09 through 2026-06
```

Current original portal downloads under `input/raw-sales` include April, May, and June 2026.

Current reference files are:

```text
input/reference/Customer_Details.csv
input/reference/90_Days_No_Purchase_Report.csv
```

All source CSV files are operational data and must not be committed to Git.

---

## 12. Current generated-data status

Current verified position as at 2026-07-14:

```text
Prepared Conlog RAW STAGING:
2025-09 through 2026-06

Atomic outputs:
2025-09 through 2026-06

Atomic Firestore upload:
ireps-test / conlog_sales_atomic
2025-09 through 2026-06
822,527 documents
UPLOAD_VERIFIED for all ten months

Monthly outputs:
historical files currently exist through 2026-03
the complete 2025-09 through 2026-06 rebuild is pending

Meter Master output:
historical file currently covers 2025-09 through 2026-02
it must be rebuilt against the locked canonical Meter Master schema

Sales All Meters output:
historical file currently covers 2025-09 through 2026-02
it must be rebuilt after the approved Meter Master rebuild
```

The existing historical Monthly, Meter Master, and Sales All Meters files must be preserved until their complete replacement files are generated and verified.

The final ten successful Stage 02 upload manifests are the evidence that the Atomic layer is loaded and verified in `ireps-test`.

---

## 13. Pipeline dependency and execution order

The Sales Pipeline has two related but different orders:

1. the **data-build dependency order**; and
2. the **Firestore upload order**.

These orders are mandatory. Script numbering alone must not be used to infer the correct operational sequence.

### 13.1 Data-build dependency order

The approved build flow is:

```text
Original Conlog portal CSV
input/raw-sales
    ↓
scripts/00_prepare_conlog_raw_sales.py
    ↓
Standardised Conlog RAW STAGING CSV
input/conlog_sales
    ↓
scripts/01_prepare_conlog_sales.py
    ↓
Upload-ready Atomic Sales CSV
output/atomic
    ↓
scripts/03_aggregate_monthly_from_atomic_outputs.py
    ↓
Monthly meter-level sales CSVs
    ↓
scripts/05_build_meter_master_v3.py
    ↓
Meter Master CSV
    ↓
scripts/06_build_sales_all_meters.py
    ↓
Sales All Meters CSV
```

The complete dependency statement is:

```text
RAW
    ↓
RAW STAGING
    ↓
Atomic Sales
    ↓
Monthly Sales
    ↓
Meter Master
    ↓
Sales All Meters
```

The monthly aggregation stage is mandatory between Atomic Sales and Meter Master.

Meter Master depends on completed and reconciled monthly meter-level sales files. It does not depend on Sales All Meters.

Sales All Meters depends on both:

```text
the approved Meter Master CSV
+
the valid monthly meter-level sales CSVs
```

No downstream dataset may be built from an incomplete, unreconciled, or ambiguous upstream dataset.

### 13.2 Firestore upload order

For each approved LM/month, after that month’s required CSV outputs have been generated and validated, Firestore uploads must run in this order:

```text
1. Upload Atomic Sales
2. Upload Monthly Sales collections
3. Upload Meter Master
4. Upload Sales All Meters
```

The corresponding scripts and collections are:

```text
1. scripts/02_upload_conlog_atomic_v2.py
   -> conlog_sales_atomic

2. scripts/04_upload_conlog_monthly_v3.py
   -> conlog_sales_monthly
   -> conlog_sales_monthly_lm
   -> conlog_sales_monthly_lm_groups

3. scripts/07_upload_meter_master_v3.py
   -> meter_master

4. scripts/08_upload_sales_all_meters.py
   -> sales-all-meters
```

Meter Master must not be processed or uploaded for a selected month until that month’s Atomic and Monthly Sales data is complete and validated.

Sales All Meters must not be processed or uploaded for a selected month until the approved Meter Master result for that month exists.

### 13.3 Operating classification

Script 00 forms the provider-download-to-raw-staging preparation lane.

Scripts 01 to 04 form the Atomic and Monthly Sales processing lane.

Scripts 05 and 06 are controlled downstream monthly builders.

Scripts 07 and 08 are controlled downstream monthly upload or update stages.

Every pipeline stage from 00 through 08 must operate at the normal grain of one LM and one month per execution.

Historical loading, recovery, or backfilling must repeat the approved monthly command for each month in chronological order. A full historical period must not be hidden inside one stage execution.

### 13.4 Universal one-LM, one-month execution contract

The normal operating contract for every Sales Pipeline Python stage is:

```text
one LM + one month per execution
```

Every applicable stage must require:

```text
--lm-pcode <LM_PCODE>
--month YYYY-MM
```

The following range-style arguments are prohibited in normal pipeline stages:

```text
--from-month
--to-month
--start-month
--end-month
--date-range
```

Mandatory behaviour:

- one execution must select exactly one month of its primary upstream sales input;
- one execution must produce or upload only the selected month’s governed result;
- a script must not silently discover and process all available months;
- a script must not hide a historical backfill loop inside the stage;
- historical months must be run individually and in chronological order;
- future monthly data must use the same command and code path as historical data;
- each stage must create a month-specific report or manifest;
- external orchestration may call a stage repeatedly, but each child execution must still be one LM and one month;
- cumulative downstream datasets may read approved prior state or reference data where their design requires it, but the new sales input selected by the execution must be one month only.

Any current Stage 03 to Stage 08 code that still assumes a full range must be updated before that script is approved for execution.

### 13.5 Architecture diagram

The editable architecture diagram for this flow is maintained at:

```text
docs/architecture/SALES_PIPELINE_FLOW.drawio
```

The diagram is explanatory. This rules file remains the governing implementation authority.

---

## 14. Month-by-month progression rule

The pipeline must support indefinite monthly progression without code changes.

A new month must be processed by supplying a new `--month YYYY-MM` value to the same approved stages.

The scripts must not hard-code a fixed list such as:

```text
2025-09 to 2026-02
```

The current historical rebuild covers September 2025 through June 2026, but it must be executed as ten separate monthly runs:

```text
2025-09
2025-10
2025-11
2025-12
2026-01
2026-02
2026-03
2026-04
2026-05
2026-06
```

When July 2026 data becomes available, the pipeline must continue with a normal July execution. No Python file should need a new date range or code edit.

Routine Stage 03 execution must create only the three selected-month outputs. Combined `ALL` CSV files are not approved as Stage 03’s normal operating output and must not be required by downstream stages.

Downstream stages must be redesigned, where necessary, to progress month by month and to preserve or update approved cumulative state without rebuilding an entire historical date range during every normal monthly run.

---

## 15. Raw, raw-staging, and Atomic Sales rule

### 15.1 RAW provider-download layer

The RAW layer is the original CSV downloaded from the vending provider portal.

For the current Conlog process, RAW files belong only in:

```text
input/raw-sales
```

RAW files must:

- preserve the original provider CSV contents unchanged as source evidence;
- never be manually converted into Atomic files;
- never be overwritten by Stage 00;
- remain excluded from Git.

For controlled local identity, the downloaded Conlog CSV may be renamed without opening, editing, re-saving, or changing its contents. The approved RAW filename contract is:

```text
conlog_raw_sales__<lmPcode>__YYYY-MM.csv
```

The local rename is filename governance only. It must not alter any CSV header, row, value, encoding, delimiter, quoting, line ending, or other source content.

The normal monthly operator responsibility is limited to:

1. downloading the original provider CSV;
2. renaming it to the approved RAW filename contract where required; and
3. placing it in `input/raw-sales` without modifying the CSV contents.

### 15.2 RAW STAGING layer

RAW STAGING is the standardised, provider-specific input produced from the original RAW provider download.

For Conlog, RAW STAGING belongs only in:

```text
input/conlog_sales
```

The approved Conlog raw-staging schema is exactly:

```text
lmPcode
txAt
meterNo
amountTotalC
costC
vatC
```

RAW STAGING is not Atomic Sales and must not be uploaded to Firestore.

The approved Stage 00 script is:

```text
scripts/00_prepare_conlog_raw_sales.py
```

Its sole transformation responsibility is:

```text
input/raw-sales
    -> input/conlog_sales
```

Stage 00 must create the approved filename pattern:

```text
conlog_prepaid_sales__<lmPcode>__YYYY-MM.csv
```

#### 15.2.1 Stage 00 monthly execution contract

Stage 00 must run one LM and one month at a time. One execution selects exactly one source file.

The approved validation command is:

```powershell
python .\scripts\00_prepare_conlog_raw_sales.py `
  --lm-pcode ZA7423 `
  --month 2026-04 `
  --preflight-only
```

The approved write command is:

```powershell
python .\scripts\00_prepare_conlog_raw_sales.py `
  --lm-pcode ZA7423 `
  --month 2026-04
```

For the requested LM and month, Stage 00 must require exactly:

```text
input/raw-sales/conlog_raw_sales__<lmPcode>__YYYY-MM.csv
```

and must plan or create exactly:

```text
input/conlog_sales/conlog_prepaid_sales__<lmPcode>__YYYY-MM.csv
```

The `--lm-pcode` and `--month` arguments are mandatory. The filename LM and month, the requested LM and month, and every valid transaction month must agree.

#### 15.2.2 Stage 00 validation and safety contract

Stage 00 must:

- resolve all paths from the repository root;
- stop if the exact monthly RAW source file is missing;
- stop if the RAW filename does not match the approved identity contract;
- stop if required Conlog portal columns are missing;
- validate dates, month membership, meter numbers, meter-column consistency, monetary values, refunds, and amount/cost/VAT reconciliation;
- preserve all valid rows, including duplicate six-field staging rows;
- report duplicate six-field staging rows as preserved, not as confirmed duplicate portal transactions;
- write a rejected-row report when any rows fail validation;
- write no RAW STAGING output when any rejected rows exist;
- write no RAW STAGING output in `--preflight-only` mode;
- leave an identical existing RAW STAGING output unchanged;
- block replacement of a different existing output unless `--replace-existing` is deliberately supplied after review;
- write through a temporary file and verify the planned SHA-256 before finalising the output;
- never edit, rename, move, delete, or overwrite the RAW source file; and
- never connect to Firebase or upload data.

`--replace-existing` is an exceptional controlled recovery or correction option. It is not part of the normal monthly command.

#### 15.2.3 Stage 00 traceability and logs

Every Stage 00 run must report at least:

```text
RAW filename
LM and month
rows read
rows prepared
rows rejected
unique meter count
duplicate six-field staging rows preserved
amount, cost, and VAT totals
RAW SHA-256
planned or written output SHA-256
planned or written output path
```

Every successful write run must create a timestamped summary under:

```text
output/logs/stage00_prep_summary__<UTC timestamp>.csv
```

When rejected rows exist, Stage 00 must create a timestamped rejected-row report under `output/logs` and terminate without writing RAW STAGING.

The operator, developer, or AI agent must not manually prepare or edit files under `input/conlog_sales` as a normal operating step. Corrections must be made through the Stage 00 mapping, validation, rejection, and rerun process.

Stage 00 must preserve source traceability, validate the source month and LM, preserve meter numbers as strings, standardise the approved six fields, report rejected rows, and stop on an ambiguous or unsafe source structure.

### 15.3 Atomic Sales layer

An Atomic Sales record represents one normalised source sales transaction.

The Atomic layer is the transaction-level source of truth for downstream monthly aggregation.

The approved Stage 01 script is:

```text
scripts/01_prepare_conlog_sales.py
```

Its sole transformation responsibility is:

```text
input/conlog_sales
    -> output/atomic
```

Files under `output/atomic` are upload-ready Atomic CSVs. They are not considered uploaded until the approved Atomic uploader writes them to `conlog_sales_atomic` and the upload is verified.

Atomic processing must preserve enough source lineage to support reconciliation, including where available:

- vending-provider identity;
- LM pCode;
- meter number;
- transaction or source identifier;
- transaction date and time;
- amount;
- raw-staging source filename;
- source row or trace reference;
- ingestion or preparation context.

Atomic files must not be manually edited after successful generation except through an agreed correction and rerun process.

---

## 16. Monthly-sales rule

Monthly outputs must be derived only from approved Atomic outputs.

The monthly layer must not independently reinterpret RAW or RAW STAGING files after the Atomic layer is approved.

The current TEST collection family remains:

```text
conlog_sales_monthly
conlog_sales_monthly_lm
conlog_sales_monthly_lm_groups
```

The corresponding local datasets are:

```text
output/monthly
output/monthly_lm
output/monthly_lm_groups
```

The Firestore document identities are:

```text
conlog_sales_monthly/{lmPcode}__{normalizedMeterNo}__{ym}

conlog_sales_monthly_lm/{lmPcode}__{ym}

conlog_sales_monthly_lm_groups/{lmPcode}__{ym}__{salesGroupId}
```

The exact Firestore document fields are governed by the applicable canonical schema documents under:

```text
C:\dev\ireps\schemas
```

Pipeline scripts must not add metadata, provider, source, status, visibility, or other fields that are absent from the approved monthly schemas.

### 16.1 Stage 03 build contract

The approved builder is:

```text
scripts/03_aggregate_monthly_from_atomic_outputs.py
```

Stage 03 must:

- resolve all paths from `PROJECT_ROOT`;
- remain environment-neutral and never connect to Firebase;
- require explicit `--lm-pcode` and `--month` arguments;
- process exactly one LM and one month per execution;
- select exactly one valid Atomic CSV for that LM/month;
- validate the exact Atomic CSV schema and governed Atomic identities;
- use the common meter-number normalisation rule without a fixed length restriction;
- stop on duplicate Atomic identities in the selected Atomic file;
- build exactly one meter-month file, one LM-month file, and one LM-month-group file for the selected month;
- reconcile transaction count, meter count, amount, cost, VAT, and first/last transaction times across all four selected-month layers;
- protect different existing outputs for the selected month unless `--replace-existing` is deliberately supplied;
- leave outputs for every other month untouched;
- write through temporary files and verify SHA-256;
- create one month-specific JSON Stage 03 build report and manifest;
- never accept `--from-month` or `--to-month`;
- never build combined `ALL` files as part of the normal monthly execution.

Stage 03 preflight must perform all validation and reconciliation without writing monthly CSV outputs.

A successful write run must record:

```text
result = BUILD_WRITTEN
status = PASS
```

The Stage 03 manifest is the only approved Stage 04 input selector. Stage 04 must not upload files merely because they exist in an output folder.

### 16.2 Stage 04 upload contract

The approved uploader is:

```text
scripts/04_upload_conlog_monthly_v3.py
```

Stage 04 operates at this grain:

```text
one Firebase project + one LM + one month per execution
```

Stage 04 must consume a Stage 03 manifest for exactly the same one LM/month. It must not consume a multi-month manifest or upload a date range in one execution.

Stage 04 must:

- require explicit `--project-id`;
- require matching `--confirm-project`;
- require an explicit service-account path;
- verify the service-account `project_id` before Firebase starts;
- require a successful Stage 03 `BUILD_WRITTEN` manifest;
- select exactly the three requested monthly CSVs from that manifest;
- verify every CSV SHA-256;
- validate exact CSV schemas, deterministic document IDs, field values, and cross-dataset reconciliation;
- verify the governed Conlog vending-provider document exists and is active;
- preflight all three Firestore collection scopes before any write begins;
- use `create-only` as the normal mode;
- support `resume` only for verified recovery from a partial upload of the same Stage 03 outputs;
- use Firestore create operations only;
- never use `merge=True`;
- never silently update, overwrite, delete, or skip a conflicting document;
- verify final counts and deterministic sample documents;
- create a JSON audit report for every preflight and execute attempt.

In `create-only` mode, all three target LM/month scopes must be empty.

In `resume` mode:

- every existing expected document must exactly match the CSV;
- missing expected documents may be created;
- conflicting documents block the run;
- unexpected extra documents block the run.

A broad merged write is prohibited:

```python
batch.set(document_ref, document, merge=True)
```

The approved normal write is a Firestore create operation:

```python
batch.create(document_ref, document)
```

### 16.3 Monthly reconciliation rule

For each LM/month:

```text
Atomic
    =
sum of conlog_sales_monthly
    =
conlog_sales_monthly_lm
    =
sum of conlog_sales_monthly_lm_groups
```

The following values must reconcile:

```text
purchasesCount
metersCount
amountTotalC
costC
vatC
firstPurchaseAtMs
lastPurchaseAtMs
```

The monetary equation must hold at every layer:

```text
amountTotalC = costC + vatC
```

Historical combined `ALL` files are not authoritative routine outputs. Stage 03 must not create or require them during normal monthly execution, and downstream stages must not depend on them.

---

## 17. Meter-number normalisation

The same meter-number normalisation rule must be used across raw sales preparation, atomic data, monthly data, Meter Master, Sales All Meters, backend lookup, meter discovery integration, and later provider adapters.

Current expected rules include:

- cast to string;
- trim leading and trailing whitespace;
- remove embedded whitespace;
- convert letters to uppercase;
- preserve leading zeroes;
- do not remove meaningful letters;
- return an empty value for missing or invalid blank input.

There is no universal fixed meter-number length rule.

A meter number must not be padded, truncated, rejected, or rewritten merely to force 11 characters.

Meter numbers must never be treated as numeric values where leading zeroes can be lost.

---

## 18. Monetary-value rule

Sales amounts must use a clear and consistent unit.

From the Atomic Sales layer onward, fields ending in `C` represent integer cents.

Examples:

```text
amountTotalC
totalAmountC
amount_2026_06_C
```

### 18.1 Controlled Conlog RAW STAGING exception

The current Conlog RAW STAGING schema contains three historical column names:

```text
amountTotalC
costC
vatC
```

In `input/conlog_sales` only, these three fields contain the provider's validated decimal rand source values, for example `100.00`, `86.96`, and `13.04`. Their historical `C` suffix does not mean that the RAW STAGING text is already expressed as integer cents.

Stage 00 parses these values into cents internally for validation and reconciliation, but writes the approved decimal rand source representation to RAW STAGING.

Stage 01 is the controlled conversion boundary. It must convert the RAW STAGING decimal rand values exactly once into integer cents for Atomic Sales, for example:

```text
100.00 -> 10000
86.96  -> 8696
13.04  -> 1304
```

From `output/atomic` onward, every approved `*C` field must contain integer cents. No downstream stage may reinterpret Atomic cent values as rand values or convert them a second time.

Do not mix rand values and cent values within the same governed layer.

Conversion must happen once at the Stage 01 boundary and must be validated.

Aggregations must use integer-cent arithmetic wherever possible.

---

## 19. Meter Master rule

`meter_master` is the thin identity and cross-reference bridge between the sales-side meter universe and iREPS operational meter identity.

It is not a transaction-history, premise, ERF, TRN, status, or service-provider collection.

### 19.1 Approved scripts

The approved Meter Master staging builder is:

```text
scripts/05_build_meter_master_v3.py
```

The approved reusable Firestore uploader is:

```text
scripts/07_upload_meter_master_v3.py
```

`scripts/07_upload_meter_master_v2.py` is historical. It is not approved for TEST, Trials, or Production because it contains hard-coded environment and input-file configuration.

The existing `scripts/05_build_meter_master_v3.py` is the approved builder and must not be recreated or renamed.

### 19.2 Builder input rule

The normal Stage 05 operating grain is:

```text
one LM + one month per execution
```

The selected sales input must be exactly:

```text
output/monthly/monthly__<scope>__YYYY-MM__from_atomic.csv
```

The builder may also use the approved reference files:

```text
input/reference/Customer_Details.csv
input/reference/90_Days_No_Purchase_Report.csv
```

Stage 05 must require `--lm-pcode` and `--month`. It must not accept a start month, end month, or selected date range.

The builder does not read raw Conlog source files directly. The selected Conlog month must first pass through the approved preparation, Atomic, and monthly aggregation stages.

The current Stage 05 implementation must not be executed if it still discovers or rebuilds a full historical month range. It must first be updated to comply with the one-LM, one-month contract while preserving the locked Meter Master schema and approved prior state.

### 19.3 Approved staging CSV schema

The output from `05_build_meter_master_v3.py` is a flat staging CSV with exactly these columns:

```text
masterId
lmPcode
meterNoRaw
meterNoNormalized
meterType
customerNo
accountNo
salesId
salesProvider
astId
```

The normalized meter number is the merge key across monthly sales, Customer Details, and the 90 Days No Purchase report.

The current provider value is:

```text
conlog
```

The builder must apply these identity rules:

```text
masterId = meterNoNormalized
salesId = meterNoNormalized
salesProvider = conlog
```

Customer Details may populate or improve `meterNoRaw`, `customerNo`, and `accountNo`.

The 90 Days No Purchase report may add meters that are absent from the other inputs and may populate `customerNo` only when a stronger customer value is not already present.

### 19.4 Firestore document identity

The uploader writes to:

```text
meter_master/{masterId}
```

Because `masterId` equals `meterNoNormalized`, the Firestore document ID is the normalized meter number.

This deterministic document-ID design must not be changed casually because AST links, direct lookups, duplicate prevention, rebuilds, and other application logic may depend on it.

### 19.5 Approved Firestore schema

Every pipeline-created Meter Master document must use this shape:

```json
{
  "lmPcode": "ZA7423",
  "meterNo": {
    "raw": "04085345850",
    "normalized": "04085345850"
  },
  "meterType": "electricity",
  "customerNo": "101516969",
  "accountNo": "101516969",
  "refs": {
    "asts": {
      "id": ""
    },
    "sales": {
      "id": "04085345850",
      "provider": "conlog"
    }
  },
  "metadata": {
    "createdAt": "Firestore Timestamp",
    "createdByUid": "SYSTEM",
    "createdByUser": "METER MASTER PIPELINE",
    "updatedAt": "Firestore Timestamp",
    "updatedByUid": "SYSTEM",
    "updatedByUser": "METER MASTER PIPELINE"
  }
}
```

The staging-to-Firestore mapping is:

```text
lmPcode             -> lmPcode
meterNoRaw          -> meterNo.raw
meterNoNormalized   -> meterNo.normalized
meterType           -> meterType
customerNo          -> customerNo
accountNo           -> accountNo
astId               -> refs.asts.id
salesId             -> refs.sales.id
salesProvider       -> refs.sales.provider
masterId            -> Firestore document ID
```

### 19.6 Metadata rule

`metadata` is mandatory on every Meter Master document.

The six required fields are:

```text
createdAt
createdByUid
createdByUser
updatedAt
updatedByUid
updatedByUser
```

Timestamps must be Firestore Timestamp values.

On first creation, the pipeline sets both the `created*` and `updated*` fields.

On a rerun, the uploader must preserve existing `metadata.createdAt`, `metadata.createdByUid`, and `metadata.createdByUser`, and refresh only the three `metadata.updated*` fields.

The pipeline system actor is:

```text
createdByUid / updatedByUid   = SYSTEM
createdByUser / updatedByUser = METER MASTER PIPELINE
```

### 19.7 Ownership and month-by-month update rule

Meter Master must progress through controlled monthly processing.

The normal Stage 05 and Stage 07 operating grain is:

```text
one Firebase project + one LM + one month per execution
```

For the selected month:

```text
missing Meter Master document
    -> create the complete canonical Meter Master document

existing Meter Master document
    -> update only the exact Sales Pipeline-owned fields approved for that month
```

The Sales Pipeline owns these business fields where it has approved source values:

```text
lmPcode
meterNo.raw
meterNo.normalized
meterType
customerNo
accountNo
refs.sales.id
refs.sales.provider
metadata.updatedAt
metadata.updatedByUid
metadata.updatedByUser
```

On first creation, the Sales Pipeline also creates the full six-field `metadata` object.

Meter discovery and meter installation workflows own:

```text
refs.asts.id
```

Mandatory protections:

- preserve existing `refs.asts.id`;
- preserve existing `metadata.createdAt`, `metadata.createdByUid`, and `metadata.createdByUser`;
- never submit a broad complete-document merge against an existing Meter Master document;
- never use `merge=True` as a substitute for field ownership;
- update only explicit approved dot paths;
- stop and report identity or ownership conflicts;
- process only the selected month’s approved monthly input;
- create a month-specific preflight and upload report.

The current Stage 05 and Stage 07 implementations must not be executed until they are confirmed or updated to follow this month-by-month rule and the locked Meter Master schema.

### 19.8 Controlled resume rule

`resume` may be used only to recover from a verified partial failure of the same LM/month execution and the same approved input fingerprint.

In `resume` mode:

```text
missing planned creation
    -> create it

existing matching planned result
    -> skip it

existing conflicting result
    -> stop and report the conflict
```

`resume` is not a general update mode and must not introduce a different month, different CSV, or changed business data into the same recovery run.

Normal progression from one month to the next uses a new explicit monthly execution, not `resume`.

### 19.9 Reusable cross-project uploader rule

The same approved Meter Master CSV must be reusable across Firebase projects.

The uploader must not hard-code:

```text
Firebase project ID
service-account path
input CSV path
```

It must require explicit runtime values for:

```text
--project-id
--confirm-project
--service-account
--input
--mode
```

The value supplied through `--confirm-project` must exactly match `--project-id`.

Before connecting to Firestore, the uploader must read the `project_id` inside the service-account JSON and verify that it exactly matches the requested target project.

A mismatch must stop the upload before any Firestore write.

The approved operating model is:

```text
Build once
    -> validate and freeze one Meter Master CSV
    -> upload the same CSV to each approved Firebase project
```

The CSV must not be manually changed between TEST, Trials, and Production uploads.

The uploader must calculate and report the CSV SHA-256 fingerprint so that uploads to different projects can be proven to use the same source file.

### 19.10 Validation and reporting rule

Before upload, the uploader must verify:

- the exact ten-column staging schema and column order;
- non-empty `masterId`;
- unique `masterId`;
- `masterId = meterNoNormalized`;
- `salesId = meterNoNormalized`;
- valid project confirmation;
- service-account project match;
- approved upload mode;
- target collection state appropriate to that mode.

The uploader must display a preflight summary containing at least:

```text
target project
target collection
input CSV
row count
unique master ID count
LM pCode values
provider values
CSV SHA-256
upload mode
```

Every run must produce a JSON report under:

```text
output/logs/meter_master
```

The report must record the project, collection, input file, fingerprint, mode, counts, result, and any conflict or failure.

No Meter Master upload is complete until the resulting Firestore document count and a sample of document shapes have been verified.

The identity design must be reviewed before multi-LM or multi-provider Production rollout.

---

## 20. Sales All Meters rule

`sales_all_meters` is a supporting all-meter sales summary.

It is built from Meter Master and valid monthly meter-level sales outputs.

It may include:

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
dynamic monthly amount columns
```

The normal Stage 06 operating grain is one LM and one month per execution.

Stage 06 must require `--lm-pcode` and `--month`. The selected month may add or update that month’s governed sales values in the cumulative Sales All Meters projection.

`totalAmountC` must reconcile to the approved cumulative monthly values represented by the resulting record.

The last purchase date must remain the latest valid purchase date after the selected month is applied.

The current Stage 06 implementation must not be executed if it still requires a discovered historical range or a full-period rebuild. It must first be updated to comply with the one-LM, one-month contract.

---

## 21. Visibility rule

Visibility terminology must follow the iREPS Master Dictionary.

The backend remains the authority.

A form-side indication may assist the user, but it must not be treated as final truth.

The pipeline must not infer operational linkage beyond what the approved Meter Master and backend rules support.

Any change to `VISIBLE` or `INVISIBLE` behaviour requires review across pipeline, backend, mobile, web, Meter Master, Master Dictionary, and rules.

---

## 22. Validation and reconciliation

No upload is complete until validation is complete.

Minimum validation must include, where applicable:

- input file count;
- input row count;
- output row count;
- rejected or skipped row count;
- duplicate count;
- unique meter count;
- total amount in cents;
- earliest purchase date;
- latest purchase date;
- monthly totals;
- LM totals;
- LM group totals;
- Meter Master row count;
- Sales All Meters row count;
- missing-month detection;
- source-to-output reconciliation.

A script must fail loudly when a required file is missing.

A script must not silently continue after a critical validation failure.

Warnings must be clear and distinguishable from successful completion.

---

## 23. Upload safety

Upload scripts must:

- require an explicit Firebase project ID;
- display the target project before writing;
- display the target collections;
- display input filenames and row counts;
- use controlled batch sizes;
- report progress;
- report total successful writes;
- report failures;
- stop on invalid configuration;
- avoid defaulting to Production;
- avoid silently switching environments.

Before an upload, the operator must confirm:

```text
provider
LM
month
target project
target collections
input row count
expected operation
```

Destructive delete-and-reload operations require a separate agreed reset plan.

---

## 24. Git and data-protection rules

The repository is initialised on branch:

```text
main
```

Initial checkpoint:

```text
21468f6 chore: initialise sales pipeline repository structure
```

The following must not be committed:

- municipal sales CSV files;
- customer reference data;
- meter reference data;
- generated output CSV files;
- service-account files;
- Firebase credentials;
- `.env` secrets;
- local credentials;
- large operational datasets.

The `.gitignore` must continue to exclude:

```text
input/**
output/**
credentials
secrets
service-account files
```

Only approved `.gitkeep` placeholders may be committed inside ignored data folders.

Before every commit involving data-folder changes, run:

```powershell
git status --short --untracked-files=all
```

Never use `git add -f` on an ignored data or credential file unless an explicit security review has approved it.

The GitHub repository, when created, must be private.

---

## 25. Large-file and ChatGPT safety

Large CSV files must not be uploaded into ChatGPT unless absolutely necessary and explicitly agreed.

Use safer evidence instead:

- filenames;
- headers;
- row counts;
- terminal output;
- validation summaries;
- a few selected sample rows;
- scripts;
- schema descriptions.

This rule exists because large file-heavy chats can crash or freeze the browser or machine.

---

## 26. Change-management rule

Before changing code:

1. Read this rules file.
2. Inspect the current script.
3. State the intended behaviour change.
4. Separate structural cleanup from business-logic changes.
5. Make the smallest safe change.
6. Test the script.
7. Verify outputs.
8. Update this file if an agreed rule changed.
9. Update the Master Dictionary if terminology changed.
10. Commit the verified change.

Do not combine unrelated changes in one patch.

Do not rewrite all scripts at once.

Work one script or one tightly related group at a time.

---

## 27. Versioning rule

Do not overwrite historical scripts without preserving traceability.

When behaviour changes materially, use a new script version or a clear Git commit history.

Old scripts may be moved to an approved history location only after the replacement is tested, outputs are reconciled, the change is committed, and the new script is accepted.

The repository history is the primary record of code evolution.

---

## 28. Current implementation sequence

The agreed immediate sequence is:

1. Keep this governing rules file current.
2. **Completed:** build and validate `scripts/00_prepare_conlog_raw_sales.py` against the original April, May, and June 2026 portal downloads.
3. **Completed:** recreate the approved six-column raw-staging files under `input/conlog_sales` through Stage 00, without manual CSV preparation.
4. **Current step:** review, correct, run, and validate `scripts/01_prepare_conlog_sales.py`.
5. Generate and validate upload-ready Atomic outputs through June 2026.
6. Review, correct, run, and validate `scripts/02_upload_conlog_atomic_v2.py` or its approved replacement.
7. Review, correct, run, and validate `scripts/03_aggregate_monthly_from_atomic_outputs.py`.
8. Review, correct, run, and validate `scripts/04_upload_conlog_monthly_v3.py`.
9. Confirm RAW, RAW STAGING, Atomic, and Monthly Sales completeness and reconciliation.
10. Run and validate `scripts/05_build_meter_master_v3.py`.
11. Approve and freeze the Meter Master CSV.
12. Upload Meter Master to `ireps-test` using `scripts/07_upload_meter_master_v3.py` in `create-only` mode.
13. Run and validate the approved dynamic Sales All Meters builder.
14. Review and correct `scripts/08_upload_sales_all_meters.py` for explicit reusable environment selection.
15. Upload Sales All Meters to `ireps-test`.
16. Record all validations and upload reports.
17. Update the README and iREPS Master Dictionary where required.
18. Commit the verified rules and code changes.

The mandatory Firestore upload order remains:

```text
Atomic Sales
    -> Monthly Sales collections
    -> Meter Master
    -> Sales All Meters
```

Do not skip directly to downstream builds or final uploads.

---

## 29. Current non-goals

The following are not part of the current sprint:

- renaming `conlog_sales_xxx`;
- implementing Landis+Gyr ingestion;
- provider-neutral collection migration;
- creating `ireps-trials`;
- creating `ireps-production`;
- full cadastral reset;
- nuclear reset of DEV and TEST;
- Production data loading;
- billing-engine development.

These items require separate approved sprints.

---

## 30. Definition of done for the current TEST sales sprint

The current sales sprint is complete only when:

- Stage 00 exists and is the approved path from `input/raw-sales` to `input/conlog_sales`;
- no manual preparation of `input/conlog_sales` is required in the normal monthly process;
- April, May, and June 2026 raw portal downloads are processed through Stage 00;
- raw-staging files cover the approved source period;
- atomic outputs cover September 2025 through June 2026;
- monthly outputs cover September 2025 through June 2026;
- September 2025 through June 2026 are processed as ten separate monthly runs;
- totals reconcile;
- Meter Master is progressed month by month through all agreed available sales months;
- Sales All Meters is progressed month by month through the same months;
- every Stage 00 to Stage 08 script requires a one-LM, one-month operating invocation;
- upload scripts require explicit reusable environment selection;
- Firestore uploads follow the mandatory order: Atomic, Monthly, Meter Master, Sales All Meters;
- each month-specific Meter Master input and result fingerprint is recorded;
- approved data is uploaded to `ireps-test`;
- validations are recorded;
- the rules file, README, and Master Dictionary are current;
- Git is clean and changes are committed.

---

## 31. Decision history

### 2026-07-13 — Governing rules file required

Every substantial iREPS analysis, design, and coding sprint must have an authoritative Markdown rules file under a `rules` folder.

The rules file must be read first whenever the sprint is revisited.

### 2026-07-13 — Clean repository structure

Python scripts were moved from the project root into `scripts`.

Project-history tree files were moved into:

```text
docs/project-history
```

The cleaned `input` and `output` structures were preserved.

### 2026-07-13 — One Master Dictionary only

No local Sales Pipeline dictionary will be created.

Sales Pipeline terminology must be maintained in the single iREPS Master Dictionary.

### 2026-07-13 — Generic `sales` naming

New governance and architecture filenames must use `sales` rather than `prepaid`.

Existing source filenames containing `prepaid` remain unchanged during the current sprint.

### 2026-07-13 — Keep Conlog collection names during TEST

The existing `conlog_sales_xxx` collection names remain active during `ireps-test` stabilisation.

Provider-neutral restructuring is deferred until Trials Readiness.

### 2026-07-13 — Dynamic full-period builders — SUPERSEDED 2026-07-14

The earlier decision proposed dynamic full-period discovery for Meter Master and Sales All Meters.

This operating model was superseded on 2026-07-14 by the universal one-LM, one-month execution contract. All historical months must now be applied through separate chronological monthly runs.

### 2026-07-13 — Environment-neutral builds

Local build scripts must not select Firebase environments.

Upload scripts must require an explicit target project.

### 2026-07-13 — Meter Master v3 and Firestore contract approved

`scripts/05_build_meter_master_v3.py` is the approved Meter Master staging builder.

Its ten-column CSV output and the final `meter_master` Firestore schema are locked in Section 19.

All Meter Master documents require six-field metadata. Pipeline reruns must preserve existing `metadata.created*` values and populated `refs.asts.id` links.

### 2026-07-13 — Reusable once-off Meter Master uploader — SUPERSEDED 2026-07-14

The earlier decision treated Meter Master loading as one frozen full-period CSV and one once-off upload per project.

That operating model is superseded by the universal one-LM, one-month contract. Stage 05 and Stage 07 must now support controlled monthly creation or Sales-owned enrichment while preserving the locked Meter Master schema, `refs.asts.id`, and `metadata.created*`.

### 2026-07-13 — RAW, RAW STAGING, and Atomic layers locked

The provider-download path is formally separated into three governed data states:

```text
RAW
input/raw-sales
    -> scripts/00_prepare_conlog_raw_sales.py
RAW STAGING
input/conlog_sales
    -> scripts/01_prepare_conlog_sales.py
ATOMIC
output/atomic
```

The operator downloads the original vending-provider CSV and places it unchanged in `input/raw-sales`.

`scripts/00_prepare_conlog_raw_sales.py` must generate the approved six-column Conlog raw-staging file. Manual preparation of files under `input/conlog_sales` is no longer an approved normal operating step.

`scripts/01_prepare_conlog_sales.py` consumes raw-staging files and generates upload-ready Atomic CSVs under `output/atomic`.

### 2026-07-13 — Stage 00 monthly operating contract proven

Stage 00 was validated and successfully run against the original Conlog portal downloads for April, May, and June 2026.

The approved operating model is one LM and one month per execution using mandatory `--lm-pcode` and `--month` arguments, with optional `--preflight-only` validation before writing.

The RAW source filename is governed as `conlog_raw_sales__<lmPcode>__YYYY-MM.csv`. The CSV contents remain unchanged; only the local filename may be standardised for controlled identity.

Stage 00 preserves duplicate six-field staging rows, blocks all output when rejected rows exist, fingerprints both the RAW source and planned output with SHA-256, protects different existing outputs unless an approved `--replace-existing` run is used, and records successful write summaries under `output/logs`.

The Conlog RAW STAGING monetary fields retain their historical names but contain validated decimal rand values. Stage 01 is the single approved conversion boundary to integer cents for Atomic Sales.

### 2026-07-13 — Pipeline dependency and upload order locked

The approved data dependency is:

```text
Atomic Sales
    -> Monthly Sales
    -> Meter Master
    -> Sales All Meters
```

The mandatory Firestore upload order is:

```text
Atomic Sales
    -> Monthly Sales collections
    -> Meter Master
    -> Sales All Meters
```

Meter Master must not be uploaded before the required Atomic and Monthly Sales data is complete and validated. Sales All Meters must not be built or uploaded before Meter Master is approved.

### 2026-07-13 — Git repository created

The local Git repository was created on `main`.

Initial commit:

```text
21468f6 chore: initialise sales pipeline repository structure
```

Operational CSV data and generated outputs remain excluded from Git.

---

### 2026-07-14 — Ten-month Atomic upload verified

The ten Conlog Atomic months from September 2025 through June 2026 were uploaded to `ireps-test/conlog_sales_atomic`.

The verified total is:

```text
822,527 documents
```

Every month completed with `UPLOAD_VERIFIED`, count verification PASS, sample verification PASS, zero conflicts, and zero extra documents.

### 2026-07-14 — Canonical meter-number length rule corrected

The pipeline must not impose an 11-character meter-number rule.

Stage 01, Stage 02, Stage 03, Stage 04, Meter Master, Sales All Meters, and future readers must preserve the canonical normalized meter number without padding or fixed-length rejection.

### 2026-07-14 — Monthly build and upload safety model approved

Stage 03 must produce a successful SHA-256 build manifest.

Stage 04 v3 must consume that manifest, preflight all three monthly collection scopes, use create-only or controlled resume, use Firestore create operations only, and verify counts and deterministic samples.

Broad `merge=True` writes are prohibited for the three monthly aggregate collections.

### 2026-07-14 — Universal month-by-month execution locked

Every Sales Pipeline Python stage from 00 through 08 must operate on one LM and one month per normal execution.

Mandatory arguments are `--lm-pcode` and `--month` where applicable. Range arguments such as `--from-month` and `--to-month` are prohibited.

Historical backfills must run each month separately and chronologically. Future monthly ingestion must use the same code path without changing Python date ranges.

Stage 03 must create only the selected month’s three monthly datasets and one month-specific manifest. It must not generate combined `ALL` outputs during normal execution.

Stages 05 through 08 must be updated to comply with this contract before they are used for the current rebuild.

---

## 32. Rule amendment

This file may be amended only when an architecture, implementation, data, safety, naming, or operating decision changes.

Every amendment must include:

- the date;
- the changed rule;
- the reason;
- the effect on code or data;
- any migration action required.

A rules update and the related code change should be committed together whenever practical.
