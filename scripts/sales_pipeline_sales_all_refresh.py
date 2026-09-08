"""Governed Stage 08 refresh support for provider-neutral Sales All Meters.

Used only by Stage 08 --mode refresh. The legacy create-only/resume path remains
unchanged. Existing operational fields are preserved because refresh updates
pipeline-owned field paths only; it never writes tbRefs, geofenceRefs or
master.visibility on an existing document.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from sales_address_enrichment import (
    ADDRESS_MAP_FIELDS,
    ADDRESS_STAGING_COLUMNS,
    address_map_from_row,
    validate_address_values,
)
from sales_pipeline_monthly_source_support import (
    COMMERCIAL_JSON_FIELDS,
    COMMERCIAL_SCALAR_FIELDS,
    canonical_sha256,
    normalize_meter,
    safe_str,
)

AMOUNT_RE = re.compile(r"^amount_(\d{4})_(\d{2})_C$")
UNITS_RE = re.compile(r"^units_(\d{4})_(\d{2})$")
PROVIDER_RE = re.compile(r"^[a-z0-9_-]+$")
VISIBILITY_VALUES = {"VISIBLE", "INVISIBLE"}
DEFAULT_VISIBILITY = "INVISIBLE"
COLLECTION = "sales-all-meters"
FIRESTORE_BATCH_SIZE = 400

BASE_COLUMNS = [
    "masterId", "meterNo", "meterNoNormalized", "provider", "customerNo", "accountNo",
    "totalAmountC", "lastPurchaseAtISO", "daysSinceLastPurchase",
]
RICH_COLUMNS = [
    "lmPcode", "accountNumber", "customerName", "sourceFileName", "sourceRow",
    "totalSalesC", "totalUnits",
    *COMMERCIAL_SCALAR_FIELDS, *COMMERCIAL_JSON_FIELDS,
]

PIPELINE_ROOT_FIELDS = {
    "meterNo", "meterNoNormalized", "provider", "customerNo", "accountNo",
    "totalAmountC", "monthlyTotalsC", "lastPurchaseAtISO", "daysSinceLastPurchase",
    "lmPcode", "accountNumber", "customerName", "sourceFileName", "sourceRow",
    "totalSalesC", "monthlySalesC", "monthlyUnits", "totalUnits",
    "adr",
    *COMMERCIAL_SCALAR_FIELDS, *COMMERCIAL_JSON_FIELDS,
}
# Frozen legacy categories are never authoritative refresh targets.
PIPELINE_ROOT_FIELDS -= {"leakageCategory", "riskTier", "riskScore"}
PIPELINE_ROOT_FIELDS.add("monthlyCategories")

@dataclass
class RefreshStats:
    rows: int
    inspected: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    conflicts: int = 0
    failed: int = 0
    writes_attempted: int = 0
    writes_succeeded: int = 0
    conflict_records: list[dict[str, Any]] = field(default_factory=list)
    failure_records: list[dict[str, Any]] = field(default_factory=list)
    read_waves: int = 0
    write_waves_attempted: int = 0
    write_waves_committed: int = 0
    verification_read_waves: int = 0
    precondition_conflicts: int = 0
    maximum_write_operations_in_any_batch: int = 0
    global_preflight_complete: bool = False
    global_preflight_gate_passed: bool = False
    committed_write_waves: list[dict[str, Any]] = field(default_factory=list)
    failed_write_wave: dict[str, Any] | None = None


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decimal(value: Any, label: str) -> Decimal:
    text = safe_str(value)
    if not text:
        return Decimal("0")
    try:
        d = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{label} is not numeric: {text!r}") from exc
    if not d.is_finite() or d < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    return d


def _int_or_blank(value: Any, label: str) -> int | None:
    text = safe_str(value)
    if not text:
        return None
    d = _decimal(text, label)
    if d != d.to_integral_value():
        raise ValueError(f"{label} must be integer")
    return int(d)


def _bool(value: Any, label: str) -> bool:
    text = safe_str(value).lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no", ""}:
        return False
    raise ValueError(f"{label} must be boolean text")


def _number_or_text(value: Any) -> Any:
    text = safe_str(value)
    if text == "":
        return ""
    try:
        d = Decimal(text)
    except InvalidOperation:
        return text
    if d == d.to_integral_value():
        return int(d)
    return float(d)


def _json_value(value: Any, label: str) -> Any:
    text = safe_str(value)
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except Exception as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(parsed, (list, dict)):
        raise ValueError(f"{label} must decode to list/object")
    return parsed


def _manifest_fingerprint(manifest: Mapping[str, Any]) -> str:
    source = manifest.get("sourceContract")
    output = manifest.get("outputContract")
    stats = manifest.get("stats")
    if not isinstance(source, Mapping) or not isinstance(output, Mapping) or not isinstance(stats, Mapping):
        raise ValueError("Stage 06 manifest contracts are incomplete")
    contract = {
        "sourceContract": dict(source),
        "outputContract": {
            k: output.get(k)
            for k in (
                "filename", "rows", "columns", "sha256", "documentIdsSha256",
                "months", "monthlyColumns", "monthlyUnitColumns", "provider",
                "totalAmountC", "totalUnits", "visibilityColumn",
                "addressEnrichment"
            )
        },
        "stats": dict(stats),
    }
    return canonical_sha256(contract)


def load_and_validate(path: Path, manifest_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError("Stage 08 input/manifest file is missing")
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    if manifest.get("schemaVersion") != 2 or manifest.get("stage") != "06":
        raise ValueError("Stage 08 refresh requires Stage 06 schemaVersion 2")
    if manifest.get("script") != "06_build_sales_all_meters.py" or manifest.get("status") != "PASS":
        raise ValueError("Stage 06 manifest identity/status mismatch")
    if safe_str(manifest.get("buildFingerprint")) != _manifest_fingerprint(manifest):
        raise ValueError("Stage 06 manifest buildFingerprint is invalid")
    source = manifest["sourceContract"]
    output = manifest["outputContract"]
    if source.get("sourceOrigin") != "monthly_source":
        raise ValueError("Stage 08 refresh package is currently governed for monthly_source schemaVersion 2")
    if source.get("recencyFactsAvailable") is not False:
        raise ValueError("Monthly-source Stage 06 must explicitly state recencyFactsAvailable=false")
    if source.get("visibilityOwnership") != "OPERATIONAL_WRITERS_ONLY":
        raise ValueError("Stage 06 visibility ownership mismatch")

    raw = path.read_bytes()
    if output.get("sha256") != hashlib.sha256(raw).hexdigest():
        raise ValueError("Stage 06 CSV SHA mismatch")
    df = pd.read_csv(io.BytesIO(raw), dtype=str, encoding="utf-8-sig").fillna("")
    if output.get("columns") != list(df.columns) or output.get("rows") != len(df):
        raise ValueError("Stage 06 outputContract does not match CSV")

    amount_columns = [c for c in df.columns if AMOUNT_RE.fullmatch(c)]
    unit_columns = [c for c in df.columns if UNITS_RE.fullmatch(c)]
    category_columns = ["monthlyCategories"] if "monthlyCategories" in df.columns else []
    expected = BASE_COLUMNS + RICH_COLUMNS + category_columns + ADDRESS_STAGING_COLUMNS + amount_columns + unit_columns
    if list(df.columns) != expected:
        raise ValueError("Stage 08 refresh CSV columns/order do not match the rich monthly-source contract")
    months = [f"{AMOUNT_RE.fullmatch(c).group(1)}-{AMOUNT_RE.fullmatch(c).group(2)}" for c in amount_columns]
    unit_months = [f"{UNITS_RE.fullmatch(c).group(1)}-{UNITS_RE.fullmatch(c).group(2)}" for c in unit_columns]
    if months != unit_months or months != list(source.get("includedMonths") or []):
        raise ValueError("Amount/unit month columns do not match the Stage 06 month contract")
    provider = safe_str(source.get("provider"))
    if not provider or not PROVIDER_RE.fullmatch(provider):
        raise ValueError("Stage 06 provider is not canonical")

    address_contract = output.get("addressEnrichment")
    if not isinstance(address_contract, Mapping) or address_contract.get("enabled") is not True:
        raise ValueError("Stage 08 rich contract requires enabled Stage 06 addressEnrichment")
    if address_contract.get("stagingColumns") != ADDRESS_STAGING_COLUMNS:
        raise ValueError("Stage 06 addressEnrichment stagingColumns mismatch")
    if address_contract.get("firestoreProjection") != "adr":
        raise ValueError("Stage 06 addressEnrichment Firestore projection must be adr")
    if address_contract.get("rawAddressMutationCount") != 0:
        raise ValueError("Stage 06 addressEnrichment reports raw address mutation")
    if address_contract.get("fabricatedSpatialRelationshipCount") != 0:
        raise ValueError("Stage 06 addressEnrichment reports fabricated spatial relationships")
    report_filename = safe_str(address_contract.get("reportFilename"))
    report_sha = safe_str(address_contract.get("reportSha256")).lower()
    if not report_filename or not report_sha:
        raise ValueError("Stage 06 addressEnrichment report fingerprint is incomplete")
    report_path = path.with_name(report_filename)
    if not report_path.is_file() or _sha(report_path) != report_sha:
        raise ValueError("Stage 06 addressEnrichment report is missing or has the wrong SHA")

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_sales = 0
    total_units = Decimal("0")
    address_enriched_rows = 0
    address_unresolved_rows = 0
    for csv_row, raw_row in enumerate(df.to_dict("records"), start=2):
        meter = normalize_meter(raw_row.get("masterId"))
        normalized = normalize_meter(raw_row.get("meterNoNormalized"))
        if not meter or meter != normalized:
            raise ValueError(f"Identity mismatch at CSV row {csv_row}")
        if meter in seen:
            raise ValueError(f"Duplicate masterId {meter}")
        seen.add(meter)
        if safe_str(raw_row.get("provider")) != provider:
            raise ValueError(f"Provider mismatch at CSV row {csv_row}")
        if safe_str(raw_row.get("lmPcode")) != safe_str(source.get("lmPcode")):
            raise ValueError(f"LM mismatch at CSV row {csv_row}")
        if safe_str(raw_row.get("lastPurchaseAtISO")) or safe_str(raw_row.get("daysSinceLastPurchase")):
            raise ValueError("Monthly-source rows may not fabricate transaction recency facts")
        try:
            validate_address_values(
                raw_row.get("strNo"), raw_row.get("strName"), raw_row.get("strType")
            )
        except ValueError as exc:
            raise ValueError(f"Invalid Sales Enrich address at CSV row {csv_row}: {exc}") from exc
        if safe_str(raw_row.get("strNo")) and safe_str(raw_row.get("strName")):
            address_enriched_rows += 1
        else:
            address_unresolved_rows += 1

        monthly_sales: dict[str, int] = {}
        monthly_units: dict[str, float] = {}
        row_total = 0
        row_units = Decimal("0")
        for month, ac, uc in zip(months, amount_columns, unit_columns):
            amount = _int_or_blank(raw_row.get(ac), ac)
            if amount is None:
                amount = 0
            units = _decimal(raw_row.get(uc), uc)
            monthly_sales[month] = amount
            monthly_units[month] = float(units)
            row_total += amount
            row_units += units
        declared_total = _int_or_blank(raw_row.get("totalAmountC"), "totalAmountC")
        declared_total_sales = _int_or_blank(raw_row.get("totalSalesC"), "totalSalesC")
        if declared_total != row_total or declared_total_sales != row_total:
            raise ValueError(f"Sales total mismatch at CSV row {csv_row}")
        declared_units = _decimal(raw_row.get("totalUnits"), "totalUnits")
        if declared_units != row_units:
            raise ValueError(f"Units total mismatch at CSV row {csv_row}")
        total_sales += row_total
        total_units += row_units

        doc: dict[str, Any] = {
            "master": {"id": meter, "visibility": DEFAULT_VISIBILITY},
            "meterNo": safe_str(raw_row.get("meterNo")) or meter,
            "meterNoNormalized": meter,
            "provider": provider,
            "customerNo": safe_str(raw_row.get("customerNo")),
            "accountNo": safe_str(raw_row.get("accountNo")),
            "accountNumber": safe_str(raw_row.get("accountNumber")),
            "customerName": safe_str(raw_row.get("customerName")),
            "sourceFileName": safe_str(raw_row.get("sourceFileName")),
            "sourceRow": _int_or_blank(raw_row.get("sourceRow"), "sourceRow"),
            "lmPcode": safe_str(raw_row.get("lmPcode")),
            "totalAmountC": row_total,
            "totalSalesC": row_total,
            "monthlyTotalsC": monthly_sales,
            "monthlySalesC": monthly_sales,
            "monthlyUnits": monthly_units,
            "totalUnits": float(row_units),
            "lastPurchaseAtISO": None,
            "daysSinceLastPurchase": None,
            "adr": address_map_from_row(raw_row),
        }
        for field in COMMERCIAL_SCALAR_FIELDS:
            raw_value = raw_row.get(field)
            if field in {"hasUsableGps", "elmAccountMatched"}:
                doc[field] = _bool(raw_value, field)
            elif field in {"sourceEndRow", "erfCandidateCount", "elmSourceRows"}:
                doc[field] = _int_or_blank(raw_value, field)
            elif field == "riskScore":
                doc[field] = _number_or_text(raw_value)
            else:
                doc[field] = safe_str(raw_value)
        for field in COMMERCIAL_JSON_FIELDS:
            doc[field] = _json_value(raw_row.get(field), field)
        if category_columns:
            from sales_monthly_categories import validate_history
            doc["monthlyCategories"] = validate_history(json.loads(raw_row["monthlyCategories"]))
        rows.append({"masterId": meter, "expected": doc})

    if output.get("totalAmountC") != total_sales:
        raise ValueError("Stage 06 outputContract totalAmountC mismatch")
    if Decimal(str(output.get("totalUnits"))) != total_units:
        raise ValueError("Stage 06 outputContract totalUnits mismatch")
    if output.get("provider") != provider or output.get("visibilityColumn") != "ABSENT":
        raise ValueError("Stage 06 output provider/visibility contract mismatch")
    if address_contract.get("enrichedRows") != address_enriched_rows:
        raise ValueError("Stage 06 addressEnrichment enrichedRows mismatch")
    if address_contract.get("unresolvedRows") != address_unresolved_rows:
        raise ValueError("Stage 06 addressEnrichment unresolvedRows mismatch")
    return rows, {
        "provider": provider,
        "lmPcode": source.get("lmPcode"),
        "months": months,
        "sourceRunId": source.get("sourceRunId"),
        "csvSha256": hashlib.sha256(raw).hexdigest(),
        "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "stage06BuildFingerprint": manifest.get("buildFingerprint"),
        "rows": len(rows),
        "totalAmountC": total_sales,
        "totalUnits": str(total_units),
        "addressEnrichment": dict(address_contract),
    }


def _conflict(existing: Mapping[str, Any], expected: Mapping[str, Any]) -> str | None:
    if "monthlyCategories" in expected:
        from sales_monthly_categories import validate_history
        try:
            old_categories = validate_history(existing.get("monthlyCategories", {}))
            new_categories = validate_history(expected["monthlyCategories"])
        except ValueError as exc:
            return str(exc)
        for month, value in old_categories.items():
            if month not in new_categories or value != new_categories[month]:
                return f"Historical category conflict at monthlyCategories.{month}"
    for monthly_field in ("monthlyTotalsC", "monthlySalesC", "monthlyUnits"):
        history = existing.get(monthly_field, {})
        wanted = expected.get(monthly_field, {})
        if not isinstance(history, Mapping) or not isinstance(wanted, Mapping):
            return f"{monthly_field} must be an object"
        for month, value in history.items():
            if month not in wanted or not _strict_equal(value, wanted[month]):
                return f"Historical Sales conflict at {monthly_field}.{month}"
    master = existing.get("master")
    if not isinstance(master, Mapping):
        return "master missing/not object"
    if master.get("id") != expected["master"]["id"]:
        return "master.id differs"
    visibility = master.get("visibility")
    if visibility not in VISIBILITY_VALUES:
        return "master.visibility invalid"
    if any(field in existing for field in ADDRESS_STAGING_COLUMNS):
        return "root strNo/strName/strType is prohibited; use adr"
    if "adr" in existing:
        adr = existing.get("adr")
        if not isinstance(adr, Mapping):
            return "adr exists but is not an object"
        if set(adr.keys()) != ADDRESS_MAP_FIELDS:
            return "adr has unexpected/missing keys"
        try:
            validate_address_values(adr.get("strNo"), adr.get("strName"), adr.get("strType"))
        except ValueError as exc:
            return f"adr is noncanonical: {exc}"
    for field in ("meterNoNormalized", "provider", "lmPcode"):
        if field in existing and existing.get(field) not in (None, "") and existing.get(field) != expected.get(field):
            return f"{field} differs"
    return None


def _strict_equal(left: Any, right: Any) -> bool:
    # Firestore distinguishes booleans, integers and doubles. Avoid Python's
    # bool/int and int/float loose equality hiding a type drift.
    if right is None:
        return left is None
    if isinstance(right, bool):
        return isinstance(left, bool) and left is right
    if isinstance(right, int) and not isinstance(right, bool):
        return isinstance(left, int) and not isinstance(left, bool) and left == right
    if isinstance(right, float):
        return isinstance(left, float) and left == right
    if isinstance(right, str):
        return isinstance(left, str) and left == right
    if isinstance(right, list):
        return (
            isinstance(left, list)
            and len(left) == len(right)
            and all(_strict_equal(a, b) for a, b in zip(left, right))
        )
    if isinstance(right, Mapping):
        return (
            isinstance(left, Mapping)
            and set(left.keys()) == set(right.keys())
            and all(_strict_equal(left[key], right[key]) for key in right)
        )
    return type(left) is type(right) and left == right


def _updates(existing: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    for field in sorted(PIPELINE_ROOT_FIELDS):
        if field == "monthlyCategories" and field not in expected:
            continue
        wanted = expected.get(field)
        if field in {"monthlyTotalsC", "monthlySalesC", "monthlyUnits", "monthlyCategories"} and isinstance(wanted, Mapping):
            existing_months = existing.get(field, {})
            for month, value in wanted.items():
                if month not in existing_months:
                    updates[f"{field}.{month}"] = value
            continue
        if not _strict_equal(existing.get(field), wanted):
            updates[field] = wanted
    return updates


def _verify_doc(payload: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    conflict = _conflict(payload, expected)
    if conflict:
        raise RuntimeError(f"Post-write identity/visibility verification failed: {conflict}")
    updates = _updates(payload, expected)
    if updates:
        raise RuntimeError(f"Post-write pipeline field mismatch: {sorted(updates)[:10]}")


def _preserved_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return every root Stage 08 does not own, including the full master map."""
    return {
        key: value
        for key, value in payload.items()
        if key not in PIPELINE_ROOT_FIELDS
    }


