# iREPS Sales Pipeline Rules

## 2026-09-05 final-state monthly refresh cutover

This controlling amendment supersedes conflicting Stage 1 transition wording in section 0D and historical rules below. The related schema is `sales_all_meters` v1.4.1. It records local implementation requirements, not deployed or migrated status.

`monthlyCategories` is the only category authority. Read the explicitly selected/report month; absent exact-month data renders unavailable/NAv. No scalar fallback, per-meter latest category substitution, Demo Sales restoration or internal classifier is permitted. Default the view to the latest successfully governed and published scope month.

Existing root `leakageCategory`, `riskTier` and `riskScore` are frozen, legacy and non-authoritative. Preserve them without update or deletion. Preserve `salesStatus` behaviour and operational ownership unchanged.

Global preflight classifies the whole intended run, checks complete accounting and rejects any history/identity conflict before writes. Normal refresh appends only approved exact month children; it cannot shrink or rewrite history. Preserve valid `created*`; material writes alone advance `updated*`. Missing creation metadata requires authoritative per-document first-capture evidence. The recorded DEV SPU UID/user convention is `fXBACUfMzybcqC0AbeNeyYyTeRu1` / `Fikile Kentane`, as evidenced by `ireps-web/functions/scripts/ireps2-users-20260621-2233.json`; do not use that user record's timestamp as Sales creation time or substitute the migration operator.

Supplier population is complete membership evidence, not classification or purchasing membership. Store immutable SHA-256-addressed snapshots at `governed-sales/{lmPcode}/{provider}/snapshots/{sha256}.json` in the configured Storage bucket, following schema v1.4.1's artifact contract. Serve through an authenticated scope-authorized backend. Direct client access is denied. Do not add a `monthlyPopulation` map or population business collection.

The scope's `publication.json` may advance only after sequential June, July and August data execution and exact verification. A local plan, partial commit or passing preflight is insufficient. Use immutable snapshot hashes, publication generation preconditions and recovery evidence. Preserve exact partial results because Firestore write waves are not collectively atomic. No remote publication or database writes are authorized by this local rules amendment.

Stage 14 remains separate: enrichment requires authoritative one-to-one linkage and preserves ambiguity as exceptions. Address parsing and candidate lists alone cannot authorize cadastral relationships. Preserve existing values and metadata; conflicting populated evidence requires separate correction. No canonical `sgCode`/`erfNo` projection is established in the inspected Sales schema: retain `sg.prclKey` as source evidence and block projection until its destination semantics are settled.

Reason: user approved final-state cutover and one coordinated local completion pass. Effect: align writers, readers and release evidence without scalar fallback. Migration action: prepare one consolidated release approval; execute no remote changes before that approval.

**File:** `rules/SALES_PIPELINE_RULES.md`
**Project:** `C:\dev\ireps-pipeline-sales`
**Status:** Governing project rules
**Version:** 1.12.1
**Effective date:** 2026-09-05
**Current phase:** Sales Monthly Refresh + Population — final local cutover contract; not released
**Canonical Atomic provider:** Conlog; approved monthly-source providers remain bound by frozen source contracts
**Current Sales Enrich regression LM / workbase:** Endumeni — `ZA5241`

---

## 0D. Version 1.12.0 — Sales monthly categories and standard metadata amendment

This section is the controlling amendment for the Sales Monthly Refresh + Population workstream. Where an older Sales All rule later in this historical file conflicts specifically with `monthlyCategories`, standard Sales `metadata`, legacy root category authority, or the exact category-write contract below, this section and `sales_all_meters` schema v1.4.0 govern. Unrelated historical rules remain unchanged.

### 0D.1 Scope and non-regression boundary

This amendment adds governance for:

```text
monthlyCategories
metadata
```

It does **not** redesign the already-working `salesStatus` contract. Existing `salesStatus` ownership, values and monotonic lifecycle behaviour remain unchanged and must be preserved by every new Sales Pipeline path.

It also does not create a Sales population field, a Firestore population collection, an iREPS category classifier, or a Targeted Batch redesign.

### 0D.2 Governing principle — persist truth, calculate convenience

Do not persist properties for facts that can be deterministically calculated from canonical data already stored.

Persist source/authoritative truth and necessary provenance. Calculate derived convenience/read-model values on demand.

The current sprint therefore does not add Sales roots such as:

```text
monthlyPopulation
earliestPurchaseMonth
firstObservedSalesMonth
lastPurchaseMonth
rolling3MonthSales
rolling5MonthSales
rolling12MonthSales
meterJoinKey
```

### 0D.3 Category authority and monthly source mapping

Mpilo is the authoritative classifier for the current sprint. iREPS does not calculate CAT1–CAT8 and must not implement an internal classifier.

The governed mapping is:

```text
2026-06 -> June workbook   -> Leakage_Category
2026-07 -> July workbook   -> July_2026_Category
2026-08 -> August workbook -> August_2026_Category
future  -> month workbook  -> <MonthName>_<YYYY>_Category
```

For July and August, `Leakage_Category` is the retained June baseline/reference and is not the current-month category.

`Risk_Tier` and `Risk_Score` are scoped to the terminal month of the corresponding classification workbook unless a later supplier contract explicitly introduces month-specific variants.

A classification row does not create Sales population identity. Category evidence may be attached only to an eligible governed Sales document. Unmatched classification-source identities are external exceptions and must not create Sales documents.

### 0D.4 Canonical `monthlyCategories` contract

The canonical Sales All shape is month-keyed:

```json
"monthlyCategories": {
  "2026-06": {
    "leakageCategory": "...",
    "riskTier": "...",
    "riskScore": 6
  },
  "2026-07": {
    "leakageCategory": "...",
    "riskTier": "...",
    "riskScore": 4
  }
}
```

Each month child contains exactly:

```text
leakageCategory
riskTier
riskScore
```

`riskScore` is a non-negative Firestore integer. Boolean, float, string and null representations are invalid. No permanent maximum such as 16 is hard-coded unless the authoritative supplier contract explicitly fixes it.

Missing authoritative category month means the key is absent. Do not persist `NAv - Not Available` or another manufactured placeholder. UI readers may display an unavailable state for an absent month.

CAT7 remains supported even when a source month contains zero CAT7 rows.

### 0D.5 Historical category safety

Normal monthly refresh is append-only at the exact month-child path.

For the governed month `M`:

```text
missing monthlyCategories.M       -> eligible CREATE of that child
identical monthlyCategories.M     -> UNCHANGED / no category write
different monthlyCategories.M     -> CONFLICT
historical month missing/drifted  -> FAIL CLOSED according to governed preflight
```

Normal refresh must never replace the whole `monthlyCategories` map.

A later month must preserve all earlier month children value-for-value according to the canonical Firestore comparison contract.

Historical correction is a separate governed correction operation and is not normal monthly refresh.

### 0D.6 Standard six-field Sales metadata

The canonical Sales metadata map contains exactly:

```text
createdAt
createdByUid
createdByUser
updatedAt
updatedByUid
updatedByUser
```

`createdAt` and `updatedAt` are Firestore Timestamps.

`createdAt`, `createdByUid` and `createdByUser` are the immutable creation triple. They identify when and by whom the Sales identity first entered iREPS; `createdAt` is not the commercial Sales month and must not be replaced with the current migration timestamp.

For the governed migration of the original Endumeni 10,216 Sales documents, the creation actor is the approved SPU user identity and the creation time must come from authoritative original iREPS Sales creation/first-capture evidence. The exact UID/user values are resolved at migration execution and must not be invented in code or documentation.

`updatedAt`, `updatedByUid` and `updatedByUser` are changed together only when an approved writer performs a material Sales document mutation. Idempotent/no-op processing must not advance the update triple.

The names `createdByLabel` and `updatedByLabel` are prohibited.

### 0D.7 Writer ownership after metadata introduction

The existing business-path ownership rules remain in force.

An approved writer may mutate only its already-owned business path(s), plus the standard metadata update triple required by a material successful Sales document mutation.

The metadata introduction therefore supersedes older Sales All statements that prohibit all metadata or state that an operational writer may touch literally no path other than its business path. The business ownership itself does not expand.

Examples:

- a visibility bridge still owns only its approved `master` path for business data, but a successful material visibility mutation also updates the metadata update triple;
- a Targeted lifecycle writer still owns only its approved Targeted / `salesStatus` business path(s), plus the metadata update triple when it materially changes the Sales document;
- Stage 08 category refresh owns the exact current-month `monthlyCategories.<YYYY-MM>` child plus the metadata update triple;
- no writer may rewrite the immutable metadata creation triple after it is established.

Stage 2 must make legitimate backend writers compatible with this rule before the first migration write.

### 0D.8 Legacy root category fields — Stage 1 non-change boundary

The existing root fields:

```text
leakageCategory
riskTier
riskScore
```

remain under their current compatibility behaviour during Stage 1. This amendment does not delete, rewrite, reclassify or formally freeze them.

Their formal `LEGACY / FROZEN / NON-AUTHORITATIVE` transition belongs to the separately approved **Stage 13 — Legacy Category Freeze**, after the preceding compatibility and consumer stages are complete.

Stage 1 therefore authorizes no behavioural change to these existing roots.

### 0D.9 `salesStatus` preservation

The approved `salesStatus` contract remains unchanged.

Stage 06 must not derive or reset it. Stage 08 must preserve an existing valid value. Operational lifecycle writers continue to use the already-approved ownership and monotonic transition rules.

No part of the Sales Monthly Refresh + Population workstream may reopen or behaviorally redesign `salesStatus` unless a separate defect is proven and explicitly approved.

### 0D.10 Stage 1 acceptance boundary

Stage 1 is complete when:

