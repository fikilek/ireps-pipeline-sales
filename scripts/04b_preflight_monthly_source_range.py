"""
Stage 04B: governed read-only preflight sweep for monthly-source Stage 04 uploads.

This helper NEVER invokes Stage 04 with --execute-upload.
It:
- validates the requested Firebase project against the service-account JSON locally;
- pins every month to one exact Stage 03B BUILD_WRITTEN run ID;
- verifies the exact cleaned-source SHA256 before any Firestore access;
- runs the existing Stage 04 uploader once per month using --preflight-only;
- stops immediately on the first failed month;
- validates the Stage 04 JSON audit report for every successful month;
- writes one sweep JSON report and one CSV summary.

Firestore behavior:
- Stage 04 performs Firestore reads for provider/scope preflight.
- This helper never requests Firestore writes.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STAGE04_SCRIPT = PROJECT_ROOT / "scripts" / "04_upload_conlog_monthly_v3.py"
DEFAULT_MANIFEST_DIR = PROJECT_ROOT / "output" / "logs" / "monthly_source_build"
DEFAULT_LOG_DIR = PROJECT_ROOT / "output" / "logs" / "monthly_preflight_sweep"

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

EXPECTED_SOURCE_STAGE = "03B"
EXPECTED_SOURCE_SCRIPT = "03b_build_monthly_from_monthly_source.py"
EXPECTED_SOURCE_RESULT = "BUILD_WRITTEN"
EXPECTED_SOURCE_OPERATION = "build-write"
EXPECTED_SOURCE_ORIGIN = "monthly_source"

EXPECTED_STAGE04_SCRIPT = "04_upload_conlog_monthly_v3.py"
EXPECTED_STAGE04_RESULT = "PREFLIGHT_OK"

DATASET_ORDER = ("monthly", "monthly_lm", "monthly_lm_groups")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run Stage 04 preflight-only across a governed monthly-source month range. "
            "No Firestore upload operation is exposed by this helper."
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
        help="Stage 04 preflight mode. refresh is monthly_source-only recurring refresh.",
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


def run_id(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_month(value: str) -> tuple[int, int]:
    if not MONTH_RE.fullmatch(value):
        raise ValueError(f"Month must use YYYY-MM format: {value!r}")
    year, month = value.split("-")
    return int(year), int(month)


def month_range(start: str, end: str) -> list[str]:
    start_y, start_m = parse_month(start)
    end_y, end_m = parse_month(end)
    if (start_y, start_m) > (end_y, end_m):
        raise ValueError(f"--from-month must not be after --to-month: {start} > {end}")

    values: list[str] = []
    year, month = start_y, start_m
    while (year, month) <= (end_y, end_m):
        values.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return values


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def validate_local_args(args: argparse.Namespace) -> tuple[str, str, str]:
    project_id = clean(args.project_id)
    confirm_project = clean(args.confirm_project)
    lm_pcode = clean(args.lm_pcode).upper()
    provider = clean(args.provider).lower()
    expected_sha = clean(args.expected_input_sha256).lower()
    source_run_id = clean(args.source_run_id)

    if not project_id:
        raise ValueError("--project-id may not be blank")
    if confirm_project != project_id:
        raise ValueError(
            f"[SAFETY] --confirm-project must exactly match --project-id: "
            f"{confirm_project!r} != {project_id!r}"
        )
    if not lm_pcode:
        raise ValueError("--lm-pcode may not be blank")
    if not provider:
        raise ValueError("--provider may not be blank")
    if not args.vending_provider_id or not clean(args.vending_provider_id):
        raise ValueError("--vending-provider-id may not be blank")
    if not RUN_ID_RE.fullmatch(source_run_id):
        raise ValueError(
            f"--source-run-id must use YYYYMMDDTHHMMSSZ: {source_run_id!r}"
        )
    if not SHA256_RE.fullmatch(expected_sha):
        raise ValueError("--expected-input-sha256 must be a lowercase 64-char SHA256")

    service_account = args.service_account.expanduser().resolve()
    if not service_account.is_file():
        raise ValueError(f"Service-account file not found: {service_account}")
    credential = read_json(service_account)
    credential_project = clean(credential.get("project_id"))
    if credential_project != project_id:
        raise ValueError(
            "[SAFETY] Service-account project mismatch. "
            f"Requested={project_id!r}, credential={credential_project!r}"
        )

    stage04 = args.stage04_script.expanduser().resolve()
    if not stage04.is_file():
        raise ValueError(f"Stage 04 script not found: {stage04}")
    if stage04.name != EXPECTED_STAGE04_SCRIPT:
        raise ValueError(
            f"Unexpected Stage 04 script name: {stage04.name!r}; "
            f"expected {EXPECTED_STAGE04_SCRIPT!r}"
        )

    manifest_dir = args.manifest_dir.expanduser().resolve()
    if not manifest_dir.is_dir():
        raise ValueError(f"Stage 03B manifest directory not found: {manifest_dir}")

    return lm_pcode, provider, expected_sha


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
    expected_sha: str,
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
        raise ValueError(f"Stage 03B manifest has no sourceInput object: {path}")

    actual_sha = clean(source_input.get("sha256")).lower()
    if actual_sha != expected_sha:
        raise ValueError(
            f"Stage 03B source SHA mismatch for {month}: "
            f"expected={expected_sha}, actual={actual_sha}"
        )

    outputs = payload.get("outputs")
    if not isinstance(outputs, list) or len(outputs) != 3:
        raise ValueError(
            f"Stage 03B manifest must declare exactly 3 outputs for {month}: {path}"
        )

    datasets = [clean(item.get("dataset")) for item in outputs if isinstance(item, dict)]
    if sorted(datasets) != sorted(DATASET_ORDER):
        raise ValueError(
            f"Stage 03B manifest datasets mismatch for {month}: {datasets}"
        )

    return payload


def stage04_report_path(
    report_dir: Path,
    *,
    project_id: str,
    lm_pcode: str,
    month: str,
) -> Path:
    matches = sorted(
        report_dir.glob(
            f"stage04_monthly_upload__{project_id}__{lm_pcode}__{month}__*.json"
        )
    )
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one Stage 04 report for {month}; found {len(matches)} "
            f"in {report_dir}"
        )
    return matches[0]


def validate_stage04_report(
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

    expected_identity = {
        "stage": "04",
        "script": EXPECTED_STAGE04_SCRIPT,
        "status": "PASS",
        "result": EXPECTED_STAGE04_RESULT,
        "operation": "preflight-only",
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
    actual_identity = {key: clean(report.get(key)) for key in expected_identity}
    if actual_identity != expected_identity:
        raise ValueError(
            f"Stage 04 report identity mismatch for {month}. "
            f"Expected={expected_identity}; actual={actual_identity}"
        )

    provider_doc = report.get("provider")
    if not isinstance(provider_doc, dict):
        raise ValueError(f"Stage 04 report provider evidence missing for {month}")
    if clean(provider_doc.get("status")).lower() != "active":
        raise ValueError(f"Provider is not active in Stage 04 report for {month}")
    if clean(provider_doc.get("providerCode")).lower() != provider:
        raise ValueError(
            f"Provider code mismatch in Stage 04 report for {month}: "
            f"{provider_doc.get('providerCode')!r}"
        )

    preflight = report.get("preflight")
    if not isinstance(preflight, dict):
        raise ValueError(f"Stage 04 report preflight object missing for {month}")

    inputs = report.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError(f"Stage 04 report inputs object missing for {month}")

    month_result: dict[str, Any] = {
        "month": month,
        "status": "PASS",
        "stage04Report": str(path),
        "sourceFingerprint": clean(
            (report.get("sourceContract") or {}).get("fingerprint")
            if isinstance(report.get("sourceContract"), dict)
            else ""
        ),
        "datasets": {},
        "plannedDocuments": 0,
    }

    for dataset in DATASET_ORDER:
        state = preflight.get(dataset)
        source = inputs.get(dataset)
        if not isinstance(state, dict) or not isinstance(source, dict):
            raise ValueError(
                f"Stage 04 report dataset evidence missing for {dataset}/{month}"
            )

        documents_before = int(state.get("documentsBefore", -1))
        planned = int(state.get("documentsPlanned", -1))
        planned_create = int(state.get("documentsPlannedCreate", -1))
        planned_update = int(state.get("documentsPlannedUpdate", -1))
        unchanged = int(state.get("unchangedDocuments", -1))
        conflicts = int(state.get("conflictCount", -1))
        extras = int(state.get("extraDocumentCount", -1))
        input_rows = int(source.get("rows", -1))

        if conflicts != 0 or extras != 0:
            raise ValueError(
                f"Preflight conflict/extra detected for {dataset}/{month}: "
                f"conflicts={conflicts}, extras={extras}"
            )
        if planned != planned_create + planned_update:
            raise ValueError(
                f"Planned write accounting mismatch for {dataset}/{month}: "
                f"planned={planned}, create={planned_create}, update={planned_update}"
            )
        if planned_create + planned_update + unchanged != input_rows:
            raise ValueError(
                f"Refresh accounting/input mismatch for {dataset}/{month}: "
                f"create={planned_create}, update={planned_update}, "
                f"unchanged={unchanged}, rows={input_rows}"
            )
        if mode == "create-only":
            if documents_before != 0:
                raise ValueError(
                    f"Create-only preflight expected empty scope for {dataset}/{month}; "
                    f"documentsBefore={documents_before}"
                )
            if planned_create != input_rows or planned_update != 0 or unchanged != 0:
                raise ValueError(
                    f"Create-only accounting mismatch for {dataset}/{month}: "
                    f"create={planned_create}, update={planned_update}, "
                    f"unchanged={unchanged}, rows={input_rows}"
                )

        month_result["datasets"][dataset] = {
            "collection": clean(state.get("collection")),
            "rows": input_rows,
            "existing": documents_before,
            "create": planned_create,
            "update": planned_update,
            "unchanged": unchanged,
            "plannedWrites": planned,
            "conflicts": conflicts,
            "extra": extras,
        }
        month_result["plannedDocuments"] += planned

    return month_result


def write_sweep_report(
    log_dir: Path,
    *,
    started: dt.datetime,
    summary: dict[str, Any],
) -> tuple[Path, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    tag = run_id(started)
    json_path = log_dir / f"stage04b_monthly_preflight_sweep__{tag}.json"
    csv_path = log_dir / f"stage04b_monthly_preflight_sweep__{tag}.csv"

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
                "monthlyCreate",
                "monthlyUpdate",
                "monthlyUnchanged",
                "monthlyLmCreate",
                "monthlyLmUpdate",
                "monthlyLmUnchanged",
                "monthlyLmGroupsCreate",
                "monthlyLmGroupsUpdate",
                "monthlyLmGroupsUnchanged",
                "totalWrites",
                "stage04Report",
            ],
        )
        writer.writeheader()
        for item in summary.get("months", []):
            datasets = item.get("datasets", {})
            writer.writerow(
                {
                    "month": item.get("month"),
                    "status": item.get("status"),
                    "monthlyCreate": (datasets.get("monthly") or {}).get("create", ""),
                    "monthlyUpdate": (datasets.get("monthly") or {}).get("update", ""),
                    "monthlyUnchanged": (datasets.get("monthly") or {}).get("unchanged", ""),
                    "monthlyLmCreate": (datasets.get("monthly_lm") or {}).get("create", ""),
                    "monthlyLmUpdate": (datasets.get("monthly_lm") or {}).get("update", ""),
                    "monthlyLmUnchanged": (datasets.get("monthly_lm") or {}).get("unchanged", ""),
                    "monthlyLmGroupsCreate": (
                        datasets.get("monthly_lm_groups") or {}
                    ).get("create", ""),
                    "monthlyLmGroupsUpdate": (
                        datasets.get("monthly_lm_groups") or {}
                    ).get("update", ""),
                    "monthlyLmGroupsUnchanged": (
                        datasets.get("monthly_lm_groups") or {}
                    ).get("unchanged", ""),
                    "totalWrites": item.get("plannedDocuments", ""),
                    "stage04Report": item.get("stage04Report", ""),
                }
            )

    return json_path, csv_path


def main() -> int:
    args = parse_args()
    started = utc_now()
    log_dir = args.log_dir.expanduser().resolve()
    sweep_dir = log_dir / f"run__{run_id(started)}"
    stage04_report_dir = sweep_dir / "stage04_reports"
    stage04_report_dir.mkdir(parents=True, exist_ok=False)

    summary: dict[str, Any] = {
        "stage": "04B",
        "script": "04b_preflight_monthly_source_range.py",
        "status": "STARTED",
        "result": "STARTED",
        "operation": "preflight-only-sweep",
        "firestoreWritesRequested": False,
        "startedAt": utc_iso(started),
        "months": [],
    }

    try:
        lm_pcode, provider, expected_sha = validate_local_args(args)
        months = month_range(args.from_month, args.to_month)
        source_run_id = clean(args.source_run_id)
        manifest_dir = args.manifest_dir.expanduser().resolve()
        stage04_script = args.stage04_script.expanduser().resolve()
        service_account = args.service_account.expanduser().resolve()
        vending_provider_id = clean(args.vending_provider_id)

        summary.update(
            {
                "targetProject": clean(args.project_id),
                "credentialProject": clean(args.project_id),
                "lmPcode": lm_pcode,
                "provider": provider,
                "vendingProviderId": vending_provider_id,
                "fromMonth": args.from_month,
                "toMonth": args.to_month,
                "monthsExpected": len(months),
                "sourceRunId": source_run_id,
                "expectedInputSha256": expected_sha,
                "manifestDir": str(manifest_dir),
                "stage04Script": str(stage04_script),
                "stage04ReportDir": str(stage04_report_dir),
                "mode": args.mode,
            }
        )

        # Validate all 03B manifests before the first Firestore call.
        manifests: dict[str, Path] = {}
        print("=" * 72)
        print("iREPS SALES PIPELINE — STAGE 04B MONTHLY PREFLIGHT SWEEP")
        print("=" * 72)
        print(f"Project       : {args.project_id}")
        print(f"LM            : {lm_pcode}")
        print(f"Provider      : {provider}")
        print(f"Provider ID   : {vending_provider_id}")
        print(f"Range         : {args.from_month} -> {args.to_month} ({len(months)} months)")
        print(f"Source run    : {source_run_id}")
        print(f"Input SHA256  : {expected_sha}")
        print(f"Stage 04 mode : {args.mode} / preflight-only")
        print("Firestore writes requested: NO")
        print("=" * 72)
        print("[LOCAL GATE] Validating every Stage 03B manifest before Firestore reads...")

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
                expected_sha=expected_sha,
            )
            manifests[month] = path

        print(f"[LOCAL GATE PASS] {len(manifests)} manifests validated.")
        print("")

        total_planned = 0
        for index, month in enumerate(months, start=1):
            print("=" * 72)
            print(f"[PREFLIGHT {index}/{len(months)}] {month}")
            print("=" * 72)

            command = [
                sys.executable,
                str(stage04_script),
                "--project-id",
                clean(args.project_id),
                "--confirm-project",
                clean(args.project_id),
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
                "--log-dir",
                str(stage04_report_dir),
                "--preflight-only",
            ]

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
                print(line, end="")
            return_code = process.wait()

            if return_code != 0:
                raise RuntimeError(
                    f"Stage 04 preflight failed for {month} with exit code {return_code}. "
                    "Sweep stopped immediately. No upload command was invoked."
                )

            report_path = stage04_report_path(
                stage04_report_dir,
                project_id=clean(args.project_id),
                lm_pcode=lm_pcode,
                month=month,
            )
            month_result = validate_stage04_report(
                report_path,
                project_id=clean(args.project_id),
                lm_pcode=lm_pcode,
                month=month,
                provider=provider,
                vending_provider_id=vending_provider_id,
                mode=args.mode,
            )
            summary["months"].append(month_result)
            total_planned += int(month_result["plannedDocuments"])

            d = month_result["datasets"]
            print(
                f"[MONTH PASS] {month} | "
                f"monthly C/U={d['monthly']['create']:,}/{d['monthly']['update']:,} | "
                f"lm C/U={d['monthly_lm']['create']:,}/{d['monthly_lm']['update']:,} | "
                f"groups C/U={d['monthly_lm_groups']['create']:,}/{d['monthly_lm_groups']['update']:,} | "
                f"writes={month_result['plannedDocuments']:,}"
            )
            print("")

        summary.update(
            {
                "status": "PASS",
                "result": "PREFLIGHT_SWEEP_PASS",
                "monthsPassed": len(summary["months"]),
                "monthsFailed": 0,
                "totalDocumentsPlanned": total_planned,
            }
        )
        print("=" * 72)
        print("STAGE 04B PREFLIGHT SWEEP COMPLETE")
        print("=" * 72)
        print("Status                  : PASS")
        print(f"Months checked          : {len(summary['months']):,}")
        print(f"Months failed           : 0")
        print(f"Total documents planned : {total_planned:,}")
        print("Firestore writes        : 0")
        print("Upload commands invoked : 0")
        print("=" * 72)
        return 0

    except Exception as exc:
        summary.update(
            {
                "status": "FAIL",
                "result": "PREFLIGHT_SWEEP_FAILED",
                "monthsPassed": len(summary.get("months", [])),
                "monthsFailed": 1,
                "errorType": type(exc).__name__,
                "error": str(exc),
            }
        )
        print("")
        print(f"[SWEEP FAILED] {exc}", file=sys.stderr)
        return 1

    finally:
        summary["finishedAt"] = utc_iso(utc_now())
        try:
            json_path, csv_path = write_sweep_report(
                sweep_dir,
                started=started,
                summary=summary,
            )
            print("")
            print(f"[SWEEP REPORT JSON] {json_path}")
            print(f"[SWEEP REPORT CSV ] {csv_path}")
        except Exception as report_exc:
            print(f"[WARN] Could not write Stage 04B sweep report: {report_exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
