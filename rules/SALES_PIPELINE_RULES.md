# iREPS Sales Pipeline Rules

**File:** `rules/SALES_PIPELINE_RULES.md`  
**Project:** `C:\dev\ireps-pipeline-sales`  
**Status:** Governing project rules  
**Version:** 1.7  
**Effective date:** 2026-07-15  
**Current phase:** Lesedi `ireps-test` sales baseline complete; mobile consumption verified  
**Current provider:** Conlog  
**Current LM / workbase:** Lesedi — `ZA7423`

---

## 1. Purpose

This file is the governing architecture, implementation, data, safety, validation and operating contract for the iREPS Sales Pipeline.

It preserves approved decisions across developers, future maintainers, Codex and other AI agents, pipeline operators, documentation work, environment migrations, Trials preparation and Production preparation.

The rules in this file must be read before analysing, changing, running, uploading or documenting any part of the Sales Pipeline.

---

## 2. Authority

The authority order is:

1. Locked canonical collection schemas under `C:\dev\ireps-schemas` for Firestore document identity, shape, field type and field ownership.
2. `rules/SALES_PIPELINE_RULES.md` for pipeline architecture, execution, safety and operating behaviour.
3. Approved iREPS architecture and data decisions.
4. The iREPS Master Dictionary.
5. Confirmed current code behaviour.
6. Sprint instructions.
7. Chat discussions.
8. Assumptions.

Code does not silently define or change a schema.

A pipeline rule must not invent a Firestore field that is absent from the applicable locked schema.

If code, this file and the schema repository disagree, the work must stop until the conflict is identified, reviewed and documented.

---

## 3. Mandatory read-first rule

Before working on this project:

1. Read this entire file.
2. Read the Sales Pipeline section in the iREPS Master Dictionary.
3. Inspect the current repository tree.
4. Inspect the scripts involved in the requested change.
5. Inspect the applicable schemas under `C:\dev\ireps-schemas`.
6. Confirm the target environment before any upload or destructive action.
7. Report conflicts before editing.

Every Codex or AI-agent request must begin with an instruction equivalent to:

> Read `rules/SALES_PIPELINE_RULES.md` first. Treat it as the governing implementation contract. Do not make changes that contradict it. Report conflicts before editing.

---

## 4. Official terminology and naming

There is one iREPS Master Dictionary:

```text
C:\dev\ireps-academy\15-dictionary\
iREPS_Master_Dictionary_v1.3.md
```

Use the generic word `sales` for new architecture, project and governance names.

Preferred examples:

```text
sales
sales_pipeline
sales_atomic
sales_monthly
sales_all_meters
SALES_PIPELINE_RULES.md
```

The word `prepaid` may remain where it accurately describes Conlog source data, an existing source filename or a user-facing report name.

Existing collection names remain unchanged during TEST stabilisation:

```text
conlog_sales_atomic
conlog_sales_monthly
conlog_sales_monthly_lm
conlog_sales_monthly_lm_groups
meter_master
sales-all-meters
```

Do not rename these collections during the current phase.

---

## 5. Approved repository structure

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

The repository root must remain clean.

Scripts must resolve the repository root from their own location:

```python
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
```

Operational input and output paths must be derived from `PROJECT_ROOT`.

---

## 6. Authoritative schema repository

The authoritative schema repository is:

```text
C:\dev\ireps-schemas
```

Current Sales Pipeline schema folders are:

```text
conlog-sales-atomic
conlog-sales-monthly
conlog-sales-monthly-lm
conlog-sales-monthly-lm-groups
meter-master
sales-all-meters
```

The pipeline rules and schema repository have connected but different responsibilities:

```text
SALES_PIPELINE_RULES.md
    = execution, architecture, safety and operating contract

C:\dev\ireps-schemas
    = Firestore identity, shape, type and field contract
```

A Firestore document-shape change requires a versioned schema update and an aligned code change.

---

## 7. Environment and provider model

Current and planned environments:

```text
ireps2            DEV
ireps-test        TEST
ireps-trials      Future Trials
ireps-production  Future Production
```

Current target:

```text
ireps-test
```

Current provider:

```text
conlog
```

Current Conlog vending-provider document:

```text
vending_providers/vpr_7f4d3c91a2b84e6f
```

Local build scripts must remain environment-neutral.