1. `sales_all_meters` schema v1.4.0 is locked with `monthlyCategories` and the six-field metadata contract;
2. this rules file is versioned to 1.12.0 with the same contract;
3. the existing `salesStatus` amendment is preserved without behavioural change;
4. no implementation code, Firestore data or runtime behaviour has yet been changed by Stage 1.

The next stage is backend compatibility. No Firestore migration is authorized by this amendment alone.

---

## 0A. Version 1.9.0 — Sales Enrich v1 superseding amendment

This section is the controlling amendment for Sales Enrich v1. Where an older Stage 06, Stage 08, provider, root-shape, or refresh statement later in this historical rules file conflicts with this section and the locked `sales_all_meters` schema v1.2.0, this section and the locked schema govern. Unrelated historical rules remain unchanged.

### 0A.1 Locked objective

Sales Enrich v1 adds a deterministic canonical physical-address projection for all approved Sales All rows. The enrichment population is not limited to no-GPS meters.

For the frozen ZA5241 release regression:

```text
Input / output rows       10,216 / 10,216
Enriched                  10,117
Unresolved                    99
No-GPS total               2,633
No-GPS enriched            2,567
No-GPS unresolved             66
Rows lost / duplicated         0 / 0
```

These counts are a regression oracle for the approved ZA5241 dataset, not universal constants for future LMs.

### 0A.2 One shared record-local parser

One shared deterministic helper must serve both `atomic` and `monthly_source` Stage 06 origins. A separate monthly-only parser is prohibited.

Runtime parsing is record-local. It may use `addressLine1` and `addressLine2` from the same frozen Sales source record, but must not infer an address fact from neighbouring Sales records.

Enrichment success requires both `strNo` and `strName`. `strType` does not determine success. When the same source record explicitly states a supported type, normalize it. Otherwise use `-`.

Approved aliases are:

```text
ST / STREET / STR -> Street
ROAD / RD         -> Road
DRIVE / DR        -> Drive
PLACE             -> Place
LANE              -> Lane
AVENUE / AVE      -> Avenue
CRESCENT/CRES/CR  -> Crescent
```

The approved alias rule supersedes the older assessment-manifest type values for explicit `STR` / `CR` records and same-record separated type evidence. The later-approved MGADI/MNGADI fail-closed amendment changes the locked full-population classification to 10,117 enriched / 99 unresolved while leaving the no-GPS counts unchanged.

No spelling correction is allowed. Display normalization may change casing while preserving the source spelling, punctuation, digits, and meaning.

Compound MGADI/MNGADI numeric forms such as `MGADI 577-1` and `562/1 MNGADI` are not proven physical street numbers from same-record evidence. They must fail closed as `MULTIPLE_RANGE_OR_CONFLICTING_ADDRESS_CANDIDATES`; no special-case parser rule may promote them merely because similar neighbouring records exist. This amendment affects 13 GPS-enabled ZA5241 records and does not change the locked no-GPS counts.

### 0A.3 Stage 06 staging contract

When Sales Enrich is enabled, Stage 06 must add exactly these flat staging columns together:

```text
strNo
strName
strType
```

Resolved:

```text
strNo   = nonblank string
strName = nonblank string
strType = supported type or "-"
```

Unresolved:

```text
strNo   = ""
strName = ""
strType = "-"
```

A partial canonical address is invalid.

The flat column names are a CSV staging contract only. They must never become separate Firestore root fields.

Stage 06 must compute raw-address preservation from a governed before/after projection, fail closed if the computed mutation count is non-zero, produce a machine-readable address-enrichment report, and bind its filename/SHA-256, computed mutation count, counts, and zero fabricated spatial relationships into the frozen Stage 06 manifest/fingerprint.

### 0A.4 Atomic safety boundary

Atomic remains the canonical/main Sales origin.

An Atomic Stage 06 build may join an approved frozen commercial source solely to obtain address evidence. That source must be recorded as:

```text
role = ADDRESS_EVIDENCE_ONLY
salesTruthAuthority = ATOMIC
```

The address-evidence join must be exact one-to-one by canonical Meter Master identity and fail closed on missing, extra, or duplicate identities. It must not change Atomic-derived identity, provider, monthly amounts, totals, purchase timestamps, or recency.

### 0A.5 Monthly-source compatibility

The existing rich `monthly_source` Stage 06 path must call the same shared address helper after canonical row construction and before final validation/write. It must preserve every raw commercial address field unchanged.

No Atomic transaction fact may be fabricated when the monthly source does not provide transaction-level truth.

### 0A.6 Firestore projection — governed root `adr`

Stage 08 is the sole Sales Pipeline projection boundary from flat staging fields into Firestore.

The canonical Firestore shape is exactly:

```json
"adr": {
  "strNo": "42",
  "strName": "Mckenzie",
  "strType": "Street"
}
```

For unresolved rows:

```json
"adr": {
  "strNo": "",
  "strName": "",
  "strType": "-"
}
```

Root `strNo`, root `strName`, and root `strType` are prohibited. `adr` contains exactly the three approved nested keys in v1.

Every document produced under the new enriched contract must contain `adr`.

### 0A.7 Strict root governance remains

Do not remove or relax authoritative root governance merely to accommodate `adr`. The locked schema explicitly governs `adr` as one approved root map. Unknown roots are not canonical simply because an operational reader tolerates them.

The intended separation is:

```text
Operational readers/writers -> additive-field tolerant, targeted, preservation-oriented
Pipeline/schema              -> strict, explicitly governed, deterministic
```

### 0A.8 Stage 08 create, resume, initial-load, and refresh

All Stage 08 paths that consume an enriched Stage 06 contract must validate the three flat staging fields and project them into one exact `adr` map.

Legacy un-enriched Atomic frozen contracts may remain readable for historical recovery, but they do not satisfy the new enriched v1.2.0 schema until separately upgraded.

For an enriched resume contract, existing `adr` must compare exactly while valid operational `master.visibility` remains preserved.

The current rich controlled refresh path must treat `adr` as one pipeline-owned root. A missing canonical `adr` may be added; a valid changed `adr` may be updated as one map. If existing `adr` is malformed, has extra/missing nested keys, or violates the resolved/unresolved invariant, refresh must fail closed.

Refresh must preserve `master`, `tbRefs`, `geofenceRefs`, and every other non-pipeline-owned root exactly according to the existing preservation contract.

The broader redesign that will make downstream refresh source-origin independent is a separate approved future workstream; Sales Enrich v1 must not implement it.

### 0A.9 Non-fabrication boundary

Sales Enrich v1 must not create, infer, derive, or modify:

- GPS / latitude / longitude;
- ward;
- ERF ID, ERF number, or ERF relationship;
- premise ID or premise relationship;
- AST relationship;
- cadastral relationship;
- field-confirmed meter-to-ERF relationship.

`suburbName` is not part of Sales Enrich v1.

### 0A.10 Operational writer preservation

Current active operational Sales All writers use targeted updates for fields such as `master.visibility`, `tbRefs`, and `geofenceRefs`. They must preserve `adr`. No Sales Enrich v1 runtime change is required in Web or Mobile merely to tolerate the new governed map. Consumption of `adr` by NGTB Web/Mobile functionality is a later sprint.

### 0A.11 Implementation and test gate

The implementation must prove at minimum:

```text
Input rows                       10,216
Output rows                      10,216
Enriched                         10,117
Unresolved                           99
No-GPS total                      2,633
No-GPS enriched                   2,567
No-GPS unresolved                    66
Raw address mutations                 0
Fabricated spatial relationships      0
Root strNo/strName/strType             0
```

It must also prove Stage 08 nested `adr` construction, exact-map validation, create/resume/initial-load/refresh compatibility, operational-field preservation, and deterministic repeated parsing.

### 0A.12 Governance files

The authoritative Firestore shape is schema v1.2.0 at:

```text
C:\dev\ireps-schemas\sales-all-meters\sales_all_meters.schema.md
```

The schema and this amendment must move together with the Sales Enrich implementation. Neither document alone authorizes a Firestore write or migration.

---


## 0B. Version 1.10.0 — Governed Firestore 400-batch execution amendment

This section is the controlling performance and concurrency amendment for every Firestore writer in this repository. It changes execution mechanics only. Existing document identities, schemas, exact-path ownership, create-only rules, conflict meanings, resume contracts, source contracts, and preservation requirements remain unchanged.

### 0B.1 Fixed governed wave size

The repository-wide governed Firestore batch size is:

```text
FIRESTORE_BATCH_SIZE = 400
```

For multi-document work, the limit counts actual Firestore operations or document references, not abstract rows. A logical record that consumes multiple reads or writes counts each Firestore operation separately against the applicable limit.

Bulk reads must use `get_all` / `getAll` style APIs in waves containing no more than 400 document references. Multi-document writes must use Firestore batched-write APIs in commits containing no more than 400 write operations.

A governed multi-document writer must never fall back to one-document-at-a-time network reads, direct writes, or one transaction per record. A runtime option must not permit a governed bulk writer to silently regress to batch size 1. Existing batch-size CLI arguments retained for compatibility must accept only the governed value 400.

For 10,216 one-write records the normal maximum write-wave shape is:

```text
25 x 400
 1 x 216
---------
26 waves
```

This is derived from actual writes. Unchanged or conflicted records may reduce the number of write operations and committed waves.

### 0B.2 Optimistic concurrency and create-only safety

Batching must not weaken concurrency safety.

An update classified from a preflighted existing document must carry that document's Firestore update-time precondition. Python writers use the supported public `LastUpdateOption(snapshot.update_time)` contract; Node writers use `{ lastUpdateTime: snapshot.updateTime }` or the equivalent supported public precondition accepted by `WriteBatch.update`.

Missing documents that are approved for creation must use `batch.create()`. `set()` must not replace create-only semantics.

