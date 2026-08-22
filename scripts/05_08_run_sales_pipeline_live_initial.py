"""Fast one-command initial Sales load for iREPS LIVE (ireps-5c3e9).

This is intentionally NOT the recurring monthly refresh orchestrator.
It is for a clean initial ZA5241 Sales All + Meter Master input scope in LIVE.

Performance/safety contract
---------------------------
- hard-gated to Firebase project ``ireps-5c3e9``;
- uses the already governed Stage 05/06 frozen artifacts under ``output``;
- runs fast input-scope preflights before the first Firestore write;
- never calls Stage 07 refresh, Stage 08 refresh, or visibility reconciliation;
- Stage 08 initial-load uses batched get_all + batch.create (400 docs/wave);
- Stage 07 initial-load uses batched get_all + batch.create (400 docs/wave);
- both stages perform full input-scope batched verification after writes;
- no delete, merge, or overwrite path exists in this orchestrator;
- every stage prints local timestamps and duration and a JSON run summary is written.

Run
---
python .\\scripts\\05_08_run_sales_pipeline_live_initial.py --execute --confirm-live RUN_LIVE_SALES_INITIAL_LOAD_IREPS_5C3E9_ZA5241
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"
LIVE_PROJECT = "ireps-5c3e9"
LIVE_CONFIRMATION = "RUN_LIVE_SALES_INITIAL_LOAD_IREPS_5C3E9_ZA5241"
EXPECTED_ROWS = 10_216
EXPECTED_LM_PCODE = "ZA5241"
EXPECTED_METER_CSV_SHA256 = "8ca6f096c4bb448ab9aafb6f843433de33bcdff8893c61fa573956d5b6181030"
EXPECTED_SALES_CSV_SHA256 = "9a37a4cffd88f0e009f6b430bec08f572e0038e3ed88e58897816f203fe27071"
HARD_TARGET_SECONDS = 2 * 60 * 60

DEFAULT_SERVICE_ACCOUNT = Path(
    r"C:\dev\secrets\ireps-5c3e9-firebase-adminsdk.json"
)
DEFAULT_METER_CSV = (
    PROJECT_ROOT
    / "output"
    / "meter_master"
    / "meter_master__ZA5241__FULL__2023-12_to_2026-06.csv"
)
DEFAULT_METER_MANIFEST = DEFAULT_METER_CSV.with_suffix(".manifest.json")
DEFAULT_SALES_CSV = (
    PROJECT_ROOT
    / "output"
    / "sales_all_meters"
    / "sales_all_meters__ZA5241__FULL__2023-12_to_2026-06.csv"
)
DEFAULT_SALES_MANIFEST = DEFAULT_SALES_CSV.with_suffix(".manifest.json")


def local_ts() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def elapsed_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fast governed initial ZA5241 Sales load into iREPS LIVE."
    )
    p.add_argument("--execute", action="store_true", help="Required to perform LIVE writes.")
    p.add_argument(
        "--preflight-only",
        action="store_true",
        help="Run both governed LIVE initial-load preflights and exit before the first write.",
    )
    p.add_argument(
        "--confirm-live",
        default="",
        help="Must exactly confirm the governed LIVE ZA5241 initial-load operation.",
    )
    p.add_argument("--service-account", type=Path, default=DEFAULT_SERVICE_ACCOUNT)
    p.add_argument("--meter-csv", type=Path, default=DEFAULT_METER_CSV)
    p.add_argument("--meter-manifest", type=Path, default=DEFAULT_METER_MANIFEST)
    p.add_argument("--sales-csv", type=Path, default=DEFAULT_SALES_CSV)
    p.add_argument("--sales-manifest", type=Path, default=DEFAULT_SALES_MANIFEST)
    return p.parse_args()


def resolve(path: Path) -> Path:
    return (path if path.is_absolute() else PROJECT_ROOT / path).expanduser().resolve()


def require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")


def read_service_account_project_id(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("type") != "service_account":
        raise ValueError("Credential is not a Firebase service account")
    return str(payload.get("project_id") or "").strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def csv_scope(path: Path) -> tuple[int, set[str]]:
    row_count = 0
    lm_pcodes: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "lmPcode" not in reader.fieldnames:
            raise ValueError(f"Governed input is missing required lmPcode column: {path}")
        for row in reader:
            row_count += 1
            lm_pcodes.add(str(row.get("lmPcode") or "").strip())
    return row_count, lm_pcodes


def validate_governed_artifact(
    *,
    csv_path: Path,
    manifest_path: Path,
    expected_stage: str,
    expected_script: str,
    expected_csv_sha256: str,
    label: str,
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    source = manifest.get("sourceContract") or {}
    output = manifest.get("outputContract") or {}

    if manifest.get("status") != "PASS" or manifest.get("result") != "BUILD_WRITTEN":
        raise ValueError(f"{label} manifest is not a passed frozen build")
    if str(manifest.get("stage")) != expected_stage or manifest.get("script") != expected_script:
        raise ValueError(f"{label} manifest stage/script identity mismatch")
    if str(source.get("lmPcode") or "").strip() != EXPECTED_LM_PCODE:
        raise ValueError(
            f"{label} manifest LM mismatch: expected={EXPECTED_LM_PCODE}; "
            f"found={source.get('lmPcode')!r}"
        )
    if int(output.get("rows") or -1) != EXPECTED_ROWS:
        raise ValueError(
            f"{label} manifest row-count mismatch: expected={EXPECTED_ROWS}; "
            f"found={output.get('rows')!r}"
        )

    actual_sha = sha256_file(csv_path)
    if actual_sha != expected_csv_sha256:
        raise ValueError(
            f"{label} CSV SHA mismatch: expected={expected_csv_sha256}; found={actual_sha}"
        )
    if str(output.get("sha256") or "").strip().lower() != actual_sha:
        raise ValueError(f"{label} manifest output SHA does not match the frozen CSV")

    row_count, lm_pcodes = csv_scope(csv_path)
    if row_count != EXPECTED_ROWS:
        raise ValueError(
            f"{label} CSV row-count mismatch: expected={EXPECTED_ROWS}; found={row_count}"
        )
    if lm_pcodes != {EXPECTED_LM_PCODE}:
        raise ValueError(
            f"{label} CSV LM scope mismatch: expected only {EXPECTED_LM_PCODE}; "
            f"found={sorted(lm_pcodes)!r}"
        )

    return {
        "label": label,
        "lmPcode": EXPECTED_LM_PCODE,
        "rows": row_count,
        "csvSha256": actual_sha,
        "manifestStage": expected_stage,
        "manifestScript": expected_script,
    }


def load_single_stage_report(report_dir: Path) -> tuple[Path, dict[str, Any]]:
    reports = sorted(report_dir.glob("*.json"))
    if len(reports) != 1:
        raise RuntimeError(
            f"Expected exactly one child JSON report in {report_dir}; found={len(reports)}"
        )
    path = reports[0]
    return path, json.loads(path.read_text(encoding="utf-8"))


def verify_child_report(
    *,
    report_dir: Path,
    collection: str,
    preflight_only: bool,
) -> dict[str, Any]:
    report_path, report = load_single_stage_report(report_dir)
    expected_result = "PREFLIGHT_PASS" if preflight_only else "UPLOAD_VERIFIED"

    if report.get("status") != "PASS" or report.get("result") != expected_result:
        raise RuntimeError(
            f"Child report did not prove {expected_result}: {report_path}"
        )
    if report.get("projectId") != LIVE_PROJECT or report.get("collection") != collection:
        raise RuntimeError(f"Child report project/collection mismatch: {report_path}")
    if report.get("mode") != "initial-load" or bool(report.get("preflightOnly")) != preflight_only:
        raise RuntimeError(f"Child report mode/preflight mismatch: {report_path}")
    if int(report.get("rowsRead") or -1) != EXPECTED_ROWS:
        raise RuntimeError(
            f"Child report rowsRead mismatch: expected={EXPECTED_ROWS}; "
            f"found={report.get('rowsRead')!r}; report={report_path}"
        )
    if int(report.get("uniqueMasterIds") or -1) != EXPECTED_ROWS:
        raise RuntimeError(
            f"Child report uniqueMasterIds mismatch: expected={EXPECTED_ROWS}; "
            f"found={report.get('uniqueMasterIds')!r}; report={report_path}"
        )

    if preflight_only:
        if int(report.get("inputScopeExistingCount") if report.get("inputScopeExistingCount") is not None else -1) != 0:
            raise RuntimeError(f"Child preflight found existing input IDs: {report_path}")
        if int(report.get("documentsCreated") if report.get("documentsCreated") is not None else -1) != 0 or int(report.get("committedBatches") if report.get("committedBatches") is not None else -1) != 0:
            raise RuntimeError(f"Child preflight unexpectedly reports writes: {report_path}")
    else:
        verification = report.get("verification") or {}
        if int(report.get("documentsCreated") or -1) != EXPECTED_ROWS:
            raise RuntimeError(
                f"Child create count mismatch: expected={EXPECTED_ROWS}; "
                f"found={report.get('documentsCreated')!r}; report={report_path}"
            )
        if int(verification.get("verifiedInputScopeCount") or -1) != EXPECTED_ROWS:
            raise RuntimeError(
                f"Child verification count mismatch: expected={EXPECTED_ROWS}; "
                f"found={verification.get('verifiedInputScopeCount')!r}; report={report_path}"
            )

    return {
        "reportPath": str(report_path),
        "result": expected_result,
        "rowsRead": EXPECTED_ROWS,
        "verifiedInputScopeCount": (
            None if preflight_only else EXPECTED_ROWS
        ),
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def emit(message: str, console_path: Path) -> None:
    line = f"[{local_ts()}] {message}"
    print(line, flush=True)
    console_path.parent.mkdir(parents=True, exist_ok=True)
    with console_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def run_child(
    label: str,
    command: list[str],
    overall_started: float,
    console_path: Path,
) -> dict[str, Any]:
    started = monotonic()
    emit("=" * 78, console_path)
    emit(f"{label} START", console_path)
    emit("=" * 78, console_path)
    emit("COMMAND: " + " ".join(command), console_path)

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    assert process.stdout is not None
    for raw_line in process.stdout:
        child_line = raw_line.rstrip("\r\n")
        if child_line:
            emit(f"{label} | {child_line}", console_path)
    returncode = process.wait()

    duration = monotonic() - started
    overall = monotonic() - overall_started

    if returncode != 0:
        emit(f"{label} FAIL duration={elapsed_text(duration)}", console_path)
        raise RuntimeError(f"{label} failed with exit code {returncode}")

    emit(f"{label} PASS duration={elapsed_text(duration)}", console_path)
    emit(f"OVERALL elapsed={elapsed_text(overall)}", console_path)
    if overall > HARD_TARGET_SECONDS:
        emit(
            f"WARNING: two-hour performance target exceeded ({elapsed_text(overall)})",
            console_path,
        )

    return {
        "label": label,
        "finishedAt": local_ts(),
        "durationSeconds": round(duration, 3),
        "status": "PASS",
    }


def stage_command(
    script_name: str,
    service_account: Path,
    csv_path: Path,
    manifest_path: Path,
    report_dir: Path,
    *,
    preflight_only: bool,
) -> list[str]:
    command = [
        sys.executable,
        str(SCRIPTS / script_name),
        "--project-id",
        LIVE_PROJECT,
        "--confirm-project",
        LIVE_PROJECT,
        "--service-account",
        str(service_account),
        "--input",
        str(csv_path),
        "--manifest",
        str(manifest_path),
        "--mode",
        "initial-load",
        "--report-dir",
        str(report_dir),
    ]
    if preflight_only:
        command.append("--preflight-only")
    return command


def main() -> None:
    args = parse_args()
    if args.preflight_only and args.execute:
        raise SystemExit(
            "No writes performed. Choose exactly one operation: --preflight-only OR --execute."
        )
    if not args.preflight_only and not args.execute:
        raise SystemExit(
            "No writes performed. Use --preflight-only for the governed LIVE read-only gate, "
            "or re-run with --execute and the exact --confirm-live token for LIVE writes."
        )
    if args.execute and args.confirm_live != LIVE_CONFIRMATION:
        raise SystemExit(
            f"No writes performed. LIVE execution requires --confirm-live {LIVE_CONFIRMATION}"
        )
    if args.preflight_only and args.confirm_live:
        raise SystemExit(
            "No writes performed. --confirm-live is not accepted with --preflight-only."
        )

    service_account = resolve(args.service_account)
    meter_csv = resolve(args.meter_csv)
    meter_manifest = resolve(args.meter_manifest)
    sales_csv = resolve(args.sales_csv)
    sales_manifest = resolve(args.sales_manifest)

    for path, label in (
        (service_account, "LIVE service account"),
        (meter_csv, "Stage 05 Meter Master CSV"),
        (meter_manifest, "Stage 05 Meter Master manifest"),
        (sales_csv, "Stage 06 Sales All CSV"),
        (sales_manifest, "Stage 06 Sales All manifest"),
        (SCRIPTS / "07_upload_meter_master_v3.py", "Stage 07 uploader"),
        (SCRIPTS / "08_upload_sales_all_meters.py", "Stage 08 uploader"),
    ):
        require_file(path, label)

    credential_project = read_service_account_project_id(service_account)
    if credential_project != LIVE_PROJECT:
        raise ValueError(
            f"Wrong service-account project: expected={LIVE_PROJECT}; found={credential_project}"
        )

    meter_artifact = validate_governed_artifact(
        csv_path=meter_csv,
        manifest_path=meter_manifest,
        expected_stage="05",
        expected_script="05_build_meter_master_v3.py",
        expected_csv_sha256=EXPECTED_METER_CSV_SHA256,
        label="Stage 05 Meter Master",
    )
    sales_artifact = validate_governed_artifact(
        csv_path=sales_csv,
        manifest_path=sales_manifest,
        expected_stage="06",
        expected_script="06_build_sales_all_meters.py",
        expected_csv_sha256=EXPECTED_SALES_CSV_SHA256,
        label="Stage 06 Sales All",
    )

    run_stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    run_dir = PROJECT_ROOT / "output" / "logs" / "sales_pipeline_live_initial" / f"run__{run_stamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    report_path = run_dir / "live_initial_load_summary.json"
    console_path = run_dir / "console_timestamped.log"

    overall_started = monotonic()
    started_at = local_ts()
    stages: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "pipeline": "SALES_LIVE_INITIAL_LOAD",
        "projectId": LIVE_PROJECT,
        "expectedRows": EXPECTED_ROWS,
        "mode": "initial-load",
        "preflightOnly": bool(args.preflight_only),
        "batchSize": 400,
        "refreshUsed": False,
        "visibilityReconciliationUsed": False,
        "deletes": 0,
        "startedAt": started_at,
        "status": "STARTED",
        "meterCsv": str(meter_csv),
        "meterManifest": str(meter_manifest),
        "salesCsv": str(sales_csv),
        "salesManifest": str(sales_manifest),
        "governedArtifactEvidence": {
            "meterMaster": meter_artifact,
            "salesAll": sales_artifact,
        },
        "runDir": str(run_dir),
        "consoleLog": str(console_path),
        "stages": stages,
    }
    write_json_atomic(report_path, report)

    emit("=" * 78, console_path)
    emit(
        "iREPS LIVE SALES INITIAL LOAD PREFLIGHT START"
        if args.preflight_only
        else "iREPS LIVE SALES INITIAL LOAD START",
        console_path,
    )
    emit("=" * 78, console_path)
    emit(f"Project: {LIVE_PROJECT}", console_path)
    emit(f"Expected input rows: {EXPECTED_ROWS:,}", console_path)
    emit("Batch size: 400", console_path)
    emit("Refresh: DISABLED", console_path)
    emit("Visibility reconciliation: DISABLED", console_path)
    emit("Deletes: 0", console_path)
    emit(f"Firestore writes permitted: {'NO' if args.preflight_only else 'YES'}", console_path)
    emit(f"Run directory: {run_dir}", console_path)
    emit("Performance target: < 02:00:00", console_path)

    try:
        # BOTH batched input-scope preflights pass before the first write.
        stage08_preflight_dir = run_dir / "stage08_preflight"
        stage07_preflight_dir = run_dir / "stage07_preflight"
        stage08_create_dir = run_dir / "stage08_create"
        stage07_create_dir = run_dir / "stage07_create"

        stage = run_child(
            "STAGE 08 SALES ALL INITIAL-LOAD PREFLIGHT (NO WRITES)",
            stage_command(
                "08_upload_sales_all_meters.py",
                service_account,
                sales_csv,
                sales_manifest,
                stage08_preflight_dir,
                preflight_only=True,
            ),
            overall_started,
            console_path,
        )
        stage["reportVerification"] = verify_child_report(
            report_dir=stage08_preflight_dir,
            collection="sales-all-meters",
            preflight_only=True,
        )
        stages.append(stage)

        stage = run_child(
            "STAGE 07 METER MASTER INITIAL-LOAD PREFLIGHT (NO WRITES)",
            stage_command(
                "07_upload_meter_master_v3.py",
                service_account,
                meter_csv,
                meter_manifest,
                stage07_preflight_dir,
                preflight_only=True,
            ),
            overall_started,
            console_path,
        )
        stage["reportVerification"] = verify_child_report(
            report_dir=stage07_preflight_dir,
            collection="meter_master",
            preflight_only=True,
        )
        stages.append(stage)

        if args.preflight_only:
            total_seconds = monotonic() - overall_started
            report.update(
                {
                    "status": "PASS",
                    "result": "LIVE_INITIAL_LOAD_PREFLIGHT_COMPLETE",
                    "finishedAt": local_ts(),
                    "durationSeconds": round(total_seconds, 3),
                    "duration": elapsed_text(total_seconds),
                    "verifiedLmPcode": EXPECTED_LM_PCODE,
                    "verifiedSalesAllPreflightCount": EXPECTED_ROWS,
                    "verifiedMeterMasterPreflightCount": EXPECTED_ROWS,
                    "firestoreWrites": 0,
                    "stages": stages,
                }
            )
            write_json_atomic(report_path, report)
            emit("=" * 78, console_path)
            emit("LIVE SALES INITIAL-LOAD PREFLIGHT COMPLETE — NO WRITES", console_path)
            emit("=" * 78, console_path)
            emit(f"Project: {LIVE_PROJECT}", console_path)
            emit(f"Verified LM: {EXPECTED_LM_PCODE}", console_path)
            emit(f"Sales All input IDs absent: {EXPECTED_ROWS:,}/{EXPECTED_ROWS:,}", console_path)
            emit(f"Meter Master input IDs absent: {EXPECTED_ROWS:,}/{EXPECTED_ROWS:,}", console_path)
            emit("Firestore writes: 0", console_path)
            emit(f"Duration: {elapsed_text(total_seconds)}", console_path)
            emit(f"Summary: {report_path}", console_path)
            emit(f"Timestamped console: {console_path}", console_path)
            return

        emit(
            f"BOTH EXACT {EXPECTED_ROWS:,}-DOCUMENT {EXPECTED_LM_PCODE} PREFLIGHTS PASS — FIRST WRITE STARTS NOW",
            console_path,
        )

        # Sales All first so refs.sales published by Stage 07 point at existing Sales docs.
        stage = run_child(
            "STAGE 08 SALES ALL INITIAL CREATE + FULL BATCHED VERIFY",
            stage_command(
                "08_upload_sales_all_meters.py",
                service_account,
                sales_csv,
                sales_manifest,
                stage08_create_dir,
                preflight_only=False,
            ),
            overall_started,
            console_path,
        )
        stage["reportVerification"] = verify_child_report(
            report_dir=stage08_create_dir,
            collection="sales-all-meters",
            preflight_only=False,
        )
        stages.append(stage)

        stage = run_child(
            "STAGE 07 METER MASTER INITIAL CREATE + FULL BATCHED VERIFY",
            stage_command(
                "07_upload_meter_master_v3.py",
                service_account,
                meter_csv,
                meter_manifest,
                stage07_create_dir,
                preflight_only=False,
            ),
            overall_started,
            console_path,
        )
        stage["reportVerification"] = verify_child_report(
            report_dir=stage07_create_dir,
            collection="meter_master",
            preflight_only=False,
        )
        stages.append(stage)

        total_seconds = monotonic() - overall_started
        report.update(
            {
                "status": "PASS",
                "result": "LIVE_INITIAL_LOAD_COMPLETE",
                "finishedAt": local_ts(),
                "durationSeconds": round(total_seconds, 3),
                "duration": elapsed_text(total_seconds),
                "withinTwoHourTarget": total_seconds < HARD_TARGET_SECONDS,
                "verifiedLmPcode": EXPECTED_LM_PCODE,
                "verifiedSalesAllCount": EXPECTED_ROWS,
                "verifiedMeterMasterCount": EXPECTED_ROWS,
                "stages": stages,
            }
        )
        write_json_atomic(report_path, report)

        emit("=" * 78, console_path)
        emit("LIVE SALES PIPELINE COMPLETE", console_path)
        emit("=" * 78, console_path)
        emit(f"Project: {LIVE_PROJECT}", console_path)
        emit(f"Input scope: {EXPECTED_ROWS:,}", console_path)
        emit("Refresh used: NO", console_path)
        emit("Deletes: 0", console_path)
        emit(f"Duration: {elapsed_text(total_seconds)}", console_path)
        emit(
            f"Under 2 hours: {'YES' if total_seconds < HARD_TARGET_SECONDS else 'NO'}",
            console_path,
        )
        emit(f"Summary: {report_path}", console_path)
        emit(f"Timestamped console: {console_path}", console_path)

    except Exception as exc:
        total_seconds = monotonic() - overall_started
        report.update(
            {
                "status": "FAIL",
                "result": "FAILED",
                "finishedAt": local_ts(),
                "durationSeconds": round(total_seconds, 3),
                "duration": elapsed_text(total_seconds),
                "withinTwoHourTarget": total_seconds < HARD_TARGET_SECONDS,
                "errorType": type(exc).__name__,
                "error": str(exc),
                "stages": stages,
            }
        )
        write_json_atomic(report_path, report)
        emit(f"LIVE SALES PIPELINE FAILED after {elapsed_text(total_seconds)}", console_path)
        emit(f"Reason: {exc}", console_path)
        emit(f"Summary: {report_path}", console_path)
        emit(f"Timestamped console: {console_path}", console_path)
        raise


if __name__ == "__main__":
    main()