Upload scripts must require an explicit Firebase project, matching project confirmation and a service account whose `project_id` matches the requested project.

Production must never be selected by default.

The provider-neutral redesign is deferred to a separate controlled Trials-readiness sprint.

---

## 8. Current source-data scope

Current LM / workbase:

```text
Lesedi
ZA7423
```

Current approved period:

```text
2025-09 through 2026-06
10 continuous months
```

Source layers:

```text
RAW provider download
input/raw-sales

RAW STAGING
input/conlog_sales
```

Current reference files:

```text
input/reference/Customer_Details.csv
input/reference/90_Days_No_Purchase_Report.csv
```

RAW source files are evidence and must remain unchanged.

RAW STAGING is generated provider-specific input. It is not Atomic Sales and must not be uploaded to Firestore.

All municipal source CSVs and generated operational CSVs must remain excluded from Git.

---

## 9. Completed `ireps-test` baseline

The Lesedi Conlog baseline was completed and verified for September 2025 through June 2026.

```text
conlog_sales_atomic             822,527 documents
conlog_sales_monthly            157,940 documents
conlog_sales_monthly_lm              10 documents
conlog_sales_monthly_lm_groups       50 documents
meter_master                     35,295 documents
sales-all-meters                 35,295 documents
```

All required month sequences are complete.

Atomic, Monthly Meter, Monthly LM and Monthly LM Group totals reconcile for the same source period.

The mandatory dependency and upload order was followed:

```text
Atomic Sales
    -> Monthly Sales collections
    -> Meter Master
    -> Sales All Meters
```

---

## 10. Pipeline architecture and operating grain

The approved build dependency is:

```text
Original Conlog portal CSV
    -> Stage 00 RAW preparation
    -> Conlog RAW STAGING
    -> Stage 01 Atomic preparation
    -> Atomic Sales
    -> Stage 03 Monthly aggregation
    -> Monthly Sales
    -> Stage 05 Meter Master build
    -> Meter Master
    -> Stage 06 Sales All Meters build
    -> Sales All Meters
```

The stages do not all use the same operating grain.

### 10.1 Stages 00 to 04

Stages 00 to 04 operate at:

```text
one LM + one month per execution
```

Applicable scripts must require explicit `--lm-pcode` and `--month` arguments.

### 10.2 Stages 05 and 06

Stages 05 and 06 are controlled full-range downstream builders.

They operate at:

```text
one LM + one explicit continuous from-month/to-month range
```

They must:

- require an explicit LM;
- require an explicit first and last month;
- discover only valid monthly files inside the requested range;
- reject duplicate months;
- stop on a missing internal month;
- sort months chronologically;
- print the discovered range;
- include the range in the output filename;
- remain environment-neutral;
- never connect to Firebase.

The completed range is:

```text
2025-09 through 2026-06
```

### 10.3 Stages 07 and 08

Stages 07 and 08 upload one approved frozen full-period CSV to one explicit Firebase project.

They operate at:

```text
one Firebase project + one approved full-period CSV per execution
```

They do not silently rebuild data and do not select a Firebase environment by default.

### 10.4 Prohibited simplification

Do not reinstate the incorrect rule that every Stage 00 to Stage 08 script must run one month at a time.

The proven approved model is:

```text
Stages 00-04: monthly
Stages 05-06: explicit continuous range build
Stages 07-08: frozen full-period upload
```

---

## 11. Raw and Atomic Sales rules

### 11.1 RAW provider download

Approved filename contract:

```text
conlog_raw_sales__<lmPcode>__YYYY-MM.csv
```

A local rename may standardise the filename, but it must not change CSV content, encoding, delimiter, quoting or line endings.

### 11.2 RAW STAGING

Approved Conlog RAW STAGING columns:

```text
lmPcode
txAt
meterNo
amountTotalC
costC
vatC
```

In RAW STAGING only, the historical `*C` column names contain validated decimal-rand source values.

Stage 01 is the single approved conversion boundary from decimal rand to integer cents.

### 11.3 Atomic Sales

Atomic Sales is the transaction-level source of truth.

Approved target:

```text
conlog_sales_atomic/{atomicId}
```

Atomic data preserves provider, LM, meter, transaction time, source file, source row and ingestion lineage.

From Atomic onward, every `*C` value is integer cents.

Normal upload mode is `create-only`.

`resume` is restricted to verified recovery from a partial upload of the same approved monthly CSV.