A concurrency-specific atomic batch failure may use exactly one bounded recovery cycle:

1. treat the failed atomic batch as zero committed writes;
2. bulk reread every participating reference in governed waves;
3. compare existence and update time with the preflight snapshot;
4. classify changed participants as record-level conflicts;
5. deterministically reclassify unchanged-state participants;
6. retry the remaining safe subset once as a governed batch;
7. if that rebuilt batch fails, stop the run.

There is no per-document transaction/write/read fallback and no unbounded outer retry loop. Authentication, permission, transport, quota, deadline, and other systemic failures fail immediately rather than entering concurrency recovery.

### 0B.3 Cross-collection transactional exception

`scripts/sales_pipeline_visibility_reconciliation_dev.py` is the approved narrow exception to WriteBatch-only execution because its Sales All visibility decision depends atomically on both:

```text
meter_master/{meterId}
sales-all-meters/{meterId}
```

A Sales All-only WriteBatch precondition cannot protect the Meter Master read state without writing to Meter Master, which is outside this operation's ownership contract.

Therefore this reconciliation must use bounded multi-document transaction waves with:

```text
maximum logical meters per transaction = 200
maximum transactional read refs        = 400
  200 Meter Master + 200 Sales All
maximum Sales All writes               = 200
```

The transaction must read the complete wave before queueing any write, update only the exact path `master.visibility`, commit once per wave, and retain bounded Firestore retry behaviour. The retrying callback must be deterministic and must not publish final counters, conflict lists, or reports until the transaction invocation succeeds.

Preflight-only and post-write verification for this operation remain non-transactional governed bulk reads of no more than 400 references per request. No individual document reads are permitted for the multi-record scope.

This exception does not reopen the general 400-WriteBatch rule for any other writer.

### 0B.4 Exact-path ownership and preservation

Batching is an I/O execution correction, not a schema or business-logic redesign. Writers must reuse their existing deterministic classification and exact-path mutation rules.

In particular, Sales All refresh must continue preserving operational roots and unknown non-pipeline-owned fields, including:

```text
master.visibility
tbRefs
geofenceRefs
```

unless the specific approved operation owns that exact path. No whole-document replacement may be introduced merely to obtain batching.

Meter Master AST, ERF, GPS, reference, identity, and metadata ownership rules remain unchanged. Create-only writers remain create-only. Existing record-level conflict continuation or blocking behaviour remains writer-specific and unchanged.

### 0B.5 Verification and observability

Multi-record preflight and verification must use governed bulk reads rather than one network read per record. Material writer reports should add, without renaming existing status/result meanings, enough evidence to prove actual batching, including where applicable:

```text
firestoreBatchSize
readWaves
writeWavesAttempted
writeWavesCommitted
writeOperationsAttempted
writeOperationsSucceeded
verificationReadWaves
preconditionConflictCount
maximumWriteOperationsInAnyBatch
perDocumentFallback = false
```

The cross-collection transaction exception should report the corresponding transaction-wave counts and maximum reads/writes per transaction.

Offline tests must prove the 400-operation partition, precondition propagation, create-only semantics, zero writes in preflight-only mode, bounded failed-wave recovery, no per-document fallback, exact-path preservation, and non-regression of already compliant Stage 02/04/07-create/08-initial-load paths before any Firestore execution.


## 0C. Version 1.11.0 — Stage 04 monthly-source recurring refresh amendment

This section is the controlling Stage 04 amendment for recurring `monthly_source` uploads. It supersedes later historical Stage 04 statements that limit every Stage 04 execution to `create-only` / `resume` and create operations only. The Atomic Stage 03 path is not changed by this amendment.

### 0C.1 Scope boundary

Stage 04 keeps the existing execution grain:

```text
one Firebase project + one LM + one month per execution
```

The existing Atomic source path remains governed by:

```text
create-only
resume
```

`refresh` is approved only when the accepted Stage 03 manifest has:

```text
stage        = 03B
sourceOrigin = monthly_source
result       = BUILD_WRITTEN
status       = PASS
```

An Atomic manifest supplied with `--mode refresh` must fail before Firebase starts.

### 0C.2 Distinct mode meanings

Stage 04 modes have separate meanings:

```text
create-only = first creation into an empty LM/month scope
refresh     = recurring reconciliation of a governed monthly_source LM/month
resume      = restricted recovery of the exact same failed create-only/resume upload contract
```

`resume` is not refresh and must not authorise changed business data. A failed refresh is reviewed and then rerun as the same governed refresh; it must not be converted into `resume`.

### 0C.3 Refresh classifications

For every expected monthly-source document, Stage 04 refresh must classify exactly one of:

```text
CREATED    expected locally and missing in Firestore
UNCHANGED  existing document exactly matches the governed expected document
UPDATED    existing compatible document has changed refresh-owned values
CONFLICT   existing document violates identity, source, shape or type governance
```

Every expected input row must be accounted for exactly once. Identical documents receive no write.

Unexpected existing documents inside the requested LM/month scope are conflicts. Refresh must not delete, hide, merge, or silently retain an unexplained extra scope member and still declare the month verified.

### 0C.4 Monthly-source ownership and immutable fields

The Firestore document ID is always immutable.

For `conlog_sales_monthly`, these existing fields are immutable during refresh:

```text
sourceOrigin
provider
lmPcode
meterNo
ym
y
m
```

The refresh-owned mutable fields are:

```text
amountTotalC
unitsTotal
salesGroupId
salesGroupLabel
sourceDocumentId
sourceEndRow
```

For `conlog_sales_monthly_lm`, these existing fields are immutable:

```text
sourceOrigin
provider
lmPcode
ym
y
m
```

The refresh-owned mutable fields are:

```text
metersCount
amountTotalC
unitsTotal
zeroSalesMetersCount
```

For `conlog_sales_monthly_lm_groups`, these existing fields are immutable:

```text
sourceOrigin
provider
lmPcode
ym
y
m
salesGroupId
```

The refresh-owned mutable fields are:

```text
salesGroupLabel
metersCount
amountTotalC
unitsTotal
zeroSalesMetersCount
```

An existing document must have the exact governed monthly-source field set. Missing fields, extra fields, invalid Firestore types, immutable-value drift, missing update-time evidence, or an incompatible source/provider identity are `CONFLICT`; refresh must not repair them silently.

### 0C.5 Refresh write mechanics

Missing expected documents use:

```python
batch.create(document_ref, expected_document)
```

Changed compatible documents use only exact refresh-owned field updates with the public Firestore update-time precondition contract:

```python
batch.update(
    document_ref,
    changed_refresh_owned_fields,
    option=LastUpdateOption(snapshot.update_time),
)
```

Stage 04 refresh must never use:

```text
set()
merge=True
whole-document blind replacement
delete()
per-document network write fallback
```

No refresh execution may change an immutable field.

### 0C.6 Governed 400-wave execution and verification

Version 1.10.0 batching remains binding. Expected-document preflight reads and post-write verification reads use governed `get_all` waves of no more than 400 document references. Multi-document writes use WriteBatch commits of no more than 400 create/update operations.

Refresh updates must carry the preflight snapshot update-time precondition. A concurrency or write failure stops the run; no per-document fallback is permitted.

After execution, Stage 04 refresh must verify:

- the LM/month scope count equals the governed expected row count;
- every expected document exists;
- every expected document exactly matches the governed monthly-source document after the write;
- no unexpected scope document remains;
- created, updated and unchanged counts reconcile exactly to the input rows for each of the three datasets.

The Stage 04 audit report must record create/update/unchanged/conflict/extra counts, governed batch size, read/write wave evidence, write-operation counts, verification evidence, and `perDocumentFallback = false`.

### 0C.7 Range helper contract

`scripts/04b_preflight_monthly_source_range.py` may orchestrate either `create-only` or `refresh` preflight across an explicit continuous range, but it still invokes the core Stage 04 uploader once per month and never exposes a Firestore write operation itself.

`scripts/04c_upload_monthly_source_range_dev.py` may orchestrate either `create-only` or `refresh` in `ireps2` DEV only. It must use the same mode for the fresh preflight and execute-upload of each month, stop on the first failed month, and verify the Stage 04 report before advancing.

### 0C.8 Canonical schema gate

The July 2026 monthly collection schemas predate the governed monthly-source document profile and still describe create-only Atomic-shaped fields. They must be amended and approved in the authoritative schema repository before any Stage 04 monthly-source `--execute-upload --mode refresh` is authorised against Firestore.

Pipeline-side implementation and offline tests may be prepared and reviewed before that schema amendment. No DEV, TEST or LIVE refresh write is approved until the authoritative schemas and the Master Dictionary agree with this section.


## 1. Purpose

This file is the governing architecture, implementation, data, safety, and operating contract for the iREPS Sales Pipeline.

It exists to preserve agreed decisions across developers, future maintainers, Codex and other AI agents, pipeline operators, documentation work, environment migrations, and future Trials and Production preparation.

The rules in this file must be read before analysing, designing, changing, running, or documenting this project.

---

## 2. Authority

The authority order for this project is:

1. Locked canonical collection schemas under `C:\dev\ireps-schemas` for Firestore document identity, shape, field type, and field ownership
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
iREPS_Master_Dictionary_v1.6.md
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

Completed Sales Pipeline baseline target:

```text
ireps-test
```

Current controlled Sales All Meters data-assessment and targeted-remediation environment:

```text
ireps2
DEV
```

The current DEV task does not authorise writes to TEST, Trials, or Production. Every DEV remediation must remain hard-locked to `ireps2`, the approved collection, the approved document set, and the approved field changes.

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

The current TEST baseline contains historical pipeline-derived `master.visibility = "INVISIBLE"` values because the earlier Stage 06 implementation wrote visibility from offline pipeline staging. That historical writer ownership is not authoritative.

