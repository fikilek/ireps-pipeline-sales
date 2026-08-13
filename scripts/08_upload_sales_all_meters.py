"""
08_upload_sales_all_meters.py

Controlled uploader for one approved, frozen Sales All Meters staging CSV.

Safety contract
---------------
- explicit Firebase project, confirmation, service account, CSV, Stage 06 manifest and mode;
- the successful Stage 06 manifest must fingerprint the exact visibility-free CSV;
- the visibility-free Stage 06 CSV shape, provider, totals, date pairs and canonical
  meter identities must pass preflight before Firebase is opened;
- current governed provider is exactly ``conlog``;
- normal mode is create-only against an empty collection;
- resume requires the failed report from the exact original Stage 08 upload contract;
- resume creates only missing documents and rejects changed sources, conflicting
  pipeline-owned fields and unexpected extra documents;
- Stage 08 initializes required ``master.visibility`` to ``INVISIBLE`` only when
  strictly creating a new document;
- resume requires existing ``master.visibility`` to be exactly ``VISIBLE`` or
  ``INVISIBLE`` and preserves the valid existing value without resetting it;
- the operational bridge owns all subsequent visibility lifecycle changes;
- Firestore create operations only: no merge, update, delete or silent overwrite;
- final collection count and deterministic document samples are verified;
- every run writes an atomic JSON report.

Create-only example
-------------------
python .\\scripts\\08_upload_sales_all_meters.py `
  --project-id ireps-test `
  --confirm-project ireps-test `
  --service-account "C:\\dev\\secrets\\ireps-test-firebase-adminsdk-fbsvc-d02929e1e3.json" `
  --input .\\output\\sales_all_meters\\sales_all_meters__ZA7423__FULL__2025-09_to_2026-06.csv `
  --manifest .\\output\\sales_all_meters\\sales_all_meters__ZA7423__FULL__2025-09_to_2026-06.manifest.json `
  --mode create-only

Resume example
--------------
python .\\scripts\\08_upload_sales_all_meters.py `
  --project-id ireps-test `
  --confirm-project ireps-test `
  --service-account "C:\\dev\\secrets\\ireps-test-firebase-adminsdk-fbsvc-d02929e1e3.json" `
  --input .\\output\\sales_all_meters\\sales_all_meters__ZA7423__FULL__2025-09_to_2026-06.csv `
  --manifest .\\output\\sales_all_meters\\sales_all_meters__ZA7423__FULL__2025-09_to_2026-06.manifest.json `
  --mode resume `
  --resume-report <previous-failed-stage08-report.json>
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from time import monotonic
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

import pandas as pd
from tqdm import tqdm

from sales_address_enrichment import (
    ADDRESS_MAP_FIELDS,
    ADDRESS_STAGING_COLUMNS,
    address_map_from_row,
    validate_address_values,
)

try:
    from google.cloud import firestore
    from google.oauth2 import service_account
except ImportError:  # Allows offline preflight/help tests without Firebase dependencies.
    firestore = None  # type: ignore[assignment]
    service_account = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTION_NAME = "sales-all-meters"
BATCH_SIZE = 400
INITIAL_LOAD_PROJECTS = {"ireps-test", "ireps-5c3e9"}
GOVERNED_PROVIDER = "conlog"
MONTH_COLUMN_RE = re.compile(r"^amount_(\d{4})_(\d{2})_C$")
METER_ID_RE = re.compile(r"^[A-Z0-9]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

BASE_COLUMNS = [
    "masterId",
    "meterNo",
    "meterNoNormalized",
    "provider",
    "customerNo",
    "accountNo",
    "totalAmountC",
    "lastPurchaseAtISO",
    "daysSinceLastPurchase",
]

PIPELINE_TOP_LEVEL_FIELDS = {
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
ALLOWED_MASTER_FIELDS = {"id", "visibility"}
ALLOWED_MASTER_VISIBILITIES = {"VISIBLE", "INVISIBLE"}
DEFAULT_MASTER_VISIBILITY = "INVISIBLE"


@dataclass(frozen=True)
class UploadConfig:
    project_id: str
    service_account_path: Path
    input_path: Path
    manifest_path: Path
    mode: str
    resume_report_path: Optional[Path]
    report_dir: Path
    preflight_only: bool


@dataclass(frozen=True)
class PreflightResult:
    row_count: int
    unique_master_ids: int
    csv_sha256: str
    document_ids_sha256: str
    months: list[str]
    providers: list[str]
    total_amount_c: int
    address_enrichment_enabled: bool
    address_enriched_rows: int
    address_unresolved_rows: int


@dataclass(frozen=True)
class JsonSnapshot:
    """A parsed JSON object and SHA produced from one immutable byte read."""

    path: Path
    payload: dict[str, Any]
    sha256: str


@dataclass
class ResumePlan:
    missing_rows: list[dict[str, str]]
    matching_count: int
    conflicts: list[dict[str, Any]]
    extra_document_ids: list[str]


@dataclass
class UploadProgress:
    documents_created: int = 0
    committed_batches: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload one frozen Sales All Meters CSV to Firestore."
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
        help="Frozen visibility-free Sales All Meters CSV generated by Stage 06.",
    )
    parser.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="Matching successful Stage 06 frozen-build manifest.",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("create-only", "initial-load", "refresh", "resume"),
        help=(
            "create-only requires an empty whole collection; initial-load requires only the "
            "input document IDs to be absent; refresh is recurring; resume is restricted recovery."
        ),
    )
    parser.add_argument(
        "--resume-report",
        type=Path,
        help="Previous failed Stage 08 JSON report. Required only for resume.",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Inspect the selected initial-load or refresh mode without writing Firestore.",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path("output/logs/sales_all_meters"),
        help="Directory for Stage 08 JSON reports.",
    )
    return parser.parse_args()


def resolve_project_path(path: Path) -> Path:
    return (path if path.is_absolute() else PROJECT_ROOT / path).expanduser().resolve()


def safe_str(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def normalize_meter_id(value: Any) -> str:
    return "".join(safe_str(value).upper().split())


def require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def read_json(path: Path, label: str) -> dict[str, Any]:
    require_file(path, label)
    try:
        with path.open("r", encoding="utf-8") as source:
            payload = json.load(source)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain one JSON object: {path}")
    return payload


def read_json_snapshot(path: Path, label: str) -> JsonSnapshot:
    require_file(path, label)
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain one JSON object: {path}")
    return JsonSnapshot(path=path, payload=payload, sha256=sha256_bytes(raw))


def read_service_account_project_id(path: Path) -> str:
    payload = read_json(path, "Service-account file")
    project_id = safe_str(payload.get("project_id"))
    if not project_id:
        raise ValueError(f"Service-account file has no project_id: {path}")
    return project_id


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def document_ids_sha256(values: Sequence[str]) -> str:
    return canonical_json_sha256(sorted(values))


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
        raise ValueError("CSV has no dynamic amount_YYYY_MM_C monthly columns")
    if months != sorted(months):
        raise ValueError(f"Monthly columns are not in chronological order: {months}")
    if len(set(months)) != len(months):
        raise ValueError(f"CSV contains duplicate month columns: {months}")

    start = pd.Period(months[0], freq="M")
    end = pd.Period(months[-1], freq="M")
    expected = [str(period) for period in pd.period_range(start, end, freq="M")]
    if months != expected:
        raise ValueError(
            "Monthly columns must form one complete contiguous range. "
            f"Found={months}; expected={expected}"
        )
    return monthly_columns, months


def parse_integer_series(
    series: pd.Series,
    column: str,
    *,
    allow_blank: bool = False,
) -> pd.Series:
    text = series.map(safe_str)
    if not allow_blank and text.eq("").any():
        rows = [int(index) + 2 for index in text[text.eq("")].index[:10]]
        raise ValueError(f"CSV contains blank {column} values at rows {rows}")

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


def validate_identity_column(df: pd.DataFrame, column: str) -> None:
    values = df[column]
    normalized = values.map(normalize_meter_id)
    invalid_shape = ~normalized.str.fullmatch(METER_ID_RE)
    noncanonical = values.ne(normalized)
    invalid = invalid_shape | noncanonical
    if invalid.any():
        examples = [
            {
                "row": int(index) + 2,
                "value": values.loc[index],
                "canonical": normalized.loc[index],
            }
            for index in values[invalid].index[:10]
        ]
        raise ValueError(
            f"{column} must already be canonical uppercase alphanumeric text with all "
            f"whitespace removed. Examples: {examples}"
        )


def validate_iso_columns(df: pd.DataFrame) -> None:
    purchase = df["lastPurchaseAtISO"]
    days = df["daysSinceLastPurchase"]

    for index, value in purchase[purchase.ne("")].items():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"lastPurchaseAtISO is invalid at CSV row {int(index) + 2}: {value!r}"
            ) from exc
        if parsed.tzinfo is None:
            raise ValueError(
                f"lastPurchaseAtISO has no timezone at CSV row {int(index) + 2}: {value!r}"
            )

    if (purchase.eq("") & days.ne("")).any():
        raise ValueError("daysSinceLastPurchase must be blank when lastPurchaseAtISO is blank")
    if (purchase.ne("") & days.eq("")).any():
        raise ValueError("daysSinceLastPurchase is required when lastPurchaseAtISO is populated")


def parse_manifest_as_of_date(value: Any) -> date:
    text = safe_str(value)
    if not DATE_RE.fullmatch(text):
        raise ValueError("Stage 06 manifest sourceContract.asOfDate must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("Stage 06 manifest sourceContract.asOfDate is invalid") from exc
    if parsed.isoformat() != text:
        raise ValueError("Stage 06 manifest sourceContract.asOfDate is not canonical")
    return parsed


def validate_recency_contract(
    df: pd.DataFrame,
    months: Sequence[str],
    as_of_date: date,
) -> None:
    approved_months = set(months)
    for index, row in df.iterrows():
        csv_row = int(index) + 2
        total = int(safe_str(row["totalAmountC"]))
        purchase_text = safe_str(row["lastPurchaseAtISO"])
        days_text = safe_str(row["daysSinceLastPurchase"])
        positive_months = [
            month
            for month in months
            if int(safe_str(row[f"amount_{month.replace('-', '_')}_C"])) > 0
        ]

        if total > 0 and (not purchase_text or not days_text):
            raise ValueError(
                "Positive totalAmountC requires populated lastPurchaseAtISO and "
                f"daysSinceLastPurchase at CSV row {csv_row}"
            )
        if total == 0 and (purchase_text or days_text):
            raise ValueError(
                "A no-sales row must have blank lastPurchaseAtISO and "
                f"daysSinceLastPurchase at CSV row {csv_row}"
            )
        if not purchase_text:
            continue

        try:
            purchase = datetime.fromisoformat(purchase_text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"lastPurchaseAtISO is invalid at CSV row {csv_row}: {purchase_text!r}"
            ) from exc
        if purchase.tzinfo is None:
            raise ValueError(
                f"lastPurchaseAtISO has no timezone at CSV row {csv_row}: {purchase_text!r}"
            )
        purchase_utc = purchase.astimezone(UTC)
        purchase_month = purchase_utc.strftime("%Y-%m")
        if purchase_month not in approved_months:
            raise ValueError(
                f"lastPurchaseAtISO month {purchase_month!r} is outside the approved "
                f"Stage 06 range at CSV row {csv_row}"
            )
        if positive_months and purchase_month != positive_months[-1]:
            raise ValueError(
                f"lastPurchaseAtISO belongs to {purchase_month!r}, but the latest "
                f"positive sales month is {positive_months[-1]!r} at CSV row {csv_row}"
            )

        expected_days = (as_of_date - purchase_utc.date()).days
        if expected_days < 0:
            raise ValueError(
                f"lastPurchaseAtISO occurs after Stage 06 asOfDate at CSV row {csv_row}"
            )
        if int(days_text) != expected_days:
            raise ValueError(
                f"daysSinceLastPurchase is {days_text!r} at CSV row {csv_row}; "
                f"expected {expected_days} from Stage 06 asOfDate {as_of_date.isoformat()}"
            )


def load_and_validate_csv(
    path: Path,
) -> tuple[pd.DataFrame, list[str], PreflightResult]:
    require_file(path, "Input CSV")
    raw_bytes = path.read_bytes()
    try:
        df = pd.read_csv(io.BytesIO(raw_bytes), dtype=str, encoding="utf-8-sig").fillna("")
    except Exception as exc:
        raise ValueError(f"Input CSV is not valid CSV data: {path}") from exc
    actual_columns = list(df.columns)
    monthly_columns, months = parse_month_columns(actual_columns)
    present_address = [column for column in ADDRESS_STAGING_COLUMNS if column in actual_columns]
    if present_address and present_address != ADDRESS_STAGING_COLUMNS:
        raise ValueError(
            "Sales Enrich staging columns must be present together in canonical order"
        )
    address_enrichment_enabled = present_address == ADDRESS_STAGING_COLUMNS
    expected_columns = (
        BASE_COLUMNS
        + (ADDRESS_STAGING_COLUMNS if address_enrichment_enabled else [])
        + monthly_columns
    )
    if actual_columns != expected_columns:
        missing = [column for column in expected_columns if column not in actual_columns]
        unexpected = [column for column in actual_columns if column not in expected_columns]
        raise ValueError(
            "Sales All Meters columns do not match the visibility-free governed contract. "
            f"Expected={expected_columns}; actual={actual_columns}; "
            f"missing={missing}; unexpected={unexpected}"
        )
    if "visibility" in actual_columns:
        raise ValueError(
            "Stage 06 must not provide a visibility column; Stage 08 initializes "
            "master.visibility to INVISIBLE only during strict document creation"
        )
    if df.empty:
        raise ValueError("Sales All Meters CSV contains zero rows")

    for column in actual_columns:
        raw_series = df[column].astype(str)
        cleaned = raw_series.map(safe_str)
        drift = raw_series.ne(cleaned)
        if drift.any():
            rows = [int(index) + 2 for index in drift[drift].index[:10]]
            raise ValueError(f"CSV contains whitespace or text drift in {column} at rows {rows}")
        df[column] = cleaned

    for column in ("masterId", "meterNoNormalized"):
        validate_identity_column(df, column)

    if df["masterId"].eq("").any():
        raise ValueError("CSV contains blank masterId values")
    duplicate_ids = df["masterId"].duplicated(keep=False)
    if duplicate_ids.any():
        examples = sorted(df.loc[duplicate_ids, "masterId"].unique().tolist())[:10]
        raise ValueError(f"CSV contains duplicate masterId values. Examples: {examples}")
    mismatch = df["masterId"].ne(df["meterNoNormalized"])
    if mismatch.any():
        sample = df.loc[mismatch, ["masterId", "meterNoNormalized"]].head(10)
        raise ValueError(
            "masterId must equal meterNoNormalized. Examples:\n" + sample.to_string(index=False)
        )
    if df["meterNo"].eq("").any():
        raise ValueError("CSV contains blank meterNo values")

    address_enriched_rows = 0
    address_unresolved_rows = 0
    if address_enrichment_enabled:
        for index, row in df.iterrows():
            try:
                validate_address_values(row["strNo"], row["strName"], row["strType"])
            except ValueError as exc:
                raise ValueError(
                    f"Invalid Sales Enrich address at CSV row {int(index) + 2}: {exc}"
                ) from exc
            if safe_str(row["strNo"]) and safe_str(row["strName"]):
                address_enriched_rows += 1
            else:
                address_unresolved_rows += 1

    invalid_provider = df["provider"].ne(GOVERNED_PROVIDER)
    if invalid_provider.any():
        examples = [
            {"row": int(index) + 2, "provider": df.at[index, "provider"]}
            for index in df.index[invalid_provider][:10]
        ]
        raise ValueError(
            f"Every Stage 08 row must have provider={GOVERNED_PROVIDER!r}; "
            f"blank and alternate values are prohibited. Examples: {examples}"
        )
    providers = [GOVERNED_PROVIDER]

    total_values = parse_integer_series(df["totalAmountC"], "totalAmountC")
    monthly_values = {
        column: parse_integer_series(df[column], column)
        for column in monthly_columns
    }
    calculated = pd.DataFrame(monthly_values).sum(axis=1).astype("Int64")
    total_mismatch = total_values.ne(calculated)
    if total_mismatch.any():
        sample = df.loc[
            total_mismatch,
            ["masterId", "totalAmountC"] + monthly_columns,
        ].head(10)
        raise ValueError(
            "totalAmountC does not equal the sum of dynamic monthly columns. Examples:\n"
            + sample.to_string(index=False)
        )

    days_values = parse_integer_series(
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
            f"Expected {expected_range_text!r} in {path.name!r}"
        )

    ids = df["masterId"].tolist()
    result = PreflightResult(
        row_count=len(df),
        unique_master_ids=int(df["masterId"].nunique()),
        csv_sha256=sha256_bytes(raw_bytes),
        document_ids_sha256=document_ids_sha256(ids),
        months=months,
        providers=providers,
        total_amount_c=int(total_values.sum()),
        address_enrichment_enabled=address_enrichment_enabled,
        address_enriched_rows=address_enriched_rows,
        address_unresolved_rows=address_unresolved_rows,
    )
    return df, monthly_columns, result


def dataframe_rows(df: pd.DataFrame) -> list[dict[str, str]]:
    columns = list(df.columns)
    return [
        {column: str(row.get(column, "")) for column in columns}
        for row in df.to_dict("records")
    ]


def optional_int(value: Any) -> int | None:
    text = safe_str(value)
    return None if not text else int(text)


def build_document(
    row: Mapping[str, Any],
    monthly_columns: Sequence[str],
) -> dict[str, Any]:
    monthly_totals: dict[str, int] = {}
    for column in monthly_columns:
        match = MONTH_COLUMN_RE.fullmatch(column)
        if match is None:
            raise ValueError(f"Invalid monthly column passed to build_document: {column!r}")
        ym = f"{match.group(1)}-{match.group(2)}"
        monthly_totals[ym] = int(str(row[column]))

    document = {
        "master": {
            "id": str(row["masterId"]),
            "visibility": DEFAULT_MASTER_VISIBILITY,
        },
        "meterNo": str(row["meterNo"]),
        "meterNoNormalized": str(row["meterNoNormalized"]),
        "provider": str(row["provider"]),
        "customerNo": str(row["customerNo"]),
        "accountNo": str(row["accountNo"]),
        "totalAmountC": int(str(row["totalAmountC"])),
        "monthlyTotalsC": monthly_totals,
        "lastPurchaseAtISO": str(row["lastPurchaseAtISO"]) or None,
        "daysSinceLastPurchase": optional_int(row["daysSinceLastPurchase"]),
    }
    present_address = [column for column in ADDRESS_STAGING_COLUMNS if column in row]
    if present_address:
        if present_address != ADDRESS_STAGING_COLUMNS:
            raise ValueError("Sales Enrich staging fields must be present together")
        document["adr"] = address_map_from_row(row)
    return document


def is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def compare_existing_document(
    existing: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> list[str]:
    """Validate canonical existing data while preserving valid operational visibility."""
    differences: list[str] = []

    expected_top_level_fields = set(PIPELINE_TOP_LEVEL_FIELDS)
    if "adr" in expected:
        expected_top_level_fields.add("adr")
    missing_top = sorted(expected_top_level_fields - set(existing.keys()))
    extra_top = sorted(set(existing.keys()) - expected_top_level_fields)
    if missing_top:
        differences.append(f"missing top-level fields: {missing_top}")
    if extra_top:
        differences.append(f"unexpected top-level fields: {extra_top}")

    master = existing.get("master")
    expected_master = expected["master"]
    if not isinstance(master, Mapping):
        differences.append("master is missing or is not an object")
    else:
        extra_master = sorted(set(master.keys()) - ALLOWED_MASTER_FIELDS)
        if extra_master:
            differences.append(f"master has unexpected fields: {extra_master}")
        master_id = master.get("id")
        if not isinstance(master_id, str):
            differences.append(f"master.id must be a string, found {type(master_id).__name__}")
        elif master_id != expected_master["id"]:
            differences.append(
                f"master.id differs: Firestore={master_id!r}; CSV={expected_master['id']!r}"
            )
        if "visibility" not in master:
            differences.append("master.visibility is required")
        else:
            visibility = master.get("visibility")
            if not isinstance(visibility, str):
                differences.append(
                    "master.visibility must be a string, "
                    f"found {type(visibility).__name__}"
                )
            elif visibility not in ALLOWED_MASTER_VISIBILITIES:
                differences.append(
                    "master.visibility must be exactly VISIBLE or INVISIBLE, "
                    f"found {visibility!r}"
                )
        # A valid existing visibility is preserved. It is not compared with the
        # Stage 08 creation default because the operational bridge owns later changes.

    for field in ("meterNo", "meterNoNormalized", "provider", "customerNo", "accountNo"):
        actual = existing.get(field)
        wanted = expected[field]
        if not isinstance(actual, str):
            differences.append(f"{field} must be a string, found {type(actual).__name__}")
        elif actual != wanted:
            differences.append(f"{field} differs: Firestore={actual!r}; CSV={wanted!r}")

    total = existing.get("totalAmountC")
    if not is_strict_int(total):
        differences.append(f"totalAmountC must be an integer, found {type(total).__name__}")
    elif total != expected["totalAmountC"]:
        differences.append(
            f"totalAmountC differs: Firestore={total!r}; CSV={expected['totalAmountC']!r}"
        )

    monthly = existing.get("monthlyTotalsC")
    expected_monthly = expected["monthlyTotalsC"]
    if not isinstance(monthly, Mapping):
        differences.append("monthlyTotalsC is missing or is not an object")
    else:
        if set(monthly.keys()) != set(expected_monthly.keys()):
            differences.append(
                "monthlyTotalsC keys differ: "
                f"Firestore={sorted(monthly.keys())}; CSV={sorted(expected_monthly.keys())}"
            )
        for ym, wanted in expected_monthly.items():
            actual = monthly.get(ym)
            if not is_strict_int(actual):
                differences.append(
                    f"monthlyTotalsC.{ym} must be an integer, found {type(actual).__name__}"
                )
            elif actual != wanted:
                differences.append(
                    f"monthlyTotalsC.{ym} differs: Firestore={actual!r}; CSV={wanted!r}"
                )

    last_purchase = existing.get("lastPurchaseAtISO")
    expected_last = expected["lastPurchaseAtISO"]
    if last_purchase is not None and not isinstance(last_purchase, str):
        differences.append(
            "lastPurchaseAtISO must be a string or null, "
            f"found {type(last_purchase).__name__}"
        )
    elif last_purchase != expected_last:
        differences.append(
            f"lastPurchaseAtISO differs: Firestore={last_purchase!r}; CSV={expected_last!r}"
        )

    days = existing.get("daysSinceLastPurchase")
    expected_days = expected["daysSinceLastPurchase"]
    if days is not None and not is_strict_int(days):
        differences.append(
            "daysSinceLastPurchase must be an integer or null, "
            f"found {type(days).__name__}"
        )
    elif days != expected_days:
        differences.append(
            f"daysSinceLastPurchase differs: Firestore={days!r}; CSV={expected_days!r}"
        )

    if "adr" in expected:
        actual_adr = existing.get("adr")
        expected_adr = expected["adr"]
        if not isinstance(actual_adr, Mapping):
            differences.append("adr is missing or is not an object")
        else:
            actual_keys = set(actual_adr.keys())
            if actual_keys != ADDRESS_MAP_FIELDS:
                differences.append(
                    f"adr keys differ: Firestore={sorted(actual_keys)}; "
                    f"expected={sorted(ADDRESS_MAP_FIELDS)}"
                )
            for field in ADDRESS_STAGING_COLUMNS:
                actual_value = actual_adr.get(field)
                wanted = expected_adr[field]
                if not isinstance(actual_value, str):
                    differences.append(
                        f"adr.{field} must be a string, found {type(actual_value).__name__}"
                    )
                elif actual_value != wanted:
                    differences.append(
                        f"adr.{field} differs: Firestore={actual_value!r}; CSV={wanted!r}"
                    )

    return differences


def chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def collection_has_documents(db: firestore.Client) -> bool:
    return next(db.collection(COLLECTION_NAME).limit(1).stream(), None) is not None


def wall_ts() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def print_batch_progress(label: str, completed: int, total: int, started: float) -> None:
    elapsed = max(monotonic() - started, 0.001)
    rate = completed / elapsed
    remaining = max(total - completed, 0)
    eta_seconds = remaining / rate if rate > 0 else 0.0
    print(
        f"[{wall_ts()}] {label}: {completed:,}/{total:,}; "
        f"elapsed={elapsed:.1f}s; rate={rate:.1f} docs/s; eta={eta_seconds:.1f}s"
    )


def find_existing_input_ids(
    db: firestore.Client,
    document_ids: Sequence[str],
    *,
    label: str,
) -> list[str]:
    ids = list(document_ids)
    existing: list[str] = []
    total = len(ids)
    started = monotonic()
    inspected = 0
    for id_batch in chunks(ids, BATCH_SIZE):
        refs = [db.collection(COLLECTION_NAME).document(doc_id) for doc_id in id_batch]
        for snapshot in db.get_all(refs):
            if snapshot.exists:
                existing.append(snapshot.id)
        inspected += len(id_batch)
        print_batch_progress(label, inspected, total, started)
    return sorted(existing)


def create_documents(
    db: firestore.Client,
    rows: Sequence[Mapping[str, Any]],
    monthly_columns: Sequence[str],
    progress: UploadProgress,
) -> None:
    total = len(rows)
    started = monotonic()
    completed = 0
    for row_batch in chunks(rows, BATCH_SIZE):
        batch = db.batch()
        for row in row_batch:
            doc_id = str(row["masterId"])
            doc_ref = db.collection(COLLECTION_NAME).document(doc_id)
            batch.create(doc_ref, build_document(row, monthly_columns))
        batch.commit()
        progress.committed_batches += 1
        progress.documents_created += len(row_batch)
        completed += len(row_batch)
        print_batch_progress("Sales All create", completed, total, started)


def create_prebuilt_documents(
    db: firestore.Client,
    rows: Sequence[Mapping[str, Any]],
    progress: UploadProgress,
) -> None:
    """Create governed rich Stage 06 documents in the existing 400-document waves."""
    for row_batch in tqdm(
        list(chunks(rows, BATCH_SIZE)),
        desc="Creating Sales All Meters documents",
        unit="batch",
    ):
        batch = db.batch()
        for item in row_batch:
            doc_id = str(item["masterId"])
            batch.create(
                db.collection(COLLECTION_NAME).document(doc_id),
                dict(item["expected"]),
            )
        batch.commit()
        progress.documents_created += len(row_batch)
        progress.committed_batches += 1


def verify_prebuilt_input_scope(
    db: firestore.Client,
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Verify every rich initial-load document with batched get_all reads."""
    expected_by_id = {str(item["masterId"]): item["expected"] for item in rows}
    failures: list[dict[str, Any]] = []
    verified = 0
    for id_batch in tqdm(
        list(chunks(list(expected_by_id), BATCH_SIZE)),
        desc="Verifying Sales All Meters input scope",
        unit="batch",
    ):
        refs = [db.collection(COLLECTION_NAME).document(doc_id) for doc_id in id_batch]
        snapshots = {snapshot.id: snapshot for snapshot in db.get_all(refs)}
        for doc_id in id_batch:
            snapshot = snapshots.get(doc_id)
            if snapshot is None or not snapshot.exists:
                failures.append({"masterId": doc_id, "differences": ["document missing"]})
                continue
            if (snapshot.to_dict() or {}) != expected_by_id[doc_id]:
                failures.append(
                    {"masterId": doc_id, "differences": ["document does not match governed Stage 06 document"]}
                )
                continue
            verified += 1
    if failures:
        raise RuntimeError(
            f"Input-scope Sales All verification failed for {len(failures)} document(s); "
            f"first={failures[:10]}"
        )
    return {"verifiedInputScopeCount": verified, "batchReadSize": BATCH_SIZE}


