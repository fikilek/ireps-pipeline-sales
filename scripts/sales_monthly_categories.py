"""Exact-month category ingestion and immutable refresh contract; no network access.

Category membership never creates a Sales identity. The Stage 08 global gate
classifies these patches before its existing preconditioned batch executor runs.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import re
from decimal import Decimal
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
FIELDS = {"leakageCategory", "riskTier", "riskScore"}
META = {"createdAt", "createdByUid", "createdByUser", "updatedAt", "updatedByUid", "updatedByUser"}
APPROVED_CREATOR = {"uid": "fXBACUfMzybcqC0AbeNeyYyTeRu1", "user": "Fikile Kentane"}
APPROVED_CREATOR_SCOPE_SHA = "41cd129e19c7c4a5a1a0900e702df8025910af0a9dec4fb1ccf8725f1c0646bb"
# Explicit approved transition, not a population/count inference or a movable
# "latest snapshot" pointer. Historical June/July packages remain immutable.
APPROVED_PREDECESSORS = {
    "2026-08": {
        "month": "2026-07",
        "snapshotSha256": "708520a602790da3c52bb52f55cdb725a2c307fb442618515482ea3ed77d4cb2",
        "finalizationSha256": "f1d48ced02bfe0ba15b87cd98cab558c1b5834702cec13bde02afa72f812f5dd",
    }
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verified_bytes(evidence, label):
    if not isinstance(evidence, dict) or not evidence.get("path"):
        raise ValueError(f"{label} evidence required")
    raw = Path(evidence["path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != evidence.get("sha256"):
        raise ValueError(f"{label} evidence hash mismatch")
    return raw


def validate_identities(contract):
    if contract.get("creator") != APPROVED_CREATOR:
        raise ValueError("Original baseline creator must be the approved SPU identity")
    if (contract.get("creatorScope") or {}).get("sha256") != APPROVED_CREATOR_SCOPE_SHA:
        raise ValueError("Original creator scope must match the approved immutable baseline")
    for name in ("creator", "actor"):
        identity = contract.get(name) or {}
        if set(identity) != {"uid", "user"} or not all(isinstance(v, str) and v.strip() for v in identity.values()):
            raise ValueError(f"Explicit authoritative {name} identity required")
        users = json.loads(verified_bytes(contract.get(name + "Evidence"), name).decode("utf-8-sig"))
        record = users.get("users/" + identity["uid"], {})
        if (record.get("uid") != identity["uid"] or
                record.get("profile", {}).get("displayName") != identity["user"] or
                record.get("__exists__") is not True):
            raise ValueError(f"{name} does not match authoritative user evidence")
    raw = verified_bytes(contract.get("creatorScope"), "creator scope")
    records = [json.loads(line) for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
    scope = [str(row.get("meterNoNormalized") or row.get("meterNo")) for row in records]
    if len(scope) != len(set(scope)) or set(contract.get("creatorEligibleIds", [])) != set(scope):
        raise ValueError("Creator eligibility differs from authoritative baseline identities")


def complete_exceptions(exceptions, values, canonical_ids):
    result = list(exceptions)
    covered = {entry.get("meterId") for entry in result}
    result.extend({"meterId": meter, "reason": "No authoritative exact-month category source row"}
        for meter in sorted(set(canonical_ids) - set(values) - covered))
    return result


def creator_eligible_ids(contract, project_id):
    """Extend baseline attribution only through an explicit, hash-bound policy."""
    eligible = set(contract.get("creatorEligibleIds", []))
    evidence = contract.get("pipelineAttributionEvidence")
    if evidence is None:
        return eligible
    policy = json.loads(verified_bytes(evidence, "pipeline attribution").decode("utf-8-sig"))
    if (policy.get("schemaVersion") != 1 or policy.get("projectId") != project_id
            or policy.get("authorityType") != "USER_CONFIRMED_PIPELINE_ATTRIBUTION"
            or policy.get("identity") != contract.get("creator")
            or policy.get("identity") != contract.get("actor")):
        raise ValueError("Pipeline attribution policy identity/project mismatch")
    verified_bytes(policy.get("confirmation"), "user attribution confirmation")
    source = verified_bytes(policy.get("sourceScope"), "pipeline attribution scope")
    scope = [row["masterId"] for row in csv.DictReader(io.StringIO(source.decode("utf-8-sig")))]
    declared = policy.get("existingDocumentIds", [])
    if (not scope or len(scope) != len(set(scope)) or len(declared) != len(set(declared))
            or set(declared) != set(scope) or not eligible <= set(scope)):
        raise ValueError("Pipeline attribution scope mismatch")
    return set(scope)


def category(value):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("Category must contain exactly leakageCategory/riskTier/riskScore")
    if any(not isinstance(value[k], str) or not value[k].strip() for k in ("leakageCategory", "riskTier")):
        raise ValueError("Category and risk tier must be supplied strings")
    if value["leakageCategory"].strip().lower().startswith(("nav", "not available")):
        raise ValueError("Unavailable categories must remain absent")
    if type(value["riskScore"]) is not int or value["riskScore"] < 0:
        raise ValueError("riskScore must be a nonnegative integer")
    return value


def validate_history(history):
    if not isinstance(history, dict):
        raise ValueError("monthlyCategories must be a map")
    for key, value in history.items():
        if not MONTH.fullmatch(key):
            raise ValueError("Existing category month is malformed")
        category(value)
    return history


def required_history_months(month):
    if not MONTH.fullmatch(month) or month < "2026-06":
        raise ValueError("Category target must be at or after the governed June 2026 baseline")
    year, number = map(int, month.split("-"))
    start = 2026 * 12 + 5
    end = year * 12 + number - 1
    return [f"{index // 12:04d}-{index % 12 + 1:02d}" for index in range(start, end)]


def append_to_commercial(records, month, values):
    """Attach one month to eligible commercial identities without altering history."""
    if not MONTH.fullmatch(month):
        raise ValueError("Invalid category month")
    by_id = {str(r.get("meterNoNormalized") or r.get("meterNo")): r for r in records}
    if set(values) - set(by_id):
        raise ValueError("Category source cannot create commercial identities")
    result = copy.deepcopy(records)
    for record in result:
        meter = str(record.get("meterNoNormalized") or record.get("meterNo"))
        history = validate_history(record.get("monthlyCategories", {}))
        if meter not in values:
            continue
        value = category(values[meter])
        if month in history and history[month] != value:
            raise ValueError(f"Historical category conflict for {meter} at {month}")
        record["monthlyCategories"] = dict(history, **{month: value})
    return result


ALLOWED_PROJECTS = {"ireps2", "ireps-test", "ireps-5c3e9"}


def validate_predecessor(package, snapshot):
    """Admit a governed transition only against its finalized execution evidence."""
    approved = APPROVED_PREDECESSORS.get(package["month"])
    if approved is None:
        return None
    predecessor = package.get("previousPopulationSnapshot") or {}
    if (package.get("previousMonth") != approved["month"]
            or snapshot.get("previousMonth") != approved["month"]
            or snapshot.get("previousSnapshotSha256") != approved["snapshotSha256"]
            or predecessor.get("sha256") != approved["snapshotSha256"]):
        raise ValueError("Approved immediate predecessor month/snapshot mismatch")
    previous = json.loads(verified_bytes(predecessor, "Finalized predecessor snapshot"))
    attestation_ref = predecessor.get("finalizationAttestation") or {}
    if attestation_ref.get("sha256") != approved["finalizationSha256"]:
        raise ValueError("Approved predecessor finalization evidence required")
    attestation = json.loads(verified_bytes(attestation_ref, "Predecessor finalization"))
    if (previous.get("month") != approved["month"]
            or previous.get("schemaVersion") != 1
            or previous.get("completeness", {}).get("complete") is not True
            or previous.get("lmPcode") != package.get("lmPcode")
            or previous.get("provider") != package.get("provider")
            or attestation.get("status") != "FINALIZED_LOCAL_AFTER_ACTUAL_WRITE_AND_FULL_READBACK"
            or attestation.get("month") != approved["month"]
            or attestation.get("projectId") not in ALLOWED_PROJECTS
            or attestation.get("collection") != "sales-all-meters"
            or attestation.get("lmPcode") != package.get("lmPcode")
            or attestation.get("provider") != package.get("provider")
            or attestation.get("memberCount") != len(previous.get("members", []))
            or attestation.get("snapshot", {}).get("sha256") != approved["snapshotSha256"]):
        raise ValueError("Predecessor is not the finalized population for this scope")
    # Hash the actual final blob, package and write/read-back evidence rather
    # than accepting a boolean 'finalized' field supplied by a new package.
    verified_bytes(attestation.get("snapshot"), "Finalized predecessor blob")
    prior_package = json.loads(verified_bytes(attestation.get("package"), "Executed predecessor package"))
    report = json.loads(verified_bytes(attestation.get("stage08Report"), "Predecessor execution report"))
    verification = json.loads(verified_bytes(attestation.get("verification"), "Predecessor full readback"))
    if (prior_package.get("month") != approved["month"]
            or prior_package.get("projectId") not in ALLOWED_PROJECTS
            or prior_package.get("populationSnapshot", {}).get("sha256") != approved["snapshotSha256"]
            or set(prior_package.get("categories", {})) != set(previous["members"])
            or report.get("projectId") not in ALLOWED_PROJECTS
            or report.get("collection") != "sales-all-meters"
            or report.get("status") != "PASS" or report.get("result") != "REFRESH_VERIFIED"
            or report.get("preflightOnly") is not False
            or report.get("verification", {}).get("status") != "PASS"
            or report.get("verification", {}).get("documentsVerified") != len(previous["members"])
            or verification.get("status") != "PASS"):
        raise ValueError("Predecessor execution/full-readback evidence mismatch")
    expected_history = {entry["month"]: entry for entry in prior_package.get("historySources", [])}
    expected_history[approved["month"]] = {
        "categoryPackage": attestation["package"], "populationSnapshot": predecessor,
    }
    history_sources = package.get("historySources", [])
    if (len(history_sources) != len(expected_history)
            or {entry.get("month") for entry in history_sources} != set(expected_history)):
        raise ValueError("Executed predecessor requires the complete immutable history bindings")
    for entry in history_sources:
        expected = expected_history[entry["month"]]
        if any(entry.get(key, {}).get("sha256") != expected.get(key, {}).get("sha256")
               for key in ("categoryPackage", "populationSnapshot")):
            raise ValueError("Immutable history differs from executed predecessor evidence")
    return prior_package


def load_package(path, expected_sha, rows, project_id):
    package = json.loads(verified_bytes({"path": path, "sha256": expected_sha}, "Category package").decode("utf-8-sig"))
    month = package.get("month", "")
    if package.get("schemaVersion") != 1 or not MONTH.fullmatch(month):
        raise ValueError("Category package schema/month invalid")
    if package.get("projectId") != project_id:
        raise ValueError("Category package project mismatch")
    validate_identities(package)
    by_id = {r["masterId"]: r for r in rows}
    if len(by_id) != len(rows):
        raise ValueError("Duplicate input identity")
    execution_ids = package.get("executionIds")
    creation_ids = set()
    predecessor_package = None
    if month in APPROVED_PREDECESSORS:
        snapshot = json.loads(verified_bytes(package.get("populationSnapshot"), "Execution population"))
        predecessor_package = validate_predecessor(package, snapshot)
        if execution_ids is None or package.get("creationStage06") is not None:
            raise ValueError("Approved August operation requires exact existing-document scope")
    if execution_ids is not None:
        if (not execution_ids or len(execution_ids) != len(set(execution_ids))
                or any(not isinstance(mid, str) or not re.fullmatch(r"[A-Z0-9]+", mid) for mid in execution_ids)):
            raise ValueError("Invalid exact execution identity set")
        snapshot = json.loads(verified_bytes(package.get("populationSnapshot"), "Execution population"))
        if (set(snapshot.get("members", [])) != set(execution_ids)
                or len(snapshot.get("members", [])) != len(execution_ids)):
            raise ValueError("Execution scope differs from population snapshot")
        existing = package.get("existingStage06")
        if existing is not None:
            # Reuse the previously admitted canonical rows as evidence only.
            # They must never enter the creation branch again.
            approved_rows = (predecessor_package or {}).get("creationStage06") or {}
            if (not approved_rows or any(existing.get(key) != approved_rows.get(key)
                    for key in ("ids", "input", "manifest", "sourceBinding"))):
                raise ValueError("Existing Stage06 supplement differs from executed predecessor evidence")
            from sales_pipeline_sales_all_refresh import load_and_validate
            for key in ("input", "manifest", "sourceBinding"):
                verified_bytes(existing.get(key), "Existing Stage06 " + key)
            supplemental, _ = load_and_validate(Path(existing["input"]["path"]), Path(existing["manifest"]["path"]))
            extra_ids = [row["masterId"] for row in supplemental]
            if (len(extra_ids) != len(set(extra_ids)) or len(existing["ids"]) != len(set(existing["ids"]))
                    or set(extra_ids) != set(existing["ids"]) or not set(extra_ids) <= set(execution_ids)
                    or set(extra_ids) & set(by_id)):
                raise ValueError("Existing Stage06 supplement identity scope mismatch")
            by_id.update({row["masterId"]: row for row in supplemental})
        creation = package.get("creationStage06")
        if creation is not None:
            from sales_pipeline_sales_all_refresh import load_and_validate
            verified_bytes(creation.get("input"), "Creation Stage06 CSV")
            verified_bytes(creation.get("manifest"), "Creation Stage06 manifest")
            new_rows, _ = load_and_validate(Path(creation["input"]["path"]), Path(creation["manifest"]["path"]))
            creation_ids = {r["masterId"] for r in new_rows}
            if (creation_ids != set(creation.get("ids", [])) or not creation_ids <= set(execution_ids)
                    or creation_ids & set(by_id)):
                raise ValueError("Creation Stage06 identity scope mismatch")
            by_id.update({r["masterId"]: r for r in new_rows})
        if not set(execution_ids) <= set(by_id):
            raise ValueError("Execution identity has no canonical Stage06 input")
        by_id = {mid: by_id[mid] for mid in execution_ids}
    elif package.get("creationStage06") is not None or package.get("existingStage06") is not None:
        raise ValueError("Supplemental Stage06 rows require an exact execution population")
    creator_ids = creator_eligible_ids(package, project_id)
    if execution_ids is None and not creator_ids <= set(by_id):
        raise ValueError("Creator attribution contains identities outside input scope")
    records = package.get("categories")
    if not isinstance(records, dict) or not records:
        raise ValueError("Category package has no intended records")
    if set(records) - set(by_id):
        raise ValueError("Category package contains ineligible Sales identities")
    source = package.get("source") or {}
    if execution_ids is not None and (snapshot.get("sourceSha256") != source.get("sha256")
            or package.get("lmPcode") != snapshot.get("lmPcode")
            or package.get("provider") != snapshot.get("provider")):
        raise ValueError("Execution population source/scope mismatch")
    creation_source_rows = {mid: None for mid in creation_ids}
    authoritative, exceptions, aliases = ingest_workbook(source["path"], source["sha256"], source["sheet"], month, set(by_id), identity_field=source.get("identityField", "MeterNumber"), source_rows=creation_source_rows)
    if (execution_ids is not None and source.get("identityField") == "CorrectedMeterNumber"
            and (exceptions or set(authoritative) != set(by_id))):
        raise ValueError("Corrected source identity/category exceptions prevent exact-scope admission")
    if records != authoritative:
        raise ValueError("Category package differs from authoritative exact-month workbook")
    if package.get("exceptions", []) != complete_exceptions(exceptions, authoritative, by_id):
        raise ValueError("Category exceptions differ from authoritative source reconciliation")
    if aliases:
        approved = json.loads(verified_bytes(package.get("identityReconciliation"), "identity reconciliation"))
        if approved != aliases:
            raise ValueError("Category identity reconciliation differs from approved evidence")
    histories = {meter: {} for meter in by_id}
    history_sources = package.get("historySources", [])
    source_months = [entry.get("month") for entry in history_sources]
    if len(source_months) != len(set(source_months)):
        raise ValueError("Duplicate historical category source month")
    if sorted(source_months) != required_history_months(month):
        raise ValueError("Complete June-to-target category history sources required")
    if month != "2026-06" and not history_sources:
        raise ValueError("Prior governed category source history required")
    for history_source in history_sources:
        historical_month = history_source["month"]
        if historical_month >= month:
            raise ValueError("Historical category source must precede target month")
        if execution_ids is not None and history_source.get("categoryPackage"):
            prior = json.loads(verified_bytes(history_source["categoryPackage"], "Verified historical package"))
            prior_snapshot = json.loads(verified_bytes(history_source.get("populationSnapshot"), "Verified historical population"))
            if (prior.get("month") != historical_month or prior.get("projectId") not in ALLOWED_PROJECTS
                    or prior_snapshot.get("month") != historical_month
                    or set(prior.get("categories", {})) != set(prior_snapshot.get("members", []))
                    or prior.get("populationSnapshot", {}).get("sha256") != history_source["populationSnapshot"]["sha256"]):
                raise ValueError("Historical package/population mismatch")
            previous = prior["categories"]
        else:
            previous, _, _ = ingest_workbook(history_source["path"], history_source["sha256"],
                history_source["sheet"], historical_month, set(by_id))
        for meter in by_id:
            if meter in previous:
                histories[meter][historical_month] = previous[meter]
    if package.get("historicalCategories", {meter: {} for meter in by_id}) != histories:
        raise ValueError("Historical category package differs from authoritative prior sources")
    selected = []
    for meter in sorted(by_id):
        value = records.get(meter)
        row = dict(by_id[meter])
        if value is not None:
            category(value)
        if meter in creation_ids:
            expected = copy.deepcopy(row["expected"])
            if (histories[meter] or value is None
                    or expected.get("monthlyCategories") != {month: value}
                    or expected.get("salesPeriodTo") != month
                    or expected.get("lmPcode") != package.get("lmPcode")
                    or expected.get("provider") != package.get("provider")):
                raise ValueError("Creation payload category/history/scope mismatch")
            # Stage06 supplies the canonical roots and validates commercial totals.
            # Blank source cells do not become asserted zero purchase history on
            # a new identity: bind each populated cell to that same source row.
            supplied = creation_source_rows[meter]
            if supplied is None:
                raise ValueError("Creation source row unavailable")
            if expected.get("sourceEndRow") != supplied["__sourceRow"]:
                raise ValueError("Creation Stage06 source row mismatch")
            expected["sourceFileName"] = Path(source["path"]).name
            expected["sourceRow"] = supplied["__sourceRow"]
            for field, suffix, scale in (("monthlySalesC", "", 100), ("monthlyUnits", ".1", 1)):
                available = {}
                for ym, canonical_value in expected[field].items():
                    raw_value = supplied.get(ym + suffix)
                    if raw_value is None or raw_value == "":
                        if canonical_value != 0:
                            raise ValueError("Creation history contains values absent from its source row")
                        continue
                    value = Decimal(str(raw_value)) * scale
                    if not value.is_finite() or value < 0 or value != Decimal(str(canonical_value)):
                        raise ValueError("Creation history differs from authoritative source row")
                    available[ym] = canonical_value
                expected[field] = available
            expected["monthlyTotalsC"] = dict(expected["monthlySalesC"])
            # Source-backed no-history members retain empty maps. Their sums
            # must still reconcile; never invent a zero month for admission.
            if (sum(expected["monthlySalesC"].values()) != expected["totalSalesC"]
                    or sum(Decimal(str(v)) for v in expected["monthlyUnits"].values()) != Decimal(str(expected["totalUnits"]))):
                raise ValueError("Creation available-history totals mismatch")
            row["expected"] = expected
            row["createOnly"] = True
            row["metadataRefresh"] = {"actor": package["actor"], "creator": package["actor"]}
            selected.append(row)
            continue
        row["categoryRefresh"] = {"month": month, "category": value,
            "creator": package["creator"] if meter in creator_ids else None,
            "pipelineAttributionConfirmed": package.get("pipelineAttributionEvidence") is not None,
            "actor": package["actor"], "requiredHistory": histories[meter]}
        if month in APPROVED_PREDECESSORS:
            row["categoryRefresh"]["requireCompleteMetadata"] = True
        selected.append(row)
    evidence = {"path": str(path), "sha256": expected_sha, "month": month,
        "intendedRecords": len(selected), "categoryRecords": len(records), "source": package["source"],
        "categoryOnly": not bool(creation_ids), "identityCreatesPermitted": bool(creation_ids),
        "creationIds": sorted(creation_ids), "exactExecutionIds": sorted(by_id)}
    evidence["categoryExceptions"] = [
        {"meterId": entry["meterId"], "reason": entry["reason"]}
        for entry in package.get("exceptions", [])
        if entry.get("meterId") in by_id and isinstance(entry.get("reason"), str)
        and entry["meterId"] not in records
    ]
    snapshot_evidence = package.get("populationSnapshot")
    if snapshot_evidence is not None:
        snapshot = json.loads(verified_bytes(snapshot_evidence, "Population snapshot").decode("utf-8-sig"))
        if (snapshot.get("month") != month or snapshot.get("schemaVersion") != 1
                or snapshot.get("completeness", {}).get("complete") is not True
                or snapshot.get("lmPcode") != selected[0]["expected"]["lmPcode"]
                or snapshot.get("provider") != selected[0]["expected"]["provider"]):
            raise ValueError("Population snapshot scope/completeness mismatch")
        evidence["populationSnapshotSha256"] = snapshot_evidence["sha256"]
    return selected, evidence


def preserved_hash(payload, month):
    result = copy.deepcopy(dict(payload))
    history = result.get("monthlyCategories")
    if isinstance(history, dict) and month is not None:
        history.pop(month, None)
        if not history:
            result.pop("monthlyCategories", None)
    metadata = result.pop("metadata", None)
    # Creation preservation is separately verified against the before-image.
    return hashlib.sha256(json.dumps(result, sort_keys=True, default=str,
        separators=(",", ":")).encode()).hexdigest()


def changes(payload, context, create_time, now):
    month = context["month"]
    wanted = category(context["category"]) if context["category"] is not None else None
    history = payload.get("monthlyCategories", {})
    if not isinstance(history, dict):
        raise ValueError("monthlyCategories must be a map")
    for key, value in history.items():
        if not MONTH.fullmatch(key):
            raise ValueError("Existing category month is malformed")
        category(value)
    if wanted is not None and month in history and history[month] != wanted:
        raise ValueError(f"Historical category conflict at monthlyCategories.{month}")
    updates = {} if wanted is None or month in history else {f"monthlyCategories.{month}": wanted}
    # Missing classification never manufactures a category or attribution.
    # An independently approved creator still permits the standard metadata
    # backfill, using the original Firestore create_time.
    if wanted is None and "metadata" not in payload and not context.get("creator"):
        return updates
    updates.update(metadata_patch(payload, context, create_time, now, bool(updates)))
    return updates


def metadata_patch(payload, context, create_time, now, material=False):
    updates = {}
    metadata = payload.get("metadata")
    if context.get("requireCompleteMetadata") and (not isinstance(metadata, dict) or set(metadata) != META):
        raise ValueError("Approved category-only operation requires complete existing metadata; backfill not permitted")
    if metadata is None:
        metadata = {}
    else:
        if not isinstance(metadata, dict) or not set(metadata) <= META:
            raise ValueError("Existing metadata must have exactly the six governed fields")
        if any(k in metadata and not isinstance(metadata[k], datetime) for k in ("createdAt", "updatedAt")):
            raise ValueError("Existing metadata timestamps invalid")
        if any(k in metadata and (not isinstance(metadata[k], str) or not metadata[k].strip()) for k in META - {"createdAt", "updatedAt"}):
            raise ValueError("Existing metadata actors invalid")
    if context.get("pipelineAttributionConfirmed") and context.get("creator"):
        for key, identity_key in (("createdByUid", "uid"), ("createdByUser", "user")):
            if key in metadata and metadata[key] != context["creator"][identity_key]:
                raise ValueError(f"Existing metadata.{key} contradicts confirmed pipeline attribution")
    for key, identity_key in (("createdByUid", "uid"), ("createdByUser", "user")):
        if key in metadata:
            continue
        if not context.get("creator"):
            raise ValueError("Creation actor provenance unavailable for this identity")
        updates[f"metadata.{key}"] = context["creator"][identity_key]
    if "createdAt" not in metadata:
        if not isinstance(create_time, datetime):
            raise ValueError("Original Firestore creation timestamp unavailable")
        updates["metadata.createdAt"] = create_time
    if updates or material or not {"updatedAt", "updatedByUid", "updatedByUser"} <= set(metadata):
        updates.update({"metadata.updatedAt": now,
            "metadata.updatedByUid": context["actor"]["uid"],
            "metadata.updatedByUser": context["actor"]["user"]})
    return updates


def load_metadata_contract(path, expected_sha, rows, project_id):
    contract = json.loads(verified_bytes({"path": path, "sha256": expected_sha}, "Metadata contract").decode("utf-8-sig"))
    if contract.get("schemaVersion") != 1 or contract.get("projectId") != project_id:
        raise ValueError("Metadata contract schema/project mismatch")
    validate_identities(contract)
    eligible = creator_eligible_ids(contract, project_id)
    if not eligible <= {row["masterId"] for row in rows}:
        raise ValueError("Creator attribution contains identities outside input scope")
    for row in rows:
        row["metadataRefresh"] = {"actor": contract["actor"],
            "pipelineAttributionConfirmed": contract.get("pipelineAttributionEvidence") is not None,
            "creator": contract["creator"] if row["masterId"] in eligible else None}
    return {"path": str(path), "sha256": expected_sha, "standardMetadata": True}


def record_metadata_expectation(item, payload, updates):
    item["metadataExpected"] = {key: updates.get(f"metadata.{key}", payload.get("metadata", {}).get(key))
        for key in META}


def verify_metadata(payload, item):
    if payload.get("metadata") != item["metadataExpected"]:
        raise RuntimeError("Post-write metadata mismatch or creation provenance changed")


def classify(item, snapshot, now):
    context = item["categoryRefresh"]
    result = {"masterId": item["masterId"], "classification": "CONFLICT",
        "reason": None, "updates": {}, "preservedHash": None}
    try:
        if snapshot is None or not snapshot.exists:
            raise ValueError("Category source cannot create a Sales identity")
        payload = snapshot.to_dict() or {}
        for field in ("provider", "lmPcode", "meterNoNormalized"):
            if payload.get(field) != item["expected"].get(field):
                raise ValueError(f"Category target {field} differs")
        if payload.get("master", {}).get("id") != item["masterId"]:
            raise ValueError("Category target master.id differs")
        for month, value in context.get("requiredHistory", {}).items():
            if payload.get("monthlyCategories", {}).get(month) != value:
                raise ValueError(f"Required historical category missing or changed at {month}")
        updates = changes(payload, context, getattr(snapshot, "create_time", None), now)
        result.update(classification="UPDATED" if updates else "UNCHANGED", updates=updates,
            preservedHash=preserved_hash(payload, context["month"] if context["category"] is not None else None))
        item["categoryUpdateBefore"] = {k: updates.get(f"metadata.{k}", payload.get("metadata", {}).get(k))
            for k in ("updatedAt", "updatedByUid", "updatedByUser")}
        item["categoryCreationBefore"] = {k: payload.get("metadata", {}).get(k,
            updates.get(f"metadata.{k}")) for k in ("createdAt", "createdByUid", "createdByUser")}
    except ValueError as exc:
        result["reason"] = str(exc)
    return result


def verify(payload, item):
    context = item["categoryRefresh"]
    if context["category"] is not None and payload.get("monthlyCategories", {}).get(context["month"]) != context["category"]:
        raise RuntimeError("Post-write category mismatch")
    if any(payload.get("metadata", {}).get(k) != value for k, value in item["categoryCreationBefore"].items()):
        raise RuntimeError("Creation metadata changed")
    if any(payload.get("metadata", {}).get(k) != value for k, value in item["categoryUpdateBefore"].items()):
        raise RuntimeError("Update metadata mismatch")
    if changes(payload, context, None, None):
        raise RuntimeError("Post-write metadata/category mismatch")


def encode_before_image(item, snapshot):
    """Firestore protobuf JSON preserves timestamp/number/reference value types."""
    from google.cloud.firestore_v1 import _helpers
    from google.cloud.firestore_v1.types import document
    record = {"masterId": item["masterId"], "exists": bool(snapshot and snapshot.exists)}
    if record["exists"]:
        payload = document.Document(fields=_helpers.encode_dict(snapshot.to_dict() or {}),
            create_time=getattr(snapshot, "create_time", None),
            update_time=getattr(snapshot, "update_time", None))
        record["document"] = json.loads(document.Document.to_json(payload))
    return record


def encode_write_plan(decision, item):
    from google.cloud.firestore_v1 import _helpers
    from google.cloud.firestore_v1.types import document
    encoded = document.Document(fields=_helpers.encode_dict(decision.get("updates", {})),
        update_time=decision.get("updateTime"))
    payload = json.loads(document.Document.to_json(encoded))
    result = {"masterId": decision["masterId"], "classification": decision["classification"],
        "preconditionUpdateTime": payload.get("updateTime"), "afterPatch": payload.get("fields", {})}
    if decision["classification"] == "CREATED":
        result["createDocument"] = json.loads(document.Document.to_json(
            document.Document(fields=_helpers.encode_dict(item["expected"]))))
    return result


def ingest_workbook(path, expected_sha, sheet, month, canonical_ids, *, identity_field="MeterNumber", source_rows=None):
    """Return sparse categories plus explicit source-identity exceptions.

    Identity strings in sources are never changed. Unique leading-zero equality
    is a comparison to the prevalidated canonical identity set, not a new ID.
    """
    import openpyxl
    raw_bytes = verified_bytes({"path": path, "sha256": expected_sha}, "Classification source")
    if not MONTH.fullmatch(month):
        raise ValueError("Invalid category month")
    if identity_field not in {"MeterNumber", "CorrectedMeterNumber"}:
        raise ValueError("Unsupported governed identity field")
    corrected = identity_field == "CorrectedMeterNumber"
    if corrected and month not in {"2026-07", "2026-08"}:
        raise ValueError("CorrectedMeterNumber requires explicitly governed July or August source")
    column = "Leakage_Category" if month == "2026-06" else datetime.strptime(month, "%Y-%m").strftime("%B_%Y_Category")
    expected_sheet = "Sheet1" if month == "2026-06" else "Prepaid_30Month_Analysis"
    if sheet != expected_sheet:
        raise ValueError("Unexpected governed classification sheet")
    aliases = {}
    for meter in canonical_ids:
        aliases.setdefault(meter.lstrip("0") or "0", []).append(meter)
    book = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
    try:
        stream = book[sheet].iter_rows(values_only=True)
        headers = list(next(stream))
        required = [identity_field, column, "Risk_Tier", "Risk_Score"]
        if corrected and any(headers.count(h) != 1 for h in ("MeterNumber", "PreviousMeterNumber")):
            raise ValueError("Corrected source must preserve original/current predecessor evidence")
        if any(headers.count(h) != 1 for h in required):
            raise ValueError("Missing/duplicate classification source header")
        indexes = [headers.index(h) for h in required]
        result, exceptions, reconciled = {}, [], []
        seen = set()
        for row_number, row in enumerate(stream, 2):
            raw, cat, tier, score = [row[i] for i in indexes]
            if corrected and (type(raw) is not str or not re.fullmatch(r"[0-9]+", raw) or not raw.strip("0")):
                raise ValueError(f"Blank/invalid CorrectedMeterNumber at row {row_number}")
            raw_text = raw if corrected else (str(raw).strip() if raw is not None else "")
            matches = [raw_text] if raw_text in canonical_ids else ([] if corrected else aliases.get(raw_text.lstrip("0") or "0", []))
            if corrected and len(matches) != 1:
                raise ValueError(f"CorrectedMeterNumber outside execution population at row {row_number}")
            if len(matches) != 1:
                exceptions.append({"sourceRow": row_number, "sourceIdentity": raw_text,
                    "reason": "unmatched" if not matches else "ambiguous identity"})
                continue
            meter = matches[0]
            if source_rows is not None and meter in source_rows:
                source_rows[meter] = dict(zip(headers, row))
                source_rows[meter]["__sourceRow"] = row_number
            if meter in seen:
                raise ValueError(f"Duplicate classification for canonical identity {meter}")
            seen.add(meter)
            value = {"leakageCategory": cat, "riskTier": tier, "riskScore": score}
            try:
                category(value)
            except ValueError as exc:
                exceptions.append({"meterId": meter, "sourceRow": row_number,
                    "sourceIdentity": raw_text, "reason": str(exc)})
                continue
            result[meter] = value
            if raw_text != meter:
                reconciled.append({"sourceIdentity": raw_text, "canonicalId": meter, "sourceRow": row_number})
        return result, exceptions, reconciled
    finally:
        book.close()