Under the current canonical compliance rule, every persisted `sales-all-meters` document must contain `master.visibility` as exactly `VISIBLE` or `INVISIBLE`. A missing value is noncompliant. The approved operational Meter Master to Sales All Meters bridge remains the authority for deriving the value: both canonical AST and sales references produce `VISIBLE`; every other canonical reference combination produces `INVISIBLE`.

Stages 06 and 08 remain outside visibility ownership pending the later governed writer assessment. This data-rule amendment does not change either writer. Any future creation or upload path that can leave the required field absent must be identified and resolved during that writer-assessment phase before another governed load is approved.

Cleanup or reconciliation of historical TEST values requires a separate approved operational action.

Earlier Meter Master or Sales All Meters files ending at February 2026 are historical and are not the approved TEST baseline.

---

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

---

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

The Stage 02 uploader must derive its parsed rows and recorded CSV SHA-256 from one immutable byte snapshot. It must validate the exact Firestore field shape and actual field types, including rejecting booleans where an integer is required. `resume` is permitted only with the exact previous failed Stage 02 execute-upload report. That report must be fingerprint-valid and must bind the same project, collection, LM, month, provider, CSV filename, original file SHA-256, business-content SHA-256, row count, planned document-ID fingerprint, meter count, monetary totals and first/last transaction timestamps. An edited report, changed CSV, mismatched upload contract, unexpected document or type/value conflict must stop recovery.

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
C:\dev\ireps-schemas
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

The Stage 03 manifest must identify Stage 03 and the approved builder, have `status = PASS` and `result = BUILD_WRITTEN`, and contain exactly one LM, exactly one month and exactly three output entries with no extras: the approved meter-month, LM-month and LM-month-group CSVs. Stage 04 must validate the manifest identity, selected atomic input evidence, the complete governed reconciliation evidence, each output collection/type/filename/path/schema/row-count/SHA contract, and the equality of the three reconciled monthly layers before Firebase starts.

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

Stage 04 must derive every parsed CSV and its accepted SHA-256 from the same immutable byte snapshot. Existing Firestore documents used for resume or verification must match the exact governed field set and actual types; numeric equality alone must not allow a boolean, float, string or other wrong Firestore type to pass as an integer.

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

### 19.2 Builder input and full-period range rule

The normal Stage 05 operating grain is:

```text
one LM + one explicit continuous from-month/to-month range per execution
```

Stage 05 must require:

```text
--lm-pcode
--from-month
--to-month
--stage03-manifest-dir
```

For the selected LM and continuous range, Stage 05 must dynamically discover and validate exactly one approved monthly meter-level file for every required month:

```text
output/monthly/monthly__<scope>__YYYY-MM__from_atomic.csv
```

The builder must not contain a fixed historical month list. It must stop when a required month is missing, duplicated, outside the requested range, or otherwise invalid.

The builder may also use the approved reference files:

```text
input/reference/Customer_Details.csv
input/reference/90_Days_No_Purchase_Report.csv
```

The builder does not read raw Conlog source files directly. Every included Conlog month must first pass through the approved preparation, Atomic, monthly aggregation, upload, and verification stages.

`--stage03-manifest-dir` defaults to the governed `output/logs/monthly_build` directory. For each required month, Stage 05 must select one unambiguous matching successful Stage 03 source contract from that approved manifest directory; repeated manifests are acceptable only when they prove the identical Atomic and output evidence. The selected manifest must prove the exact LM/month, approved Stage 03 script, `operation = build-write`, `status = PASS`, `result = BUILD_WRITTEN`, exact three-output set, complete reconciliation evidence, and the exact meter-month filename, schema, row count and SHA-256 used by Stage 05. This local frozen-build evidence check does not replace the separate pre-Trials orchestration check for upload order and verified Firestore completion.

Stage 05 must parse each monthly/reference input and calculate the hash recorded in its manifest from one immutable byte snapshot, so the validated data and recorded SHA-256 cannot describe different file versions. Its deterministic build fingerprint must cover the nested Stage 03 manifest, Atomic-input and reconciliation evidence recorded for every monthly input.

One successful Stage 05 execution produces one complete Meter Master staging CSV for the explicit approved range. The output must record the LM, from-month, to-month, included months, row counts, reconciliation results, and SHA-256 fingerprint.

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

For 90 Days No Purchase duplicates, a customer number equal to the meter identifier is a weak placeholder. A different non-empty customer number may replace it. A blank customer number has no precedence and must never replace a valid placeholder or real customer number. Remaining competing non-placeholder customer numbers may be resolved only by the latest valid purchase date; unresolved conflicts stop the build.

The builder must report the count resolved by each approved rule.
### 19.4 Firestore document identity

The uploader writes to:

```text
meter_master/{masterId}
```

Because `masterId` equals `meterNoNormalized`, the Firestore document ID is the normalized meter number.

This deterministic document-ID design must not be changed casually because AST links, direct lookups, duplicate prevention, rebuilds, and other application logic may depend on it.

### 19.5 Approved Firestore schema and missing-data rule

Every Meter Master document created by any writer—including the Sales Pipeline, Meter Discovery, Meter Installation, migration utilities, repair utilities, and backend services—must use exactly this canonical shape:

```json
{
  "lmPcode": "ZA7423",
  "meterNo": {
    "raw": "04085345850",
    "normalized": "04085345850"
  },
  "meterType": "electricity",
  "customerNo": "",
  "accountNo": "",
  "refs": {
    "asts": {
      "id": ""
    },
    "sales": {
      "id": "",
      "provider": ""
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

The Firestore document identity is:

```text
meter_master/{meterNo.normalized}
```

The document ID must exactly equal `meterNo.normalized`.

Canonical string fields must not use `null`. Where approved source data is not available:

```text
customerNo          = ""
accountNo           = ""
refs.asts.id        = ""
refs.sales.id       = ""
refs.sales.provider = ""
```

The following creation fields are mandatory and must not be blank:

```text
lmPcode
meterNo.raw
meterNo.normalized
meterType
```

Every writer must use the common meter-number normalisation rule in Section 17 before constructing the document ID or `meterNo.normalized`.

The canonical root contains only:

```text
lmPcode
meterNo
meterType
customerNo
accountNo
refs
metadata
```

The canonical nested reference paths are only:

```text
refs.asts.id
refs.sales.id
refs.sales.provider
```

These fields are prohibited in Meter Master:

```text
id
createdBySource
updatedBySource
metadata.createdBySource
metadata.updatedBySource
parents
refs.premise
refs.trns
status
serviceProvider
visibility
```

No writer may add a field that is absent from the authoritative Meter Master schema.

The Stage 05 staging-to-Firestore mapping is:

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

`metadata` is mandatory on every Meter Master document and must contain exactly these six fields:

```text
createdAt
createdByUid
createdByUser
updatedAt
updatedByUid
updatedByUser
```

`createdBySource`, `updatedBySource`, and any additional metadata fields are prohibited.

`createdAt` and `updatedAt` must be Firestore Timestamp values. ISO strings are not permitted in these fields.

On first creation, the writer sets both the `created*` and `updated*` fields.

On every ordinary update:

```text
metadata.createdAt
metadata.createdByUid
metadata.createdByUser
```

must remain unchanged, and only these metadata paths may be refreshed:

```text
metadata.updatedAt
metadata.updatedByUid
metadata.updatedByUser
```

The Sales Pipeline system actor is:

```text
createdByUid / updatedByUid   = SYSTEM
createdByUser / updatedByUser = METER MASTER PIPELINE
```

Meter Discovery, Meter Installation, and other backend workflows must use the authenticated or otherwise approved workflow actor while keeping the same exact six-field metadata constitution.

### 19.7 Cross-writer creation, ownership, and update rule

The first approved writer that creates a missing Meter Master document must create the complete canonical document. A partial, legacy, flat, or workflow-specific creation shape is prohibited.

The shared canonical identity fields are:

```text
lmPcode
meterNo.raw
meterNo.normalized
meterType
```

The first writer creates them. Later writers must validate and preserve them. A conflicting non-empty value must stop the operation for reconciliation; it must not be silently overwritten.

The Firestore document ID and `meterNo.normalized` are immutable shared identity. `lmPcode` and `meterType` are controlled values supplied by the first approved creator; later writers validate equality and compatibility and must not migrate them during ordinary creation, matching, refresh or resume. `meterNo.raw` is supplied by the first approved creator and is preserved after creation.

Meter Discovery and Meter Installation own:

```text
refs.asts.id
```

The Sales Pipeline owns:

```text
customerNo
accountNo
refs.sales.id
refs.sales.provider
```

All writers may refresh only the three `metadata.updated*` fields during an approved update.

For an existing Meter Master document, every writer must:

- update only explicit approved dot paths;
- preserve all fields owned by other workflows;
- preserve all `metadata.created*` fields;
- reject identity, LM, meter-type, or ownership conflicts;
- remain idempotent for an already-applied identical result;
- never replace the complete `meterNo`, `refs`, or `metadata` object;
- never use a broad complete-document merge as a substitute for field ownership.

Prohibited Meter Master write patterns include:

```javascript
set(documentRef, broadDocument, { merge: true })
transaction.set(documentRef, broadDocument, { merge: true })
```

and:

```python
batch.set(document_ref, broad_document, merge=True)
```

A governed create may use a strict create operation after confirming that the deterministic document does not exist. A governed update must use explicit field-level paths.

New-document creation must use strict create semantics or an equivalent create precondition. No writer may broadly replace the complete Meter Master document, `refs`, `meterNo`, or `metadata` map.

Meter Discovery and Meter Installation must use the exact Section 17 normalisation rule:

```javascript
String(value ?? "")
  .replace(/\s+/g, "")
  .trim()
  .toUpperCase()
