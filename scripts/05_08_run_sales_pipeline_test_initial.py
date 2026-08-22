"""Fast one-command initial Sales load for iREPS TEST (ireps-test).

This is intentionally NOT the recurring monthly refresh orchestrator.
It is for a clean initial ZA5241 Sales All + Meter Master input scope in TEST.

Performance/safety contract
---------------------------
- hard-gated to Firebase project ``ireps-test``;
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
python .\\scripts\\05_08_run_sales_pipeline_test_initial.py --execute
"""
from __future__ import annotations

import argparse
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
TEST_PROJECT = "ireps-test"
EXPECTED_ROWS = 10_216
HARD_TARGET_SECONDS = 2 * 60 * 60

DEFAULT_SERVICE_ACCOUNT = Path(
    r"C:\dev\secrets\ireps-test-firebase-adminsdk-fbsvc-d02929e1e3.json"
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
        description="Fast governed initial ZA5241 Sales load into iREPS TEST."
    )
    p.add_argument("--execute", action="store_true", help="Required to perform TEST writes.")
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
        TEST_PROJECT,
        "--confirm-project",
        TEST_PROJECT,
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
    if not args.execute:
        raise SystemExit(
            "No writes performed. Re-run with --execute after confirming TEST initial-load scope."
        )

    service_account = resolve(args.service_account)
    meter_csv = resolve(args.meter_csv)
    meter_manifest = resolve(args.meter_manifest)
    sales_csv = resolve(args.sales_csv)
    sales_manifest = resolve(args.sales_manifest)

    for path, label in (
        (service_account, "TEST service account"),
        (meter_csv, "Stage 05 Meter Master CSV"),
        (meter_manifest, "Stage 05 Meter Master manifest"),
        (sales_csv, "Stage 06 Sales All CSV"),
        (sales_manifest, "Stage 06 Sales All manifest"),
        (SCRIPTS / "07_upload_meter_master_v3.py", "Stage 07 uploader"),
        (SCRIPTS / "08_upload_sales_all_meters.py", "Stage 08 uploader"),
    ):
        require_file(path, label)

    credential_project = read_service_account_project_id(service_account)
    if credential_project != TEST_PROJECT:
        raise ValueError(
            f"Wrong service-account project: expected={TEST_PROJECT}; found={credential_project}"
        )

    run_stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    run_dir = PROJECT_ROOT / "output" / "logs" / "sales_pipeline_test_initial" / f"run__{run_stamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    report_path = run_dir / "test_initial_load_summary.json"
    console_path = run_dir / "console_timestamped.log"

    overall_started = monotonic()
    started_at = local_ts()
    stages: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "pipeline": "SALES_TEST_INITIAL_LOAD",
        "projectId": TEST_PROJECT,
        "expectedRows": EXPECTED_ROWS,
        "mode": "initial-load",
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
        "runDir": str(run_dir),
        "consoleLog": str(console_path),
        "stages": stages,
    }
    write_json_atomic(report_path, report)

    emit("=" * 78, console_path)
    emit("iREPS TEST SALES INITIAL LOAD START", console_path)
    emit("=" * 78, console_path)
    emit(f"Project: {TEST_PROJECT}", console_path)
    emit(f"Expected input rows: {EXPECTED_ROWS:,}", console_path)
    emit("Batch size: 400", console_path)
    emit("Refresh: DISABLED", console_path)
    emit("Visibility reconciliation: DISABLED", console_path)
    emit("Deletes: 0", console_path)
    emit(f"Run directory: {run_dir}", console_path)
    emit("Performance target: < 02:00:00", console_path)

    try:
        # BOTH batched input-scope preflights pass before the first write.
        stages.append(
            run_child(
                "STAGE 08 SALES ALL INITIAL-LOAD PREFLIGHT (NO WRITES)",
                stage_command(
                    "08_upload_sales_all_meters.py",
                    service_account,
                    sales_csv,
                    sales_manifest,
                    run_dir / "stage08_preflight",
                    preflight_only=True,
                ),
                overall_started,
                console_path,
            )
        )
        stages.append(
            run_child(
                "STAGE 07 METER MASTER INITIAL-LOAD PREFLIGHT (NO WRITES)",
                stage_command(
                    "07_upload_meter_master_v3.py",
                    service_account,
                    meter_csv,
                    meter_manifest,
                    run_dir / "stage07_preflight",
                    preflight_only=True,
                ),
                overall_started,
                console_path,
            )
        )

        emit("BOTH INPUT-SCOPE PREFLIGHTS PASS — FIRST WRITE STARTS NOW", console_path)

        # Sales All first so refs.sales published by Stage 07 point at existing Sales docs.
        stages.append(
            run_child(
                "STAGE 08 SALES ALL INITIAL CREATE + FULL BATCHED VERIFY",
                stage_command(
                    "08_upload_sales_all_meters.py",
                    service_account,
                    sales_csv,
                    sales_manifest,
                    run_dir / "stage08_create",
                    preflight_only=False,
                ),
                overall_started,
                console_path,
            )
        )
        stages.append(
            run_child(
                "STAGE 07 METER MASTER INITIAL CREATE + FULL BATCHED VERIFY",
                stage_command(
                    "07_upload_meter_master_v3.py",
                    service_account,
                    meter_csv,
                    meter_manifest,
                    run_dir / "stage07_create",
                    preflight_only=False,
                ),
                overall_started,
                console_path,
            )
        )

        total_seconds = monotonic() - overall_started
        report.update(
            {
                "status": "PASS",
                "result": "TEST_INITIAL_LOAD_COMPLETE",
                "finishedAt": local_ts(),
                "durationSeconds": round(total_seconds, 3),
                "duration": elapsed_text(total_seconds),
                "withinTwoHourTarget": total_seconds < HARD_TARGET_SECONDS,
                "stages": stages,
            }
        )
        write_json_atomic(report_path, report)

        emit("=" * 78, console_path)
        emit("TEST SALES PIPELINE COMPLETE", console_path)
        emit("=" * 78, console_path)
        emit(f"Project: {TEST_PROJECT}", console_path)
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
        emit(f"TEST SALES PIPELINE FAILED after {elapsed_text(total_seconds)}", console_path)
        emit(f"Reason: {exc}", console_path)
        emit(f"Summary: {report_path}", console_path)
        emit(f"Timestamped console: {console_path}", console_path)
        raise


if __name__ == "__main__":
    main()