def _projection_hash(payload: Mapping[str, Any]) -> str:
    return canonical_sha256(_preserved_projection(payload))


def _chunks(items: Sequence[Any], size: int = FIRESTORE_BATCH_SIZE):
    if size <= 0 or size > FIRESTORE_BATCH_SIZE:
        raise ValueError(f"Governed Firestore wave size must be 1..{FIRESTORE_BATCH_SIZE}")
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _bulk_snapshots(db: Any, refs: Sequence[Any]) -> dict[str, Any]:
    if len(refs) > FIRESTORE_BATCH_SIZE:
        raise ValueError("Bulk read exceeds governed 400-reference limit")
    snapshots = list(db.get_all(list(refs)))
    by_path: dict[str, Any] = {}
    for snapshot in snapshots:
        reference = getattr(snapshot, "reference", None)
        path = getattr(reference, "path", None)
        if not path:
            path = f"{COLLECTION}/{snapshot.id}"
        by_path[str(path)] = snapshot
    return by_path


def _snapshot_for(by_path: Mapping[str, Any], ref: Any) -> Any | None:
    path = getattr(ref, "path", None) or f"{COLLECTION}/{getattr(ref, 'id', '')}"
    return by_path.get(str(path))


def _snapshot_token(snapshot: Any | None) -> tuple[bool, Any]:
    if snapshot is None or not snapshot.exists:
        return (False, None)
    return (True, getattr(snapshot, "update_time", None))