```

They must reject an empty normalized result, preserve leading zeroes, impose no fixed length, and create or update only the canonical Meter Master shape.

### 19.7.1 Derived lifecycle classifications

Meter Master lifecycle classifications are derived from the canonical references and are not persisted as a status, visibility or lifecycle field:

| Classification | `refs.asts.id` | `refs.sales.id` |
|---|---|---|
| `SALES_ONLY` | Blank | Populated |
| `FIELD_ONLY` | Populated | Blank |
| `MATCHED` | Populated | Populated |
| `EMPTY_OR_INCOMPLETE` | Blank | Blank |

When the Sales Pipeline creates a missing document, it must create the complete canonical shape, populate approved sales-owned values, set `refs.sales.id` to the canonical normalized meter number, set `refs.sales.provider` to the governed Conlog provider value, leave `refs.asts.id = ""`, and populate both creation and update metadata with the approved pipeline actor. This produces `SALES_ONLY`.

When Meter Discovery or Meter Installation creates a missing document, it must create the complete canonical shape, populate `refs.asts.id`, leave the sales reference, sales provider and unknown customer/account values as `""`, and populate both creation and update metadata with the operational actor. It must not invent sales data or write prohibited fields. This produces `FIELD_ONLY`.

When an operational writer finds a compatible `SALES_ONLY` document, it may update only `refs.asts.id` and `metadata.updated*`. It must preserve every identity, classification, sales-owned and `metadata.created*` field. The derived transition is `SALES_ONLY -> MATCHED`.

When the Sales Pipeline finds a compatible `FIELD_ONLY` document, it may update only `customerNo`, `accountNo`, `refs.sales.id`, `refs.sales.provider` and `metadata.updated*`. It must preserve `refs.asts.id`, all identity and classification fields, and `metadata.created*`. The derived transition is `FIELD_ONLY -> MATCHED`.

### 19.7.2 Blank-value preservation

A blank incoming value must not erase an existing valid value. Ordinary creation, matching, recurring refresh and resume are not deletion or correction processes.

In particular, a blank incoming value must not clear a populated `refs.asts.id`, `refs.sales.id`, `refs.sales.provider`, `customerNo` or `accountNo`. Intentionally clearing a protected value requires a separately governed correction or migration process.

### 19.7.3 Cloud Function and backend writer responsibilities

Every Cloud Function or backend writer touching Meter Master must use the canonical normalization rule; create the complete canonical shape with strict create semantics when absent; use exact writer-owned field paths when present; preserve other workflows' fields and `metadata.created*`; use native Firestore Timestamp values for Meter Master metadata; use `""`, not `null`, for missing canonical strings; reject prohibited fields and broad merges; validate identity, `lmPcode`, `meterType`, canonical shape, ownership and Firestore types before writing; remain idempotent; and produce or return stable conflict information for an unsafe record.

### 19.8 Stage 05 build and Stage 07 upload operating rule

Stage 05 operates at:

```text
one LM + one explicit continuous from-month/to-month range
```

It dynamically discovers and validates every monthly meter-level file in that range and produces one complete approved Meter Master CSV.

Stage 07 operates at:

```text
one explicit Firebase project
+ one approved frozen full-period Meter Master CSV
+ the matching successful Stage 05 BUILD_WRITTEN manifest
```

Before any Firestore connection or write, Stage 07 must verify the Stage 05 manifest schema, status, result, deterministic build fingerprint, governed LM/range/provider/meter type, output row count, exact ten-column schema, output filename, and output SHA-256 against the supplied CSV.

Stage 07 has three distinct governed modes:

```text
create-only
refresh
resume
```

`create-only` is the first approved load into an empty Meter Master collection and creates complete canonical sales-originated documents.

`refresh` applies a newly approved full-period Stage 05 build to an established Meter Master collection under Section 19.8.1.

`resume` is restricted recovery under Section 19.9 from a failed execution of the exact same frozen upload contract. It is not a refresh, enrichment, migration or correction mode.

Stage 07 must never broadly merge an existing Meter Master document.

### 19.8.1 Recurring Meter Master refresh

For every incoming row, `refresh` must independently classify the record as `CREATED`, `UPDATED`, `UNCHANGED`, `CONFLICT` or `FAILED`.

- A missing document is `CREATED` using the complete canonical sales-originated shape and strict create semantics.
- A compatible document requiring sales enrichment is `UPDATED` only at exact approved sales-owned paths and `metadata.updated*`. Operational fields, identity fields and `metadata.created*` are preserved.
- A compatible document with identical approved sales-owned values is `UNCHANGED`. It receives no Firestore write and `metadata.updated*` does not change.
- A conflicting or unsafe document is `CONFLICT`. It receives no write, is added to the conflict report, and does not stop remaining valid records.
- An isolated record write failure is `FAILED`, is reported, and does not automatically stop remaining valid records. A systemic failure is governed by Section 19.11.

No Meter Master document may be deleted merely because it is absent from a refreshed sales build.

Refresh must be idempotent. Reapplying the same approved input to the same final document state must produce the same classifications, must not create unnecessary writes, and must classify compatible identical records as `UNCHANGED`.

### 19.9 Controlled resume rule

`resume` may be used only to recover from a verified partial failure involving:

```text
the same Firebase project
the same approved Stage 05 manifest and build fingerprint
the same approved frozen full-period CSV and CSV SHA-256
the same row count and canonical planned document-ID set
the same LM, range, provider, and meter type
```

Stage 07 `resume` must require `--resume-report` pointing to the previous failed Stage 07 report. The report must itself contain a valid untampered upload-contract fingerprint and must match the current upload contract exactly. A changed or expanded CSV must never be treated as recovery.

In `resume` mode:

```text
missing planned creation
    -> create it

existing exact matching pipeline-created result
    -> skip it

existing null in place of a governed empty string
    -> stop and report the type conflict

invalid or non-pipeline metadata actor/timestamp
    -> stop and report the metadata conflict

populated or changed operational refs.asts.id
    -> stop; use a separate reviewed reconciliation or migration process

existing conflicting result
    -> stop and report the conflict

unexpected extra document
    -> stop and report the conflict
```

`resume` is not a general update, refresh, migration, or enrichment mode. It must not introduce a different CSV, range, fingerprint, or changed business data.

### 19.9.1 Record-level conflict continuation

An isolated identity, LM, meter-type, reference, metadata, type or shape conflict is record-level. Stage 07 must skip the affected record, record existing and incoming evidence plus the source writer or stage, assign a stable conflict code, and continue processing all remaining valid records.

Stable conflict codes include:

```text
MM_DOCUMENT_ID_NONCANONICAL
MM_NORMALIZED_IDENTITY_CONFLICT
MM_LM_CONFLICT
MM_METER_TYPE_CONFLICT
MM_AST_REFERENCE_CONFLICT
MM_SALES_REFERENCE_CONFLICT
MM_SALES_PROVIDER_CONFLICT
MM_CREATED_METADATA_INVALID
MM_GOVERNED_FIELD_TYPE_INVALID
MM_DOCUMENT_SHAPE_UNSAFE
MM_CANONICAL_FIELD_MISSING
MM_BLANK_WOULD_ERASE_VALID_VALUE
MM_TRANSACTION_PRECONDITION_CHANGED
MM_RECORD_WRITE_FAILED
```

A widespread or systemic form of the same problem may be promoted to a run-level failure. The governing record-level rule is: skip the affected record, report it, and continue.

### 19.10 Reusable cross-project uploader rule

The same approved frozen Meter Master CSV may be reused across explicitly approved Firebase projects.

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
--manifest
--mode
```

`--resume-report` is additionally mandatory when `--mode resume` is selected and is prohibited in normal `create-only` mode.

The value supplied through `--confirm-project` must exactly match `--project-id`.

Before connecting to Firestore, the uploader must read the `project_id` inside the service-account JSON and verify that it exactly matches the requested target project.

A mismatch must stop the upload before any Firestore write.

The approved operating model is:

```text
Build one full-period CSV
    -> validate and freeze it
    -> upload that same approved CSV to each explicitly approved Firebase project
```

The CSV must not be manually changed between TEST, Trials, and Production uploads.

The uploader must calculate and report the CSV SHA-256 fingerprint so that uploads to different projects can be proven to use the same source file.

### 19.11 Validation and reporting rule

Before upload, the uploader must verify:

- the matching successful and untampered Stage 05 manifest and build fingerprint;
- the CSV SHA-256, row count, filename, exact ten-column schema and column order against that manifest;
- non-empty, unique, already canonical `masterId` values;
- `masterId = meterNoNormalized` and `salesId = meterNoNormalized` after canonical verification;
- current governed values `salesProvider = conlog` and `meterType = electricity`;
- blank pipeline `astId` values so Stage 07 cannot create operational AST links;
- the explicit approved full-period range represented by the input;
- valid project confirmation;
- service-account project match;
- approved upload mode and, for resume, the exact failed original upload report;
- target collection state appropriate to that mode.

The uploader must display a preflight summary containing at least:

```text
target project
target collection
input CSV
from-month
to-month
included months
row count
unique master ID count
LM pCode values
provider values
CSV SHA-256
upload mode
```

Every run must produce a local JSON report under:

```text
output/logs/meter_master
```

The report must record at least `runId`, `stage`, `script`, `operation`, `mode`, `projectId`, `collection`, Stage 05 manifest identity, CSV fingerprint, manifest fingerprint, `lmPcode`, approved from-month, approved to-month, included months, `startedAt`, `finishedAt`, `status`, `result`, `rowsRead`, created, updated, unchanged, conflict and failed counts, write-attempt and write-success counts, verification evidence, and the conflict-report path when conflicts exist.

