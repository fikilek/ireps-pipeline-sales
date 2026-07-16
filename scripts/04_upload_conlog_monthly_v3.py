"""
Stage 04: safely upload one LM/month of the three Conlog Monthly Sales datasets.

Inputs are selected only from a successful Stage 03 BUILD_WRITTEN manifest.

Target collections:
    conlog_sales_monthly
    conlog_sales_monthly_lm
    conlog_sales_monthly_lm_groups

Safety model:
    - one Firebase project + one LM + one month per execution;
    - explicit target project, matching confirmation, and service-account path;
    - service-account project_id must match before Firebase starts;
    - Stage 03 manifest, exact CSV schemas, SHA-256, identities, and reconciliation;
    - Conlog provider document must exist and be active;
    - create-only for normal uploads;
    - resume only with a previous failed Stage 04 execute-upload report that proves
      the exact same project, LM, month, Stage 03 manifest, input SHA set, and planned IDs;
    - Firestore create operations only: no merge, update, delete, or silent skip;
    - all three target scopes are preflighted before any write;
    - post-upload counts and deterministic sample verification;
    - JSON audit report for every preflight or upload attempt.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = PROJECT_ROOT / "output" / "logs" / "monthly_upload"

COLL_MONTHLY = "conlog_sales_monthly"
COLL_MONTHLY_LM = "conlog_sales_monthly_lm"
COLL_MONTHLY_LM_GROUPS = "conlog_sales_monthly_lm_groups"

PROVIDER_COLLECTION = "vending_providers"
CONLOG_VENDING_PROVIDER_ID = "vpr_7f4d3c91a2b84e6f"
CONLOG_PROVIDER_CODE = "CONLOG"
BATCH_SIZE = 400
STAGE03_SCRIPT = "03_aggregate_monthly_from_atomic_outputs.py"
STAGE03_OPERATION = "build-write"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
ATOMIC_FILENAME_RE = re.compile(
    r"^atomic__conlog_prepaid_sales__"
    r"(?P<lm_pcode>[A-Za-z0-9_-]+)__"
    r"(?P<period>\d{4}-\d{2})__"
    r"(?P<rows>\d+)\.csv$"
)

ATOMIC_COLUMNS = [
    "atomicId",
    "vendingProviderId",
    "lmPcode",
    "meterNo",
    "txAtISO",
    "txAtMs",
    "ym",
    "y",
    "m",
    "amountTotalC",
    "costC",
    "vatC",
    "currency",
    "sourceFileId",
    "sourceRow",
    "ingestedAtISO",
    "ingestedAtMs",
]

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

MONTHLY_LM_COLUMNS = [
    "docId",
    "lmPcode",
    "ym",
    "y",
    "m",
    "purchasesCount",
    "metersCount",
    "amountTotalC",
    "costC",
    "vatC",
    "firstPurchaseAtISO",
    "lastPurchaseAtISO",
    "firstPurchaseAtMs",
    "lastPurchaseAtMs",
]

MONTHLY_LM_GROUP_COLUMNS = [
    "docId",
    "lmPcode",
    "ym",
    "y",
    "m",
    "salesGroupId",
    "salesGroupLabel",
    "metersCount",
    "purchasesCount",
    "amountTotalC",
    "costC",
    "vatC",
    "firstPurchaseAtISO",
    "lastPurchaseAtISO",
    "firstPurchaseAtMs",
    "lastPurchaseAtMs",
]

DOCUMENT_COLUMNS = {
    "monthly": [column for column in MONTHLY_COLUMNS if column != "docId"],
    "monthly_lm": [column for column in MONTHLY_LM_COLUMNS if column != "docId"],
    "monthly_lm_groups": [
        column for column in MONTHLY_LM_GROUP_COLUMNS if column != "docId"
    ],
}

COLLECTIONS = {
    "monthly": COLL_MONTHLY,
    "monthly_lm": COLL_MONTHLY_LM,
    "monthly_lm_groups": COLL_MONTHLY_LM_GROUPS,
}

EXPECTED_COLUMNS = {
    "monthly": MONTHLY_COLUMNS,
    "monthly_lm": MONTHLY_LM_COLUMNS,
    "monthly_lm_groups": MONTHLY_LM_GROUP_COLUMNS,
}

APPROVED_OUTPUT_LOCATIONS = {
    "monthly": ("monthly", "monthly__FULL__{month}__from_atomic.csv"),
    "monthly_lm": (
        "monthly_lm",
        "monthly_lm__FULL__{month}__from_atomic.csv",
    ),
    "monthly_lm_groups": (
        "monthly_lm_groups",
        "monthly_lm_groups__FULL__{month}__from_atomic.csv",
    ),
}

STAGE03_RECONCILIATION_FIELDS = [
    "lmPcode",
    "month",
    "purchasesCount",
    "metersCount",
    "amountTotalC",
    "costC",
    "vatC",
]


@dataclass(frozen=True)
class CredentialIdentity:
    path: Path
    project_id: str


@dataclass
class MonthlyDataset:
    dataset: str
    collection: str
    path: Path
    frame: pd.DataFrame
    file_sha256: str


@dataclass
class ExistingState:
    count: int
    matching: int
    missing: int
    conflicts: int
    extra: int
    missing_ids: list[str]
    conflict_examples: list[dict[str, Any]]
    extra_examples: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely upload one Conlog Monthly Sales LM/month to Firestore."
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--confirm-project", required=True)
    parser.add_argument("--service-account", required=True, type=Path)
    parser.add_argument("--lm-pcode", required=True)
    parser.add_argument("--month", required=True)
    parser.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="Successful Stage 03 BUILD_WRITTEN JSON manifest.",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("create-only", "resume"),
        help="create-only is normal; resume is only for verified partial-upload recovery.",
    )
    parser.add_argument(
        "--resume-report",
        type=Path,
        help=(
            "Required when --mode resume. Path to the previous failed Stage 04 "
            "execute-upload JSON report for the exact same source contract."
        ),
    )
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--preflight-only", action="store_true")
    operation.add_argument("--execute-upload", action="store_true")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument(
        "--vending-provider-id",
        default=CONLOG_VENDING_PROVIDER_ID,
    )
    return parser.parse_args()


def clean_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def normalize_meter_no(value: object) -> str:
    return "".join(clean_text(value).upper().split())


def validate_month(value: str) -> None:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError(f"--month must use YYYY-MM format: {value!r}")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def run_id(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def read_file_snapshot(path: Path, *, label: str) -> bytes:
    resolved = path.expanduser().resolve()
    try:
        return resolved.read_bytes()
    except FileNotFoundError as exc:
        raise ValueError(f"{label} is missing: {resolved}") from exc
    except OSError as exc:
        raise ValueError(f"Could not read {label}: {resolved}: {exc}") from exc


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return sha256_text(payload)


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.expanduser().resolve().open("r", encoding="utf-8") as source:
            payload = json.load(source)
    except FileNotFoundError as exc:
        raise ValueError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON file: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return payload


def read_json_snapshot(path: Path) -> tuple[dict[str, Any], str]:
    payload_bytes = read_file_snapshot(path, label="JSON file")
    try:
        payload = json.loads(payload_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON file: {path.expanduser().resolve()}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return payload, sha256_bytes(payload_bytes)


def require_json_int(
    value: object,
    *,
    label: str,
    minimum: Optional[int] = None,
) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be a JSON integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be at least {minimum}; found {value}")
    return value


def require_sha256(value: object, *, label: str) -> str:
    cleaned = clean_text(value)
    if SHA256_RE.fullmatch(cleaned) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 fingerprint")
    return cleaned


def parse_stage03_timestamp(value: object, *, label: str) -> dt.datetime:
    text = clean_text(value)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text):
        raise ValueError(f"{label} must be a UTC timestamp ending in Z")
    try:
        return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc
        )
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid UTC timestamp") from exc


def validate_project_identity(args: argparse.Namespace) -> CredentialIdentity:
    project_id = clean_text(args.project_id)
    confirm_project = clean_text(args.confirm_project)
    if not project_id:
        raise ValueError("--project-id cannot be blank")
    if confirm_project != project_id:
        raise ValueError(
            f"Project confirmation mismatch: {project_id!r} != {confirm_project!r}"
        )

    path = args.service_account.expanduser().resolve()
    payload = read_json(path)
    credential_project = clean_text(payload.get("project_id"))
    if not credential_project:
        raise ValueError(f"Service account contains no project_id: {path}")
    if credential_project != project_id:
        raise ValueError(
            f"Service-account project mismatch: requested={project_id!r}, "
            f"credential={credential_project!r}"
        )
    return CredentialIdentity(path=path, project_id=credential_project)


def read_csv_robust_bytes(payload: bytes, path: Path) -> pd.DataFrame:
    last_error: Optional[Exception] = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(payload), dtype=str, encoding=encoding)
        except Exception as exc:
            last_error = exc
    if last_error is None:
        raise RuntimeError(f"Unable to read CSV: {path}")
    raise last_error


def strict_integer_series(frame: pd.DataFrame, column: str) -> pd.Series:
    raw = frame[column].map(clean_text)
    invalid = ~raw.str.fullmatch(r"-?\d+")
    if invalid.any():
        examples = [int(index) + 2 for index in invalid[invalid].index[:5]]
        raise ValueError(
            f"{column} must contain integer text. Invalid CSV lines: {examples}"
        )
    return raw.astype("int64")


def sales_group_from_amount_total_c(total_cents: int) -> str:
    if total_cents <= 9_999:
        return "GR1"
    if total_cents <= 29_999:
        return "GR2"
    if total_cents <= 49_999:
        return "GR3"
    if total_cents <= 99_999:
        return "GR4"
    return "GR5"


def sales_group_label(group_id: str) -> str:
    return {
        "GR1": "<=99.99",
        "GR2": "100-299.99",
        "GR3": "300-499.99",
        "GR4": "500-999.99",
        "GR5": ">=1000",
    }[group_id]


def load_manifest_outputs(
    manifest_path: Path,
    *,
    expected_lm_pcode: str,
    expected_month: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
    manifest, manifest_sha256 = read_json_snapshot(manifest_path)
    if clean_text(manifest.get("stage")) != "03":
        raise ValueError("Manifest is not a Stage 03 report")
    if clean_text(manifest.get("script")) != STAGE03_SCRIPT:
        raise ValueError(
            f"Stage 03 manifest script must be {STAGE03_SCRIPT!r}"
        )
    if clean_text(manifest.get("operation")) != STAGE03_OPERATION:
        raise ValueError(
            f"Stage 03 manifest operation must be {STAGE03_OPERATION!r}"
        )
    if clean_text(manifest.get("status")) != "PASS":
        raise ValueError("Stage 03 manifest status is not PASS")
    if clean_text(manifest.get("result")) != "BUILD_WRITTEN":
        raise ValueError("Stage 03 manifest must have result BUILD_WRITTEN")
    if clean_text(manifest.get("lmPcode")) != expected_lm_pcode:
        raise ValueError("Stage 03 manifest LM does not match --lm-pcode")
    if clean_text(manifest.get("month")) != expected_month:
        raise ValueError("Stage 03 manifest month does not match --month")

    started_at = parse_stage03_timestamp(
        manifest.get("startedAt"), label="Stage 03 manifest startedAt"
    )
    finished_at = parse_stage03_timestamp(
        manifest.get("finishedAt"), label="Stage 03 manifest finishedAt"
    )
    if finished_at < started_at:
        raise ValueError("Stage 03 manifest finishedAt precedes startedAt")

    outputs = manifest.get("outputs")
    if not isinstance(outputs, list) or len(outputs) != len(COLLECTIONS):
        raise ValueError(
            "Stage 03 manifest must contain exactly three monthly output entries"
        )

    selected: dict[str, dict[str, Any]] = {}
    for position, item in enumerate(outputs):
        if not isinstance(item, dict):
            raise ValueError(
                f"Stage 03 manifest output entry {position} must be an object"
            )
        dataset = clean_text(item.get("dataset"))
        if dataset not in COLLECTIONS:
            raise ValueError(
                f"Stage 03 manifest contains an unapproved output dataset: {dataset!r}"
            )
        if dataset in selected:
            raise ValueError(
                f"Stage 03 manifest contains duplicate {dataset}/{expected_month} outputs"
            )
        if clean_text(item.get("month")) != expected_month:
            raise ValueError(
                f"Stage 03 manifest output {dataset} is not for {expected_month}"
            )

        directory, filename_template = APPROVED_OUTPUT_LOCATIONS[dataset]
        expected_path = (
            PROJECT_ROOT
            / "output"
            / directory
            / filename_template.format(month=expected_month)
        ).resolve()
        path_text = clean_text(item.get("path"))
        if not path_text:
            raise ValueError(f"Stage 03 manifest output {dataset} has no path")
        actual_path = Path(path_text).expanduser().resolve()
        if actual_path != expected_path:
            raise ValueError(
                f"Stage 03 manifest output {dataset} must use approved path "
                f"{expected_path}; found {actual_path}"
            )
        if clean_text(item.get("filename")) != expected_path.name:
            raise ValueError(
                f"Stage 03 manifest output {dataset} filename mismatch"
            )

        require_json_int(
            item.get("rows"),
            label=f"Stage 03 manifest output {dataset} rows",
            minimum=1,
        )
        output_sha = require_sha256(
            item.get("sha256"),
            label=f"Stage 03 manifest output {dataset} sha256",
        )
        existing_state = clean_text(item.get("existingState"))
        existing_sha = clean_text(item.get("existingSha256"))
        if existing_state not in {"MISSING", "IDENTICAL", "DIFFERENT"}:
            raise ValueError(
                f"Stage 03 manifest output {dataset} has invalid existingState"
            )
        if existing_state == "MISSING":
            if existing_sha:
                raise ValueError(
                    f"Stage 03 manifest output {dataset} was MISSING but has an existing SHA"
                )
        else:
            require_sha256(
                existing_sha,
                label=f"Stage 03 manifest output {dataset} existingSha256",
            )
            if existing_state == "IDENTICAL" and existing_sha != output_sha:
                raise ValueError(
                    f"Stage 03 manifest output {dataset} IDENTICAL SHA mismatch"
                )
            if existing_state == "DIFFERENT" and existing_sha == output_sha:
                raise ValueError(
                    f"Stage 03 manifest output {dataset} DIFFERENT SHA is identical"
                )

        selected[dataset] = item

    missing = sorted(set(COLLECTIONS) - set(selected))
    if missing:
        raise ValueError(
            f"Stage 03 manifest is missing monthly outputs for {missing}"
        )

    write_summary = manifest.get("writeSummary")
    if not isinstance(write_summary, dict):
        raise ValueError("Stage 03 manifest writeSummary must be an object")
    written = require_json_int(
        write_summary.get("written"),
        label="Stage 03 manifest writeSummary.written",
        minimum=0,
    )
    unchanged = require_json_int(
        write_summary.get("unchanged"),
        label="Stage 03 manifest writeSummary.unchanged",
        minimum=0,
    )
    if written + unchanged != len(COLLECTIONS):
        raise ValueError(
            "Stage 03 manifest writeSummary must account for exactly three outputs"
        )

    return manifest, selected, manifest_sha256


def validate_iso_ms_pair(
    frame: pd.DataFrame,
    *,
    iso_column: str,
    ms_column: str,
    dataset: str,
) -> None:
    parsed = pd.to_datetime(
        frame[iso_column],
        format="%Y-%m-%dT%H:%M:%SZ",
        errors="coerce",
        utc=True,
    )
    if parsed.isna().any():
        raise ValueError(f"{dataset}: invalid {iso_column}")
    expected = (parsed.astype("int64") // 1_000_000).astype("int64")
    if not frame[ms_column].eq(expected).all():
        raise ValueError(f"{dataset}: {ms_column} does not match {iso_column}")


def validate_dataset(
    dataset: str,
    manifest_entry: dict[str, Any],
    *,
    expected_lm_pcode: str,
    expected_month: str,
) -> MonthlyDataset:
    path = Path(clean_text(manifest_entry.get("path"))).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Stage 03 output is missing: {path}")

    expected_sha = require_sha256(
        manifest_entry.get("sha256"),
        label=f"Stage 03 output {dataset} sha256",
    )
    csv_payload = read_file_snapshot(path, label=f"Stage 03 output {dataset}")
    actual_sha = sha256_bytes(csv_payload)
    if actual_sha != expected_sha:
        raise ValueError(
            f"Stage 03 output SHA-256 mismatch for {path.name}: "
            f"manifest={expected_sha}, actual={actual_sha}"
        )

    # Parse the exact byte snapshot that was fingerprinted. Reopening the path here
    # would allow the accepted SHA and the rows prepared for upload to describe
    # different file versions if the file changed between operations.
    frame = read_csv_robust_bytes(csv_payload, path)
    expected_columns = EXPECTED_COLUMNS[dataset]
    if list(frame.columns) != expected_columns:
        raise ValueError(
            f"{dataset} schema mismatch. Expected {expected_columns}; "
            f"found {list(frame.columns)}"
        )
    if frame.empty:
        raise ValueError(f"{dataset} CSV is empty: {path}")

    string_columns = [
        "docId",
        "lmPcode",
        "ym",
        "firstPurchaseAtISO",
        "lastPurchaseAtISO",
    ]
    if dataset == "monthly":
        string_columns += ["meterNo", "salesGroupId", "salesGroupLabel"]
    elif dataset == "monthly_lm_groups":
        string_columns += ["salesGroupId", "salesGroupLabel"]

    for column in string_columns:
        cleaned = frame[column].map(clean_text)
        if cleaned.eq("").any():
            raise ValueError(f"{dataset}: blank values in {column}")
        if frame[column].astype(str).ne(cleaned).any():
            raise ValueError(f"{dataset}: whitespace drift in {column}")
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
    if dataset != "monthly":
        numeric_columns.insert(3, "metersCount")
    for column in numeric_columns:
        frame[column] = strict_integer_series(frame, column)

    if not frame["lmPcode"].eq(expected_lm_pcode).all():
        raise ValueError(f"{dataset}: lmPcode mismatch")
    if not frame["ym"].eq(expected_month).all():
        raise ValueError(f"{dataset}: ym mismatch")
    expected_year, expected_month_number = (
        int(part) for part in expected_month.split("-")
    )
    if not frame["y"].eq(expected_year).all():
        raise ValueError(f"{dataset}: y mismatch")
    if not frame["m"].eq(expected_month_number).all():
        raise ValueError(f"{dataset}: m mismatch")

    if frame["docId"].duplicated().any():
        raise ValueError(f"{dataset}: duplicate docId values")
    if not (
        frame["purchasesCount"].gt(0)
        & frame["amountTotalC"].ge(0)
        & frame["costC"].ge(0)
        & frame["vatC"].ge(0)
    ).all():
        raise ValueError(f"{dataset}: invalid count or money value")
    if not frame["amountTotalC"].eq(frame["costC"] + frame["vatC"]).all():
        raise ValueError(f"{dataset}: amountTotalC != costC + vatC")
    if not frame["firstPurchaseAtMs"].le(frame["lastPurchaseAtMs"]).all():
        raise ValueError(f"{dataset}: purchase date range is reversed")

    validate_iso_ms_pair(
        frame,
        iso_column="firstPurchaseAtISO",
        ms_column="firstPurchaseAtMs",
        dataset=dataset,
    )
    validate_iso_ms_pair(
        frame,
        iso_column="lastPurchaseAtISO",
        ms_column="lastPurchaseAtMs",
        dataset=dataset,
    )

    if dataset == "monthly":
        normalized = frame["meterNo"].map(normalize_meter_no)
        meter_valid = frame["meterNo"].str.fullmatch(r"[A-Z0-9]+")
        if not meter_valid.all() or not frame["meterNo"].eq(normalized).all():
            raise ValueError(
                "monthly: meterNo must be canonical uppercase alphanumeric text"
            )
        expected_doc_id = (
            frame["lmPcode"] + "__" + frame["meterNo"] + "__" + frame["ym"]
        )
        if not frame["docId"].eq(expected_doc_id).all():
            raise ValueError("monthly: docId mismatch")
        expected_group = frame["amountTotalC"].map(sales_group_from_amount_total_c)
        if not frame["salesGroupId"].eq(expected_group).all():
            raise ValueError("monthly: salesGroupId mismatch")
        if not frame["salesGroupLabel"].eq(
            frame["salesGroupId"].map(sales_group_label)
        ).all():
            raise ValueError("monthly: salesGroupLabel mismatch")

    elif dataset == "monthly_lm":
        expected_doc_id = frame["lmPcode"] + "__" + frame["ym"]
        if not frame["docId"].eq(expected_doc_id).all():
            raise ValueError("monthly_lm: docId mismatch")
        if len(frame) != 1:
            raise ValueError(
                f"monthly_lm must contain one LM/month row; found {len(frame)}"
            )
        if not frame["metersCount"].gt(0).all():
            raise ValueError("monthly_lm: metersCount must be positive")

    else:
        expected_doc_id = (
            frame["lmPcode"] + "__" + frame["ym"] + "__" + frame["salesGroupId"]
        )
        if not frame["docId"].eq(expected_doc_id).all():
            raise ValueError("monthly_lm_groups: docId mismatch")
        valid_groups = {"GR1", "GR2", "GR3", "GR4", "GR5"}
        if not set(frame["salesGroupId"]).issubset(valid_groups):
            raise ValueError("monthly_lm_groups: invalid salesGroupId")
        if not frame["salesGroupLabel"].eq(
            frame["salesGroupId"].map(sales_group_label)
        ).all():
            raise ValueError("monthly_lm_groups: salesGroupLabel mismatch")
        if not frame["metersCount"].gt(0).all():
            raise ValueError("monthly_lm_groups: metersCount must be positive")

    declared_rows = require_json_int(
        manifest_entry.get("rows"),
        label=f"Stage 03 manifest output {dataset} rows",
        minimum=1,
    )
    if len(frame) != declared_rows:
        raise ValueError(
            f"{dataset}: manifest declares {declared_rows} rows but CSV has {len(frame)}"
        )

    return MonthlyDataset(
        dataset=dataset,
        collection=COLLECTIONS[dataset],
        path=path,
        frame=frame,
        file_sha256=actual_sha,
    )


def reconcile_datasets(datasets: dict[str, MonthlyDataset]) -> dict[str, int]:
    monthly = datasets["monthly"].frame
    monthly_lm = datasets["monthly_lm"].frame
    groups = datasets["monthly_lm_groups"].frame

    expected = {
        "purchasesCount": int(monthly["purchasesCount"].sum()),
        "metersCount": int(len(monthly)),
        "amountTotalC": int(monthly["amountTotalC"].sum()),
        "costC": int(monthly["costC"].sum()),
        "vatC": int(monthly["vatC"].sum()),
        "firstPurchaseAtMs": int(monthly["firstPurchaseAtMs"].min()),
        "lastPurchaseAtMs": int(monthly["lastPurchaseAtMs"].max()),
    }
    lm_row = monthly_lm.iloc[0]
    for field, value in expected.items():
        if int(lm_row[field]) != value:
            raise ValueError(
                f"Monthly vs monthly_lm reconciliation failed for {field}: "
                f"{value} != {int(lm_row[field])}"
            )

    monthly_by_group = (
        monthly.groupby(
            ["salesGroupId", "salesGroupLabel"],
            as_index=False,
            sort=True,
        )
        .agg(
            metersCount=("meterNo", "count"),
            purchasesCount=("purchasesCount", "sum"),
            amountTotalC=("amountTotalC", "sum"),
            costC=("costC", "sum"),
            vatC=("vatC", "sum"),
            firstPurchaseAtMs=("firstPurchaseAtMs", "min"),
            lastPurchaseAtMs=("lastPurchaseAtMs", "max"),
        )
    )
    group_fields = [
        "metersCount",
        "purchasesCount",
        "amountTotalC",
        "costC",
        "vatC",
        "firstPurchaseAtMs",
        "lastPurchaseAtMs",
    ]
    expected_group_ids = set(monthly_by_group["salesGroupId"])
    actual_group_ids = set(groups["salesGroupId"])
    if actual_group_ids != expected_group_ids:
        raise ValueError(
            "monthly_lm_groups group-set reconciliation failed: "
            f"expected={sorted(expected_group_ids)}, actual={sorted(actual_group_ids)}"
        )
    actual_groups = groups.set_index("salesGroupId", drop=False)
    for expected_group in monthly_by_group.itertuples(index=False):
        actual_group = actual_groups.loc[expected_group.salesGroupId]
        if clean_text(actual_group["salesGroupLabel"]) != expected_group.salesGroupLabel:
            raise ValueError(
                "monthly_lm_groups label reconciliation failed for "
                f"{expected_group.salesGroupId}"
            )
        for field in group_fields:
            expected_value = int(getattr(expected_group, field))
            actual_value = int(actual_group[field])
            if actual_value != expected_value:
                raise ValueError(
                    "monthly_lm_groups per-group reconciliation failed for "
                    f"{expected_group.salesGroupId}.{field}: "
                    f"{actual_value} != {expected_value}"
                )

    group_totals = {
        "purchasesCount": int(groups["purchasesCount"].sum()),
        "metersCount": int(groups["metersCount"].sum()),
        "amountTotalC": int(groups["amountTotalC"].sum()),
        "costC": int(groups["costC"].sum()),
        "vatC": int(groups["vatC"].sum()),
        "firstPurchaseAtMs": int(groups["firstPurchaseAtMs"].min()),
        "lastPurchaseAtMs": int(groups["lastPurchaseAtMs"].max()),
    }
    for field, value in expected.items():
        if group_totals[field] != value:
            raise ValueError(
                f"monthly_lm_groups reconciliation failed for {field}: "
                f"{group_totals[field]} != {value}"
            )
    return expected


def validate_atomic_manifest_evidence(
    manifest: dict[str, Any],
    *,
    expected_lm_pcode: str,
    expected_month: str,
) -> dict[str, Any]:
    atomic_file = manifest.get("atomicFile")
    if not isinstance(atomic_file, dict):
        raise ValueError("Stage 03 manifest atomicFile must be an object")
    if clean_text(atomic_file.get("month")) != expected_month:
        raise ValueError("Stage 03 manifest Atomic month mismatch")

    path_text = clean_text(atomic_file.get("path"))
    if not path_text:
        raise ValueError("Stage 03 manifest Atomic path is blank")
    atomic_path = Path(path_text).expanduser().resolve()
    approved_atomic_dir = (PROJECT_ROOT / "output" / "atomic").resolve()
    if atomic_path.parent != approved_atomic_dir:
        raise ValueError(
            f"Stage 03 Atomic evidence must use {approved_atomic_dir}; "
            f"found {atomic_path.parent}"
        )
    if clean_text(atomic_file.get("filename")) != atomic_path.name:
        raise ValueError("Stage 03 manifest Atomic filename/path mismatch")

    filename_match = ATOMIC_FILENAME_RE.fullmatch(atomic_path.name)
    if filename_match is None:
        raise ValueError(f"Invalid Stage 03 Atomic filename: {atomic_path.name}")
    if filename_match.group("lm_pcode") != expected_lm_pcode:
        raise ValueError("Stage 03 Atomic filename LM mismatch")
    if filename_match.group("period") != expected_month:
        raise ValueError("Stage 03 Atomic filename month mismatch")

    declared_rows = require_json_int(
        atomic_file.get("rows"),
        label="Stage 03 manifest atomicFile.rows",
        minimum=1,
    )
    if int(filename_match.group("rows")) != declared_rows:
        raise ValueError("Stage 03 Atomic filename row count mismatch")

    expected_sha = require_sha256(
        atomic_file.get("sha256"),
        label="Stage 03 manifest atomicFile.sha256",
    )
    atomic_payload = read_file_snapshot(atomic_path, label="Stage 03 Atomic input")
    actual_sha = sha256_bytes(atomic_payload)
    if actual_sha != expected_sha:
        raise ValueError(
            "Stage 03 Atomic SHA-256 mismatch: "
            f"manifest={expected_sha}, actual={actual_sha}"
        )

    atomic = read_csv_robust_bytes(atomic_payload, atomic_path)
    if list(atomic.columns) != ATOMIC_COLUMNS:
        raise ValueError(
            f"Stage 03 Atomic schema mismatch. Expected {ATOMIC_COLUMNS}; "
            f"found {list(atomic.columns)}"
        )
    if atomic.empty or len(atomic) != declared_rows:
        raise ValueError(
            f"Stage 03 Atomic row count mismatch: declared={declared_rows}, "
            f"actual={len(atomic)}"
        )

    for column in ("atomicId", "vendingProviderId", "lmPcode", "meterNo", "ym"):
        cleaned = atomic[column].map(clean_text)
        if cleaned.eq("").any():
            raise ValueError(f"Stage 03 Atomic evidence has blank {column}")
        if atomic[column].astype(str).ne(cleaned).any():
            raise ValueError(f"Stage 03 Atomic evidence has whitespace drift in {column}")
        atomic[column] = cleaned

    if atomic["atomicId"].duplicated().any():
        raise ValueError("Stage 03 Atomic evidence has duplicate atomicId values")
    if not atomic["vendingProviderId"].eq(CONLOG_VENDING_PROVIDER_ID).all():
        raise ValueError("Stage 03 Atomic evidence has an unexpected provider")
    if not atomic["lmPcode"].eq(expected_lm_pcode).all():
        raise ValueError("Stage 03 Atomic evidence LM mismatch")
    if not atomic["ym"].eq(expected_month).all():
        raise ValueError("Stage 03 Atomic evidence month mismatch")
    normalized_meter = atomic["meterNo"].map(normalize_meter_no)
    if not atomic["meterNo"].eq(normalized_meter).all():
        raise ValueError("Stage 03 Atomic evidence contains noncanonical meter numbers")

    for column in ("txAtMs", "amountTotalC", "costC", "vatC"):
        atomic[column] = strict_integer_series(atomic, column)
    if not (
        atomic["amountTotalC"].ge(0)
        & atomic["costC"].ge(0)
        & atomic["vatC"].ge(0)
    ).all():
        raise ValueError("Stage 03 Atomic evidence contains negative money")
    if not atomic["amountTotalC"].eq(atomic["costC"] + atomic["vatC"]).all():
        raise ValueError("Stage 03 Atomic evidence monetary reconciliation failed")

    parsed_tx = pd.to_datetime(
        atomic["txAtISO"],
        format="%Y-%m-%dT%H:%M:%S",
        errors="coerce",
    )
    if parsed_tx.isna().any():
        raise ValueError("Stage 03 Atomic evidence contains invalid txAtISO")
    if not parsed_tx.dt.strftime("%Y-%m").eq(expected_month).all():
        raise ValueError("Stage 03 Atomic evidence contains a transaction outside the month")
    expected_tx_ms = (parsed_tx.astype("int64") // 1_000_000).astype("int64")
    if not atomic["txAtMs"].eq(expected_tx_ms).all():
        raise ValueError("Stage 03 Atomic evidence txAtMs does not match txAtISO")

    return {
        "purchasesCount": len(atomic),
        "metersCount": int(atomic["meterNo"].nunique()),
        "amountTotalC": int(atomic["amountTotalC"].sum()),
        "costC": int(atomic["costC"].sum()),
        "vatC": int(atomic["vatC"].sum()),
        "firstPurchaseAtMs": int(atomic["txAtMs"].min()),
        "lastPurchaseAtMs": int(atomic["txAtMs"].max()),
        "rows": len(atomic),
        "sha256": actual_sha,
        "path": str(atomic_path),
    }


def validate_manifest_evidence(
    manifest: dict[str, Any],
    datasets: dict[str, MonthlyDataset],
    reconciliation: dict[str, int],
    *,
    expected_lm_pcode: str,
    expected_month: str,
) -> dict[str, Any]:
    atomic = validate_atomic_manifest_evidence(
        manifest,
        expected_lm_pcode=expected_lm_pcode,
        expected_month=expected_month,
    )
    for field in (
        "purchasesCount",
        "metersCount",
        "amountTotalC",
        "costC",
        "vatC",
        "firstPurchaseAtMs",
        "lastPurchaseAtMs",
    ):
        if atomic[field] != reconciliation[field]:
            raise ValueError(
                f"Stage 03 Atomic-to-monthly reconciliation failed for {field}: "
                f"{atomic[field]} != {reconciliation[field]}"
            )

    count_evidence = {
        "atomicRows": atomic["rows"],
        "atomicUniqueMeters": reconciliation["metersCount"],
        "monthlyRows": len(datasets["monthly"].frame),
        "monthlyLmRows": len(datasets["monthly_lm"].frame),
        "monthlyLmGroupRows": len(datasets["monthly_lm_groups"].frame),
    }
    for field, expected_value in count_evidence.items():
        actual_value = require_json_int(
            manifest.get(field),
            label=f"Stage 03 manifest {field}",
            minimum=1,
        )
        if actual_value != expected_value:
            raise ValueError(
                f"Stage 03 manifest {field} mismatch: "
                f"{actual_value} != {expected_value}"
            )

    recorded_reconciliation = manifest.get("reconciliation")
    if not isinstance(recorded_reconciliation, list) or len(recorded_reconciliation) != 1:
        raise ValueError(
            "Stage 03 manifest reconciliation must contain exactly one LM/month entry"
        )
    recorded = recorded_reconciliation[0]
    if not isinstance(recorded, dict):
        raise ValueError("Stage 03 manifest reconciliation entry must be an object")
    if set(recorded) != set(STAGE03_RECONCILIATION_FIELDS):
        raise ValueError(
            "Stage 03 manifest reconciliation entry has an unexpected constitution"
        )

    expected_recorded = {
        "lmPcode": expected_lm_pcode,
        "month": expected_month,
        "purchasesCount": reconciliation["purchasesCount"],
        "metersCount": reconciliation["metersCount"],
        "amountTotalC": reconciliation["amountTotalC"],
        "costC": reconciliation["costC"],
        "vatC": reconciliation["vatC"],
    }
    for field in STAGE03_RECONCILIATION_FIELDS:
        if field in {"lmPcode", "month"}:
            if type(recorded[field]) is not str:
                raise ValueError(
                    f"Stage 03 reconciliation {field} must be a JSON string"
                )
        else:
            require_json_int(
                recorded[field],
                label=f"Stage 03 reconciliation {field}",
                minimum=0,
            )
        if recorded[field] != expected_recorded[field]:
            raise ValueError(
                f"Stage 03 reconciliation {field} mismatch: "
                f"{recorded[field]!r} != {expected_recorded[field]!r}"
            )

    return {
        "verification": "PASS",
        "atomic": atomic,
        "recordedReconciliation": expected_recorded,
        "countEvidence": count_evidence,
    }


def row_to_document(dataset: str, row: pd.Series) -> dict[str, Any]:
    integer_fields = {
        "y",
        "m",
        "purchasesCount",
        "metersCount",
        "amountTotalC",
        "costC",
        "vatC",
        "firstPurchaseAtMs",
        "lastPurchaseAtMs",
    }
    return {
        column: (
            int(row[column]) if column in integer_fields else str(row[column])
        )
        for column in DOCUMENT_COLUMNS[dataset]
    }


def sorted_doc_id_sha256(frame: pd.DataFrame) -> str:
    document_ids = sorted(frame["docId"].map(clean_text).tolist())
    return sha256_text("\n".join(document_ids))


def source_contract_core(contract: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": contract.get("version"),
        "projectId": contract.get("projectId"),
        "lmPcode": contract.get("lmPcode"),
        "month": contract.get("month"),
        "stage03ManifestSha256": contract.get("stage03ManifestSha256"),
        "datasets": contract.get("datasets"),
    }


def build_source_contract(
    *,
    project_id: str,
    lm_pcode: str,
    month: str,
    manifest_path: Path,
    manifest_sha256: str,
    datasets: dict[str, MonthlyDataset],
) -> dict[str, Any]:
    resolved_manifest = manifest_path.expanduser().resolve()
    require_sha256(
        manifest_sha256,
        label="Accepted Stage 03 manifest sha256",
    )

    contract_core = {
        "version": 1,
        "projectId": clean_text(project_id),
        "lmPcode": clean_text(lm_pcode).upper(),
        "month": clean_text(month),
        "stage03ManifestSha256": manifest_sha256,
        "datasets": {
            dataset: {
                "collection": value.collection,
                "sha256": value.file_sha256,
                "rows": len(value.frame),
                "docIdsSha256": sorted_doc_id_sha256(value.frame),
            }
            for dataset, value in sorted(datasets.items())
        },
    }

    return {
        **contract_core,
        "stage03ManifestPath": str(resolved_manifest),
        "fingerprint": canonical_json_sha256(contract_core),
    }


def validate_resume_contract(
    args: argparse.Namespace,
    *,
    source_contract: dict[str, Any],
) -> Optional[dict[str, Any]]:
    if args.mode == "create-only":
        if args.resume_report is not None:
            raise ValueError(
                "--resume-report is only valid when --mode resume"
            )
        return None

    if args.resume_report is None:
        raise ValueError(
            "--mode resume requires --resume-report pointing to the previous "
            "failed Stage 04 execute-upload report"
        )

    report_path = args.resume_report.expanduser().resolve()
    previous = read_json(report_path)

    if clean_text(previous.get("stage")) != "04":
        raise ValueError("Resume report is not a Stage 04 report")
    if clean_text(previous.get("script")) != "04_upload_conlog_monthly_v3.py":
        raise ValueError("Resume report was not produced by the Stage 04 v3 uploader")
    if clean_text(previous.get("operation")) != "execute-upload":
        raise ValueError("Resume report must come from a failed execute-upload attempt")
    if clean_text(previous.get("status")) != "FAIL":
        raise ValueError("Resume report status must be FAIL")
    if clean_text(previous.get("result")) != "FAILED":
        raise ValueError("Resume report result must be FAILED")

    expected_identity = {
        "targetProject": clean_text(args.project_id),
        "lmPcode": clean_text(args.lm_pcode).upper(),
        "month": clean_text(args.month),
    }
    actual_identity = {
        "targetProject": clean_text(previous.get("targetProject")),
        "lmPcode": clean_text(previous.get("lmPcode")).upper(),
        "month": clean_text(previous.get("month")),
    }
    if actual_identity != expected_identity:
        raise ValueError(
            "Resume report identity mismatch. "
            f"Expected={expected_identity}; report={actual_identity}"
        )

    previous_contract = previous.get("sourceContract")
    if not isinstance(previous_contract, dict):
        raise ValueError(
            "Resume report has no governed sourceContract. "
            "Do not resume from a legacy report; perform a controlled review."
        )

    previous_fingerprint = clean_text(previous_contract.get("fingerprint"))
    recomputed_previous_fingerprint = canonical_json_sha256(
        source_contract_core(previous_contract)
    )
    if (
        not previous_fingerprint
        or previous_fingerprint != recomputed_previous_fingerprint
    ):
        raise ValueError(
            "Resume report sourceContract fingerprint is missing or internally "
            "inconsistent. The failed report may have been edited or corrupted."
        )

    current_fingerprint = clean_text(source_contract.get("fingerprint"))
    recomputed_current_fingerprint = canonical_json_sha256(
        source_contract_core(source_contract)
    )
    if (
        not current_fingerprint
        or current_fingerprint != recomputed_current_fingerprint
    ):
        raise ValueError(
            "Current sourceContract fingerprint is internally inconsistent."
        )

    if previous_fingerprint != current_fingerprint:
        raise ValueError(
            "Resume source contract mismatch. The current Stage 03 manifest, "
            "input SHA set, row counts, or planned document IDs differ from "
            "the failed upload."
        )

    return {
        "reportPath": str(report_path),
        "previousRunStartedAt": previous.get("startedAt"),
        "previousRunFinishedAt": previous.get("finishedAt"),
        "previousMode": previous.get("mode"),
        "previousErrorType": previous.get("errorType"),
        "previousError": previous.get("error"),
        "sourceContractFingerprint": current_fingerprint,
        "verification": "PASS",
    }


def initialize_firestore(
    *,
    credential: CredentialIdentity,
    requested_project_id: str,
):
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
    except ImportError as exc:
        raise RuntimeError(
            "firebase-admin is required for Stage 04. Install project dependencies."
        ) from exc

    app_name = f"stage04-{requested_project_id}-{run_id(utc_now())}"
    cred = credentials.Certificate(str(credential.path))
    app = firebase_admin.initialize_app(
        cred,
        {"projectId": requested_project_id},
        name=app_name,
    )
    db = firestore.client(app=app)
    return firebase_admin, app, db


def validate_provider_document(db: Any, provider_id: str) -> dict[str, Any]:
    snapshot = db.collection(PROVIDER_COLLECTION).document(provider_id).get()
    if not snapshot.exists:
        raise ValueError(
            f"Required provider document does not exist: "
            f"{PROVIDER_COLLECTION}/{provider_id}"
        )
    data = snapshot.to_dict() or {}
    actual_id = clean_text(data.get("providerId"))
    code = clean_text(data.get("providerCode")).upper()
    status = clean_text(data.get("status")).lower()
    if actual_id != provider_id:
        raise ValueError("Provider document providerId mismatch")
    if code != CONLOG_PROVIDER_CODE:
        raise ValueError("Provider document providerCode mismatch")
    if status != "active":
        raise ValueError(f"Provider document is not active: {status!r}")
    return {
        "path": f"{PROVIDER_COLLECTION}/{provider_id}",
        "providerId": actual_id,
        "providerCode": code,
        "providerName": clean_text(data.get("providerName")),
        "status": status,
    }


def scope_query(db: Any, collection: str, lm_pcode: str, month: str):
    return (
        db.collection(collection)
        .where("lmPcode", "==", lm_pcode)
        .where("ym", "==", month)
    )


def query_count(query: Any) -> int:
    try:
        results = query.count().get()
        if results:
            first = results[0]
            if isinstance(first, (list, tuple)) and first:
                first = first[0]
            value = getattr(first, "value", None)
            if value is not None:
                return int(value)
    except Exception:
        pass
    return sum(1 for _ in query.stream())


def strict_document_differences(
    actual: object,
    expected: dict[str, Any],
) -> list[str]:
    if type(actual) is not dict:
        return ["<document>"]

    differences = set(actual) ^ set(expected)
    for field in set(actual) & set(expected):
        actual_value = actual[field]
        expected_value = expected[field]
        # bool is a subclass of int in Python, and ordinary equality also treats
        # 100.0 as equal to 100. Firestore schema verification must reject both.
        if type(actual_value) is not type(expected_value) or actual_value != expected_value:
            differences.add(field)
    return sorted(differences)


def inspect_existing_state(
    db: Any,
    dataset: MonthlyDataset,
    *,
    lm_pcode: str,
    month: str,
    mode: str,
) -> ExistingState:
    query = scope_query(db, dataset.collection, lm_pcode, month)
    expected_ids = set(dataset.frame["docId"].tolist())

    if mode == "create-only":
        count = query_count(query)
        if count != 0:
            raise ValueError(
                f"create-only requires empty scope: {dataset.collection}/"
                f"{lm_pcode}/{month} contains {count} documents"
            )
        return ExistingState(
            count=0,
            matching=0,
            missing=len(expected_ids),
            conflicts=0,
            extra=0,
            missing_ids=sorted(expected_ids),
            conflict_examples=[],
            extra_examples=[],
        )

    frame_by_id = dataset.frame.set_index("docId", drop=False)
    existing_ids: set[str] = set()
    matching = 0
    conflicts = 0
    conflict_examples: list[dict[str, Any]] = []

    for snapshot in query.stream():
        document_id = snapshot.id
        existing_ids.add(document_id)
        if document_id not in expected_ids:
            continue
        expected = row_to_document(dataset.dataset, frame_by_id.loc[document_id])
        actual = snapshot.to_dict() or {}
        differing = strict_document_differences(actual, expected)
        if not differing:
            matching += 1
        else:
            conflicts += 1
            if len(conflict_examples) < 5:
                conflict_examples.append(
                    {"docId": document_id, "differingFields": differing}
                )

    extra_ids = sorted(existing_ids - expected_ids)
    missing_ids = sorted(expected_ids - existing_ids)
    if conflicts or extra_ids:
        raise ValueError(
            f"Resume conflict in {dataset.collection}: conflicts={conflicts}, "
            f"extra={len(extra_ids)}, examples={conflict_examples}, "
            f"extraExamples={extra_ids[:5]}"
        )
    if matching + len(missing_ids) != len(expected_ids):
        raise ValueError(f"Resume did not account for all {dataset.dataset} rows")

    return ExistingState(
        count=len(existing_ids),
        matching=matching,
        missing=len(missing_ids),
        conflicts=conflicts,
        extra=len(extra_ids),
        missing_ids=missing_ids,
        conflict_examples=conflict_examples,
        extra_examples=extra_ids[:5],
    )


def batched(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def create_documents(
    db: Any,
    dataset: MonthlyDataset,
    document_ids: list[str],
) -> tuple[int, int]:
    if not document_ids:
        return 0, 0
    frame_by_id = dataset.frame.set_index("docId", drop=False)
    total_batches = (len(document_ids) + BATCH_SIZE - 1) // BATCH_SIZE
    created = 0
    committed = 0
    for number, chunk in enumerate(batched(document_ids, BATCH_SIZE), start=1):
        batch = db.batch()
        for document_id in chunk:
            row = frame_by_id.loc[document_id]
            document_ref = db.collection(dataset.collection).document(document_id)
            batch.create(document_ref, row_to_document(dataset.dataset, row))
        batch.commit()
        committed += 1
        created += len(chunk)
        print(
            f"  - {dataset.collection} batch {number}/{total_batches}: "
            f"created {len(chunk):,} (total {created:,}/{len(document_ids):,})"
        )
    return created, committed


def deterministic_sample_ids(frame: pd.DataFrame) -> list[str]:
    ids = frame["docId"].tolist()
    positions = sorted({0, len(ids) // 2, len(ids) - 1})
    return [ids[position] for position in positions]


def verify_post_upload(
    db: Any,
    dataset: MonthlyDataset,
    *,
    lm_pcode: str,
    month: str,
) -> dict[str, Any]:
    final_count = query_count(
        scope_query(db, dataset.collection, lm_pcode, month)
    )
    expected_count = len(dataset.frame)
    if final_count != expected_count:
        raise ValueError(
            f"{dataset.collection} count verification failed: "
            f"expected {expected_count}, found {final_count}"
        )

    frame_by_id = dataset.frame.set_index("docId", drop=False)
    samples: list[dict[str, Any]] = []
    for document_id in deterministic_sample_ids(dataset.frame):
        snapshot = db.collection(dataset.collection).document(document_id).get()
        if not snapshot.exists:
            raise ValueError(
                f"Missing sample document: {dataset.collection}/{document_id}"
            )
        expected = row_to_document(dataset.dataset, frame_by_id.loc[document_id])
        actual = snapshot.to_dict() or {}
        differing = strict_document_differences(actual, expected)
        matches = not differing
        samples.append({"docId": document_id, "matches": matches})
        if not matches:
            raise ValueError(
                f"Sample verification failed for {dataset.collection}/{document_id}: "
                f"{differing}"
            )
    return {
        "expectedCount": expected_count,
        "finalCount": final_count,
        "countVerification": "PASS",
        "sampleVerification": "PASS",
        "samples": samples,
    }


def base_report(args: argparse.Namespace, started: dt.datetime) -> dict[str, Any]:
    return {
        "stage": "04",
        "script": "04_upload_conlog_monthly_v3.py",
        "status": "STARTED",
        "operation": "execute-upload" if args.execute_upload else "preflight-only",
        "mode": args.mode,
        "targetProject": clean_text(args.project_id),
        "confirmProject": clean_text(args.confirm_project),
        "targetCollections": list(COLLECTIONS.values()),
        "providerId": clean_text(args.vending_provider_id),
        "lmPcode": clean_text(args.lm_pcode).upper(),
        "month": clean_text(args.month),
        "manifestPath": str(args.manifest.expanduser().resolve()),
        "resumeReportPath": (
            str(args.resume_report.expanduser().resolve())
            if args.resume_report is not None
            else None
        ),
        "startedAt": utc_iso(started),
    }


def report_path(log_dir: Path, report: dict[str, Any], started: dt.datetime) -> Path:
    project = re.sub(r"[^A-Za-z0-9_-]+", "_", report["targetProject"] or "unknown")
    lm = re.sub(r"[^A-Za-z0-9_-]+", "_", report["lmPcode"] or "unknown")
    month = re.sub(r"[^0-9-]+", "_", report["month"] or "unknown")
    return (
        log_dir.expanduser().resolve()
        / f"stage04_monthly_upload__{project}__{lm}__{month}__{run_id(started)}.json"
    )


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as target:
        json.dump(report, target, indent=2, sort_keys=True, ensure_ascii=False)
        target.write("\n")
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    started = utc_now()
    report = base_report(args, started)
    report_file = report_path(args.log_dir, report, started)

    firebase_admin_module = None
    firebase_app = None
    created_counts = {dataset: 0 for dataset in COLLECTIONS}
    committed_counts = {dataset: 0 for dataset in COLLECTIONS}

    try:
        validate_month(args.month)
        lm_pcode = clean_text(args.lm_pcode).upper()
        if not lm_pcode:
            raise ValueError("--lm-pcode cannot be blank")

        provider_id = clean_text(args.vending_provider_id)
        if provider_id != CONLOG_VENDING_PROVIDER_ID:
            raise ValueError(
                f"This uploader is governed for {CONLOG_VENDING_PROVIDER_ID!r}"
            )

        credential = validate_project_identity(args)
        manifest, selected, manifest_sha256 = load_manifest_outputs(
            args.manifest,
            expected_lm_pcode=lm_pcode,
            expected_month=args.month,
        )
        datasets = {
            dataset: validate_dataset(
                dataset,
                selected[dataset],
                expected_lm_pcode=lm_pcode,
                expected_month=args.month,
            )
            for dataset in COLLECTIONS
        }
        reconciliation = reconcile_datasets(datasets)
        manifest_evidence = validate_manifest_evidence(
            manifest,
            datasets,
            reconciliation,
            expected_lm_pcode=lm_pcode,
            expected_month=args.month,
        )
        source_contract = build_source_contract(
            project_id=args.project_id,
            lm_pcode=lm_pcode,
            month=args.month,
            manifest_path=args.manifest,
            manifest_sha256=manifest_sha256,
            datasets=datasets,
        )
        recovery = validate_resume_contract(
            args,
            source_contract=source_contract,
        )

        report.update(
            {
                "credentialProject": credential.project_id,
                "serviceAccountPath": str(credential.path),
                "stage03ManifestResult": manifest.get("result"),
                "stage03Month": manifest.get("month"),
                "stage03ManifestSha256": manifest_sha256,
                "stage03Evidence": manifest_evidence,
                "inputs": {
                    dataset: {
                        "path": str(value.path),
                        "filename": value.path.name,
                        "sha256": value.file_sha256,
                        "rows": len(value.frame),
                        "collection": value.collection,
                    }
                    for dataset, value in datasets.items()
                },
                "reconciliation": reconciliation,
                "sourceContract": source_contract,
                "recovery": recovery,
            }
        )

        firebase_admin_module, firebase_app, db = initialize_firestore(
            credential=credential,
            requested_project_id=args.project_id,
        )
        provider = validate_provider_document(db, provider_id)
        states = {
            dataset: inspect_existing_state(
                db,
                value,
                lm_pcode=lm_pcode,
                month=args.month,
                mode=args.mode,
            )
            for dataset, value in datasets.items()
        }
        report["provider"] = provider
        report["preflight"] = {
            dataset: {
                "collection": datasets[dataset].collection,
                "documentsBefore": state.count,
                "matchingDocuments": state.matching,
                "documentsPlanned": state.missing,
                "conflictCount": state.conflicts,
                "extraDocumentCount": state.extra,
                "conflictExamples": state.conflict_examples,
                "extraDocumentExamples": state.extra_examples,
            }
            for dataset, state in states.items()
        }

        print("[STAGE 04] MONTHLY SALES CSVs -> FIRESTORE")
        print(f"  operation:          {report['operation']}")
        print(f"  mode:               {args.mode}")
        print(f"  target project:     {args.project_id}")
        print(f"  credential project: {credential.project_id}")
        print(f"  LM / month:         {lm_pcode} / {args.month}")
        print(f"  Stage 03 manifest:  {args.manifest.expanduser().resolve()}")
        print(f"  source fingerprint: {source_contract['fingerprint']}")
        if recovery is not None:
            print(f"  resume report:      {recovery['reportPath']}")
            print("  resume contract:    PASS")
        print("  cross-reconciliation: PASS")
        for dataset, value in datasets.items():
            state = states[dataset]
            print(
                f"  {value.collection}: rows={len(value.frame):,}, "
                f"existing={state.count:,}, create={state.missing:,}, "
                f"conflicts={state.conflicts:,}, extra={state.extra:,}"
            )

        if args.preflight_only:
            report["status"] = "PASS"
            report["result"] = "PREFLIGHT_OK"
            print("\n[PREFLIGHT OK] No Monthly Sales documents were written.")
        else:
            for dataset in ("monthly", "monthly_lm", "monthly_lm_groups"):
                value = datasets[dataset]
                created, committed = create_documents(
                    db,
                    value,
                    states[dataset].missing_ids,
                )
                created_counts[dataset] = created
                committed_counts[dataset] = committed

            verification = {
                dataset: verify_post_upload(
                    db,
                    value,
                    lm_pcode=lm_pcode,
                    month=args.month,
                )
                for dataset, value in datasets.items()
            }
            report.update(
                {
                    "documentsCreated": created_counts,
                    "committedBatches": committed_counts,
                    "verification": verification,
                    "status": "PASS",
                    "result": "UPLOAD_VERIFIED",
                }
            )
            print("\n[VERIFY PASS] All three collection counts and samples match.")
        return 0

    except Exception as exc:
        report.update(
            {
                "status": "FAIL",
                "result": "FAILED",
                "documentsCreated": created_counts,
                "committedBatches": committed_counts,
                "errorType": type(exc).__name__,
                "error": str(exc),
            }
        )
        print(f"\n[FAILED] {exc}", file=sys.stderr)
        return 1

    finally:
        if firebase_admin_module is not None and firebase_app is not None:
            try:
                firebase_admin_module.delete_app(firebase_app)
            except Exception:
                pass
        report["finishedAt"] = utc_iso(utc_now())
        try:
            write_report(report_file, report)
            print(f"\n[REPORT] {report_file}")
        except Exception as report_error:
            print(
                f"[WARN] Could not write Stage 04 report: {report_error}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    raise SystemExit(main())