Create operations are required. Merge, update, overwrite and silent conflict skipping are prohibited.

---

## 12. Monthly Sales rules

Monthly outputs are derived only from approved Atomic outputs.

The three monthly grains are:

```text
conlog_sales_monthly
    one meter + one LM + one month

conlog_sales_monthly_lm
    one LM + one month

conlog_sales_monthly_lm_groups
    one LM + one month + one sales group
```

Document identities:

```text
conlog_sales_monthly/{lmPcode}__{normalizedMeterNo}__{ym}
conlog_sales_monthly_lm/{lmPcode}__{ym}
conlog_sales_monthly_lm_groups/{lmPcode}__{ym}__{salesGroupId}
```

Sales groups:

```text
GR1  below R100.00
GR2  R100.00 to R299.99
GR3  R300.00 to R499.99
GR4  R500.00 to R999.99
GR5  R1,000.00 and above
```

For each LM/month:

```text
Atomic totals
    = sum of Monthly Meter totals
    = Monthly LM total
    = sum of Monthly LM Group totals
```

Reconciliation includes purchases, meters, amount, cost, VAT, first purchase time and last purchase time.

Stage 04 v3 consumes a successful Stage 03 manifest, validates all three datasets before writing, uses create operations and verifies final counts and deterministic samples.

---

## 13. Meter-number normalisation

The same meter-number normalisation rule applies across every sales and meter layer.

Rules:

- cast to string;
- trim leading and trailing whitespace;
- remove embedded whitespace;
- uppercase letters;
- preserve leading zeroes;
- preserve meaningful letters;
- reject blank or invalid identities;
- do not impose a fixed meter-number length.

A meter number must never be treated as a numeric value where leading zeroes can be lost.

---

## 14. Monetary-value rule

From Atomic onward:

```text
fields ending in C = integer cents
```

Required equation:

```text
amountTotalC = costC + vatC
```

Aggregations must use integer arithmetic wherever possible.

No downstream stage may reinterpret Atomic cent values as rand values or convert them a second time.

---

## 15. Meter Master rule

`meter_master` is the thin identity and cross-reference bridge between the sales-side meter universe and iREPS operational meter identity.

It is not a transaction-history, premise, ERF, TRN, status, visibility or service-provider collection.

### 15.1 Approved scripts

```text
scripts/05_build_meter_master_v3.py
scripts/07_upload_meter_master_v3.py
```

### 15.2 Builder inputs

```text
output/monthly/monthly__<scope>__YYYY-MM__from_atomic.csv
input/reference/Customer_Details.csv
input/reference/90_Days_No_Purchase_Report.csv
```

The builder uses the complete explicit continuous monthly range.

It does not read RAW or RAW STAGING sales files directly.

### 15.3 Approved staging CSV

The staging CSV has exactly ten columns in this order:

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

Identity rules:

```text
masterId = meterNoNormalized
salesId = meterNoNormalized
salesProvider = conlog
```

### 15.4 Governed duplicate-resolution rules

Customer Details duplicate resolution may use only:

```text
MeterNumber
CustomerNo
AccountNo
AccountStatus
LastPurchaseDate
```

Customer name, ERF, ERF description, physical address and postal address must not decide customer/account identity.

Approved Customer Details rules:

1. `CustomerNo = AccountNo = MeterNumber` is a weak placeholder.
2. Prefer the normal dominant pattern `CustomerNo = AccountNo` and `CustomerNo != MeterNumber` over the placeholder.
3. An Active row may resolve the approved compatible Active versus Block Purchases pattern.
4. Competing approved real identities may use the latest valid `LastPurchaseDate`.
5. Tied, missing or still-conflicting identities stop the build.

Approved 90 Days No Purchase rules:

1. `CustomerNo1 = MeterIdentifier` is a weak placeholder.
2. Prefer a non-empty `CustomerNo1` that differs from `MeterIdentifier`.
3. Competing real customer numbers may use the latest valid `LastPurchaseDate`.
4. Tied, missing or unresolved identities stop the build.

The completed build reported:

```text
Customer placeholder duplicates resolved:     124
Customer Active-status duplicates resolved:    10
Customer latest-purchase duplicates resolved:   1
NPR placeholder duplicates resolved:            13
NPR latest-purchase duplicates resolved:          0
```

### 15.5 Firestore identity

