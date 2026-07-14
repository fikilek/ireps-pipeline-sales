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
    - resume only for verified recovery from a partial upload;
    - Firestore create operations only: no merge, update, delete, or silent skip;
    - all three target scopes are preflighted before any write;
    - post-upload counts and deterministic sample verification;
    - JSON audit report for every preflight or upload attempt.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


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


def read_csv_robust(path: Path) -> pd.DataFrame:
    last_error: Optional[Exception] = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return pd.read_csv(path, dtype=str, encoding=encoding)
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
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest = read_json(manifest_path)
    if clean_text(manifest.get("stage")) != "03":
        raise ValueError("Manifest is not a Stage 03 report")
    if clean_text(manifest.get("status")) != "PASS":
        raise ValueError("Stage 03 manifest status is not PASS")
    if clean_text(manifest.get("result")) != "BUILD_WRITTEN":
        raise ValueError("Stage 03 manifest must have result BUILD_WRITTEN")
    if clean_text(manifest.get("lmPcode")).upper() != expected_lm_pcode:
        raise ValueError("Stage 03 manifest LM does not match --lm-pcode")
    if clean_text(manifest.get("month")) != expected_month:
        raise ValueError("Stage 03 manifest month does not match --month")

    selected: dict[str, dict[str, Any]] = {}
    for item in manifest.get("outputs") or []:
        if not isinstance(item, dict):
            continue
        dataset = clean_text(item.get("dataset"))
        month = clean_text(item.get("month"))
        if dataset in COLLECTIONS and month == expected_month:
            if dataset in selected:
                raise ValueError(
                    f"Stage 03 manifest contains duplicate {dataset}/{expected_month} outputs"
                )
            selected[dataset] = item

    missing = sorted(set(COLLECTIONS) - set(selected))
    if missing:
        raise ValueError(
            f"Stage 03 manifest is missing monthly outputs for {missing}"
        )
    return manifest, selected


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

    expected_sha = clean_text(manifest_entry.get("sha256"))
    actual_sha = sha256_file(path)
    if actual_sha != expected_sha:
        raise ValueError(
            f"Stage 03 output SHA-256 mismatch for {path.name}: "
            f"manifest={expected_sha}, actual={actual_sha}"
        )

    frame = read_csv_robust(path)
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

    declared_rows = int(manifest_entry.get("rows", -1))
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
        if actual == expected:
            matching += 1
        else:
            conflicts += 1
            if len(conflict_examples) < 5:
                differing = sorted(
                    {
                        key
                        for key in set(actual) | set(expected)
                        if actual.get(key) != expected.get(key)
                    }
                )
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
        matches = actual == expected
        samples.append({"docId": document_id, "matches": matches})
        if not matches:
            differing = sorted(
                {
                    key
                    for key in set(actual) | set(expected)
                    if actual.get(key) != expected.get(key)
                }
            )
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
        manifest, selected = load_manifest_outputs(
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

        report.update(
            {
                "credentialProject": credential.project_id,
                "serviceAccountPath": str(credential.path),
                "stage03ManifestResult": manifest.get("result"),
                "stage03Month": manifest.get("month"),
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
