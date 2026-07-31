#!/usr/bin/env python3
"""
Strictly upload and verify the 20 exact-GPS Endumeni Sales pilot documents.

Target:
- Firebase project: ireps2
- Firestore collection: demo_sales_meters
- Exactly 20 documents
- All documents must have exact one-to-one GPS enrichment
- No geometry fields

Run --preflight-only first. It performs Firestore reads but no writes.
Write mode requires the exact confirmation token.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXPECTED_PROJECT = "ireps2"
COLLECTION = "demo_sales_meters"
EXPECTED_PILOT_COUNT = 20
CONFIRM_TOKEN = "UPLOAD_20_EXACT_GPS_SALES_PILOT_TO_IREPS2"
FORBIDDEN_GEOMETRY_KEYS = {"geometry", "geometryjson"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--expected-input-sha256", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--confirm-project", required=True)
    parser.add_argument("--service-account", required=True, type=Path)
    parser.add_argument("--report-file", required=True, type=Path)

    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--preflight-only", action="store_true")
    operation.add_argument("--execute-pilot", action="store_true")

    parser.add_argument(
        "--confirm",
        default="",
        help=(
            "Required only for --execute-pilot. Must equal "
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


def validate_record(
    wrapper: dict[str, Any],
    line_number: int,
) -> tuple[str, dict[str, Any]]:
    doc_id = clean_text(wrapper.get("docId"))
    data = wrapper.get("data")

    if not doc_id or not isinstance(data, dict):
        raise ValueError(
            f"Invalid upload wrapper at line {line_number}"
        )

    meter_number = clean_text(data.get("MeterNumber"))
    expected_doc_id = meter_number
    if not meter_number or doc_id != expected_doc_id:
        raise ValueError(
            f"Document ID mismatch at line {line_number}. "
            f"Expected {expected_doc_id!r}, found {doc_id!r}."
        )

    forbidden = find_forbidden_geometry_keys(data)
    if forbidden:
        raise ValueError(
            f"Geometry found in pilot document {doc_id}: "
            f"{forbidden[:5]}"
        )

    if data.get("HasUsableGps") is not True:
        raise ValueError(
            f"Pilot document {doc_id} has no usable GPS"
        )
    if clean_text(data.get("GpsMatchStatus")) != "MATCHED_SINGLE_GPS":
        raise ValueError(
            f"Pilot document {doc_id} is not MATCHED_SINGLE_GPS"
        )
    if data.get("ErfCandidateCount") != 1:
        raise ValueError(
            f"Pilot document {doc_id} does not declare one candidate"
        )

    candidates = data.get("ErfCandidates")
    if not isinstance(candidates, list) or len(candidates) != 1:
        raise ValueError(
            f"Pilot document {doc_id} does not contain one candidate"
        )

    candidate = candidates[0]
    if not isinstance(candidate, dict):
        raise ValueError(
            f"Invalid candidate in pilot document {doc_id}"
        )

    if clean_text(candidate.get("LmPcode")).upper() != "ZA5241":
        raise ValueError(
            f"Wrong LM in pilot document {doc_id}"
        )
    if not clean_text(candidate.get("ErfId")):
        raise ValueError(
            f"Blank ErfId in pilot document {doc_id}"
        )
    if not clean_text(candidate.get("WardPcode")):
        raise ValueError(
            f"Blank WardPcode in pilot document {doc_id}"
        )
    if not finite_coordinate(candidate.get("Latitude"), -90, 90):
        raise ValueError(
            f"Invalid latitude in pilot document {doc_id}"
        )
    if not finite_coordinate(candidate.get("Longitude"), -180, 180):
        raise ValueError(
            f"Invalid longitude in pilot document {doc_id}"
        )

    return doc_id, data


def load_pilot(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    with path.open("r", encoding="utf-8-sig") as source:
        for line_number, raw_line in enumerate(source, start=1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                wrapper = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON at line {line_number}: {exc.msg}"
                ) from exc
            if not isinstance(wrapper, dict):
                raise ValueError(
                    f"Upload line {line_number} is not an object"
                )
            doc_id, data = validate_record(wrapper, line_number)
            records.append({"docId": doc_id, "data": data})

    if len(records) != EXPECTED_PILOT_COUNT:
        raise ValueError(
            f"Pilot input must contain exactly {EXPECTED_PILOT_COUNT} "
            f"documents; found {len(records)}."
        )

    doc_ids = [record["docId"] for record in records]
    if len(doc_ids) != len(set(doc_ids)):
        raise ValueError("Duplicate document IDs found in pilot input")

    return records


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


def inspect_existing(
    collection: Any,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    identical = 0
    different = 0
    missing = 0
    examples: list[dict[str, Any]] = []

    for index, record in enumerate(records, start=1):
        snapshot = collection.document(record["docId"]).get()
        if not snapshot.exists:
            missing += 1
            state = "MISSING"
        else:
            actual = snapshot.to_dict() or {}
            if comparable_firestore_data(actual) == record["data"]:
                identical += 1
                state = "IDENTICAL"
            else:
                different += 1
                state = "DIFFERENT"

        if state != "IDENTICAL" and len(examples) < 10:
            examples.append(
                {
                    "docId": record["docId"],
                    "state": state,
                }
            )

        print(
            f"[PREFLIGHT READ] {index:,}/{len(records):,} "
            f"{record['docId']} -> {state}"
        )

    return {
        "identical": identical,
        "different": different,
        "missing": missing,
        "examples": examples,
    }


def verify_after_write(
    collection: Any,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    verified = 0
    failures: list[dict[str, Any]] = []

    for index, record in enumerate(records, start=1):
        snapshot = collection.document(record["docId"]).get()
        if not snapshot.exists:
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
                    {
                        key
                        for key in set(comparable_firestore_data(actual))
                        | set(record["data"])
                        if comparable_firestore_data(actual).get(key)
                        != record["data"].get(key)
                    }
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

        print(
            f"[VERIFY READ] {index:,}/{len(records):,} "
            f"{record['docId']}"
        )

    if failures:
        raise ValueError(
            f"Pilot read-back verification failed for "
            f"{len(failures)} document(s): {failures[:5]}"
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
        "script": "07_upload_exact_gps_pilot_dev.py",
        "startedAt": utc_iso(),
        "targetProject": clean_text(args.project_id),
        "targetCollection": COLLECTION,
        "operation": (
            "execute-pilot"
            if args.execute_pilot
            else "preflight-only"
        ),
        "firestoreReadsPerformed": False,
        "firestoreWritesPerformed": False,
        "status": "RUNNING",
    }

    firebase_admin_module = None
    firebase_app = None

    try:
        input_path = args.input.resolve()
        if not input_path.is_file():
            raise FileNotFoundError(
                f"Pilot input not found: {input_path}"
            )

        expected_sha = require_sha256(
            args.expected_input_sha256
        )
        actual_sha = sha256_file(input_path)
        if actual_sha.lower() != expected_sha:
            raise ValueError(
                f"Pilot input SHA-256 mismatch. "
                f"Expected {expected_sha}, found {actual_sha}."
            )

        project_id = clean_text(args.project_id)
        confirm_project = clean_text(args.confirm_project)
        if project_id != EXPECTED_PROJECT:
            raise ValueError(
                f"This pilot uploader is locked to {EXPECTED_PROJECT}; "
                f"received {project_id!r}."
            )
        if confirm_project != project_id:
            raise ValueError(
                "--confirm-project must exactly match --project-id"
            )

        if args.execute_pilot and clean_text(args.confirm) != CONFIRM_TOKEN:
            raise ValueError(
                f"--execute-pilot requires --confirm {CONFIRM_TOKEN}"
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

        records = load_pilot(input_path)

        report.update(
            {
                "inputPath": str(input_path),
                "inputSha256": actual_sha,
                "pilotDocumentCount": len(records),
                "uniqueDocumentIds": len(
                    {record["docId"] for record in records}
                ),
                "documentIdRule": "Plain MeterNumber",
                "credentialProject": credential_identity["projectId"],
                "credentialClientEmail": credential_identity["clientEmail"],
            }
        )

        print("")
        print("============================================================")
        print("EXACT-GPS SALES PILOT -> FIRESTORE")
        print("============================================================")
        print(f"Operation:          {report['operation']}")
        print(f"Project:            {project_id}")
        print(f"Collection:         {COLLECTION}")
        print(f"Pilot input:        {input_path}")
        print(f"Input SHA-256:      {actual_sha}")
        print(f"Documents:          {len(records)}")
        print("Eligibility:        exact one-to-one GPS only")
        print("Geometry included:  NO")

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
        existing = inspect_existing(collection, records)
        report["existingState"] = existing

        print("")
        print("[PREFLIGHT SUMMARY]")
        print(f"  identical existing: {existing['identical']}")
        print(f"  different existing: {existing['different']}")
        print(f"  missing:            {existing['missing']}")

        if args.preflight_only:
            report.update(
                {
                    "status": "PASS",
                    "result": "PREFLIGHT_OK",
                }
            )
            print("")
            print("============================================================")
            print("PREFLIGHT PASSED")
            print("============================================================")
            print("Firestore reads performed:  YES")
            print("Firestore writes performed: NO")
            return 0

        print("")
        print("[UPLOAD] Writing exactly 20 pilot documents")
        batch = db.batch()
        for record in records:
            data = dict(record["data"])
            data["loadedAt"] = firestore.SERVER_TIMESTAMP
            batch.set(
                collection.document(record["docId"]),
                data,
            )
        batch.commit()
        report["firestoreWritesPerformed"] = True
        report["documentsWritten"] = len(records)
        print("[UPLOAD] Batch committed: 20/20")

        verification = verify_after_write(
            collection,
            records,
        )
        report.update(
            {
                "verification": verification,
                "status": "PASS",
                "result": "PILOT_UPLOAD_VERIFIED",
            }
        )

        print("")
        print("============================================================")
        print("PILOT UPLOAD AND READ-BACK VERIFICATION PASSED")
        print("============================================================")
        print(f"Documents written:  {len(records)}")
        print(f"Documents verified: {verification['verified']}")
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