Every incoming row must be accounted for exactly once:

```text
created + updated + unchanged + conflicts + failed = rowsRead
```

The companion local conflict report must record at least `runId`, `masterId`, `lmPcode`, source row when applicable, conflict code, conflicting paths, existing and incoming values, message, `detectedAt`, `writeAttempted`, and an investigation recommendation. No Firestore reporting collection may be created.

Allowed final run results are `COMPLETED`, `COMPLETED_WITH_CONFLICTS`, and `FAILED`. `COMPLETED_WITH_CONFLICTS` means all safely processable records completed while conflicts were skipped and reported. `FAILED` is reserved for a genuine run-level failure such as an invalid input contract or fingerprint, project or credential mismatch, systemic Firestore failure, inability to produce the mandatory report, invalid final accounting, or final verification failure that prevents trust in the complete run.

No Meter Master upload is complete until the resulting Firestore document count and deterministic document samples have been verified. Sample verification must confirm exact nested shape, actual string types rather than `null`, canonical identity values, governed Conlog/electricity values, Firestore Timestamp-compatible metadata values, and the exact pipeline metadata actors.

The identity design must be reviewed before multi-LM or multi-provider Production rollout.

---

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

Stage 06 must require the matching successful Stage 05 manifest through `--master-manifest`. Before building, it must verify the Stage 05 build fingerprint, Meter Master filename, schema, row count and SHA-256, together with the exact monthly filenames, row counts and SHA-256 values recorded by Stage 05.

Stage 06 must read each validated Stage 05 manifest, Meter Master CSV and monthly CSV as an immutable byte snapshot. Every parsed input and accepted or recorded hash must come from that same snapshot.

`--as-of-date` is mandatory. It must not default to the machine date because identical source files must produce the same `daysSinceLastPurchase` values on every approved rebuild.

A successful Stage 06 execution must atomically write both:

```text
sales_all_meters__<lmPcode>__FULL__<from-month>_to_<to-month>.csv
sales_all_meters__<lmPcode>__FULL__<from-month>_to_<to-month>.manifest.json
```

Both files must be written under `output/sales_all_meters`. The CSV filename and directory are exact governed identities; aliases, alternate directories and merely range-containing filenames are not approved.

The Stage 06 manifest must record the Stage 05 manifest fingerprint, Meter Master SHA-256, every included monthly input SHA-256, LM, range, included months, explicit as-of date, provider, output schema, output row count, output SHA-256, planned document-ID fingerprint, total sales amount, visibility ownership, build statistics and one deterministic build fingerprint.

### 20.2 Staging contract

Fixed columns:

```text
masterId
meterNo
meterNoNormalized
provider
customerNo
accountNo
totalAmountC
lastPurchaseAtISO
daysSinceLastPurchase
```

The Stage 06 staging CSV must not contain a `visibility` column.

They are followed by one dynamic integer-cent column per included month:

```text
amount_YYYY_MM_C
```

`totalAmountC` must equal the sum of all included monthly amount columns. Every positive monthly sales amount must have a valid timezone-aware `lastPurchaseAtISO` belonging to that same applicable sales month and therefore to the approved continuous range. `lastPurchaseAtISO` is the latest valid purchase across the included range. Every positive `totalAmountC` must have populated recency fields. `daysSinceLastPurchase` is calculated from the explicit build as-of date when reproducibility is required.

Every Meter Master identity remains present. A meter with no sales has zero totals and blank CSV last-purchase fields.

### 20.3 Required operational visibility and ownership

Every persisted `sales-all-meters` document must contain:

```text
master.visibility
```

The field is mandatory.

Its Firestore type must be:

```text
string
```

Its only allowed values are:

```text
VISIBLE
INVISIBLE
```

A missing, null, blank, `NAv`, differently cased, or otherwise unsupported value is noncompliant.

For a governed remediation of an existing document where `master.visibility` is absent and no approved evidence establishes `VISIBLE`, the safe default is:

```text
INVISIBLE
```

This required-field rule does not transfer visibility ownership to the Sales Pipeline. The Sales Pipeline prepares and uploads Sales All Meters sales-awareness fields only.

The Sales Pipeline does not own the operational visibility lifecycle. Stage 08 has one narrowly bounded creation responsibility: when it strictly creates a previously nonexistent document, it must initialize `master.visibility = "INVISIBLE"`. This safe creation default satisfies required canonical field presence; it is not an operational visibility decision and is not derived from Meter Master relationships. Stage 08 must preserve the existing valid value on every existing document and must not overwrite, clear, reset, or derive it.

Mandatory current stage ownership:

```text
Stage 05 may carry astId as part of the Meter Master staging contract.
Stage 06 must ignore astId for visibility and must not output a visibility column.
Stage 08 must initialize master.visibility = INVISIBLE only during strict first creation.
Stage 08 must preserve existing master.visibility and must not overwrite, clear, reset, or derive it.
```

`master.visibility` is owned by the approved operational Meter Master to Sales All Meters bridge used by Meter Discovery, Meter Installation, and other approved meter-registration workflows.

The operational derivation is:

```text
refs.asts.id populated
AND
refs.sales.id populated
    -> MATCHED
    -> master.visibility = VISIBLE

any other canonical reference combination
    -> SALES_ONLY, FIELD_ONLY, or EMPTY_OR_INCOMPLETE
    -> master.visibility = INVISIBLE
```

This visibility is a Sales All Meters operational projection. It is not stored in Meter Master and it does not change the derived Meter Master lifecycle classification.

A sales link by itself does not make a meter `VISIBLE`. An AST link by itself does not make a meter `VISIBLE`. Both canonical links are required.

Required field presence and operational ownership are distinct. Stage 08 supplies only the safe `INVISIBLE` creation default. The approved operational bridge owns every subsequent lifecycle change and derives the value from canonical Meter Master relationships.

### 20.3.1 Operational bridge write contract

The approved bridge must:

- normalize and use the canonical Meter Master document ID;
- preserve all Sales Pipeline-owned Sales All Meters fields;
- update only `master.id` and `master.visibility`;
- ensure that a reconciled Sales All Meters document contains a valid required `master.visibility`;
- derive `VISIBLE` or `INVISIBLE` from the canonical Meter Master references rather than from a UI assumption;
- remain idempotent and perform no write when the required field is already present and the relevant Meter Master references and derived visibility have not changed;
- never create, merge, overwrite, or clear sales totals, monthly totals, customer, account, provider, purchase date, or purchase-recency fields;
- never add `metadata` or any other field that is absent from the authoritative Sales All Meters schema;
- report a missing Sales All Meters projection when a sales-linked Meter Master exists but the expected projection document is absent;
- avoid broad complete-document writes.

A missing `master.visibility` is a compliance defect. Correcting that defect must change only the approved visibility path unless a separate migration explicitly approves other field changes.

The current operational implementation must be corrected during the later writer-assessment phase if it writes `metadata.updated*` into Sales All Meters, because the locked current Sales All Meters shape has no metadata contract. This rules amendment does not itself change or deploy that implementation.

A blank Meter Master `refs.asts.id` means only that no operational AST relationship is linked. It does not prove that sales data is absent. A blank `refs.sales.id` means only that no approved sales relationship is linked. It does not prove that the physical meter is absent.

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

The canonical root field set is exactly:

```text
master
meterNo
meterNoNormalized
provider
customerNo
accountNo
totalAmountC
monthlyTotalsC
lastPurchaseAtISO
daysSinceLastPurchase
```

The canonical `master` map contains exactly:

```text
id
visibility
```

The Firestore document identity rule is:

```text
document ID = master.id = meterNoNormalized
```

`master.visibility` is required and must be exactly `VISIBLE` or `INVISIBLE`.

The `monthlyTotalsC` keys are dynamic. The current Sales All Meters schema does not contain metadata. Root-level `metadata` is noncanonical.

Stage 06 must omit a staging `visibility` column. Stage 08 must add `master.visibility = "INVISIBLE"` only while strictly creating a previously nonexistent Firestore document. For an existing document, Stage 08 must preserve the valid stored value and must never reset `VISIBLE` to `INVISIBLE`. A persisted document with missing visibility is noncanonical.

The approved operational bridge may add or update only:

```text
master.id
master.visibility
```

An operational bridge must preserve every Sales Pipeline-owned field and must not add `metadata.updated*` unless a future governed schema amendment explicitly introduces metadata.

### 20.5 Upload safety

The uploader requires explicit `--project-id`, `--confirm-project`, `--service-account`, `--input`, `--manifest` and `--mode` values.

Stage 08 must validate the successful Stage 06 manifest before connecting to Firestore. The manifest build fingerprint, CSV filename, CSV SHA-256, row count, columns, document-ID fingerprint, month range, provider, total amount and visibility ownership must match the supplied CSV exactly. A missing, edited, stale or mismatched manifest must stop the upload.

The Stage 08 preflight must reject any staging CSV that:

- contains a `visibility` column;
- contains a provider other than the currently governed `conlog` value;
- contains any blank provider value; every row must use exactly `conlog`;
- contains a noncanonical `masterId` or `meterNoNormalized` value;
- contains blank, negative, non-integer or unreconciled monetary values;
- contains a broken last-purchase and days-since-last-purchase pair;
- contains positive sales with either recency field blank;
- contains a purchase timestamp outside the manifest range or outside the latest applicable positive sales month;
- contains a `daysSinceLastPurchase` value that does not equal the day difference between the UTC purchase date and the Stage 06 manifest `asOfDate`;
- contains a non-contiguous dynamic month range.

Normal mode is `create-only` against an empty collection. `resume` is restricted to recovery from a verified partial upload of the exact same frozen CSV and upload contract.