```text
meter_master/{normalizedMeterNo}
```

The Firestore document ID equals `meterNo.normalized`.

Random IDs, suffix IDs and unofficial composite IDs are prohibited.

### 15.6 Locked Firestore shape

```json
{
  "lmPcode": "ZA7423",
  "meterNo": {
    "raw": "04085348348",
    "normalized": "04085348348"
  },
  "meterType": "electricity",
  "customerNo": "",
  "accountNo": "",
  "refs": {
    "asts": {
      "id": ""
    },
    "sales": {
      "id": "04085348348",
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

Canonical root fields are only:

```text
lmPcode
meterNo
meterType
customerNo
accountNo
refs
metadata
```

Prohibited fields include:

```text
id
parents
refs.premise
refs.trns
status
serviceProvider
visibility
createdBySource
updatedBySource
```

### 15.7 Metadata constitution

`metadata` contains exactly six fields:

```text
createdAt
createdByUid
createdByUser
updatedAt
updatedByUid
updatedByUser
```

Timestamps are native Firestore Timestamp values.

Do not place source-provenance fields at the root or inside metadata in the current locked Meter Master schema.

### 15.8 Upload safety

Normal mode is:

```text
create-only
```

The initial target collection must be empty.

Controlled `resume` may create missing documents and skip exact matches from the same frozen CSV, but it must stop on conflicts or unexpected documents.

Broad `merge=True` writes are prohibited.

A blank staging `astId` must never erase an existing populated `refs.asts.id`.

### 15.9 Completed Meter Master baseline

```text
Monthly-backed meters:       19,904
Customer-only seeded meters:  3,987
NPR-only seeded meters:      11,404
Total rows/documents:        35,295
Validation:                  PASS
```

Approved CSV:

```text
output/meter_master/meter_master__ZA7423__FULL__2025-09_to_2026-06.csv
```

Upload report:

```text
output/meter_master/upload-reports/
meter_master_upload__ireps-test__20260714T200849Z.json
```

The governed duplicate fix was committed as:

```text
82060dc fix: resolve meter master duplicate customer records
```

---

## 16. Sales All Meters rule

`sales-all-meters` is the governed supporting sales-awareness projection for every approved Meter Master identity, including meters with no sales in the selected period.

It is not the Atomic source of truth.

### 16.1 Approved scripts

```text
scripts/06_build_sales_all_meters.py
scripts/08_upload_sales_all_meters.py
```

### 16.2 Build inputs

```text
approved Meter Master CSV
+
valid monthly meter-level sales CSVs
```

The build starts from Meter Master so that zero-sales meters remain represented.

### 16.3 Approved staging CSV

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

Dynamic monthly columns:

```text
amount_YYYY_MM_C
```

Completed range columns:

```text
amount_2025_09_C
amount_2025_10_C
amount_2025_11_C
amount_2025_12_C
amount_2026_01_C
amount_2026_02_C
amount_2026_03_C
amount_2026_04_C
amount_2026_05_C
amount_2026_06_C
```

`totalAmountC` must equal the sum of all included monthly amount columns.

`lastPurchaseAtISO` is the latest valid included purchase timestamp.

`daysSinceLastPurchase` is calculated against the explicit build `--as-of-date`.

Zero-sales meters remain present with zero monthly totals and blank CSV purchase-age fields.

### 16.4 Visibility projection

```text
Meter Master astId populated -> VISIBLE
Meter Master astId blank     -> INVISIBLE
```

Visibility is a supporting Sales All Meters projection. It is not canonical Meter Master truth and must not be written back into `meter_master`.

The completed staging CSV contained blank `astId` values, therefore all 35,295 Sales All Meters rows were `INVISIBLE`.

This does not prove that no related AST exists elsewhere in iREPS. It records only the linkage available in the approved staging Meter Master CSV.

### 16.5 Firestore identity and shape

```text
sales-all-meters/{masterId}
```

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

`monthlyTotalsC` keys are generated dynamically from the approved CSV month columns.

The current TEST schema does not add a metadata object to Sales All Meters.

### 16.6 Upload safety

Required arguments:

```text
--project-id
--confirm-project
--service-account
--input
--mode
```

Supported modes:

```text
create-only
resume
```

`create-only` is normal operation and requires an empty target collection.

`resume` is restricted to recovery from a verified partial upload of the same frozen CSV:

```text
missing document             -> create
existing matching document   -> skip
existing conflicting document -> stop
unexpected extra document    -> stop
```

Create operations are required. Broad `merge=True` writes are prohibited.

Every run must print a complete preflight, calculate the CSV SHA-256, report progress, verify the final count and write a JSON report.

### 16.7 Completed Sales All Meters baseline

```text
Rows/documents:       35,295
Meters with sales:    19,904
Meters without sales: 15,391
Visible meters:            0
Invisible meters:     35,295
Total amount cents:   9,728,029,408
Validation:           PASS
```

Approved CSV:

```text
output/sales_all_meters/
sales_all_meters__ZA7423__FULL__2025-09_to_2026-06.csv
```

CSV SHA-256:

```text
139e1775ed4404696077ccf5df4355288eabcb0357fbb7ddeebe578d69179087
```

Upload report:

```text
output/sales_all_meters/upload-reports/
sales_all_meters_upload__ireps-test__20260714T212554Z.json
```

The governed builder and uploader were committed as:

```text
66f06fb fix: govern sales all meters build and upload
```

---

## 17. Validation and reconciliation

No build or upload is complete until validation is complete.

Minimum validation includes, where applicable:

- input file count;
- input row count;
- output row count;
- rejected or skipped rows;
- duplicate identities;
- unique meter count;
- total integer cents;
- earliest and latest purchase dates;
- complete continuous month sequence;
- Atomic-to-Monthly reconciliation;
- Monthly Meter-to-LM reconciliation;
- Monthly Group-to-LM reconciliation;
- Meter Master row and identity validation;
- Sales All Meters row and total reconciliation;
- target collection count;
- deterministic sample verification;
- CSV SHA-256;
- JSON run report.

A script must fail loudly on missing, incomplete, ambiguous or conflicting data.

Warnings must not be presented as successful completion.

---

## 18. Upload safety

Every uploader must:

- require an explicit Firebase project ID;
- require matching project confirmation;
- verify the credential project before Firebase starts;
- display the target project and collections;
- display input filenames and row counts;
- display the expected operation;
- validate all data before writing;
- use controlled batch sizes;
- report progress and final writes;
- stop on conflicts;
- avoid Production defaults;
- avoid silent environment switching;
- create a run report;
- verify final counts and samples.

Destructive delete-and-reload operations require a separate approved plan, a backup and explicit confirmation.

The old 11-document `meter_master` TEST collection was backed up before its controlled reset and full approved reload.

---

## 19. Git and data protection

The repository branch is:

```text
main
```

Do not commit:

- municipal sales CSV files;
- Customer Details or 90 Days No Purchase source data;
- generated output CSVs;
- upload reports containing operational data;
- service-account files;
- Firebase credentials;
- `.env` secrets;
- local credentials;
- large operational datasets.

Before committing, run:

```powershell
git status --short --untracked-files=all
```

Do not use `git add -f` to force operational data or credentials into Git.

Work in small verified commits. Do not combine unrelated changes.

---

## 20. Mobile consumption verification

On 2026-07-15, the completed June 2026 sales data was manually synced and displayed successfully in `ireps-mobile` on the **Prepaid Revenue Report** screen.

The observed screen confirmed:

- June 2026 Monthly mode loaded;
- Lesedi workbase context loaded;
- 16,089 total meters displayed for June 2026;
- Sales Group G4 filtering displayed records;
- meter-level purchase counts and monthly amounts displayed;
- cached mobile data was available after sync.

This is recorded as:

```text
MANUAL MOBILE CONSUMPTION VERIFICATION: PASS
```

This manual verification complements, but does not replace, automated pipeline, Firestore count and reconciliation validation.

The milestone proves that the completed data flow reaches the intended user-facing report:

```text
Conlog source
    -> Atomic
    -> Monthly
    -> Meter Master
    -> Sales All Meters
    -> iREPS Mobile Prepaid Revenue Report