def verify_input_scope_post_upload(
    db: firestore.Client,
    rows: Sequence[dict[str, str]],
    monthly_columns: Sequence[str],
) -> dict[str, Any]:
    row_by_id = {row["masterId"]: row for row in rows}
    ids = sorted(row_by_id)
    total = len(ids)
    verified = 0
    started = monotonic()
    failures: list[dict[str, Any]] = []

    for id_batch in chunks(ids, BATCH_SIZE):
        refs = [db.collection(COLLECTION_NAME).document(doc_id) for doc_id in id_batch]
        snapshots_by_id = {snapshot.id: snapshot for snapshot in db.get_all(refs)}
        for doc_id in id_batch:
            snapshot = snapshots_by_id.get(doc_id)
            if snapshot is None or not snapshot.exists:
                failures.append({"masterId": doc_id, "differences": ["document missing"]})
                continue
            expected = build_document(row_by_id[doc_id], monthly_columns)
            actual = snapshot.to_dict() or {}
            differences = compare_existing_document(actual, expected)
            if differences:
                failures.append({"masterId": doc_id, "differences": differences})
        verified += len(id_batch)
        print_batch_progress("Sales All verify", verified, total, started)

    if failures:
        raise RuntimeError(
            f"Input-scope Sales All verification failed for {len(failures)} document(s); "
            f"first={failures[0]}"
        )

    return {
        "expectedInputScopeCount": total,
        "verifiedInputScopeCount": total,
        "inputScopeVerification": "PASS",
        "verificationMode": "BATCHED_GET_ALL_FULL_INPUT_SCOPE",
    }