`resume` must require the previous failed Stage 08 JSON report. The report fingerprint must match the same Firebase project, Stage 06 manifest SHA-256 and build fingerprint, Stage 05 upstream fingerprint, CSV SHA-256, row count, planned document-ID set, month range, provider, explicit as-of date and total sales amount. A changed or edited report, a different CSV, conflicts, or unexpected documents must stop recovery.

Stage 08 compares and verifies all Sales Pipeline-owned fields and required canonical visibility presence. During strict creation it initializes `master.visibility = "INVISIBLE"`. During resume or any other encounter with an existing document, it preserves the existing valid visibility value and excludes that value from pipeline-owned equality comparison. It must not overwrite, clear, reset, or derive existing visibility, classify a missing, null, blank, or invalid value as canonical, or use the ownership exception to tolerate changes to `master.id` or any Sales Pipeline-owned field.

Firestore create operations are required. Broad merge, update, delete and overwrite operations are prohibited. Every run must record the CSV SHA-256 and deterministic upload fingerprint, write an atomic JSON report, verify the final collection count, and verify deterministic document samples against the exact pipeline-owned Firestore shape and types.

Stage 08 must parse the supplied CSV and calculate the accepted CSV SHA-256 from one immutable byte snapshot. A file version read after parsing must not be used as the hash evidence for the already parsed rows.

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

The document count and sales totals remain the approved historical TEST baseline. Its pipeline-derived `master.visibility` values are not authoritative and are governed by the correction in Section 20.3.

---

## 21. Visibility rule

Visibility terminology must follow the iREPS Master Dictionary.

Every persisted Sales All Meters document must contain:

```text
master.visibility
```

The field is required, must be a string, and must be exactly one of:

```text
VISIBLE
INVISIBLE
```

The approved operational Meter Master to Sales All Meters bridge is the authority for deriving `master.visibility`.

The governing derivation is:

```text
MATCHED Meter Master
refs.asts.id populated + refs.sales.id populated
    -> VISIBLE

SALES_ONLY, FIELD_ONLY, or EMPTY_OR_INCOMPLETE
    -> INVISIBLE
```

Stage 06 must not calculate, output, or change visibility. Stage 08 must initialize `INVISIBLE` only during strict first creation and must preserve the valid value on existing documents. Neither stage determines operational visibility. The approved operational bridge owns all subsequent lifecycle changes.

A form-side or report-side indication may assist the user, but it must not be treated as final truth unless it comes from the approved operational `master.visibility` field in Sales All Meters.

Any change to `VISIBLE` or `INVISIBLE` behaviour requires review across pipeline, backend, mobile, web, Meter Master, Sales All Meters, Master Dictionary, canonical schemas, and rules.

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
- Sales All Meters exact root field set;
- Sales All Meters identity equality between document ID, `master.id`, and `meterNoNormalized`;
- required Sales All Meters `master.visibility` presence, string type, and allowed enum value;
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

## 28. Current implementation status and next governance actions

Sales All Meters visibility governance and the authoritative schema must remain aligned before a writer assessment or governed load proceeds.

The Lesedi `ireps-test` Sales Pipeline baseline is complete:

1. RAW and RAW STAGING prepared through June 2026.
2. Atomic Sales built, uploaded and verified for ten months.
3. Monthly meter, LM and group datasets built, reconciled and uploaded.
4. Meter Master rebuilt, validated and uploaded with 35,295 documents.
5. Sales All Meters rebuilt, reconciled and uploaded with 35,295 documents.
6. CSV fingerprints and JSON upload reports recorded.
7. Governed script changes committed and pushed.

Immediate governed actions are:

1. keep this single rules file current;
2. keep the authoritative Sales All Meters schema under `C:\dev\ireps-schemas` aligned with this contract;
3. assess every Sales All Meters writer only in a separately governed writer-assessment phase;
4. update the project README and iREPS Master Dictionary where required;
5. commit verified rules and schema changes in their respective repositories;
6. keep operational CSVs, credentials and upload reports outside Git.

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

---

## 31. Decision history

### 2026-07-20 — Sales All Meters creation default and operational ownership aligned

Version 1.8.9 requires `master.visibility` on every canonical `sales-all-meters` document. Stage 06 remains visibility-free. Stage 08 initializes `INVISIBLE` only when strictly creating a previously nonexistent document and preserves the valid value thereafter. This default establishes required field presence but does not determine operational visibility. The approved operational bridge owns all subsequent visibility lifecycle changes and may update only `master.id` and `master.visibility` while preserving every Sales Pipeline-owned field. Root metadata and unexpected fields remain prohibited.

### 2026-07-20 — Sales All Meters visibility made mandatory in DEV governance

Version 1.8.8 makes `master.visibility` a required field on every persisted `sales-all-meters` document. The only allowed values are `VISIBLE` and `INVISIBLE`; missing, null, blank, `NAv`, or unsupported values are noncompliant. For controlled remediation of an existing missing value, the safe default is `INVISIBLE` unless approved canonical Meter Master evidence establishes `MATCHED` and therefore `VISIBLE`.

This decision established the required stored field. Version 1.8.9 subsequently clarified the strict-creation default and continuing operational ownership. The repository continues to use the single `rules/SALES_PIPELINE_RULES.md` file with dedicated sections; no separate Sales All Meters rules file is introduced.

### 2026-07-19 — Meter Master lifecycle and recurring-refresh governance approved

Version 1.8.6 locks the source-neutral Meter Master lifecycle classifications, cross-writer ownership boundaries, blank-value preservation, native Firestore Timestamp metadata, and exact-path update rules. Stage 07 now has separate `create-only`, `refresh`, and `resume` modes, record-level conflict continuation, idempotent no-write handling for unchanged records, local run and conflict reporting, and run-level failure criteria. Lifecycle classifications remain derived and no status field or Firestore reporting collection is introduced.

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

### 2026-07-13 — Dynamic full-period builders approved

Stages 05 and 06 are governed downstream full-period builders.

Each execution requires one LM and one explicit continuous `--from-month` to `--to-month` range. The builders must dynamically discover and validate every required monthly meter-level file inside that range and must not contain a fixed historical month list.

### 2026-07-13 — Environment-neutral builds

Local build scripts must not select Firebase environments.

Upload scripts must require an explicit target project.

### 2026-07-13 — Meter Master v3 and Firestore contract approved

`scripts/05_build_meter_master_v3.py` is the approved Meter Master staging builder.

Its ten-column CSV output and the final `meter_master` Firestore schema are locked in Section 19.

All Meter Master documents require six-field metadata. Pipeline reruns must preserve existing `metadata.created*` values and populated `refs.asts.id` links.

### 2026-07-13 — Reusable frozen full-period Meter Master uploader approved

Stage 07 uploads one approved frozen full-period Meter Master CSV to one explicitly selected Firebase project.

At the time of this decision, the approved mode was `create-only` against an empty collection and `resume` was restricted recovery from a partial upload of the same approved CSV and fingerprint. The 2026-07-19 decision supersedes the mode summary by adding the distinct governed `refresh` mode while preserving the restricted meaning of `resume`.

The uploader is not a normal month-by-month enrichment mechanism. Any replacement, reconciliation, or migration of an established Meter Master collection requires a separate reviewed plan that protects workflow-owned fields and immutable creation metadata.

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

### 2026-07-14 — Split stage-execution grains locked

The approved Sales Pipeline operating model is:

```text
Stages 00–04
    one LM + one month per execution

Stages 05–06
    one LM + one explicit continuous from-month/to-month range

Stages 07–08
    one explicit Firebase project + one approved frozen full-period CSV
```

Stages 05 and 06 must dynamically discover and validate all months inside the explicit range and must not contain a fixed historical month list.

Stage 08 uses `create-only` as its normal mode. Stage 07 supports the separately governed `create-only`, `refresh`, and `resume` modes under the 2026-07-19 Meter Master decision. For both stages, `resume` remains restricted to recovery from a partial upload of the same frozen CSV and fingerprint.

The earlier universal month-by-month statement for Stages 05 through 08 was incorrect and is not governing.

---

### 2026-07-14 — Lesedi TEST Sales Pipeline baseline completed

---

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
### 2026-07-16 — Meter Master governance contradiction corrected

The rules file contained contradictory Stage 05–08 operating models: the correct split-stage model in Sections 13 and 14, and an incorrect universal month-by-month model in the former Section 19 and decision history.

This amendment confirms:

```text
Stages 00–04 = one LM + one month
Stages 05–06 = one LM + explicit continuous range
Stages 07–08 = one approved frozen full-period CSV to one explicit project
```

It also confirms that every Meter Master writer must create the exact canonical shape, use governed empty strings where optional data is unavailable, use Firestore Timestamp metadata, apply the common whitespace-removing meter-number normalization, preserve fields owned by other workflows, and avoid broad document merges.

No backend writer code may be changed until a fresh read-only assessment has been run against this corrected governing file and the authoritative Meter Master schema.

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

### 2026-07-20 — Sales All Meters visibility creation and lifecycle ownership aligned

Changed rule:

- `master.visibility` is required and must be exactly `VISIBLE` or `INVISIBLE`;
- Stage 06 does not calculate, output, or change visibility;
- Stage 08 initializes `master.visibility = "INVISIBLE"` only during strict first creation;
- Stage 08 preserves a valid existing visibility and must not overwrite, clear, reset, or derive it;
- the approved operational bridge owns all subsequent lifecycle changes and may update only `master.id` and `master.visibility` while preserving every Sales Pipeline-owned field;
- root metadata and unexpected root or `master` fields remain prohibited.

Reason:

Required canonical field presence and authority to determine operational visibility are separate responsibilities. A strict first creation needs a safe required value, while later operational truth must remain controlled by the approved bridge.

