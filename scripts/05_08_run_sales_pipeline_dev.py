"""One-command DEV orchestrator for the iREPS monthly-source Sales Pipeline.

Default invocation performs only local Stage 05/06 builds and validations.
Firestore writes require ALL of:
  --project-id ireps2 --confirm-project ireps2 --execute
  --confirm-sales-all-validator-deployed ireps2

When --execute is supplied the governed sequence is:
  Stage 05 build -> Stage 06 build -> Stage 07 refresh preflight ->
  Stage 08 refresh preflight -> Stage 08 refresh+full verify ->
  Stage 07 refresh+verify -> visibility reconciliation preflight ->
  visibility reconciliation+full verify.

Both Stage 07 and Stage 08 Firestore preflights must pass before the FIRST
Firestore write. The first write stage is Stage 08, so Sales All targets exist
before Meter Master sales links are published. Explicit final reconciliation
then covers both changed and already-unchanged Meter Master documents.

The orchestrator never deletes documents and stops on the first failed
child stage or governed report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"
DEV_PROJECT = "ireps2"
EXPECTED_SCRIPT_SHA256 = {
    "05_build_meter_master_v3.py": "37d61e02b94969ba6879787ab481db5b02ed6d37be74183738051d9dec154a7a",
    "06_build_sales_all_meters.py": "58a68f209618f81730fc66c1af1f25764d3a961e243a2195fecaff707f59c7af",
    "07_upload_meter_master_v3.py": "6beea2b87984c61498dd014ca0693559acff95651f83abd5f01fe212d79982f3",
    "08_upload_sales_all_meters.py": "335507bae9f5f4d1145dbf064a3af805af1a3fa0d9c09a53c4dbbd0639045f5c",
    "sales_pipeline_monthly_source_support.py": "1cfebcad96850cb9872f2a966d8955e366511e6fc622a974b845a4897b009319",
    "sales_pipeline_sales_all_refresh.py": "95d0a2ec3bdc4ea58712bb8a1cc70b0b7dd8c2aad899d8ec07023977562e9fc0",
    "sales_address_enrichment.py": "e438676e50c855ed78ff75824e540b11161b5ecba2acde8754435e048d847d05",
    "sales_pipeline_visibility_reconciliation_dev.py": "5f523405b857d0264845feaf69af1bc6263d6582f2e76994bbcc27e8c8f57734",
}


def safe(value: Any) -> str:
    return "" if value is None else str(value).strip()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run governed Sales Pipeline Stages 05-08 in ireps2 DEV.")
    p.add_argument("--project-id", default=DEV_PROJECT)
    p.add_argument("--confirm-project", default=DEV_PROJECT)
    p.add_argument("--service-account", type=Path, help="Required only with --execute; must belong to ireps2.")
    p.add_argument("--lm-pcode", required=True)
    p.add_argument("--provider", required=True)
    p.add_argument("--from-month", required=True)
    p.add_argument("--to-month", required=True)
    p.add_argument("--commercial-source", required=True, type=Path)
    p.add_argument("--expected-commercial-source-sha256", required=True)
    p.add_argument("--source-run-id", required=True)
    p.add_argument("--monthly-dir", type=Path, default=Path("output/monthly"))
    p.add_argument("--execute", action="store_true", help="Enable governed DEV Firestore writes after all mandatory gates pass.")
    p.add_argument(
        "--confirm-sales-all-validator-deployed",
        help="With --execute, must be exactly 'ireps2' to confirm the reviewed rich Sales All validator has already been deployed to DEV.",
    )
    return p.parse_args()


def resolve(path: Path) -> Path:
    return (path if path.is_absolute() else PROJECT_ROOT / path).expanduser().resolve()


def validate_script_hashes() -> None:
    for name, expected in EXPECTED_SCRIPT_SHA256.items():
        path = SCRIPTS / name
        if not path.is_file():
            raise FileNotFoundError(f"Required script missing: {path}")
        actual = sha256(path)
        if actual != expected:
            raise RuntimeError(f"Script SHA mismatch for {name}: expected={expected}; actual={actual}")


def run_child(label: str, command: list[str]) -> None:
    print("\n" + "=" * 78)
    print(label)
    print("=" * 78)
    print("COMMAND:", " ".join(command))
    result = subprocess.run(command, cwd=PROJECT_ROOT)
    if result.returncode != 0:
        raise RuntimeError(f"{label} failed with exit code {result.returncode}")


def require_manifest(path: Path, stage: str, schema: int = 2) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Expected Stage {stage} manifest not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("stage") != stage or payload.get("schemaVersion") != schema:
        raise RuntimeError(f"Unexpected Stage {stage} manifest identity: {path}")
    if payload.get("status") != "PASS" or payload.get("result") != "BUILD_WRITTEN":
        raise RuntimeError(f"Stage {stage} manifest is not BUILD_WRITTEN/PASS")
    return payload


def newest_report(directory: Path, pattern: str) -> tuple[Path, dict[str, Any]]:
    files = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime_ns)
    if not files:
        raise FileNotFoundError(f"Expected report not found in {directory}: {pattern}")
    path = files[-1]
    return path, json.loads(path.read_text(encoding="utf-8"))


def require_report(directory: Path, pattern: str, allowed_results: set[str]) -> tuple[Path, dict[str, Any]]:
    path, payload = newest_report(directory, pattern)
    if payload.get("status") != "PASS" or payload.get("result") not in allowed_results:
        raise RuntimeError(f"Report is not an approved PASS: {path}; result={payload.get('result')}")
    return path, payload


def main() -> None:
    args = parse_args()
    if safe(args.project_id) != DEV_PROJECT or safe(args.confirm_project) != DEV_PROJECT:
        raise ValueError("This orchestrator is DEV-only and hard-gated to project ireps2")
    service_account = resolve(args.service_account) if args.service_account is not None else None
    commercial = resolve(args.commercial_source)
    monthly_dir = resolve(args.monthly_dir)
    if args.execute:
        if service_account is None or not service_account.is_file():
            raise FileNotFoundError("--execute requires a valid --service-account file")
        sa = json.loads(service_account.read_text(encoding="utf-8"))
        if safe(sa.get("project_id")) != DEV_PROJECT:
            raise ValueError("Service-account project_id is not ireps2")
        if safe(args.confirm_sales_all_validator_deployed) != DEV_PROJECT:
            raise ValueError(
                "--execute requires --confirm-sales-all-validator-deployed ireps2 "
                "after the reviewed Sales All validator has been deployed and verified in DEV"
            )
    if sha256(commercial) != safe(args.expected_commercial_source_sha256).lower():
        raise ValueError("Commercial source SHA256 does not match --expected-commercial-source-sha256")
    validate_script_hashes()

    lm = safe(args.lm_pcode).upper()
    provider = safe(args.provider).lower()
    first = safe(args.from_month)
    last = safe(args.to_month)
    meter_master = PROJECT_ROOT / "output" / "meter_master" / f"meter_master__{lm}__FULL__{first}_to_{last}.csv"
    meter_manifest = meter_master.with_suffix(".manifest.json")
    sales_all = PROJECT_ROOT / "output" / "sales_all_meters" / f"sales_all_meters__{lm}__FULL__{first}_to_{last}.csv"
    sales_manifest = sales_all.with_suffix(".manifest.json")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = PROJECT_ROOT / "output" / "logs" / "sales_pipeline_dev_orchestrator" / f"run__{run_id}"
    run_dir.mkdir(parents=True, exist_ok=False)

    common05 = [
        sys.executable, str(SCRIPTS / "05_build_meter_master_v3.py"),
        "--lm-pcode", lm, "--from-month", first, "--to-month", last,
        "--provider", provider, "--source-origin", "monthly_source",
        "--commercial-source", str(commercial),
        "--expected-commercial-source-sha256", safe(args.expected_commercial_source_sha256).lower(),
        "--source-run-id", safe(args.source_run_id), "--monthly-dir", str(monthly_dir),
        "--output", str(meter_master),
    ]
    run_child("STAGE 05 — BUILD METER MASTER", common05)
    m05 = require_manifest(meter_manifest, "05")
    if m05["sourceContract"].get("provider") != provider or m05["sourceContract"].get("lmPcode") != lm:
        raise RuntimeError("Stage 05 provider/LM manifest mismatch")

    common06 = [
        sys.executable, str(SCRIPTS / "06_build_sales_all_meters.py"),
        "--lm-pcode", lm, "--from-month", first, "--to-month", last,
        "--provider", provider, "--source-origin", "monthly_source",
        "--master", str(meter_master), "--master-manifest", str(meter_manifest),
        "--commercial-source", str(commercial),
        "--expected-commercial-source-sha256", safe(args.expected_commercial_source_sha256).lower(),
        "--monthly-dir", str(monthly_dir), "--output", str(sales_all),
    ]
    run_child("STAGE 06 — BUILD SALES ALL METERS", common06)
    m06 = require_manifest(sales_manifest, "06")
    if m06["sourceContract"].get("provider") != provider or m06["sourceContract"].get("lmPcode") != lm:
        raise RuntimeError("Stage 06 provider/LM manifest mismatch")

    local_summary = {
        "stage": "05-08", "projectId": DEV_PROJECT, "lmPcode": lm, "provider": provider,
        "fromMonth": first, "toMonth": last, "executeRequested": bool(args.execute),
        "salesAllValidatorDeploymentConfirmedFor": safe(args.confirm_sales_all_validator_deployed) or None,
        "stage05": {"manifest": str(meter_manifest), "fingerprint": m05.get("buildFingerprint"), "rows": m05["outputContract"].get("rows")},
        "stage06": {"manifest": str(sales_manifest), "fingerprint": m06.get("buildFingerprint"), "rows": m06["outputContract"].get("rows")},
    }
    (run_dir / "local_build_summary.json").write_text(json.dumps(local_summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if not args.execute:
        print("\nLOCAL BUILD / CONTRACT VALIDATION PASS")
        print("Firestore writes: 0")
        print("Run again with --execute only after review approval.")
        print(f"Run dir: {run_dir}")
        return

    assert service_account is not None
    stage07_pre = run_dir / "stage07_preflight"
    stage07_exec = run_dir / "stage07_refresh"
    stage08_pre = run_dir / "stage08_preflight"
    stage08_exec = run_dir / "stage08_refresh"
    visibility_pre = run_dir / "visibility_reconciliation_preflight"
    visibility_exec = run_dir / "visibility_reconciliation"

    base07 = [
        sys.executable, str(SCRIPTS / "07_upload_meter_master_v3.py"),
        "--project-id", DEV_PROJECT, "--confirm-project", DEV_PROJECT,
        "--service-account", str(service_account), "--input", str(meter_master),
        "--manifest", str(meter_manifest), "--mode", "refresh",
    ]
    run_child("STAGE 07 — REFRESH PREFLIGHT (NO WRITES)", base07 + ["--preflight-only", "--report-dir", str(stage07_pre)])
    r07p, p07 = require_report(stage07_pre, "meter_master_upload__ireps2__*.json", {"PREFLIGHT_PASS"})

    base08 = [
        sys.executable, str(SCRIPTS / "08_upload_sales_all_meters.py"),
        "--project-id", DEV_PROJECT, "--confirm-project", DEV_PROJECT,
        "--service-account", str(service_account), "--input", str(sales_all),
        "--manifest", str(sales_manifest), "--mode", "refresh",
    ]
    run_child("STAGE 08 — REFRESH PREFLIGHT (NO WRITES)", base08 + ["--preflight-only", "--report-dir", str(stage08_pre)])
    r08p, p08 = require_report(stage08_pre, "sales_all_meters_refresh__ireps2__*.json", {"PREFLIGHT_PASS"})

    # Bind both preflights to the exact current Stage 05/06 build evidence before any write.
    if p07.get("csvSha256") != sha256(meter_master):
        raise RuntimeError("Stage 07 preflight is not bound to the current Stage 05 CSV")
    if (p08.get("sourceEvidence") or {}).get("csvSha256") != sha256(sales_all):
        raise RuntimeError("Stage 08 preflight is not bound to the current Stage 06 CSV")

    print("\n[BOTH PREFLIGHTS PASS] FIRST FIRESTORE WRITE will be Stage 08 Sales All refresh.")

    # FIRST FIRESTORE WRITE: create/refresh Sales All before publishing Meter Master sales links.
    run_child("STAGE 08 — REFRESH + FULL INPUT-SCOPE VERIFY", base08 + ["--report-dir", str(stage08_exec)])
    r08, w08 = require_report(stage08_exec, "sales_all_meters_refresh__ireps2__*.json", {"REFRESH_VERIFIED"})
    if (w08.get("verification") or {}).get("status") != "PASS":
        raise RuntimeError("Stage 08 full input-scope verification did not PASS")

    # SECOND WRITE STAGE: Meter Master sales links.
    run_child("STAGE 07 — REFRESH + VERIFY", base07 + ["--report-dir", str(stage07_exec)])
    r07, w07 = require_report(stage07_exec, "meter_master_upload__ireps2__*.json", {"COMPLETED"})

    # Mandatory explicit operational reconciliation. This is required even when
    # Stage 07 classified an existing Meter Master document as UNCHANGED and no
    # Cloud Function event was emitted.
    base_visibility = [
        sys.executable, str(SCRIPTS / "sales_pipeline_visibility_reconciliation_dev.py"),
        "--project-id", DEV_PROJECT, "--confirm-project", DEV_PROJECT,
        "--service-account", str(service_account),
        "--input", str(meter_master), "--manifest", str(meter_manifest),
        "--sales-input", str(sales_all), "--sales-manifest", str(sales_manifest),
    ]
    run_child(
        "VISIBILITY RECONCILIATION — PREFLIGHT (NO WRITES)",
        base_visibility + ["--preflight-only", "--report-dir", str(visibility_pre)],
    )
    rvp, vp = require_report(
        visibility_pre,
        "sales_all_visibility_reconciliation__ireps2__*.json",
        {"PREFLIGHT_PASS"},
    )
    if (vp.get("sourceEvidence") or {}).get("documentIdsSha256") != p07.get("documentIdsSha256"):
        raise RuntimeError("Visibility preflight Stage 05 scope hash mismatch")
    if ((vp.get("sourceEvidence") or {}).get("stage06") or {}).get("stage06DocumentIdsSha256") != m06["outputContract"].get("documentIdsSha256"):
        raise RuntimeError("Visibility preflight Stage 06 scope hash mismatch")

    run_child(
        "VISIBILITY RECONCILIATION — APPLY + FULL VERIFY",
        base_visibility + ["--report-dir", str(visibility_exec)],
    )
    rv, vr = require_report(
        visibility_exec,
        "sales_all_visibility_reconciliation__ireps2__*.json",
        {"RECONCILIATION_VERIFIED"},
    )
    if (vr.get("verification") or {}).get("status") != "PASS":
        raise RuntimeError("Visibility reconciliation full verification did not PASS")
    if int(vr.get("conflictCount", -1)) != 0 or int(vr.get("failedCount", -1)) != 0:
        raise RuntimeError("Visibility reconciliation reported conflict/failure")

    final = {
        **local_summary,
        "status": "PASS", "result": "DEV_PIPELINE_VERIFIED",
        "writeOrder": ["STAGE_08", "STAGE_07", "VISIBILITY_RECONCILIATION"],
        "firstFirestoreWrite": "STAGE_08_SALES_ALL_REFRESH",
        "stage07PreflightReport": str(r07p), "stage07RefreshReport": str(r07),
        "stage08PreflightReport": str(r08p), "stage08RefreshReport": str(r08),
        "visibilityPreflightReport": str(rvp),
        "visibilityReconciliationReport": str(rv),
        "reportSha256": {
            "stage07Preflight": sha256(r07p),
            "stage07Refresh": sha256(r07),
            "stage08Preflight": sha256(r08p),
            "stage08Refresh": sha256(r08),
            "visibilityPreflight": sha256(rvp),
            "visibilityReconciliation": sha256(rv),
        },
        "deletes": 0,
        "finishedAt": datetime.now(UTC).isoformat(),
    }
    final_path = run_dir / "stage05_08_dev_final_report.json"
    final_path.write_text(json.dumps(final, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("\n" + "=" * 78)
    print("STAGE 05-08 DEV ORCHESTRATION COMPLETE")
    print("=" * 78)
    print("Status: PASS")
    print("Project: ireps2")
    print("Deletes: 0")
    print(f"Final report: {final_path}")


if __name__ == "__main__":
    main()