def build_resume_plan(
    db: firestore.Client,
    rows: Sequence[dict[str, str]],
    monthly_columns: Sequence[str],
) -> ResumePlan:
    missing_rows: list[dict[str, str]] = []
    conflicts: list[dict[str, Any]] = []
    matching_count = 0
    row_by_id = {row["masterId"]: row for row in rows}
    ids = sorted(row_by_id.keys())

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
                "differences": [
                    "document exists in Firestore but is absent from the frozen Stage 06 CSV"
                ],
            }
        )

    return ResumePlan(
        missing_rows=missing_rows,
        matching_count=matching_count,
        conflicts=conflicts,
        extra_document_ids=extra_document_ids,
    )


def deterministic_sample_ids(ids: Sequence[str], sample_count: int = 7) -> list[str]:
    ordered = sorted(ids)
    if not ordered:
        return []
    count = min(sample_count, len(ordered))
    if count == 1:
        return [ordered[0]]
    positions = sorted(
        {round(index * (len(ordered) - 1) / (count - 1)) for index in range(count)}
    )
    return [ordered[position] for position in positions]


def verify_post_upload(
    db: firestore.Client,
    rows: Sequence[dict[str, str]],
    monthly_columns: Sequence[str],
) -> dict[str, Any]:
    expected_count = len(rows)
    final_count = sum(1 for _ in db.collection(COLLECTION_NAME).stream())
    if final_count != expected_count:
        raise RuntimeError(
            f"Post-upload count verification failed: expected={expected_count}, found={final_count}"
        )

    row_by_id = {row["masterId"]: row for row in rows}
    samples: list[dict[str, Any]] = []
    for doc_id in deterministic_sample_ids(list(row_by_id.keys())):
        snapshot = db.collection(COLLECTION_NAME).document(doc_id).get()
        if not snapshot.exists:
            raise RuntimeError(f"Post-upload sample is missing: {COLLECTION_NAME}/{doc_id}")
        expected = build_document(row_by_id[doc_id], monthly_columns)
        actual = snapshot.to_dict() or {}
        differences = compare_existing_document(actual, expected)
        operational_visibility_present = (
            isinstance(actual.get("master"), Mapping)
            and "visibility" in actual["master"]
        )
        samples.append(
            {
                "masterId": doc_id,
                "matchesPipelineFields": not differences,
                "operationalVisibilityPresent": operational_visibility_present,
            }
        )
        if differences:
            raise RuntimeError(
                f"Post-upload sample verification failed for {COLLECTION_NAME}/{doc_id}: "
                f"{differences}"
            )

    return {
        "expectedCount": expected_count,
        "finalCount": final_count,
        "countVerification": "PASS",
        "sampleVerification": "PASS",
        "samples": samples,
    }