def _classify_snapshot(item: Mapping[str, Any], snapshot: Any | None) -> dict[str, Any]:
    if item.get("createOnly") and snapshot is not None and snapshot.exists:
        return {"masterId": str(item["masterId"]), "classification": "CONFLICT",
            "reason": "Approved create target already exists; review required", "updates": {}, "preservedHash": None}
    if "categoryRefresh" in item:
        from sales_monthly_categories import classify
        return classify(item, snapshot, datetime.now(UTC))
    doc_id = str(item["masterId"])
    expected = item["expected"]
    if snapshot is None or not snapshot.exists:
        if "metadataRefresh" in item:
            from sales_monthly_categories import metadata_patch, record_metadata_expectation
            context = dict(item["metadataRefresh"])
            context["creator"] = context["actor"]
            for legacy_field in ("leakageCategory", "riskTier", "riskScore"):
                expected.pop(legacy_field, None)
            now = datetime.now(UTC)
            metadata_updates = metadata_patch({}, context, now, now, True)
            expected["metadata"] = {key.split(".")[1]: value for key, value in metadata_updates.items()}
            record_metadata_expectation(item, expected, {})
        return {
            "masterId": doc_id,
            "classification": "CREATED",
            "reason": None,
            "updates": {},
            "snapshot": snapshot,
            "preservedHash": None,
        }
    payload = snapshot.to_dict() or {}
    conflict = _conflict(payload, expected)
    if conflict:
        return {
            "masterId": doc_id,
            "classification": "CONFLICT",
            "reason": conflict,
            "updates": {},
            "snapshot": snapshot,
            "preservedHash": None,
        }
    changes = _updates(payload, expected)
    preserved = _projection_hash(payload)
    if "metadataRefresh" in item:
        from sales_monthly_categories import metadata_patch, record_metadata_expectation
        try:
            metadata_updates = metadata_patch(payload, item["metadataRefresh"],
                getattr(snapshot, "create_time", None), datetime.now(UTC), bool(changes))
        except ValueError as exc:
            return {"masterId": doc_id, "classification": "CONFLICT", "reason": str(exc),
                "updates": {}, "preservedHash": None}
        changes.update(metadata_updates)
        record_metadata_expectation(item, payload, metadata_updates)
        preserved = _projection_hash({k: v for k, v in payload.items() if k != "metadata"})
    return {
        "masterId": doc_id,
        "classification": "UPDATED" if changes else "UNCHANGED",
        "reason": None,
        "updates": changes,
        "snapshot": snapshot,
        "preservedHash": preserved,
    }


