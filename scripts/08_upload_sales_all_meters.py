"""
08_upload_sales_all_meters.py

Reusable, controlled uploader for an approved Sales All Meters staging CSV.

Design goals
------------
- No hard-coded Firebase project, service account, input CSV, or month list.
- The service-account project must match --project-id.
- --confirm-project must exactly match --project-id.
- create-only requires an empty sales-all-meters collection.
- resume creates only missing documents, skips exact matches, and blocks conflicts.
- Dynamic monthly columns become the Firestore monthlyTotalsC map.
- No merge=True writes.
- A SHA-256 fingerprint and JSON report provide traceability.

Example
-------
python .\\scripts\\08_upload_sales_all_meters.py `
  --project-id ireps-test `
  --confirm-project ireps-test `
  --service-account "C:\\dev\\secrets\\ireps-test-firebase-adminsdk-fbsvc-d02929e1e3.json" `
  --input .\\output\\sales_all_meters\\sales_all_meters__ZA7423__FULL__2025-09_to_2026-06.csv `
  --mode create-only `
  --report-dir .\\output\\sales_all_meters\\upload-reports
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import pandas as pd
from google.cloud import firestore
from google.oauth2 import service_account
from tqdm import tqdm


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTION_NAME = "sales-all-meters"
BATCH_SIZE = 450
MONTH_COLUMN_RE = re.compile(r"^amount_(\d{4})_(\d{2})_C$")

BASE_COLUMNS = [
    "masterId",
    "visibility",
    "meterNo",
    "meterNoNormalized",
    "provider",
    "customerNo",
    "accountNo",
    "totalAmountC",
    "lastPurchaseAtISO",
    "daysSinceLastPurchase",
]

ALLOWED_TOP_LEVEL_FIELDS = {
    "master",
    "meterNo",
    "meterNoNormalized",
    "provider",
    "customerNo",
    "accountNo",
    "totalAmountC",
    "monthlyTotalsC",
    "lastPurchaseAtISO",
    "daysSinceLastPurchase",
}


@dataclass(frozen=True)
class UploadConfig:
    project_id: str
    service_account_path: Path
    input_path: Path
    mode: str
    report_dir: Path


@dataclass(frozen=True)
class PreflightResult:
    row_count: int
    unique_master_ids: int
    csv_sha256: str
    months: list[str]
    providers: list[str]
    visible_count: int
    invisible_count: int
    total_amount_c: int


@dataclass
class ResumePlan:
    missing_rows: list[dict[str, str]]
    matching_count: int
    conflicts: list[dict[str, Any]]
    extra_document_ids: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Upload an approved Sales All Meters CSV to an explicitly selected "
            "Firebase project."
        )
    )
    parser.add_argument("--project-id", required=True, help="Target Firebase project ID.")
    parser.add_argument(
        "--confirm-project",
        required=True,
        help="Must exactly repeat --project-id.",
    )
    parser.add_argument(
        "--service-account",
        required=True,
        type=Path,
        help="Service-account JSON belonging to the target project.",
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Approved Sales All Meters staging CSV.",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=["create-only", "resume"],
        help="create-only requires an empty collection; resume completes a verified partial upload.",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path("output/sales_all_meters/upload-reports"),
        help="Directory for the JSON upload report.",
    )
    return parser.parse_args()


def resolve_project_path(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path


def safe_str(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")


def read_service_account_project_id(path: Path) -> str:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    project_id = safe_str(payload.get("project_id"))
    if not project_id:
        raise ValueError(f"Service-account file has no project_id: {path}")
    return project_id


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_month_columns(columns: Sequence[str]) -> tuple[list[str], list[str]]:
    monthly_columns: list[str] = []
    months: list[str] = []
    for column in columns:
        match = MONTH_COLUMN_RE.fullmatch(column)
        if not match:
            continue
        year, month = int(match.group(1)), int(match.group(2))
        if not 1 <= month <= 12:
            raise ValueError(f"Invalid monthly column: {column!r}")
        monthly_columns.append(column)
        months.append(f"{year:04d}-{month:02d}")

    if not monthly_columns:
        raise ValueError("CSV has no dynamic amount_YYYY_MM_C monthly columns.")
    if months != sorted(months):
        raise ValueError(f"Monthly columns are not in chronological order: {months}")
    if len(set(months)) != len(months):
        raise ValueError(f"CSV contains duplicate month columns: {months}")

    expected: list[str] = []
    start_year, start_month = map(int, months[0].split("-"))
    end_year, end_month = map(int, months[-1].split("-"))
    year, month = start_year, start_month
    while (year, month) <= (end_year, end_month):
        expected.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            month = 1
            year += 1
    if months != expected:
        raise ValueError(
            f"Monthly columns must form one complete contiguous range. Found={months}; expected={expected}"
        )
    return monthly_columns, months


def parse_int_series(series: pd.Series, column: str, allow_blank: bool = False) -> pd.Series:
    text = series.map(safe_str)
    if not allow_blank and text.eq("").any():
        raise ValueError(f"CSV contains blank {column} values.")
    numeric = pd.to_numeric(text.replace("", pd.NA), errors="coerce")
    invalid = text.ne("") & numeric.isna()
    if invalid.any():
        examples = text.loc[invalid].head(10).tolist()
        raise ValueError(f"{column} contains non-numeric values. Examples: {examples}")
    non_integral = numeric.notna() & (numeric % 1 != 0)
    if non_integral.any():
        examples = text.loc[non_integral].head(10).tolist()
        raise ValueError(f"{column} contains non-integer values. Examples: {examples}")
    negative = numeric.notna() & (numeric < 0)
    if negative.any():
        examples = text.loc[negative].head(10).tolist()
        raise ValueError(f"{column} contains negative values. Examples: {examples}")
    return numeric.astype("Int64")


def validate_iso_columns(df: pd.DataFrame) -> None:
    purchase = df["lastPurchaseAtISO"].map(safe_str)
    days = df["daysSinceLastPurchase"].map(safe_str)

    for index, value in purchase[purchase.ne("")].items():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"lastPurchaseAtISO is invalid at CSV row {index + 2}: {value!r}"
            ) from exc
        if parsed.tzinfo is None:
            raise ValueError(
                f"lastPurchaseAtISO has no timezone at CSV row {index + 2}: {value!r}"
            )

    blank_purchase_with_days = purchase.eq("") & days.ne("")
    purchase_without_days = purchase.ne("") & days.eq("")
    if blank_purchase_with_days.any():
        raise ValueError("daysSinceLastPurchase must be blank when lastPurchaseAtISO is blank.")
    if purchase_without_days.any():
        raise ValueError("daysSinceLastPurchase is required when lastPurchaseAtISO is populated.")


def load_and_validate_csv(path: Path) -> tuple[pd.DataFrame, list[str], PreflightResult]:
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    actual_columns = list(df.columns)
    monthly_columns, months = parse_month_columns(actual_columns)
    expected_columns = BASE_COLUMNS + monthly_columns
    if actual_columns != expected_columns:
        missing = [column for column in expected_columns if column not in actual_columns]
        unexpected = [column for column in actual_columns if column not in expected_columns]
        raise ValueError(
            "Sales All Meters columns do not match the approved dynamic contract. "
            f"Expected={expected_columns}; actual={actual_columns}; "
            f"missing={missing}; unexpected={unexpected}"
        )
    if df.empty:
        raise ValueError("Sales All Meters CSV contains zero rows.")

    for column in actual_columns:
        df[column] = df[column].map(safe_str)

    blank_master = df["masterId"].eq("")
    if blank_master.any():
        raise ValueError(f"CSV contains {int(blank_master.sum())} blank masterId value(s).")
    duplicates = df["masterId"].duplicated(keep=False)
    if duplicates.any():
        examples = sorted(df.loc[duplicates, "masterId"].unique().tolist())[:10]
        raise ValueError(f"CSV contains duplicate masterId values. Examples: {examples}")
    invalid_ids = df["masterId"].str.contains("/", regex=False)
    if invalid_ids.any():
        examples = df.loc[invalid_ids, "masterId"].head(10).tolist()
        raise ValueError(f"masterId may not contain '/'. Examples: {examples}")
    id_mismatch = df["masterId"] != df["meterNoNormalized"]
    if id_mismatch.any():
        sample = df.loc[id_mismatch, ["masterId", "meterNoNormalized"]].head(10)
        raise ValueError(
            "masterId must equal meterNoNormalized. Examples:\n" + sample.to_string(index=False)
        )
    if df["meterNo"].eq("").any():
        raise ValueError("CSV contains blank meterNo values.")
    if df["provider"].eq("").any():
        raise ValueError("CSV contains blank provider values.")
    if not df["visibility"].isin(["VISIBLE", "INVISIBLE"]).all():
        examples = df.loc[~df["visibility"].isin(["VISIBLE", "INVISIBLE"]), "visibility"].head(10).tolist()
        raise ValueError(f"Invalid visibility values. Examples: {examples}")

    total_values = parse_int_series(df["totalAmountC"], "totalAmountC")
    monthly_values = {
        column: parse_int_series(df[column], column)
        for column in monthly_columns
    }
    calculated = pd.DataFrame(monthly_values).sum(axis=1).astype("Int64")
    mismatch = total_values != calculated
    if mismatch.any():
        sample = df.loc[mismatch, ["masterId", "totalAmountC"] + monthly_columns].head(10)
        raise ValueError(
            "totalAmountC does not equal the sum of dynamic monthly columns. Examples:\n"
            + sample.to_string(index=False)
        )

    days_values = parse_int_series(
        df["daysSinceLastPurchase"],
        "daysSinceLastPurchase",
        allow_blank=True,
    )
    validate_iso_columns(df)

    df["totalAmountC"] = total_values.map(lambda value: str(int(value)))
    for column in monthly_columns:
        df[column] = monthly_values[column].map(lambda value: str(int(value)))
    df["daysSinceLastPurchase"] = days_values.map(
        lambda value: "" if pd.isna(value) else str(int(value))
    )

    expected_range_text = f"{months[0]}_to_{months[-1]}"
    if expected_range_text not in path.name:
        raise ValueError(
            "Input filename does not accurately state its monthly range. "
            f"Expected {expected_range_text!r} in {path.name!r}."
        )

    result = PreflightResult(
        row_count=len(df),
        unique_master_ids=int(df["masterId"].nunique()),
        csv_sha256=sha256_file(path),
        months=months,
        providers=sorted(value for value in df["provider"].unique().tolist() if value),
        visible_count=int((df["visibility"] == "VISIBLE").sum()),
        invisible_count=int((df["visibility"] == "INVISIBLE").sum()),
        total_amount_c=int(total_values.sum()),
    )
    return df, monthly_columns, result


def dataframe_rows(df: pd.DataFrame) -> list[dict[str, str]]:
    columns = list(df.columns)
    return [
        {column: safe_str(row.get(column)) for column in columns}
        for _, row in df.iterrows()
    ]


def optional_int(value: Any) -> int | None:
    text = safe_str(value)
    return None if not text else int(text)


def build_document(row: Mapping[str, Any], monthly_columns: Sequence[str]) -> dict[str, Any]:
    monthly_totals: dict[str, int] = {}
    for column in monthly_columns:
        match = MONTH_COLUMN_RE.fullmatch(column)
        if match is None:
            raise ValueError(f"Invalid monthly column passed to build_document: {column!r}")
        ym = f"{match.group(1)}-{match.group(2)}"
        monthly_totals[ym] = int(safe_str(row.get(column)) or "0")

    return {
        "master": {
            "id": safe_str(row.get("masterId")),
            "visibility": safe_str(row.get("visibility")),
        },
        "meterNo": safe_str(row.get("meterNo")),
        "meterNoNormalized": safe_str(row.get("meterNoNormalized")),
        "provider": safe_str(row.get("provider")),
        "customerNo": safe_str(row.get("customerNo")),
        "accountNo": safe_str(row.get("accountNo")),
        "totalAmountC": int(safe_str(row.get("totalAmountC")) or "0"),
        "monthlyTotalsC": monthly_totals,
        "lastPurchaseAtISO": safe_str(row.get("lastPurchaseAtISO")) or None,
        "daysSinceLastPurchase": optional_int(row.get("daysSinceLastPurchase")),
    }


def compare_existing_document(existing: Mapping[str, Any], expected: Mapping[str, Any]) -> list[str]:
    differences: list[str] = []
    existing_keys = set(existing.keys())
    missing = sorted(ALLOWED_TOP_LEVEL_FIELDS - existing_keys)
    extra = sorted(existing_keys - ALLOWED_TOP_LEVEL_FIELDS)
    if missing:
        differences.append(f"missing top-level fields: {missing}")
    if extra:
        differences.append(f"unexpected top-level fields: {extra}")

    for field in sorted(ALLOWED_TOP_LEVEL_FIELDS):
        if existing.get(field) != expected.get(field):
            differences.append(
                f"{field} differs: Firestore={existing.get(field)!r}; CSV={expected.get(field)!r}"
            )
    return differences


def chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def collection_has_documents(db: firestore.Client) -> bool:
    return next(db.collection(COLLECTION_NAME).limit(1).stream(), None) is not None


def create_documents(
    db: firestore.Client,
    rows: Sequence[Mapping[str, Any]],
    monthly_columns: Sequence[str],
) -> int:
    written = 0
    row_batches = list(chunks(rows, BATCH_SIZE))
    for row_batch in tqdm(
        row_batches,
        desc="Writing Sales All Meters batches",
        unit="batch",
    ):
        batch = db.batch()
        for row in row_batch:
            doc_id = safe_str(row.get("masterId"))
            doc_ref = db.collection(COLLECTION_NAME).document(doc_id)
            batch.create(doc_ref, build_document(row, monthly_columns))
        batch.commit()
        written += len(row_batch)
    return written


def build_resume_plan(
    db: firestore.Client,
    rows: Sequence[dict[str, str]],
    monthly_columns: Sequence[str],
) -> ResumePlan:
    missing_rows: list[dict[str, str]] = []
    conflicts: list[dict[str, Any]] = []
    matching_count = 0
    row_by_id = {row["masterId"]: row for row in rows}
    ids = list(row_by_id.keys())

    for id_batch in tqdm(
        list(chunks(ids, BATCH_SIZE)),
        desc="Checking existing Sales All Meters documents",
        unit="batch",
    ):
        refs = [db.collection(COLLECTION_NAME).document(doc_id) for doc_id in id_batch]
        snapshots_by_id = {snapshot.id: snapshot for snapshot in db.get_all(refs)}
        for doc_id in id_batch:
            row = row_by_id[doc_id]
            snapshot = snapshots_by_id.get(doc_id)
            if snapshot is None or not snapshot.exists:
                missing_rows.append(row)
                continue
            expected = build_document(row, monthly_columns)
            differences = compare_existing_document(snapshot.to_dict() or {}, expected)
            if differences:
                conflicts.append({"masterId": doc_id, "differences": differences})
            else:
                matching_count += 1

    existing_ids = {snapshot.id for snapshot in db.collection(COLLECTION_NAME).stream()}
    extra_document_ids = sorted(existing_ids - set(ids))
    for doc_id in extra_document_ids[:100]:
        conflicts.append(
            {
                "masterId": doc_id,
                "differences": ["document exists in Firestore but is absent from the approved CSV"],
            }
        )

    return ResumePlan(
        missing_rows=missing_rows,
        matching_count=matching_count,
        conflicts=conflicts,
        extra_document_ids=extra_document_ids,
    )


def make_report_path(report_dir: Path, project_id: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return report_dir / f"sales_all_meters_upload__{project_id}__{timestamp}.json"


def write_report(report_path: Path, payload: Mapping[str, Any]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)
        handle.write("\n")


def build_config(args: argparse.Namespace) -> UploadConfig:
    project_id = safe_str(args.project_id)
    confirm_project = safe_str(args.confirm_project)
    if not project_id:
        raise ValueError("--project-id may not be blank.")
    if confirm_project != project_id:
        raise ValueError(
            f"Project confirmation failed: --project-id={project_id!r}, "
            f"--confirm-project={confirm_project!r}"
        )
    return UploadConfig(
        project_id=project_id,
        service_account_path=resolve_project_path(args.service_account).resolve(),
        input_path=resolve_project_path(args.input).resolve(),
        mode=args.mode,
        report_dir=resolve_project_path(args.report_dir).resolve(),
    )


def print_preflight(config: UploadConfig, result: PreflightResult) -> None:
    print("\n=== SALES ALL METERS UPLOAD PREFLIGHT ===")
    print(f"Target project:     {config.project_id}")
    print(f"Collection:         {COLLECTION_NAME}")
    print(f"Mode:               {config.mode}")
    print(f"Input CSV:          {config.input_path}")
    print(f"Rows:               {result.row_count:,}")
    print(f"Unique master IDs:  {result.unique_master_ids:,}")
    print(f"Months:             {len(result.months)} ({result.months[0]} to {result.months[-1]})")
    print(f"Providers:          {', '.join(result.providers)}")
    print(f"Visible meters:     {result.visible_count:,}")
    print(f"Invisible meters:   {result.invisible_count:,}")
    print(f"Total amount cents: {result.total_amount_c:,}")
    print(f"CSV SHA-256:        {result.csv_sha256}")
    print("===========================================\n")


def main() -> None:
    args = parse_args()
    config = build_config(args)
    started_at = datetime.now(UTC)
    report_path = make_report_path(config.report_dir, config.project_id)

    report: dict[str, Any] = {
        "operation": "sales_all_meters_once_off_upload",
        "projectId": config.project_id,
        "collection": COLLECTION_NAME,
        "mode": config.mode,
        "inputPath": str(config.input_path),
        "serviceAccountPath": str(config.service_account_path),
        "startedAt": started_at.isoformat(),
        "status": "STARTED",
    }

    try:
        require_file(config.input_path, "Input CSV")
        require_file(config.service_account_path, "Service-account file")

        credential_project = read_service_account_project_id(config.service_account_path)
        report["credentialProjectId"] = credential_project
        if credential_project != config.project_id:
            raise ValueError(
                "Service-account project mismatch. "
                f"Requested={config.project_id!r}; credential={credential_project!r}"
            )

        df, monthly_columns, preflight = load_and_validate_csv(config.input_path)
        rows = dataframe_rows(df)
        report.update(
            {
                "csvSha256": preflight.csv_sha256,
                "rowsRead": preflight.row_count,
                "uniqueMasterIds": preflight.unique_master_ids,
                "months": preflight.months,
                "providers": preflight.providers,
                "visibleMeters": preflight.visible_count,
                "invisibleMeters": preflight.invisible_count,
                "totalAmountC": preflight.total_amount_c,
            }
        )
        print_preflight(config, preflight)

        credentials = service_account.Credentials.from_service_account_file(
            str(config.service_account_path)
        )
        db = firestore.Client(project=config.project_id, credentials=credentials)

        if config.mode == "create-only":
            if collection_has_documents(db):
                raise RuntimeError(
                    f"Upload blocked: {COLLECTION_NAME} is not empty in {config.project_id}. "
                    "Use --mode resume only for recovery from a verified partial upload."
                )
            written = create_documents(db, rows, monthly_columns)
            matching = 0
            missing_before = len(rows)
        else:
            plan = build_resume_plan(db, rows, monthly_columns)
            report["matchingDocuments"] = plan.matching_count
            report["missingDocumentsBeforeWrite"] = len(plan.missing_rows)
            report["conflictCount"] = len(plan.conflicts)
            report["extraDocumentCount"] = len(plan.extra_document_ids)
            if plan.extra_document_ids:
                report["extraDocumentIds"] = plan.extra_document_ids[:100]
            if plan.conflicts:
                report["conflicts"] = plan.conflicts[:100]
                raise RuntimeError(
                    f"Resume blocked: {len(plan.conflicts)} conflicting document(s) found. "
                    f"See report: {report_path}"
                )
            written = create_documents(db, plan.missing_rows, monthly_columns)
            matching = plan.matching_count
            missing_before = len(plan.missing_rows)

        final_count = sum(1 for _ in db.collection(COLLECTION_NAME).stream())
        if final_count != preflight.unique_master_ids:
            raise RuntimeError(
                f"Post-upload verification failed: collection count={final_count}, "
                f"expected exactly {preflight.unique_master_ids}."
            )

        report.update(
            {
                "documentsWritten": written,
                "matchingDocuments": matching,
                "missingDocumentsBeforeWrite": missing_before,
                "finalCollectionCount": final_count,
                "status": "PASSED",
            }
        )
        print("=== SALES ALL METERS UPLOAD COMPLETE ===")
        print(f"Project:            {config.project_id}")
        print(f"Documents written:  {written:,}")
        print(f"Existing matching:  {matching:,}")
        print(f"Final collection:   {final_count:,}")
        print(f"Run report:         {report_path}")

    except Exception as exc:
        report["status"] = "FAILED"
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["finishedAt"] = datetime.now(UTC).isoformat()
        write_report(report_path, report)


if __name__ == "__main__":
    main()