def stage06_fingerprint_contract(manifest: Mapping[str, Any]) -> dict[str, Any]:
    source = manifest.get("sourceContract")
    output = manifest.get("outputContract")
    stats = manifest.get("stats")
    if not isinstance(source, Mapping) or not isinstance(output, Mapping) or not isinstance(stats, Mapping):
        raise ValueError("Stage 06 manifest is missing sourceContract, outputContract, or stats")
    stage05 = source.get("stage05Manifest")
    master = source.get("meterMaster")
    monthly = source.get("monthlyInputs")
    if not isinstance(stage05, Mapping) or not isinstance(master, Mapping) or not isinstance(monthly, list):
        raise ValueError("Stage 06 source contract is incomplete")
    contract: dict[str, Any] = {
        "lmPcode": source.get("lmPcode"),
        "fromMonth": source.get("fromMonth"),
        "toMonth": source.get("toMonth"),
        "includedMonths": source.get("includedMonths"),
        "provider": source.get("provider"),
        "asOfDate": source.get("asOfDate"),
        "visibilityOwnership": source.get("visibilityOwnership"),
        "stage05Manifest": {
            "filename": stage05.get("filename"),
            "sha256": stage05.get("sha256"),
            "buildFingerprint": stage05.get("buildFingerprint"),
        },
        "meterMaster": {
            "filename": master.get("filename"),
            "rows": master.get("rows"),
            "columns": master.get("columns"),
            "sha256": master.get("sha256"),
            "documentIdsSha256": master.get("documentIdsSha256"),
        },
        "monthlyInputs": [
            {
                "month": item.get("month"),
                "filename": item.get("filename"),
                "rows": item.get("rows"),
                "columns": item.get("columns"),
                "sha256": item.get("sha256"),
            }
            for item in monthly if isinstance(item, Mapping)
        ],
        "output": {
            "filename": output.get("filename"),
            "rows": output.get("rows"),
            "columns": output.get("columns"),
            "sha256": output.get("sha256"),
            "documentIdsSha256": output.get("documentIdsSha256"),
            "months": output.get("months"),
            "provider": output.get("provider"),
            "totalAmountC": output.get("totalAmountC"),
            "visibilityColumn": output.get("visibilityColumn"),
            **(
                {"addressEnrichment": dict(output.get("addressEnrichment") or {})}
                if "addressEnrichment" in output
                else {}
            ),
        },
        "stats": dict(stats),
    }
    if "commercialAddressSource" in source:
        commercial = source.get("commercialAddressSource")
        if not isinstance(commercial, Mapping):
            raise ValueError("Stage 06 commercialAddressSource must be an object")
        contract["commercialAddressSource"] = dict(commercial)
    return contract