def _add_classification(stats: RefreshStats, classified: Mapping[str, Any]) -> None:
    stats.inspected += 1
    classification = str(classified["classification"])
    if classification == "CONFLICT":
        stats.conflicts += 1
        stats.conflict_records.append(
            {"masterId": classified["masterId"], "reason": classified.get("reason")}
        )
    else:
        counter_name = classification.lower()
        setattr(stats, counter_name, getattr(stats, counter_name) + 1)


def _build_write_batch(
    *,
    db: Any,
    collection: Any,
    operations: Sequence[Mapping[str, Any]],
    last_update_option_cls: Any,
) -> Any:
    if len(operations) > FIRESTORE_BATCH_SIZE:
        raise ValueError("Write batch exceeds governed 400-operation limit")
    batch = db.batch()
    for operation in operations:
        ref = collection.document(str(operation["masterId"]))
        if operation["classification"] == "CREATED":
            batch.create(ref, operation["expected"])
        elif operation["classification"] == "UPDATED":
            update_time = operation.get("updateTime")
            if update_time is None:
                snapshot = operation.get("snapshot")
                update_time = getattr(snapshot, "update_time", None)
            if update_time is None:
                raise RuntimeError(f"Missing update_time for {operation['masterId']}")
            batch.update(
                ref,
                dict(operation["updates"]),
                option=last_update_option_cls(update_time),
            )
        else:
            raise ValueError(f"Non-write classification in batch: {operation['classification']}")
    return batch


