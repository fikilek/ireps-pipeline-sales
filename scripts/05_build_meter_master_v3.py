"""
05_build_meter_master_v3.py

Build an environment-neutral meter master staging CSV from:
- every approved meter-level monthly sales CSV in one explicit continuous range
- Customer_Details.csv
- 90_Days_No_Purchase_Report.csv

The script does not connect to Firebase and does not choose a Firebase project.
The generated CSV can later be uploaded to ireps-test, ireps-trials, or
ireps-production by an environment-aware uploader.

Default monthly filename format:
    monthly__FULL__YYYY-MM__from_atomic.csv

Default output filename:
    meter_master__<lmPcode>__FULL__<first-month>_to_<last-month>.csv

Governance controls:
- every project path resolves from this script's repository root
- every month requires matching successful one-month Stage 03 manifest evidence
- monthly rows must satisfy the exact Stage 03 schema, identities, types, reconciliation, LM, and filename month
- every source is parsed from the same immutable byte snapshot whose SHA-256 is recorded
- meter identifiers must be non-empty uppercase alphanumeric values after normalisation
- Customer Details duplicates prefer the dominant identity pattern where CustomerNo equals AccountNo and differs from MeterNumber
- an Active duplicate may replace a Block Purchases duplicate without consulting ERF, address, or customer-name fields
- competing dominant identities may resolve only by the latest valid LastPurchaseDate
- tied or missing purchase dates still stop the build
- 90-day report duplicates use the same placeholder preference and may resolve competing real customer numbers by latest valid LastPurchaseDate
- tied or missing NPR purchase dates still stop genuine competing non-placeholder identities
- --from-month and --to-month are mandatory and every month in the range must exist
- the current governed provider is conlog and the current meter type is electricity
- the final CSV is accompanied by a frozen Stage 05 JSON manifest containing input hashes,
  output hash, range, included months, build statistics, and a deterministic fingerprint
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GOVERNED_PROVIDER = "conlog"
GOVERNED_METER_TYPE = "electricity"
MANIFEST_SCHEMA_VERSION = 1
STAGE03_SCRIPT = "03_aggregate_monthly_from_atomic_outputs.py"
STAGE03_DATASETS = ("monthly", "monthly_lm", "monthly_lm_groups")
MONTHLY_FILENAME_RE = re.compile(
    r"^monthly__(?P<scope>[A-Za-z0-9_-]+)__(?P<period>\d{4}-\d{2})__from_atomic\.csv$"
)
METER_NO_RE = re.compile(r"^[A-Z0-9]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

MONTHLY_COLUMNS = [
    "docId",
    "lmPcode",
    "meterNo",
    "ym",
    "y",
    "m",
    "purchasesCount",
    "amountTotalC",
    "costC",
    "vatC",
    "firstPurchaseAtISO",
    "lastPurchaseAtISO",
    "firstPurchaseAtMs",
    "lastPurchaseAtMs",
    "salesGroupId",
    "salesGroupLabel",
]

MASTER_COLUMNS = [
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

MeterKey = str
MasterMap = Dict[MeterKey, Dict[str, Any]]
CustomerMap = Dict[MeterKey, Dict[str, Any]]
NprMap = Dict[MeterKey, Dict[str, Any]]


@dataclass(frozen=True)
class MonthlyInput:
    period: str
    path: Path


@dataclass(frozen=True)
class CsvSnapshot:
    path: Path
    payload: bytes
    frame: pd.DataFrame
    sha256: str


@dataclass(frozen=True)
class JsonSnapshot:
    path: Path
    payload: Mapping[str, Any]
    sha256: str


@dataclass(frozen=True)
class ApprovedMonthlyInput:
    period: str
    csv: CsvSnapshot
    frame: pd.DataFrame
    reconciliation: Mapping[str, Any]
    stage03_manifest: JsonSnapshot

    @property
    def path(self) -> Path:
        return self.csv.path


@dataclass(frozen=True)
class BuildConfig:
    lm_pcode: str
    provider: str
    meter_type: str


@dataclass
class BuildStats:
    monthly_backed_meters: int = 0
    customer_only_seeded_meters: int = 0
    npr_only_seeded_meters: int = 0
    customer_placeholder_duplicates_resolved: int = 0
    customer_active_status_duplicates_resolved: int = 0
    customer_latest_purchase_duplicates_resolved: int = 0
    npr_placeholder_duplicates_resolved: int = 0
    npr_latest_purchase_duplicates_resolved: int = 0
    total_master_rows: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build meter_master from one continuous range of approved Stage 03 "
            "monthly sales files."
        )
    )
    parser.add_argument(
        "--lm-pcode",
        required=True,
        help="Local municipality/workbase code, for example ZA7423.",
    )
    parser.add_argument(
        "--monthly-dir",
        type=Path,
        default=Path("output/monthly"),
        help="Directory containing monthly meter-level CSV files.",
    )
    parser.add_argument(
        "--scope",
        default="FULL",
        choices=("FULL",),
        help="Monthly filename scope to consume. Default: FULL.",
    )
    parser.add_argument(
        "--stage03-manifest-dir",
        type=Path,
        default=Path("output/logs/monthly_build"),
        help=(
            "Directory containing successful one-month Stage 03 BUILD_WRITTEN "
            "manifests for every requested month."
        ),
    )
    parser.add_argument(
        "--from-month",
        required=True,
        help="Required inclusive first month in YYYY-MM format.",
    )
    parser.add_argument(
        "--to-month",
        required=True,
        help="Required inclusive last month in YYYY-MM format.",
    )
    parser.add_argument(
        "--customer-details",
        type=Path,
        default=Path("input/reference/Customer_Details.csv"),
        help="Customer details reference CSV.",
    )
    parser.add_argument(
        "--npr",
        type=Path,
        default=Path("input/reference/90_Days_No_Purchase_Report.csv"),
        help="90-days-no-purchase reference CSV.",
    )
    parser.add_argument(
        "--provider",
        default=GOVERNED_PROVIDER,
        choices=(GOVERNED_PROVIDER,),
        help="Governed sales provider. Current approved value: conlog.",
    )
    parser.add_argument(
        "--meter-type",
        default=GOVERNED_METER_TYPE,
        choices=(GOVERNED_METER_TYPE,),
        help="Governed meter type. Current approved value: electricity.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/meter_master"),
        help="Directory for the generated meter master CSV.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional exact output CSV path. Overrides --output-dir.",
    )
    return parser.parse_args()


def validate_month(value: str, argument_name: str) -> None:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError(f"{argument_name} must use YYYY-MM format: {value}")


def resolve_project_path(path: Path) -> Path:
    """Resolve relative runtime paths from the repository root, never the shell CWD."""
    return path if path.is_absolute() else PROJECT_ROOT / path


def require_file(filepath: Path) -> None:
    if not filepath.is_file():
        raise FileNotFoundError(f"Required file not found: {filepath}")


def require_columns(df: pd.DataFrame, required: Sequence[str], source: Path) -> None:
    missing = [column for column in required if column not in df.columns]
    if missing:
        raise ValueError(f"{source} is missing required columns: {', '.join(missing)}")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def read_csv_snapshot(path: Path, label: str) -> CsvSnapshot:
    """Read and hash one immutable byte snapshot, then parse only those bytes."""
    require_file(path)
    payload = path.read_bytes()
    last_error: Optional[Exception] = None
    frame: Optional[pd.DataFrame] = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            frame = pd.read_csv(io.BytesIO(payload), dtype=str, encoding=encoding).fillna("")
            break
        except Exception as exc:
            last_error = exc
    if frame is None:
        raise ValueError(f"{label} is not a valid CSV: {path}") from last_error
    return CsvSnapshot(
        path=path.resolve(),
        payload=payload,
        frame=frame,
        sha256=sha256_bytes(payload),
    )


def read_json_snapshot(path: Path, label: str) -> JsonSnapshot:
    """Read, hash, and decode one immutable JSON byte snapshot."""
    require_file(path)
    payload = path.read_bytes()
    try:
        decoded = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} must contain one JSON object: {path}")
    return JsonSnapshot(
        path=path.resolve(),
        payload=decoded,
        sha256=sha256_bytes(payload),
    )


def require_json_int(
    value: Any,
    label: str,
    *,
    minimum: Optional[int] = None,
) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be a JSON integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return value


def require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 fingerprint")
    return value


def discover_monthly_inputs(
    monthly_dir: Path,
    scope: str,
    from_month: Optional[str] = None,
    to_month: Optional[str] = None,
) -> list[MonthlyInput]:
    if not monthly_dir.is_dir():
        raise FileNotFoundError(f"Monthly directory not found: {monthly_dir}")

    discovered: dict[str, Path] = {}

    for path in monthly_dir.glob("monthly__*__????-??__from_atomic.csv"):
        match = MONTHLY_FILENAME_RE.fullmatch(path.name)
        if not match or match.group("scope") != scope:
            continue

        period = match.group("period")
        validate_month(period, "Monthly filename period")

        if from_month and period < from_month:
            continue
        if to_month and period > to_month:
            continue

        if period in discovered:
            raise ValueError(
                f"Duplicate monthly files found for {period}: "
                f"{discovered[period]} and {path}"
            )
        discovered[period] = path

    inputs = [MonthlyInput(period, discovered[period]) for period in sorted(discovered)]
    if not inputs:
        raise FileNotFoundError(
            f"No monthly files found in {monthly_dir} for scope={scope!r}"
        )

    return inputs


def month_sequence(first_month: str, last_month: str) -> list[str]:
    first = pd.Period(first_month, freq="M")
    last = pd.Period(last_month, freq="M")
    return [str(period) for period in pd.period_range(first, last, freq="M")]


def validate_month_continuity(
    monthly_inputs: Sequence[MonthlyInput],
    from_month: str,
    to_month: str,
) -> list[str]:
    discovered = [item.period for item in monthly_inputs]
    expected = month_sequence(from_month, to_month)
    missing = [period for period in expected if period not in discovered]
    unexpected = [period for period in discovered if period not in expected]
    if missing or unexpected or discovered != expected:
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unexpected:
            details.append("unexpected=" + ",".join(unexpected))
        if not details:
            details.append("discovered month order does not match the required range")
        raise ValueError(
            "Stage 05 requires every consecutive month from "
            f"{from_month} through {to_month}: " + "; ".join(details)
        )
    return expected


def normalize_meter_no(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return "".join(str(value).strip().upper().split())


def validate_meter_no(value: Any, source: Path, row_number: int) -> str:
    """Normalise one meter identifier and reject blank or unsafe characters."""
    normalized = normalize_meter_no(value)
    if not normalized:
        raise ValueError(f"{source} row {row_number} has a blank meter identifier.")
    if not METER_NO_RE.fullmatch(normalized):
        raise ValueError(
            f"{source} row {row_number} has invalid meter identifier {value!r}; "
            "after normalisation, only A-Z and 0-9 are allowed."
        )
    return normalized


def safe_str(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def strict_integer_series(
    frame: pd.DataFrame,
    column: str,
    source: Path,
) -> pd.Series:
    raw = frame[column].map(safe_str)
    invalid = ~raw.str.fullmatch(r"-?\d+")
    if invalid.any():
        examples = [int(index) + 2 for index in invalid[invalid].index[:5]]
        raise ValueError(
            f"{source.name}: {column} must contain integer text in every row; "
            f"invalid CSV line examples: {examples}"
        )
    if frame[column].astype(str).ne(raw).any():
        raise ValueError(f"{source.name}: whitespace drift in integer column {column}")
    return raw.astype("int64")


def sales_group_from_amount_total_c(amount_total_c: int) -> str:
    if amount_total_c <= 9_999:
        return "GR1"
    if amount_total_c <= 29_999:
        return "GR2"
    if amount_total_c <= 49_999:
        return "GR3"
    if amount_total_c <= 99_999:
        return "GR4"
    return "GR5"


def sales_group_label(group_id: str) -> str:
    labels = {
        "GR1": "<=99.99",
        "GR2": "100-299.99",
        "GR3": "300-499.99",
        "GR4": "500-999.99",
        "GR5": ">=1000",
    }
    return labels[group_id]


def validate_monthly_snapshot(
    monthly_input: MonthlyInput,
    snapshot: CsvSnapshot,
    config: BuildConfig,
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    """Validate the complete Stage 03 meter-month contract from one byte snapshot."""
    frame = snapshot.frame.copy()
    source = snapshot.path
    if list(frame.columns) != MONTHLY_COLUMNS:
        raise ValueError(
            f"{source.name}: monthly schema mismatch. Expected {MONTHLY_COLUMNS}; "
            f"found {list(frame.columns)}"
        )
    if frame.empty:
        raise ValueError(f"Monthly CSV is empty: {source}")

    string_columns = [
        "docId",
        "lmPcode",
        "meterNo",
        "ym",
        "firstPurchaseAtISO",
        "lastPurchaseAtISO",
        "salesGroupId",
        "salesGroupLabel",
    ]
    for column in string_columns:
        cleaned = frame[column].map(safe_str)
        if cleaned.eq("").any():
            raise ValueError(f"{source.name}: blank values in {column}")
        if frame[column].astype(str).ne(cleaned).any():
            raise ValueError(f"{source.name}: whitespace drift in {column}")
        frame[column] = cleaned

    numeric_columns = [
        "y",
        "m",
        "purchasesCount",
        "amountTotalC",
        "costC",
        "vatC",
        "firstPurchaseAtMs",
        "lastPurchaseAtMs",
    ]
    for column in numeric_columns:
        frame[column] = strict_integer_series(frame, column, source)

    if frame["docId"].duplicated().any():
        raise ValueError(f"{source.name}: duplicate docId values")
    if frame["meterNo"].duplicated().any():
        raise ValueError(f"{source.name}: duplicate meterNo values")
    if not frame["lmPcode"].eq(config.lm_pcode).all():
        raise ValueError(f"{source.name}: lmPcode mismatch")
    if not frame["ym"].eq(monthly_input.period).all():
        raise ValueError(f"{source.name}: ym mismatch")

    expected_year, expected_month = (
        int(part) for part in monthly_input.period.split("-")
    )
    if not frame["y"].eq(expected_year).all() or not frame["m"].eq(expected_month).all():
        raise ValueError(
            f"{source.name}: y/m values do not match {monthly_input.period}"
        )

    normalized = frame["meterNo"].map(normalize_meter_no)
    valid_meter = frame["meterNo"].str.fullmatch(METER_NO_RE)
    if not valid_meter.all() or not frame["meterNo"].eq(normalized).all():
        raise ValueError(
            f"{source.name}: meterNo must already be canonical uppercase alphanumeric text"
        )
    expected_doc_id = (
        frame["lmPcode"] + "__" + frame["meterNo"] + "__" + frame["ym"]
    )
    if not frame["docId"].eq(expected_doc_id).all():
        raise ValueError(f"{source.name}: deterministic docId mismatch")

    if not frame["purchasesCount"].gt(0).all():
        raise ValueError(f"{source.name}: purchasesCount must be positive")
    if not (
        frame["amountTotalC"].ge(0)
        & frame["costC"].ge(0)
        & frame["vatC"].ge(0)
    ).all():
        raise ValueError(f"{source.name}: negative monthly monetary value")
    if not frame["amountTotalC"].eq(frame["costC"] + frame["vatC"]).all():
        raise ValueError(f"{source.name}: amountTotalC != costC + vatC")

    first_purchase = pd.to_datetime(
        frame["firstPurchaseAtISO"],
        format="%Y-%m-%dT%H:%M:%SZ",
        errors="coerce",
        utc=True,
    )
    last_purchase = pd.to_datetime(
        frame["lastPurchaseAtISO"],
        format="%Y-%m-%dT%H:%M:%SZ",
        errors="coerce",
        utc=True,
    )
    if first_purchase.isna().any() or last_purchase.isna().any():
        raise ValueError(f"{source.name}: invalid first/last purchase timestamp")
    if not frame["firstPurchaseAtISO"].eq(
        first_purchase.dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    ).all() or not frame["lastPurchaseAtISO"].eq(
        last_purchase.dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    ).all():
        raise ValueError(f"{source.name}: purchase timestamps are not canonical UTC text")
    if not first_purchase.dt.strftime("%Y-%m").eq(monthly_input.period).all():
        raise ValueError(f"{source.name}: firstPurchaseAtISO is outside the month")
    if not last_purchase.dt.strftime("%Y-%m").eq(monthly_input.period).all():
        raise ValueError(f"{source.name}: lastPurchaseAtISO is outside the month")
    if not first_purchase.le(last_purchase).all():
        raise ValueError(f"{source.name}: first purchase is later than last purchase")
    expected_first_ms = (first_purchase.astype("int64") // 1_000_000).astype("int64")
    expected_last_ms = (last_purchase.astype("int64") // 1_000_000).astype("int64")
    if not frame["firstPurchaseAtMs"].eq(expected_first_ms).all():
        raise ValueError(f"{source.name}: firstPurchaseAtMs does not match ISO")
    if not frame["lastPurchaseAtMs"].eq(expected_last_ms).all():
        raise ValueError(f"{source.name}: lastPurchaseAtMs does not match ISO")

    expected_groups = frame["amountTotalC"].map(sales_group_from_amount_total_c)
    if not frame["salesGroupId"].eq(expected_groups).all():
        raise ValueError(f"{source.name}: salesGroupId classification mismatch")
    expected_labels = frame["salesGroupId"].map(sales_group_label)
    if not frame["salesGroupLabel"].eq(expected_labels).all():
        raise ValueError(f"{source.name}: salesGroupLabel mismatch")

    reconciliation = {
        "lmPcode": config.lm_pcode,
        "month": monthly_input.period,
        "purchasesCount": int(frame["purchasesCount"].sum()),
        "metersCount": int(len(frame)),
        "amountTotalC": int(frame["amountTotalC"].sum()),
        "costC": int(frame["costC"].sum()),
        "vatC": int(frame["vatC"].sum()),
    }
    return frame, reconciliation


def expected_stage03_filename(dataset: str, scope: str, month: str) -> str:
    prefixes = {
        "monthly": "monthly",
        "monthly_lm": "monthly_lm",
        "monthly_lm_groups": "monthly_lm_groups",
    }
    return f"{prefixes[dataset]}__{scope}__{month}__from_atomic.csv"


def validate_stage03_manifest(
    manifest: JsonSnapshot,
    *,
    monthly_input: MonthlyInput,
    monthly_snapshot: CsvSnapshot,
    reconciliation: Mapping[str, Any],
    config: BuildConfig,
    scope: str,
) -> None:
    payload = manifest.payload
    label = manifest.path.name
    expected_manifest_prefix = (
        f"stage03_monthly_build__{config.lm_pcode}__{monthly_input.period}__"
    )
    if not label.startswith(expected_manifest_prefix) or not label.endswith(".json"):
        raise ValueError(f"{label}: Stage 03 manifest filename identity mismatch")
    if payload.get("stage") != "03" or payload.get("script") != STAGE03_SCRIPT:
        raise ValueError(f"{label}: manifest is not from approved Stage 03")
    if payload.get("status") != "PASS" or payload.get("result") != "BUILD_WRITTEN":
        raise ValueError(f"{label}: manifest is not a successful Stage 03 build")
    if payload.get("operation") != "build-write":
        raise ValueError(f"{label}: Stage 03 operation must be build-write")
    if payload.get("lmPcode") != config.lm_pcode:
        raise ValueError(f"{label}: Stage 03 LM mismatch")
    if payload.get("month") != monthly_input.period:
        raise ValueError(f"{label}: Stage 03 month mismatch")

    manifest_reconciliation = payload.get("reconciliation")
    if not isinstance(manifest_reconciliation, list) or len(manifest_reconciliation) != 1:
        raise ValueError(f"{label}: Stage 03 reconciliation must contain exactly one LM/month")
    recorded_reconciliation = manifest_reconciliation[0]
    if not isinstance(recorded_reconciliation, Mapping):
        raise ValueError(f"{label}: Stage 03 reconciliation row is invalid")
    expected_reconciliation_keys = {
        "lmPcode",
        "month",
        "purchasesCount",
        "metersCount",
        "amountTotalC",
        "costC",
        "vatC",
    }
    if set(recorded_reconciliation) != expected_reconciliation_keys:
        raise ValueError(f"{label}: Stage 03 reconciliation fields are incomplete or unexpected")
    for field in ("purchasesCount", "metersCount", "amountTotalC", "costC", "vatC"):
        require_json_int(recorded_reconciliation.get(field), f"{label} reconciliation.{field}", minimum=0)
    if recorded_reconciliation.get("purchasesCount") == 0 or recorded_reconciliation.get("metersCount") == 0:
        raise ValueError(f"{label}: Stage 03 reconciliation counts must be positive")
    if recorded_reconciliation.get("amountTotalC") != (
        recorded_reconciliation.get("costC") + recorded_reconciliation.get("vatC")
    ):
        raise ValueError(f"{label}: Stage 03 reconciliation money does not balance")
    if dict(recorded_reconciliation) != dict(reconciliation):
        raise ValueError(f"{label}: reconciliation does not match the monthly CSV")

    outputs = payload.get("outputs")
    if not isinstance(outputs, list) or len(outputs) != 3:
        raise ValueError(f"{label}: Stage 03 manifest must contain exactly three outputs")
    by_dataset: dict[str, Mapping[str, Any]] = {}
    for item in outputs:
        if not isinstance(item, Mapping):
            raise ValueError(f"{label}: Stage 03 output entry is invalid")
        dataset = item.get("dataset")
        if dataset not in STAGE03_DATASETS or dataset in by_dataset:
            raise ValueError(f"{label}: duplicate or unsupported Stage 03 dataset {dataset!r}")
        by_dataset[str(dataset)] = item
    if set(by_dataset) != set(STAGE03_DATASETS):
        raise ValueError(f"{label}: Stage 03 output dataset set is incomplete")

    output_rows: dict[str, int] = {}
    for dataset in STAGE03_DATASETS:
        item = by_dataset[dataset]
        expected_filename = expected_stage03_filename(dataset, scope, monthly_input.period)
        if item.get("month") != monthly_input.period:
            raise ValueError(f"{label}: {dataset} output month mismatch")
        if item.get("filename") != expected_filename:
            raise ValueError(f"{label}: {dataset} output filename mismatch")
        recorded_path = item.get("path")
        if not isinstance(recorded_path, str) or Path(recorded_path).name != expected_filename:
            raise ValueError(f"{label}: {dataset} output path identity mismatch")
        rows = require_json_int(item.get("rows"), f"{label} {dataset}.rows", minimum=1)
        output_rows[dataset] = rows
        require_sha256(item.get("sha256"), f"{label} {dataset}.sha256")

    monthly_evidence = by_dataset["monthly"]
    if Path(str(monthly_evidence["path"])).resolve() != monthly_snapshot.path:
        raise ValueError(f"{label}: monthly output path does not match the selected CSV")
    if monthly_evidence.get("rows") != len(monthly_snapshot.frame):
        raise ValueError(f"{label}: monthly output row count does not match the selected CSV")
    if monthly_evidence.get("sha256") != monthly_snapshot.sha256:
        raise ValueError(f"{label}: monthly output SHA-256 does not match the selected CSV")

    atomic_file = payload.get("atomicFile")
    if not isinstance(atomic_file, Mapping):
        raise ValueError(f"{label}: missing Atomic source evidence")
    atomic_rows = require_json_int(atomic_file.get("rows"), f"{label} atomicFile.rows", minimum=1)
    atomic_filename = atomic_file.get("filename")
    if not isinstance(atomic_filename, str):
        raise ValueError(f"{label}: invalid Atomic filename evidence")
    atomic_pattern = re.compile(
        rf"^atomic__conlog_prepaid_sales__{re.escape(config.lm_pcode)}__"
        rf"{re.escape(monthly_input.period)}__(?P<rows>\d+)\.csv$"
    )
    atomic_match = atomic_pattern.fullmatch(atomic_filename)
    if not atomic_match or int(atomic_match.group("rows")) != atomic_rows:
        raise ValueError(f"{label}: Atomic filename/row evidence mismatch")
    if atomic_file.get("month") != monthly_input.period:
        raise ValueError(f"{label}: Atomic month evidence mismatch")
    atomic_path = atomic_file.get("path")
    if not isinstance(atomic_path, str) or Path(atomic_path).name != atomic_filename:
        raise ValueError(f"{label}: Atomic path evidence mismatch")
    require_sha256(atomic_file.get("sha256"), f"{label} atomicFile.sha256")

    if require_json_int(payload.get("atomicRows"), f"{label} atomicRows", minimum=1) != atomic_rows:
        raise ValueError(f"{label}: atomicRows does not match atomicFile.rows")
    if atomic_rows != reconciliation["purchasesCount"]:
        raise ValueError(f"{label}: Atomic row count does not reconcile to monthly purchases")
    if require_json_int(payload.get("atomicUniqueMeters"), f"{label} atomicUniqueMeters", minimum=1) != reconciliation["metersCount"]:
        raise ValueError(f"{label}: Atomic unique meters do not reconcile to monthly rows")
    if require_json_int(payload.get("monthlyRows"), f"{label} monthlyRows", minimum=1) != output_rows["monthly"]:
        raise ValueError(f"{label}: monthlyRows does not match monthly output")
    if require_json_int(payload.get("monthlyLmRows"), f"{label} monthlyLmRows", minimum=1) != output_rows["monthly_lm"] or output_rows["monthly_lm"] != 1:
        raise ValueError(f"{label}: monthly LM output must contain exactly one row")
    if require_json_int(payload.get("monthlyLmGroupRows"), f"{label} monthlyLmGroupRows", minimum=1) != output_rows["monthly_lm_groups"]:
        raise ValueError(f"{label}: monthly group row count mismatch")

    write_summary = payload.get("writeSummary")
    if not isinstance(write_summary, Mapping):
        raise ValueError(f"{label}: missing Stage 03 write summary")
    written = require_json_int(write_summary.get("written"), f"{label} writeSummary.written", minimum=0)
    unchanged = require_json_int(write_summary.get("unchanged"), f"{label} writeSummary.unchanged", minimum=0)
    if written + unchanged != 3:
        raise ValueError(f"{label}: Stage 03 write summary does not cover all three outputs")


def stage03_source_identity(manifest: JsonSnapshot) -> str:
    atomic = manifest.payload["atomicFile"]
    reconciliation = manifest.payload["reconciliation"]
    outputs = sorted(
        (
            {
                "dataset": item["dataset"],
                "filename": item["filename"],
                "rows": item["rows"],
                "sha256": item["sha256"],
            }
            for item in manifest.payload["outputs"]
        ),
        key=lambda item: item["dataset"],
    )
    contract = {
        "atomicFilename": atomic["filename"],
        "atomicRows": atomic["rows"],
        "atomicSha256": atomic["sha256"],
        "reconciliation": reconciliation,
        "outputs": outputs,
    }
    return canonical_json_sha256(contract)


def approve_monthly_input(
    monthly_input: MonthlyInput,
    *,
    manifest_dir: Path,
    config: BuildConfig,
    scope: str,
) -> ApprovedMonthlyInput:
    csv_snapshot = read_csv_snapshot(monthly_input.path, "monthly input")
    frame, reconciliation = validate_monthly_snapshot(
        monthly_input,
        csv_snapshot,
        config,
    )
    pattern = (
        f"stage03_monthly_build__{config.lm_pcode}__"
        f"{monthly_input.period}__*.json"
    )
    candidates = sorted(manifest_dir.glob(pattern))
    if not candidates:
        raise FileNotFoundError(
            f"No Stage 03 manifest found for {config.lm_pcode}/{monthly_input.period} "
            f"in {manifest_dir}"
        )

    valid: list[JsonSnapshot] = []
    errors: list[str] = []
    for path in candidates:
        try:
            manifest = read_json_snapshot(path, "Stage 03 manifest")
            validate_stage03_manifest(
                manifest,
                monthly_input=monthly_input,
                monthly_snapshot=csv_snapshot,
                reconciliation=reconciliation,
                config=config,
                scope=scope,
            )
            valid.append(manifest)
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
    if not valid:
        detail = "; ".join(errors[:3])
        raise ValueError(
            f"No successful matching Stage 03 manifest for "
            f"{config.lm_pcode}/{monthly_input.period}. {detail}"
        )

    identities = {stage03_source_identity(item) for item in valid}
    if len(identities) != 1:
        raise ValueError(
            f"Ambiguous Stage 03 source evidence for {config.lm_pcode}/"
            f"{monthly_input.period}: {len(identities)} different valid Atomic contracts"
        )
    selected = max(valid, key=lambda item: item.path.name)
    return ApprovedMonthlyInput(
        period=monthly_input.period,
        csv=csv_snapshot,
        frame=frame,
        reconciliation=reconciliation,
        stage03_manifest=selected,
    )


def approve_monthly_inputs(
    monthly_inputs: Sequence[MonthlyInput],
    *,
    manifest_dir: Path,
    config: BuildConfig,
    scope: str,
) -> list[ApprovedMonthlyInput]:
    if not manifest_dir.is_dir():
        raise FileNotFoundError(f"Stage 03 manifest directory not found: {manifest_dir}")
    return [
        approve_monthly_input(
            item,
            manifest_dir=manifest_dir,
            config=config,
            scope=scope,
        )
        for item in monthly_inputs
    ]


def merge_duplicate_reference_record(
    existing: Dict[str, Any],
    candidate: Dict[str, Any],
    *,
    source: Path,
    row_number: int,
    normalized: str,
    conflict_fields: Sequence[str],
) -> None:
    """Merge a duplicate reference row only when populated values do not conflict."""
    for field in conflict_fields:
        current = safe_str(existing.get(field))
        incoming = safe_str(candidate.get(field))
        if current and incoming and current != incoming:
            raise ValueError(
                f"{source} contains conflicting duplicate meter {normalized!r}: "
                f"{field} is {current!r} and {incoming!r} (conflict at row {row_number})."
            )
        if not current and incoming:
            existing[field] = incoming

    if not safe_str(existing.get("meterNoRaw")) and safe_str(candidate.get("meterNoRaw")):
        existing["meterNoRaw"] = safe_str(candidate.get("meterNoRaw"))


def customer_identity_pattern(record: Dict[str, Any], normalized: str) -> str:
    """Classify a Customer Details identity using the approved Lesedi source pattern."""
    customer_no = safe_str(record.get("customerNo"))
    account_no = safe_str(record.get("accountNo"))

    if customer_no and account_no and customer_no == account_no:
        if customer_no == normalized:
            return "METER_EQUALS_CUSTOMER_AND_ACCOUNT"
        return "CUSTOMER_EQUALS_ACCOUNT_NOT_METER"

    if not customer_no or not account_no:
        return "INCOMPLETE"

    return "MIXED_OR_OTHER"


def parse_customer_purchase_date(value: Any) -> Optional[pd.Timestamp]:
    """Parse one Customer Details purchase timestamp, returning None for blank or invalid values."""
    text = safe_str(value)
    if not text:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed)


def replace_customer_record(existing: Dict[str, Any], candidate: Dict[str, Any]) -> None:
    """Replace the stored duplicate candidate while preserving the normalized map key."""
    existing.clear()
    existing.update(candidate)


def merge_customer_duplicate_record(
    existing: Dict[str, Any],
    candidate: Dict[str, Any],
    *,
    source: Path,
    row_number: int,
    normalized: str,
) -> Optional[str]:
    """
    Resolve one duplicate Customer Details row.

    Returns:
    - ``"placeholder"`` when a dominant identity replaces or defeats a meter-number placeholder;
    - ``"active_status"`` when an Active row replaces a Block Purchases row;
    - ``"latest_purchase"`` when two dominant identities resolve by latest valid purchase;
    - ``None`` for identical or complementary duplicates.

    ERF, address, customer name, and other descriptive fields are deliberately excluded
    from duplicate resolution because they are not part of the locked Meter Master schema.
    Competing dominant identities remain a hard error when both purchase dates are
    missing/invalid or when the dates tie.
    """
    conflict_fields = [
        field
        for field in ("customerNo", "accountNo")
        if safe_str(existing.get(field))
        and safe_str(candidate.get(field))
        and safe_str(existing.get(field)) != safe_str(candidate.get(field))
    ]

    if not conflict_fields:
        merge_duplicate_reference_record(
            existing,
            candidate,
            source=source,
            row_number=row_number,
            normalized=normalized,
            conflict_fields=["customerNo", "accountNo"],
        )
        existing_date = parse_customer_purchase_date(existing.get("lastPurchaseDate"))
        candidate_date = parse_customer_purchase_date(candidate.get("lastPurchaseDate"))
        if candidate_date is not None and (existing_date is None or candidate_date > existing_date):
            existing["lastPurchaseDate"] = safe_str(candidate.get("lastPurchaseDate"))
            existing["sourceRow"] = row_number
        return None

    existing_pattern = customer_identity_pattern(existing, normalized)
    candidate_pattern = customer_identity_pattern(candidate, normalized)
    dominant = "CUSTOMER_EQUALS_ACCOUNT_NOT_METER"
    placeholder = "METER_EQUALS_CUSTOMER_AND_ACCOUNT"

    # Resolve the approved source-identity pattern before considering status or
    # descriptive fields. A meter-number placeholder is weaker than a row where
    # CustomerNo = AccountNo and that identity differs from the meter number.
    # Placeholder ERF/address data may be stale and is not part of Meter Master.
    if existing_pattern == placeholder and candidate_pattern == dominant:
        replace_customer_record(existing, candidate)
        return "placeholder"

    if existing_pattern == dominant and candidate_pattern == placeholder:
        return "placeholder"

    existing_status = safe_str(existing.get("accountStatus")).upper()
    candidate_status = safe_str(candidate.get("accountStatus")).upper()
    status_pair = {existing_status, candidate_status}

    if status_pair == {"ACTIVE", "BLOCK PURCHASES"}:
        # AccountStatus is used only as an identity-resolution signal.
        # ERF, address, customer name, and purchase-date equality are not checked here
        # because those descriptive fields are outside the Meter Master contract.
        if existing_status != "ACTIVE":
            replace_customer_record(existing, candidate)
        return "active_status"

    if existing_pattern == dominant and candidate_pattern == dominant:
        existing_date = parse_customer_purchase_date(existing.get("lastPurchaseDate"))
        candidate_date = parse_customer_purchase_date(candidate.get("lastPurchaseDate"))
        existing_row = safe_str(existing.get("sourceRow")) or "unknown"

        if existing_date is None and candidate_date is None:
            raise ValueError(
                f"{source} contains unresolved duplicate meter {normalized!r}. "
                f"Rows {existing_row} and {row_number} both match the dominant customer/account pattern, "
                "but neither has a valid LastPurchaseDate."
            )
        if existing_date is not None and candidate_date is not None and existing_date == candidate_date:
            raise ValueError(
                f"{source} contains unresolved duplicate meter {normalized!r}. "
                f"Rows {existing_row} and {row_number} both match the dominant customer/account pattern "
                f"and share the same LastPurchaseDate {existing_date.isoformat()!r}."
            )

        if candidate_date is not None and (existing_date is None or candidate_date > existing_date):
            replace_customer_record(existing, candidate)
        return "latest_purchase"

    existing_row = safe_str(existing.get("sourceRow")) or "unknown"
    raise ValueError(
        f"{source} contains unresolved duplicate meter {normalized!r}. "
        f"Existing row {existing_row}: customerNo={safe_str(existing.get('customerNo'))!r}, "
        f"accountNo={safe_str(existing.get('accountNo'))!r}, pattern={existing_pattern}; "
        f"row {row_number}: customerNo={safe_str(candidate.get('customerNo'))!r}, "
        f"accountNo={safe_str(candidate.get('accountNo'))!r}, pattern={candidate_pattern}. "
        "Only the approved placeholder rule or latest-purchase rule may resolve duplicates."
    )


def make_base_master_record(config: BuildConfig) -> Dict[str, Any]:
    return {
        "masterId": "",
        "lmPcode": config.lm_pcode,
        "meterNoRaw": "",
        "meterNoNormalized": "",
        "meterType": config.meter_type,
        "customerNo": "",
        "accountNo": "",
        "salesId": "",
        "salesProvider": config.provider,
        "astId": "",
        "sources": {"monthly": False, "customer": False, "npr": False},
    }


def choose_best_raw_meter_no(
    current_raw: Optional[str],
    candidate_raw: Optional[str],
    source_name: str,
) -> str:
    current = safe_str(current_raw)
    candidate = safe_str(candidate_raw)

    if not candidate:
        return current
    if not current:
        return candidate
    if source_name == "customer":
        return candidate
    return current


def load_monthly_meter_universe(
    monthly_inputs: Iterable[ApprovedMonthlyInput],
    config: BuildConfig,
) -> MasterMap:
    master_map: MasterMap = {}

    for monthly_input in monthly_inputs:
        df = monthly_input.frame

        lm_values = df["lmPcode"].map(safe_str).str.upper()
        wrong_lm = lm_values != config.lm_pcode
        if wrong_lm.any():
            examples = sorted(set(lm_values[wrong_lm].head(10).tolist()))
            raise ValueError(
                f"{monthly_input.path} contains {int(wrong_lm.sum())} row(s) outside "
                f"LM {config.lm_pcode}. Examples: {examples}"
            )

        ym_values = df["ym"].map(safe_str)
        wrong_month = ym_values != monthly_input.period
        if wrong_month.any():
            examples = sorted(set(ym_values[wrong_month].head(10).tolist()))
            raise ValueError(
                f"{monthly_input.path} contains {int(wrong_month.sum())} row(s) whose ym "
                f"does not match filename month {monthly_input.period}. Examples: {examples}"
            )

        for index, row in df.iterrows():
            meter_no_raw = row.get("meterNo")
            normalized = validate_meter_no(
                meter_no_raw, monthly_input.path, int(index) + 2
            )

            rec = master_map.setdefault(normalized, make_base_master_record(config))
            rec["meterNoRaw"] = choose_best_raw_meter_no(
                rec.get("meterNoRaw"), meter_no_raw, "monthly"
            )
            rec["meterNoNormalized"] = normalized
            rec["sources"]["monthly"] = True

    return master_map


def load_customer_details(snapshot: CsvSnapshot, stats: BuildStats) -> CustomerMap:
    filepath = snapshot.path
    df = snapshot.frame
    require_columns(
        df,
        [
            "MeterNumber",
            "CustomerNo",
            "AccountNo",
            "AccountStatus",
            "LastPurchaseDate",
        ],
        filepath,
    )

    customer_map: CustomerMap = {}
    for index, row in df.iterrows():
        row_number = int(index) + 2
        normalized = validate_meter_no(row.get("MeterNumber"), filepath, row_number)
        candidate = {
            "meterNoRaw": safe_str(row.get("MeterNumber")),
            "customerNo": safe_str(row.get("CustomerNo")),
            "accountNo": safe_str(row.get("AccountNo")),
            "lastPurchaseDate": safe_str(row.get("LastPurchaseDate")),
            "accountStatus": safe_str(row.get("AccountStatus")),
            "sourceRow": row_number,
        }

        existing = customer_map.get(normalized)
        if existing is None:
            customer_map[normalized] = candidate
        else:
            resolution = merge_customer_duplicate_record(
                existing,
                candidate,
                source=filepath,
                row_number=row_number,
                normalized=normalized,
            )
            if resolution == "placeholder":
                stats.customer_placeholder_duplicates_resolved += 1
            elif resolution == "active_status":
                stats.customer_active_status_duplicates_resolved += 1
            elif resolution == "latest_purchase":
                stats.customer_latest_purchase_duplicates_resolved += 1

    return customer_map


def merge_npr_duplicate_record(
    existing: Dict[str, Any],
    candidate: Dict[str, Any],
    *,
    source: Path,
    row_number: int,
    normalized: str,
) -> Optional[str]:
    """Resolve one duplicate 90-day report row using governed identity evidence.

    Returns:
    - ``"placeholder"`` when a real customer number replaces or defeats a
      customer number equal to the meter number;
    - ``"latest_purchase"`` when two different real customer numbers resolve
      by the latest valid LastPurchaseDate;
    - ``None`` for identical or complementary duplicates.

    Two different non-placeholder customer numbers remain a hard error when
    both purchase dates are missing/invalid or when the dates tie.
    """
    current_customer = safe_str(existing.get("customerNo"))
    incoming_customer = safe_str(candidate.get("customerNo"))

    # A blank NPR identity carries no authority to replace or rank a populated
    # customer number, including the governed meter-number placeholder.
    if current_customer and not incoming_customer:
        return None

    if not current_customer or current_customer == incoming_customer:
        merge_duplicate_reference_record(
            existing,
            candidate,
            source=source,
            row_number=row_number,
            normalized=normalized,
            conflict_fields=["customerNo"],
        )
        existing_date = parse_customer_purchase_date(existing.get("lastPurchaseDate"))
        candidate_date = parse_customer_purchase_date(candidate.get("lastPurchaseDate"))
        if candidate_date is not None and (existing_date is None or candidate_date > existing_date):
            existing["lastPurchaseDate"] = safe_str(candidate.get("lastPurchaseDate"))
            existing["sourceRow"] = row_number
        return None

    existing_placeholder = current_customer == normalized
    candidate_placeholder = incoming_customer == normalized

    if existing_placeholder and not candidate_placeholder:
        replace_customer_record(existing, candidate)
        return "placeholder"

    if not existing_placeholder and candidate_placeholder:
        return "placeholder"

    existing_date = parse_customer_purchase_date(existing.get("lastPurchaseDate"))
    candidate_date = parse_customer_purchase_date(candidate.get("lastPurchaseDate"))
    existing_row = safe_str(existing.get("sourceRow")) or "unknown"

    if existing_date is None and candidate_date is None:
        raise ValueError(
            f"{source} contains unresolved duplicate meter {normalized!r}. "
            f"Rows {existing_row} and {row_number} contain different non-placeholder "
            "CustomerNo1 values, but neither has a valid LastPurchaseDate."
        )
    if existing_date is not None and candidate_date is not None and existing_date == candidate_date:
        raise ValueError(
            f"{source} contains unresolved duplicate meter {normalized!r}. "
            f"Rows {existing_row} and {row_number} contain different non-placeholder "
            f"CustomerNo1 values and share the same LastPurchaseDate {existing_date.isoformat()!r}."
        )

    if candidate_date is not None and (existing_date is None or candidate_date > existing_date):
        replace_customer_record(existing, candidate)
    return "latest_purchase"


def load_npr(snapshot: CsvSnapshot, stats: BuildStats) -> NprMap:
    filepath = snapshot.path
    df = snapshot.frame
    require_columns(
        df,
        ["MeterIdentifier", "CustomerNo1", "LastPurchaseDate"],
        filepath,
    )

    npr_map: NprMap = {}
    for index, row in df.iterrows():
        row_number = int(index) + 2
        normalized = validate_meter_no(row.get("MeterIdentifier"), filepath, row_number)
        candidate = {
            "meterNoRaw": safe_str(row.get("MeterIdentifier")),
            "customerNo": safe_str(row.get("CustomerNo1")),
            "lastPurchaseDate": safe_str(row.get("LastPurchaseDate")),
            "sourceRow": row_number,
        }

        existing = npr_map.get(normalized)
        if existing is None:
            npr_map[normalized] = candidate
        else:
            resolution = merge_npr_duplicate_record(
                existing,
                candidate,
                source=filepath,
                row_number=row_number,
                normalized=normalized,
            )
            if resolution == "placeholder":
                stats.npr_placeholder_duplicates_resolved += 1
            elif resolution == "latest_purchase":
                stats.npr_latest_purchase_duplicates_resolved += 1

    return npr_map


def merge_customer_details(
    master_map: MasterMap,
    customer_map: CustomerMap,
    config: BuildConfig,
    stats: BuildStats,
) -> MasterMap:
    for normalized, customer_rec in customer_map.items():
        rec = master_map.get(normalized)
        if rec is None:
            rec = make_base_master_record(config)
            rec["meterNoNormalized"] = normalized
            master_map[normalized] = rec
            stats.customer_only_seeded_meters += 1

        rec["meterNoRaw"] = choose_best_raw_meter_no(
            rec.get("meterNoRaw"), customer_rec.get("meterNoRaw"), "customer"
        )
        if customer_rec.get("customerNo"):
            rec["customerNo"] = customer_rec["customerNo"]
        if customer_rec.get("accountNo"):
            rec["accountNo"] = customer_rec["accountNo"]
        rec["sources"]["customer"] = True

    return master_map


def merge_npr(
    master_map: MasterMap,
    npr_map: NprMap,
    config: BuildConfig,
    stats: BuildStats,
) -> MasterMap:
    for normalized, npr_rec in npr_map.items():
        rec = master_map.get(normalized)
        if rec is None:
            rec = make_base_master_record(config)
            rec["meterNoNormalized"] = normalized
            master_map[normalized] = rec
            stats.npr_only_seeded_meters += 1

        rec["meterNoRaw"] = choose_best_raw_meter_no(
            rec.get("meterNoRaw"), npr_rec.get("meterNoRaw"), "npr"
        )
        if not rec.get("customerNo") and npr_rec.get("customerNo"):
            rec["customerNo"] = npr_rec["customerNo"]
        rec["sources"]["npr"] = True

    return master_map


def finalize_master_records(
    master_map: MasterMap,
    config: BuildConfig,
) -> MasterMap:
    for normalized, rec in master_map.items():
        rec["masterId"] = normalized
        rec["lmPcode"] = config.lm_pcode
        rec["meterNoNormalized"] = normalized
        rec["salesId"] = normalized
        rec["salesProvider"] = config.provider
        rec["meterType"] = safe_str(rec.get("meterType")) or config.meter_type
        rec["astId"] = safe_str(rec.get("astId"))
        rec["meterNoRaw"] = safe_str(rec.get("meterNoRaw")) or normalized
        rec["customerNo"] = safe_str(rec.get("customerNo"))
        rec["accountNo"] = safe_str(rec.get("accountNo"))
    return master_map


def master_rows_to_dataframe(master_map: MasterMap) -> pd.DataFrame:
    rows = [
        {column: safe_str(record.get(column)) for column in MASTER_COLUMNS}
        for record in master_map.values()
    ]
    if not rows:
        return pd.DataFrame(columns=MASTER_COLUMNS)

    return (
        pd.DataFrame(rows)[MASTER_COLUMNS]
        .sort_values(
            by=["meterNoNormalized", "meterNoRaw"],
            ascending=[True, True],
            kind="stable",
        )
        .reset_index(drop=True)
    )


def resolve_output_path(
    explicit_output: Optional[Path],
    output_dir: Path,
    lm_pcode: str,
    scope: str,
    first_month: str,
    last_month: str,
) -> Path:
    if explicit_output:
        return explicit_output
    return output_dir / (
        f"meter_master__{lm_pcode}__{scope}__{first_month}_to_{last_month}.csv"
    )


def validate_staging_dataframe(df: pd.DataFrame, config: BuildConfig) -> None:
    actual_columns = list(df.columns)
    if actual_columns != MASTER_COLUMNS:
        raise ValueError(
            f"Meter Master output columns do not match the approved contract: {actual_columns}"
        )
    if df.empty:
        raise ValueError("Meter Master build produced zero rows.")
    if df["masterId"].duplicated().any():
        examples = df.loc[df["masterId"].duplicated(keep=False), "masterId"].head(10).tolist()
        raise ValueError(f"Meter Master output contains duplicate masterId values: {examples}")
    if not df["masterId"].map(lambda value: bool(METER_NO_RE.fullmatch(safe_str(value)))).all():
        raise ValueError("Meter Master output contains invalid masterId characters.")
    if not (df["masterId"] == df["meterNoNormalized"]).all():
        raise ValueError("Meter Master identity failure: masterId must equal meterNoNormalized.")
    if not (df["salesId"] == df["meterNoNormalized"]).all():
        raise ValueError("Meter Master identity failure: salesId must equal meterNoNormalized.")
    if not (df["lmPcode"] == config.lm_pcode).all():
        raise ValueError("Meter Master output contains an unexpected lmPcode.")
    if not (df["salesProvider"] == config.provider).all():
        raise ValueError("Meter Master output contains an unexpected salesProvider.")


def utc_iso_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def canonical_json_sha256(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_manifest_path(output_path: Path) -> Path:
    return output_path.with_suffix(".manifest.json")


def build_manifest(
    *,
    config: BuildConfig,
    scope: str,
    from_month: str,
    to_month: str,
    included_months: Sequence[str],
    monthly_inputs: Sequence[ApprovedMonthlyInput],
    customer_details: CsvSnapshot,
    npr: CsvSnapshot,
    output: CsvSnapshot,
    output_rows: int,
    stats: BuildStats,
) -> Dict[str, Any]:
    monthly_evidence: list[Dict[str, Any]] = []
    for item in monthly_inputs:
        stage03_atomic = item.stage03_manifest.payload["atomicFile"]
        monthly_evidence.append(
            {
                "month": item.period,
                "path": str(item.path),
                "filename": item.path.name,
                "rows": len(item.frame),
                "columns": list(MONTHLY_COLUMNS),
                "sha256": item.csv.sha256,
                "stage03Manifest": {
                    "path": str(item.stage03_manifest.path),
                    "filename": item.stage03_manifest.path.name,
                    "sha256": item.stage03_manifest.sha256,
                    "atomicFile": {
                        "filename": stage03_atomic["filename"],
                        "rows": stage03_atomic["rows"],
                        "sha256": stage03_atomic["sha256"],
                    },
                    "reconciliation": dict(item.reconciliation),
                },
            }
        )
    source_contract: Dict[str, Any] = {
        "lmPcode": config.lm_pcode,
        "scope": scope,
        "fromMonth": from_month,
        "toMonth": to_month,
        "includedMonths": list(included_months),
        "provider": config.provider,
        "meterType": config.meter_type,
        "monthlyInputs": monthly_evidence,
        "customerDetails": {
            "path": str(customer_details.path),
            "filename": customer_details.path.name,
            "rows": len(customer_details.frame),
            "sha256": customer_details.sha256,
        },
        "npr": {
            "path": str(npr.path),
            "filename": npr.path.name,
            "rows": len(npr.frame),
            "sha256": npr.sha256,
        },
    }
    output_contract: Dict[str, Any] = {
        "path": str(output.path),
        "filename": output.path.name,
        "rows": output_rows,
        "columns": list(MASTER_COLUMNS),
        "sha256": output.sha256,
    }
    stats_payload = {
        "monthlyBackedMeters": stats.monthly_backed_meters,
        "customerOnlySeededMeters": stats.customer_only_seeded_meters,
        "nprOnlySeededMeters": stats.npr_only_seeded_meters,
        "customerPlaceholderDuplicatesResolved": stats.customer_placeholder_duplicates_resolved,
        "customerActiveStatusDuplicatesResolved": stats.customer_active_status_duplicates_resolved,
        "customerLatestPurchaseDuplicatesResolved": stats.customer_latest_purchase_duplicates_resolved,
        "nprPlaceholderDuplicatesResolved": stats.npr_placeholder_duplicates_resolved,
        "nprLatestPurchaseDuplicatesResolved": stats.npr_latest_purchase_duplicates_resolved,
        "totalMasterRows": stats.total_master_rows,
    }
    fingerprint_contract = {
        "lmPcode": config.lm_pcode,
        "scope": scope,
        "fromMonth": from_month,
        "toMonth": to_month,
        "includedMonths": list(included_months),
        "provider": config.provider,
        "meterType": config.meter_type,
        "monthlyInputs": [
            {
                "month": item["month"],
                "filename": item["filename"],
                "rows": item["rows"],
                "columns": item["columns"],
                "sha256": item["sha256"],
                "stage03Manifest": {
                    "filename": item["stage03Manifest"]["filename"],
                    "sha256": item["stage03Manifest"]["sha256"],
                    "atomicFile": item["stage03Manifest"]["atomicFile"],
                    "reconciliation": item["stage03Manifest"]["reconciliation"],
                },
            }
            for item in monthly_evidence
        ],
        "customerDetails": {
            "filename": source_contract["customerDetails"]["filename"],
            "rows": source_contract["customerDetails"]["rows"],
            "sha256": source_contract["customerDetails"]["sha256"],
        },
        "npr": {
            "filename": source_contract["npr"]["filename"],
            "rows": source_contract["npr"]["rows"],
            "sha256": source_contract["npr"]["sha256"],
        },
        "output": {
            "filename": output_contract["filename"],
            "rows": output_contract["rows"],
            "columns": output_contract["columns"],
            "sha256": output_contract["sha256"],
        },
        "stats": stats_payload,
    }
    return {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "stage": "05",
        "script": "05_build_meter_master_v3.py",
        "status": "PASS",
        "result": "BUILD_WRITTEN",
        "createdAt": utc_iso_now(),
        "sourceContract": source_contract,
        "outputContract": output_contract,
        "stats": stats_payload,
        "buildFingerprint": canonical_json_sha256(fingerprint_contract),
    }


def write_json(payload: Dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(output_path.name + ".tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as target:
            json.dump(payload, target, indent=2, sort_keys=True, ensure_ascii=False)
            target.write("\n")
        temp_path.replace(output_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def write_csv(df: pd.DataFrame, output_path: Path) -> CsvSnapshot:
    """Write through a temporary file so a failed write cannot leave partial output."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(output_path.name + ".tmp")
    payload = df.to_csv(index=False, lineterminator="\n").encode("utf-8")
    expected_sha256 = sha256_bytes(payload)
    try:
        temp_path.write_bytes(payload)
        if sha256_bytes(temp_path.read_bytes()) != expected_sha256:
            raise ValueError(f"Temporary Meter Master SHA-256 mismatch: {output_path}")
        temp_path.replace(output_path)
        output = read_csv_snapshot(output_path, "Meter Master output")
        if output.sha256 != expected_sha256:
            raise ValueError(f"Written Meter Master SHA-256 mismatch: {output_path}")
        if list(output.frame.columns) != MASTER_COLUMNS or len(output.frame) != len(df):
            raise ValueError(f"Written Meter Master CSV does not match the planned output: {output_path}")
        return output
    finally:
        if temp_path.exists():
            temp_path.unlink()


