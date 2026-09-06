"""
Stage 04: safely upload one LM/month of the three Conlog Monthly Sales datasets.

Inputs are selected only from a successful Stage 03 Atomic or Stage 03B monthly-source BUILD_WRITTEN manifest.

Target collections:
    conlog_sales_monthly
    conlog_sales_monthly_lm
    conlog_sales_monthly_lm_groups

Safety model:
    - one Firebase project + one LM + one month per execution;
    - explicit target project, matching confirmation, and service-account path;
    - service-account project_id must match before Firebase starts;
    - source manifest, exact origin-specific CSV schemas, SHA-256, identities, and reconciliation;
    - governed vending-provider document must exist and be active;
    - create-only for normal uploads;
    - monthly_source refresh is a distinct recurring mode: missing documents are created,
      compatible changed pipeline-owned fields are preconditioned updates, and identical
      documents receive no write;
    - resume only with a previous failed Stage 04 execute-upload report that proves
      the exact same project, LM, month, Stage 03 manifest, input SHA set, and planned IDs;
    - Atomic remains create-only/resume; refresh is prohibited for Atomic inputs;
    - no merge, delete, blind overwrite, or silent conflict skip;
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
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
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
STAGE03B_SCRIPT = "03b_build_monthly_from_monthly_source.py"
STAGE03B_OPERATION = "build-write"
SOURCE_ORIGIN_ATOMIC = "atomic"
SOURCE_ORIGIN_MONTHLY = "monthly_source"
PROVIDER_KEY_RE = re.compile(r"^[a-z0-9_-]+$")
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

MONTHLY_SOURCE_COLUMNS = [
    "docId",
    "sourceOrigin",
    "provider",
    "lmPcode",
    "meterNo",
    "ym",
    "y",
    "m",
    "amountTotalC",
    "unitsTotal",
    "salesGroupId",
    "salesGroupLabel",
    "sourceDocumentId",
    "sourceEndRow",
]

MONTHLY_SOURCE_LM_COLUMNS = [
    "docId",
    "sourceOrigin",
    "provider",
    "lmPcode",
    "ym",
    "y",
    "m",
    "metersCount",
    "amountTotalC",
    "unitsTotal",
    "zeroSalesMetersCount",
]

MONTHLY_SOURCE_LM_GROUP_COLUMNS = [
    "docId",
    "sourceOrigin",
    "provider",
    "lmPcode",
    "ym",
    "y",
    "m",
    "salesGroupId",
    "salesGroupLabel",
    "metersCount",
    "amountTotalC",
    "unitsTotal",
    "zeroSalesMetersCount",
]

MONTHLY_SOURCE_EXPECTED_COLUMNS = {
    "monthly": MONTHLY_SOURCE_COLUMNS,
    "monthly_lm": MONTHLY_SOURCE_LM_COLUMNS,
    "monthly_lm_groups": MONTHLY_SOURCE_LM_GROUP_COLUMNS,
}

MONTHLY_SOURCE_DOCUMENT_COLUMNS = {
    dataset: [column for column in columns if column != "docId"]
    for dataset, columns in MONTHLY_SOURCE_EXPECTED_COLUMNS.items()
}

MONTHLY_SOURCE_OUTPUT_LOCATIONS = {
    "monthly": ("monthly", "monthly__FULL__{month}__from_monthly_source.csv"),
    "monthly_lm": (
        "monthly_lm",
        "monthly_lm__FULL__{month}__from_monthly_source.csv",
    ),
    "monthly_lm_groups": (
        "monthly_lm_groups",
        "monthly_lm_groups__FULL__{month}__from_monthly_source.csv",
    ),
}

MONTHLY_SOURCE_RECONCILIATION_FIELDS = [
    "lmPcode",
    "month",
    "metersCount",
    "amountTotalC",
    "unitsTotal",
    "zeroSalesMetersCount",
]

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


@dataclass(frozen=True)
class PlannedUpdate:
    document_id: str
    updates: dict[str, Any]
    update_time: Any
    differing_fields: tuple[str, ...]


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
    updated: int = 0
    update_operations: list[PlannedUpdate] = field(default_factory=list)
    read_waves: int = 0


MONTHLY_SOURCE_REFRESH_IMMUTABLE_FIELDS = {
    "monthly": {
        "sourceOrigin",
        "provider",
        "lmPcode",
        "meterNo",
        "ym",
        "y",
        "m",
    },
    "monthly_lm": {
        "sourceOrigin",
        "provider",
        "lmPcode",
        "ym",
        "y",
        "m",
    },
    "monthly_lm_groups": {
        "sourceOrigin",
        "provider",
        "lmPcode",
        "ym",
        "y",
        "m",
        "salesGroupId",
    },
}

MONTHLY_SOURCE_INTEGER_DOCUMENT_FIELDS = {
    "y",
    "m",
    "metersCount",
    "amountTotalC",
    "zeroSalesMetersCount",
}


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
        choices=("create-only", "refresh", "resume"),
        help=(
            "create-only is normal; refresh is governed recurring monthly_source; "
            "resume is only verified partial-upload recovery."
        ),
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


def canonical_json_line_sha256(value: object) -> str:
    payload = (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    return sha256_bytes(payload)


def manifest_source_contract(manifest: dict[str, Any]) -> tuple[str, str]:
    stage = clean_text(manifest.get("stage")).upper()
    if stage == "03":
        return SOURCE_ORIGIN_ATOMIC, "conlog"
    if stage == "03B":
        source_origin = clean_text(manifest.get("sourceOrigin")).lower()
        provider = clean_text(manifest.get("provider")).lower()
        if source_origin != SOURCE_ORIGIN_MONTHLY:
            raise ValueError(
                f"Stage 03B sourceOrigin must be {SOURCE_ORIGIN_MONTHLY!r}"
            )
        if not PROVIDER_KEY_RE.fullmatch(provider):
            raise ValueError(f"Stage 03B provider is invalid: {provider!r}")
        return source_origin, provider
    raise ValueError(f"Manifest stage must be '03' or '03B'; found {stage!r}")


def strict_units_series(frame: pd.DataFrame, column: str) -> pd.Series:
    values: list[float] = []
    invalid_lines: list[int] = []
    for index, value in frame[column].items():
        text = clean_text(value)
        try:
            amount = Decimal(text)
        except (InvalidOperation, ValueError):
            invalid_lines.append(int(index) + 2)
            values.append(0.0)
            continue
        if not amount.is_finite() or amount < 0:
            invalid_lines.append(int(index) + 2)
            values.append(0.0)
            continue
        normalized = amount.quantize(Decimal("0.1"))
        if amount != normalized:
            invalid_lines.append(int(index) + 2)
            values.append(0.0)
            continue
        values.append(float(normalized))
    if invalid_lines:
        raise ValueError(
            f"{column} must contain finite non-negative values normalized to one "
            f"decimal place. Invalid CSV lines: {invalid_lines[:5]}"
        )
    return pd.Series(values, index=frame.index, dtype="float64")


def decimal_units_sum(values: Iterable[object]) -> Decimal:
    total = Decimal("0.0")
    for value in values:
        total += Decimal(str(value)).quantize(Decimal("0.1"))
    return total.quantize(Decimal("0.1"))


def format_units(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.1')):.1f}"


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


def load_atomic_manifest_outputs(
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


def load_monthly_source_manifest_outputs(
    manifest_path: Path,
    *,
    expected_lm_pcode: str,
    expected_month: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
    manifest, manifest_sha256 = read_json_snapshot(manifest_path)
    if clean_text(manifest.get("stage")).upper() != "03B":
        raise ValueError("Manifest is not a Stage 03B report")
    if clean_text(manifest.get("script")) != STAGE03B_SCRIPT:
        raise ValueError(f"Stage 03B manifest script must be {STAGE03B_SCRIPT!r}")
    if clean_text(manifest.get("operation")) != STAGE03B_OPERATION:
        raise ValueError(f"Stage 03B manifest operation must be {STAGE03B_OPERATION!r}")
    if clean_text(manifest.get("status")) != "PASS":
        raise ValueError("Stage 03B manifest status is not PASS")
    if clean_text(manifest.get("result")) != "BUILD_WRITTEN":
        raise ValueError("Stage 03B manifest must have result BUILD_WRITTEN")

    source_origin, provider = manifest_source_contract(manifest)
    if source_origin != SOURCE_ORIGIN_MONTHLY:
        raise ValueError("Stage 03B manifest source contract is not monthly_source")
    if clean_text(manifest.get("provider")) != provider:
        raise ValueError("Stage 03B provider must be canonical lowercase text")
    if clean_text(manifest.get("lmPcode")) != expected_lm_pcode:
        raise ValueError("Stage 03B manifest LM does not match --lm-pcode")
    if clean_text(manifest.get("month")) != expected_month:
        raise ValueError("Stage 03B manifest month does not match --month")
    parse_stage03_timestamp(manifest.get("builtAt"), label="Stage 03B manifest builtAt")

    source_input = manifest.get("sourceInput")
    if not isinstance(source_input, dict):
        raise ValueError("Stage 03B manifest sourceInput must be an object")
    source_path = clean_text(source_input.get("path"))
    if not source_path:
        raise ValueError("Stage 03B manifest sourceInput.path is blank")
    require_sha256(
        source_input.get("sha256"), label="Stage 03B manifest sourceInput.sha256"
    )
    require_json_int(
        source_input.get("rows"), label="Stage 03B manifest sourceInput.rows", minimum=1
    )

    source_facts = manifest.get("sourceFacts")
    expected_source_facts = {
        "purchasesCountAvailable": False,
        "costVatBreakdownAvailable": False,
        "purchaseTimestampsAvailable": False,
        "atomicTransactionsAvailable": False,
    }
    if source_facts != expected_source_facts:
        raise ValueError(
            "Stage 03B sourceFacts must explicitly prove that Atomic-only facts "
            "were not fabricated"
        )

    outputs = manifest.get("outputs")
    if not isinstance(outputs, list) or len(outputs) != len(COLLECTIONS):
        raise ValueError(
            "Stage 03B manifest must contain exactly three monthly output entries"
        )

    selected: dict[str, dict[str, Any]] = {}
    for position, item in enumerate(outputs):
        if not isinstance(item, dict):
            raise ValueError(
                f"Stage 03B manifest output entry {position} must be an object"
            )
        dataset = clean_text(item.get("dataset"))
        if dataset not in COLLECTIONS:
            raise ValueError(
                f"Stage 03B manifest contains an unapproved output dataset: {dataset!r}"
            )
        if dataset in selected:
            raise ValueError(
                f"Stage 03B manifest contains duplicate {dataset}/{expected_month} outputs"
            )
        if clean_text(item.get("month")) != expected_month:
            raise ValueError(
                f"Stage 03B manifest output {dataset} is not for {expected_month}"
            )

        directory, filename_template = MONTHLY_SOURCE_OUTPUT_LOCATIONS[dataset]
        expected_path = (
            PROJECT_ROOT / "output" / directory / filename_template.format(month=expected_month)
        ).resolve()
        actual_path = Path(clean_text(item.get("path"))).expanduser().resolve()
        if actual_path != expected_path:
            raise ValueError(
                f"Stage 03B manifest output {dataset} must use approved path "
                f"{expected_path}; found {actual_path}"
            )
        if clean_text(item.get("filename")) != expected_path.name:
            raise ValueError(f"Stage 03B manifest output {dataset} filename mismatch")
        if item.get("columns") != MONTHLY_SOURCE_EXPECTED_COLUMNS[dataset]:
            raise ValueError(
                f"Stage 03B manifest output {dataset} columns do not match the "
                "approved monthly-source contract"
            )
        require_json_int(
            item.get("rows"),
            label=f"Stage 03B manifest output {dataset} rows",
            minimum=1,
        )
        require_sha256(
            item.get("sha256"),
            label=f"Stage 03B manifest output {dataset} sha256",
        )
        write_state = clean_text(item.get("writeState"))
        if write_state not in {"WRITTEN", "UNCHANGED"}:
            raise ValueError(
                f"Stage 03B manifest output {dataset} has invalid writeState "
                f"{write_state!r}"
            )
        selected[dataset] = item

    missing = sorted(set(COLLECTIONS) - set(selected))
    if missing:
        raise ValueError(f"Stage 03B manifest is missing monthly outputs for {missing}")

    write_summary = manifest.get("writeSummary")
    if not isinstance(write_summary, dict):
        raise ValueError("Stage 03B manifest writeSummary must be an object")
    if not set(write_summary).issubset({"WRITTEN", "UNCHANGED"}):
        raise ValueError("Stage 03B manifest writeSummary has an unexpected state")
    accounted = 0
    for key, value in write_summary.items():
        accounted += require_json_int(
            value, label=f"Stage 03B manifest writeSummary.{key}", minimum=0
        )
    if accounted != len(COLLECTIONS):
        raise ValueError(
            "Stage 03B manifest writeSummary must account for exactly three outputs"
        )

    fingerprint = require_sha256(
        manifest.get("buildFingerprint"), label="Stage 03B manifest buildFingerprint"
    )
    manifest_core = dict(manifest)
    manifest_core.pop("buildFingerprint", None)
    expected_fingerprint = canonical_json_line_sha256(manifest_core)
    if fingerprint != expected_fingerprint:
        raise ValueError("Stage 03B manifest buildFingerprint mismatch")

    return manifest, selected, manifest_sha256


def load_manifest_outputs(
    manifest_path: Path,
    *,
    expected_lm_pcode: str,
    expected_month: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
    manifest, _ = read_json_snapshot(manifest_path)
    source_origin, _provider = manifest_source_contract(manifest)
    if source_origin == SOURCE_ORIGIN_ATOMIC:
        return load_atomic_manifest_outputs(
            manifest_path,
            expected_lm_pcode=expected_lm_pcode,
            expected_month=expected_month,
        )
    return load_monthly_source_manifest_outputs(
        manifest_path,
        expected_lm_pcode=expected_lm_pcode,
        expected_month=expected_month,
    )


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


def validate_atomic_dataset(
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


def validate_monthly_source_dataset(
    dataset: str,
    manifest_entry: dict[str, Any],
    *,
    expected_lm_pcode: str,
    expected_month: str,
    expected_provider: str,
) -> MonthlyDataset:
    path = Path(clean_text(manifest_entry.get("path"))).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Stage 03B output is missing: {path}")

    expected_sha = require_sha256(
        manifest_entry.get("sha256"), label=f"Stage 03B output {dataset} sha256"
    )
    csv_payload = read_file_snapshot(path, label=f"Stage 03B output {dataset}")
    actual_sha = sha256_bytes(csv_payload)
    if actual_sha != expected_sha:
        raise ValueError(
            f"Stage 03B output SHA-256 mismatch for {path.name}: "
            f"manifest={expected_sha}, actual={actual_sha}"
        )

    frame = read_csv_robust_bytes(csv_payload, path)
    expected_columns = MONTHLY_SOURCE_EXPECTED_COLUMNS[dataset]
    if list(frame.columns) != expected_columns:
        raise ValueError(
            f"{dataset} monthly-source schema mismatch. Expected {expected_columns}; "
            f"found {list(frame.columns)}"
        )
    if frame.empty:
        raise ValueError(f"{dataset} monthly-source CSV is empty: {path}")

    string_columns = ["docId", "sourceOrigin", "provider", "lmPcode", "ym"]
    if dataset == "monthly":
        string_columns += [
            "meterNo", "salesGroupId", "salesGroupLabel", "sourceDocumentId"
        ]
    elif dataset == "monthly_lm_groups":
        string_columns += ["salesGroupId", "salesGroupLabel"]

    for column in string_columns:
        cleaned = frame[column].map(clean_text)
        if cleaned.eq("").any():
            raise ValueError(f"{dataset}: blank values in {column}")
        if frame[column].astype(str).ne(cleaned).any():
            raise ValueError(f"{dataset}: whitespace drift in {column}")
        frame[column] = cleaned

    integer_columns = ["y", "m", "amountTotalC"]
    if dataset != "monthly":
        integer_columns += ["metersCount", "zeroSalesMetersCount"]
    for column in integer_columns:
        frame[column] = strict_integer_series(frame, column)
    frame["unitsTotal"] = strict_units_series(frame, "unitsTotal")

    if dataset == "monthly":
        source_end_rows: list[object] = []
        for index, value in frame["sourceEndRow"].items():
            cleaned = clean_text(value)
            if not cleaned:
                source_end_rows.append(None)
                continue
            if not re.fullmatch(r"\d+", cleaned):
                raise ValueError(
                    f"monthly: sourceEndRow must be blank or a non-negative integer; "
                    f"CSV line {int(index) + 2}"
                )
            source_end_rows.append(int(cleaned))
        frame["sourceEndRow"] = pd.Series(source_end_rows, index=frame.index, dtype="object")

    if not frame["sourceOrigin"].eq(SOURCE_ORIGIN_MONTHLY).all():
        raise ValueError(f"{dataset}: sourceOrigin mismatch")
    if not frame["provider"].eq(expected_provider).all():
        raise ValueError(f"{dataset}: provider mismatch")
    if not frame["lmPcode"].eq(expected_lm_pcode).all():
        raise ValueError(f"{dataset}: lmPcode mismatch")
    if not frame["ym"].eq(expected_month).all():
        raise ValueError(f"{dataset}: ym mismatch")

    expected_year, expected_month_number = (int(part) for part in expected_month.split("-"))
    if not frame["y"].eq(expected_year).all():
        raise ValueError(f"{dataset}: y mismatch")
    if not frame["m"].eq(expected_month_number).all():
        raise ValueError(f"{dataset}: m mismatch")
    if frame["docId"].duplicated().any():
        raise ValueError(f"{dataset}: duplicate docId values")
    if not frame["amountTotalC"].ge(0).all():
        raise ValueError(f"{dataset}: amountTotalC cannot be negative")
    if not frame["unitsTotal"].ge(0).all():
        raise ValueError(f"{dataset}: unitsTotal cannot be negative")

    if dataset == "monthly":
        normalized = frame["meterNo"].map(normalize_meter_no)
        if not frame["meterNo"].eq(normalized).all():
            raise ValueError("monthly: meterNo is not canonical")
        if frame["meterNo"].duplicated().any():
            raise ValueError("monthly: duplicate meterNo values")
        expected_doc_id = frame["lmPcode"] + "__" + frame["meterNo"] + "__" + frame["ym"]
        if not frame["docId"].eq(expected_doc_id).all():
            raise ValueError("monthly: docId mismatch")
        expected_group = frame["amountTotalC"].map(sales_group_from_amount_total_c)
        if not frame["salesGroupId"].eq(expected_group).all():
            raise ValueError("monthly: salesGroupId mismatch")
        if not frame["salesGroupLabel"].eq(frame["salesGroupId"].map(sales_group_label)).all():
            raise ValueError("monthly: salesGroupLabel mismatch")
    elif dataset == "monthly_lm":
        expected_doc_id = frame["lmPcode"] + "__" + frame["ym"]
        if not frame["docId"].eq(expected_doc_id).all():
            raise ValueError("monthly_lm: docId mismatch")
        if len(frame) != 1:
            raise ValueError(f"monthly_lm must contain one LM/month row; found {len(frame)}")
        if not frame["metersCount"].gt(0).all():
            raise ValueError("monthly_lm: metersCount must be positive")
        if not (frame["zeroSalesMetersCount"].ge(0) & frame["zeroSalesMetersCount"].le(frame["metersCount"])).all():
            raise ValueError("monthly_lm: invalid zeroSalesMetersCount")
    else:
        expected_doc_id = frame["lmPcode"] + "__" + frame["ym"] + "__" + frame["salesGroupId"]
        if not frame["docId"].eq(expected_doc_id).all():
            raise ValueError("monthly_lm_groups: docId mismatch")
        valid_groups = {"GR1", "GR2", "GR3", "GR4", "GR5"}
        if not set(frame["salesGroupId"]).issubset(valid_groups):
            raise ValueError("monthly_lm_groups: invalid salesGroupId")
        if not frame["salesGroupLabel"].eq(frame["salesGroupId"].map(sales_group_label)).all():
            raise ValueError("monthly_lm_groups: salesGroupLabel mismatch")
        if not frame["metersCount"].gt(0).all():
            raise ValueError("monthly_lm_groups: metersCount must be positive")
        if not (frame["zeroSalesMetersCount"].ge(0) & frame["zeroSalesMetersCount"].le(frame["metersCount"])).all():
            raise ValueError("monthly_lm_groups: invalid zeroSalesMetersCount")

    declared_rows = require_json_int(
        manifest_entry.get("rows"),
        label=f"Stage 03B manifest output {dataset} rows",
        minimum=1,
    )
    if len(frame) != declared_rows:
        raise ValueError(
            f"{dataset}: manifest declares {declared_rows} rows but CSV has {len(frame)}"
        )

    return MonthlyDataset(
        dataset=dataset, collection=COLLECTIONS[dataset], path=path, frame=frame, file_sha256=actual_sha
    )


def validate_dataset(
    dataset: str,
    manifest_entry: dict[str, Any],
    *,
    expected_lm_pcode: str,
    expected_month: str,
    source_origin: str,
    expected_provider: str,
) -> MonthlyDataset:
    if source_origin == SOURCE_ORIGIN_ATOMIC:
        return validate_atomic_dataset(
            dataset,
            manifest_entry,
            expected_lm_pcode=expected_lm_pcode,
            expected_month=expected_month,
        )
    if source_origin == SOURCE_ORIGIN_MONTHLY:
        return validate_monthly_source_dataset(
            dataset,
            manifest_entry,
            expected_lm_pcode=expected_lm_pcode,
            expected_month=expected_month,
            expected_provider=expected_provider,
        )
    raise ValueError(f"Unsupported sourceOrigin: {source_origin!r}")


def reconcile_monthly_source_datasets(
    datasets: dict[str, MonthlyDataset],
) -> dict[str, Any]:
    monthly = datasets["monthly"].frame
    monthly_lm = datasets["monthly_lm"].frame
    groups = datasets["monthly_lm_groups"].frame

    amount_total = int(monthly["amountTotalC"].sum())
    units_total = decimal_units_sum(monthly["unitsTotal"].tolist())
    zero_sales = int(monthly["amountTotalC"].eq(0).sum())
    expected = {
        "metersCount": int(len(monthly)),
        "amountTotalC": amount_total,
        "unitsTotal": format_units(units_total),
        "zeroSalesMetersCount": zero_sales,
    }

    lm_row = monthly_lm.iloc[0]
    if int(lm_row["metersCount"]) != expected["metersCount"]:
        raise ValueError("Monthly vs monthly_lm reconciliation failed for metersCount")
    if int(lm_row["amountTotalC"]) != amount_total:
        raise ValueError("Monthly vs monthly_lm reconciliation failed for amountTotalC")
    if format_units(Decimal(str(lm_row["unitsTotal"]))) != expected["unitsTotal"]:
        raise ValueError("Monthly vs monthly_lm reconciliation failed for unitsTotal")
    if int(lm_row["zeroSalesMetersCount"]) != zero_sales:
        raise ValueError("Monthly vs monthly_lm reconciliation failed for zeroSalesMetersCount")

    expected_groups: dict[str, dict[str, Any]] = {}
    for row in monthly.itertuples(index=False):
        group_id = clean_text(row.salesGroupId)
        bucket = expected_groups.setdefault(
            group_id,
            {
                "salesGroupLabel": clean_text(row.salesGroupLabel),
                "metersCount": 0,
                "amountTotalC": 0,
                "unitsTotal": Decimal("0.0"),
                "zeroSalesMetersCount": 0,
            },
        )
        bucket["metersCount"] += 1
        bucket["amountTotalC"] += int(row.amountTotalC)
        bucket["unitsTotal"] += Decimal(str(row.unitsTotal)).quantize(Decimal("0.1"))
        if int(row.amountTotalC) == 0:
            bucket["zeroSalesMetersCount"] += 1

    if set(groups["salesGroupId"]) != set(expected_groups):
        raise ValueError("monthly_lm_groups group-set reconciliation failed")
    actual_groups = groups.set_index("salesGroupId", drop=False)
    for group_id, expected_group in expected_groups.items():
        actual = actual_groups.loc[group_id]
        if clean_text(actual["salesGroupLabel"]) != expected_group["salesGroupLabel"]:
            raise ValueError(f"monthly_lm_groups label mismatch for {group_id}")
        for field in ("metersCount", "amountTotalC", "zeroSalesMetersCount"):
            if int(actual[field]) != int(expected_group[field]):
                raise ValueError(f"monthly_lm_groups reconciliation failed for {group_id}.{field}")
        actual_units = Decimal(str(actual["unitsTotal"])).quantize(Decimal("0.1"))
        if actual_units != expected_group["unitsTotal"].quantize(Decimal("0.1")):
            raise ValueError(f"monthly_lm_groups reconciliation failed for {group_id}.unitsTotal")

    if int(groups["metersCount"].sum()) != expected["metersCount"]:
        raise ValueError("monthly_lm_groups totals failed for metersCount")
    if int(groups["amountTotalC"].sum()) != amount_total:
        raise ValueError("monthly_lm_groups totals failed for amountTotalC")
    if decimal_units_sum(groups["unitsTotal"].tolist()) != units_total:
        raise ValueError("monthly_lm_groups totals failed for unitsTotal")
    if int(groups["zeroSalesMetersCount"].sum()) != zero_sales:
        raise ValueError("monthly_lm_groups totals failed for zeroSalesMetersCount")

    return expected


def reconcile_datasets(
    datasets: dict[str, MonthlyDataset], *, source_origin: str
) -> dict[str, Any]:
    if source_origin == SOURCE_ORIGIN_ATOMIC:
        return reconcile_atomic_datasets(datasets)
    if source_origin == SOURCE_ORIGIN_MONTHLY:
        return reconcile_monthly_source_datasets(datasets)
    raise ValueError(f"Unsupported sourceOrigin: {source_origin!r}")


def reconcile_atomic_datasets(datasets: dict[str, MonthlyDataset]) -> dict[str, int]:
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


def validate_atomic_source_manifest_evidence(
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


def validate_monthly_source_manifest_evidence(
    manifest: dict[str, Any],
    datasets: dict[str, MonthlyDataset],
    reconciliation: dict[str, Any],
    *,
    expected_lm_pcode: str,
    expected_month: str,
) -> dict[str, Any]:
    source_input = manifest.get("sourceInput")
    if not isinstance(source_input, dict):
        raise ValueError("Stage 03B manifest sourceInput must be an object")
    source_path = Path(clean_text(source_input.get("path"))).expanduser().resolve()
    if not source_path.is_file():
        raise ValueError(f"Stage 03B source input is missing: {source_path}")
    expected_source_sha = require_sha256(
        source_input.get("sha256"), label="Stage 03B sourceInput.sha256"
    )
    actual_source_sha = sha256_bytes(read_file_snapshot(source_path, label="Stage 03B source input"))
    if actual_source_sha != expected_source_sha:
        raise ValueError(
            "Stage 03B source input SHA-256 mismatch: "
            f"manifest={expected_source_sha}, actual={actual_source_sha}"
        )
    source_rows = require_json_int(
        source_input.get("rows"), label="Stage 03B sourceInput.rows", minimum=1
    )

    recorded = manifest.get("reconciliation")
    if not isinstance(recorded, dict):
        raise ValueError("Stage 03B manifest reconciliation must be an object")
    if set(recorded) != set(MONTHLY_SOURCE_RECONCILIATION_FIELDS):
        raise ValueError(
            "Stage 03B manifest reconciliation has an unexpected constitution"
        )
    expected_recorded = {
        "lmPcode": expected_lm_pcode,
        "month": expected_month,
        **reconciliation,
    }
    for field in MONTHLY_SOURCE_RECONCILIATION_FIELDS:
        if recorded.get(field) != expected_recorded[field]:
            raise ValueError(
                f"Stage 03B reconciliation {field} mismatch: "
                f"{recorded.get(field)!r} != {expected_recorded[field]!r}"
            )

    count_evidence = {
        "monthlyRows": len(datasets["monthly"].frame),
        "monthlyLmRows": len(datasets["monthly_lm"].frame),
        "monthlyLmGroupRows": len(datasets["monthly_lm_groups"].frame),
    }
    return {
        "verification": "PASS",
        "sourceOrigin": SOURCE_ORIGIN_MONTHLY,
        "provider": clean_text(manifest.get("provider")).lower(),
        "sourceInput": {
            "path": str(source_path),
            "sha256": actual_source_sha,
            "rows": source_rows,
        },
        "sourceFacts": manifest.get("sourceFacts"),
        "recordedReconciliation": expected_recorded,
        "countEvidence": count_evidence,
    }


def validate_manifest_evidence(
    manifest: dict[str, Any],
    datasets: dict[str, MonthlyDataset],
    reconciliation: dict[str, Any],
    *,
    expected_lm_pcode: str,
    expected_month: str,
    source_origin: str,
) -> dict[str, Any]:
    if source_origin == SOURCE_ORIGIN_ATOMIC:
        return validate_atomic_source_manifest_evidence(
            manifest,
            datasets,
            reconciliation,
            expected_lm_pcode=expected_lm_pcode,
            expected_month=expected_month,
        )
    if source_origin == SOURCE_ORIGIN_MONTHLY:
        return validate_monthly_source_manifest_evidence(
            manifest,
            datasets,
            reconciliation,
            expected_lm_pcode=expected_lm_pcode,
            expected_month=expected_month,
        )
    raise ValueError(f"Unsupported sourceOrigin: {source_origin!r}")


def row_to_document(dataset: str, row: pd.Series) -> dict[str, Any]:
    if "sourceOrigin" in row.index:
        integer_fields = {
            "y", "m", "metersCount", "amountTotalC", "zeroSalesMetersCount"
        }
        document: dict[str, Any] = {}
        for column in MONTHLY_SOURCE_DOCUMENT_COLUMNS[dataset]:
            value = row[column]
            if column in integer_fields:
                document[column] = int(value)
            elif column == "unitsTotal":
                document[column] = float(Decimal(str(value)).quantize(Decimal("0.1")))
            elif column == "sourceEndRow":
                document[column] = None if value is None or pd.isna(value) else int(value)
            else:
                document[column] = str(value)
        return document

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
    core = {
        "version": contract.get("version"),
        "projectId": contract.get("projectId"),
        "lmPcode": contract.get("lmPcode"),
        "month": contract.get("month"),
        "stage03ManifestSha256": contract.get("stage03ManifestSha256"),
        "datasets": contract.get("datasets"),
    }
    if contract.get("version") == 2:
        core["sourceOrigin"] = contract.get("sourceOrigin")
        core["provider"] = contract.get("provider")
    return core


def build_source_contract(
    *,
    project_id: str,
    lm_pcode: str,
    month: str,
    manifest_path: Path,
    manifest_sha256: str,
    datasets: dict[str, MonthlyDataset],
    source_origin: str,
    provider: str,
) -> dict[str, Any]:
    resolved_manifest = manifest_path.expanduser().resolve()
    require_sha256(manifest_sha256, label="Accepted source manifest sha256")

    contract_core: dict[str, Any] = {
        "version": 1 if source_origin == SOURCE_ORIGIN_ATOMIC else 2,
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
    if source_origin != SOURCE_ORIGIN_ATOMIC:
        contract_core["sourceOrigin"] = source_origin
        contract_core["provider"] = provider

    return {
        **contract_core,
        "stage03ManifestPath": str(resolved_manifest),
        "fingerprint": canonical_json_sha256(contract_core),
    }

def validate_mode_source_contract(mode: str, source_origin: str) -> None:
    if mode == "refresh" and source_origin != SOURCE_ORIGIN_MONTHLY:
        raise ValueError(
            "Stage 04 refresh is approved only for sourceOrigin=monthly_source; "
            "Atomic remains create-only/resume"
        )


def validate_resume_contract(
    args: argparse.Namespace,
    *,
    source_contract: dict[str, Any],
) -> Optional[dict[str, Any]]:
    if args.mode in {"create-only", "refresh"}:
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


def validate_provider_document(
    db: Any, provider_id: str, *, expected_provider: str, source_origin: str
) -> dict[str, Any]:
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
    if source_origin == SOURCE_ORIGIN_ATOMIC:
        if code != CONLOG_PROVIDER_CODE:
            raise ValueError("Provider document providerCode mismatch")
    elif code.lower() != expected_provider.lower():
        raise ValueError(
            f"Provider document providerCode mismatch: expected "
            f"{expected_provider.upper()!r}, found {code!r}"
        )
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


def is_valid_monthly_source_document_value(field_name: str, value: Any) -> bool:
    if field_name in MONTHLY_SOURCE_INTEGER_DOCUMENT_FIELDS:
        return type(value) is int
    if field_name == "unitsTotal":
        return type(value) is float
    if field_name == "sourceEndRow":
        return value is None or type(value) is int
    return type(value) is str


def classify_monthly_source_refresh_snapshot(
    dataset: MonthlyDataset,
    snapshot: Any,
    expected: dict[str, Any],
) -> tuple[str, Optional[PlannedUpdate], list[str]]:
    actual = snapshot.to_dict()
    if type(actual) is not dict:
        return "CONFLICT", None, ["<document>"]

    expected_fields = set(expected)
    actual_fields = set(actual)
    shape_drift = sorted(expected_fields ^ actual_fields)
    if shape_drift:
        return "CONFLICT", None, shape_drift

    invalid_types = sorted(
        field_name
        for field_name, actual_value in actual.items()
        if not is_valid_monthly_source_document_value(field_name, actual_value)
    )
    if invalid_types:
        return "CONFLICT", None, invalid_types

    immutable_fields = MONTHLY_SOURCE_REFRESH_IMMUTABLE_FIELDS[dataset.dataset]
    immutable_differences = sorted(
        field_name
        for field_name in immutable_fields
        if actual[field_name] != expected[field_name]
        or type(actual[field_name]) is not type(expected[field_name])
    )
    if immutable_differences:
        return "CONFLICT", None, immutable_differences

    mutable_fields = expected_fields - immutable_fields
    differing_fields = sorted(
        field_name
        for field_name in mutable_fields
        if actual[field_name] != expected[field_name]
        or type(actual[field_name]) is not type(expected[field_name])
    )
    if not differing_fields:
        return "UNCHANGED", None, []

    update_time = getattr(snapshot, "update_time", None)
    if update_time is None:
        return "CONFLICT", None, ["<updateTime>"]

    updates = {field_name: expected[field_name] for field_name in differing_fields}
    return (
        "UPDATED",
        PlannedUpdate(
            document_id=str(snapshot.id),
            updates=updates,
            update_time=update_time,
            differing_fields=tuple(differing_fields),
        ),
        differing_fields,
    )


def bulk_get_expected_snapshots(
    db: Any,
    dataset: MonthlyDataset,
    document_ids: list[str],
) -> tuple[dict[str, Any], int]:
    snapshots: dict[str, Any] = {}
    read_waves = 0
    for chunk in batched(document_ids, BATCH_SIZE):
        refs = [
            db.collection(dataset.collection).document(document_id)
            for document_id in chunk
        ]
        read_waves += 1
        for snapshot in db.get_all(refs):
            snapshots[str(snapshot.id)] = snapshot
    return snapshots, read_waves


def inspect_refresh_state(
    db: Any,
    dataset: MonthlyDataset,
    *,
    lm_pcode: str,
    month: str,
) -> ExistingState:
    if "sourceOrigin" not in dataset.frame.columns:
        raise ValueError("Stage 04 refresh is only approved for monthly_source inputs")

    scope_count = query_count(scope_query(db, dataset.collection, lm_pcode, month))
    frame_by_id = dataset.frame.set_index("docId", drop=False)
    expected_ids = sorted(frame_by_id.index.map(str).tolist())
    snapshots, read_waves = bulk_get_expected_snapshots(db, dataset, expected_ids)

    missing_ids: list[str] = []
    update_operations: list[PlannedUpdate] = []
    conflict_examples: list[dict[str, Any]] = []
    matching = 0
    conflicts = 0
    present_in_requested_scope = 0

    for document_id in expected_ids:
        snapshot = snapshots.get(document_id)
        if snapshot is None or not bool(getattr(snapshot, "exists", False)):
            missing_ids.append(document_id)
            continue

        actual = snapshot.to_dict() or {}
        if (
            type(actual) is dict
            and actual.get("lmPcode") == lm_pcode
            and actual.get("ym") == month
        ):
            present_in_requested_scope += 1

        expected = row_to_document(dataset.dataset, frame_by_id.loc[document_id])
        classification, update_operation, differing = (
            classify_monthly_source_refresh_snapshot(dataset, snapshot, expected)
        )
        if classification == "UNCHANGED":
            matching += 1
        elif classification == "UPDATED":
            if update_operation is None:
                raise RuntimeError("Refresh classification produced no update operation")
            update_operations.append(update_operation)
        else:
            conflicts += 1
            if len(conflict_examples) < 5:
                conflict_examples.append(
                    {
                        "docId": document_id,
                        "differingFields": differing,
                        "classification": "CONFLICT",
                    }
                )

    extra = scope_count - present_in_requested_scope
    if extra < 0:
        raise ValueError(
            f"Refresh scope accounting failed for {dataset.collection}: "
            f"scopeCount={scope_count}, expectedInScope={present_in_requested_scope}"
        )

    accounted = matching + len(update_operations) + conflicts + len(missing_ids)
    if accounted != len(expected_ids):
        raise ValueError(
            f"Refresh did not account for all {dataset.dataset} rows: "
            f"accounted={accounted}, expected={len(expected_ids)}"
        )

    return ExistingState(
        count=scope_count,
        matching=matching,
        missing=len(missing_ids),
        conflicts=conflicts,
        extra=extra,
        missing_ids=missing_ids,
        conflict_examples=conflict_examples,
        extra_examples=[],
        updated=len(update_operations),
        update_operations=update_operations,
        read_waves=read_waves,
    )


def inspect_existing_state(
    db: Any,
    dataset: MonthlyDataset,
    *,
    lm_pcode: str,
    month: str,
    mode: str,
) -> ExistingState:
    if mode == "refresh":
        return inspect_refresh_state(
            db,
            dataset,
            lm_pcode=lm_pcode,
            month=month,
        )

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


def last_update_option(update_time: Any) -> Any:
    try:
        from google.cloud.firestore_v1 import LastUpdateOption
    except ImportError as exc:
        raise RuntimeError(
            "google-cloud-firestore LastUpdateOption is required for Stage 04 refresh"
        ) from exc
    return LastUpdateOption(update_time)


def write_refresh_documents(
    db: Any,
    dataset: MonthlyDataset,
    state: ExistingState,
) -> dict[str, int]:
    frame_by_id = dataset.frame.set_index("docId", drop=False)
    planned: list[tuple[str, str, Optional[PlannedUpdate]]] = []
    planned.extend((document_id, "CREATE", None) for document_id in state.missing_ids)
    planned.extend(
        (operation.document_id, "UPDATE", operation)
        for operation in state.update_operations
    )
    planned.sort(key=lambda item: (item[0], item[1]))

    if not planned:
        return {
            "created": 0,
            "updated": 0,
            "committedBatches": 0,
            "writeOperationsAttempted": 0,
            "writeOperationsSucceeded": 0,
            "maximumWriteOperationsInAnyBatch": 0,
        }

    total_batches = (len(planned) + BATCH_SIZE - 1) // BATCH_SIZE
    created = 0
    updated = 0
    committed = 0
    maximum_batch = 0

    for number, chunk in enumerate(batched(planned, BATCH_SIZE), start=1):
        batch = db.batch()
        chunk_created = 0
        chunk_updated = 0
        for document_id, operation_type, update_operation in chunk:
            document_ref = db.collection(dataset.collection).document(document_id)
            if operation_type == "CREATE":
                row = frame_by_id.loc[document_id]
                batch.create(
                    document_ref,
                    row_to_document(dataset.dataset, row),
                )
                chunk_created += 1
                continue

            if update_operation is None:
                raise RuntimeError(
                    f"Missing refresh update operation for {dataset.collection}/{document_id}"
                )
            batch.update(
                document_ref,
                update_operation.updates,
                option=last_update_option(update_operation.update_time),
            )
            chunk_updated += 1

        batch.commit()
        committed += 1
        created += chunk_created
        updated += chunk_updated
        maximum_batch = max(maximum_batch, len(chunk))
        print(
            f"  - {dataset.collection} batch {number}/{total_batches}: "
            f"created {chunk_created:,}, updated {chunk_updated:,} "
            f"(writes {len(chunk):,})"
        )

    return {
        "created": created,
        "updated": updated,
        "committedBatches": committed,
        "writeOperationsAttempted": len(planned),
        "writeOperationsSucceeded": created + updated,
        "maximumWriteOperationsInAnyBatch": maximum_batch,
    }


def verify_refresh_post_upload(
    db: Any,
    dataset: MonthlyDataset,
    *,
    lm_pcode: str,
    month: str,
) -> dict[str, Any]:
    final_count = query_count(scope_query(db, dataset.collection, lm_pcode, month))
    expected_count = len(dataset.frame)
    if final_count != expected_count:
        raise ValueError(
            f"{dataset.collection} refresh count verification failed: "
            f"expected {expected_count}, found {final_count}"
        )

    frame_by_id = dataset.frame.set_index("docId", drop=False)
    expected_ids = sorted(frame_by_id.index.map(str).tolist())
    snapshots, read_waves = bulk_get_expected_snapshots(db, dataset, expected_ids)
    mismatch_examples: list[dict[str, Any]] = []
    missing_count = 0
    mismatch_count = 0

    for document_id in expected_ids:
        snapshot = snapshots.get(document_id)
        if snapshot is None or not bool(getattr(snapshot, "exists", False)):
            missing_count += 1
            if len(mismatch_examples) < 5:
                mismatch_examples.append(
                    {"docId": document_id, "differingFields": ["<missing>"]}
                )
            continue

        expected = row_to_document(dataset.dataset, frame_by_id.loc[document_id])
        actual = snapshot.to_dict() or {}
        differing = strict_document_differences(actual, expected)
        if differing:
            mismatch_count += 1
            if len(mismatch_examples) < 5:
                mismatch_examples.append(
                    {"docId": document_id, "differingFields": differing}
                )

    if missing_count or mismatch_count:
        raise ValueError(
            f"{dataset.collection} refresh verification failed: "
            f"missing={missing_count}, mismatched={mismatch_count}, "
            f"examples={mismatch_examples}"
        )

    return {
        "expectedCount": expected_count,
        "finalCount": final_count,
        "countVerification": "PASS",
        "fullDocumentVerification": "PASS",
        "documentsVerified": expected_count,
        "verificationReadWaves": read_waves,
        "mismatchCount": 0,
        "missingCount": 0,
        "mismatchExamples": [],
    }


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
    sample_ids = deterministic_sample_ids(dataset.frame)
    refs = [db.collection(dataset.collection).document(document_id) for document_id in sample_ids]
    snapshots = {snapshot.id: snapshot for snapshot in db.get_all(refs)}
    samples: list[dict[str, Any]] = []
    for document_id in sample_ids:
        snapshot = snapshots.get(document_id)
        if snapshot is None or not snapshot.exists:
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
    db = None
    created_counts = {dataset: 0 for dataset in COLLECTIONS}
    updated_counts = {dataset: 0 for dataset in COLLECTIONS}
    committed_counts = {dataset: 0 for dataset in COLLECTIONS}
    write_operations_attempted = {dataset: 0 for dataset in COLLECTIONS}
    write_operations_succeeded = {dataset: 0 for dataset in COLLECTIONS}
    maximum_batch_operations = {dataset: 0 for dataset in COLLECTIONS}

    try:
        validate_month(args.month)
        lm_pcode = clean_text(args.lm_pcode).upper()
        if not lm_pcode:
            raise ValueError("--lm-pcode cannot be blank")

        provider_id = clean_text(args.vending_provider_id)

        credential = validate_project_identity(args)
        manifest, selected, manifest_sha256 = load_manifest_outputs(
            args.manifest,
            expected_lm_pcode=lm_pcode,
            expected_month=args.month,
        )
        source_origin, source_provider = manifest_source_contract(manifest)
        if source_origin == SOURCE_ORIGIN_ATOMIC and provider_id != CONLOG_VENDING_PROVIDER_ID:
            raise ValueError(
                f"Atomic Stage 03 uploads remain governed for "
                f"{CONLOG_VENDING_PROVIDER_ID!r}"
            )
        validate_mode_source_contract(args.mode, source_origin)

        datasets = {
            dataset: validate_dataset(
                dataset,
                selected[dataset],
                expected_lm_pcode=lm_pcode,
                expected_month=args.month,
                source_origin=source_origin,
                expected_provider=source_provider,
            )
            for dataset in COLLECTIONS
        }
        reconciliation = reconcile_datasets(datasets, source_origin=source_origin)
        manifest_evidence = validate_manifest_evidence(
            manifest,
            datasets,
            reconciliation,
            expected_lm_pcode=lm_pcode,
            expected_month=args.month,
            source_origin=source_origin,
        )
        source_contract = build_source_contract(
            project_id=args.project_id,
            lm_pcode=lm_pcode,
            month=args.month,
            manifest_path=args.manifest,
            manifest_sha256=manifest_sha256,
            datasets=datasets,
            source_origin=source_origin,
            provider=source_provider,
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
                "sourceStage": clean_text(manifest.get("stage")),
                "sourceOrigin": source_origin,
                "sourceProvider": source_provider,
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
        provider = validate_provider_document(
            db,
            provider_id,
            expected_provider=source_provider,
            source_origin=source_origin,
        )
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
                "unchangedDocuments": state.matching,
                "documentsPlanned": state.missing + state.updated,
                "documentsPlannedCreate": state.missing,
                "documentsPlannedUpdate": state.updated,
                "preflightReadWaves": state.read_waves,
                "conflictCount": state.conflicts,
                "extraDocumentCount": state.extra,
                "conflictExamples": state.conflict_examples,
                "extraDocumentExamples": state.extra_examples,
            }
            for dataset, state in states.items()
        }
        report["firestoreBatching"] = {
            "firestoreBatchSize": BATCH_SIZE,
            "preflightReadWaves": {
                dataset: states[dataset].read_waves for dataset in COLLECTIONS
            },
            "writeWavesCommitted": {dataset: 0 for dataset in COLLECTIONS},
            "writeOperationsAttempted": {dataset: 0 for dataset in COLLECTIONS},
            "writeOperationsSucceeded": {dataset: 0 for dataset in COLLECTIONS},
            "maximumWriteOperationsInAnyBatch": {
                dataset: 0 for dataset in COLLECTIONS
            },
            "perDocumentFallback": False,
        }

        print("[STAGE 04] MONTHLY SALES CSVs -> FIRESTORE")
        print(f"  operation:          {report['operation']}")
        print(f"  mode:               {args.mode}")
        print(f"  target project:     {args.project_id}")
        print(f"  credential project: {credential.project_id}")
        print(f"  LM / month:         {lm_pcode} / {args.month}")
        print(f"  source manifest:    {args.manifest.expanduser().resolve()}")
        print(f"  source fingerprint: {source_contract['fingerprint']}")
        if recovery is not None:
            print(f"  resume report:      {recovery['reportPath']}")
            print("  resume contract:    PASS")
        print(f"  source origin:      {source_origin}")
        print(f"  source provider:    {source_provider}")
        print("  cross-reconciliation: PASS")
        for dataset, value in datasets.items():
            state = states[dataset]
            print(
                f"  {value.collection}: rows={len(value.frame):,}, "
                f"existing={state.count:,}, create={state.missing:,}, "
                f"update={state.updated:,}, unchanged={state.matching:,}, "
                f"conflicts={state.conflicts:,}, extra={state.extra:,}"
            )

        if args.mode == "refresh":
            blocked = {
                dataset: {
                    "conflicts": state.conflicts,
                    "extra": state.extra,
                }
                for dataset, state in states.items()
                if state.conflicts or state.extra
            }
            if blocked:
                raise ValueError(
                    "Stage 04 refresh blocked by conflicting or unexpected existing "
                    f"documents: {blocked}"
                )

        if args.preflight_only:
            report["status"] = "PASS"
            report["result"] = "PREFLIGHT_OK"
            print("\n[PREFLIGHT OK] No Monthly Sales documents were written.")
        else:
            if args.mode == "refresh":
                for dataset in ("monthly", "monthly_lm", "monthly_lm_groups"):
                    value = datasets[dataset]
                    write_result = write_refresh_documents(
                        db,
                        value,
                        states[dataset],
                    )
                    created_counts[dataset] = write_result["created"]
                    updated_counts[dataset] = write_result["updated"]
                    committed_counts[dataset] = write_result["committedBatches"]
                    write_operations_attempted[dataset] = write_result[
                        "writeOperationsAttempted"
                    ]
                    write_operations_succeeded[dataset] = write_result[
                        "writeOperationsSucceeded"
                    ]
                    maximum_batch_operations[dataset] = write_result[
                        "maximumWriteOperationsInAnyBatch"
                    ]

                verification = {
                    dataset: verify_refresh_post_upload(
                        db,
                        value,
                        lm_pcode=lm_pcode,
                        month=args.month,
                    )
                    for dataset, value in datasets.items()
                }
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
                    write_operations_attempted[dataset] = created
                    write_operations_succeeded[dataset] = created
                    maximum_batch_operations[dataset] = min(BATCH_SIZE, created)

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
                    "documentsUpdated": updated_counts,
                    "documentsUnchanged": {
                        dataset: states[dataset].matching for dataset in COLLECTIONS
                    },
                    "committedBatches": committed_counts,
                    "firestoreBatching": {
                        "firestoreBatchSize": BATCH_SIZE,
                        "preflightReadWaves": {
                            dataset: states[dataset].read_waves
                            for dataset in COLLECTIONS
                        },
                        "writeWavesCommitted": committed_counts,
                        "writeOperationsAttempted": write_operations_attempted,
                        "writeOperationsSucceeded": write_operations_succeeded,
                        "maximumWriteOperationsInAnyBatch": maximum_batch_operations,
                        "perDocumentFallback": False,
                    },
                    "verification": verification,
                    "status": "PASS",
                    "result": "UPLOAD_VERIFIED",
                }
            )
            if args.mode == "refresh":
                print(
                    "\n[VERIFY PASS] All three collection counts and full "
                    "monthly_source documents match."
                )
            else:
                print("\n[VERIFY PASS] All three collection counts and samples match.")
        return 0

    except Exception as exc:
        report.update(
            {
                "status": "FAIL",
                "result": "FAILED",
                "documentsCreated": created_counts,
                "documentsUpdated": updated_counts,
                "committedBatches": committed_counts,
                "firestoreBatching": {
                    "firestoreBatchSize": BATCH_SIZE,
                    "writeOperationsAttempted": write_operations_attempted,
                    "writeOperationsSucceeded": write_operations_succeeded,
                    "maximumWriteOperationsInAnyBatch": maximum_batch_operations,
                    "perDocumentFallback": False,
                },
                "errorType": type(exc).__name__,
                "error": str(exc),
            }
        )
        print(f"\n[FAILED] {exc}", file=sys.stderr)
        return 1

    finally:
        if db is not None:
            try:
                close_client = getattr(db, "close", None)
                if callable(close_client):
                    close_client()
                    print("[CLEANUP] Firestore client closed.")
            except Exception as close_error:
                print(
                    f"[WARN] Could not close Firestore client cleanly: {close_error}",
                    file=sys.stderr,
                )
        if firebase_admin_module is not None and firebase_app is not None:
            try:
                firebase_admin_module.delete_app(firebase_app)
                print("[CLEANUP] Firebase app deleted.")
            except Exception as delete_error:
                print(
                    f"[WARN] Could not delete Firebase app cleanly: {delete_error}",
                    file=sys.stderr,
                )
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