def _write_operations_for_wave(
    *,
    db: Any,
    collection: Any,
    wave_items: Sequence[Mapping[str, Any]],
    classified: Sequence[Mapping[str, Any]],
    stats: RefreshStats,
    preserved_before: dict[str, str],
    last_update_option_cls: Any,
    concurrency_exceptions: tuple[type[BaseException], ...],
) -> list[Mapping[str, Any]]:
    """Commit one governed wave, with one bounded concurrency recovery."""
    item_by_id = {str(item["masterId"]): item for item in wave_items}
    operations: list[dict[str, Any]] = []

    for decision in classified:
        classification = str(decision["classification"])
        if classification in {"UNCHANGED", "CONFLICT"}:
            _add_classification(stats, decision)
            if classification == "UNCHANGED" and decision.get("preservedHash") is not None:
                preserved_before[str(decision["masterId"])] = str(decision["preservedHash"])
            continue
        operations.append(
            {
                **dict(decision),
                "expected": item_by_id[str(decision["masterId"])]["expected"],
                "originalToken": _snapshot_token(decision.get("snapshot")),
            }
        )

    if not operations:
        return []

    stats.maximum_write_operations_in_any_batch = max(
        stats.maximum_write_operations_in_any_batch, len(operations)
    )
    stats.write_waves_attempted += 1
    stats.writes_attempted += len(operations)
    batch = _build_write_batch(
        db=db,
        collection=collection,
        operations=operations,
        last_update_option_cls=last_update_option_cls,
    )
    try:
        batch.commit()
        committed = operations
    except concurrency_exceptions as first_exc:
        # Atomic batch failed: zero writes committed. Re-read the complete failed
        # wave once, convert changed participants to conflicts, and retry only the
        # unchanged safe subset as one batch. Never fall back to per-record I/O.
        refs = [collection.document(str(item["masterId"])) for item in wave_items]
        refreshed = _bulk_snapshots(db, refs)
        stats.read_waves += 1
        retry_operations: list[dict[str, Any]] = []
        committed = []
        for operation in operations:
            ref = collection.document(str(operation["masterId"]))
            fresh_snapshot = _snapshot_for(refreshed, ref)
            fresh_token = _snapshot_token(fresh_snapshot)
            if fresh_token != operation["originalToken"]:
                stats.precondition_conflicts += 1
                _add_classification(
                    stats,
                    {
                        "masterId": operation["masterId"],
                        "classification": "CONFLICT",
                        "reason": "batch precondition changed after preflight",
                    },
                )
                continue
            fresh_decision = _classify_snapshot(item_by_id[str(operation["masterId"])], fresh_snapshot)
            fresh_classification = str(fresh_decision["classification"])
            if fresh_classification in {"CONFLICT", "UNCHANGED"}:
                _add_classification(stats, fresh_decision)
                if fresh_classification == "UNCHANGED" and fresh_decision.get("preservedHash") is not None:
                    preserved_before[str(fresh_decision["masterId"])] = str(fresh_decision["preservedHash"])
                continue
            retry_operations.append(
                {
                    **dict(fresh_decision),
                    "expected": item_by_id[str(operation["masterId"])]["expected"],
                    "originalToken": fresh_token,
                }
            )

        if retry_operations:
            stats.maximum_write_operations_in_any_batch = max(
                stats.maximum_write_operations_in_any_batch, len(retry_operations)
            )
            stats.write_waves_attempted += 1
            stats.writes_attempted += len(retry_operations)
            retry_batch = _build_write_batch(
                db=db,
                collection=collection,
                operations=retry_operations,
                last_update_option_cls=last_update_option_cls,
            )
            try:
                retry_batch.commit()
            except concurrency_exceptions as second_exc:
                raise RuntimeError(
                    "Stage 08 refresh concurrency recovery batch failed; "
                    "no per-document fallback is permitted"
                ) from second_exc
            committed = retry_operations
        else:
            committed = []

    if committed:
        stats.write_waves_committed += 1
        stats.writes_succeeded += len(committed)
        for operation in committed:
            classification = str(operation["classification"])
            stats.inspected += 1
            setattr(stats, classification.lower(), getattr(stats, classification.lower()) + 1)
            if classification == "UPDATED" and operation.get("preservedHash") is not None:
                preserved_before[str(operation["masterId"])] = str(operation["preservedHash"])
    return committed



def classify_all(
    *,
    db: Any,
    collection: Any,
    rows: Sequence[Mapping[str, Any]],
    stats: RefreshStats,
    preserved_before: dict[str, str],
    recovery_sink: Any = None,
) -> list[dict[str, Any]]:
    """Classify the complete intended run before any Firestore write begins.

    The returned plan deliberately retains no Firestore snapshot objects and no
    duplicate expected documents. Only the update-time token needed for an
    optimistic update precondition is carried into the write phase.
    """
    plan: list[dict[str, Any]] = []
    processed = 0
    wave_number = 0

    for wave_items in _chunks(rows):
        wave_number += 1
        refs = [collection.document(str(item["masterId"])) for item in wave_items]
        snapshots = _bulk_snapshots(db, refs)
        stats.read_waves += 1
        compact_decisions: list[dict[str, Any]] = []

        for item, ref in zip(wave_items, refs):
            snapshot = _snapshot_for(snapshots, ref)
            if recovery_sink is not None:
                recovery_sink(item, snapshot)
            decision = _classify_snapshot(item, snapshot)
            _add_classification(stats, decision)

            doc_id = str(decision["masterId"])
            preserved_hash = decision.get("preservedHash")
            if preserved_hash is not None:
                preserved_before[doc_id] = str(preserved_hash)

            compact_decisions.append(
                {
                    "masterId": doc_id,
                    "classification": str(decision["classification"]),
                    "reason": decision.get("reason"),
                    "updates": dict(decision.get("updates") or {}),
                    "updateTime": (
                        getattr(snapshot, "update_time", None)
                        if snapshot is not None and snapshot.exists
                        else None
                    ),
                }
            )

        plan.append(
            {
                "waveNumber": wave_number,
                "decisions": compact_decisions,
            }
        )
        processed += len(wave_items)
        print(
            f"Stage 08 global preflight {processed:,}/{len(rows):,}: "
            f"create={stats.created:,} update={stats.updated:,} "
            f"unchanged={stats.unchanged:,} conflict={stats.conflicts:,} "
            f"failed={stats.failed:,}; readWaves={stats.read_waves:,}; writes=0"
        )

    stats.global_preflight_complete = True
    return plan


