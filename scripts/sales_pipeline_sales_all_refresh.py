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
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
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
    expected = BASE_COLUMNS + RICH_COLUMNS + ADDRESS_STAGING_COLUMNS + amount_columns + unit_columns
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
        "csvSha256": _sha(path),
        "manifestSha256": _sha(manifest_path),
        "stage06BuildFingerprint": manifest.get("buildFingerprint"),
        "rows": len(rows),
        "totalAmountC": total_sales,
        "totalUnits": str(total_units),
        "addressEnrichment": dict(address_contract),
    }


def _conflict(existing: Mapping[str, Any], expected: Mapping[str, Any]) -> str | None:
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
        wanted = expected.get(field)
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
    doc_id = str(item["masterId"])
    expected = item["expected"]
    if snapshot is None or not snapshot.exists:
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
    return {
        "masterId": doc_id,
        "classification": "UPDATED" if changes else "UNCHANGED",
        "reason": None,
        "updates": changes,
        "snapshot": snapshot,
        "preservedHash": _projection_hash(payload),
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
            snapshot = operation["snapshot"]
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


def run_refresh(
    *,
    project_id: str,
    confirm_project: str,
    service_account_path: Path,
    input_path: Path,
    manifest_path: Path,
    report_dir: Path,
    preflight_only: bool,
) -> Path:
    if project_id != confirm_project:
        raise ValueError("Project confirmation mismatch")
    if not service_account_path.is_file():
        raise FileNotFoundError(f"Service account not found: {service_account_path}")
    rows, evidence = load_and_validate(input_path, manifest_path)
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
        processed = 0
        for wave_items in _chunks(rows):
            refs = [collection.document(str(item["masterId"])) for item in wave_items]
            snapshots = _bulk_snapshots(db, refs)
            stats.read_waves += 1
            classified = [
                _classify_snapshot(item, _snapshot_for(snapshots, ref))
                for item, ref in zip(wave_items, refs)
            ]

            if preflight_only:
                for decision in classified:
                    _add_classification(stats, decision)
            else:
                try:
                    _write_operations_for_wave(
                        db=db,
                        collection=collection,
                        wave_items=wave_items,
                        classified=classified,
                        stats=stats,
                        preserved_before=preserved_before,
                        last_update_option_cls=LastUpdateOption,
                        concurrency_exceptions=concurrency_exceptions,
                    )
                except systemic_exceptions:
                    raise

            processed += len(wave_items)
            print(
                f"Stage 08 refresh progress {processed:,}/{len(rows):,}: "
                f"create={stats.created:,} update={stats.updated:,} unchanged={stats.unchanged:,} "
                f"conflict={stats.conflicts:,} failed={stats.failed:,}; "
                f"readWaves={stats.read_waves:,} writeWaves={stats.write_waves_committed:,}"
            )

        classified_count = stats.created + stats.updated + stats.unchanged + stats.conflicts + stats.failed
        if classified_count != stats.rows or stats.inspected != stats.rows:
            raise RuntimeError(
                f"Stage 08 refresh accounting imbalance: rows={stats.rows}; "
                f"classified={classified_count}; inspected={stats.inspected}"
            )
        if stats.conflicts or stats.failed:
            raise RuntimeError(
                f"Stage 08 refresh blocked/incomplete: conflicts={stats.conflicts}; failed={stats.failed}"
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
                    _verify_doc(payload, item_by_id[doc_id]["expected"])
                    if doc_id in preserved_before:
                        before_hash = preserved_before[doc_id]
                        after_hash = _projection_hash(payload)
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

        batch_evidence = {
            "firestoreBatchSize": FIRESTORE_BATCH_SIZE,
            "readWaves": stats.read_waves,
            "writeWavesAttempted": stats.write_waves_attempted,
            "writeWavesCommitted": stats.write_waves_committed,
            "writeOperationsAttempted": stats.writes_attempted,
            "writeOperationsSucceeded": stats.writes_succeeded,
            "verificationReadWaves": stats.verification_read_waves,
            "preconditionConflictCount": stats.precondition_conflicts,
            "maximumWriteOperationsInAnyBatch": stats.maximum_write_operations_in_any_batch,
            "perDocumentFallback": False,
        }
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
            "firestoreWrites": 0 if preflight_only else stats.writes_succeeded,
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
            "batchEvidence": {
                "firestoreBatchSize": FIRESTORE_BATCH_SIZE,
                "readWaves": stats.read_waves,
                "writeWavesAttempted": stats.write_waves_attempted,
                "writeWavesCommitted": stats.write_waves_committed,
                "writeOperationsAttempted": stats.writes_attempted,
                "writeOperationsSucceeded": stats.writes_succeeded,
                "verificationReadWaves": stats.verification_read_waves,
                "preconditionConflictCount": stats.precondition_conflicts,
                "maximumWriteOperationsInAnyBatch": stats.maximum_write_operations_in_any_batch,
                "perDocumentFallback": False,
            },
            "status": "FAIL", "result": "FAILED",
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