def main() -> None:
    args = parse_args()
    validate_month(args.from_month, "--from-month")
    validate_month(args.to_month, "--to-month")
    if args.from_month > args.to_month:
        raise ValueError("--from-month cannot be later than --to-month")

    config = BuildConfig(
        lm_pcode=safe_str(args.lm_pcode).upper(),
        provider=safe_str(args.provider).lower(),
        meter_type=safe_str(args.meter_type).lower(),
    )
    if not config.lm_pcode:
        raise ValueError("--lm-pcode may not be blank.")
    if not re.fullmatch(r"[A-Z0-9_-]+", config.lm_pcode):
        raise ValueError("--lm-pcode may contain only A-Z, 0-9, underscore, and hyphen.")
    if config.provider != GOVERNED_PROVIDER:
        raise ValueError(
            f"Stage 05 is currently governed only for provider={GOVERNED_PROVIDER!r}."
        )
    if config.meter_type != GOVERNED_METER_TYPE:
        raise ValueError(
            f"Stage 05 is currently governed only for meterType={GOVERNED_METER_TYPE!r}."
        )

    monthly_dir = resolve_project_path(args.monthly_dir)
    stage03_manifest_dir = resolve_project_path(args.stage03_manifest_dir)
    customer_details_path = resolve_project_path(args.customer_details)
    npr_path = resolve_project_path(args.npr)
    output_dir = resolve_project_path(args.output_dir)
    explicit_output = (
        resolve_project_path(args.output) if args.output is not None else None
    )

    require_file(customer_details_path)
    require_file(npr_path)

    monthly_inputs = discover_monthly_inputs(
        monthly_dir,
        args.scope,
        args.from_month,
        args.to_month,
    )
    included_months = validate_month_continuity(
        monthly_inputs,
        args.from_month,
        args.to_month,
    )
    first_month = args.from_month
    last_month = args.to_month
    approved_monthly_inputs = approve_monthly_inputs(
        monthly_inputs,
        manifest_dir=stage03_manifest_dir,
        config=config,
        scope=args.scope,
    )
    customer_details = read_csv_snapshot(customer_details_path, "Customer Details")
    npr = read_csv_snapshot(npr_path, "90 Days No Purchase report")

    output_path = resolve_output_path(
        explicit_output,
        output_dir,
        config.lm_pcode,
        args.scope,
        first_month,
        last_month,
    )
    stats = BuildStats()

    print("=== METER MASTER BUILD ===")
    print(f"Repository root: {PROJECT_ROOT}")
    print(f"LM/workbase: {config.lm_pcode}")
    print(f"Provider: {config.provider}")
    print(f"Customer details: {customer_details_path}")
    print(f"90-day report: {npr_path}")
    print(f"Stage 03 manifests: {stage03_manifest_dir}")
    print(
        f"Months approved: {len(approved_monthly_inputs)} "
        f"({first_month} to {last_month})"
    )
    for item in approved_monthly_inputs:
        print(
            f"  - {item.period}: {item.path} "
            f"[{item.stage03_manifest.path.name}]"
        )

    master_map = load_monthly_meter_universe(approved_monthly_inputs, config)
    stats.monthly_backed_meters = len(master_map)

    customer_map = load_customer_details(customer_details, stats)
    npr_map = load_npr(npr, stats)
    merge_customer_details(master_map, customer_map, config, stats)
    merge_npr(master_map, npr_map, config, stats)
    finalize_master_records(master_map, config)

    df = master_rows_to_dataframe(master_map)
    validate_staging_dataframe(df, config)
    output = write_csv(df, output_path)
    stats.total_master_rows = len(df)
    manifest_path = resolve_manifest_path(output_path)
    manifest = build_manifest(
        config=config,
        scope=args.scope,
        from_month=first_month,
        to_month=last_month,
        included_months=included_months,
        monthly_inputs=approved_monthly_inputs,
        customer_details=customer_details,
        npr=npr,
        output=output,
        output_rows=len(df),
        stats=stats,
    )
    write_json(manifest, manifest_path)

    print("=== BUILD SUMMARY ===")
    print(f"Monthly-backed meters: {stats.monthly_backed_meters}")
    print(f"Customer-only seeded meters: {stats.customer_only_seeded_meters}")
    print(f"NPR-only seeded meters: {stats.npr_only_seeded_meters}")
    print(
        "Customer placeholder duplicates resolved: "
        f"{stats.customer_placeholder_duplicates_resolved}"
    )
    print(
        "Customer Active-status duplicates resolved: "
        f"{stats.customer_active_status_duplicates_resolved}"
    )
    print(
        "Customer latest-purchase duplicates resolved: "
        f"{stats.customer_latest_purchase_duplicates_resolved}"
    )
    print(
        "NPR placeholder duplicates resolved: "
        f"{stats.npr_placeholder_duplicates_resolved}"
    )
    print(
        "NPR latest-purchase duplicates resolved: "
        f"{stats.npr_latest_purchase_duplicates_resolved}"
    )
    print(f"Total meter master rows: {stats.total_master_rows}")
    print("Validation: PASS")
    print(f"Output: {output_path}")
    print(f"Output SHA-256: {manifest['outputContract']['sha256']}")
    print(f"Build fingerprint: {manifest['buildFingerprint']}")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
