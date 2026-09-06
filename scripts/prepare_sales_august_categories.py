"""Prepare the approved August category-only package entirely offline.

Reads pinned source/prior execution artifacts and the adversarial review's
captured DEV before-images. No credentials, Firestore client, or write executor.
"""
from __future__ import annotations

import argparse
import copy
import json
import subprocess
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from google.cloud.firestore_v1 import _helpers
from google.cloud.firestore_v1.types import document

import sales_monthly_categories as categories
import sales_pipeline_sales_all_refresh as refresh
import sales_population_artifacts as population

ROOT = Path(__file__).resolve().parents[1]
MONTH = "2026-08"
SOURCE_SHA = "2e5f58c43d8e3fc948ebd7ab969c6228bd7289dfceb5487dc69d83302727534b"
EXPECTED_CATEGORIES = {"CAT1": 32, "CAT2": 975, "CAT3": 2, "CAT4": 2291,
                       "CAT5": 655, "CAT6": 303, "CAT7": 0, "CAT8": 1369, "Normal": 4614}
DISCLOSURE = "August category acceptance does not constitute full commercial/unit reconciliation to the August analytics workbook."
ALLOWED_PATHS = {f"monthlyCategories.{MONTH}", "metadata.updatedAt",
                 "metadata.updatedByUid", "metadata.updatedByUser"}


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def evidence(path):
    return {"path": str(Path(path).resolve()), "sha256": categories.sha(path)}


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return evidence(path)


def pinned(ref, label):
    return json.loads(categories.verified_bytes(ref, label))


class CapturedDocuments:
    """Only the review's already-recorded data; cannot contact or write Firestore."""
    def __init__(self, records):
        self.records = records

    def get_all(self, refs):
        result = []
        for ref in refs:
            record = self.records.get(ref.id)
            if record is None:
                result.append(SimpleNamespace(id=ref.id, reference=ref, exists=False, update_time=None))
                continue
            native = document.Document.from_json(json.dumps(record["document"]))
            payload = _helpers.decode_dict(native.fields, None)
            result.append(SimpleNamespace(id=ref.id, reference=ref, exists=True,
                create_time=native.create_time, update_time=native.update_time,
                to_dict=lambda d=payload: copy.deepcopy(d)))
        return result

    def batch(self):
        raise RuntimeError("Offline preparation cannot construct a Firestore write batch")


NODE_SCHEMA_SWEEP = """
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
const [helperPath,inputPath,outputPath]=process.argv.slice(2);
const {validateExistingSalesAllMetersTarget:validate}=await import(pathToFileURL(helperPath).href);
function decode(v){
 if('mapValue'in v)return Object.fromEntries(Object.entries(v.mapValue.fields||{}).map(([k,x])=>[k,decode(x)]));
 if('arrayValue'in v)return(v.arrayValue.values||[]).map(decode);
 if('timestampValue'in v){const s=v.timestampValue;return {seconds:Math.floor(Date.parse(s)/1000),
   nanoseconds:Number(((s.match(/\\.(\\d+)/)||[])[1]||'').padEnd(9,'0').slice(0,9))};}
 if('integerValue'in v)return Number(v.integerValue);
 if('doubleValue'in v)return Number(v.doubleValue);
 if('nullValue'in v)return null;
 if('booleanValue'in v)return v.booleanValue;
 if('stringValue'in v)return v.stringValue;
 return Object.values(v)[0];
}
let checked=0;const failures=[],groups={};
for(const line of fs.readFileSync(inputPath,'utf8').trim().split('\\n')){
 const x=JSON.parse(line);const existing=Object.fromEntries(Object.entries(x.document.fields).map(([k,v])=>[k,decode(v)]));
 const r=validate({meterId:x.masterId,existing,sourceWriter:'AUGUST_STEP1_OFFLINE_CATEGORY_PACKAGE'});
 checked++;
 if(!r.valid){failures.push({id:x.masterId,code:r.code,paths:r.conflictingPaths});
   (groups[r.code]??=[]).push(x.masterId);}
}
fs.writeFileSync(outputPath,JSON.stringify({status:failures.length?'BLOCKED':'PASS',
 recordsChecked:checked,failures,failureGroups:groups,firestoreWrites:0},null,2)+'\\n');
console.log(JSON.stringify({schemaRecords:checked,schemaFailures:failures.length}));
"""