```

---

## 21. Current implementation status

Completed:

1. RAW-to-RAW-STAGING preparation established.
2. Atomic Sales prepared through June 2026.
3. 822,527 Atomic documents uploaded and verified.
4. Monthly Meter, LM and LM Group outputs rebuilt through June 2026.
5. All three Monthly collections uploaded and verified.
6. Meter Master rebuilt with approved duplicate-resolution rules.
7. 35,295 Meter Master documents uploaded and verified.
8. Sales All Meters rebuilt with ten dynamic months.
9. 35,295 Sales All Meters documents uploaded and verified.
10. Governed Stage 06 and Stage 08 changes committed and pushed.
11. June 2026 data synced and displayed successfully in iREPS Mobile.

Immediate governance close-out:

1. Keep this rules file current.
2. Update and commit the six authoritative schemas under `C:\dev\ireps-schemas`.
3. Update the project README where required.
4. Update the iREPS Master Dictionary where terminology requires it.
5. Keep operational data and credentials outside Git.

---

## 22. Definition of done for the Lesedi TEST sales baseline

The baseline is complete because:

- RAW STAGING covers September 2025 through June 2026;
- Atomic Sales covers the same ten months;
- Monthly Meter, LM and Group data covers the same ten months;
- all month sequences are complete;
- financial totals reconcile;
- Meter Master was rebuilt from the full approved range and references;
- Sales All Meters was rebuilt from the same range;
- builders no longer contain the old fixed February 2026 end range;
- upload scripts require explicit environment selection;
- upload order was followed;
- final collection counts were verified;
- run reports were recorded;
- script changes were committed and pushed;
- June 2026 data was successfully consumed by iREPS Mobile.

Milestone:

```text
Lesedi Conlog Sales Pipeline
ZA7423
2025-09 through 2026-06
ireps-test
DATA, FIRESTORE AND MOBILE CONSUMPTION BASELINE COMPLETE
```

---

## 23. Decision history

### 2026-07-13 — Governing rules file required

Every substantial iREPS coding and data sprint must have an authoritative Markdown rules file.

### 2026-07-13 — Clean repository structure

Python scripts were moved into `scripts`, and data folders remained excluded from Git.

### 2026-07-13 — One Master Dictionary

No separate Sales Pipeline dictionary will be created.

### 2026-07-13 — Generic sales naming

New architecture and governance names use `sales`; existing accurate prepaid source and report names may remain.

### 2026-07-13 — Environment-neutral builds

Build scripts do not select Firebase environments. Upload scripts require an explicit target project.

### 2026-07-13 — Pipeline dependency and upload order locked

```text
Atomic -> Monthly -> Meter Master -> Sales All Meters
```

### 2026-07-14 — Ten-month Atomic upload verified

822,527 Atomic documents were loaded into `ireps-test` for September 2025 through June 2026.

### 2026-07-14 — Monthly build and upload completed

All ten Monthly Meter datasets, ten Monthly LM documents and fifty Monthly LM Group documents were built, reconciled, uploaded and verified.

### 2026-07-14 — Meter-number length rule corrected

There is no universal 11-character meter-number rule.

### 2026-07-14 — Meter Master schema locked

The deterministic normalized meter number is the document ID. The canonical document contains the exact approved root shape and six-field metadata object. `createdBySource` and `updatedBySource` are prohibited.

### 2026-07-14 — Meter Master duplicate rules proven

The approved placeholder, account-status and latest-purchase rules resolved the known duplicate patterns. Customer name, ERF and addresses were excluded from identity resolution.

### 2026-07-14 — Meter Master baseline completed

35,295 Meter Master documents were created and verified in `ireps-test`.

### 2026-07-14 — Sales All Meters governed

The builder was changed to use an explicit continuous range and dynamic monthly columns. The uploader was changed to explicit project selection, create-only/controlled-resume safety and conflict blocking.

### 2026-07-14 — Sales All Meters baseline completed

35,295 Sales All Meters documents were created and verified in `ireps-test`.

### 2026-07-15 — Operating-grain correction

The incorrect universal one-month rule for Stages 05 to 08 was removed.

Approved operating grain:

```text
Stages 00-04: one LM and one month
Stages 05-06: one LM and one explicit continuous range
Stages 07-08: one project and one frozen full-period CSV
```

### 2026-07-15 — Mobile consumption verified

June 2026 sales synced successfully and displayed on the iREPS Mobile Prepaid Revenue Report, including the 16,089-meter monthly result and meter-level group records.

---

## 24. Rule amendment

This file may be amended only when an architecture, implementation, data, safety, naming, schema or operating decision changes.

Every amendment must record:

- the date;
- the changed rule;
- the reason;
- the effect on code or data;
- any required migration.

A rules update and its related code or schema change should be committed together whenever practical.
