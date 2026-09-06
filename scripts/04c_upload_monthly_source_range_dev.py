"""
Stage 04C: governed monthly-source range uploader for iREPS DEV.

This helper is intentionally DEV-only and hard-gated to Firebase project `ireps2`.

For every requested month it:
1. validates the exact Stage 03B BUILD_WRITTEN manifest locally;
2. runs Stage 04 in the selected create-only or monthly_source refresh preflight mode;
3. validates the generated Stage 04 preflight report;
4. runs Stage 04 in the same selected execute-upload mode;
5. validates the generated Stage 04 upload report and post-write verification;
6. stops immediately on the first failure.

Safety:
- target project must be exactly ireps2;
- service-account project must be exactly ireps2;
- Stage 04 script SHA256 must match the approved cleanup-fixed version;
- Stage 04 is invoked with explicit mode=create-only or mode=refresh;
- refresh remains monthly_source-only and never deletes documents;
- no delete/update/overwrite option exists here;
- all Stage 03B manifests are validated before the first Firestore call;
- a fresh Stage 04 preflight occurs immediately before each month's write;
- Stage 04 itself repeats the preflight during execute-upload;
- failures stop the range immediately so Stage 04 resume can be used deliberately.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STAGE04_SCRIPT = PROJECT_ROOT / "scripts" / "04_upload_conlog_monthly_v3.py"
DEFAULT_MANIFEST_DIR = PROJECT_ROOT / "output" / "logs" / "monthly_source_build"
DEFAULT_LOG_DIR = PROJECT_ROOT / "output" / "logs" / "monthly_dev_upload_range"

DEV_PROJECT_ID = "ireps2"
EXPECTED_STAGE04_SHA256 = (
    "da10225a7de887da5123d62f9819b619e732556693ae5caf936328aa9f759f9d"
)
EXPECTED_STAGE04_SCRIPT = "04_upload_conlog_monthly_v3.py"

EXPECTED_SOURCE_STAGE = "03B"
EXPECTED_SOURCE_SCRIPT = "03b_build_monthly_from_monthly_source.py"
EXPECTED_SOURCE_RESULT = "BUILD_WRITTEN"
EXPECTED_SOURCE_OPERATION = "build-write"
EXPECTED_SOURCE_ORIGIN = "monthly_source"

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATASET_ORDER = ("monthly", "monthly_lm", "monthly_lm_groups")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Upload a governed monthly-source month range to ireps2 DEV using "
            "Stage 04 create-only or controlled refresh semantics."
        )
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--confirm-project", required=True)
    parser.add_argument("--service-account", required=True, type=Path)
    parser.add_argument("--lm-pcode", required=True)
    parser.add_argument("--from-month", required=True)
    parser.add_argument("--to-month", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--vending-provider-id", required=True)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--expected-input-sha256", required=True)
    parser.add_argument(
        "--mode",
        choices=("create-only", "refresh"),
        default="create-only",
        help="Stage 04 DEV range mode. refresh is monthly_source-only recurring refresh.",
    )
    parser.add_argument("--stage04-script", type=Path, default=DEFAULT_STAGE04_SCRIPT)
    parser.add_argument("--manifest-dir", type=Path, default=DEFAULT_MANIFEST_DIR)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    return parser.parse_args()


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def make_run_id(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def parse_month(value: str) -> tuple[int, int]:
    if not MONTH_RE.fullmatch(value):
        raise ValueError(f"Month must use YYYY-MM format: {value!r}")
    y, m = value.split("-")
    return int(y), int(m)


def month_range(start: str, end: str) -> list[str]:
    sy, sm = parse_month(start)
    ey, em = parse_month(end)
    if (sy, sm) > (ey, em):
        raise ValueError(f"--from-month must not be after --to-month: {start} > {end}")

    result: list[str] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        result.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y += 1
            m = 1
    return result


def validate_local_args(args: argparse.Namespace) -> tuple[str, str, str, Path, Path]:
    project_id = clean(args.project_id)
    confirm_project = clean(args.confirm_project)
    lm_pcode = clean(args.lm_pcode).upper()
    provider = clean(args.provider).lower()
    expected_input_sha = clean(args.expected_input_sha256).lower()
    source_run_id = clean(args.source_run_id)

    if project_id != DEV_PROJECT_ID:
        raise ValueError(
            f"[SAFETY] Stage 04C is DEV-only. Expected --project-id "
            f"{DEV_PROJECT_ID!r}, got {project_id!r}."
        )
    if confirm_project != DEV_PROJECT_ID:
        raise ValueError(
            f"[SAFETY] --confirm-project must be exactly {DEV_PROJECT_ID!r}."
        )
    if not lm_pcode:
        raise ValueError("--lm-pcode may not be blank")
    if not provider:
        raise ValueError("--provider may not be blank")
    if not clean(args.vending_provider_id):
        raise ValueError("--vending-provider-id may not be blank")
    if not RUN_ID_RE.fullmatch(source_run_id):
        raise ValueError(
            f"--source-run-id must use YYYYMMDDTHHMMSSZ: {source_run_id!r}"
        )
    if not SHA256_RE.fullmatch(expected_input_sha):
        raise ValueError("--expected-input-sha256 must be lowercase 64-char SHA256")

    service_account = args.service_account.expanduser().resolve()
    if not service_account.is_file():
        raise ValueError(f"Service-account file not found: {service_account}")
    credential = read_json(service_account)
    credential_project = clean(credential.get("project_id"))
    if credential_project != DEV_PROJECT_ID:
        raise ValueError(
            "[SAFETY] Service-account project mismatch. "
            f"Expected={DEV_PROJECT_ID!r}, credential={credential_project!r}"
        )

    stage04 = args.stage04_script.expanduser().resolve()
    if not stage04.is_file():
        raise ValueError(f"Stage 04 script not found: {stage04}")
    if stage04.name != EXPECTED_STAGE04_SCRIPT:
        raise ValueError(
            f"Unexpected Stage 04 script name {stage04.name!r}; "
            f"expected {EXPECTED_STAGE04_SCRIPT!r}"
        )
    actual_stage04_sha = sha256_file(stage04)
    if actual_stage04_sha != EXPECTED_STAGE04_SHA256:
        raise ValueError(
            "[SAFETY] Stage 04 SHA256 mismatch. "
            f"Expected={EXPECTED_STAGE04_SHA256}, actual={actual_stage04_sha}"
        )

    manifest_dir = args.manifest_dir.expanduser().resolve()
    if not manifest_dir.is_dir():
        raise ValueError(f"Stage 03B manifest directory not found: {manifest_dir}")

    return lm_pcode, provider, expected_input_sha, service_account, stage04


def manifest_path_for(
    manifest_dir: Path,
    *,
    lm_pcode: str,
    month: str,
    source_run_id: str,
) -> Path:
    return (
        manifest_dir
        / f"stage03b_monthly_source_build__{lm_pcode}__{month}__{source_run_id}.json"
    )


def validate_manifest(
    path: Path,
    *,
    lm_pcode: str,
    month: str,
    provider: str,
    expected_input_sha: str,
) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"Required Stage 03B manifest not found: {path}")

    payload = read_json(path)
    expected = {
        "stage": EXPECTED_SOURCE_STAGE,
        "script": EXPECTED_SOURCE_SCRIPT,
        "status": "PASS",
        "result": EXPECTED_SOURCE_RESULT,
        "operation": EXPECTED_SOURCE_OPERATION,
        "sourceOrigin": EXPECTED_SOURCE_ORIGIN,
        "provider": provider,
        "lmPcode": lm_pcode,
        "month": month,
    }
    actual = {key: clean(payload.get(key)) for key in expected}
    if actual != expected:
        raise ValueError(
            f"Stage 03B manifest identity mismatch for {month}. "
            f"Expected={expected}; actual={actual}"
        )

    source_input = payload.get("sourceInput")
    if not isinstance(source_input, dict):
        raise ValueError(f"Stage 03B manifest sourceInput missing for {month}")
    actual_sha = clean(source_input.get("sha256")).lower()
    if actual_sha != expected_input_sha:
        raise ValueError(
            f"Stage 03B source SHA mismatch for {month}: "
            f"expected={expected_input_sha}, actual={actual_sha}"
        )

    outputs = payload.get("outputs")
    if not isinstance(outputs, list) or len(outputs) != 3:
        raise ValueError(
            f"Stage 03B manifest must contain exactly three outputs for {month}"
        )
    datasets = [
        clean(item.get("dataset")) for item in outputs if isinstance(item, dict)
    ]
    if sorted(datasets) != sorted(DATASET_ORDER):
        raise ValueError(
            f"Stage 03B output dataset mismatch for {month}: {datasets}"
        )

    return payload


def run_child(command: list[str]) -> int:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
    return process.wait()


def exactly_one_report(
    directory: Path,
    *,
    project_id: str,
    lm_pcode: str,
    month: str,
) -> Path:
    matches = sorted(
        directory.glob(
            f"stage04_monthly_upload__{project_id}__{lm_pcode}__{month}__*.json"
        )
    )
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one Stage 04 report for {month} in {directory}; "
            f"found {len(matches)}"
        )
    return matches[0]


def validate_common_stage04_report(
    report: dict[str, Any],
    *,
    operation: str,
    result: str,
    project_id: str,
    lm_pcode: str,
    month: str,
    provider: str,
    vending_provider_id: str,
    mode: str,
) -> None:
    expected = {
        "stage": "04",
        "script": EXPECTED_STAGE04_SCRIPT,
        "status": "PASS",
        "result": result,
        "operation": operation,
        "mode": mode,
        "targetProject": project_id,
        "confirmProject": project_id,
        "credentialProject": project_id,
        "providerId": vending_provider_id,
        "lmPcode": lm_pcode,
        "month": month,
        "sourceStage": EXPECTED_SOURCE_STAGE,
        "sourceOrigin": EXPECTED_SOURCE_ORIGIN,
        "sourceProvider": provider,
    }
    actual = {key: clean(report.get(key)) for key in expected}
    if actual != expected:
        raise ValueError(
            f"Stage 04 {operation} report identity mismatch for {month}. "
            f"Expected={expected}; actual={actual}"
        )

    provider_doc = report.get("provider")
    if not isinstance(provider_doc, dict):
        raise ValueError(f"Stage 04 provider evidence missing for {month}")
    if clean(provider_doc.get("status")).lower() != "active":
        raise ValueError(f"Provider is not active in Stage 04 report for {month}")
    if clean(provider_doc.get("providerCode")).lower() != provider:
        raise ValueError(
            f"Provider code mismatch for {month}: "
            f"{provider_doc.get('providerCode')!r}"
        )

    reconciliation = report.get("reconciliation")
    if not isinstance(reconciliation, dict):
        raise ValueError(f"Stage 04 reconciliation evidence missing for {month}")


def validate_preflight_report(
    path: Path,
    *,
    project_id: str,
    lm_pcode: str,
    month: str,
    provider: str,
    vending_provider_id: str,
    mode: str,
) -> dict[str, Any]:
    report = read_json(path)
    validate_common_stage04_report(
        report,
        operation="preflight-only",
        result="PREFLIGHT_OK",
        project_id=project_id,
        lm_pcode=lm_pcode,
        month=month,
        provider=provider,
        vending_provider_id=vending_provider_id,
        mode=mode,
    )

    inputs = report.get("inputs")
    preflight = report.get("preflight")
    if not isinstance(inputs, dict) or not isinstance(preflight, dict):
        raise ValueError(f"Stage 04 preflight/input evidence missing for {month}")

    datasets: dict[str, Any] = {}
    total = 0
    for dataset in DATASET_ORDER:
        source = inputs.get(dataset)
        state = preflight.get(dataset)
        if not isinstance(source, dict) or not isinstance(state, dict):
            raise ValueError(f"Dataset evidence missing for {dataset}/{month}")

        rows = int(source.get("rows", -1))
        before = int(state.get("documentsBefore", -1))
        planned = int(state.get("documentsPlanned", -1))
        planned_create = int(state.get("documentsPlannedCreate", -1))
        planned_update = int(state.get("documentsPlannedUpdate", -1))
        unchanged = int(state.get("unchangedDocuments", -1))
        conflicts = int(state.get("conflictCount", -1))
        extras = int(state.get("extraDocumentCount", -1))

        if conflicts != 0 or extras != 0:
            raise ValueError(
                f"Conflict/extra detected for {dataset}/{month}: "
                f"conflicts={conflicts}, extras={extras}"
            )
        if planned != planned_create + planned_update:
            raise ValueError(
                f"Planned write accounting mismatch for {dataset}/{month}: "
                f"planned={planned}, create={planned_create}, update={planned_update}"
            )
        if planned_create + planned_update + unchanged != rows:
            raise ValueError(
                f"Preflight/input accounting mismatch for {dataset}/{month}: "
                f"create={planned_create}, update={planned_update}, "
                f"unchanged={unchanged}, rows={rows}"
            )
        if mode == "create-only":
            if before != 0:
                raise ValueError(
                    f"Create-only DEV scope is not empty for {dataset}/{month}: "
                    f"documentsBefore={before}"
                )
            if planned_create != rows or planned_update != 0 or unchanged != 0:
                raise ValueError(
                    f"Create-only accounting mismatch for {dataset}/{month}"
                )

        datasets[dataset] = {
            "rows": rows,
            "existing": before,
            "create": planned_create,
            "update": planned_update,
            "unchanged": unchanged,
            "planned": planned,
            "conflicts": conflicts,
            "extra": extras,
        }
        total += planned

    return {
        "reportPath": str(path),
        "datasets": datasets,
        "totalPlanned": total,
        "sourceFingerprint": clean(
            (report.get("sourceContract") or {}).get("fingerprint")
            if isinstance(report.get("sourceContract"), dict)
            else ""
        ),
    }


def validate_upload_report(
    path: Path,
    *,
    project_id: str,
    lm_pcode: str,
    month: str,
    provider: str,
    vending_provider_id: str,
    mode: str,
) -> dict[str, Any]:
    report = read_json(path)
    validate_common_stage04_report(
        report,
        operation="execute-upload",
        result="UPLOAD_VERIFIED",
        project_id=project_id,
        lm_pcode=lm_pcode,
        month=month,
        provider=provider,
        vending_provider_id=vending_provider_id,
        mode=mode,
    )

    inputs = report.get("inputs")
    created = report.get("documentsCreated")
    updated = report.get("documentsUpdated")
    unchanged = report.get("documentsUnchanged")
    verification = report.get("verification")
    if (
        not isinstance(inputs, dict)
        or not isinstance(created, dict)
        or not isinstance(updated, dict)
        or not isinstance(unchanged, dict)
        or not isinstance(verification, dict)
    ):
        raise ValueError(f"Stage 04 upload verification evidence missing for {month}")

    datasets: dict[str, Any] = {}
    total_created = 0
    total_updated = 0
    total_unchanged = 0
    for dataset in DATASET_ORDER:
        source = inputs.get(dataset)
        verify = verification.get(dataset)
        if not isinstance(source, dict) or not isinstance(verify, dict):
            raise ValueError(f"Upload dataset evidence missing for {dataset}/{month}")

        rows = int(source.get("rows", -1))
        created_count = int(created.get(dataset, -1))
        updated_count = int(updated.get(dataset, -1))
        unchanged_count = int(unchanged.get(dataset, -1))
        expected_count = int(verify.get("expectedCount", -1))
        final_count = int(verify.get("finalCount", -1))
        count_status = clean(verify.get("countVerification"))
        sample_status = clean(verify.get("sampleVerification"))
        full_status = clean(verify.get("fullDocumentVerification"))

        if created_count + updated_count + unchanged_count != rows:
            raise ValueError(
                f"Upload accounting mismatch for {dataset}/{month}: "
                f"created={created_count}, updated={updated_count}, "
                f"unchanged={unchanged_count}, rows={rows}"
            )
        if mode == "create-only" and (
            created_count != rows or updated_count != 0 or unchanged_count != 0
        ):
            raise ValueError(
                f"Create-only upload accounting mismatch for {dataset}/{month}"
            )
        if expected_count != rows or final_count != rows:
            raise ValueError(
                f"Final count mismatch for {dataset}/{month}: "
                f"rows={rows}, expected={expected_count}, final={final_count}"
            )
        if count_status != "PASS":
            raise ValueError(
                f"Count verification failed for {dataset}/{month}: {count_status!r}"
            )
        if mode == "refresh":
            if full_status != "PASS":
                raise ValueError(
                    f"Full refresh verification failed for {dataset}/{month}: "
                    f"{full_status!r}"
                )
        elif sample_status != "PASS":
            raise ValueError(
                f"Sample verification failed for {dataset}/{month}: {sample_status!r}"
            )

        datasets[dataset] = {
            "rows": rows,
            "created": created_count,
            "updated": updated_count,
            "unchanged": unchanged_count,
            "expectedCount": expected_count,
            "finalCount": final_count,
            "countVerification": count_status,
            "sampleVerification": sample_status,
            "fullDocumentVerification": full_status,
        }
        total_created += created_count
        total_updated += updated_count
        total_unchanged += unchanged_count

    return {
        "reportPath": str(path),
        "datasets": datasets,
        "totalCreated": total_created,
        "totalUpdated": total_updated,
        "totalUnchanged": total_unchanged,
        "totalWrites": total_created + total_updated,
        "sourceFingerprint": clean(
            (report.get("sourceContract") or {}).get("fingerprint")
            if isinstance(report.get("sourceContract"), dict)
            else ""
        ),
    }


def write_summary(
    run_dir: Path,
    *,
    started: dt.datetime,
    summary: dict[str, Any],
) -> tuple[Path, Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    tag = make_run_id(started)
    json_path = run_dir / f"stage04c_dev_upload_range__{tag}.json"
    csv_path = run_dir / f"stage04c_dev_upload_range__{tag}.csv"

    json_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "month",
                "status",
                "monthlyCreated",
                "monthlyLmCreated",
                "monthlyLmGroupsCreated",
                "totalCreated",
                "preflightReport",
                "uploadReport",
            ],
        )
        writer.writeheader()
        for item in summary.get("months", []):
            upload = item.get("upload") or {}
            datasets = upload.get("datasets") or {}
            writer.writerow(
                {
                    "month": item.get("month"),
                    "status": item.get("status"),
                    "monthlyCreated": (datasets.get("monthly") or {}).get("created", ""),
                    "monthlyLmCreated": (datasets.get("monthly_lm") or {}).get("created", ""),
                    "monthlyLmGroupsCreated": (
                        datasets.get("monthly_lm_groups") or {}
                    ).get("created", ""),
                    "totalCreated": upload.get("totalCreated", ""),
                    "preflightReport": (item.get("preflight") or {}).get(
                        "reportPath", ""
                    ),
                    "uploadReport": upload.get("reportPath", ""),
                }
            )

    return json_path, csv_path


def main() -> int:
    args = parse_args()
    started = utc_now()
    run_root = args.log_dir.expanduser().resolve() / f"run__{make_run_id(started)}"
    preflight_root = run_root / "preflight_reports"
    upload_root = run_root / "upload_reports"
    preflight_root.mkdir(parents=True, exist_ok=False)
    upload_root.mkdir(parents=True, exist_ok=False)

    summary: dict[str, Any] = {
        "stage": "04C",
        "script": "04c_upload_monthly_source_range_dev.py",
        "status": "STARTED",
        "result": "STARTED",
        "targetProject": DEV_PROJECT_ID,
        "writeSemantics": args.mode,
        "deletesAllowed": False,
        "updatesAllowed": args.mode == "refresh",
        "startedAt": utc_iso(started),
        "months": [],
    }

    current_month: str | None = None

    try:
        (
            lm_pcode,
            provider,
            expected_input_sha,
            service_account,
            stage04_script,
        ) = validate_local_args(args)

        months = month_range(args.from_month, args.to_month)
        source_run_id = clean(args.source_run_id)
        manifest_dir = args.manifest_dir.expanduser().resolve()
        vending_provider_id = clean(args.vending_provider_id)

        summary.update(
            {
                "lmPcode": lm_pcode,
                "provider": provider,
                "vendingProviderId": vending_provider_id,
                "fromMonth": args.from_month,
                "toMonth": args.to_month,
                "monthsExpected": len(months),
                "sourceRunId": source_run_id,
                "expectedInputSha256": expected_input_sha,
                "stage04Sha256": EXPECTED_STAGE04_SHA256,
                "manifestDir": str(manifest_dir),
                "preflightReportDir": str(preflight_root),
                "uploadReportDir": str(upload_root),
                "mode": args.mode,
            }
        )

        print("=" * 76)
        print("iREPS SALES PIPELINE — STAGE 04C DEV MONTHLY RANGE UPLOAD")
        print("=" * 76)
        print(f"Project            : {DEV_PROJECT_ID} (DEV ONLY)")
        print(f"LM                 : {lm_pcode}")
        print(f"Provider           : {provider}")
        print(f"Provider ID        : {vending_provider_id}")
        print(f"Range              : {args.from_month} -> {args.to_month}")
        print(f"Months             : {len(months)}")
        print(f"Source run         : {source_run_id}")
        print(f"Input SHA256       : {expected_input_sha}")
        print(f"Stage 04 SHA256    : {EXPECTED_STAGE04_SHA256}")
        print(f"Write mode         : {args.mode}")
        print(f"Updates allowed    : {args.mode == 'refresh'}")
        print("Deletes            : NEVER")
        print("Stop on first fail : YES")
        print("=" * 76)
        print("[LOCAL GATE] Validating all Stage 03B manifests before Firestore access...")

        manifests: dict[str, Path] = {}
        for month in months:
            path = manifest_path_for(
                manifest_dir,
                lm_pcode=lm_pcode,
                month=month,
                source_run_id=source_run_id,
            )
            validate_manifest(
                path,
                lm_pcode=lm_pcode,
                month=month,
                provider=provider,
                expected_input_sha=expected_input_sha,
            )
            manifests[month] = path

        print(f"[LOCAL GATE PASS] {len(manifests)} manifests validated.")
        print("")

        total_created = 0
        total_updated = 0
        total_unchanged = 0

        for index, month in enumerate(months, start=1):
            current_month = month
            month_result: dict[str, Any] = {
                "month": month,
                "status": "STARTED",
                "manifestPath": str(manifests[month]),
            }
            summary["months"].append(month_result)

            print("=" * 76)
            print(f"[MONTH {index}/{len(months)}] {month}")
            print("=" * 76)
            print("[STEP A] Fresh Stage 04 preflight — NO WRITES")

            month_preflight_dir = preflight_root / month
            month_upload_dir = upload_root / month
            month_preflight_dir.mkdir(parents=True, exist_ok=False)
            month_upload_dir.mkdir(parents=True, exist_ok=False)

            common = [
                sys.executable,
                str(stage04_script),
                "--project-id",
                DEV_PROJECT_ID,
                "--confirm-project",
                DEV_PROJECT_ID,
                "--service-account",
                str(service_account),
                "--lm-pcode",
                lm_pcode,
                "--month",
                month,
                "--manifest",
                str(manifests[month]),
                "--mode",
                args.mode,
                "--vending-provider-id",
                vending_provider_id,
            ]

            preflight_command = common + [
                "--log-dir",
                str(month_preflight_dir),
                "--preflight-only",
            ]
            preflight_rc = run_child(preflight_command)
            if preflight_rc != 0:
                raise RuntimeError(
                    f"Fresh Stage 04 preflight failed for {month} "
                    f"(exit code {preflight_rc}). No upload for this month was started."
                )

            preflight_report_path = exactly_one_report(
                month_preflight_dir,
                project_id=DEV_PROJECT_ID,
                lm_pcode=lm_pcode,
                month=month,
            )
            preflight = validate_preflight_report(
                preflight_report_path,
                project_id=DEV_PROJECT_ID,
                lm_pcode=lm_pcode,
                month=month,
                provider=provider,
                vending_provider_id=vending_provider_id,
                mode=args.mode,
            )
            month_result["preflight"] = preflight

            print(
                f"[STEP A PASS] {month} | planned={preflight['totalPlanned']:,} | "
                "conflicts=0 | extra=0"
            )
            print("")
            print(f"[STEP B] Stage 04 {args.mode} upload + post-write verification")

            upload_command = common + [
                "--log-dir",
                str(month_upload_dir),
                "--execute-upload",
            ]
            upload_rc = run_child(upload_command)
            if upload_rc != 0:
                # Stage 04 writes a failure report even on partial failure.
                failure_reports = sorted(
                    month_upload_dir.glob(
                        f"stage04_monthly_upload__{DEV_PROJECT_ID}__"
                        f"{lm_pcode}__{month}__*.json"
                    )
                )
                failure_hint = (
                    str(failure_reports[-1])
                    if failure_reports
                    else "No Stage 04 failure report was found."
                )
                recovery_action = (
                    "review the failure report, then rerun the same governed refresh "
                    "after resolving any conflict"
                    if args.mode == "refresh"
                    else "review the failure report before using governed resume"
                )
                raise RuntimeError(
                    f"Stage 04 upload failed for {month} (exit code {upload_rc}). "
                    f"STOPPED. Failure report: {failure_hint}. Next action: "
                    f"{recovery_action}."
                )

            upload_report_path = exactly_one_report(
                month_upload_dir,
                project_id=DEV_PROJECT_ID,
                lm_pcode=lm_pcode,
                month=month,
            )
            upload = validate_upload_report(
                upload_report_path,
                project_id=DEV_PROJECT_ID,
                lm_pcode=lm_pcode,
                month=month,
                provider=provider,
                vending_provider_id=vending_provider_id,
                mode=args.mode,
            )

            if preflight["sourceFingerprint"] != upload["sourceFingerprint"]:
                raise RuntimeError(
                    f"Source fingerprint changed between preflight and upload "
                    f"for {month}: preflight={preflight['sourceFingerprint']}, "
                    f"upload={upload['sourceFingerprint']}"
                )

            if preflight["totalPlanned"] != upload["totalWrites"]:
                raise RuntimeError(
                    f"Planned/write total mismatch for {month}: "
                    f"planned={preflight['totalPlanned']}, "
                    f"writes={upload['totalWrites']}"
                )

            month_result["upload"] = upload
            month_result["status"] = "PASS"
            total_created += int(upload["totalCreated"])
            total_updated += int(upload["totalUpdated"])
            total_unchanged += int(upload["totalUnchanged"])

            print("")
            print(
                f"[MONTH VERIFIED] {month} | created={upload['totalCreated']:,} | "
                f"updated={upload['totalUpdated']:,} | "
                f"unchanged={upload['totalUnchanged']:,} | verification=PASS"
            )
            print("")

        summary.update(
            {
                "status": "PASS",
                "result": "DEV_RANGE_UPLOAD_VERIFIED",
                "monthsPassed": len(months),
                "monthsFailed": 0,
                "totalDocumentsCreated": total_created,
                "totalDocumentsUpdated": total_updated,
                "totalDocumentsUnchanged": total_unchanged,
                "totalWriteOperations": total_created + total_updated,
            }
        )

        print("=" * 76)
        print("STAGE 04C DEV RANGE UPLOAD COMPLETE")
        print("=" * 76)
        print("Status                  : PASS")
        print(f"Months uploaded         : {len(months):,}")
        print("Months failed           : 0")
        print(f"Total documents created : {total_created:,}")
        print(f"Total documents updated : {total_updated:,}")
        print(f"Total unchanged         : {total_unchanged:,}")
        print(f"Write semantics         : {args.mode}")
        print("Deletes                 : 0")
        print("Post-write verification : PASS for every month")
        print("=" * 76)
        return 0

    except Exception as exc:
        summary.update(
            {
                "status": "FAIL",
                "result": "DEV_RANGE_UPLOAD_FAILED",
                "failedMonth": current_month,
                "monthsPassed": sum(
                    1 for item in summary.get("months", []) if item.get("status") == "PASS"
                ),
                "monthsFailed": 1,
                "errorType": type(exc).__name__,
                "error": str(exc),
            }
        )
        print("")
        print("=" * 76, file=sys.stderr)
        print("STAGE 04C STOPPED", file=sys.stderr)
        print("=" * 76, file=sys.stderr)
        print(f"Failed month : {current_month}", file=sys.stderr)
        print(f"Reason       : {exc}", file=sys.stderr)
        print("No later month was attempted.", file=sys.stderr)
        if args.mode == "refresh":
            print(
                "For refresh failures, review the Stage 04 report and rerun the same "
                "governed refresh only after resolving conflicts; do not switch to resume.",
                file=sys.stderr,
            )
        else:
            print(
                "If Stage 04 partially wrote the failed month, use its governed "
                "resume mode only after reviewing the failure report.",
                file=sys.stderr,
            )
        print("=" * 76, file=sys.stderr)
        return 1

    finally:
        summary["finishedAt"] = utc_iso(utc_now())
        try:
            json_path, csv_path = write_summary(
                run_root,
                started=started,
                summary=summary,
            )
            print("")
            print(f"[RANGE REPORT JSON] {json_path}")
            print(f"[RANGE REPORT CSV ] {csv_path}")
        except Exception as report_exc:
            print(
                f"[WARN] Could not write Stage 04C range report: {report_exc}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    raise SystemExit(main())