def validate_stage06_manifest(
    config: UploadConfig,
    preflight: PreflightResult,
    actual_columns: Sequence[str],
    manifest_snapshot: JsonSnapshot,
) -> dict[str, Any]:
    manifest = manifest_snapshot.payload
    if manifest.get("schemaVersion") != 1:
        raise ValueError("Unsupported Stage 06 manifest schemaVersion")
    if manifest.get("stage") != "06" or manifest.get("script") != "06_build_sales_all_meters.py":
        raise ValueError("Manifest is not from Stage 06")
    if manifest.get("status") != "PASS" or manifest.get("result") != "BUILD_WRITTEN":
        raise ValueError("Stage 06 manifest is not a successful frozen build")

    recorded = safe_str(manifest.get("buildFingerprint"))
    calculated = canonical_json_sha256(stage06_fingerprint_contract(manifest))
    if recorded != calculated:
        raise ValueError("Stage 06 buildFingerprint is invalid; manifest may be edited or corrupt")

    source = manifest.get("sourceContract")
    output = manifest.get("outputContract")
    if not isinstance(source, Mapping) or not isinstance(output, Mapping):
        raise ValueError("Stage 06 manifest contracts are missing")
    if source.get("includedMonths") != preflight.months:
        raise ValueError("Stage 06 manifest includedMonths do not match the CSV")
    if source.get("fromMonth") != preflight.months[0] or source.get("toMonth") != preflight.months[-1]:
        raise ValueError("Stage 06 manifest range does not match the CSV")
    if source.get("provider") != GOVERNED_PROVIDER:
        raise ValueError("Stage 06 manifest provider is outside the governed contract")
    if source.get("visibilityOwnership") != "OPERATIONAL_WRITERS_ONLY":
        raise ValueError("Stage 06 manifest visibility ownership is invalid")
    as_of_date = parse_manifest_as_of_date(source.get("asOfDate"))

    address_contract = output.get("addressEnrichment")
    if preflight.address_enrichment_enabled:
        if not isinstance(address_contract, Mapping) or address_contract.get("enabled") is not True:
            raise ValueError("Stage 06 manifest is missing enabled addressEnrichment contract")
        if address_contract.get("stagingColumns") != ADDRESS_STAGING_COLUMNS:
            raise ValueError("Stage 06 addressEnrichment stagingColumns mismatch")
        if address_contract.get("firestoreProjection") != "adr":
            raise ValueError("Stage 06 addressEnrichment Firestore projection must be adr")
        if address_contract.get("enrichedRows") != preflight.address_enriched_rows:
            raise ValueError("Stage 06 addressEnrichment enrichedRows mismatch")
        if address_contract.get("unresolvedRows") != preflight.address_unresolved_rows:
            raise ValueError("Stage 06 addressEnrichment unresolvedRows mismatch")
        if address_contract.get("rawAddressMutationCount") != 0:
            raise ValueError("Stage 06 addressEnrichment reports raw address mutation")
        if address_contract.get("fabricatedSpatialRelationshipCount") != 0:
            raise ValueError("Stage 06 addressEnrichment reports fabricated spatial relationships")
        report_filename = safe_str(address_contract.get("reportFilename"))
        report_sha = safe_str(address_contract.get("reportSha256")).lower()
        if not report_filename or not report_sha:
            raise ValueError("Stage 06 addressEnrichment report fingerprint is incomplete")
        report_path = config.input_path.with_name(report_filename)
        require_file(report_path, "Stage 06 address enrichment report")
        if sha256_file(report_path) != report_sha:
            raise ValueError("Stage 06 address enrichment report SHA mismatch")
        commercial_address_source = source.get("commercialAddressSource")
        if not isinstance(commercial_address_source, Mapping):
            raise ValueError("Atomic enriched Stage 06 manifest is missing commercialAddressSource")
        if commercial_address_source.get("role") != "ADDRESS_EVIDENCE_ONLY":
            raise ValueError("Atomic commercialAddressSource role must be ADDRESS_EVIDENCE_ONLY")
        if commercial_address_source.get("salesTruthAuthority") != "ATOMIC":
            raise ValueError("Atomic commercialAddressSource may not become Sales truth")
    else:
        if address_contract is not None:
            raise ValueError("Stage 06 manifest advertises addressEnrichment but CSV lacks address columns")

    expected = {
        "filename": config.input_path.name,
        "rows": preflight.row_count,
        "columns": list(actual_columns),
        "sha256": preflight.csv_sha256,
        "documentIdsSha256": preflight.document_ids_sha256,
        "months": preflight.months,
        "provider": GOVERNED_PROVIDER,
        "totalAmountC": preflight.total_amount_c,
        "visibilityColumn": "ABSENT",
        **(
            {"addressEnrichment": dict(address_contract)}
            if preflight.address_enrichment_enabled
            else {}
        ),
    }
    for field, wanted in expected.items():
        if output.get(field) != wanted:
            raise ValueError(
                f"Stage 06 manifest outputContract.{field} does not match the supplied CSV"
            )

    return {
        "path": str(manifest_snapshot.path),
        "filename": manifest_snapshot.path.name,
        "sha256": manifest_snapshot.sha256,
        "buildFingerprint": recorded,
        "stage05BuildFingerprint": (source.get("stage05Manifest") or {}).get("buildFingerprint"),
        "asOfDate": as_of_date.isoformat(),
        "lmPcode": source.get("lmPcode"),
        "fromMonth": source.get("fromMonth"),
        "toMonth": source.get("toMonth"),
        "addressEnrichment": dict(address_contract) if isinstance(address_contract, Mapping) else None,
    }