Effect on code or data:

- the governing rules and authoritative schema move to one consistent contract;
- Stage 08 requires a later, separately governed writer correction before another load if its implementation does not yet supply the creation default and preservation behaviour;
- no runtime writer or Firestore data is changed by this governance amendment.

Migration action:

- none; any future code correction or data remediation requires its own governed task.

### 2026-07-20 — Sales All Meters required visibility amendment

Changed rule:

- every persisted `sales-all-meters` document must contain `master.visibility`;
- the field must be a string with exactly `VISIBLE` or `INVISIBLE`;
- missing, null, blank, `NAv`, or unsupported values are noncompliant;
- the governed safe value for a missing field was `INVISIBLE` unless approved Meter Master evidence established `MATCHED`.

Reason:

The project approved visibility as a mandatory canonical field rather than an optional projection.

Effect on code or data:

- this amendment changed the governing data-compliance rule;
- it did not change or deploy Stage 06, Stage 08, the operational bridge, or any other writer;
- version 1.8.9 supersedes its unresolved creation-ownership language.

Supersession note:

Version 1.8.9 preserves the required-field rule and supersedes the earlier conclusion that Stage 08 could not supply the safe strict-creation default.

### 2026-07-16 — Sales All Meters visibility ownership corrected

Historical status: Stage 06 remains outside visibility authority. Version 1.8.9 supersedes the Stage 08 omission rule with the safe strict-creation default and preservation contract.

Changed rule:

- the Sales Pipeline must not derive operational visibility in `sales-all-meters`;
- Stage 06 must not output a `visibility` column and must not derive visibility from Meter Master `astId`;
- Stage 08 was originally required to omit `master.visibility` from pipeline-created documents; version 1.8.9 supersedes that historical rule;
- approved operational meter-registration, Meter Discovery, and Meter Installation writers own subsequent lifecycle changes.

Reason:

Sales data and pipeline staging cannot prove whether a physical meter has been registered, discovered, installed, or made operationally visible in iREPS. A blank `astId` is not evidence of invisibility.

Effect on code or data:

- `scripts/06_build_sales_all_meters.py` must remove the visibility projection and related statistics;
- `scripts/08_upload_sales_all_meters.py` must be corrected separately before the next governed upload;
- existing TEST `master.visibility = "INVISIBLE"` values are historical pipeline-derived values and require a separate approved cleanup or migration decision.

### 2026-07-16 — Stage 07 frozen-build and resume controls strengthened

Changed rule:

- Stage 07 must require and verify the matching successful Stage 05 manifest before connecting to Firestore;
- Meter Master identity columns must already satisfy the canonical whitespace-removal and uppercase rule, not merely equal one another;
- `resume` must require the exact previous failed Stage 07 report and match its manifest, CSV SHA-256, row count, planned document-ID fingerprint, project, LM, range, provider, and meter type;
- resume comparison must distinguish `null` from governed empty strings and validate actual metadata timestamp types and pipeline actor values;
- operational `refs.asts.id` changes are outside upload recovery and must block Stage 07 resume;
- post-upload completion requires count verification plus deterministic document-shape samples.

Reason:

Equality-only checks and count-only verification could accept noncanonical IDs, type drift, invalid metadata, a different recovery source, or an operationally changed collection.

Effect on code or data:

- `scripts/07_upload_meter_master_v3.py` requires `--manifest` for all uploads and `--resume-report` for resume;
- old Stage 07 reports without the frozen upload contract cannot authorise resume;
- no Firestore data is changed by this rules amendment; established or operationally modified collections require a separate reviewed reconciliation or migration plan.

### 2026-07-16 — Stage 08 frozen-upload and visibility-preservation controls strengthened

Historical status: the visibility-free Stage 06 CSV remains current. Version 1.8.9 supersedes Stage 08 omission with the strict-creation `INVISIBLE` default and retains preservation of existing visibility.

Changed rule:

- Stage 08 must consume the visibility-free Stage 06 CSV contract and must reject a `visibility` column;
- the current governed provider is exactly `conlog`;
- `masterId` and `meterNoNormalized` must already satisfy the canonical uppercase whitespace-removal rule;
- `resume` must require the exact previous failed Stage 08 report and match its project, CSV SHA-256, row count, planned document-ID fingerprint, month range, provider and total amount;
- Stage 08 must preserve operational `master.visibility` on existing documents while comparing every Sales Pipeline-owned field strictly;
- post-upload completion requires exact collection-count verification plus deterministic document-shape and type samples.

Reason:

The previous uploader could consume the obsolete visibility-bearing CSV, accept unsupported providers, treat a different CSV as resume input, and declare success from count verification alone. These behaviours could create or validate incorrect Sales All Meters documents and could overwrite the established visibility ownership boundary.

Effect on code or data:

- `scripts/08_upload_sales_all_meters.py` now requires the visibility-free Stage 06 shape and `provider = conlog`;
- old Stage 08 reports without the frozen upload contract cannot authorise resume;
- the historical implementation created documents with `master.id` but no `master.visibility`; version 1.8.9 requires a later writer correction before another load;
- an operationally added `master.visibility` is preserved during recovery and verification;
- no existing Firestore data is changed by this amendment; historical pipeline-derived TEST visibility values still require a separate approved migration decision.

### 2026-07-16 — Stage 06 to Stage 08 frozen manifest chain completed

Changed rule:

- Stage 06 must require and validate the exact successful Stage 05 manifest and the exact monthly files recorded by that manifest;
- `--as-of-date` is mandatory and may not default to the machine date;
- Stage 06 must atomically produce a matching successful JSON manifest containing source hashes, output hash, document-ID fingerprint, totals and a deterministic build fingerprint;
- Stage 08 must require and validate that Stage 06 manifest before opening Firestore;
- Stage 08 resume must be bound to both the Stage 06 build fingerprint and the exact failed Stage 08 upload contract.

Reason:

A CSV SHA proves only the file currently supplied. Without the Stage 06 manifest, Stage 08 could not prove that the CSV came from the approved Stage 05 Meter Master, the exact governed monthly inputs and the explicit reproducible as-of date.

Effect on code or data:

- `scripts/06_build_sales_all_meters.py` now requires `--master-manifest` and `--as-of-date`, rejects blank required monthly amounts, writes the output CSV atomically and creates the Stage 06 frozen-build manifest;
- `scripts/08_upload_sales_all_meters.py` now requires `--manifest` and validates the complete Stage 06 proof chain before Firestore access;
- old Stage 06 CSVs without a matching Stage 06 manifest are not approved inputs for the corrected Stage 08 uploader;
- no existing Firestore data is changed by this amendment.

### 2026-07-16 — Confirmed blocking upload and frozen-input defects corrected

Changed rule:

- Stage 02 resume is bound to the fingerprint-valid failed report and exact original Atomic CSV contract, and its Firestore comparisons are shape/type-strict;
- Stage 04 accepts only the exact one-LM, one-month, three-output Stage 03 manifest and validates its complete identity, Atomic and reconciliation evidence;
- Stage 05 requires matching Stage 03 frozen-build evidence for every monthly input, prevents blank NPR customer numbers from replacing populated values, and fingerprints the nested evidence;
- Stage 06 accepts only immutable validated input snapshots, enforces its exact governed output identity, and rejects positive sales without a purchase date in the applicable approved month;
- Stage 08 requires every provider value to be exactly `conlog`, validates purchase recency against the latest applicable month, and recomputes days since purchase from the Stage 06 manifest `asOfDate`;
- Stages 02, 04, 05, 06 and 08 must calculate accepted input hashes from the same immutable bytes they parse.

Reason:

Separate file reads, incomplete manifest checks, value-only Firestore comparisons, blank-provider filtering, weak NPR precedence and unrecomputed recency could approve a different source version or incorrect data while presenting a passing fingerprint or verification result.

Effect on code or data:

- the five corrected scripts fail closed on stale, incomplete, type-invalid or internally inconsistent offline inputs;
- the corrections do not connect to Firebase and do not change existing Firestore data;
- unresolved upload-order and verified-completion orchestration remains assigned to the separate pre-Trials validation/orchestration script and is not implemented inside these stage scripts.

### 2026-07-19 — Meter Master to Sales All Meters bridge contract confirmed

Changed rule:

- Meter Master lifecycle classifications remain derived and are not stored;
- `MATCHED` means both `refs.asts.id` and `refs.sales.id` are populated;
- the approved operational bridge projects `MATCHED` as `master.visibility = "VISIBLE"` in Sales All Meters;
- `SALES_ONLY`, `FIELD_ONLY`, and `EMPTY_OR_INCOMPLETE` project as `master.visibility = "INVISIBLE"` when a Sales All Meters document exists;
- Stages 06 and 08 remain prohibited from writing visibility;
- the operational bridge may update only `master.id` and `master.visibility`;
- Sales All Meters metadata is not approved under the current locked schema.

Reason:

DEV tests proved both supported field-entry paths:

```text
Meter Discovery:
SALES_ONLY -> MATCHED -> VISIBLE

Meter Installation:
SALES_ONLY -> MATCHED -> VISIBLE
```

They also proved that an LM mismatch is rejected and that an already-linked different AST is rejected without replacing the existing Meter Master link.

Effect on code or data:

- `onMeterMasterUpdated` may synchronize visibility only when the relevant Meter Master references or derived visibility change;
- the bridge must preserve every Sales Pipeline-owned Sales All Meters field;
- any current `metadata.updated*` write by the bridge is a schema-contract defect and must be removed before the writer is declared fully approved;
- the full Meter Master canonical migration does not require Sales All Meters rewrites when Meter Master references and derived visibility remain unchanged;
- Stage 08 recurring refresh remains a separate pending implementation and is not provided by `create-only` or `resume`.