def evaluate_global_gate(
    *,
    rows: Sequence[Mapping[str, Any]],
    plan: Sequence[Mapping[str, Any]],
    stats: RefreshStats,
) -> None:
    """Prove full-run accounting and conflict freedom before writes may start."""
    planned_count = sum(len(wave["decisions"]) for wave in plan)
    classified_count = (
        stats.created
        + stats.updated
        + stats.unchanged
        + stats.conflicts
        + stats.failed
    )

    if not stats.global_preflight_complete:
        raise RuntimeError("Stage 08 global preflight did not complete")

    if (
        stats.writes_attempted != 0
        or stats.writes_succeeded != 0
        or stats.write_waves_attempted != 0
        or stats.write_waves_committed != 0
    ):
        raise RuntimeError(
            "Stage 08 global preflight accounting gate observed writes before approval"
        )

    if (
        planned_count != len(rows)
        or classified_count != len(rows)
        or stats.inspected != len(rows)
    ):
        raise RuntimeError(
            "Stage 08 global preflight accounting imbalance: "
            f"rows={len(rows)}; planned={planned_count}; "
            f"classified={classified_count}; inspected={stats.inspected}"
        )

    if stats.conflicts or stats.failed:
        raise RuntimeError(
            "Stage 08 global preflight blocked before writes: "
            f"conflicts={stats.conflicts}; failed={stats.failed}; "
            "writeAttemptCount=0; firestoreWrites=0"
        )

    stats.global_preflight_gate_passed = True


