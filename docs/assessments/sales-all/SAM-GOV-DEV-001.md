# SAM-GOV-DEV-001 — Sales All Meters Governance Alignment

## 1. Executive summary

The governing rules required `master.visibility` on every canonical `sales-all-meters` document, while the locked authoritative schema version 1.0.0 made it optional and required Stage 08 to omit it. This task resolves that contradiction through one ownership model:

- Stage 06 remains visibility-free and has no visibility authority.
- Stage 08 initializes `master.visibility = "INVISIBLE"` only when strictly creating a previously nonexistent document.
- Stage 08 preserves a valid existing visibility and never overwrites, clears, resets, or derives it.
- The approved operational bridge owns all subsequent lifecycle changes and may update only `master.id` and `master.visibility` while preserving every Sales Pipeline-owned field.

Files modified:

1. `C:\dev\ireps-pipeline-sales\rules\SALES_PIPELINE_RULES.md`
2. `C:\dev\ireps-schemas\sales-all-meters\sales_all_meters.schema.md`
3. `C:\dev\ireps-pipeline-sales\docs\assessments\sales-all\SAM-GOV-DEV-001.md`

**Final alignment result: PASS.** No runtime writer was modified; implementation work remains outside this task.

## 2. Sources inspected

| Source | Repository branch | Commit at inspection | Path |
|---|---|---|---|
| Governing rules | `main` | `75ed66468504c583af150245dcd8b2717f657c99` | `C:\dev\ireps-pipeline-sales\rules\SALES_PIPELINE_RULES.md` |
| Previous writer assessment | `main` | `75ed66468504c583af150245dcd8b2717f657c99` | `C:\dev\ireps-pipeline-sales\docs\assessments\sales-all\SAM-WRITER-DEV-001.md` |
| Authoritative schema | `main` | `a17123142afba10224d793c2ea00bb9f745b64f3` | `C:\dev\ireps-schemas\sales-all-meters\sales_all_meters.schema.md` |
| Existing DEV evidence | `main` | `75ed66468504c583af150245dcd8b2717f657c99` | `C:\dev\ireps-pipeline-sales\scripts\tools\sales-all\reports\SAM-DATA-DEV-004__20260720T113922872Z` |

The schema package contains one Sales All Meters file and no directly associated executable validators, tests, fixtures, indexes, package manifest, or version metadata outside that file.

The supplied task identifies the earlier assessment's correct gate failure as starting evidence. The current local `SAM-WRITER-DEV-001.md` is internally inconsistent with that history: it labels the rules/schema visibility discrepancy “non-blocking” and reports a PASS despite documenting required-versus-optional wording. This governance task does not alter that historical report; it resolves the underlying authoritative conflict.

## 3. Original conflict

Before this task:

- Rules version 1.8.8 required `master.visibility` on every persisted document and allowed only `VISIBLE` or `INVISIBLE`.
- Locked schema version 1.0.0 called visibility optional, showed canonical examples without it, instructed Stage 08 to omit it, and treated its absence as canonical.
- Current read-only DEV evidence showed 34,657 documents, all with valid visibility, zero noncanonical documents under the stricter validator, zero identity mismatches, and no prohibited root metadata.

The documentary conflict was material because the rules rejected a newly created visibility-free document while schema 1.0.0 explicitly authorised one. A writer assessment could not reliably classify creation behaviour against two different canonical contracts; blocking at that gate was therefore correct.

## 4. Approved ownership model

### Stage 06 responsibility

Stage 06 does not calculate, derive, output, or change visibility. Its CSV remains visibility-free.

### Stage 08 creation responsibility

On strict first creation of a previously nonexistent Firestore document, Stage 08 writes the complete canonical document and initializes `master.visibility = "INVISIBLE"`. This safe default establishes required field presence only; it is not an operational visibility determination.

### Stage 08 preservation responsibility

When Stage 08 encounters an existing document, including controlled resume, it preserves the existing valid value. It must not overwrite, clear, reset `VISIBLE` to `INVISIBLE`, or derive visibility. Missing, null, blank, or unsupported visibility remains noncanonical.

### Operational bridge responsibility

The approved bridge owns all subsequent visibility lifecycle changes. It derives operational visibility from governed Meter Master relationships, may update only `master.id` and `master.visibility`, and must preserve every Sales Pipeline-owned field. Root metadata remains prohibited.

