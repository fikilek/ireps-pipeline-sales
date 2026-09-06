"""Prepare hash-bound local category release drafts from governed originals.

This command has no Firestore client. Drafts deliberately leave the execution
actor unresolved; approval and provenance must be supplied before Stage 08.
Supplier-population completeness is separate from category classification.
"""
from __future__ import annotations

import argparse
import csv
import json
from decimal import Decimal
from pathlib import Path

from sales_monthly_categories import ingest_workbook, sha, complete_exceptions
from sales_pipeline_sales_all_refresh import load_and_validate

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = [
    ("2026-06", "Sheet1", "Prepaid_Analysis_categories_30Months_Updated.xlsx", "c04fec4eeb293c7cd42b6468c763e85628a94e2655de581bd7ea1eb70c489296"),
    ("2026-07", "Prepaid_30Month_Analysis", "Prepaid_Analysis_categories_30Months_Updated_July2026.xlsx", "9eadc9f7ad5324af454a9b35d31d8037d417a2fb7ec7d1dfabfcff4af89e5f73"),
    ("2026-08", "Prepaid_30Month_Analysis", "Prepaid_Analysis_categories_30Months_Updated_August2026.xlsx", "136344f6fb426a666c7c1c98b7a5295e6710581980799b5b1e48881b96bfac0e"),
]


def write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"path": str(path), "sha256": sha(path)}