def prepare(args):
    started = time.monotonic()
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    review = load(args.review_dir / "pre_stage10_august_readiness.json")
    if (review["source"]["sha256"] != SOURCE_SHA or review["month"] != MONTH
            or review["projectId"] != "ireps2" or review["lmPcode"] != "ZA5241"):
        raise ValueError("Wrong approved August review/source scope")
    source = {k: review["source"][k] for k in ("path", "sha256", "sheet")}
    source["identityField"] = "CorrectedMeterNumber"
    categories.verified_bytes(source, "Approved August v2 source")
    print("[OFFLINE 1/5] Verify accepted source and finalized July evidence", flush=True)
    july_attestation_ref = evidence(args.july_finalization)
    if july_attestation_ref["sha256"] != categories.APPROVED_PREDECESSORS[MONTH]["finalizationSha256"]:
        raise ValueError("Unapproved July finalization")
    att = pinned(july_attestation_ref, "July finalization")
    july_package = pinned(att["package"], "Executed July package")
    july_snapshot = pinned(att["snapshot"], "Finalized July snapshot")
    june_history = july_package["historySources"][0]
    june_snapshot = pinned(june_history["populationSnapshot"], "Finalized June snapshot")
    june_package = pinned(june_history["categoryPackage"], "Verified June category package")
    # Reuse the reviewed governed ID rows; ingestion revalidates the corrected
    # loader against the pinned source without rebuilding commercial history.
    row_ref = review["evidenceArtifacts"]["AUGUST_IDENTITY_ROWS.json"]
    reviewed_rows = pinned(row_ref, "Reviewed August identity rows")
    ids = set(reviewed_rows)
    source_rows = dict.fromkeys(ids)
    values, exceptions, aliases = categories.ingest_workbook(
        source["path"], source["sha256"], source["sheet"], MONTH, ids,
        identity_field="CorrectedMeterNumber", source_rows=source_rows)
    counts = Counter(v["leakageCategory"].split(" - ")[0] for v in values.values())
    if (len(ids) != 10241 or set(values) != ids or exceptions or aliases
            or any(counts[k] != n for k, n in EXPECTED_CATEGORIES.items())
            or ids != set(july_snapshot["members"])):
        raise ValueError("August source differs from accepted identity/category/population facts")
    known = set(june_snapshot["members"]) | ids
    if len(known) != 10272:
        raise ValueError("Known-through-August differs from accepted evidence")
    reconciliation = write(out / "AUGUST_MEMBERSHIP_AND_SOURCE_BINDING.json", {
        "month": MONTH, "lmPcode": "ZA5241", "provider": "contour",
        "source": source, "identityField": "CorrectedMeterNumber",
        "sourceRows": {mid: {"row": row["__sourceRow"], "CorrectedMeterNumber": mid,
                       "MeterNumber": row["MeterNumber"], "PreviousMeterNumber": row["PreviousMeterNumber"]}
                       for mid, row in source_rows.items()},
        "julyActive": 10241, "augustActive": 10241, "continuing": 10241,
        "entered": [], "exited": [], "knownThroughAugust": sorted(known),
        "categoryCounts": EXPECTED_CATEGORIES, "identityExceptions": [], "categoryExceptions": [],
        "review": evidence(args.review_dir / "pre_stage10_august_readiness.json"),
        "sourceWarnings": review["evidenceArtifacts"]["SOURCE_PROFILE.json"],
        "predecessorEvidence": review["evidenceArtifacts"]["REPLACEMENT_REVIEW.json"],
        "movementReplacements": [], "commercialDifferenceDisclosure": DISCLOSURE,
        "sourceStoredUnitDifferenceRecords": 1934, "sourceMinusStoredUnits": 193400,
        "commercialReconciliationPerformed": False,
    })
    snap = population.build_snapshot(lm_pcode="ZA5241", provider="contour", month=MONTH,
        source_sha256=SOURCE_SHA, members=sorted(ids), evidence_sha256=reconciliation["sha256"],
        previous_sha256=att["snapshot"]["sha256"])
    snap.update(previousMonth="2026-07", runId=out.name, identityField="CorrectedMeterNumber",
        enteredActive=[], exitedActive=[], knownThroughAugust=sorted(known),
        status="CANDIDATE_NOT_EXECUTED_NOT_PUBLISHED", reconciliationEvidence=reconciliation)
    snapshot_path, _ = population.write_snapshot(out / "snapshot_candidate", snap)
    previous_ref = dict(att["snapshot"], finalizationAttestation=july_attestation_ref)
    package = {k: copy.deepcopy(july_package[k]) for k in (
        "actor", "actorEvidence", "creator", "creatorEvidence", "creatorScope",
        "creatorEligibleIds", "pipelineAttributionEvidence", "attributionConfirmation")}
    package.update(schemaVersion=1, projectId="ireps2", collection="sales-all-meters",
        lmPcode="ZA5241", provider="contour", month=MONTH, source=source,
        runId=out.name, executionIds=sorted(ids), categories=values, exceptions=[],
        canonicalStage06=copy.deepcopy(july_package["canonicalStage06"]),
        existingStage06=copy.deepcopy(july_package["creationStage06"]),
        previousMonth="2026-07", previousPopulationSnapshot=previous_ref,
        populationSnapshot=evidence(snapshot_path), membershipReconciliation=reconciliation,
        historySources=[june_history, {"month": "2026-07", "categoryPackage": att["package"],
                                      "populationSnapshot": previous_ref}],
        historicalCategories={mid: {
            **({"2026-06": june_package["categories"][mid]} if mid in june_package["categories"] else {}),
            "2026-07": july_package["categories"][mid]} for mid in sorted(ids)},
        approvedUpdatePaths=sorted(ALLOWED_PATHS),
        proposedCounts={"updates": 10241, "creates": 0, "deletes": 0,
                        "juneCategoryMutations": 0, "julyCategoryMutations": 0},
        executionAuthorized=False, preflightRun=False,
        commercialDifferenceDisclosure=DISCLOSURE,
        commercialReconciliationPerformed=False, sourceStoredUnitDifferenceRecords=1934,
        sourceMinusStoredUnits=193400, commercialPathDebt={
            "status": "DEFERRED_NOT_CURRENT_CATEGORY_OPERATION",
            "sparseSourceHistoriesRejectedIfRebuilt": 5762,
            "evidence": review["evidenceArtifacts"]["SOURCE_SPARSE_SCHEMA_COUNTEREXAMPLES.json"]},
        runtimeSourceHashes={str(ROOT / "scripts" / name): categories.sha(ROOT / "scripts" / name)
            for name in ("sales_monthly_categories.py", "sales_pipeline_sales_all_refresh.py",
                         "08_upload_sales_all_meters.py", "prepare_sales_august_categories.py")})
    package_path = out / "AUGUST_STEP1_CATEGORY_PACKAGE.json"
    package_ref = write(package_path, package)
    print("[OFFLINE 2/5] Admit exact August package through the existing Stage 08 loader", flush=True)
    canonical = package["canonicalStage06"]
    for key in ("input", "manifest"):
        categories.verified_bytes(canonical[key], "Previously approved canonical " + key)
    input_rows, input_evidence = refresh.load_and_validate(
        Path(canonical["input"]["path"]), Path(canonical["manifest"]["path"]))
    selected, admission = categories.load_package(package_path, package_ref["sha256"], input_rows, "ireps2")
    if ({r["masterId"] for r in selected} != ids or len(selected) != 10241
            or any("createOnly" in r or "metadataRefresh" in r or "categoryRefresh" not in r for r in selected)):
        raise ValueError("Wrong path: August must admit existing category updates only")
    write(out / "STEP2_PACKAGE_ADMISSION_OFFLINE.json", dict(admission,
        status="PASS", package=package_ref, inputEvidence=input_evidence, livePreflightRun=False))
    captures = review["evidenceArtifacts"]["CURRENT_DEV_ZA5241_READONLY.jsonl"]
    captured_bytes = categories.verified_bytes(captures, "Readiness DEV before-images")
    records = {r["masterId"]: r for line in captured_bytes.decode().splitlines()
               if line.strip() for r in [json.loads(line)]}
    if not ids <= set(records) or len(records) != 10273:
        raise ValueError("Captured DEV scope differs from accepted review")
    db = CapturedDocuments(records)
    collection = SimpleNamespace(document=lambda mid: SimpleNamespace(id=mid, path=f"sales-all-meters/{mid}"))
    stats = refresh.RefreshStats(len(selected))
    print("[OFFLINE 3/5] Classify all 10,241 captured targets; no network client", flush=True)
    plan = refresh.classify_all(db=db, collection=collection, rows=selected, stats=stats, preserved_before={})
    failures = []
    try:
        refresh.evaluate_global_gate(rows=selected, plan=plan, stats=stats)
    except Exception as exc:
        failures.append({"code": "GLOBAL_ACCOUNTING_OR_CONFLICT", "id": None, "detail": str(exc)})
    by_id = {row["masterId"]: row for row in selected}
    after_path, patches_path = out / "AUGUST_PROPOSED_AFTER_TYPED.jsonl", out / "AUGUST_PROPOSED_PATCHES_TYPED.jsonl"
    changed_roots = Counter()
    field_counts = Counter()
    june_checked = july_checked = metadata_checked = 0
    safety = Counter({k: 0 for k in (
        "juneMutations", "julyMutations", "legacyScalarMutations", "purchaseUnitMutations",
        "operationalRootMutations", "validCreatedFieldOverwrites", "outsideScopeMutations",
        "historicalCategoryConflicts", "categoryMismatches", "invalidMetadata")})
    print("[OFFLINE 4/5] Check complete after-states, history and every preserved root", flush=True)
    with after_path.open("w", encoding="utf-8") as after_stream, patches_path.open("w", encoding="utf-8") as patch_stream:
        for wave in plan:
            for decision in wave["decisions"]:
                mid = decision["masterId"]
                before = db.get_all([collection.document(mid)])[0].to_dict()
                after = copy.deepcopy(before)
                if decision["classification"] != "UPDATED":
                    failures.append({"code": "EXPECTED_EXISTING_UPDATE", "id": mid, "detail": decision.get("reason")})
                if set(decision["updates"]) != ALLOWED_PATHS:
                    failures.append({"code": "UNEXPECTED_PATCH_PATHS", "id": mid, "paths": sorted(decision["updates"])})
                for key, value in decision["updates"].items():
                    field_counts[key] += 1
                    root, field = key.split(".", 1)
                    after.setdefault(root, {})[field] = value
                for root in set(before) | set(after):
                    if before.get(root) != after.get(root):
                        changed_roots[root] += 1
                        if root not in ("monthlyCategories", "metadata"):
                            safety["operationalRootMutations"] += 1
                            failures.append({"code": "PRESERVED_ROOT_CHANGED", "id": mid, "root": root})
                for month, expected in package["historicalCategories"][mid].items():
                    if month == "2026-06": june_checked += 1
                    if month == "2026-07": july_checked += 1
                    if before.get("monthlyCategories", {}).get(month) != expected:
                        safety["historicalCategoryConflicts"] += 1
                        failures.append({"code": "HISTORY_SOURCE_CONFLICT", "id": mid, "month": month})
                    if after.get("monthlyCategories", {}).get(month) != before.get("monthlyCategories", {}).get(month):
                        safety["juneMutations" if month == "2026-06" else "julyMutations"] += 1
                if after.get("monthlyCategories", {}).get(MONTH) != values[mid]:
                    safety["categoryMismatches"] += 1
                    failures.append({"code": "AUGUST_SOURCE_MISMATCH", "id": mid})
                meta = after.get("metadata", {})
                valid_meta = (set(meta) == categories.META
                    and all(isinstance(meta.get(k), datetime) for k in ("createdAt", "updatedAt"))
                    and all(isinstance(meta.get(k), str) and meta[k].strip()
                            for k in categories.META - {"createdAt", "updatedAt"}))
                metadata_checked += 1
                if not valid_meta:
                    safety["invalidMetadata"] += 1
                    failures.append({"code": "METADATA_SHAPE_OR_TYPE", "id": mid})
                for key in ("createdAt", "createdByUid", "createdByUser"):
                    if meta.get(key) != before.get("metadata", {}).get(key):
                        safety["validCreatedFieldOverwrites"] += 1
                        failures.append({"code": "CREATED_FIELD_CHANGED", "id": mid, "field": key})
                for root in ("monthlySalesC", "monthlyTotalsC", "monthlyUnits", "totalSalesC", "totalAmountC", "totalUnits"):
                    if before.get(root) != after.get(root): safety["purchaseUnitMutations"] += 1
                for root in ("leakageCategory", "riskTier", "riskScore"):
                    if before.get(root) != after.get(root): safety["legacyScalarMutations"] += 1
                typed = json.loads(document.Document.to_json(document.Document(fields=_helpers.encode_dict(after))))
                after_stream.write(json.dumps({"masterId": mid, "document": typed}, separators=(",", ":")) + "\n")
                patch_stream.write(json.dumps(categories.encode_write_plan(decision, by_id[mid]), separators=(",", ":")) + "\n")
    outside = sorted(set(records) - ids)
    assert len(outside) == 32 and set(field_counts) <= ALLOWED_PATHS
    print("[OFFLINE 5/5] Validate all after-states with the byte-verified deployed schema", flush=True)
    backend = review["deployedCompatibility"]
    helper = ROOT.parent / "ireps-web/functions/salesAllMeters/helpers.js"
    if categories.sha(helper) != backend["deployedValidatorSha256"]:
        raise ValueError("Validator changed from reviewed deployed bytes")
    schema_path = out / "AUGUST_ALL_AFTER_STATES_SCHEMA.json"
    result = subprocess.run([str(args.node), "--input-type=module", "-", str(helper),
                             str(after_path), str(schema_path)], input=NODE_SCHEMA_SWEEP,
                            text=True, capture_output=True, timeout=30, check=False)
    (out / "AUGUST_SCHEMA_PROCESS.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        failures.append({"code": "SCHEMA_PROCESS", "id": None, "exitCode": result.returncode})
    if not schema_path.exists():
        write(schema_path, {"status": "BLOCKED", "recordsChecked": 0, "failures": [],
                            "failureGroups": {"SCHEMA_PROCESS": [result.returncode]},
                            "processLog": evidence(out / "AUGUST_SCHEMA_PROCESS.log")})
    schema_result = load(schema_path)
    failures.extend(schema_result["failures"])
    if schema_result["recordsChecked"] != 10241:
        failures.append({"code": "SCHEMA_SCOPE", "id": None})
    if (stats.updated, stats.created, stats.unchanged, stats.conflicts, stats.failed) != (10241, 0, 0, 0, 0):
        failures.append({"code": "CLASSIFICATION_COUNTS", "id": None})
    for key, count in safety.items():
        if count: failures.append({"code": key, "id": None, "count": count})
    groups = defaultdict(list)
    for fail in failures:
        groups[fail["code"]].append(fail.get("id"))
    sweep = {
        "status": "PASS" if not failures else "BLOCKED", "package": package_ref,
        "recordsChecked": len(selected), "categoriesSourceMatched": 10241 - safety["categoryMismatches"],
        "updates": stats.updated, "creates": stats.created, "unchanged": stats.unchanged, "deletes": 0,
        "conflicts": stats.conflicts, "failures": failures, "failureGroups": dict(groups),
        "metadataChecked": metadata_checked, "juneHistoryChecked": june_checked, "julyHistoryChecked": july_checked,
        "safety": dict(safety), "proposedFieldCounts": dict(field_counts), "changedRootCounts": dict(changed_roots),
        "outsideScopeExcludedIds": outside, "offlineReadWaves": stats.read_waves,
        "globalClassificationComplete": stats.global_preflight_complete,
        "globalGatePassedOffline": stats.global_preflight_gate_passed,
        "sourceBeforeImages": captures, "proposedAfterStates": evidence(after_path),
        "proposedPatches": evidence(patches_path), "schemaValidation": evidence(schema_path),
        "snapshotCandidate": evidence(snapshot_path), "previousSnapshot": previous_ref,
        "deployedCompatibilityReused": review["evidenceArtifacts"]["CURRENT_BACKEND_COMPATIBILITY.json"],
        "firestoreReadsThisStep": 0, "firestoreWrites": 0, "livePreflightRun": False,
        "commercialReconciliationPerformed": False, "commercialDifferenceDisclosure": DISCLOSURE,
        "elapsedSeconds": round(time.monotonic() - started, 3),
    }
    write(out / "AUGUST_FULL_CONTRACT_SWEEP.json", sweep)
    print(json.dumps({"status": sweep["status"], "package": package_ref,
                      "snapshot": evidence(snapshot_path), "updates": stats.updated,
                      "creates": stats.created, "failures": len(failures),
                      "elapsedSeconds": sweep["elapsedSeconds"], "firestoreWrites": 0}), flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-dir", type=Path, required=True)
    parser.add_argument("--july-finalization", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(prepare(args))