## 5. Rules changes

`SALES_PIPELINE_RULES.md` advanced from working-tree version 1.8.8 to 1.8.9.

| Section | Final line reference | Change |
|---|---:|---|
| Document control | 6–8 | Version 1.8.9 and governance-alignment phase |
| 20.3 Required operational visibility and ownership | 1671–1735 | Separates required presence from lifecycle ownership; defines Stage 08 strict-creation default and existing-document preservation |
| 20.3.1 Operational bridge write contract | 1737–1756 | Retains bridge-only lifecycle derivation and exact write boundary |
| 20.4 Firestore shape | 1758–1830 | Requires exactly `id` and `visibility` in `master`; prohibits missing visibility, metadata, and unexpected fields |
| 20.5 Upload safety | 1832–1859 | Requires strict-creation default, existing-value preservation, and canonical visibility validation |
| 21 Visibility rule | 1877–1911 | States Stage 06, Stage 08, and bridge responsibilities generically |
| 28 Current implementation status | 2085 onward | Removes obsolete DEV incident/remediation instructions and retains generic next actions |
| 31 Decision history | 2153–2163 | Adds concise version 1.8.9 decision entry |
| 32 Rule amendment | 2407 onward | Adds the required changed-rule/reason/effect/migration record and marks older omission language historical/superseded |

No unrelated pipeline requirements were intentionally changed.

## 6. Schema changes

The authoritative schema advanced from locked version 1.0.0 to locked version 1.1.0.

- All ten root fields remain required and exact: `master`, `meterNo`, `meterNoNormalized`, `provider`, `customerNo`, `accountNo`, `totalAmountC`, `monthlyTotalsC`, `lastPurchaseAtISO`, and `daysSinceLastPurchase`.
- `master` now contains exactly required `id` and required `visibility`.
- Visibility is a string enum containing only `VISIBLE` and `INVISIBLE`.
- Missing, null, blank, or unsupported visibility is explicitly noncanonical.
- Root metadata, unexpected root fields, and unexpected `master` fields remain prohibited.
- Both canonical examples now include `master.visibility = "INVISIBLE"`.
- Stage 08 creation, preservation, and operational bridge ownership are defined consistently with rules version 1.8.9.
- Deterministic identity, leading-zero preservation, `provider = conlog`, customer/account string rules, integer cents, non-negative values, total reconciliation, monthly syntax/continuity, recency pairing, and Firestore type rules are unchanged.

No associated executable validators or tests existed to update.

## 7. Conflict search results

Searched both repositories for case-insensitive variants of:

- `visibility optional`
- `optional ... visibility`
- `omit ... visibility`
- `visibility ... omit`
- `must omit visibility`
- `visibility may be absent`
- `master.visibility optional`
- `without master.visibility`
- `no master.visibility`
- pipeline-created documents containing no visibility

Remaining occurrences:

| Location | Classification | Explanation |
|---|---|---|
| Rules line 1821 | corrected | “Stage 06 must omit a staging visibility column” is valid; the same sentence requires Stage 08 to initialize `INVISIBLE` on strict creation |
| Rules line 2477 | valid historical reference | Records the former Stage 08 omission rule and explicitly says version 1.8.9 supersedes it |
| `SAM-WRITER-DEV-001.md` line 77 | valid historical reference | Describes the original required-versus-optional discrepancy |
| `SAM-WRITER-DEV-001.md` lines 549 and 594 | valid historical reference | Records pre-alignment Stage 08 risk; the historical report is not authoritative after this alignment |

No unresolved conflict remains in the governing rules or authoritative schema.

## 8. Validation results

