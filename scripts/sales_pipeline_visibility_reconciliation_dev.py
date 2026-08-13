"""DEV-only operational visibility reconciliation for Sales All Meters.

This is NOT a commercial Stage 08 writer. It is an explicit operational
reconciliation pass run only after Stage 08 has created/refreshed Sales All and
Stage 07 has established the final Meter Master sales links.

For every governed Stage 05 input ID it transactionally reads:
  meter_master/{id}
  sales-all-meters/{id}

Expected visibility is derived ONLY from final Meter Master operational truth:
  refs.asts.id nonblank AND refs.sales.id nonblank -> VISIBLE
  otherwise                                         -> INVISIBLE

The only permitted write path is:
  master.visibility

No document creation, deletion, commercial update, tbRefs/geofenceRefs update,
or unknown-root update exists in this script.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

DEV_PROJECT = "ireps2"
METER_MASTER_COLLECTION = "meter_master"
SALES_ALL_COLLECTION = "sales-all-meters"
CANONICAL_ID = re.compile(r"^[A-Z0-9]+$")
VISIBILITIES = {"VISIBLE", "INVISIBLE"}
FIRESTORE_READ_LIMIT = 400
LOGICAL_METERS_PER_TRANSACTION = 200
TRANSACTION_MAX_ATTEMPTS = 5
STAGE05_COLUMNS = [
    "masterId",
    "lmPcode",
    "meterNoRaw",
    "meterNoNormalized",
    "meterType",
    "customerNo",
    "accountNo",
    "salesId",
    "salesProvider",
    "astId",
]


def safe(value: Any) -> str:
    return "" if value is None else str(value).strip()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def derive_visibility(master: Mapping[str, Any]) -> str:
    ast_id = safe(((master.get("refs") or {}).get("asts") or {}).get("id"))
    sales_id = safe(((master.get("refs") or {}).get("sales") or {}).get("id"))
    return "VISIBLE" if ast_id and sales_id else "INVISIBLE"


def without_visibility(payload: Mapping[str, Any]) -> dict[str, Any]:
    clone = json.loads(json.dumps(payload, default=str))
    master = clone.get("master")
    if isinstance(master, dict):
        master.pop("visibility", None)
    return clone


@dataclass
class Stats:
    rows: int
    inspected: int = 0
    updated: int = 0
    unchanged: int = 0
    conflicts: int = 0
    failed: int = 0
    writes_attempted: int = 0
    writes_succeeded: int = 0
    visible_expected: int = 0
    invisible_expected: int = 0
    conflict_records: list[dict[str, Any]] = field(default_factory=list)
    failure_records: list[dict[str, Any]] = field(default_factory=list)
    read_waves: int = 0
    transaction_waves_attempted: int = 0
    transaction_waves_committed: int = 0
    verification_read_waves: int = 0
    maximum_reads_in_any_wave: int = 0
    maximum_writes_in_any_transaction: int = 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Reconcile Sales All visibility from final Meter Master truth in ireps2 DEV.")
    p.add_argument("--project-id", required=True)
    p.add_argument("--confirm-project", required=True)
    p.add_argument("--service-account", required=True, type=Path)
    p.add_argument("--input", required=True, type=Path, help="Stage 05 Meter Master CSV identifying the governed input scope.")
    p.add_argument("--manifest", required=True, type=Path, help="Matching Stage 05 schemaVersion 2 manifest.")
    p.add_argument("--sales-input", required=True, type=Path, help="Matching Stage 06 Sales All CSV for scope binding.")
    p.add_argument("--sales-manifest", required=True, type=Path, help="Matching Stage 06 schemaVersion 2 manifest.")
    p.add_argument("--report-dir", required=True, type=Path)
    p.add_argument("--preflight-only", action="store_true")
    return p.parse_args()


def load_scope(input_path: Path, manifest_path: Path) -> tuple[list[str], dict[str, Any]]:
    if not input_path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError("Stage 05 CSV/manifest missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schemaVersion") != 2 or manifest.get("stage") != "05":
        raise ValueError("Visibility reconciliation requires Stage 05 schemaVersion 2")
    if manifest.get("script") != "05_build_meter_master_v3.py" or manifest.get("status") != "PASS":
        raise ValueError("Stage 05 manifest identity/status mismatch")
    if manifest.get("result") != "BUILD_WRITTEN":
        raise ValueError("Stage 05 manifest must be BUILD_WRITTEN")

    output = manifest.get("outputContract") or {}
    source = manifest.get("sourceContract") or {}
    if output.get("sha256") != sha256(input_path):
        raise ValueError("Stage 05 CSV SHA mismatch")
    if output.get("columns") != STAGE05_COLUMNS:
        raise ValueError("Stage 05 manifest columns do not match approved ten-column contract")

    rows: list[dict[str, str]] = []
    with input_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != STAGE05_COLUMNS:
            raise ValueError("Stage 05 CSV columns do not match approved ten-column contract")
        rows = [dict(row) for row in reader]
    if len(rows) != output.get("rows"):
        raise ValueError("Stage 05 row count mismatch")

    provider = safe(source.get("provider")).lower()
    lm_pcode = safe(source.get("lmPcode")).upper()
    if not provider or not lm_pcode:
        raise ValueError("Stage 05 source provider/LM missing")

    ids: list[str] = []
    seen: set[str] = set()
    for row_no, row in enumerate(rows, start=2):
        meter = safe(row.get("masterId")).upper()
        normalized = safe(row.get("meterNoNormalized")).upper()
        if not meter or not CANONICAL_ID.fullmatch(meter) or meter != normalized:
            raise ValueError(f"Stage 05 identity mismatch at row {row_no}")
        if meter in seen:
            raise ValueError(f"Stage 05 duplicate masterId {meter}")
        seen.add(meter)
        if safe(row.get("lmPcode")).upper() != lm_pcode:
            raise ValueError(f"Stage 05 LM mismatch at row {row_no}")
        if safe(row.get("salesProvider")).lower() != provider:
            raise ValueError(f"Stage 05 provider mismatch at row {row_no}")
        if safe(row.get("salesId")).upper() != meter:
            raise ValueError(f"Stage 05 salesId mismatch at row {row_no}")
        ids.append(meter)

    ids_sorted = sorted(ids)
    document_ids_sha256 = canonical_sha256(ids_sorted)

    return ids_sorted, {
        "provider": provider,
        "lmPcode": lm_pcode,
        "rows": len(ids_sorted),
        "stage05Csv": str(input_path),
        "stage05CsvSha256": sha256(input_path),
        "stage05Manifest": str(manifest_path),
        "stage05ManifestSha256": sha256(manifest_path),
        "documentIdsSha256": document_ids_sha256,
        "stage05BuildFingerprint": manifest.get("buildFingerprint"),
    }


def bind_stage06_scope(
    sales_input: Path,
    sales_manifest: Path,
    *,
    stage05_ids: list[str],
    stage05_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    if not sales_input.is_file() or not sales_manifest.is_file():
        raise FileNotFoundError("Stage 06 CSV/manifest missing")
    manifest = json.loads(sales_manifest.read_text(encoding="utf-8"))
    if manifest.get("schemaVersion") != 2 or manifest.get("stage") != "06":
        raise ValueError("Visibility reconciliation requires Stage 06 schemaVersion 2")
    if manifest.get("script") != "06_build_sales_all_meters.py" or manifest.get("status") != "PASS":
        raise ValueError("Stage 06 manifest identity/status mismatch")
    if manifest.get("result") != "BUILD_WRITTEN":
        raise ValueError("Stage 06 manifest must be BUILD_WRITTEN")

    source = manifest.get("sourceContract") or {}
    output = manifest.get("outputContract") or {}
    if output.get("sha256") != sha256(sales_input):
        raise ValueError("Stage 06 CSV SHA mismatch")
    if int(output.get("rows", -1)) != len(stage05_ids):
        raise ValueError("Stage 06 row count does not match Stage 05 scope")
    if safe(source.get("provider")).lower() != stage05_evidence["provider"]:
        raise ValueError("Stage 06 provider does not match Stage 05 scope")
    if safe(source.get("lmPcode")).upper() != stage05_evidence["lmPcode"]:
        raise ValueError("Stage 06 LM does not match Stage 05 scope")
    if output.get("documentIdsSha256") != stage05_evidence["documentIdsSha256"]:
        raise ValueError("Stage 06 documentIdsSha256 does not match Stage 05 scope")

    columns = list(output.get("columns") or [])
    if "masterId" not in columns:
        raise ValueError("Stage 06 output contract is missing masterId")
    ids: list[str] = []
    with sales_input.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "masterId" not in reader.fieldnames:
            raise ValueError("Stage 06 CSV is missing masterId")
        for row_no, row in enumerate(reader, start=2):
            meter = safe(row.get("masterId")).upper()
            if not meter or not CANONICAL_ID.fullmatch(meter):
                raise ValueError(f"Stage 06 noncanonical masterId at row {row_no}")
            ids.append(meter)
    if len(ids) != len(set(ids)):
        raise ValueError("Stage 06 CSV contains duplicate masterId")
    if sorted(ids) != stage05_ids:
        raise ValueError("Stage 06 document IDs do not exactly match Stage 05 scope")

    return {
        "stage06Csv": str(sales_input),
        "stage06CsvSha256": sha256(sales_input),
        "stage06Manifest": str(sales_manifest),
        "stage06ManifestSha256": sha256(sales_manifest),
        "stage06BuildFingerprint": manifest.get("buildFingerprint"),
        "stage06DocumentIdsSha256": output.get("documentIdsSha256"),
        "rows": len(ids),
    }


def validate_pair(
    *,
    meter_id: str,
    master: Mapping[str, Any] | None,
    sales: Mapping[str, Any] | None,
    lm_pcode: str,
    provider: str,
) -> tuple[str | None, str | None]:
    if not isinstance(master, Mapping):
        return None, "meter_master target missing/not object"
    if not isinstance(sales, Mapping):
        return None, "sales-all-meters target missing/not object"

    master_meter = master.get("meterNo") or {}
    if not isinstance(master_meter, Mapping) or safe(master_meter.get("normalized")).upper() != meter_id:
        return None, "meter_master normalized identity mismatch"
    if safe(master.get("lmPcode")).upper() != lm_pcode:
        return None, "meter_master lmPcode mismatch"
    refs = master.get("refs") or {}
    if not isinstance(refs, Mapping):
        return None, "meter_master refs missing/not object"
    sales_ref = refs.get("sales") or {}
    ast_ref = refs.get("asts") or {}
    if not isinstance(sales_ref, Mapping) or not isinstance(ast_ref, Mapping):
        return None, "meter_master refs subshape invalid"
    if safe(sales_ref.get("id")).upper() != meter_id:
        return None, "meter_master refs.sales.id mismatch"
    if safe(sales_ref.get("provider")).lower() != provider:
        return None, "meter_master refs.sales.provider mismatch"
    if not isinstance(ast_ref.get("id", ""), str):
        return None, "meter_master refs.asts.id must be string"

    sales_master = sales.get("master")
    if not isinstance(sales_master, Mapping):
        return None, "sales-all-meters master missing/not object"
    if safe(sales_master.get("id")).upper() != meter_id:
        return None, "sales-all-meters master.id mismatch"
    if sales_master.get("visibility") not in VISIBILITIES:
        return None, "sales-all-meters master.visibility invalid"
    if safe(sales.get("meterNoNormalized")).upper() != meter_id:
        return None, "sales-all-meters normalized identity mismatch"
    if safe(sales.get("lmPcode")).upper() != lm_pcode:
        return None, "sales-all-meters lmPcode mismatch"
    if safe(sales.get("provider")).lower() != provider:
        return None, "sales-all-meters provider mismatch"

    return derive_visibility(master), None


def chunks(values: list[str], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def pair_refs(db: Any, meter_ids: list[str]) -> list[Any]:
    refs: list[Any] = []
    for meter_id in meter_ids:
        refs.append(db.collection(METER_MASTER_COLLECTION).document(meter_id))
        refs.append(db.collection(SALES_ALL_COLLECTION).document(meter_id))
    if len(refs) > FIRESTORE_READ_LIMIT:
        raise ValueError("Visibility read wave exceeds governed 400-reference limit")
    return refs


def snapshots_by_path(snapshots: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for snapshot in snapshots:
        reference = getattr(snapshot, "reference", None)
        path = getattr(reference, "path", None)
        if path:
            result[str(path)] = snapshot
    return result


def snapshot_for(result: Mapping[str, Any], ref: Any) -> Any | None:
    return result.get(str(getattr(ref, "path", "")))


def bulk_pair_read(db: Any, meter_ids: list[str]) -> tuple[list[Any], dict[str, Any]]:
    refs = pair_refs(db, meter_ids)
    return refs, snapshots_by_path(db.get_all(refs))


def run() -> None:
    args = parse_args()
    project_id = safe(args.project_id)
    confirm_project = safe(args.confirm_project)
    if project_id != DEV_PROJECT or confirm_project != DEV_PROJECT or project_id != confirm_project:
        raise ValueError("Visibility reconciliation is DEV-only and hard-gated to ireps2")
    if not args.service_account.is_file():
        raise FileNotFoundError(f"Service account not found: {args.service_account}")
    sa = json.loads(args.service_account.read_text(encoding="utf-8"))
    if safe(sa.get("project_id")) != DEV_PROJECT:
        raise ValueError("Service-account project_id is not ireps2")

    ids, evidence = load_scope(args.input, args.manifest)
    stage06_evidence = bind_stage06_scope(
        args.sales_input, args.sales_manifest, stage05_ids=ids, stage05_evidence=evidence
    )
    evidence["stage06"] = stage06_evidence
    try:
        from google.cloud import firestore
        from google.api_core import exceptions as google_exceptions
        from google.oauth2 import service_account
    except ImportError as exc:
        raise RuntimeError("google-cloud-firestore/google-auth are required") from exc

    credentials = service_account.Credentials.from_service_account_file(str(args.service_account))
    db = firestore.Client(project=DEV_PROJECT, credentials=credentials)
    stats = Stats(rows=len(ids))
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    args.report_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.report_dir / f"sales_all_visibility_reconciliation__ireps2__{run_id}.json"
    report: dict[str, Any] = {
        "stage": "05-08", "script": "sales_pipeline_visibility_reconciliation_dev.py",
        "operation": "post_stage07_sales_all_visibility_reconciliation",
        "projectId": DEV_PROJECT, "preflightOnly": bool(args.preflight_only),
        "sourceEvidence": evidence, "status": "STARTED", "result": "STARTED",
        "writePaths": ["master.visibility"], "deletes": 0,
        "startedAt": datetime.now(UTC).isoformat(),
        "firestoreReadLimit": FIRESTORE_READ_LIMIT,
        "logicalMetersPerTransaction": LOGICAL_METERS_PER_TRANSACTION,
    }
    before_non_visibility: dict[str, str] = {}

    def finish_report() -> None:
        report["finishedAt"] = datetime.now(UTC).isoformat()
        temp = report_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        temp.replace(report_path)

    def publish_outcomes(
        outcomes: list[dict[str, Any]],
        *,
        committed_write_count: int = 0,
    ) -> None:
        observed_updates = sum(1 for outcome in outcomes if outcome.get("outcome") == "UPDATED")
        if committed_write_count not in {0, observed_updates}:
            raise RuntimeError(
                "Visibility reconciliation write accounting mismatch: "
                f"committed={committed_write_count}; classifiedUpdates={observed_updates}"
            )
        for outcome in outcomes:
            stats.inspected += 1
            if outcome["outcome"] == "CONFLICT":
                stats.conflicts += 1
                stats.conflict_records.append({"masterId": outcome["masterId"], "reason": outcome["reason"]})
                continue
            expected = outcome["expected"]
            if expected == "VISIBLE": stats.visible_expected += 1
            else: stats.invisible_expected += 1
            if outcome["outcome"] == "UPDATED": stats.updated += 1
            else: stats.unchanged += 1
            before_non_visibility[outcome["masterId"]] = outcome["beforeHash"]
        if committed_write_count:
            stats.writes_attempted += committed_write_count
            stats.writes_succeeded += committed_write_count

    try:
        processed = 0
        for meter_wave in chunks(ids, LOGICAL_METERS_PER_TRANSACTION):
            if args.preflight_only:
                refs, by_path = bulk_pair_read(db, meter_wave)
                stats.read_waves += 1
                stats.maximum_reads_in_any_wave = max(stats.maximum_reads_in_any_wave, len(refs))
                outcomes: list[dict[str, Any]] = []
                for meter_id in meter_wave:
                    master_ref = db.collection(METER_MASTER_COLLECTION).document(meter_id)
                    sales_ref = db.collection(SALES_ALL_COLLECTION).document(meter_id)
                    master_snap = snapshot_for(by_path, master_ref)
                    sales_snap = snapshot_for(by_path, sales_ref)
                    master_data = master_snap.to_dict() if master_snap is not None and master_snap.exists else None
                    sales_data = sales_snap.to_dict() if sales_snap is not None and sales_snap.exists else None
                    expected, reason = validate_pair(
                        meter_id=meter_id, master=master_data, sales=sales_data,
                        lm_pcode=evidence["lmPcode"], provider=evidence["provider"],
                    )
                    if reason:
                        outcomes.append({"masterId": meter_id, "outcome": "CONFLICT", "reason": reason})
                    else:
                        assert expected is not None and isinstance(sales_data, Mapping)
                        outcomes.append({
                            "masterId": meter_id,
                            "outcome": "UNCHANGED" if sales_data["master"]["visibility"] == expected else "UPDATED",
                            "reason": None,
                            "expected": expected,
                            "beforeHash": canonical_sha256(without_visibility(sales_data)),
                        })
                publish_outcomes(outcomes, committed_write_count=0)
            else:
                stats.transaction_waves_attempted += 1
                transaction = db.transaction(max_attempts=TRANSACTION_MAX_ATTEMPTS)

                @firestore.transactional
                def apply_wave(transaction):
                    refs = pair_refs(db, meter_wave)
                    snapshots = snapshots_by_path(transaction.get_all(refs))
                    local: list[dict[str, Any]] = []
                    pending_updates: list[tuple[Any, str]] = []
                    # Firestore requires all reads before writes; all refs are read above.
                    for meter_id in meter_wave:
                        master_ref = db.collection(METER_MASTER_COLLECTION).document(meter_id)
                        sales_ref = db.collection(SALES_ALL_COLLECTION).document(meter_id)
                        master_snap = snapshot_for(snapshots, master_ref)
                        sales_snap = snapshot_for(snapshots, sales_ref)
                        master_data = master_snap.to_dict() if master_snap is not None and master_snap.exists else None
                        sales_data = sales_snap.to_dict() if sales_snap is not None and sales_snap.exists else None
                        expected, reason = validate_pair(
                            meter_id=meter_id, master=master_data, sales=sales_data,
                            lm_pcode=evidence["lmPcode"], provider=evidence["provider"],
                        )
                        if reason:
                            local.append({"masterId": meter_id, "outcome": "CONFLICT", "reason": reason})
                            continue
                        assert expected is not None and isinstance(sales_data, Mapping)
                        before_hash = canonical_sha256(without_visibility(sales_data))
                        actual = sales_data["master"]["visibility"]
                        if actual == expected:
                            local.append({
                                "masterId": meter_id, "outcome": "UNCHANGED", "reason": None,
                                "expected": expected, "beforeHash": before_hash,
                            })
                        else:
                            local.append({
                                "masterId": meter_id, "outcome": "UPDATED", "reason": None,
                                "expected": expected, "beforeHash": before_hash,
                            })
                            pending_updates.append((sales_ref, expected))
                    for sales_ref, expected in pending_updates:
                        transaction.update(sales_ref, {"master.visibility": expected})
                    return local, len(refs), len(pending_updates)

                outcomes, read_count, write_count = apply_wave(transaction)
                stats.transaction_waves_committed += 1
                stats.read_waves += 1
                stats.maximum_reads_in_any_wave = max(stats.maximum_reads_in_any_wave, read_count)
                stats.maximum_writes_in_any_transaction = max(
                    stats.maximum_writes_in_any_transaction, write_count
                )
                publish_outcomes(outcomes, committed_write_count=write_count)

            processed += len(meter_wave)
            print(
                f"Visibility reconciliation {processed:,}/{len(ids):,}: "
                f"update={stats.updated:,} unchanged={stats.unchanged:,} "
                f"conflict={stats.conflicts:,} failed={stats.failed:,}; "
                f"readWaves={stats.read_waves:,} transactionWaves={stats.transaction_waves_committed:,}"
            )

        if stats.inspected != stats.rows:
            raise RuntimeError("Visibility reconciliation accounting imbalance")
        if stats.conflicts or stats.failed:
            raise RuntimeError(
                f"Visibility reconciliation blocked/incomplete: conflicts={stats.conflicts}; failed={stats.failed}"
            )

        verification: dict[str, Any] = {"status": "NOT_RUN" if args.preflight_only else "PASS"}
        if not args.preflight_only:
            checked = 0
            preserved = 0
            for meter_wave in chunks(ids, LOGICAL_METERS_PER_TRANSACTION):
                refs, by_path = bulk_pair_read(db, meter_wave)
                stats.verification_read_waves += 1
                for meter_id in meter_wave:
                    master_ref = db.collection(METER_MASTER_COLLECTION).document(meter_id)
                    sales_ref = db.collection(SALES_ALL_COLLECTION).document(meter_id)
                    master_snap = snapshot_for(by_path, master_ref)
                    sales_snap = snapshot_for(by_path, sales_ref)
                    master_data = master_snap.to_dict() if master_snap is not None and master_snap.exists else None
                    sales_data = sales_snap.to_dict() if sales_snap is not None and sales_snap.exists else None
                    expected, reason = validate_pair(
                        meter_id=meter_id, master=master_data, sales=sales_data,
                        lm_pcode=evidence["lmPcode"], provider=evidence["provider"],
                    )
                    if reason:
                        raise RuntimeError(f"Post-reconciliation verification conflict for {meter_id}: {reason}")
                    assert isinstance(sales_data, Mapping)
                    if sales_data["master"]["visibility"] != expected:
                        raise RuntimeError(f"Visibility mismatch after reconciliation for {meter_id}")
                    before_hash = before_non_visibility[meter_id]
                    after_hash = canonical_sha256(without_visibility(sales_data))
                    if before_hash != after_hash:
                        raise RuntimeError(
                            f"Non-visibility Sales All fields changed during reconciliation for {meter_id}"
                        )
                    preserved += 1
                    checked += 1
                print(f"Visibility verification {checked:,}/{len(ids):,}")
            verification = {
                "status": "PASS", "documentsVerified": checked,
                "nonVisibilityPreservationVerified": preserved,
                "scope": "FULL_STAGE05_INPUT_IDS",
                "readWaves": stats.verification_read_waves,
            }

        report.update({
            "rows": stats.rows, "recordsInspected": stats.inspected,
            "updatedCount": stats.updated, "unchangedCount": stats.unchanged,
            "conflictCount": stats.conflicts, "failedCount": stats.failed,
            "visibleExpectedCount": stats.visible_expected,
            "invisibleExpectedCount": stats.invisible_expected,
            "writeAttemptCount": stats.writes_attempted,
            "writeSuccessCount": stats.writes_succeeded,
            "conflicts": stats.conflict_records, "failedRecords": stats.failure_records,
            "verification": verification,
            "batchEvidence": {
                "firestoreReadLimit": FIRESTORE_READ_LIMIT,
                "logicalMetersPerTransaction": LOGICAL_METERS_PER_TRANSACTION,
                "readWaves": stats.read_waves,
                "transactionWavesAttempted": stats.transaction_waves_attempted,
                "transactionWavesCommitted": stats.transaction_waves_committed,
                "verificationReadWaves": stats.verification_read_waves,
                "maximumReadsInAnyWave": stats.maximum_reads_in_any_wave,
                "maximumWritesInAnyTransaction": stats.maximum_writes_in_any_transaction,
                "perDocumentFallback": False,
            },
            "firestoreWrites": 0 if args.preflight_only else stats.writes_succeeded,
            "deletes": 0, "status": "PASS",
            "result": "PREFLIGHT_PASS" if args.preflight_only else "RECONCILIATION_VERIFIED",
        })
        finish_report()
        print("\nVISIBILITY RECONCILIATION PASS")
        print(f"Project: {DEV_PROJECT}")
        print(f"Rows: {stats.rows:,}")
        print(f"Writes: {0 if args.preflight_only else stats.writes_succeeded:,}")
        print("Deletes: 0")
        print(f"Report: {report_path}")
    except Exception as exc:
        report.update({
            "rows": stats.rows, "recordsInspected": stats.inspected,
            "updatedCount": stats.updated, "unchangedCount": stats.unchanged,
            "conflictCount": stats.conflicts, "failedCount": stats.failed,
            "conflicts": stats.conflict_records, "failedRecords": stats.failure_records,
            "status": "FAIL", "result": "FAILED",
            "errorType": type(exc).__name__, "error": str(exc),
            "firestoreWrites": 0 if args.preflight_only else stats.writes_succeeded,
            "deletes": 0,
        })
        finish_report()
        raise
    finally:
        try:
            db.close()
            print("[CLEANUP] Firestore client closed.")
        except Exception:
            pass


if __name__ == "__main__":
    run()