def make_upload_contract(
    config: UploadConfig,
    preflight: PreflightResult,
    stage06_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "projectId": config.project_id,
        "collection": COLLECTION_NAME,
        "stage06ManifestFilename": stage06_evidence["filename"],
        "stage06ManifestSha256": stage06_evidence["sha256"],
        "stage06BuildFingerprint": stage06_evidence["buildFingerprint"],
        "stage05BuildFingerprint": stage06_evidence["stage05BuildFingerprint"],
        "lmPcode": stage06_evidence["lmPcode"],
        "asOfDate": stage06_evidence["asOfDate"],
        "csvFilename": config.input_path.name,
        "csvSha256": preflight.csv_sha256,
        "rows": preflight.row_count,
        "documentIdsSha256": preflight.document_ids_sha256,
        "months": preflight.months,
        "providers": preflight.providers,
        "totalAmountC": preflight.total_amount_c,
        "visibilityColumn": "ABSENT",
        "visibilityCreationDefault": DEFAULT_MASTER_VISIBILITY,
        "visibilityResumePolicy": "PRESERVE_VALID_EXISTING_OR_BLOCK",
        "visibilityLifecycleOwner": "OPERATIONAL_BRIDGE",
        "visibilityOwnership": "OPERATIONAL_WRITERS_ONLY",
        "addressEnrichment": stage06_evidence.get("addressEnrichment"),
    }