| Validation | Command or method | Result |
|---|---|---|
| Schema test discovery | `rg --files C:\dev\ireps-schemas` filtered for package, validator, test, fixture, index, and version assets | PASS — no executable test framework or associated assets exist |
| Rules structure | PowerShell heading enumeration and duplicate check | PASS for edited structure; 109 headings found. One unrelated pre-existing duplicate heading (`2026-07-14 — Lesedi TEST Sales Pipeline baseline completed`) remains outside this task |
| Canonical examples | Extracted both fenced JSON examples and parsed with `ConvertFrom-Json` | PASS — 2/2 parsed; each has 10 root fields, exactly 2 master fields, and `INVISIBLE` |
| Conflict phrases | Repository-wide `rg` search in both repositories | PASS — no unresolved authoritative conflict |
| Valid canonical case | Local schema-contract assertion | PASS — valid required `INVISIBLE` accepted |
| Missing visibility | Local schema-contract assertion | PASS — rejected |
| Null visibility | Local schema-contract assertion | PASS — rejected |
| `UNKNOWN` visibility | Local schema-contract assertion | PASS — rejected |
| Root metadata | Local schema-contract assertion | PASS — rejected |
| Current DEV evidence | Existing `SAM-DATA-DEV-004` read-only profile | PASS — 34,657/34,657 have valid `VISIBLE` or `INVISIBLE`; zero noncanonical, identity mismatches, or root metadata |

No fresh Firestore scan was needed and no Firestore connection was opened.

## 9. Rules–schema–data reconciliation

| Requirement | Rules 1.8.9 | Schema 1.1.0 | Current DEV evidence | Result |
|---|---|---|---|---|
| Visibility required | Required | Required | Present on 34,657/34,657 | PASS |
| Visibility enum | `VISIBLE`, `INVISIBLE` only | Same closed enum | 34,640 `INVISIBLE`; 17 `VISIBLE` | PASS |
| Safe creation default | Stage 08 strict creation uses `INVISIBLE`; not operational derivation | Same | Compatible with every current value/type | PASS |
| Stage 08 preservation | Preserve valid existing value; never overwrite/reset/derive | Same | Current valid values are preservable | PASS |
| Bridge ownership | Owns subsequent lifecycle changes; exact two-field boundary | Same | Provenance not asserted by data profile; contract aligned | PASS |
| Metadata prohibition | Prohibited | Prohibited | Zero root metadata | PASS |
| Exact field sets | Ten root fields; `master.id` and `master.visibility` | Same | One exact root shape; both master fields present | PASS |
| Identity | Document ID = `master.id` = `meterNoNormalized` | Same | Zero mismatches | PASS |
| Cents and totals | Non-negative Firestore integers; exact reconciliation | Same | Full assessment passed | PASS |
| Monthly keys | `YYYY-MM`, contiguous approved range | Same | Syntax and per-document continuity passed | PASS |
| Recency | Governed positive/zero pairing and types | Same | Full assessment passed | PASS |

**RULES–SCHEMA–DATA RECONCILIATION: PASS**

## 10. Readiness for writer assessment

The governance gate is now aligned. Runtime implementations have not been corrected or reclassified by this task.

**SAM-WRITER-DEV-001 MAY BE RERUN**

## 11. Git status

### `C:\dev\ireps-pipeline-sales`

Pre-existing before this task:

- `rules/SALES_PIPELINE_RULES.md` was already modified with uncommitted version 1.8.8 visibility/remediation work.
- `docs/assessments/` was already untracked and contained `SAM-WRITER-DEV-001.md`.
- `scripts/tools/sales-all/` was already untracked.

Changes made by this task:

- Further modified `rules/SALES_PIPELINE_RULES.md` to version 1.8.9.
- Created `docs/assessments/sales-all/SAM-GOV-DEV-001.md`.

Final porcelain status:

```text
## main...origin/main
 M rules/SALES_PIPELINE_RULES.md
?? docs/assessments/
?? scripts/tools/sales-all/
```

### `C:\dev\ireps-schemas`

Pre-existing status was clean on branch `main`.

Changes made by this task:

- Modified `sales-all-meters/sales_all_meters.schema.md` from version 1.0.0 to 1.1.0.

Final porcelain status:

```text
## main
 M sales-all-meters/sales_all_meters.schema.md
```

## 12. Final safety confirmation

- No Stage 06, Stage 08, operational bridge, Cloud Function, web, mobile, migration, remediation, Firestore-rule, or deployment code was modified.
- No Firestore read or write was performed during this task; existing local read-only evidence was used.
- No Firestore collection was created.
- No deployment occurred.
- No commit occurred.
- No push occurred.

**Report path:** `C:\dev\ireps-pipeline-sales\docs\assessments\sales-all\SAM-GOV-DEV-001.md`