def prepare(output):
    output.mkdir(parents=True, exist_ok=True)
    source_root = ROOT / "input/endumeni_demo_sales/source_originals"
    stage06 = ROOT / "output/sales_all_meters/sales_all_meters__ZA5241__FULL__2023-12_to_2026-08.csv"
    manifest = stage06.with_suffix(".manifest.json")
    rows, stage06_evidence = load_and_validate(stage06, manifest)
    canonical_ids = {r["masterId"] for r in rows}
    baseline = ROOT.parent / "ireps-web/docs/reports/demo-sales-migration-cleaning/DEMO_SALES_MIGRATION_CLEAN_20260808T010917Z/02_CLEAN_PIPELINE_INPUT.jsonl"
    if sha(baseline) != "41cd129e19c7c4a5a1a0900e702df8025910af0a9dec4fb1ccf8725f1c0646bb":
        raise ValueError("Approved baseline hash changed")
    baseline_records = [json.loads(line) for line in baseline.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    baseline_ids = sorted(str(r.get("meterNoNormalized") or r.get("meterNo")) for r in baseline_records)
    if len(baseline_ids) != 10216 or not set(baseline_ids) <= canonical_ids:
        raise ValueError("Baseline identity contract mismatch")
    creator_path = ROOT.parent / "ireps-web/functions/scripts/ireps2-users-20260621-2233.json"
    creator_evidence = {"path": str(creator_path), "sha256": sha(creator_path)}
    inventory = []
    all_categories = {}
    history_sources = []
    for month, sheet, filename, source_sha in REGISTRY:
        path = source_root / "classification" / month / filename
        values, exceptions, reconciled = ingest_workbook(path, source_sha, sheet, month, canonical_ids)
        exceptions = complete_exceptions(exceptions, values, canonical_ids)
        all_categories[month] = values
        source = {"path": str(path), "sha256": source_sha, "sheet": sheet,
            "categoryColumn": "Leakage_Category" if month == "2026-06" else {"2026-07":"July_2026_Category","2026-08":"August_2026_Category"}[month],
            "riskTierColumn": "Risk_Tier", "riskScoreColumn": "Risk_Score"}
        package = {"schemaVersion": 1, "projectId": "ireps2", "month": month,
            "source": source, "creator": {"uid": "fXBACUfMzybcqC0AbeNeyYyTeRu1", "user": "Fikile Kentane"},
            "creatorEvidence": creator_evidence, "creatorEligibleIds": baseline_ids,
            "creatorScope": {"path": str(baseline), "sha256": sha(baseline)},
            "historySources": list(history_sources),
            "historicalCategories": {meter: {previous: cats[meter] for previous, cats in all_categories.items()
                if previous < month and meter in cats} for meter in canonical_ids},
            "actor": None, "actorEvidence": None, "categories": values,
            "exceptions": exceptions, "populationSnapshot": None,
            "readiness": "DRAFT_BLOCKED_ACTOR_IDENTITY_RECONCILIATION_AND_LIVE_PREFLIGHT"}
        identity = write(output / f"identity_reconciliation__{month}.candidate.json", reconciled)
        package["identityReconciliation"] = identity
        draft = write(output / f"category_package__{month}.draft.json", package)
        history_sources.append(dict(source, month=month))
        exception_file = write(output / f"category_exceptions__{month}.json", exceptions)
        inventory.append({"month": month, "source": source, "categoryCount": len(values),
            "exceptionCount": len(exceptions), "leadingZeroComparisons": len(reconciled),
            "package": draft, "identityCandidate": identity, "exceptions": exception_file,
            "requiredReadWaves": (len(canonical_ids)+399)//400, "scopeRecordCount": len(canonical_ids),
            "approvedWritePaths": [f"monthlyCategories.{month}", "metadata.updatedAt", "metadata.updatedByUid", "metadata.updatedByUser"],
            "conditionalMissingMetadataPaths": ["metadata.createdAt", "metadata.createdByUid", "metadata.createdByUser"]})
        print(f"{month}: categories={len(values)} exceptions={len(exceptions)} leadingZeroComparisons={len(reconciled)}", flush=True)
    deltas = []
    for previous, current in (("2026-06", "2026-07"), ("2026-07", "2026-08")):
        old, new = all_categories[previous], all_categories[current]
        common = set(old) & set(new)
        deltas.append({"previousMonth": previous, "month": current,
            "commonClassifiedIdentities": len(common),
            "differentSuppliedCategoryTriple": sum(old[k] != new[k] for k in common),
            "onlyPreviousClassified": len(set(old)-set(new)), "onlyCurrentClassified": len(set(new)-set(old)),
            "populationMovementInference": False})
    suppliers = []
    for folder, filename, expected, terminal in [
        ("2026-07-29", "END 2026-07-29.xlsx", "c0aacbcf86f7fff3f2dee79f3d2be8104e29cd4e6bfb8539bdf222d410070f81", "2026-06"),
        ("2026-09-02", "END20260902.xlsx", "4954cde993de9b435e3da11a24324868abb1bc1421b86f2d59f4bb45ddd484e2", "2026-08")]:
        path = source_root / "supplier_sales" / folder / filename
        if sha(path) != expected:
            raise ValueError("Supplier original hash changed")
        suppliers.append({"path": str(path), "sha256": expected, "terminalSalesMonth": terminal,
            "membershipCompleteness": "requires governed attestation; purchase totals alone are insufficient"})
    stage06_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    cumulative_evidence = stage06_manifest["sourceContract"]["commercialSource"]
    cumulative_path = Path(cumulative_evidence["path"])
    if sha(cumulative_path) != cumulative_evidence["sha256"]:
        raise ValueError("Current cumulative commercial source hash changed")
    cumulative = {str(row.get("meterNoNormalized") or row.get("meterNo")): row
        for row in (json.loads(line) for line in cumulative_path.read_text(encoding="utf-8-sig").splitlines() if line.strip())}
    old_by_id = {str(row.get("meterNoNormalized") or row.get("meterNo")): row for row in baseline_records}
    if set(old_by_id) - set(cumulative):
        raise ValueError("Current commercial source lost baseline identities")
    new_ids = sorted(set(cumulative) - set(old_by_id))
    historical_months = sorted({month for row in baseline_records for month in row.get("monthlySalesC", {})})
    sales_deltas = []
    for month in historical_months:
        item = {"month": month, "existingIdentitySalesDeltaC": 0, "existingIdentityUnitsDelta": "0"}
        for field, label in (("monthlySalesC", "SalesC"), ("monthlyUnits", "Units")):
            old_total = sum(Decimal(str(row.get(field, {}).get(month, 0))) for row in baseline_records)
            existing_total = sum(Decimal(str(cumulative[meter].get(field, {}).get(month, 0))) for meter in old_by_id)
            new_total = sum(Decimal(str(cumulative[meter].get(field, {}).get(month, 0))) for meter in new_ids)
            mismatches = sum(Decimal(str(old_by_id[meter].get(field, {}).get(month, 0))) !=
                Decimal(str(cumulative[meter].get(field, {}).get(month, 0))) for meter in old_by_id)
            item[field] = {"baselineTotal": str(old_total), "currentExistingTotal": str(existing_total),
                "existingIdentityDelta": str(existing_total-old_total), "changedExistingIdentityCount": mismatches,
                "newIdentityHistoricalTotal": str(new_total), "currentCumulativeTotal": str(existing_total+new_total)}
            if mismatches:
                raise ValueError("Existing identity historical Sales/units changed")
        sales_deltas.append(item)
    historical_artifact = write(output / "historical_sales_deltas.json", {
        "baseline": {"path": str(baseline), "sha256": sha(baseline), "records": len(old_by_id)},
        "currentCumulative": cumulative_evidence, "newIdentityCount": len(new_ids),
        "newIdentities": new_ids, "months": sales_deltas,
        "meaning": "Existing identity history preserved; appended supplier identities can carry authoritative earlier-month sales."})
    release = {"status": "PREPARED_NOT_RELEASE_READY", "projectId": "ireps2",
        "collection": "sales-all-meters", "firestoreAccess": False, "firestoreWrites": 0,
        "stage06Evidence": stage06_evidence, "categoryMonths": inventory,
        "categoryComparison": deltas, "historicalSalesMutationPlanned": False,
        "historicalSalesDeltas": historical_artifact,
        "legacyScalarMutationPlanned": False, "supplierSources": suppliers,
        "blockers": ["DEV TLS authenticated read", "migration actor evidence and approval",
            "prior Stage0 identity reconciliation approval for candidate comparisons",
            "July distinct supplier membership evidence; never reuse August membership as July",
            "supplier membership completeness attestation before snapshot publication",
            "first-capture evidence must be verified against snapshot.create_time before missing metadata fill",
            "nonbaseline identity creation actor provenance when existing metadata missing",
            "exact live before-images/update-time tokens and live full-run preflight"],
        "recovery": {"status": "PREPARED_NOT_CAPTURED", "scope": "exact intended document IDs only",
            "requiredBeforeWrite": ["hash-bound typed before-images including absent markers for approved paths",
                "document create_time and update_time", "frozen complete global plan", "report SHA and source hashes"],
            "restorePolicy": "Restore only own changed paths when current update_time matches recorded post-write version; otherwise exception. Never broad overwrite or automatic rollback.",
            "publicationPolicy": "No month published until full writes and preservation verification succeed"},
        "executionOrder": ["2026-06", "2026-07", "2026-08"]}
    result = write(output / "pipeline_release_preparation.json", release)
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    prepare(parser.parse_args().output.resolve())