def validate_resume_report(
    path: Path,
    current_contract: Mapping[str, Any],
    current_fingerprint: str,
) -> dict[str, Any]:
    previous = read_json(path, "Previous failed Stage 08 report")
    if safe_str(previous.get("stage")) != "08":
        raise ValueError("--resume-report is not a Stage 08 report")
    if safe_str(previous.get("script")) != "08_upload_sales_all_meters.py":
        raise ValueError("--resume-report script identity mismatch")
    if safe_str(previous.get("operation")) != "sales_all_meters_upload":
        raise ValueError("--resume-report operation identity mismatch")
    if safe_str(previous.get("status")) != "FAIL" or safe_str(previous.get("result")) != "FAILED":
        raise ValueError("--resume-report must be from a failed Stage 08 upload")

    previous_contract = previous.get("uploadContract")
    if not isinstance(previous_contract, Mapping):
        raise ValueError("--resume-report has no uploadContract")
    recorded_fingerprint = safe_str(previous.get("uploadFingerprint"))
    recalculated_previous = canonical_json_sha256(previous_contract)
    if recorded_fingerprint != recalculated_previous:
        raise ValueError("--resume-report fingerprint is invalid; the report may be edited or corrupt")
    if dict(previous_contract) != dict(current_contract):
        raise ValueError(
            "Resume blocked: current project, CSV SHA, row set, month range, provider or "
            "sales total does not match the failed original Stage 08 upload"
        )
    if recorded_fingerprint != current_fingerprint:
        raise ValueError("Resume blocked: upload fingerprint differs from the failed original upload")

    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "previousStartedAt": previous.get("startedAt"),
        "previousFinishedAt": previous.get("finishedAt"),
        "previousMode": previous.get("mode"),
    }