def _wave_evidence(
    *,
    wave_number: int,
    operations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    ids = [str(operation["masterId"]) for operation in operations]
    return {
        "waveNumber": wave_number,
        "operationCount": len(operations),
        "firstMasterId": ids[0] if ids else None,
        "lastMasterId": ids[-1] if ids else None,
    }


def execute_global_plan(
    *,
    db: Any,
    collection: Any,
    rows: Sequence[Mapping[str, Any]],
    plan: Sequence[Mapping[str, Any]],
    stats: RefreshStats,
    last_update_option_cls: Any,
    concurrency_exceptions: tuple[type[BaseException], ...],
    scope_guard: Any = None,
) -> None:
    """Execute a globally approved plan; stop on the first failed write wave."""
    if not stats.global_preflight_gate_passed:
        raise RuntimeError("Stage 08 write phase cannot start before the global gate passes")
    if scope_guard is not None:
        scope_guard(rows, plan)

    item_by_id = {str(item["masterId"]): item for item in rows}

    for wave in plan:
        wave_number = int(wave["waveNumber"])
        operations: list[dict[str, Any]] = []

        for decision in wave["decisions"]:
            classification = str(decision["classification"])
            if classification not in {"CREATED", "UPDATED"}:
                continue

            doc_id = str(decision["masterId"])
            operations.append(
                {
                    "masterId": doc_id,
                    "classification": classification,
                    "expected": item_by_id[doc_id]["expected"],
                    "updates": dict(decision.get("updates") or {}),
                    "updateTime": decision.get("updateTime"),
                }
            )

        if not operations:
            continue

        if scope_guard is not None:
            scope_guard(rows, plan)

        evidence = _wave_evidence(
            wave_number=wave_number,
            operations=operations,
        )
        stats.maximum_write_operations_in_any_batch = max(
            stats.maximum_write_operations_in_any_batch,
            len(operations),
        )
        stats.write_waves_attempted += 1
        stats.writes_attempted += len(operations)

        batch = _build_write_batch(
            db=db,
            collection=collection,
            operations=operations,
            last_update_option_cls=last_update_option_cls,
        )

        try:
            batch.commit()
        except concurrency_exceptions as exc:
            stats.precondition_conflicts += 1
            stats.failed_write_wave = evidence
            raise RuntimeError(
                "Stage 08 refresh concurrency failure after global preflight: "
                f"failedWriteWave={wave_number}; "
                f"committedWriteWaves="
                f"{[item['waveNumber'] for item in stats.committed_write_waves]}"
            ) from exc
        except Exception:
            stats.failed_write_wave = evidence
            raise

        stats.write_waves_committed += 1
        stats.writes_succeeded += len(operations)
        stats.committed_write_waves.append(evidence)

        print(
            f"Stage 08 write wave {wave_number:,}: "
            f"operations={len(operations):,}; "
            f"writesSucceeded={stats.writes_succeeded:,}"
        )


def _failure_result(stats: RefreshStats) -> str:
    if stats.failed_write_wave is not None:
        return (
            "REFRESH_ABORTED_PARTIAL"
            if stats.writes_succeeded
            else "REFRESH_ABORTED_NO_WRITE"
        )
    if not stats.global_preflight_gate_passed:
        return "REFRESH_ABORTED_NO_WRITE"
    return "FAILED"


def _batch_evidence(stats: RefreshStats) -> dict[str, Any]:
    return {
        "firestoreBatchSize": FIRESTORE_BATCH_SIZE,
        "readWaves": stats.read_waves,
        "writeWavesAttempted": stats.write_waves_attempted,
        "writeWavesCommitted": stats.write_waves_committed,
        "committedWriteWaves": list(stats.committed_write_waves),
        "failedWriteWave": stats.failed_write_wave,
        "writeOperationsAttempted": stats.writes_attempted,
        "writeOperationsSucceeded": stats.writes_succeeded,
        "verificationReadWaves": stats.verification_read_waves,
        "preconditionConflictCount": stats.precondition_conflicts,
        "maximumWriteOperationsInAnyBatch": stats.maximum_write_operations_in_any_batch,
        "globalPreflightComplete": stats.global_preflight_complete,
        "globalPreflightGatePassed": stats.global_preflight_gate_passed,
        "perDocumentFallback": False,
    }


ALLOWED_PROJECTS = {"ireps2", "ireps-test", "ireps-5c3e9"}


def run_refresh(
    *,
    project_id: str,
    confirm_project: str,
    service_account_path: Path,
    input_path: Path,
    manifest_path: Path,
    report_dir: Path,
    preflight_only: bool,
    category_package_path: Path | None = None,
    category_package_sha256: str | None = None,
    metadata_contract_path: Path | None = None,
    metadata_contract_sha256: str | None = None,
    june_package_path: Path | None = None,
    june_package_sha256: str | None = None,
) -> Path:
    if project_id != confirm_project:
        raise ValueError("Project confirmation mismatch")
    if not service_account_path.is_file():
        raise FileNotFoundError(f"Service account not found: {service_account_path}")
    june_ids = None
    scope_guard = None
    if june_package_path is not None:
        if any(value is not None for value in (input_path, manifest_path, category_package_path,
                category_package_sha256, metadata_contract_path, metadata_contract_sha256)):
            raise ValueError("June baseline mode cannot use cumulative/Stage06/category inputs")
        from sales_june_baseline import load_june_package, exact_ids, guard_plan
        rows, evidence, june_ids = load_june_package(june_package_path, june_package_sha256, project_id)
        exact_ids((row["masterId"] for row in rows), june_ids)
        scope_guard = lambda selected, plan: guard_plan(selected, plan, june_ids)
    else:
        rows, evidence = load_and_validate(input_path, manifest_path)
    if category_package_path is not None:
        from sales_monthly_categories import load_package
        rows, category_evidence = load_package(category_package_path,
            category_package_sha256, rows, project_id)
        evidence["monthlyCategoryPackage"] = category_evidence
        evidence["governedMonth"] = category_evidence["month"]
        evidence["categoryPackageSha256"] = category_evidence["sha256"]
        evidence["classificationSourceSha256"] = category_evidence["source"]["sha256"]
        if "populationSnapshotSha256" in category_evidence:
            evidence["populationSnapshotSha256"] = category_evidence["populationSnapshotSha256"]
        evidence["categoryExceptions"] = category_evidence["categoryExceptions"]
        # Exception identities are included in the actual read scope; there is
        # no uninspected identity allowance for population publication.
        evidence["categoryExceptionDocumentIds"] = []
    elif category_package_sha256:
        raise ValueError("Category SHA requires a category package")
    if metadata_contract_path:
        if category_package_path:
            raise ValueError("Category package already owns metadata; no separate metadata contract")
        from sales_monthly_categories import load_metadata_contract
        evidence["metadataContract"] = load_metadata_contract(metadata_contract_path,
            metadata_contract_sha256, rows, project_id)
    elif metadata_contract_sha256:
        raise ValueError("Metadata SHA requires a metadata contract")
    if not preflight_only and not category_package_path and not metadata_contract_path and not june_package_path:
        raise ValueError("Material refresh requires an approved metadata contract; preflight remains read-only")
    scope_ids = sorted(str(row["masterId"]) for row in rows)
    evidence["scopeDocumentIds"] = scope_ids
    evidence["scopeDocumentIdsSha256"] = canonical_sha256(scope_ids)
    evidence["scopeRecordCount"] = len(scope_ids)
    try:
        sa = json.loads(service_account_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("Service account is not valid JSON") from exc
    if safe_str(sa.get("project_id")) != project_id:
        raise ValueError("Service-account project mismatch")

    try:
        from google.cloud import firestore
        from google.cloud.firestore_v1 import LastUpdateOption
        from google.api_core import exceptions as google_exceptions
        from google.oauth2 import service_account
    except ImportError as exc:
        raise RuntimeError("google-cloud-firestore/google-auth are required for Stage 08 refresh") from exc

    credentials = service_account.Credentials.from_service_account_file(str(service_account_path))
    db = firestore.Client(project=project_id, credentials=credentials)
    collection = db.collection(COLLECTION)
    stats = RefreshStats(rows=len(rows))
    preserved_before: dict[str, str] = {}
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"sales_all_meters_refresh__{project_id}__{run_id}.json"
    report: dict[str, Any] = {
        "stage": "08", "script": "08_upload_sales_all_meters.py",
        "operation": "sales_all_meters_refresh", "mode": "refresh",
        "preflightOnly": preflight_only, "projectId": project_id,
        "collection": COLLECTION, "startedAt": datetime.now(UTC).isoformat(),
        "sourceEvidence": evidence, "status": "STARTED", "result": "STARTED",
        "firestoreBatchSize": FIRESTORE_BATCH_SIZE,
    }

    concurrency_exceptions = tuple(
        exc for exc in (
            google_exceptions.AlreadyExists,
            google_exceptions.Aborted,
            getattr(google_exceptions, "FailedPrecondition", None),
        ) if exc is not None
    )
    systemic_exceptions = (
        google_exceptions.Unauthenticated,
        google_exceptions.PermissionDenied,
        google_exceptions.ServiceUnavailable,
        google_exceptions.DeadlineExceeded,
    )

    def write_report() -> None:
        report["finishedAt"] = datetime.now(UTC).isoformat()
        temp = report_path.with_suffix(".json.tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, indent=2, sort_keys=True, default=str)
            f.write("\n")
        temp.replace(report_path)

    try:
        # A category migration captures typed before-images from the same reads
        # used for global classification. No additional Firestore reads or
        # snapshots retained in the compact plan are needed.
        recovery_path = report_path.with_suffix(".before.jsonl")
        recovery_stream = None
        recovery_count = 0
        def capture_before_image(item, snapshot):
            nonlocal recovery_count
            from sales_monthly_categories import encode_before_image
            recovery_stream.write(json.dumps(encode_before_image(item, snapshot),
                sort_keys=True, separators=(",", ":")) + "\n")
            recovery_count += 1
        try:
            if june_package_path is not None or ((category_package_path is not None or metadata_contract_path is not None) and not preflight_only):
                recovery_stream = recovery_path.open("x", encoding="utf-8", newline="\n")
            if june_ids is not None:
                exact_ids((row["masterId"] for row in rows), june_ids)
            global_plan = classify_all(
                db=db,
                collection=collection,
                rows=rows,
                stats=stats,
                preserved_before=preserved_before,
                recovery_sink=capture_before_image if recovery_stream else None,
            )
        finally:
            if recovery_stream:
                recovery_stream.flush()
                os.fsync(recovery_stream.fileno())
                recovery_stream.close()
                report["recoveryEvidence"] = {"path": str(recovery_path),
                    "sha256": _sha(recovery_path), "records": recovery_count,
                    "complete": recovery_count == len(rows),
                    "format": "Firestore-protobuf-JSONL", "remoteBackup": False}
        evaluate_global_gate(
            rows=rows,
            plan=global_plan,
            stats=stats,
        )
        if scope_guard is not None:
            scope_guard(rows, global_plan)

        if recovery_stream is not None and (recovery_count != len(rows)
                or report.get("recoveryEvidence", {}).get("complete") is not True):
            raise RuntimeError("Recovery before-image accounting incomplete; writes blocked")

        if recovery_stream is not None:
            from sales_monthly_categories import encode_write_plan
            plan_path = report_path.with_suffix(".plan.jsonl")
            item_by_id = {str(item["masterId"]): item for item in rows}
            plan_count = 0
            with plan_path.open("x", encoding="utf-8", newline="\n") as stream:
                for wave in global_plan:
                    for decision in wave["decisions"]:
                        stream.write(json.dumps(encode_write_plan(decision,
                            item_by_id[decision["masterId"]]), sort_keys=True, separators=(",", ":")) + "\n")
                        plan_count += 1
                stream.flush()
                os.fsync(stream.fileno())
            report["planEvidence"] = {"path": str(plan_path), "sha256": _sha(plan_path),
                "records": plan_count, "complete": plan_count == len(rows),
                "scopeDocumentIdsSha256": evidence["scopeDocumentIdsSha256"],
                "categoryPackageSha256": evidence.get("categoryPackageSha256"),
                "format": "Firestore-protobuf-patch-JSONL"}
            if plan_count != len(rows):
                raise RuntimeError("Recovery planned-patch accounting incomplete; writes blocked")
            # Persist the admission evidence before the first commit, so a
            # process interruption still leaves reviewable recovery bindings.
            write_report()

        if not preflight_only:
            execute_global_plan(
                db=db,
                collection=collection,
                rows=rows,
                plan=global_plan,
                stats=stats,
                last_update_option_cls=LastUpdateOption,
                concurrency_exceptions=concurrency_exceptions,
                scope_guard=scope_guard,
            )

        verification: dict[str, Any] = {"status": "NOT_RUN" if preflight_only else "PASS"}
        if not preflight_only:
            checked = 0
            preserved_checked = 0
            preservation_pairs_before: list[tuple[str, str]] = []
            preservation_pairs_after: list[tuple[str, str]] = []
            item_by_id = {str(item["masterId"]): item for item in rows}
            ordered_ids = list(item_by_id)
            for id_wave in _chunks(ordered_ids):
                refs = [collection.document(doc_id) for doc_id in id_wave]
                snapshots = _bulk_snapshots(db, refs)
                stats.verification_read_waves += 1
                for doc_id, ref in zip(id_wave, refs):
                    snap = _snapshot_for(snapshots, ref)
                    if snap is None or not snap.exists:
                        raise RuntimeError(f"Post-write verification missing {doc_id}")
                    payload = snap.to_dict() or {}
                    item = item_by_id[doc_id]
                    if "categoryRefresh" in item:
                        from sales_monthly_categories import verify
                        verify(payload, item)
                    else:
                        _verify_doc(payload, item["expected"])
                        if "metadataRefresh" in item:
                            from sales_monthly_categories import verify_metadata
                            verify_metadata(payload, item)
                    if doc_id in preserved_before:
                        before_hash = preserved_before[doc_id]
                        if "categoryRefresh" in item:
                            from sales_monthly_categories import preserved_hash
                            context = item["categoryRefresh"]
                            after_hash = preserved_hash(payload, context["month"] if context["category"] is not None else None)
                        else:
                            after_hash = _projection_hash({k: v for k, v in payload.items() if k != "metadata"}) if "metadataRefresh" in item else _projection_hash(payload)
                        if before_hash != after_hash:
                            raise RuntimeError(
                                f"Stage 08 changed non-pipeline-owned fields for {doc_id}"
                            )
                        preservation_pairs_before.append((doc_id, before_hash))
                        preservation_pairs_after.append((doc_id, after_hash))
                        preserved_checked += 1
                    checked += 1
                print(f"Stage 08 verification {checked:,}/{len(rows):,}")
            verification = {
                "status": "PASS",
                "documentsVerified": checked,
                "scope": "INPUT_DOCUMENT_IDS_ONLY",
                "preservationVerifiedExistingDocuments": preserved_checked,
                "preservedProjectionBeforeSha256": canonical_sha256(preservation_pairs_before),
                "preservedProjectionAfterSha256": canonical_sha256(preservation_pairs_after),
                "readWaves": stats.verification_read_waves,
                "maximumReferencesPerWave": FIRESTORE_BATCH_SIZE,
            }

        batch_evidence = _batch_evidence(stats)
        report.update({
            "rowsRead": stats.rows,
            "recordsInspected": stats.inspected,
            "createdCount": stats.created,
            "updatedCount": stats.updated,
            "unchangedCount": stats.unchanged,
            "conflictCount": stats.conflicts,
            "failedCount": stats.failed,
            "writeAttemptCount": stats.writes_attempted,
            "writeSuccessCount": stats.writes_succeeded,
            "conflicts": stats.conflict_records,
            "failedRecords": stats.failure_records,
            "verification": verification,
            "batchEvidence": batch_evidence,
            "stats": {
                "rows": stats.rows,
                "inspected": stats.inspected,
                "created": stats.created,
                "updated": stats.updated,
                "unchanged": stats.unchanged,
                "conflicts": stats.conflicts,
                "failed": stats.failed,
                "writes_attempted": stats.writes_attempted,
                "writes_succeeded": stats.writes_succeeded,
            },
            "firestoreWrites": stats.writes_succeeded,
            "globalPreflight": {
                "complete": stats.global_preflight_complete,
                "gatePassed": stats.global_preflight_gate_passed,
            },
            "updatesOrCreatesOnly": True,
            "deletes": 0,
            "preservedOperationalFields": [
                "master.visibility", "tbRefs", "geofenceRefs", "all non-pipeline-owned fields"
            ],
            "status": "PASS",
            "result": "PREFLIGHT_PASS" if preflight_only else "REFRESH_VERIFIED",
        })
        return report_path
    except Exception as exc:
        report.update({
            "rowsRead": stats.rows,
            "recordsInspected": stats.inspected,
            "createdCount": stats.created,
            "updatedCount": stats.updated,
            "unchangedCount": stats.unchanged,
            "conflictCount": stats.conflicts,
            "failedCount": stats.failed,
            "writeAttemptCount": stats.writes_attempted,
            "writeSuccessCount": stats.writes_succeeded,
            "conflicts": stats.conflict_records,
            "failedRecords": stats.failure_records,
            "batchEvidence": _batch_evidence(stats),
            "firestoreWrites": stats.writes_succeeded,
            "globalPreflight": {
                "complete": stats.global_preflight_complete,
                "gatePassed": stats.global_preflight_gate_passed,
            },
            "status": "FAIL",
            "result": _failure_result(stats),
            "errorType": type(exc).__name__, "error": str(exc),
            "deletes": 0,
        })
        raise
    finally:
        try:
            write_report()
        finally:
            try:
                db.close()
            except Exception:
                pass
