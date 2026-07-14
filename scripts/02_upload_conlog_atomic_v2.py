"""
Stage 02: safely upload one approved Conlog Atomic Sales CSV to Firestore.

Operating grain:
    one Firebase project + one LM + one month per execution

Input filename contract:
    output/atomic/
    atomic__conlog_prepaid_sales__<lmPcode>__YYYY-MM__<rows>.csv

Target collection:
    conlog_sales_atomic/{atomicId}

Safety model:
    - explicit target project, matching confirmation, and service-account path;
    - service-account project_id must match the requested project before Firebase starts;
    - exact 17-column Atomic schema and full-row validation;
    - Conlog provider document must exist and be active;
    - create-only for normal uploads;
    - resume only for verified recovery from a partial upload;
    - Firestore create operations only: no merge, update, delete, or silent skip;
    - one invalid or conflicting row blocks the entire month before writes begin;
    - post-upload count and deterministic sample verification;
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
DEFAULT_ATOMIC_DIR = PROJECT_ROOT / "output" / "atomic"
DEFAULT_LOG_DIR = PROJECT_ROOT / "output" / "logs" / "atomic_upload"

COLLECTION = "conlog_sales_atomic"
PROVIDER_COLLECTION = "vending_providers"
CONLOG_VENDING_PROVIDER_ID = "vpr_7f4d3c91a2b84e6f"
CONLOG_PROVIDER_CODE = "CONLOG"
BATCH_SIZE = 400
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

ATOMIC_BUSINESS_COLUMNS = [
    column
    for column in ATOMIC_COLUMNS
    if column not in {"ingestedAtISO", "ingestedAtMs"}
]

DOCUMENT_FIELDS = [column for column in ATOMIC_COLUMNS if column != "atomicId"]

ATOMIC_FILENAME_RE = re.compile(
    r"^atomic__conlog_prepaid_sales__"
    r"(?P<lm_pcode>[A-Za-z0-9_-]+)__"
    r"(?P<period>\d{4}-\d{2})__"
    r"(?P<rows>\d+)\.csv$"
)

SOURCE_FILENAME_RE = re.compile(
    r"^conlog_prepaid_sales__"
    r"(?P<lm_pcode>[A-Za-z0-9_-]+)__"
    r"(?P<period>\d{4}-\d{2})\.csv$"
)


@dataclass(frozen=True)
class CredentialIdentity:
    path: Path
    project_id: str


@dataclass
class AtomicFile:
    path: Path
    frame: pd.DataFrame
    lm_pcode: str
    period: str
    declared_rows: int
    file_sha256: str
    business_sha256: str
    unique_atomic_ids: int
    unique_meters: int
    amount_total_cents: int
    cost_total_cents: int
    vat_total_cents: int
    earliest_tx_at_iso: str
    latest_tx_at_iso: str


@dataclass
class ExistingState:
    count: int
    matching: int = 0
    missing: int = 0
    conflicts: int = 0
    extra: int = 0
    missing_ids: Optional[list[str]] = None
    conflict_examples: Optional[list[dict[str, Any]]] = None
    extra_examples: Optional[list[str]] = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and safely upload one Conlog Atomic Sales month to "
            "conlog_sales_atomic."
        )
    )
    parser.add_argument(
        "--project-id",
        required=True,
        help="Explicit Firebase project ID. No project is selected by default.",
    )
    parser.add_argument(
        "--confirm-project",
        required=True,
        help="Must exactly match --project-id.",
    )
    parser.add_argument(
        "--service-account",
        required=True,
        type=Path,
        help="Path to the service-account JSON for the requested Firebase project.",
    )
    parser.add_argument(
        "--lm-pcode",
        required=True,
        help="Expected LM pCode. It must match the filename and every CSV row.",
    )
    parser.add_argument(
        "--month",
        required=True,
        help="Atomic Sales month in YYYY-MM format.",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("create-only", "resume"),
        help=(
            "create-only is normal operation; resume is restricted to recovery "
            "from a verified partial upload of the same CSV."
        ),
    )
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument(
        "--preflight-only",
        action="store_true",
        help="Validate local data and Firestore target state without writing.",
    )
    operation.add_argument(
        "--execute-upload",
        action="store_true",
        help="Perform the upload only after all preflight checks pass.",
    )
    parser.add_argument(
        "--atomic-dir",
        type=Path,
        default=DEFAULT_ATOMIC_DIR,
        help="Directory containing upload-ready Atomic Sales CSVs.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help="Directory for Stage 02 JSON audit reports.",
    )
    parser.add_argument(
        "--vending-provider-id",
        default=CONLOG_VENDING_PROVIDER_ID,
        help=(
            "Stable Conlog document ID in vending_providers. "
            f"Governed value: {CONLOG_VENDING_PROVIDER_ID}."
        ),
    )
    return parser.parse_args()


def clean_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


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


def dataframe_csv_bytes(frame: pd.DataFrame, columns: list[str]) -> bytes:
    return frame[columns].to_csv(index=False, lineterminator="\n").encode("utf-8")


def business_sha256(frame: pd.DataFrame) -> str:
    return hashlib.sha256(
        dataframe_csv_bytes(frame, ATOMIC_BUSINESS_COLUMNS)
    ).hexdigest()


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as source:
            payload = json.load(source)
    except FileNotFoundError as exc:
        raise ValueError(f"Service-account file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Service-account file is not valid JSON: {path}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"Service-account JSON must contain an object: {path}")
    return payload


def validate_project_identity(args: argparse.Namespace) -> CredentialIdentity:
    project_id = clean_text(args.project_id)
    confirm_project = clean_text(args.confirm_project)

    if not project_id:
        raise ValueError("--project-id cannot be blank")
    if confirm_project != project_id:
        raise ValueError(
            "Project confirmation mismatch: "
            f"--project-id={project_id!r}, --confirm-project={confirm_project!r}"
        )

    service_account_path = args.service_account.expanduser().resolve()
    credential_payload = read_json(service_account_path)
    credential_project = clean_text(credential_payload.get("project_id"))

    if not credential_project:
        raise ValueError(
            f"Service-account JSON has no non-empty project_id: {service_account_path}"
        )
    if credential_project != project_id:
        raise ValueError(
            "Service-account project mismatch: "
            f"requested={project_id!r}, credential={credential_project!r}"
        )

    return CredentialIdentity(
        path=service_account_path,
        project_id=credential_project,
    )


def select_atomic_file(
    atomic_dir: Path,
    *,
    lm_pcode: str,
    period: str,
) -> Path:
    directory = atomic_dir.expanduser().resolve()
    if not directory.exists():
        raise ValueError(f"Atomic output directory not found: {directory}")
    if not directory.is_dir():
        raise ValueError(f"Atomic output path is not a directory: {directory}")

    pattern = f"atomic__conlog_prepaid_sales__{lm_pcode}__{period}__*.csv"
    matches = sorted(directory.glob(pattern))

    if not matches:
        raise ValueError(
            f"Expected Atomic Sales file not found in {directory}: {pattern}"
        )
    if len(matches) > 1:
        names = ", ".join(path.name for path in matches)
        raise ValueError(
            "Ambiguous Atomic Sales input. Expected exactly one monthly file, "
            f"found {len(matches)}: {names}"
        )

    return matches[0]


def read_csv_robust(path: Path) -> pd.DataFrame:
    last_error: Optional[Exception] = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return pd.read_csv(path, dtype=str, encoding=encoding)
        except Exception as exc:  # pragma: no cover - final error is re-raised
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
            f"{column} must contain integer text in every row. "
            f"Invalid CSV line examples: {examples}"
        )
    return raw.astype("int64")


def validate_atomic_ids(frame: pd.DataFrame) -> None:
    base_key = (
        frame["vendingProviderId"].astype(str)
        + "|"
        + frame["lmPcode"].astype(str)
        + "|"
        + frame["meterNo"].astype(str)
        + "|"
        + frame["txAtISO"].astype(str)
        + "|"
        + frame["amountTotalC"].astype(str)
        + "|"
        + frame["costC"].astype(str)
        + "|"
        + frame["vatC"].astype(str)
    )
    base_hash = base_key.map(sha1_text)
    duplicate_index = base_hash.groupby(base_hash, sort=False).cumcount() + 1
    expected = base_hash.where(
        duplicate_index.eq(1),
        base_hash + "__" + duplicate_index.astype(str),
    )

    mismatch = frame["atomicId"].ne(expected)
    if mismatch.any():
        examples = []
        for index in mismatch[mismatch].index[:5]:
            examples.append(
                {
                    "csvLine": int(index) + 2,
                    "actual": frame.at[index, "atomicId"],
                    "expected": expected.at[index],
                }
            )
        raise ValueError(
            "atomicId does not match the governed provider-aware identity rule. "
            f"Examples: {examples}"
        )


def validate_and_load_atomic(
    path: Path,
    *,
    expected_lm_pcode: str,
    expected_period: str,
    expected_provider_id: str,
) -> AtomicFile:
    filename_match = ATOMIC_FILENAME_RE.fullmatch(path.name)
    if not filename_match:
        raise ValueError(
            "Invalid Atomic filename. Expected "
            "atomic__conlog_prepaid_sales__<lmPcode>__YYYY-MM__<rows>.csv: "
            f"{path.name}"
        )

    filename_lm = filename_match.group("lm_pcode").upper()
    filename_period = filename_match.group("period")
    declared_rows = int(filename_match.group("rows"))

    if filename_lm != expected_lm_pcode:
        raise ValueError(
            f"Atomic filename LM {filename_lm!r} does not match "
            f"--lm-pcode {expected_lm_pcode!r}"
        )
    if filename_period != expected_period:
        raise ValueError(
            f"Atomic filename month {filename_period!r} does not match "
            f"--month {expected_period!r}"
        )

    frame = read_csv_robust(path)
    if list(frame.columns) != ATOMIC_COLUMNS:
        raise ValueError(
            "Atomic CSV schema mismatch. Expected exact columns and order: "
            f"{ATOMIC_COLUMNS}. Found: {list(frame.columns)}"
        )
    if frame.empty:
        raise ValueError(f"Atomic CSV contains zero rows: {path}")
    if len(frame) != declared_rows:
        raise ValueError(
            f"Atomic filename declares {declared_rows:,} rows but CSV contains "
            f"{len(frame):,}: {path.name}"
        )

    string_columns = [
        "atomicId",
        "vendingProviderId",
        "lmPcode",
        "meterNo",
        "txAtISO",
        "ym",
        "currency",
        "sourceFileId",
        "ingestedAtISO",
    ]
    for column in string_columns:
        cleaned = frame[column].map(clean_text)
        if cleaned.eq("").any():
            examples = [int(index) + 2 for index in cleaned.eq("")[cleaned.eq("")].index[:5]]
            raise ValueError(
                f"{column} contains blank values. CSV line examples: {examples}"
            )
        if frame[column].astype(str).ne(cleaned).any():
            raise ValueError(
                f"{column} contains leading or trailing whitespace. "
                "Atomic CSVs must be uploaded exactly as generated by Stage 01."
            )
        frame[column] = cleaned

    numeric_columns = [
        "txAtMs",
        "y",
        "m",
        "amountTotalC",
        "costC",
        "vatC",
        "sourceRow",
        "ingestedAtMs",
    ]
    for column in numeric_columns:
        frame[column] = strict_integer_series(frame, column)

    if frame["atomicId"].duplicated().any():
        duplicated = frame.loc[frame["atomicId"].duplicated(False), "atomicId"].head(5).tolist()
        raise ValueError(f"Atomic CSV contains duplicate atomicId values: {duplicated}")

    if not frame["vendingProviderId"].eq(expected_provider_id).all():
        values = sorted(frame["vendingProviderId"].unique().tolist())
        raise ValueError(
            "Atomic CSV contains an unexpected vendingProviderId. "
            f"Expected {expected_provider_id!r}; found {values}"
        )
    if not frame["lmPcode"].eq(expected_lm_pcode).all():
        values = sorted(frame["lmPcode"].unique().tolist())
        raise ValueError(
            f"Atomic CSV LM mismatch. Expected {expected_lm_pcode!r}; found {values}"
        )
    if not frame["ym"].eq(expected_period).all():
        values = sorted(frame["ym"].unique().tolist())
        raise ValueError(
            f"Atomic CSV month mismatch. Expected {expected_period!r}; found {values}"
        )

    expected_year, expected_month = (int(part) for part in expected_period.split("-"))
    if not frame["y"].eq(expected_year).all():
        raise ValueError(f"Atomic y values do not all equal {expected_year}")
    if not frame["m"].eq(expected_month).all():
        raise ValueError(f"Atomic m values do not all equal {expected_month}")

    meter_valid = frame["meterNo"].str.fullmatch(r"[A-Z0-9]+")
    if not meter_valid.all():
        examples = frame.loc[~meter_valid, "meterNo"].head(5).tolist()
        raise ValueError(
            "Atomic meterNo values must be non-empty uppercase alphanumeric strings. "
            f"Examples: {examples}"
        )

    parsed_tx = pd.to_datetime(
        frame["txAtISO"],
        format="%Y-%m-%dT%H:%M:%S",
        errors="coerce",
    )
    if parsed_tx.isna().any():
        examples = frame.loc[parsed_tx.isna(), "txAtISO"].head(5).tolist()
        raise ValueError(f"Invalid txAtISO values: {examples}")
    if not parsed_tx.dt.strftime("%Y-%m").eq(expected_period).all():
        raise ValueError("txAtISO contains transaction dates outside the requested month")

    expected_tx_ms = (parsed_tx.astype("int64") // 1_000_000).astype("int64")
    if not frame["txAtMs"].eq(expected_tx_ms).all():
        examples = []
        mismatch = frame["txAtMs"].ne(expected_tx_ms)
        for index in mismatch[mismatch].index[:5]:
            examples.append(
                {
                    "csvLine": int(index) + 2,
                    "txAtISO": frame.at[index, "txAtISO"],
                    "actualTxAtMs": int(frame.at[index, "txAtMs"]),
                    "expectedTxAtMs": int(expected_tx_ms.at[index]),
                }
            )
        raise ValueError(f"txAtMs does not match txAtISO: {examples}")

    money_nonnegative = (
        frame["amountTotalC"].ge(0)
        & frame["costC"].ge(0)
        & frame["vatC"].ge(0)
    )
    if not money_nonnegative.all():
        raise ValueError("Atomic money fields must contain non-negative integer cents")
    if not frame["amountTotalC"].eq(frame["costC"] + frame["vatC"]).all():
        raise ValueError("Atomic amountTotalC does not reconcile to costC + vatC")
    if not frame["currency"].eq("ZAR").all():
        values = sorted(frame["currency"].unique().tolist())
        raise ValueError(f"Atomic currency must be ZAR. Found: {values}")

    expected_source_file = (
        f"conlog_prepaid_sales__{expected_lm_pcode}__{expected_period}.csv"
    )
    if not frame["sourceFileId"].eq(expected_source_file).all():
        values = sorted(frame["sourceFileId"].unique().tolist())
        raise ValueError(
            f"sourceFileId must be {expected_source_file!r}. Found: {values}"
        )
    if not frame["sourceRow"].gt(0).all():
        raise ValueError("sourceRow must contain positive integers")
    expected_source_rows = pd.Series(range(1, len(frame) + 1), index=frame.index)
    if not frame["sourceRow"].eq(expected_source_rows).all():
        raise ValueError(
            "sourceRow must preserve the original Stage 01 row sequence 1..N"
        )

    parsed_ingested = pd.to_datetime(
        frame["ingestedAtISO"],
        format="%Y-%m-%dT%H:%M:%SZ",
        errors="coerce",
        utc=True,
    )
    if parsed_ingested.isna().any():
        examples = frame.loc[parsed_ingested.isna(), "ingestedAtISO"].head(5).tolist()
        raise ValueError(f"Invalid ingestedAtISO values: {examples}")
    if frame["ingestedAtISO"].nunique() != 1 or frame["ingestedAtMs"].nunique() != 1:
        raise ValueError(
            "One Atomic monthly file must contain one shared ingestion timestamp pair"
        )
    # Stage 01 historically serialised ingestedAtISO at whole-second precision
    # while ingestedAtMs retained the same clock instant's sub-second component.
    # Require both fields to identify the same UTC second. This accepts an exact
    # match or a governed 0..999 ms precision remainder, but still rejects any
    # second, minute, timezone, or date mismatch.
    ingested_second_ms = (
        parsed_ingested.astype("int64") // 1_000_000
    ).astype("int64")
    ingested_subsecond_ms = frame["ingestedAtMs"] - ingested_second_ms
    valid_ingested_pair = ingested_subsecond_ms.between(0, 999, inclusive="both")
    if not valid_ingested_pair.all():
        examples = []
        for index in valid_ingested_pair[~valid_ingested_pair].index[:5]:
            examples.append(
                {
                    "csvLine": int(index) + 2,
                    "ingestedAtISO": frame.at[index, "ingestedAtISO"],
                    "actualIngestedAtMs": int(frame.at[index, "ingestedAtMs"]),
                    "expectedSecondStartMs": int(ingested_second_ms.at[index]),
                    "millisecondDelta": int(ingested_subsecond_ms.at[index]),
                }
            )
        raise ValueError(
            "ingestedAtMs is not within the UTC second represented by "
            f"ingestedAtISO: {examples}"
        )

    validate_atomic_ids(frame)

    return AtomicFile(
        path=path,
        frame=frame,
        lm_pcode=expected_lm_pcode,
        period=expected_period,
        declared_rows=declared_rows,
        file_sha256=sha256_file(path),
        business_sha256=business_sha256(frame),
        unique_atomic_ids=int(frame["atomicId"].nunique()),
        unique_meters=int(frame["meterNo"].nunique()),
        amount_total_cents=int(frame["amountTotalC"].sum()),
        cost_total_cents=int(frame["costC"].sum()),
        vat_total_cents=int(frame["vatC"].sum()),
        earliest_tx_at_iso=str(frame["txAtISO"].min()),
        latest_tx_at_iso=str(frame["txAtISO"].max()),
    )


def row_to_document(row: pd.Series) -> dict[str, Any]:
    return {
        field: (
            int(row[field])
            if field
            in {
                "txAtMs",
                "y",
                "m",
                "amountTotalC",
                "costC",
                "vatC",
                "sourceRow",
                "ingestedAtMs",
            }
            else str(row[field])
        )
        for field in DOCUMENT_FIELDS
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
            "firebase-admin is required for Stage 02. Install the project dependencies "
            "before running this uploader."
        ) from exc

    app_name = f"stage02-{requested_project_id}-{run_id(utc_now())}"
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
    provider_code = clean_text(data.get("providerCode")).upper()
    status = clean_text(data.get("status")).lower()

    if actual_id != provider_id:
        raise ValueError(
            f"Provider document providerId mismatch: expected {provider_id!r}, "
            f"found {actual_id!r}"
        )
    if provider_code != CONLOG_PROVIDER_CODE:
        raise ValueError(
            f"Provider document providerCode mismatch: expected "
            f"{CONLOG_PROVIDER_CODE!r}, found {provider_code!r}"
        )
    if status != "active":
        raise ValueError(
            f"Provider document is not active: status={status!r}"
        )

    return {
        "path": f"{PROVIDER_COLLECTION}/{provider_id}",
        "providerId": actual_id,
        "providerCode": provider_code,
        "providerName": clean_text(data.get("providerName")),
        "status": status,
    }


def month_query(db: Any, *, lm_pcode: str, period: str):
    return (
        db.collection(COLLECTION)
        .where("lmPcode", "==", lm_pcode)
        .where("ym", "==", period)
    )


def query_count(query: Any) -> int:
    try:
        aggregation = query.count()
        results = aggregation.get()
        if results:
            first = results[0]
            if isinstance(first, (list, tuple)) and first:
                first = first[0]
            value = getattr(first, "value", None)
            if value is not None:
                return int(value)
    except Exception:
        # Compatibility fallback for older firebase-admin/google-cloud-firestore.
        pass

    return sum(1 for _ in query.stream())


def compare_existing_for_resume(
    query: Any,
    atomic: AtomicFile,
) -> ExistingState:
    csv_ids = set(atomic.frame["atomicId"].tolist())
    existing_ids: set[str] = set()
    matching = 0
    conflicts = 0
    conflict_examples: list[dict[str, Any]] = []

    frame_by_id = atomic.frame.set_index("atomicId", drop=False)

    for snapshot in query.stream():
        document_id = snapshot.id
        existing_ids.add(document_id)
        if document_id not in csv_ids:
            continue

        expected = row_to_document(frame_by_id.loc[document_id])
        actual = snapshot.to_dict() or {}
        if actual == expected:
            matching += 1
        else:
            conflicts += 1
            if len(conflict_examples) < 5:
                differing_fields = sorted(
                    {
                        key
                        for key in set(actual) | set(expected)
                        if actual.get(key) != expected.get(key)
                    }
                )
                conflict_examples.append(
                    {
                        "atomicId": document_id,
                        "differingFields": differing_fields,
                    }
                )

    extra_ids = sorted(existing_ids - csv_ids)
    missing_ids = sorted(csv_ids - existing_ids)

    state = ExistingState(
        count=len(existing_ids),
        matching=matching,
        missing=len(missing_ids),
        conflicts=conflicts,
        extra=len(extra_ids),
        missing_ids=missing_ids,
        conflict_examples=conflict_examples,
        extra_examples=extra_ids[:5],
    )

    if state.conflicts or state.extra:
        raise ValueError(
            "Resume preflight found Firestore conflicts. "
            f"conflicting documents={state.conflicts}, "
            f"documents not represented in CSV={state.extra}, "
            f"conflict examples={state.conflict_examples}, "
            f"extra examples={state.extra_examples}"
        )

    if state.matching + state.missing != len(atomic.frame):
        raise ValueError(
            "Resume reconciliation did not account for every Atomic CSV row"
        )

    return state


def inspect_existing_state(
    db: Any,
    *,
    atomic: AtomicFile,
    mode: str,
) -> ExistingState:
    query = month_query(db, lm_pcode=atomic.lm_pcode, period=atomic.period)

    if mode == "create-only":
        count = query_count(query)
        if count != 0:
            raise ValueError(
                "create-only requires an empty target LM/month scope. "
                f"Found {count:,} existing documents in "
                f"{COLLECTION} for {atomic.lm_pcode}/{atomic.period}. "
                "Use resume only for a verified partial upload of this exact CSV."
            )
        return ExistingState(
            count=0,
            matching=0,
            missing=len(atomic.frame),
            conflicts=0,
            extra=0,
            missing_ids=atomic.frame["atomicId"].tolist(),
            conflict_examples=[],
            extra_examples=[],
        )

    return compare_existing_for_resume(query, atomic)


def batched(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def create_documents(
    db: Any,
    *,
    atomic: AtomicFile,
    document_ids: list[str],
) -> tuple[int, int]:
    if not document_ids:
        print("[UPLOAD] No missing documents. Firestore already matches the CSV.")
        return 0, 0

    frame_by_id = atomic.frame.set_index("atomicId", drop=False)
    total_batches = (len(document_ids) + BATCH_SIZE - 1) // BATCH_SIZE
    created = 0
    committed_batches = 0

    for batch_number, id_chunk in enumerate(
        batched(document_ids, BATCH_SIZE), start=1
    ):
        batch = db.batch()
        for document_id in id_chunk:
            row = frame_by_id.loc[document_id]
            document = row_to_document(row)
            document_ref = db.collection(COLLECTION).document(document_id)
            batch.create(document_ref, document)

        # Deliberately no blind retry. An ambiguous commit outcome must be
        # recovered with the governed resume mode.
        batch.commit()
        committed_batches += 1
        created += len(id_chunk)
        print(
            f"  - batch {batch_number}/{total_batches}: created {len(id_chunk):,} "
            f"(total {created:,}/{len(document_ids):,})"
        )

    return created, committed_batches


def deterministic_sample_ids(frame: pd.DataFrame) -> list[str]:
    ids = frame["atomicId"].tolist()
    positions = sorted({0, len(ids) // 2, len(ids) - 1})
    return [ids[position] for position in positions]


def verify_post_upload(
    db: Any,
    *,
    atomic: AtomicFile,
) -> dict[str, Any]:
    query = month_query(db, lm_pcode=atomic.lm_pcode, period=atomic.period)
    final_count = query_count(query)
    expected_count = len(atomic.frame)
    if final_count != expected_count:
        raise ValueError(
            "Post-upload count verification failed: "
            f"expected {expected_count:,}, found {final_count:,}"
        )

    frame_by_id = atomic.frame.set_index("atomicId", drop=False)
    sample_results: list[dict[str, Any]] = []
    for document_id in deterministic_sample_ids(atomic.frame):
        snapshot = db.collection(COLLECTION).document(document_id).get()
        if not snapshot.exists:
            raise ValueError(
                f"Post-upload sample document is missing: {COLLECTION}/{document_id}"
            )
        expected = row_to_document(frame_by_id.loc[document_id])
        actual = snapshot.to_dict() or {}
        matches = actual == expected
        sample_results.append(
            {
                "atomicId": document_id,
                "matches": matches,
            }
        )
        if not matches:
            differing_fields = sorted(
                {
                    key
                    for key in set(actual) | set(expected)
                    if actual.get(key) != expected.get(key)
                }
            )
            raise ValueError(
                "Post-upload sample verification failed for "
                f"{document_id}: differing fields={differing_fields}"
            )

    return {
        "expectedCount": expected_count,
        "finalCount": final_count,
        "countVerification": "PASS",
        "sampleVerification": "PASS",
        "samples": sample_results,
    }


def print_preflight(
    *,
    args: argparse.Namespace,
    credential: CredentialIdentity,
    atomic: AtomicFile,
    provider: dict[str, Any],
    existing: ExistingState,
) -> None:
    operation = "execute-upload" if args.execute_upload else "preflight-only"
    print("[STAGE 02] ATOMIC SALES CSV -> FIRESTORE")
    print(f"  operation:            {operation}")
    print(f"  upload mode:          {args.mode}")
    print(f"  target project:       {args.project_id}")
    print(f"  credential project:   {credential.project_id}")
    print(f"  target collection:    {COLLECTION}")
    print(f"  provider document:    {provider['path']}")
    print(f"  provider code/status: {provider['providerCode']} / {provider['status']}")
    print(f"  LM / month:           {atomic.lm_pcode} / {atomic.period}")
    print(f"  input file:           {atomic.path}")
    print(f"  CSV SHA-256:          {atomic.file_sha256}")
    print(f"  business SHA-256:     {atomic.business_sha256}")
    print(f"  rows:                 {len(atomic.frame):,}")
    print(f"  unique atomic IDs:    {atomic.unique_atomic_ids:,}")
    print(f"  unique meters:        {atomic.unique_meters:,}")
    print(f"  amount total:         {atomic.amount_total_cents:,} cents / R {atomic.amount_total_cents / 100:,.2f}")
    print(f"  cost total:           {atomic.cost_total_cents:,} cents / R {atomic.cost_total_cents / 100:,.2f}")
    print(f"  VAT total:            {atomic.vat_total_cents:,} cents / R {atomic.vat_total_cents / 100:,.2f}")
    print(f"  earliest transaction: {atomic.earliest_tx_at_iso}")
    print(f"  latest transaction:   {atomic.latest_tx_at_iso}")
    print(f"  existing documents:   {existing.count:,}")
    print(f"  matching documents:   {existing.matching:,}")
    print(f"  documents to create:  {existing.missing:,}")
    print(f"  conflicts:            {existing.conflicts:,}")
    print(f"  extra documents:      {existing.extra:,}")


def base_report(
    *,
    args: argparse.Namespace,
    started_at: dt.datetime,
) -> dict[str, Any]:
    return {
        "stage": "02",
        "script": "02_upload_conlog_atomic_v2.py",
        "status": "STARTED",
        "operation": "execute-upload" if args.execute_upload else "preflight-only",
        "mode": args.mode,
        "targetProject": clean_text(args.project_id),
        "confirmProject": clean_text(args.confirm_project),
        "targetCollection": COLLECTION,
        "providerId": clean_text(args.vending_provider_id),
        "lmPcode": clean_text(args.lm_pcode).upper(),
        "month": clean_text(args.month),
        "startedAt": utc_iso(started_at),
    }


def report_path(
    *,
    log_dir: Path,
    report: dict[str, Any],
    started_at: dt.datetime,
) -> Path:
    project = re.sub(r"[^A-Za-z0-9_-]+", "_", report.get("targetProject") or "unknown")
    lm_pcode = re.sub(r"[^A-Za-z0-9_-]+", "_", report.get("lmPcode") or "unknown")
    period = re.sub(r"[^0-9-]+", "_", report.get("month") or "unknown")
    return (
        log_dir.expanduser().resolve()
        / f"stage02_atomic_upload__{project}__{lm_pcode}__{period}__{run_id(started_at)}.json"
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
    started_at = utc_now()
    report = base_report(args=args, started_at=started_at)
    report_file = report_path(
        log_dir=args.log_dir,
        report=report,
        started_at=started_at,
    )

    firebase_admin_module = None
    firebase_app = None
    db = None
    created = 0
    committed_batches = 0

    try:
        validate_month(args.month)
        expected_lm = clean_text(args.lm_pcode).upper()
        if not expected_lm:
            raise ValueError("--lm-pcode cannot be blank")

        provider_id = clean_text(args.vending_provider_id)
        if provider_id != CONLOG_VENDING_PROVIDER_ID:
            raise ValueError(
                "This Conlog Stage 02 uploader is governed for provider ID "
                f"{CONLOG_VENDING_PROVIDER_ID!r}. Received {provider_id!r}."
            )

        # Project and credential identity are validated before Firebase starts.
        credential = validate_project_identity(args)
        atomic_path = select_atomic_file(
            args.atomic_dir,
            lm_pcode=expected_lm,
            period=args.month,
        )
        atomic = validate_and_load_atomic(
            atomic_path,
            expected_lm_pcode=expected_lm,
            expected_period=args.month,
            expected_provider_id=provider_id,
        )

        report.update(
            {
                "credentialProject": credential.project_id,
                "serviceAccountPath": str(credential.path),
                "inputPath": str(atomic.path),
                "csvSha256": atomic.file_sha256,
                "atomicBusinessSha256": atomic.business_sha256,
                "rowsRead": len(atomic.frame),
                "uniqueAtomicIds": atomic.unique_atomic_ids,
                "uniqueMeters": atomic.unique_meters,
                "amountTotalC": atomic.amount_total_cents,
                "costC": atomic.cost_total_cents,
                "vatC": atomic.vat_total_cents,
                "earliestTransaction": atomic.earliest_tx_at_iso,
                "latestTransaction": atomic.latest_tx_at_iso,
            }
        )

        firebase_admin_module, firebase_app, db = initialize_firestore(
            credential=credential,
            requested_project_id=args.project_id,
        )
        provider = validate_provider_document(db, provider_id)
        existing = inspect_existing_state(db, atomic=atomic, mode=args.mode)

        report.update(
            {
                "provider": provider,
                "documentsBefore": existing.count,
                "matchingDocuments": existing.matching,
                "documentsPlanned": existing.missing,
                "conflictCount": existing.conflicts,
                "extraDocumentCount": existing.extra,
                "conflictExamples": existing.conflict_examples or [],
                "extraDocumentExamples": existing.extra_examples or [],
            }
        )

        print_preflight(
            args=args,
            credential=credential,
            atomic=atomic,
            provider=provider,
            existing=existing,
        )

        if args.preflight_only:
            report["status"] = "PASS"
            report["result"] = "PREFLIGHT_OK"
            print("\n[PREFLIGHT OK] Local Atomic data and Firestore target state are safe.")
            print("No Firestore Atomic Sales documents were written.")
        else:
            missing_ids = existing.missing_ids or []
            print(
                f"\n[UPLOAD] Creating {len(missing_ids):,} documents in "
                f"{args.project_id}/{COLLECTION}"
            )
            created, committed_batches = create_documents(
                db,
                atomic=atomic,
                document_ids=missing_ids,
            )
            verification = verify_post_upload(db, atomic=atomic)
            report.update(
                {
                    "documentsCreated": created,
                    "committedBatches": committed_batches,
                    "documentsAfter": verification["finalCount"],
                    "verification": verification,
                    "status": "PASS",
                    "result": "UPLOAD_VERIFIED",
                }
            )
            print("\n[VERIFY PASS] Firestore count and deterministic samples match the CSV.")
            print(
                f"[OK] Stage 02 completed: created {created:,} documents; "
                f"final month count {verification['finalCount']:,}."
            )

        return 0

    except Exception as exc:
        report.update(
            {
                "status": "FAIL",
                "result": "FAILED",
                "documentsCreated": created,
                "committedBatches": committed_batches,
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
                f"[WARN] Could not write Stage 02 report: {report_error}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    raise SystemExit(main())
