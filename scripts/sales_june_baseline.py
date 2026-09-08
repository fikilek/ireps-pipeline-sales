"""June-only admission contract anchored to the accepted original baseline."""
from __future__ import annotations

import json
from pathlib import Path
from sales_monthly_categories import (
    APPROVED_CREATOR_SCOPE_SHA, META, category, complete_exceptions,
    ingest_workbook, validate_identities, verified_bytes,
)

JUNE_COUNT = 10216
JUNE_SOURCE_SHA = "c04fec4eeb293c7cd42b6468c763e85628a94e2655de581bd7ea1eb70c489296"
ALLOWED_PATHS = {"monthlyCategories.2026-06"} | {"metadata." + key for key in META}


def exact_ids(actual, approved):
    actual = list(actual)
    approved = list(approved)
    if (not approved or len(set(approved)) != len(approved)
            or len(actual) != len(approved) or len(set(actual)) != len(approved)
            or set(actual) != set(approved)):
        raise ValueError("June execution IDs must equal the exact approved baseline IDs")


def guard_plan(rows, plan, approved):
    exact_ids((row["masterId"] for row in rows), approved)
    decisions = [decision for wave in plan for decision in wave["decisions"]]
    exact_ids((decision["masterId"] for decision in decisions), approved)
    for decision in decisions:
        if decision["classification"] not in {"UPDATED", "UNCHANGED"}:
            raise ValueError("June permits existing-document amendments only; creates/conflicts are forbidden")
        if not set(decision.get("updates", {})) <= ALLOWED_PATHS:
            raise ValueError("June plan contains a forbidden field path")
        if "monthlyCategories.2026-06" in decision.get("updates", {}):
            category(decision["updates"]["monthlyCategories.2026-06"])


ALLOWED_PROJECTS = {"ireps2", "ireps-test", "ireps-5c3e9"}


def load_june_package(path: Path, digest: str, project_id: str):
    package = json.loads(verified_bytes({"path": str(path), "sha256": digest}, "June package"))
    if package.get("schemaVersion") == 2:
        from sales_june_analytics_baseline import load_analytics_package
        return load_analytics_package(package, digest, project_id)
    if (project_id not in ALLOWED_PROJECTS or package.get("projectId") != project_id
            or package.get("month") != "2026-06" or package.get("schemaVersion") != 1
            or package.get("operation") != "AMEND_ORIGINAL_JUNE_BASELINE"
            or package.get("lmPcode") != "ZA5241" or package.get("provider") != "contour"):
        raise ValueError("June package scope/project contract mismatch")
    baseline = package.get("baseline") or {}
    if baseline.get("sha256") != APPROVED_CREATOR_SCOPE_SHA:
        raise ValueError("June requires the independently pinned original baseline")
    original = [json.loads(line) for line in verified_bytes(baseline, "June baseline").decode("utf-8-sig").splitlines() if line.strip()]
    approved = [row["meterNoNormalized"] for row in original]
    if len(approved) != JUNE_COUNT:
        raise ValueError("Historical original baseline must contain 10,216 identities")
    exact_ids(package.get("executionIds", []), approved)
    if any(row.get("lmPcode") != "ZA5241" for row in original):
        raise ValueError("June baseline municipality mismatch")
    manifest = json.loads(verified_bytes(package.get("baselineIdManifest"), "June ID manifest"))
    exact_ids(manifest.get("members", []), approved)
    if manifest.get("baselineSha256") != APPROVED_CREATOR_SCOPE_SHA:
        raise ValueError("June ID manifest baseline mismatch")
    validate_identities(package)
    verified_bytes(package.get("attributionConfirmation"), "June attribution confirmation")
    if package.get("pipelineAttributionEvidence") is not None:
        raise ValueError("Cumulative attribution scope is not a June execution contract")
    source = package.get("source") or {}
    if source.get("sha256") != JUNE_SOURCE_SHA:
        raise ValueError("June classification source fingerprint mismatch")
    values, exceptions, aliases = ingest_workbook(source["path"], source["sha256"], source["sheet"], "2026-06", set(approved))
    exceptions = complete_exceptions(exceptions, values, approved)
    if package.get("categories") != values or package.get("exceptions") != exceptions:
        raise ValueError("June categories/exceptions differ from authoritative baseline-scoped source join")
    accepted_aliases = json.loads(verified_bytes(package.get("identityReconciliation"), "June comparison mapping"))
    if aliases != accepted_aliases:
        raise ValueError("June identity comparisons differ from accepted comparison evidence")
    snapshot = json.loads(verified_bytes(package.get("populationSnapshot"), "June snapshot candidate"))
    membership_bytes = verified_bytes(package.get("membershipEvidence"), "June membership evidence")
    membership = json.loads(membership_bytes)
    exact_ids(snapshot.get("members", []), approved)
    if (snapshot.get("schemaVersion") != 1 or snapshot.get("month") != "2026-06" or snapshot.get("lmPcode") != "ZA5241"
            or snapshot.get("provider") != "contour" or snapshot.get("previousSnapshotSha256") is not None
            or snapshot.get("replacements") != [] or snapshot.get("sourceSha256") != JUNE_SOURCE_SHA
            or snapshot.get("completeness") != {"complete": True, "evidenceSha256": package["membershipEvidence"]["sha256"]}
            or membership.get("baselineSha256") != APPROVED_CREATOR_SCOPE_SHA):
        raise ValueError("June snapshot must be the original baseline without May transitions")
    rows = [{"masterId": mid, "expected": {"master": {"id": mid}, "meterNoNormalized": mid,
            "provider": "contour", "lmPcode": "ZA5241"},
        "categoryRefresh": {"month": "2026-06", "category": values.get(mid),
            "creator": package["creator"], "actor": package["actor"],
            "pipelineAttributionConfirmed": True, "requiredHistory": {}}} for mid in sorted(approved)]
    evidence = {"governedMonth": "2026-06", "juneBaseline": baseline,
        "baselineIdManifest": package["baselineIdManifest"], "exactJuneIdSetVerified": True,
        "categoryPackageSha256": digest, "classificationSourceSha256": source["sha256"],
        "populationSnapshotSha256": package["populationSnapshot"]["sha256"],
        "categoryExceptionDocumentIds": [], "categoryExceptions": [entry for entry in exceptions if entry.get("meterId") in approved],
        "allSourceExceptions": exceptions, "monthlyCategoryPackage": {"path": str(path), "sha256": digest,
            "month": "2026-06", "intendedRecords": JUNE_COUNT, "categoryRecords": len(values), "source": source},
        "outsideJuneWritesPermitted": 0, "documentCreatesPermitted": 0,
        "snapshotFinalized": False}
    return rows, evidence, tuple(sorted(approved))
