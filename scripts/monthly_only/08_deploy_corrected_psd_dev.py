#!/usr/bin/env python3
"""
Preflight, deploy, and verify the corrected Endumeni Sales PSD in ireps2 DEV.

Target:
- Firebase project: ireps2
- Firestore collection: demo_sales_meters
- Full PSD: 10,216 documents
- Exact-GPS documents: 7,583
- Existing verified pilot: 20 identical documents
- Remaining documents expected before first full execution: 10,196
- Firestore document ID: plain MeterNumber
- Geometry fields: forbidden

Run --preflight-only first. It performs Firestore reads but no writes.
Execute mode writes only MISSING documents; it never overwrites IDENTICAL
documents and refuses to proceed if any existing document is DIFFERENT.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


EXPECTED_PROJECT = "ireps2"
COLLECTION = "demo_sales_meters"
CONFIRM_TOKEN = "UPLOAD_REMAINING_CORRECTED_SALES_PSD_TO_IREPS2"
FORBIDDEN_GEOMETRY_KEYS = {"geometry", "geometryjson"}
DEFAULT_READ_BATCH_SIZE = 400
DEFAULT_WRITE_BATCH_SIZE = 400


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--expected-input-sha256", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--confirm-project", required=True)
    parser.add_argument("--service-account", required=True, type=Path)
    parser.add_argument("--report-file", required=True, type=Path)
    parser.add_argument("--expected-total-count", type=int, default=10_216)
    parser.add_argument("--expected-exact-gps-count", type=int, default=7_583)
    parser.add_argument("--expected-existing-identical", type=int, default=20)
    parser.add_argument("--expected-missing", type=int, default=10_196)
    parser.add_argument(
        "--read-batch-size",
        type=int,
        default=DEFAULT_READ_BATCH_SIZE,
    )
    parser.add_argument(
        "--write-batch-size",
        type=int,
        default=DEFAULT_WRITE_BATCH_SIZE,
    )

    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--preflight-only", action="store_true")
    operation.add_argument("--execute-rest", action="store_true")

    parser.add_argument(
        "--confirm",
        default="",
        help=(
            "Required only for --execute-rest. Must equal "
            f"{CONFIRM_TOKEN}."
        ),
    )
    return parser.parse_args()


def clean_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_sha256(value: str) -> str:
    cleaned = clean_text(value).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", cleaned):
        raise ValueError(
            "Expected input SHA-256 must contain 64 hexadecimal characters"
        )
    return cleaned


def find_forbidden_geometry_keys(
    value: Any,
    path: str = "",
) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if str(key).lower() in FORBIDDEN_GEOMETRY_KEYS:
                found.append(child_path)
            found.extend(
                find_forbidden_geometry_keys(child, child_path)
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(
                find_forbidden_geometry_keys(
                    child,
                    f"{path}[{index}]",
                )
            )
    return found


def finite_coordinate(value: Any, low: float, high: float) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and low <= number <= high


def validate_psd_document(
    data: dict[str, Any],
    line_number: int,
) -> tuple[str, bool]:
    meter_number = clean_text(data.get("MeterNumber"))
    if not meter_number:
        raise ValueError(
            f"Blank MeterNumber at PSD line {line_number}"
        )

    forbidden = find_forbidden_geometry_keys(data)
    if forbidden:
        raise ValueError(
            f"Geometry found in PSD document {meter_number}: "
            f"{forbidden[:5]}"
        )

    candidates = data.get("ErfCandidates")
    if not isinstance(candidates, list):
        raise ValueError(
            f"ErfCandidates is not an array for meter {meter_number}"
        )

    candidate_count = data.get("ErfCandidateCount")
    if candidate_count != len(candidates):
        raise ValueError(
            f"ErfCandidateCount mismatch for meter {meter_number}"
        )

    if len(candidates) > 1:
        raise ValueError(
            f"Meter {meter_number} has more than one ERF candidate"
        )

    has_gps = data.get("HasUsableGps") is True

    if has_gps:
        if len(candidates) != 1:
            raise ValueError(
                f"GPS meter {meter_number} does not contain one candidate"
            )
        if clean_text(data.get("GpsMatchStatus")) != "MATCHED_SINGLE_GPS":
            raise ValueError(
                f"GPS meter {meter_number} is not MATCHED_SINGLE_GPS"
            )

        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise ValueError(
                f"Invalid candidate for meter {meter_number}"
            )
        if clean_text(candidate.get("LmPcode")).upper() != "ZA5241":
            raise ValueError(
                f"Wrong LM pCode for meter {meter_number}"
            )
        if not clean_text(candidate.get("ErfId")):
            raise ValueError(
                f"Blank ErfId for meter {meter_number}"
            )
        if not clean_text(candidate.get("WardNumber")):
            raise ValueError(
                f"Blank WardNumber for meter {meter_number}"
            )
        if not clean_text(candidate.get("WardPcode")):
            raise ValueError(
                f"Blank WardPcode for meter {meter_number}"
            )
        if not finite_coordinate(
            candidate.get("Latitude"), -90, 90
        ):
            raise ValueError(
                f"Invalid latitude for meter {meter_number}"
            )
        if not finite_coordinate(
            candidate.get("Longitude"), -180, 180
        ):
            raise ValueError(
                f"Invalid longitude for meter {meter_number}"
            )
    else:
        if candidates:
            raise ValueError(
                f"Non-GPS meter {meter_number} contains a candidate"
            )
        if candidate_count != 0:
            raise ValueError(
                f"Non-GPS meter {meter_number} has nonzero candidate count"
            )

    sales = data.get("Sales")
    units = data.get("Units")
    if not isinstance(sales, dict) or not isinstance(units, dict):
        raise ValueError(
            f"Sales or Units shape invalid for meter {meter_number}"
        )
    if not isinstance(data.get("trnBatchIds"), list):
        raise ValueError(
            f"trnBatchIds is not an array for meter {meter_number}"
        )

    return meter_number, has_gps


def load_full_psd(
    path: Path,
    expected_total: int,
    expected_exact_gps: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records: list[dict[str, Any]] = []
    document_ids: set[str] = set()
    exact_gps_count = 0
    status_counts: Counter[str] = Counter()
    root_field_set: set[str] | None = None

    with path.open("r", encoding="utf-8-sig") as source:
        for line_number, raw_line in enumerate(source, start=1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            try:
                data = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON at line {line_number}: {exc.msg}"
                ) from exc

            if not isinstance(data, dict):
                raise ValueError(
                    f"PSD line {line_number} is not an object"
                )

            meter_number, has_gps = validate_psd_document(
                data,
                line_number,
            )

            if meter_number in document_ids:
                raise ValueError(
                    f"Duplicate MeterNumber/document ID: {meter_number}"
                )
            document_ids.add(meter_number)

            current_fields = set(data)
            if root_field_set is None:
                root_field_set = current_fields
            elif current_fields != root_field_set:
                missing = sorted(root_field_set - current_fields)
                unexpected = sorted(current_fields - root_field_set)
                raise ValueError(
                    f"Root-field shape mismatch for meter {meter_number}. "
                    f"Missing={missing}; unexpected={unexpected}"
                )

            exact_gps_count += int(has_gps)
            status_counts[clean_text(data.get("GpsMatchStatus"))] += 1
            records.append(
                {
                    "docId": meter_number,
                    "data": data,
                }
            )

            if len(records) % 1_000 == 0:
                print(
                    f"[LOCAL VALIDATION] {len(records):,} PSD documents; "
                    f"exact-GPS {exact_gps_count:,}"
                )

    if len(records) != expected_total:
        raise ValueError(
            f"Full PSD count mismatch. Expected {expected_total:,}, "
            f"found {len(records):,}."
        )
    if exact_gps_count != expected_exact_gps:
        raise ValueError(
            f"Exact-GPS count mismatch. Expected {expected_exact_gps:,}, "
            f"found {exact_gps_count:,}."
        )

    return records, {
        "records": len(records),
        "uniqueDocumentIds": len(document_ids),
        "exactGps": exact_gps_count,
        "withoutExactGps": len(records) - exact_gps_count,
        "statusCounts": dict(sorted(status_counts.items())),
        "rootFields": sorted(root_field_set or set()),
    }


def load_service_account_identity(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(
            f"Service account not found: {path}"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid service-account JSON: {exc.msg}"
        ) from exc

    project_id = clean_text(value.get("project_id"))
    client_email = clean_text(value.get("client_email"))
    if not project_id or not client_email:
        raise ValueError(
            "Service account is missing project_id or client_email"
        )

    return {
        "projectId": project_id,
        "clientEmail": client_email,
    }


def comparable_firestore_data(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: child
        for key, child in value.items()
        if key != "loadedAt"
    }


def chunks(
    values: list[dict[str, Any]],
    size: int,
) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def inspect_existing(
    db: Any,
    collection: Any,
    records: list[dict[str, Any]],
    read_batch_size: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    identical = 0
    different = 0
    missing = 0
    examples: list[dict[str, Any]] = []
    missing_records: list[dict[str, Any]] = []
    checked = 0

    for batch_number, batch_records in enumerate(
        chunks(records, read_batch_size),
        start=1,
    ):
        refs = [
            collection.document(record["docId"])
            for record in batch_records
        ]
        snapshots = list(db.get_all(refs))
        snapshot_by_id = {
            snapshot.id: snapshot
            for snapshot in snapshots
        }

        for record in batch_records:
            snapshot = snapshot_by_id.get(record["docId"])
            if snapshot is None or not snapshot.exists:
                missing += 1
                state = "MISSING"
                missing_records.append(record)
            else:
                actual = snapshot.to_dict() or {}
                if comparable_firestore_data(actual) == record["data"]:
                    identical += 1
                    state = "IDENTICAL"
                else:
                    different += 1
                    state = "DIFFERENT"

            if state != "IDENTICAL" and len(examples) < 20:
                examples.append(
                    {
                        "docId": record["docId"],
                        "state": state,
                    }
                )

            checked += 1

        print(
            f"[FIRESTORE PREFLIGHT] Batch {batch_number}: "
            f"checked {checked:,}/{len(records):,}; "
            f"identical={identical:,}, "
            f"missing={missing:,}, "
            f"different={different:,}"
        )

    return {
        "identical": identical,
        "different": different,
        "missing": missing,
        "examples": examples,
    }, missing_records


def verify_all(
    db: Any,
    collection: Any,
    records: list[dict[str, Any]],
    read_batch_size: int,
) -> dict[str, Any]:
    verified = 0
    failures: list[dict[str, Any]] = []
    checked = 0

    for batch_number, batch_records in enumerate(
        chunks(records, read_batch_size),
        start=1,
    ):
        refs = [
            collection.document(record["docId"])
            for record in batch_records
        ]
        snapshots = list(db.get_all(refs))
        snapshot_by_id = {
            snapshot.id: snapshot
            for snapshot in snapshots
        }

        for record in batch_records:
            snapshot = snapshot_by_id.get(record["docId"])
            if snapshot is None or not snapshot.exists:
                failures.append(
                    {
                        "docId": record["docId"],
                        "reason": "MISSING_AFTER_WRITE",
                    }
                )
            else:
                actual = snapshot.to_dict() or {}
                if "loadedAt" not in actual:
                    failures.append(
                        {
                            "docId": record["docId"],
                            "reason": "LOADED_AT_MISSING",
                        }
                    )
                elif comparable_firestore_data(actual) != record["data"]:
                    differing = sorted(
                        key
                        for key in (
                            set(comparable_firestore_data(actual))
                            | set(record["data"])
                        )
                        if comparable_firestore_data(actual).get(key)
                        != record["data"].get(key)
                    )
                    failures.append(
                        {
                            "docId": record["docId"],
                            "reason": "DATA_MISMATCH",
                            "differingFields": differing,
                        }
                    )
                else:
                    verified += 1

            checked += 1

        print(
            f"[FULL VERIFY] Batch {batch_number}: "
            f"checked {checked:,}/{len(records):,}; "
            f"verified={verified:,}; failures={len(failures):,}"
        )

    if failures:
        raise ValueError(
            f"Full read-back verification failed for "
            f"{len(failures):,} document(s): {failures[:5]}"
        )

    return {
        "verified": verified,
        "failures": failures,
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n"
    )
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    args = parse_args()

    report: dict[str, Any] = {
        "script": "08_deploy_corrected_psd_dev.py",
        "startedAt": utc_iso(),
        "targetProject": clean_text(args.project_id),
        "targetCollection": COLLECTION,
        "operation": (
            "execute-rest"
            if args.execute_rest
            else "preflight-only"
        ),
        "firestoreReadsPerformed": False,
        "firestoreWritesPerformed": False,
        "status": "RUNNING",
    }

    firebase_admin_module = None
    firebase_app = None

    try:
        if args.read_batch_size <= 0:
            raise ValueError("--read-batch-size must be positive")
        if not 1 <= args.write_batch_size <= 500:
            raise ValueError(
                "--write-batch-size must be between 1 and 500"
            )

        input_path = args.input.resolve()
        if not input_path.is_file():
            raise FileNotFoundError(
                f"Full PSD input not found: {input_path}"
            )

        expected_sha = require_sha256(
            args.expected_input_sha256
        )
        actual_sha = sha256_file(input_path)
        if actual_sha.lower() != expected_sha:
            raise ValueError(
                f"Full PSD SHA-256 mismatch. "
                f"Expected {expected_sha}, found {actual_sha}."
            )

        project_id = clean_text(args.project_id)
        confirm_project = clean_text(args.confirm_project)
        if project_id != EXPECTED_PROJECT:
            raise ValueError(
                f"This deployer is locked to {EXPECTED_PROJECT}; "
                f"received {project_id!r}."
            )
        if confirm_project != project_id:
            raise ValueError(
                "--confirm-project must exactly match --project-id"
            )

        if args.execute_rest and clean_text(args.confirm) != CONFIRM_TOKEN:
            raise ValueError(
                f"--execute-rest requires --confirm {CONFIRM_TOKEN}"
            )
        if args.preflight_only and clean_text(args.confirm):
            raise ValueError(
                "--confirm must not be supplied in preflight-only mode"
            )

        credential_identity = load_service_account_identity(
            args.service_account.resolve()
        )
        if credential_identity["projectId"] != project_id:
            raise ValueError(
                "Service-account project mismatch. "
                f"Expected {project_id}, found "
                f"{credential_identity['projectId']}."
            )

        print("")
        print("============================================================")
        print("CORRECTED SALES PSD -> FIRESTORE DEV")
        print("============================================================")
        print(f"Operation:              {report['operation']}")
        print(f"Project:                {project_id}")
        print(f"Collection:             {COLLECTION}")
        print(f"Full PSD input:         {input_path}")
        print(f"Input SHA-256:          {actual_sha}")
        print(f"Expected total:         {args.expected_total_count:,}")
        print(f"Expected exact GPS:     {args.expected_exact_gps_count:,}")
        print("Document ID rule:       plain MeterNumber")
        print("Geometry included:      NO")
        print("Overwrite DIFFERENT:    NEVER")

        print("")
        print("[LOCAL PSD VALIDATION]")
        records, local_stats = load_full_psd(
            input_path,
            args.expected_total_count,
            args.expected_exact_gps_count,
        )
        report.update(
            {
                "inputPath": str(input_path),
                "inputSha256": actual_sha,
                "localValidation": local_stats,
                "credentialProject": credential_identity["projectId"],
                "credentialClientEmail": credential_identity["clientEmail"],
                "documentIdRule": "Plain MeterNumber",
            }
        )

        print("")
        print("[LOCAL PSD SUMMARY]")
        print(f"  documents:           {local_stats['records']:,}")
        print(f"  unique IDs:          {local_stats['uniqueDocumentIds']:,}")
        print(f"  exact-GPS:           {local_stats['exactGps']:,}")
        print(f"  without exact GPS:   {local_stats['withoutExactGps']:,}")
        print("  multiple candidates: 0")
        print("  geometry:            0")

        try:
            import firebase_admin
            from firebase_admin import credentials, firestore
        except ImportError as exc:
            raise RuntimeError(
                "firebase-admin is not installed. "
                "Run: python -m pip install firebase-admin"
            ) from exc

        firebase_admin_module = firebase_admin
        credential = credentials.Certificate(
            str(args.service_account.resolve())
        )
        firebase_app = firebase_admin.initialize_app(
            credential,
            {"projectId": project_id},
        )
        db = firestore.client()
        collection = db.collection(COLLECTION)

        report["firestoreReadsPerformed"] = True
        existing, missing_records = inspect_existing(
            db,
            collection,
            records,
            args.read_batch_size,
        )
        report["existingState"] = existing

        print("")
        print("[FULL PREFLIGHT SUMMARY]")
        print(f"  identical existing: {existing['identical']:,}")
        print(f"  different existing: {existing['different']:,}")
        print(f"  missing:            {existing['missing']:,}")

        if existing["different"] != 0:
            raise ValueError(
                "Deployment blocked: one or more existing documents "
                "are DIFFERENT from the corrected PSD."
            )
        if existing["identical"] != args.expected_existing_identical:
            raise ValueError(
                "Identical-existing count mismatch. "
                f"Expected {args.expected_existing_identical:,}, "
                f"found {existing['identical']:,}."
            )
        if existing["missing"] != args.expected_missing:
            raise ValueError(
                "Missing-document count mismatch. "
                f"Expected {args.expected_missing:,}, "
                f"found {existing['missing']:,}."
            )
        if (
            existing["identical"]
            + existing["missing"]
            + existing["different"]
            != len(records)
        ):
            raise ValueError(
                "Firestore preflight accounting does not equal PSD count"
            )

        if args.preflight_only:
            report.update(
                {
                    "status": "PASS",
                    "result": "FULL_PREFLIGHT_OK",
                }
            )
            print("")
            print("============================================================")
            print("FULL DEPLOYMENT PREFLIGHT PASSED")
            print("============================================================")
            print("Firestore reads performed:  YES")
            print("Firestore writes performed: NO")
            return 0

        print("")
        print(
            f"[UPLOAD] Writing only {len(missing_records):,} "
            "missing documents"
        )

        written = 0
        batches_committed = 0

        for batch_records in chunks(
            missing_records,
            args.write_batch_size,
        ):
            batch = db.batch()
            for record in batch_records:
                data = dict(record["data"])
                data["loadedAt"] = firestore.SERVER_TIMESTAMP
                batch.set(
                    collection.document(record["docId"]),
                    data,
                )

            batch.commit()
            batches_committed += 1
            written += len(batch_records)

            print(
                f"[UPLOAD] Batch {batches_committed}: "
                f"wrote {len(batch_records):,}; "
                f"total {written:,}/{len(missing_records):,}"
            )

        report["firestoreWritesPerformed"] = written > 0
        report["documentsWritten"] = written
        report["writeBatchesCommitted"] = batches_committed

        if written != args.expected_missing:
            raise ValueError(
                f"Write count mismatch. Expected {args.expected_missing:,}, "
                f"wrote {written:,}."
            )

        print("")
        print("[VERIFY] Reading back all 10,216 corrected documents")
        verification = verify_all(
            db,
            collection,
            records,
            args.read_batch_size,
        )

        report.update(
            {
                "verification": verification,
                "status": "PASS",
                "result": "FULL_DEPLOYMENT_VERIFIED",
            }
        )

        print("")
        print("============================================================")
        print("FULL DEPLOYMENT AND READ-BACK VERIFICATION PASSED")
        print("============================================================")
        print(f"Existing pilot preserved: {existing['identical']:,}")
        print(f"Documents written:        {written:,}")
        print(f"Documents verified:       {verification['verified']:,}")
        print("Firestore reads performed:  YES")
        print("Firestore writes performed: YES")
        return 0

    except Exception as exc:
        report.update(
            {
                "status": "FAIL",
                "result": "FAILED",
                "errorType": type(exc).__name__,
                "error": str(exc),
            }
        )
        print(f"\n[FAILED] {exc}", file=sys.stderr)
        return 1

    finally:
        if firebase_admin_module is not None and firebase_app is not None:
            try:
                firebase_admin_module.delete_app(firebase_app)
            except Exception:
                pass

        report["finishedAt"] = utc_iso()
        try:
            write_report(
                args.report_file,
                report,
            )
            print(f"\n[REPORT] {args.report_file.resolve()}")
        except Exception as report_error:
            print(
                f"[WARN] Could not write report: {report_error}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    raise SystemExit(main())
