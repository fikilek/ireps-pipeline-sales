# Sales Pipeline TEST Initial Fast Assessment — 2026-08-09

## Scope

Static assessment only. The ZIP was inspected without installation; no pipeline or Firestore operation ran. Its three proposed files are byte-for-byte identical (SHA-256 equality) to current `scripts/07_upload_meter_master_v3.py`, `scripts/08_upload_sales_all_meters.py`, and `scripts/05_08_run_sales_pipeline_test_initial.py`. Installing it over this checkout would make no content change.

## Findings

1. **Refresh isolation — PASS.** TEST INITIAL LOAD USES REFRESH: **NO**. The orchestrator always emits `--mode initial-load` (`05_08...py:169-198`) and invokes only Stages 08 and 07 (`:273-338`). It does not invoke the DEV orchestrator, visibility reconciliation, or monthly pathways. Stage 08's refresh import (`08...py:1217-1233`) and Stage 07's transaction refresh path (`07...py:1726-1745`) are unreachable with that fixed mode.

2. **Reads — PASS.** SERIAL 10,216-DOC READ LOOP PRESENT: **NO** in the reachable path. BATCHED GET_ALL PRESENT: **YES**. Both uploaders chunk IDs and call `db.get_all(refs)` (`07:921-965`; `08:766-834`). READ BATCH SIZE: **400**. Expected waves are 26 per complete input pass. The orchestrator performs a no-write preflight, the create invocation repeats the conflict gate, then full verification: 78 read waves per collection. Other `.stream()`, sample `.get()`, and transaction reads are confined to create-only/resume/refresh branches.

3. **Writes — PASS.** SERIAL PER-DOCUMENT TRANSACTIONS PRESENT: **NO** in initial-load. BATCH.CREATE USED: **YES** (`07:981-1001`; `08:787-807`). WRITE BATCH SIZE: **400**, below Firestore's 500-write limit. Expected write waves: Sales All **26**; Meter Master **26**.

4. **Meter Master scope — PASS.** WHOLE-COLLECTION EMPTY REQUIREMENT REMOVED FOR TEST INITIAL LOAD: **YES**. The whole-collection gate remains only under legacy `create-only` (`07:1653-1661`). Initial-load checks only incoming IDs (`07:1595-1614`, `:1662-1677`). Any matching existing ID blocks before that invocation writes. Unrelated national documents neither block nor enter a batch, and `batch.create` does not modify them.

5. **Sales All safety — PASS.** Initial-load checks incoming IDs only and blocks on any existing target (`08:1325-1349`, `:1360-1375`). It uses `batch.create`, so a race also fails rather than overwriting. No update, merge, or delete is reachable. The three reachable files contain no `demo_sales_meters` access and invoke no DEV path.

6. **Project safety — PASS.** The orchestrator fixes `TEST_PROJECT = "ireps-test"`, exposes no project option, passes it as project and confirmation, and verifies service-account `project_id` before launching children (`05_08:36,72-82,169-198,208-229`). Both children independently verify confirmation and credential project before constructing the explicitly-project-scoped client (`07:1441-1467,1524-1533`; `08:1167-1193,1262-1268,1320-1323`). PROJECT HARD GATE: **PASS**.

7. **Verification — PASS.** FULL SALES ALL VERIFY: **YES**. FULL METER MASTER VERIFY: **YES**. Method: full input scope in 400-ID `get_all` chunks (`07:942-978`; `08:809-848`). SERIAL VERIFY LOOP PRESENT: **NO**; per-ID loops inspect already-returned snapshots without issuing per-ID reads.

8. **Observability — PASS.** The orchestrator logs timestamped pipeline/stage starts, PASS/FAIL, durations, overall elapsed, final outcome, and total runtime (`05_08:108-166,237-271,340-388`). Uploaders log timestamp, processed/total, elapsed, rate, and ETA after each batch (`07:906-918`; `08:750-763`). Committed batch totals are recorded.

9. **Report paths — PASS.** The orchestrator creates the run directory and all atomic/log writers create parents (`05_08:101-113,231-235`). Uploaders also create report parents (`07:1427-1437`; `08:1154-1164`). No visibility report is invoked. REPORT PATH SAFETY: **PASS**.

10. **Network-operation shape and runtime.**

| Collection | Preflight reads | Execution gate reads | Writes | Verification reads | Approx. batch ops |
|---|---:|---:|---:|---:|---:|
| Sales All | 26 | 26 | 26 | 26 | 104 |
| Meter Master | 26 | 26 | 26 | 26 | 104 |
| Total | 52 | 52 | 52 | 52 | 208 |

LIKELY UNDER 2 HOURS: **YES**. About 208 bounded sequential batch operations replace tens of thousands of sequential remote calls. This is a static estimate, not a measured guarantee; actual duration depends on network/Firestore latency, retries, and payload size.

11. **Regression/contract — PASS relative to current source.** Because the ZIP is byte-identical to current source, it introduces no change to Meter Master shape, Sales All shape, Timestamp semantics, refs ownership, visibility rules, monthly refresh semantics, or DEV behavior. Canonical builders are reused; Meter Master creates retain one UTC `datetime` for `metadata.createdAt`/`updatedAt` (`07:602-605`), and Sales All creation retains `master.visibility = INVISIBLE`.

ZIP SAFE TO INSTALL:
YES

REFRESH USED IN TEST INITIAL LOAD:
NO

SERIAL 10,216 READ LOOP:
NO

SERIAL 10,216 TRANSACTION LOOP:
NO

BATCH READ SIZE:
400

BATCH WRITE SIZE:
400

FULL INPUT-SCOPE VERIFICATION:
YES

PROJECT HARD GATE:
PASS

REPORT/TIMESTAMP SUPPORT:
PASS

LIKELY TO FINISH UNDER 2 HOURS:
YES

BLOCKERS BEFORE INSTALL:
none

FILES CODEX MODIFIED:
0
