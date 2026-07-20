"""
07_upload_meter_master_v3.py

Controlled uploader for one approved, frozen Stage 05 Meter Master build.

Safety contract
---------------
- explicit Firebase project, confirmation, service account, CSV, Stage 05 manifest and mode;
- the Stage 05 manifest and deterministic build fingerprint must be valid;
- the CSV SHA-256, row count, schema, LM, provider and meter type must match the manifest;
- Meter Master identities must already be canonical uppercase alphanumeric text;
- create-only is the first load against an empty collection;
- refresh transactionally creates missing documents or updates only governed sales-owned paths;
- resume requires the failed report from the exact original Stage 07 upload contract;
- resume creates only missing documents and rejects conflicts, null drift, invalid metadata,
  operational AST-link changes and unexpected extra documents;
- no merge, delete or silent overwrite; refresh updates use exact field paths;
- final collection count and deterministic document samples are verified;
- every run writes a JSON report.

Create-only example
-------------------
python .\\scripts\\07_upload_meter_master_v3.py `
  --project-id ireps-test `
  --confirm-project ireps-test `
  --service-account "C:\\dev\\secrets\\ireps-test-firebase-adminsdk-fbsvc-d02929e1e3.json" `
  --input .\\output\\meter_master\\meter_master__ZA7423__FULL__2025-09_to_2026-06.csv `
  --manifest .\\output\\meter_master\\meter_master__ZA7423__FULL__2025-09_to_2026-06.manifest.json `
  --mode create-only

Resume example
--------------
python .\\scripts\\07_upload_meter_master_v3.py `
  --project-id ireps-test `
  --confirm-project ireps-test `
  --service-account "C:\\dev\\secrets\\ireps-test-firebase-adminsdk-fbsvc-d02929e1e3.json" `
  --input .\\output\\meter_master\\meter_master__ZA7423__FULL__2025-09_to_2026-06.csv `
  --manifest .\\output\\meter_master\\meter_master__ZA7423__FULL__2025-09_to_2026-06.manifest.json `
  --mode resume `
  --resume-report <previous-failed-stage07-report.json>

Refresh example
---------------
Use the same frozen Stage 05 inputs and explicit project arguments as create-only,
with ``--mode refresh`` and no ``--resume-report``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence
from uuid import uuid4

from google.api_core import exceptions as google_exceptions
from google.cloud import firestore
from google.oauth2 import service_account
import pandas as pd
from tqdm import tqdm


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTION_NAME = "meter_master"
BATCH_SIZE = 450
SYSTEM_UID = "SYSTEM"
SYSTEM_USER = "METER MASTER PIPELINE"
GOVERNED_PROVIDER = "conlog"
GOVERNED_METER_TYPE = "electricity"
STAGE05_MANIFEST_SCHEMA_VERSION = 1
METER_ID_RE = re.compile(r"^[A-Z0-9]+$")
LM_PCODE_RE = re.compile(r"^[A-Z0-9_-]+$")

REQUIRED_COLUMNS = [
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

ALLOWED_TOP_LEVEL_FIELDS = {
    "lmPcode",
    "meterNo",
    "meterType",
    "customerNo",
    "accountNo",
    "refs",
    "metadata",
}
METADATA_FIELDS = {
    "createdAt",
    "createdByUid",
    "createdByUser",
    "updatedAt",
    "updatedByUid",
    "updatedByUser",
}


@dataclass(frozen=True)
class UploadConfig:
    project_id: str
    confirm_project: str
    service_account_path: Path
    input_path: Path
    manifest_path: Path
    mode: str
    resume_report_path: Optional[Path]
    report_dir: Path


@dataclass(frozen=True)
class PreflightResult:
    row_count: int
    unique_master_ids: int
    csv_sha256: str
    document_ids_sha256: str
    lm_pcodes: list[str]
    providers: list[str]
    meter_types: list[str]
    from_month: str
    to_month: str
    included_months: list[str]
    manifest_sha256: str
    stage05_build_fingerprint: str


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


@dataclass(frozen=True)
class RefreshDecision:
    classification: str
    code: str
    updates: dict[str, Any]
    evidence: dict[str, Any]
    write_attempted: bool = False
    write_succeeded: bool = False


@dataclass
class RefreshRunState:
    run_id: str
    rows_read: int
    records_inspected: int = 0
    created_count: int = 0
    updated_count: int = 0
    unchanged_count: int = 0
    conflict_count: int = 0
    failed_count: int = 0
    write_attempt_count: int = 0
    write_success_count: int = 0
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    successful_rows: list[dict[str, str]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "CREATED": self.created_count,
            "UPDATED": self.updated_count,
            "UNCHANGED": self.unchanged_count,
            "CONFLICT": self.conflict_count,
            "FAILED": self.failed_count,
        }


REFRESH_UPDATE_PATHS = (
    "customerNo",
    "accountNo",
    "refs.sales.id",
    "refs.sales.provider",
    "metadata.updatedAt",
    "metadata.updatedByUid",
    "metadata.updatedByUser",
)

SYSTEMIC_FIRESTORE_EXCEPTIONS = (
    google_exceptions.Unauthenticated,
    google_exceptions.PermissionDenied,
    google_exceptions.ServiceUnavailable,
    google_exceptions.DeadlineExceeded,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload one frozen Stage 05 Meter Master build to Firestore."
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
        help="Frozen Meter Master CSV generated by Stage 05.",
    )
    parser.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="Matching successful Stage 05 BUILD_WRITTEN manifest.",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("create-only", "refresh", "resume"),
        help="create-only is the first load; refresh is recurring; resume is restricted recovery.",
    )
    parser.add_argument(
        "--resume-report",
        type=Path,
        help="Previous failed Stage 07 JSON report. Required only for resume.",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path("output/logs/meter_master"),
        help="Directory for Stage 07 JSON reports.",
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


def month_sequence(first_month: str, last_month: str) -> list[str]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", first_month):
        raise ValueError(f"Invalid fromMonth in Stage 05 manifest: {first_month!r}")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", last_month):
        raise ValueError(f"Invalid toMonth in Stage 05 manifest: {last_month!r}")
    first = pd.Period(first_month, freq="M")
    last = pd.Period(last_month, freq="M")
    if first > last:
        raise ValueError("Stage 05 manifest fromMonth is later than toMonth")
    return [str(period) for period in pd.period_range(first, last, freq="M")]


def stage05_fingerprint_contract(manifest: Mapping[str, Any]) -> dict[str, Any]:
    source = manifest.get("sourceContract")
    output = manifest.get("outputContract")
    stats = manifest.get("stats")
    if not isinstance(source, Mapping):
        raise ValueError("Stage 05 manifest sourceContract is missing or invalid")
    if not isinstance(output, Mapping):
        raise ValueError("Stage 05 manifest outputContract is missing or invalid")
    if not isinstance(stats, Mapping):
        raise ValueError("Stage 05 manifest stats is missing or invalid")

    monthly_inputs = source.get("monthlyInputs")
    if not isinstance(monthly_inputs, list) or not monthly_inputs:
        raise ValueError("Stage 05 manifest monthlyInputs is missing or empty")

    customer_details = source.get("customerDetails")
    npr = source.get("npr")
    if not isinstance(customer_details, Mapping):
        raise ValueError("Stage 05 manifest customerDetails is invalid")
    if not isinstance(npr, Mapping):
        raise ValueError("Stage 05 manifest npr is invalid")

    return {
        "lmPcode": safe_str(source.get("lmPcode")),
        "scope": safe_str(source.get("scope")),
        "fromMonth": safe_str(source.get("fromMonth")),
        "toMonth": safe_str(source.get("toMonth")),
        "includedMonths": list(source.get("includedMonths") or []),
        "provider": safe_str(source.get("provider")),
        "meterType": safe_str(source.get("meterType")),
        "monthlyInputs": [
            {
                "month": safe_str(item.get("month")),
                "filename": safe_str(item.get("filename")),
                "rows": int(item.get("rows")),
                "sha256": safe_str(item.get("sha256")),
            }
            for item in monthly_inputs
            if isinstance(item, Mapping)
        ],
        "customerDetails": {
            "filename": safe_str(customer_details.get("filename")),
            "rows": int(customer_details.get("rows")),
            "sha256": safe_str(customer_details.get("sha256")),
        },
        "npr": {
            "filename": safe_str(npr.get("filename")),
            "rows": int(npr.get("rows")),
            "sha256": safe_str(npr.get("sha256")),
        },
        "output": {
            "filename": safe_str(output.get("filename")),
            "rows": int(output.get("rows")),
            "columns": list(output.get("columns") or []),
            "sha256": safe_str(output.get("sha256")),
        },
        "stats": dict(stats),
    }


def load_and_validate_csv(path: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    actual_columns = list(df.columns)
    if actual_columns != REQUIRED_COLUMNS:
        missing = [column for column in REQUIRED_COLUMNS if column not in actual_columns]
        unexpected = [column for column in actual_columns if column not in REQUIRED_COLUMNS]
        raise ValueError(
            "Meter Master staging columns do not match the approved contract. "
            f"Expected={REQUIRED_COLUMNS}; actual={actual_columns}; "
            f"missing={missing}; unexpected={unexpected}"
        )
    if df.empty:
        raise ValueError("Meter Master staging CSV contains zero rows")

    for column in REQUIRED_COLUMNS:
        df[column] = df[column].map(safe_str)

    required_nonblank = [
        "masterId",
        "lmPcode",
        "meterNoRaw",
        "meterNoNormalized",
        "meterType",
        "salesId",
        "salesProvider",
    ]
    for column in required_nonblank:
        blank = df[column].eq("")
        if blank.any():
            raise ValueError(f"CSV contains {int(blank.sum())} blank {column} value(s)")

    duplicate_ids = df["masterId"].duplicated(keep=False)
    if duplicate_ids.any():
        examples = sorted(df.loc[duplicate_ids, "masterId"].unique().tolist())[:10]
        raise ValueError(f"CSV contains duplicate masterId values. Examples: {examples}")

    for column in ("masterId", "meterNoNormalized", "salesId"):
        normalized = df[column].map(normalize_meter_id)
        noncanonical = df[column].ne(normalized) | ~normalized.map(
            lambda value: bool(METER_ID_RE.fullmatch(value))
        )
        if noncanonical.any():
            examples = df.loc[noncanonical, column].head(10).tolist()
            raise ValueError(
                f"{column} must already be canonical uppercase alphanumeric text. "
                f"Examples: {examples}"
            )

    if not df["masterId"].eq(df["meterNoNormalized"]).all():
        raise ValueError("masterId must equal meterNoNormalized for every row")
    if not df["salesId"].eq(df["meterNoNormalized"]).all():
        raise ValueError("salesId must equal meterNoNormalized for every row")

    lm_values = sorted(df["lmPcode"].unique().tolist())
    if len(lm_values) != 1:
        raise ValueError(f"Meter Master CSV must contain exactly one LM: {lm_values}")
    lm_value = lm_values[0]
    if lm_value != lm_value.upper() or not LM_PCODE_RE.fullmatch(lm_value):
        raise ValueError(f"Invalid canonical lmPcode: {lm_value!r}")

    if not df["salesProvider"].eq(GOVERNED_PROVIDER).all():
        examples = sorted(df.loc[~df["salesProvider"].eq(GOVERNED_PROVIDER), "salesProvider"].unique())[:10]
        raise ValueError(
            f"Stage 07 is governed only for salesProvider={GOVERNED_PROVIDER!r}. "
            f"Found: {examples}"
        )
    if not df["meterType"].eq(GOVERNED_METER_TYPE).all():
        examples = sorted(df.loc[~df["meterType"].eq(GOVERNED_METER_TYPE), "meterType"].unique())[:10]
        raise ValueError(
            f"Stage 07 is governed only for meterType={GOVERNED_METER_TYPE!r}. "
            f"Found: {examples}"
        )

    populated_ast = df["astId"].ne("")
    if populated_ast.any():
        examples = df.loc[populated_ast, ["masterId", "astId"]].head(10)
        raise ValueError(
            "Stage 07 pipeline input may not create operational AST links. "
            "astId must be blank in the frozen Stage 05 CSV. Examples:\n"
            + examples.to_string(index=False)
        )

    evidence = {
        "rowCount": len(df),
        "uniqueMasterIds": int(df["masterId"].nunique()),
        "csvSha256": sha256_file(path),
        "documentIdsSha256": document_ids_sha256(df["masterId"].tolist()),
        "lmPcodes": lm_values,
        "providers": sorted(df["salesProvider"].unique().tolist()),
        "meterTypes": sorted(df["meterType"].unique().tolist()),
    }
    return df, evidence


def validate_stage05_manifest(
    manifest_path: Path,
    input_path: Path,
    csv_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = read_json(manifest_path, "Stage 05 manifest")
    if manifest.get("schemaVersion") != STAGE05_MANIFEST_SCHEMA_VERSION:
        raise ValueError("Unsupported Stage 05 manifest schemaVersion")
    if safe_str(manifest.get("stage")) != "05":
        raise ValueError("Manifest is not a Stage 05 manifest")
    if safe_str(manifest.get("script")) != "05_build_meter_master_v3.py":
        raise ValueError("Stage 05 manifest script identity mismatch")
    if safe_str(manifest.get("status")) != "PASS":
        raise ValueError("Stage 05 manifest status must be PASS")
    if safe_str(manifest.get("result")) != "BUILD_WRITTEN":
        raise ValueError("Stage 05 manifest result must be BUILD_WRITTEN")

    fingerprint_contract = stage05_fingerprint_contract(manifest)
    expected_fingerprint = canonical_json_sha256(fingerprint_contract)
    recorded_fingerprint = safe_str(manifest.get("buildFingerprint"))
    if recorded_fingerprint != expected_fingerprint:
        raise ValueError(
            "Stage 05 manifest buildFingerprint is invalid; the manifest may be edited or corrupt"
        )

    source = manifest["sourceContract"]
    output = manifest["outputContract"]
    included_months = list(source.get("includedMonths") or [])
    from_month = safe_str(source.get("fromMonth"))
    to_month = safe_str(source.get("toMonth"))
    expected_months = month_sequence(from_month, to_month)
    if included_months != expected_months:
        raise ValueError(
            f"Stage 05 manifest includedMonths are not complete: "
            f"found={included_months}, expected={expected_months}"
        )

    if safe_str(source.get("provider")) != GOVERNED_PROVIDER:
        raise ValueError("Stage 05 manifest provider is not governed conlog")
    if safe_str(source.get("meterType")) != GOVERNED_METER_TYPE:
        raise ValueError("Stage 05 manifest meterType is not governed electricity")
    if safe_str(source.get("lmPcode")) not in csv_evidence["lmPcodes"]:
        raise ValueError("Stage 05 manifest LM does not match the CSV")

    if list(output.get("columns") or []) != REQUIRED_COLUMNS:
        raise ValueError("Stage 05 manifest output columns do not match Stage 07 contract")
    if int(output.get("rows", -1)) != int(csv_evidence["rowCount"]):
        raise ValueError("Stage 05 manifest output row count does not match the CSV")
    if safe_str(output.get("sha256")) != csv_evidence["csvSha256"]:
        raise ValueError("Stage 05 manifest output SHA-256 does not match the CSV")
    if safe_str(output.get("filename")) != input_path.name:
        raise ValueError("Stage 05 manifest output filename does not match --input")

    return {
        "manifestSha256": sha256_file(manifest_path),
        "stage05BuildFingerprint": recorded_fingerprint,
        "fromMonth": from_month,
        "toMonth": to_month,
        "includedMonths": included_months,
        "lmPcode": safe_str(source.get("lmPcode")),
        "scope": safe_str(source.get("scope")),
    }


def dataframe_rows(df: pd.DataFrame) -> list[dict[str, str]]:
    return [
        {column: safe_str(row.get(column)) for column in REQUIRED_COLUMNS}
        for _, row in df.iterrows()
    ]


def build_create_doc(row: Mapping[str, Any], timestamp: datetime) -> dict[str, Any]:
    return {
        "lmPcode": str(row["lmPcode"]),
        "meterNo": {
            "raw": str(row["meterNoRaw"]),
            "normalized": str(row["meterNoNormalized"]),
        },
        "meterType": str(row["meterType"]),
        "customerNo": str(row["customerNo"]),
        "accountNo": str(row["accountNo"]),
        "refs": {
            "asts": {"id": str(row["astId"])},
            "sales": {
                "id": str(row["salesId"]),
                "provider": str(row["salesProvider"]),
            },
        },
        "metadata": {
            "createdAt": timestamp,
            "createdByUid": SYSTEM_UID,
            "createdByUser": SYSTEM_USER,
            "updatedAt": timestamp,
            "updatedByUid": SYSTEM_UID,
            "updatedByUser": SYSTEM_USER,
        },
    }


def nested_get(payload: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = payload
    for part in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def refresh_conflict(
    code: str,
    doc_id: str,
    row: Mapping[str, Any],
    existing: Any,
    detail: str,
    conflicting_paths: Optional[Sequence[str]] = None,
) -> RefreshDecision:
    return RefreshDecision(
        "CONFLICT",
        code,
        {},
        {
            "masterId": doc_id,
            "detail": detail,
            "conflictingPaths": list(conflicting_paths or []),
            "incoming": {key: safe_str(row.get(key)) for key in REQUIRED_COLUMNS},
            "existing": existing,
        },
    )


def classify_refresh_document(
    doc_id: str,
    existing: Any,
    row: Mapping[str, Any],
    timestamp: datetime,
) -> RefreshDecision:
    """Classify one transactionally re-read document and build exact-path updates."""
    if normalize_meter_id(doc_id) != doc_id or not METER_ID_RE.fullmatch(doc_id):
        return refresh_conflict(
            "MM_DOCUMENT_ID_NONCANONICAL", doc_id, row, existing, "document ID is not canonical"
        )
    if doc_id != safe_str(row.get("masterId")):
        return refresh_conflict(
            "MM_DOCUMENT_ID_MISMATCH", doc_id, row, existing, "document ID differs from incoming masterId"
        )
    if not isinstance(existing, Mapping):
        return refresh_conflict(
            "MM_DOCUMENT_SHAPE_UNSAFE", doc_id, row, existing, "document is not an object"
        )

    shape_contract = (
        ((), ALLOWED_TOP_LEVEL_FIELDS),
        (("meterNo",), {"raw", "normalized"}),
        (("refs",), {"asts", "sales"}),
        (("refs", "asts"), {"id"}),
        (("refs", "sales"), {"id", "provider"}),
        (("metadata",), METADATA_FIELDS),
    )
    for path, expected_keys in shape_contract:
        value = existing if not path else nested_get(existing, path)
        label = ".".join(path) or "root"
        if not isinstance(value, Mapping):
            return refresh_conflict(
                "MM_DOCUMENT_SHAPE_UNSAFE", doc_id, row, existing,
                f"{label} must be an object", [label],
            )
        actual_keys = set(value)
        missing = sorted(expected_keys - actual_keys)
        extra = sorted(actual_keys - expected_keys)
        if missing:
            return refresh_conflict(
                "MM_CANONICAL_FIELD_MISSING", doc_id, row, existing,
                f"{label} missing fields: {missing}",
                [f"{label}.{name}" if path else name for name in missing],
            )
        if extra:
            return refresh_conflict(
                "MM_DOCUMENT_SHAPE_UNSAFE", doc_id, row, existing,
                f"{label} has prohibited fields: {extra}",
                [f"{label}.{name}" if path else name for name in extra],
            )

    meter_no = existing["meterNo"]
    refs = existing["refs"]
    metadata = existing["metadata"]
    asts = refs["asts"]
    sales = refs["sales"]
    if not isinstance(meter_no, Mapping):
        return refresh_conflict(
            "MM_DOCUMENT_SHAPE_UNSAFE", doc_id, row, existing, "meterNo must be an object"
        )

    string_paths = (
        ("lmPcode",), ("meterNo", "raw"), ("meterNo", "normalized"), ("meterType",),
        ("customerNo",), ("accountNo",),
        ("refs", "sales", "id"), ("refs", "sales", "provider"),
        ("metadata", "createdByUid"), ("metadata", "createdByUser"),
        ("metadata", "updatedByUid"), ("metadata", "updatedByUser"),
    )
    wrong_types = [
        ".".join(path) for path in string_paths
        if not isinstance(nested_get(existing, path), str)
    ]
    if not isinstance(asts["id"], str):
        return refresh_conflict(
            "MM_AST_REFERENCE_CONFLICT", doc_id, row, existing,
            f"refs.asts.id must be a string, found {type(asts['id']).__name__}",
        )
    for field in ("createdAt", "updatedAt"):
        value = metadata.get(field)
        if not isinstance(value, datetime) or value.tzinfo is None:
            wrong_types.append(f"metadata.{field}")
    if wrong_types:
        return refresh_conflict(
            "MM_GOVERNED_FIELD_TYPE_INVALID", doc_id, row, existing,
            f"invalid field types: {wrong_types}", wrong_types,
        )
    if not metadata["createdByUid"].strip() or not metadata["createdByUser"].strip():
        return refresh_conflict(
            "MM_CREATED_METADATA_INVALID", doc_id, row, existing, "creation actor metadata is blank"
        )
    if meter_no["normalized"] != doc_id:
        return refresh_conflict(
            "MM_NORMALIZED_IDENTITY_CONFLICT", doc_id, row, existing,
            f"meterNo.normalized={meter_no['normalized']!r}",
        )
    if not meter_no["raw"].strip():
        return refresh_conflict(
            "MM_CANONICAL_FIELD_MISSING", doc_id, row, existing, "meterNo.raw is blank"
        )
    if meter_no["raw"] != safe_str(row.get("meterNoRaw")):
        return refresh_conflict(
            "MM_RAW_IDENTITY_CONFLICT", doc_id, row, existing,
            f"meterNo.raw={meter_no['raw']!r} incoming={safe_str(row.get('meterNoRaw'))!r}",
            ["meterNo.raw"],
        )
    if existing["lmPcode"] != safe_str(row.get("lmPcode")):
        return refresh_conflict("MM_LM_CONFLICT", doc_id, row, existing, "lmPcode differs")
    if existing["meterType"] != safe_str(row.get("meterType")):
        return refresh_conflict("MM_METER_TYPE_CONFLICT", doc_id, row, existing, "meterType differs")
    incoming_sales_id = safe_str(row.get("salesId"))
    incoming_provider = safe_str(row.get("salesProvider"))
    if sales["id"] and sales["id"] != incoming_sales_id:
        return refresh_conflict("MM_SALES_REFERENCE_CONFLICT", doc_id, row, existing, "refs.sales.id differs")
    if sales["provider"] and sales["provider"] != incoming_provider:
        return refresh_conflict("MM_SALES_PROVIDER_CONFLICT", doc_id, row, existing, "refs.sales.provider differs")
    if bool(sales["id"]) != bool(sales["provider"]):
        return refresh_conflict(
            "MM_SALES_REFERENCE_PAIR_CONFLICT", doc_id, row, existing, "sales reference is only partially populated"
        )

    desired = {
        "customerNo": safe_str(row.get("customerNo")) or existing["customerNo"],
        "accountNo": safe_str(row.get("accountNo")) or existing["accountNo"],
        "refs.sales.id": incoming_sales_id or sales["id"],
        "refs.sales.provider": incoming_provider or sales["provider"],
    }
    updates = {
        path: value for path, value in desired.items()
        if nested_get(existing, tuple(path.split("."))) != value
    }
    if not updates:
        return RefreshDecision(
            "UNCHANGED", "MM_VALUES_UNCHANGED", {},
            {"masterId": doc_id, "expectedAstId": asts["id"]},
        )
    updates.update(
        {
            "metadata.updatedAt": timestamp,
            "metadata.updatedByUid": SYSTEM_UID,
            "metadata.updatedByUser": SYSTEM_USER,
        }
    )
    return RefreshDecision(
        "UPDATED", "MM_SALES_FIELDS_UPDATED", updates,
        {"masterId": doc_id, "updatedPaths": sorted(updates), "expectedAstId": asts["id"]},
    )


def compare_string_field(
    differences: list[str],
    existing: Mapping[str, Any],
    path: Sequence[str],
    expected: str,
) -> None:
    actual = nested_get(existing, path)
    label = ".".join(path)
    if not isinstance(actual, str):
        differences.append(f"{label} must be a string, found {type(actual).__name__}")
    elif actual != expected:
        differences.append(f"{label} existing={actual!r} expected={expected!r}")


def compare_existing_document(
    existing: Mapping[str, Any], row: Mapping[str, Any]
) -> list[str]:
    """Strictly verify one pipeline-created Meter Master document during recovery."""
    differences: list[str] = []

    actual_top = set(existing.keys())
    missing_top = sorted(ALLOWED_TOP_LEVEL_FIELDS - actual_top)
    extra_top = sorted(actual_top - ALLOWED_TOP_LEVEL_FIELDS)
    if missing_top:
        differences.append(f"missing top-level fields: {missing_top}")
    if extra_top:
        differences.append(f"unexpected top-level fields: {extra_top}")

    meter_no = existing.get("meterNo")
    if not isinstance(meter_no, Mapping) or set(meter_no.keys()) != {"raw", "normalized"}:
        differences.append("meterNo must contain exactly raw and normalized")

    refs = existing.get("refs")
    if not isinstance(refs, Mapping) or set(refs.keys()) != {"asts", "sales"}:
        differences.append("refs must contain exactly asts and sales")
    else:
        asts = refs.get("asts")
        sales = refs.get("sales")
        if not isinstance(asts, Mapping) or set(asts.keys()) != {"id"}:
            differences.append("refs.asts must contain exactly id")
        if not isinstance(sales, Mapping) or set(sales.keys()) != {"id", "provider"}:
            differences.append("refs.sales must contain exactly id and provider")

    expected_strings = {
        ("lmPcode",): str(row["lmPcode"]),
        ("meterNo", "raw"): str(row["meterNoRaw"]),
        ("meterNo", "normalized"): str(row["meterNoNormalized"]),
        ("meterType",): str(row["meterType"]),
        ("customerNo",): str(row["customerNo"]),
        ("accountNo",): str(row["accountNo"]),
        ("refs", "asts", "id"): str(row["astId"]),
        ("refs", "sales", "id"): str(row["salesId"]),
        ("refs", "sales", "provider"): str(row["salesProvider"]),
    }
    for path, expected in expected_strings.items():
        compare_string_field(differences, existing, path, expected)

    metadata = existing.get("metadata")
    if not isinstance(metadata, Mapping):
        differences.append("metadata is missing or is not an object")
    else:
        metadata_keys = set(metadata.keys())
        missing_metadata = sorted(METADATA_FIELDS - metadata_keys)
        extra_metadata = sorted(metadata_keys - METADATA_FIELDS)
        if missing_metadata:
            differences.append(f"metadata missing fields: {missing_metadata}")
        if extra_metadata:
            differences.append(f"metadata has unexpected fields: {extra_metadata}")

        created_at = metadata.get("createdAt")
        updated_at = metadata.get("updatedAt")
        for field, value in (("createdAt", created_at), ("updatedAt", updated_at)):
            if not isinstance(value, datetime):
                differences.append(
                    f"metadata.{field} must be a Firestore Timestamp/datetime, "
                    f"found {type(value).__name__}"
                )
            elif value.tzinfo is None:
                differences.append(f"metadata.{field} must be timezone-aware")
        if isinstance(created_at, datetime) and isinstance(updated_at, datetime):
            if created_at != updated_at:
                differences.append(
                    "metadata.updatedAt differs from createdAt; document changed after pipeline creation"
                )

        actor_values = {
            "createdByUid": SYSTEM_UID,
            "createdByUser": SYSTEM_USER,
            "updatedByUid": SYSTEM_UID,
            "updatedByUser": SYSTEM_USER,
        }
        for field, expected in actor_values.items():
            actual = metadata.get(field)
            if not isinstance(actual, str):
                differences.append(
                    f"metadata.{field} must be a string, found {type(actual).__name__}"
                )
            elif actual != expected:
                differences.append(
                    f"metadata.{field} existing={actual!r} expected={expected!r}"
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
    timestamp: datetime,
    progress: UploadProgress,
) -> None:
    row_batches = list(chunks(rows, BATCH_SIZE))
    for row_batch in tqdm(
        row_batches,
        desc="Writing Meter Master batches",
        unit="batch",
    ):
        batch = db.batch()
        for row in row_batch:
            doc_id = str(row["masterId"])
            doc_ref = db.collection(COLLECTION_NAME).document(doc_id)
            batch.create(doc_ref, build_create_doc(row, timestamp))
        batch.commit()
        progress.committed_batches += 1
        progress.documents_created += len(row_batch)


def refresh_one_document(
    db: firestore.Client,
    row: Mapping[str, Any],
    timestamp: datetime,
) -> RefreshDecision:
    """Atomically re-read, reclassify and conditionally write one refresh row."""
    doc_id = str(row["masterId"])
    doc_ref = db.collection(COLLECTION_NAME).document(doc_id)
    transaction = db.transaction()
    write_state = {"attempted": False}

    @firestore.transactional
    def apply(transaction: Any) -> RefreshDecision:
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            write_state["attempted"] = True
            transaction.create(doc_ref, build_create_doc(row, timestamp))
            return RefreshDecision(
                "CREATED", "MM_DOCUMENT_CREATED", {},
                {"masterId": doc_id, "expectedAstId": ""}, True, True,
            )
        decision = classify_refresh_document(doc_id, snapshot.to_dict(), row, timestamp)
        if decision.classification == "UPDATED":
            write_state["attempted"] = True
            transaction.update(doc_ref, decision.updates)
            return RefreshDecision(
                decision.classification, decision.code, decision.updates,
                decision.evidence, True, True,
            )
        return decision

    try:
        return apply(transaction)
    except google_exceptions.AlreadyExists as exc:
        return RefreshDecision(
            "CONFLICT", "MM_TRANSACTION_PRECONDITION_CHANGED", {},
            {"masterId": doc_id, "errorType": type(exc).__name__, "error": str(exc)},
            write_state["attempted"], False,
        )
    except google_exceptions.Aborted as exc:
        return RefreshDecision(
            "CONFLICT", "MM_TRANSACTION_PRECONDITION_CHANGED", {},
            {"masterId": doc_id, "errorType": type(exc).__name__, "error": str(exc)},
            write_state["attempted"], False,
        )
    except SYSTEMIC_FIRESTORE_EXCEPTIONS:
        raise
    except Exception as exc:
        return RefreshDecision(
            "FAILED", "MM_RECORD_WRITE_FAILED", {},
            {"masterId": doc_id, "errorType": type(exc).__name__, "error": str(exc)},
            write_state["attempted"], False,
        )


def refresh_documents(
    db: firestore.Client,
    rows: Sequence[Mapping[str, Any]],
    timestamp: datetime,
    state: RefreshRunState,
) -> RefreshRunState:
    total = len(rows)
    for processed, row in enumerate(rows, start=1):
        decision = refresh_one_document(db, row, timestamp)
        state.records_inspected += 1
        counter_name = f"{decision.classification.lower()}_count"
        setattr(state, counter_name, getattr(state, counter_name) + 1)
        state.write_attempt_count += int(decision.write_attempted)
        state.write_success_count += int(decision.write_succeeded)
        source_row = {key: safe_str(row.get(key)) for key in REQUIRED_COLUMNS}
        detail = {
            "runId": state.run_id,
            "masterId": safe_str(row.get("masterId")),
            "lmPcode": safe_str(row.get("lmPcode")),
            "sourceRow": source_row,
            "code": decision.code,
            "conflictingPaths": decision.evidence.get("conflictingPaths", []),
            "existingValues": decision.evidence.get("existing"),
            "incomingValues": decision.evidence.get("incoming", source_row),
            "message": decision.evidence.get("detail", decision.evidence.get("error", decision.code)),
            "detectedAt": datetime.now(UTC).isoformat(),
            "writeAttempted": decision.write_attempted,
            "investigationRecommendation": (
                "Review the existing Meter Master document and approved Stage 05 source; "
                "do not overwrite operational fields manually."
            ),
            **decision.evidence,
        }
        if decision.classification == "CONFLICT":
            state.conflicts.append(detail)
        elif decision.classification == "FAILED":
            state.failures.append(detail)
        else:
            successful_row = dict(source_row)
            successful_row["_expectedAstId"] = safe_str(decision.evidence.get("expectedAstId"))
            state.successful_rows.append(successful_row)
        counts = state.counts()
        if processed == 1 or processed % 100 == 0 or processed == total:
            print(
                f"Refresh progress: {processed:,}/{total:,} inspected; "
                f"created={counts['CREATED']:,}, updated={counts['UPDATED']:,}, "
                f"unchanged={counts['UNCHANGED']:,}, conflicts={counts['CONFLICT']:,}, "
                f"failed={counts['FAILED']:,}"
            )
    return state


def build_resume_plan(
    db: firestore.Client,
    rows: Sequence[dict[str, str]],
) -> ResumePlan:
    missing_rows: list[dict[str, str]] = []
    conflicts: list[dict[str, Any]] = []
    matching_count = 0

    row_by_id = {row["masterId"]: row for row in rows}
    ids = sorted(row_by_id.keys())

    for id_batch in tqdm(
        list(chunks(ids, BATCH_SIZE)),
        desc="Checking existing Meter Master documents",
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

            differences = compare_existing_document(snapshot.to_dict() or {}, row)
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
                    "document exists in Firestore but is absent from the frozen Stage 05 CSV"
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
        differences = compare_existing_document(snapshot.to_dict() or {}, row_by_id[doc_id])
        samples.append({"masterId": doc_id, "matches": not differences})
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


def refresh_state_report_fields(state: RefreshRunState) -> dict[str, Any]:
    return {
        "runId": state.run_id,
        "rowsRead": state.rows_read,
        "recordsInspected": state.records_inspected,
        "createdCount": state.created_count,
        "updatedCount": state.updated_count,
        "unchangedCount": state.unchanged_count,
        "conflictCount": state.conflict_count,
        "failedCount": state.failed_count,
        "writeAttemptCount": state.write_attempt_count,
        "writeSuccessCount": state.write_success_count,
        "classificationCounts": state.counts(),
        "conflicts": state.conflicts,
        "failedRecords": state.failures,
    }


def validate_refresh_accounting(state: RefreshRunState) -> dict[str, Any]:
    classified = sum(state.counts().values())
    balanced = classified == state.rows_read == state.records_inspected
    writes_balanced = (
        state.write_success_count == state.created_count + state.updated_count
        and state.write_attempt_count >= state.write_success_count
    )
    evidence = {
        "rowsRead": state.rows_read,
        "classifiedCount": classified,
        "recordsInspected": state.records_inspected,
        "balanced": balanced,
        "writesBalanced": writes_balanced,
    }
    if not balanced or not writes_balanced:
        raise RuntimeError(f"Refresh final accounting failed: {evidence}")
    return evidence


def verify_refresh_post_write(
    db: firestore.Client,
    successful_rows: Sequence[Mapping[str, Any]],
    collection_count_before: int,
    collection_count_after: int,
    created_count: int,
) -> dict[str, Any]:
    expected_count = collection_count_before + created_count
    if collection_count_after < collection_count_before:
        raise RuntimeError("Refresh verification failed: collection count decreased")
    if collection_count_after < expected_count:
        raise RuntimeError(
            "Refresh verification failed: successful creates are not reflected in collection count"
        )

    row_by_id = {safe_str(item.get("masterId")): item for item in successful_rows}
    sample_ids = deterministic_sample_ids(list(row_by_id))
    samples: list[dict[str, Any]] = []
    for doc_id in sample_ids:
        snapshot = db.collection(COLLECTION_NAME).document(doc_id).get()
        if not snapshot.exists:
            raise RuntimeError(f"Refresh verification failed: missing sample {doc_id}")
        payload = snapshot.to_dict()
        decision = classify_refresh_document(doc_id, payload, row_by_id[doc_id], datetime.now(UTC))
        if decision.classification != "UNCHANGED":
            raise RuntimeError(
                f"Refresh verification failed for {doc_id}: "
                f"{decision.classification}/{decision.code}"
            )
        expected_ast = safe_str(row_by_id[doc_id].get("_expectedAstId"))
        actual_ast = nested_get(payload, ("refs", "asts", "id"))
        if actual_ast != expected_ast:
            raise RuntimeError(
                f"Refresh verification failed: AST reference changed for {doc_id}"
            )
        samples.append(
            {"masterId": doc_id, "exists": True, "classification": "UNCHANGED", "astPreserved": True}
        )

    return {
        "status": "PASS",
        "collectionCountBefore": collection_count_before,
        "collectionCountAfter": collection_count_after,
        "expectedCountFromCreates": expected_count,
        "concurrentCountAnomaly": collection_count_after > expected_count,
        "sampleIds": sample_ids,
        "samples": samples,
    }


def make_conflict_report_path(report_dir: Path, project_id: str, run_id: str) -> Path:
    return report_dir / f"meter_master_refresh_conflicts__{project_id}__{run_id}.json"


def write_refresh_conflict_report(
    path: Path,
    state: RefreshRunState,
    project_id: str,
) -> None:
    write_report(
        path,
        {
            "stage": "07",
            "script": "07_upload_meter_master_v3.py",
            "operation": "meter_master_refresh_conflicts",
            "runId": state.run_id,
            "projectId": project_id,
            "conflictCount": state.conflict_count,
            "failedCount": state.failed_count,
            "records": [*state.conflicts, *state.failures],
            "writtenAt": datetime.now(UTC).isoformat(),
        },
    )


def governed_refresh_result(state: RefreshRunState) -> str:
    return (
        "COMPLETED_WITH_CONFLICTS"
        if state.conflict_count or state.failed_count
        else "COMPLETED"
    )


def complete_refresh_report(
    report: dict[str, Any],
    state: RefreshRunState,
    accounting: Mapping[str, Any],
    verification: Mapping[str, Any],
) -> None:
    report.update(refresh_state_report_fields(state))
    report.update(
        {
            "accounting": dict(accounting),
            "verification": dict(verification),
            "status": "PASS",
            "result": governed_refresh_result(state),
        }
    )


def fail_refresh_report(
    report: dict[str, Any], state: RefreshRunState, exc: Exception
) -> None:
    report.update(refresh_state_report_fields(state))
    report.update(
        {
            "status": "FAIL",
            "result": "FAILED",
            "errorType": type(exc).__name__,
            "error": str(exc),
        }
    )


def make_upload_contract(
    config: UploadConfig,
    preflight: PreflightResult,
) -> dict[str, Any]:
    return {
        "projectId": config.project_id,
        "collection": COLLECTION_NAME,
        "manifestFilename": config.manifest_path.name,
        "manifestSha256": preflight.manifest_sha256,
        "stage05BuildFingerprint": preflight.stage05_build_fingerprint,
        "csvFilename": config.input_path.name,
        "csvSha256": preflight.csv_sha256,
        "rows": preflight.row_count,
        "documentIdsSha256": preflight.document_ids_sha256,
        "lmPcodes": preflight.lm_pcodes,
        "providers": preflight.providers,
        "meterTypes": preflight.meter_types,
        "fromMonth": preflight.from_month,
        "toMonth": preflight.to_month,
        "includedMonths": preflight.included_months,
    }


def validate_resume_report(
    path: Path,
    current_contract: Mapping[str, Any],
    current_fingerprint: str,
) -> dict[str, Any]:
    previous = read_json(path, "Previous failed Stage 07 report")
    if safe_str(previous.get("stage")) != "07":
        raise ValueError("--resume-report is not a Stage 07 report")
    if safe_str(previous.get("script")) != "07_upload_meter_master_v3.py":
        raise ValueError("--resume-report script identity mismatch")
    if safe_str(previous.get("operation")) != "meter_master_upload":
        raise ValueError("--resume-report operation identity mismatch")
    if safe_str(previous.get("status")) != "FAIL" or safe_str(previous.get("result")) != "FAILED":
        raise ValueError("--resume-report must be from a failed Stage 07 upload")

    previous_contract = previous.get("uploadContract")
    if not isinstance(previous_contract, Mapping):
        raise ValueError("--resume-report has no uploadContract")
    recorded_fingerprint = safe_str(previous.get("uploadFingerprint"))
    recalculated_previous = canonical_json_sha256(previous_contract)
    if recorded_fingerprint != recalculated_previous:
        raise ValueError("--resume-report fingerprint is invalid; the report may be edited or corrupt")
    if dict(previous_contract) != dict(current_contract):
        raise ValueError(
            "Resume blocked: current project, Stage 05 manifest, CSV, row set or range "
            "does not match the failed original Stage 07 upload"
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
    return report_dir / f"meter_master_upload__{project_id}__{timestamp}.json"


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

    if args.mode == "resume" and args.resume_report is None:
        raise ValueError("--mode resume requires --resume-report")
    if args.mode != "resume" and args.resume_report is not None:
        raise ValueError("--resume-report may be used only with --mode resume")

    return UploadConfig(
        project_id=project_id,
        confirm_project=confirm_project,
        service_account_path=resolve_project_path(args.service_account),
        input_path=resolve_project_path(args.input),
        manifest_path=resolve_project_path(args.manifest),
        mode=args.mode,
        resume_report_path=(
            resolve_project_path(args.resume_report) if args.resume_report is not None else None
        ),
        report_dir=resolve_project_path(args.report_dir),
    )


def print_preflight(config: UploadConfig, result: PreflightResult) -> None:
    print("\n=== METER MASTER UPLOAD PREFLIGHT ===")
    print(f"Target project:       {config.project_id}")
    print(f"Collection:           {COLLECTION_NAME}")
    print(f"Mode:                 {config.mode}")
    print(f"Input CSV:            {config.input_path}")
    print(f"Stage 05 manifest:    {config.manifest_path}")
    print(f"Rows:                 {result.row_count:,}")
    print(f"Unique master IDs:    {result.unique_master_ids:,}")
    print(f"LM/workbase:          {', '.join(result.lm_pcodes)}")
    print(f"Provider:             {', '.join(result.providers)}")
    print(f"Meter type:           {', '.join(result.meter_types)}")
    print(f"Range:                {result.from_month} to {result.to_month}")
    print(f"Included months:      {', '.join(result.included_months)}")
    print(f"CSV SHA-256:          {result.csv_sha256}")
    print(f"Stage 05 fingerprint: {result.stage05_build_fingerprint}")
    print("=======================================\n")


def main() -> None:
    args = parse_args()
    config = build_config(args)
    started_at = datetime.now(UTC)
    report_path = make_report_path(config.report_dir, config.project_id)
    progress = UploadProgress()
    firestore_started = False
    run_id = uuid4().hex
    refresh_state = RefreshRunState(run_id=run_id, rows_read=0)
    conflict_report_path: Optional[Path] = None

    report: dict[str, Any] = {
        "stage": "07",
        "script": "07_upload_meter_master_v3.py",
        "operation": "meter_master_upload",
        "projectId": config.project_id,
        "collection": COLLECTION_NAME,
        "mode": config.mode,
        "runId": run_id,
        "inputPath": str(config.input_path),
        "manifestPath": str(config.manifest_path),
        "serviceAccountPath": str(config.service_account_path),
        "startedAt": started_at.isoformat(),
        "status": "STARTED",
        "result": "STARTED",
    }

    try:
        require_file(config.input_path, "Input CSV")
        require_file(config.manifest_path, "Stage 05 manifest")
        require_file(config.service_account_path, "Service-account file")

        credential_project = read_service_account_project_id(config.service_account_path)
        report["credentialProjectId"] = credential_project
        if credential_project != config.project_id:
            raise ValueError(
                "Service-account project mismatch. "
                f"Requested={config.project_id!r}; credential={credential_project!r}"
            )

        df, csv_evidence = load_and_validate_csv(config.input_path)
        manifest_evidence = validate_stage05_manifest(
            config.manifest_path,
            config.input_path,
            csv_evidence,
        )
        preflight = PreflightResult(
            row_count=int(csv_evidence["rowCount"]),
            unique_master_ids=int(csv_evidence["uniqueMasterIds"]),
            csv_sha256=str(csv_evidence["csvSha256"]),
            document_ids_sha256=str(csv_evidence["documentIdsSha256"]),
            lm_pcodes=list(csv_evidence["lmPcodes"]),
            providers=list(csv_evidence["providers"]),
            meter_types=list(csv_evidence["meterTypes"]),
            from_month=str(manifest_evidence["fromMonth"]),
            to_month=str(manifest_evidence["toMonth"]),
            included_months=list(manifest_evidence["includedMonths"]),
            manifest_sha256=str(manifest_evidence["manifestSha256"]),
            stage05_build_fingerprint=str(manifest_evidence["stage05BuildFingerprint"]),
        )
        rows = dataframe_rows(df)
        refresh_state.rows_read = len(rows)
        upload_contract = make_upload_contract(config, preflight)
        upload_fingerprint = canonical_json_sha256(upload_contract)
        report.update(
            {
                "uploadContract": upload_contract,
                "uploadFingerprint": upload_fingerprint,
                "rowsRead": preflight.row_count,
                "uniqueMasterIds": preflight.unique_master_ids,
                "csvSha256": preflight.csv_sha256,
                "documentIdsSha256": preflight.document_ids_sha256,
                "manifestSha256": preflight.manifest_sha256,
                "stage05BuildFingerprint": preflight.stage05_build_fingerprint,
                "lmPcode": preflight.lm_pcodes[0],
                "fromMonth": preflight.from_month,
                "toMonth": preflight.to_month,
                "includedMonths": preflight.included_months,
            }
        )

        if config.mode == "resume":
            assert config.resume_report_path is not None
            report["resumeEvidence"] = validate_resume_report(
                config.resume_report_path,
                upload_contract,
                upload_fingerprint,
            )

        print_preflight(config, preflight)

        credentials = service_account.Credentials.from_service_account_file(
            str(config.service_account_path)
        )
        db = firestore.Client(project=config.project_id, credentials=credentials)
        firestore_started = True
        operation_timestamp = datetime.now(UTC)

        if config.mode == "create-only":
            if collection_has_documents(db):
                raise RuntimeError(
                    f"Upload blocked: {COLLECTION_NAME} is not empty in {config.project_id}. "
                    "An established collection requires a separate reviewed migration plan."
                )
            matching = 0
            missing_before = len(rows)
            create_documents(db, rows, operation_timestamp, progress)
        elif config.mode == "resume":
            plan = build_resume_plan(db, rows)
            report["matchingDocuments"] = plan.matching_count
            report["missingDocumentsBeforeWrite"] = len(plan.missing_rows)
            report["conflictCount"] = len(plan.conflicts)
            report["extraDocumentCount"] = len(plan.extra_document_ids)
            if plan.extra_document_ids:
                report["extraDocumentIds"] = plan.extra_document_ids[:100]
            if plan.conflicts:
                report["conflicts"] = plan.conflicts[:100]
                raise RuntimeError(
                    f"Resume blocked: {len(plan.conflicts)} conflicting document(s) found"
                )
            matching = plan.matching_count
            missing_before = len(plan.missing_rows)
            create_documents(db, plan.missing_rows, operation_timestamp, progress)

        if config.mode in ("create-only", "resume"):
            verification = verify_post_upload(db, rows)
            report.update(
                {
                    "documentsCreated": progress.documents_created,
                    "committedBatches": progress.committed_batches,
                    "matchingDocuments": matching,
                    "missingDocumentsBeforeWrite": missing_before,
                    "verification": verification,
                    "finalCollectionCount": verification["finalCount"],
                    "status": "PASS",
                    "result": "UPLOAD_VERIFIED",
                }
            )
            print("=== METER MASTER UPLOAD COMPLETE ===")
            print(f"Project:            {config.project_id}")
            print(f"Documents created:  {progress.documents_created:,}")
            print(f"Existing matching:  {matching:,}")
            print(f"Final collection:   {verification['finalCount']:,}")
            print("Sample verification: PASS")
        else:
            established_before = sum(1 for _ in db.collection(COLLECTION_NAME).stream())
            refresh_documents(db, rows, operation_timestamp, refresh_state)
            accounting = validate_refresh_accounting(refresh_state)
            if refresh_state.conflicts or refresh_state.failures:
                conflict_report_path = make_conflict_report_path(
                    config.report_dir, config.project_id, run_id
                )
                write_refresh_conflict_report(
                    conflict_report_path, refresh_state, config.project_id
                )
                report["conflictReportPath"] = str(conflict_report_path)
            established_after = sum(1 for _ in db.collection(COLLECTION_NAME).stream())
            verification = verify_refresh_post_write(
                db,
                refresh_state.successful_rows,
                established_before,
                established_after,
                refresh_state.created_count,
            )
            complete_refresh_report(report, refresh_state, accounting, verification)
            report.update(
                {
                    "inputRowCount": len(rows),
                    "systemicFailures": [],
                    "establishedCollectionCountBefore": established_before,
                    "finalCollectionCount": established_after,
                }
            )
            print("=== METER MASTER REFRESH COMPLETE ===")
            print(f"Records inspected:  {refresh_state.records_inspected:,}")
            for name in ("CREATED", "UPDATED", "UNCHANGED", "CONFLICT", "FAILED"):
                print(f"{name.title():19}{refresh_state.counts()[name]:,}")
            print(f"Final collection:   {established_after:,}")
        print(f"Run report:         {report_path}")

    except Exception as exc:
        if config.mode == "refresh":
            fail_refresh_report(report, refresh_state, exc)
        else:
            report.update(
                {
                    "status": "FAIL",
                    "result": "FAILED",
                    "errorType": type(exc).__name__,
                    "error": str(exc),
                }
            )
        report.update(
            {"documentsCreated": progress.documents_created, "committedBatches": progress.committed_batches}
        )
        if config.mode == "refresh" and firestore_started:
            failure_code = (
                "MM_SYSTEMIC_FIRESTORE_FAILURE"
                if isinstance(exc, SYSTEMIC_FIRESTORE_EXCEPTIONS)
                else "MM_REFRESH_RUN_FAILURE"
            )
            report["systemicFailures"] = [
                {"code": failure_code, "errorType": type(exc).__name__, "error": str(exc)}
            ]
        if (
            config.mode == "refresh"
            and (refresh_state.conflicts or refresh_state.failures)
            and conflict_report_path is None
        ):
            conflict_report_path = make_conflict_report_path(
                config.report_dir, config.project_id, run_id
            )
            try:
                write_refresh_conflict_report(
                    conflict_report_path, refresh_state, config.project_id
                )
                report["conflictReportPath"] = str(conflict_report_path)
            except Exception as report_exc:
                report["conflictReportError"] = str(report_exc)
        raise

    finally:
        report["finishedAt"] = datetime.now(UTC).isoformat()
        write_report(report_path, report)
        print(f"\n[REPORT] {report_path}")


if __name__ == "__main__":
    main()
