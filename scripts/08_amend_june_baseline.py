"""Stage 08 amendment of the independently pinned original June baseline only."""
import argparse
from pathlib import Path
from sales_pipeline_sales_all_refresh import run_refresh


ALLOWED_PROJECTS = ["ireps2", "ireps-test", "ireps-5c3e9"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True, choices=ALLOWED_PROJECTS)
    parser.add_argument("--confirm-project", required=True, choices=ALLOWED_PROJECTS)
    parser.add_argument("--service-account", type=Path, required=True)
    parser.add_argument("--mode", choices=["refresh"], required=True)
    parser.add_argument("--june-package", type=Path, required=True)
    parser.add_argument("--june-package-sha256", required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    if args.project_id != args.confirm_project:
        raise ValueError(
            f"Project confirmation failed: --project-id={args.project_id!r}, "
            f"--confirm-project={args.confirm_project!r}"
        )
    result = run_refresh(project_id=args.project_id, confirm_project=args.confirm_project,
        service_account_path=args.service_account, input_path=None, manifest_path=None,
        report_dir=args.report_dir, preflight_only=args.preflight_only,
        june_package_path=args.june_package, june_package_sha256=args.june_package_sha256)
    print(f"June baseline report: {result}")


if __name__ == "__main__":
    main()