def make_report_path(report_dir: Path, project_id: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return report_dir / f"sales_all_meters_upload__{project_id}__{timestamp}.json"


def write_report(report_path: Path, payload: Mapping[str, Any]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_suffix(report_path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as target:
            json.dump(payload, target, indent=2, sort_keys=True, default=str)
            target.write("\n")
        temporary.replace(report_path)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_config(args: argparse.Namespace) -> UploadConfig:
    project_id = safe_str(args.project_id)
    confirm_project = safe_str(args.confirm_project)
    if not project_id:
        raise ValueError("--project-id may not be blank")
    if confirm_project != project_id:
        raise ValueError(
            f"Project confirmation failed: --project-id={project_id!r}, "
            f"--confirm-project={confirm_project!r}"
        )
    if args.mode == "initial-load" and project_id not in INITIAL_LOAD_PROJECTS:
        allowed = ", ".join(sorted(INITIAL_LOAD_PROJECTS))
        raise ValueError(f"Stage 08 initial-load is hard-gated to projects: {allowed}")
    if args.mode == "resume" and args.resume_report is None:
        raise ValueError("--mode resume requires --resume-report")
    if args.mode != "resume" and args.resume_report is not None:
        raise ValueError("--resume-report may be used only with --mode resume")

    return UploadConfig(
        project_id=project_id,
        service_account_path=resolve_project_path(args.service_account),
        input_path=resolve_project_path(args.input),
        manifest_path=resolve_project_path(args.manifest),
        mode=args.mode,
        resume_report_path=(
            resolve_project_path(args.resume_report) if args.resume_report is not None else None
        ),
        report_dir=resolve_project_path(args.report_dir),
        preflight_only=bool(args.preflight_only),
    )


def print_preflight(config: UploadConfig, result: PreflightResult) -> None:
    print("\n=== SALES ALL METERS UPLOAD PREFLIGHT ===")
    print(f"Target project:       {config.project_id}")
    print(f"Collection:           {COLLECTION_NAME}")
    print(f"Mode:                 {config.mode}")
    print(f"Input CSV:            {config.input_path}")
    print(f"Stage 06 manifest:   {config.manifest_path}")
    print(f"Rows:                 {result.row_count:,}")
    print(f"Unique master IDs:    {result.unique_master_ids:,}")
    print(f"Months:               {len(result.months)} ({result.months[0]} to {result.months[-1]})")
    print(f"Provider:             {', '.join(result.providers)}")
    print(f"Total amount cents:   {result.total_amount_c:,}")
    print(f"CSV SHA-256:          {result.csv_sha256}")
    print(
        "Address enrichment:   "
        + (
            f"ENABLED ({result.address_enriched_rows:,} enriched / "
            f"{result.address_unresolved_rows:,} unresolved)"
            if result.address_enrichment_enabled
            else "LEGACY / NOT PRESENT"
        )
    )
    print("Visibility column:    ABSENT (Stage 06)")
    print(f"Stage 08 default:     {DEFAULT_MASTER_VISIBILITY}")
    print("Resume visibility:    PRESERVE VALID / BLOCK INVALID")
    print("===========================================\n")


def main() -> None:
    args = parse_args()
    if args.mode == "refresh":
        from sales_pipeline_sales_all_refresh import run_refresh
        report_path = run_refresh(
            project_id=safe_str(args.project_id),
            confirm_project=safe_str(args.confirm_project),
            service_account_path=resolve_project_path(args.service_account),
            input_path=resolve_project_path(args.input),
            manifest_path=resolve_project_path(args.manifest),
            report_dir=resolve_project_path(args.report_dir),
            preflight_only=bool(args.preflight_only),
        )
        print("=== SALES ALL METERS REFRESH COMPLETE ===")
        print(f"Mode: {'PREFLIGHT ONLY' if args.preflight_only else 'REFRESH + VERIFY'}")
        print(f"Project: {safe_str(args.project_id)}")
        print("Deletes: 0")
        print(f"Report: {report_path}")
        return
    if args.preflight_only and args.mode not in ("initial-load", "refresh"):
        raise ValueError("--preflight-only is supported with --mode initial-load or refresh")
    config = build_config(args)
    started_at = datetime.now(UTC)
    report_path = make_report_path(config.report_dir, config.project_id)
    progress = UploadProgress()

    report: dict[str, Any] = {
        "stage": "08",
        "script": "08_upload_sales_all_meters.py",
        "operation": "sales_all_meters_upload",
        "projectId": config.project_id,
        "collection": COLLECTION_NAME,
        "mode": config.mode,
        "preflightOnly": config.preflight_only,
        "inputPath": str(config.input_path),
        "manifestPath": str(config.manifest_path),
        "serviceAccountPath": str(config.service_account_path),
        "startedAt": started_at.isoformat(),
        "status": "STARTED",
        "result": "STARTED",
    }

    try:
        require_file(config.input_path, "Input CSV")
        require_file(config.manifest_path, "Stage 06 manifest")
        require_file(config.service_account_path, "Service-account file")

        credential_project = read_service_account_project_id(config.service_account_path)
        report["credentialProjectId"] = credential_project
        if credential_project != config.project_id:
            raise ValueError(
                "Service-account project mismatch. "
                f"Requested={config.project_id!r}; credential={credential_project!r}"
            )

        rich_initial_load = config.mode == "initial-load"
        if rich_initial_load:
            from sales_pipeline_sales_all_refresh import load_and_validate as load_current_stage06

            rows, stage06_evidence = load_current_stage06(
                config.input_path,
                config.manifest_path,
            )
            monthly_columns = []
            report.update(
                {
                    "stage06Evidence": dict(stage06_evidence),
                    "csvSha256": stage06_evidence["csvSha256"],
                    "rowsRead": len(rows),
                    "uniqueMasterIds": len({str(row["masterId"]) for row in rows}),
                    "months": stage06_evidence["months"],
                    "providers": [stage06_evidence["provider"]],
                    "totalAmountC": stage06_evidence["totalAmountC"],
                    "visibilityColumn": "ABSENT",
                    "visibilityCreationDefault": DEFAULT_MASTER_VISIBILITY,
                    "visibilityResumePolicy": "NOT_APPLICABLE_CREATE_ONLY",
                    "visibilityLifecycleOwner": "OPERATIONAL_BRIDGE",
                    "currentStage06RichContract": True,
                }
            )
            print("\n=== SALES ALL METERS PREFLIGHT ===")
            print(f"Mode:                 {config.mode}")
            print(f"Target project:       {config.project_id}")
            print(f"Rows:                 {len(rows):,}")
            print(f"Provider:             {stage06_evidence['provider']}")
            print("Stage 06 contract:    CURRENT RICH MONTHLY-SOURCE")
            print("Visibility column:    ABSENT (Stage 06)")
            print(f"Stage 08 default:     {DEFAULT_MASTER_VISIBILITY}")
            print("===========================================\n")
        else:
            df, monthly_columns, preflight = load_and_validate_csv(config.input_path)
            manifest_snapshot = read_json_snapshot(config.manifest_path, "Stage 06 manifest")
            stage06_evidence = validate_stage06_manifest(
                config,
                preflight,
                list(df.columns),
                manifest_snapshot,
            )
            validate_recency_contract(
                df,
                preflight.months,
                parse_manifest_as_of_date(stage06_evidence["asOfDate"]),
            )
            rows = dataframe_rows(df)
            upload_contract = make_upload_contract(config, preflight, stage06_evidence)
            upload_fingerprint = canonical_json_sha256(upload_contract)
            report.update(
                {
                    "uploadContract": upload_contract,
                    "uploadFingerprint": upload_fingerprint,
                    "stage06Evidence": dict(stage06_evidence),
                    "csvSha256": preflight.csv_sha256,
                    "documentIdsSha256": preflight.document_ids_sha256,
                    "rowsRead": preflight.row_count,
                    "uniqueMasterIds": preflight.unique_master_ids,
                    "months": preflight.months,
                    "providers": preflight.providers,
                    "totalAmountC": preflight.total_amount_c,
                    "visibilityColumn": "ABSENT",
                    "visibilityCreationDefault": DEFAULT_MASTER_VISIBILITY,
                    "visibilityResumePolicy": "PRESERVE_VALID_EXISTING_OR_BLOCK",
                    "visibilityLifecycleOwner": "OPERATIONAL_BRIDGE",
                }
            )

        if config.mode == "resume":
            assert config.resume_report_path is not None
            report["resumeEvidence"] = validate_resume_report(
                config.resume_report_path,
                upload_contract,
                upload_fingerprint,
            )

        if not rich_initial_load:
            print_preflight(config, preflight)

        if firestore is None or service_account is None:
            raise RuntimeError(
                "google-cloud-firestore and google-auth are required for Stage 08 upload"
            )

        credentials = service_account.Credentials.from_service_account_file(
            str(config.service_account_path)
        )
        db = firestore.Client(project=config.project_id, credentials=credentials)

        if config.mode == "initial-load" and config.preflight_only:
            existing_ids = find_existing_input_ids(
                db,
                [str(row["masterId"]) for row in rows],
                label="Sales All initial-load preflight",
            )
            report.update(
                {
                    "inputScopeExistingCount": len(existing_ids),
                    "inputScopeExistingIds": existing_ids[:100],
                    "documentsCreated": 0,
                    "committedBatches": 0,
                }
            )
            if existing_ids:
                raise RuntimeError(
                    f"Stage 08 initial-load blocked: {len(existing_ids)} input document ID(s) "
                    f"already exist; first={existing_ids[:10]}"
                )
            report.update({"status": "PASS", "result": "PREFLIGHT_PASS"})
            print("=== SALES ALL INITIAL-LOAD PREFLIGHT PASS ===")
            print(f"Input IDs checked: {len(rows):,}")
            print("Existing input IDs: 0")
            print("Firestore writes: 0")
            return

        if config.mode == "create-only":
            if collection_has_documents(db):
                raise RuntimeError(
                    f"Upload blocked: {COLLECTION_NAME} is not empty in {config.project_id}. "
                    "Use --mode resume only for recovery from the exact failed upload report."
                )
            matching = 0
            missing_before = len(rows)
            create_documents(db, rows, monthly_columns, progress)
        elif config.mode == "initial-load":
            existing_ids = find_existing_input_ids(
                db,
                [str(row["masterId"]) for row in rows],
                label="Sales All initial-load gate",
            )
            report["inputScopeExistingCount"] = len(existing_ids)
            if existing_ids:
                report["inputScopeExistingIds"] = existing_ids[:100]
                raise RuntimeError(
                    f"Initial-load blocked: {len(existing_ids)} input Sales All ID(s) already exist; "
                    f"first={existing_ids[:10]}"
                )
            matching = 0
            missing_before = len(rows)
            create_prebuilt_documents(db, rows, progress)
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
            matching = plan.matching_count
            missing_before = len(plan.missing_rows)
            create_documents(db, plan.missing_rows, monthly_columns, progress)

        verification = (
            verify_prebuilt_input_scope(db, rows)
            if config.mode == "initial-load"
            else verify_post_upload(db, rows, monthly_columns)
        )
        report.update(
            {
                "documentsCreated": progress.documents_created,
                "committedBatches": progress.committed_batches,
                "matchingDocuments": matching,
                "missingDocumentsBeforeWrite": missing_before,
                "verification": verification,
                "status": "PASS",
                "result": "UPLOAD_VERIFIED",
            }
        )

        print("=== SALES ALL METERS UPLOAD COMPLETE ===")
        print(f"Project:            {config.project_id}")
        print(f"Mode:               {config.mode}")
        print(f"Documents created:  {progress.documents_created:,}")
        print(f"Existing matching:  {matching:,}")
        if config.mode == "initial-load":
            print(f"Input scope verified:{verification['verifiedInputScopeCount']:,}")
            print("Verification:        FULL INPUT SCOPE / BATCHED get_all")
        else:
            print(f"Final collection:   {verification['finalCount']:,}")
        print(f"Creation visibility: {DEFAULT_MASTER_VISIBILITY}")
        print("Existing visibility: PRESERVED WHEN VALID")
        print(f"Run report:         {report_path}")

    except Exception as exc:
        report.update(
            {
                "documentsCreated": progress.documents_created,
                "committedBatches": progress.committed_batches,
                "status": "FAIL",
                "result": "FAILED",
                "errorType": type(exc).__name__,
                "error": str(exc),
            }
        )
        raise
    finally:
        report["finishedAt"] = datetime.now(UTC).isoformat()
        write_report(report_path, report)


if __name__ == "__main__":
    main()
